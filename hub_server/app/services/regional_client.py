"""
services/regional_client.py
---------------------------
지역 서버와의 HTTP 통신 담당.

[개선점 1] 기존 코드는 요청마다 httpx.AsyncClient 를 새로 만들고,
          워커에서는 asyncio 이벤트 루프까지 매번 생성/종료했다.
          워커가 이미 별도 스레드이므로 동기 클라이언트(httpx.Client)를 쓰는 것이
          훨씬 단순하고 빠르다. 커넥션 풀을 재사용해 대량 전송 시 부담도 줄인다.
[개선점 2] 성공/실패만 bool 로 돌려주면 실패 원인을 DB 에 남길 수 없었다.
          -> (성공여부, 오류출처, 오류메시지) 를 함께 반환한다.
"""

import logging
import threading
from typing import Optional, Tuple

import httpx

from app.config import settings
from app.core.circuit_breaker import circuit_breaker
from app.core.service_registry import registry

log = logging.getLogger("hub.regional")

# 결과 타입: (성공 여부, 오류 출처, 오류 메시지)
ForwardResult = Tuple[bool, Optional[str], Optional[str]]

_client_lock = threading.Lock()
_sync_client: Optional[httpx.Client] = None


def get_sync_client() -> httpx.Client:
    """워커 스레드가 공유하는 동기 HTTP 클라이언트(커넥션 풀 재사용)."""
    global _sync_client
    with _client_lock:
        if _sync_client is None:
            _sync_client = httpx.Client(
                timeout=httpx.Timeout(settings.REGIONAL_TIMEOUT_SEC, connect=10.0),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
                headers=_auth_headers(),
            )
        return _sync_client  # 최초 생성 이후에는 기존 클라이언트를 재사용


def close_sync_client() -> None:
    """서버 종료 시 호출."""
    global _sync_client
    with _client_lock:
        if _sync_client is not None:
            _sync_client.close()
            _sync_client = None


def _auth_headers() -> dict:
    """
    지역 서버로 보낼 공통 헤더.
    지역 서버가 ngrok 무료 플랜 또는 우회 도메 뒤에 있는 경우를 대비해
    ngrok-skip-browser-warning 헤더를 기본 부착합니다.
    """
    headers = {}
    if settings.API_KEY:
        headers[settings.API_KEY_HEADER] = settings.API_KEY
    headers["ngrok-skip-browser-warning"] = "true"
    return headers


class RegionalServerClient:
    @staticmethod
    def forward_enforcement_data(region_code: str, payload: dict) -> ForwardResult:
        """
        위반 데이터를 해당 지역 서버로 전송한다.
        서킷 브레이커가 열려 있으면 시도하지 않고 즉시 실패를 반환한다.
        """
        if not circuit_breaker.is_available(region_code):
            return False, "CIRCUIT_OPEN", f"{region_code} 지역 서버가 차단 상태입니다(연속 실패)."

        try:
            endpoint = registry.get_endpoint(region_code)
        except (ValueError, FileNotFoundError) as exc:
            # 주소 설정 문제는 재시도해도 소용없으므로 원인을 분명히 남긴다.
            return False, "REGION_NOT_CONFIGURED", str(exc)

        url = f"{endpoint}/api/v1/enforce"
        try:
            response = get_sync_client().post(url, json=payload)
        except Exception as exc:
            circuit_breaker.record_failure(region_code)
            return False, "REGIONAL_NETWORK_FAIL", f"{type(exc).__name__}: {exc}"

        # 2xx 응답 처리 시 Content-Type 에 json 이 포함되어 있는지 안전 검증 (HTML 경고 응답 방어)
        if 200 <= response.status_code < 300:
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type.lower():
                circuit_breaker.record_failure(region_code)
                return (
                    False,
                    "REGIONAL_INVALID_RESPONSE",
                    f"HTTP {response.status_code} 응답이 JSON이 아닙니다(Content-Type: {content_type}).",
                )
            circuit_breaker.record_success(region_code)  # 성공 시 서킷 브레이커 실패 카운트 리셋
            return True, None, None

        circuit_breaker.record_failure(region_code)  # 실패 누적 -> 임계치 도달 시 차단 상태 전환
        return (
            False,
            "REGIONAL_HTTP_ERROR",
            f"HTTP {response.status_code}: {response.text[:500]}",
        )
