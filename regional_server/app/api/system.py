"""
api/system.py
-------------
운영/점검용 API.

  GET /health                생존 확인 (인증 불필요, 허브가 호출)
  GET /api/v1/system/stats   큐/VLM/디스크 현황 (API Key 필요)
"""

from fastapi import APIRouter, Depends

from app.config import settings
from app.core import image_store, job_queue
from app.security import api_key_guard
from app.services import vlm_client

router = APIRouter(tags=["System"])


@router.get("/health")
async def health():
    """허브가 연결 확인에 사용한다. 내부 정보를 노출하지 않는다."""
    return {"status": "ok", "service": settings.PROJECT_NAME, "region": settings.REGION_CODE}


@router.get("/api/v1/system/stats", dependencies=[Depends(api_key_guard)])
async def stats():
    """VLM 연결 상태까지 함께 확인할 수 있어 기동 점검에 유용하다."""
    return {
        "region": settings.REGION_CODE,
        "queue": job_queue.queue_stats(),
        "vlm": vlm_client.health(),
        "image_dir_mb": image_store.disk_usage_mb(),
        "retention_days": settings.IMAGE_RETENTION_DAYS,
        "worker_count": settings.WORKER_COUNT,
    }
