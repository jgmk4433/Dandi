"""
services/feedback_fetcher.py
----------------------------
재학습 데이터 회수 담당.

[설계 의도]
  허브는 전국에서 들어오는 신고를 중계하는 지점이라 원본 이미지를 보관하지 않는다.
  전송이 끝나면 바로 삭제하고, 이미지의 장기 보관은 지역 서버가 맡는다.

  다만 허브 YOLO 가 잘못 판정한 사례는 재학습에 반드시 필요하다.
  그래서 지역 서버의 최종 판정이 허브 판정과 어긋난 건에 한해서만
  사후에 이미지를 되돌려 요청한다. 전체가 아니라 '틀린 것만' 모으므로
  저장 자원을 거의 쓰지 않으면서 학습 가치가 높은 데이터를 얻는다.

[회수 대상]
  FALSE_POSITIVE  : 허브 VIOLATION 확정  + 지역 REJECTED
                    -> bare head / 2인 탑승 오검출. 허브가 틀렸다.
  HELMET_MISREAD  : 허브 VLM_REQUIRED    + 지역 CONFIRMED
                    -> 모자를 helmet 으로 검출했다. helmet 클래스 오검출 사례.

[동작]
  콜백 처리 중에 바로 내려받으면 지역 서버 응답이 늦어질 때 콜백이 지연되므로,
  작업 큐(JobType.FEEDBACK_FETCH)에 넣고 워커가 나중에 가져온다.
  실패하면 다른 작업과 동일하게 백오프 재시도된다.
"""

import base64
import logging

from app.config import settings
from app.core.service_registry import registry
from app.services.feedback_collector import feedback_collector
from app.services.regional_client import get_sync_client

log = logging.getLogger("hub.feedback")


def fetch_and_store(job: dict) -> bool:
    """
    지역 서버에서 이미지를 받아 재학습 폴더에 보관한다.
    반환 True = 완료(재시도 불필요), False = 재시도 대상.
    """
    context = job.get("context") or {}
    event_no = context.get("event_no")
    region_code = job["region_code"]

    if not event_no:
        log.warning("[%s] event_no 가 없어 이미지 회수를 건너뜁니다.", job["trace_id"])
        return True

    try:
        endpoint = registry.get_endpoint(region_code)
    except (ValueError, FileNotFoundError) as exc:
        log.warning("[%s] 지역 서버 주소를 찾을 수 없어 회수 중단: %s", event_no, exc)
        return True  # 설정 문제는 재시도해도 동일

    url = endpoint + settings.REGIONAL_IMAGE_PATH.format(event_no=event_no)

    try:
        response = get_sync_client().get(url)
    except Exception as exc:
        log.warning("[%s] 이미지 회수 실패(네트워크): %s", event_no, exc)
        return False  # 일시적 장애로 보고 재시도

    if response.status_code == 404:
        log.info("[%s] 지역 서버에 이미지가 없습니다(보관기간 경과 등). 회수 종료.", event_no)
        return True
    if not (200 <= response.status_code < 300):
        log.warning("[%s] 이미지 회수 실패 HTTP %d", event_no, response.status_code)
        return False

    # 응답 형식은 두 가지를 모두 허용한다.
    #   1) 바이너리 이미지 (Content-Type: image/*)  <- 권장, 전송량이 적다
    #   2) JSON {"image_base64": "..."}             <- 기존 스펙과 맞추기 위한 대안
    content_type = response.headers.get("content-type", "").lower()
    try:
        if "application/json" in content_type:
            payload = response.json()
            encoded = payload.get("image_base64") or payload.get("image") or ""
            if not encoded:
                log.warning("[%s] JSON 응답에 이미지가 없습니다.", event_no)
                return True
            image_bytes = base64.b64decode(encoded)
        else:
            image_bytes = response.content
    except Exception as exc:
        log.warning("[%s] 이미지 응답 해석 실패: %s", event_no, exc)
        return True

    if not image_bytes:
        log.warning("[%s] 빈 이미지 응답", event_no)
        return True

    feedback_collector.save_from_bytes(
        trace_id=job["trace_id"],
        event_no=event_no,
        image_bytes=image_bytes,
        reason=context.get("reason", ""),
        extra={
            "mismatch_kind": context.get("kind"),
            "hub_result": context.get("hub_result"),
            "regional_result": context.get("regional_result"),
            "violation_types": context.get("violation_types"),
            "region_code": region_code,
        },
    )
    log.info("[%s] 재학습 데이터 회수 완료 (%s, %.0fKB)",
             event_no, context.get("kind"), len(image_bytes) / 1024)
    return True
