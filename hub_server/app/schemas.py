"""
schemas.py
----------
API 요청/응답 형식 정의(Pydantic).
dict 로 그냥 받으면 필드 누락을 런타임에야 알게 되므로, 스키마로 미리 검증한다.
"""

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field


# ---------- 신고 접수 ----------
class SubmitAccepted(BaseModel):
    """POST /api/v1/enforce/submit 응답."""

    status: str = "ACCEPTED"
    trace_id: str  # 처리 상태 조회용 고유 식별자
    message: str = "Enforcement request queued for processing."


class EnforcementStatus(BaseModel):
    """
    GET /api/v1/enforce/status/{trace_id} 응답.
    앱이 업로드 후 처리 결과를 폴링해서 확인하는 용도.
    """

    trace_id: str
    event_no: Optional[str] = None  # 위반 확정 시 부여되는 사건번호
    region_code: str
    yolo_result: str  # YOLO 탐지 결과 요약
    status: str
    violation_types: List[str] = Field(default_factory=list)  # 허브가 판정한 위반 종류
    hub_verified: bool = False          # True = 허브에서 위반 확정
    queue_state: Optional[str] = None   # QUEUED / RUNNING / DONE / FAILED
    attempts: int = 0  # 처리 재시도 횟수
    error_origin: Optional[str] = None  # 오류 발생 지점
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ---------- 이의제기 ----------
class AppealRequest(BaseModel):
    """POST /api/v1/appeal/submit 본문."""

    event_no: str = Field(..., min_length=1, max_length=64)  # 이의제기 대상 사건번호
    appeal_reason: str = Field(..., min_length=1, max_length=2000)  # 이의제기 사유


class AppealResult(BaseModel):
    status: str
    message: str


# ---------- 지역 서버 콜백 ----------
class CallbackRequest(BaseModel):
    """
    POST /api/v1/callback/complete 본문.
    지역 서버가 심의를 끝내고 결과를 알려줄 때 호출한다.
    event_no 또는 trace_id 중 하나만 있어도 매칭한다.
    """

    event_no: Optional[str] = None
    trace_id: Optional[str] = None
    status: str = Field(..., min_length=1, max_length=32)  # 예: CONFIRMED / REJECTED
    reason: Optional[str] = None  # 반려/기각 등 사유
    detail: Optional[Any] = None  # 추가 상세 정보(자유 형식)


class CallbackResult(BaseModel):
    status: str
    trace_id: Optional[str] = None