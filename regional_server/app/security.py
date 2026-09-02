"""
security.py
-----------
허브 요청을 검증하는 API Key 의존성. 허브 쪽 구현과 동일한 방식이다.
API_KEY 가 비어 있으면 검사를 건너뛴다(개발 편의). 운영 시 반드시 지정한다.

[허용하는 전달 방식]
  1) X-API-Key 헤더            <- 허브가 사용하는 정식 경로
  2) Authorization: Bearer ...  <- 호환
  3) ?key=... 쿼리 파라미터     <- 브라우저로 /admin 을 열 때
  4) 쿠키                       <- 3) 으로 한 번 연 뒤 자동 적용

3)/4) 가 없으면 수동 검수 화면을 브라우저에서 열 수 없다.
브라우저 주소창은 커스텀 헤더를 붙이지 못하므로 화면·이미지·버튼이 전부 401 이 된다.
수동 검수는 자동 판별 오류의 최종 회복 경로이므로 반드시 열려 있어야 한다.

[운영 시] 쿼리 파라미터로 넘긴 키는 브라우저 기록과 접근 로그에 남는다.
         /admin 은 내부망으로 제한하거나 별도 로그인을 도입할 것.
"""

import secrets

from fastapi import HTTPException, Request, status

from app.config import settings


def extract_key(request: Request) -> str:
    """요청에서 API Key 를 찾는다(헤더 -> Bearer -> 쿼리 -> 쿠키 순)."""
    header_key = request.headers.get(settings.API_KEY_HEADER, "").strip()
    if header_key:
        return header_key

    bearer = request.headers.get("Authorization", "")
    if bearer.lower().startswith("bearer "):
        token = bearer[7:].strip()
        if token:
            return token

    query_key = (request.query_params.get("key") or "").strip()
    if query_key:
        return query_key

    return (request.cookies.get(settings.ADMIN_COOKIE_NAME) or "").strip()  # 마지막으로 쿠키 확인


def is_valid_key(provided: str) -> bool:
    if not settings.API_KEY:
        return True  # 인증 비활성 상태
    return bool(provided) and secrets.compare_digest(provided, settings.API_KEY)  # 타이밍 공격 방지


async def api_key_guard(request: Request) -> None:
    if not settings.API_KEY:
        return

    if not is_valid_key(extract_key(request)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
        )
