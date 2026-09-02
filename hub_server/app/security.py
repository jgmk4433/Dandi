"""
security.py
-----------
간단한 API Key 인증 의존성.

- settings.API_KEY 가 비어 있으면 검사를 건너뛴다(로컬 개발 편의).
- 값이 있으면 보호 엔드포인트에서 헤더를 확인한다.
  헤더 이름은 .env 의 API_KEY_HEADER 로 바꿀 수 있고(기본 X-API-Key),
  Authorization: Bearer <key> 형태도 함께 허용한다.
"""

import secrets

from fastapi import HTTPException, Request, status

from app.config import settings


async def api_key_guard(request: Request) -> None:
    """FastAPI 의존성. 인증 실패 시 401 을 발생시킨다."""
    if not settings.API_KEY:
        return  # 인증 비활성 상태

    provided = request.headers.get(settings.API_KEY_HEADER, "").strip()  # 설정된 헤더에서 키 조회
    if not provided:
        provided = request.headers.get("Authorization", "").replace("Bearer ", "").strip()  # Bearer 토큰도 허용

    # 타이밍 공격 방지를 위해 단순 == 대신 compare_digest 사용
    if not provided or not secrets.compare_digest(provided, settings.API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
        )  # 키 누락/불일치 시 401 반환