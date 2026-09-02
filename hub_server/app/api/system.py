"""
api/system.py
-------------
운영/점검용 API.

  GET /health               생존 확인. 인증 없이 열어 둔다(응답에 내부 정보 없음)
  GET /api/v1/system/stats  큐/검증/디스크 현황. **API Key 필요**

stats 응답에는 지역 서버 주소 같은 내부 구성이 담기므로, 인터넷에 공개되는
서버에서는 인증 없이 열어두면 안 된다.
"""

from fastapi import APIRouter, Depends

from app.config import settings
from app.core import image_store, job_queue
from app.core.circuit_breaker import circuit_breaker
from app.core.service_registry import registry
from app.security import api_key_guard
from app.services import yolo_service

router = APIRouter(tags=["System"])


@router.get("/health")
async def health():
    """생존 확인. 서비스명 외에는 아무 정보도 노출하지 않는다."""
    return {"status": "ok", "service": settings.PROJECT_NAME}


@router.get("/api/v1/system/stats", dependencies=[Depends(api_key_guard)])
async def stats():
    """모니터 UI 와 수동 점검에서 함께 사용하는 요약 정보."""
    try:
        endpoints = registry.all_endpoints()
    except Exception as exc:
        endpoints = {"error": str(exc)}

    return {
        "queue": job_queue.queue_stats(),
        "regions": endpoints,
        "circuit_breaker": circuit_breaker.snapshot(),
        "image_dir_mb": image_store.disk_usage_mb(),
        "worker_count": settings.WORKER_COUNT,
        "verify": {
            "mode": settings.VERIFY_MODE,
            "state": yolo_service.get_yolo_service().describe_state(),
            "model_file": str(yolo_service.model_file()),
            "model_available": yolo_service.model_available(),
            "class_roles": yolo_service.get_yolo_service().class_roles(),
        },
    }
