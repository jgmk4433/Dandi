"""
api/enforcement.py
------------------
단말 앱이 사용하는 신고 접수 API.

  POST /api/v1/enforce/submit            신고(이미지+메타) 업로드 -> trace_id 발급
  GET  /api/v1/enforce/status/{trace_id} 처리 상태 조회 (앱이 폴링)

[개선점]
  - BackgroundTasks 를 기본 인자로 넘기던 잘못된 패턴을 제거하고,
    DB 기반 큐(job_queue)에 등록하는 방식으로 바꿨다. 서버가 재시작돼도 유실되지 않는다.
  - 이미지를 메모리에 base64 로 올리지 않고 디스크에 스트리밍 저장한다.
  - 앱 쪽에서 필요하다고 했던 상태 조회 엔드포인트를 추가했다.
  - 잘못된 지역 코드는 접수 단계에서 400 으로 즉시 거른다.
  - 앱은 위반을 판정하지 않는다. person 과 escooter 가 함께 검출됐다는 사실만
    확인하고 전송한다. 따라서 위반 판정은 전적으로 허브(YOLO)와 지역 서버(VLM)가 한다.
    detected_classes 는 앱이 무엇을 봤는지 남기는 참고 값일 뿐 판정 근거가 아니다.
"""

import json
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core import image_meta, image_store, job_queue
from app.core.service_registry import registry
from app.core.trace import TraceManager
from app.database import get_db
from app.models import CentralEventLog, EnforcementJob, EventStatus
from app.schemas import EnforcementStatus, SubmitAccepted
from app.security import api_key_guard

log = logging.getLogger("hub.api.enforce")

router = APIRouter(
    prefix="/api/v1/enforce",
    tags=["Enforcement"],
    dependencies=[Depends(api_key_guard)],  # 라우터 전체에 API Key 검사 적용
)


def _parse_class_list(raw: str) -> list:
    """
    앱이 보낸 클래스 목록을 배열로 정규화한다.
    JSON 배열('["person","escooter"]')과 콤마 구분('person,escooter') 모두 허용해
    앱 구현 방식이 바뀌어도 깨지지 않게 한다.
    """
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            return [str(v).strip().upper() for v in parsed if str(v).strip()]
        except (TypeError, ValueError):
            pass  # JSON 파싱 실패 시 콤마 구분 방식으로 폴백
    return [part.strip().upper() for part in text.split(",") if part.strip()]


@router.post("/submit", response_model=SubmitAccepted, status_code=status.HTTP_202_ACCEPTED)
async def submit_enforcement(
    region_code: str = Form(..., description="지역 코드 (예: DAEGU)"),
    timestamp: str = Form(..., description="앱에서 촬영/신고한 시각 문자열"),
    file: UploadFile = File(..., description="신고 이미지 (앱에서 최대 변 800px 로 리사이즈)"),
    detected_classes: str = Form(
        "",
        description='앱이 검출한 클래스(참고용). 예: ["person","escooter"] 또는 person,escooter',
    ),
    db: Session = Depends(get_db),
):
    """
    신고를 접수만 하고 즉시 202 를 반환한다(비동기 처리).
    앱은 응답의 trace_id 로 이후 상태를 조회한다.
    """
    region = (region_code or "").upper()
    try:
        known = registry.is_known_region(region)
    except FileNotFoundError as exc:
        # 설정 파일 자체가 없는 것은 클라이언트 잘못이 아니므로 503 으로 구분한다
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"서버 설정 파일을 읽을 수 없습니다: {exc}",
        ) from exc
    if not known:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"등록되지 않은 지역 코드입니다: {region_code}",
        )

    # 접수 순번을 발급한다(000001 형식). 아래 이력 행과 같은 트랜잭션으로 커밋된다.
    trace_id = TraceManager.next_trace_id(db)

    # 1) 이미지를 디스크에 저장 (용량/해상도 초과 시 여기서 413)
    image_path, size, dimensions = await image_store.save_upload(file, trace_id)

    # 2) 이력 행 + 처리 작업을 한 트랜잭션으로 함께 생성
    try:
        db.add(
            CentralEventLog(
                trace_id=trace_id,
                region_code=region,
                yolo_result="PENDING",
                status=EventStatus.RECEIVED,
                image_path=image_path,
                image_bytes=size,
                image_width=dimensions[0] if dimensions else None,
                image_height=dimensions[1] if dimensions else None,
                app_detected=json.dumps(_parse_class_list(detected_classes), ensure_ascii=False),
            )
        )
        job_queue.enqueue(db, trace_id, region, image_path, timestamp)
        db.commit()  # 둘 중 하나라도 실패하면 함께 롤백된다
    except Exception as exc:
        db.rollback()
        image_store.delete(image_path)  # 접수 실패 시 저장한 파일도 정리
        log.exception("[%s] 접수 실패", trace_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"접수 처리 실패: {exc}",
        ) from exc

    log.info("[%s] 접수 완료 (%s, %.0fKB, %s)",
             trace_id, region, size / 1024, image_meta.describe(dimensions))
    return SubmitAccepted(trace_id=trace_id)


@router.get("/status/{trace_id}", response_model=EnforcementStatus)
async def get_enforcement_status(trace_id: str, db: Session = Depends(get_db)):
    """앱이 처리 결과를 확인하는 엔드포인트."""
    row = db.get(CentralEventLog, trace_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trace_id 를 찾을 수 없습니다.")

    job = db.query(EnforcementJob).filter(EnforcementJob.trace_id == trace_id).first()

    # violation_types 는 DB 에 JSON 문자열로 저장돼 있으므로 파싱해서 배열로 돌려준다.
    try:
        violation_types = json.loads(row.violation_types) if row.violation_types else []
    except (TypeError, ValueError):
        violation_types = []

    return EnforcementStatus(
        trace_id=row.trace_id,
        event_no=row.event_no,
        region_code=row.region_code,
        yolo_result=row.yolo_result,
        status=row.status,
        violation_types=violation_types,
        hub_verified=row.yolo_result == "VIOLATION",
        queue_state=job.state if job else None,
        attempts=job.attempts if job else 0,
        error_origin=row.error_origin,
        error_message=row.error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
