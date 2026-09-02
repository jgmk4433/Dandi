"""
schemas.py
----------
API 요청/응답 형식(Pydantic).
허브의 REGIONAL_INTEGRATION.md 명세를 그대로 반영한다. 필드가 어긋나면 422 로 즉시 드러난다.
"""

from typing import Any, Dict, List, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

# 허브가 보내는 purpose 값. 표에 없는 값이 오면 HELMET_VERIFY 로 대체한다.
KNOWN_PURPOSES = ("HELMET_VERIFY", "HEADWEAR_UNKNOWN", "BARE_HEAD_VERIFY")
DEFAULT_PURPOSE = "HELMET_VERIFY"


class ImageMeta(BaseModel):
    """허브가 알려주는 이미지 좌표계 정보."""

    width: Optional[int] = None
    height: Optional[int] = None
    coord_system: str = "absolute_xyxy_topleft"
    # [중요] False 고정. 앱이 EXIF 회전을 픽셀에 구워 보내므로
    #        디코딩 시 회전을 다시 적용하면 좌표가 어긋난다.
    apply_exif_rotation: bool = False


class CropTarget(BaseModel):
    """크롭할 관심영역 1건."""

    roi_id: Optional[str] = None
    purpose: str = DEFAULT_PURPOSE    # HELMET_VERIFY / HEADWEAR_UNKNOWN / BARE_HEAD_VERIFY
    label: Optional[str] = None
    conf: Optional[float] = None
    bbox: List[float] = Field(default_factory=list)         # 원본 검출 박스(참고용)
    padded_bbox: List[float] = Field(default_factory=list)  # 실제 크롭 권장 영역
    crop_px: List[int] = Field(default_factory=list)
    # true = 크롭 최소변 48px 미만. 지역 서버가 확장/업스케일로 보정해야 한다.
    low_resolution: bool = False


class EnforceRequest(BaseModel):
    """POST /api/v1/enforce — 허브가 보내는 단속 건."""

    trace_id: Optional[str] = None
    event_no: str = Field(..., min_length=1, max_length=64)
    region_code: str = Field(default="", max_length=16)
    timestamp: Optional[str] = None

    violation_types: List[str] = Field(default_factory=list)
    hub_verified: bool = False
    requires_vlm: bool = True

    image_base64: str = Field(..., min_length=1)
    image: ImageMeta = Field(default_factory=ImageMeta)

    crop_targets: List[CropTarget] = Field(default_factory=list)
    # 이전 스펙 호환용 별칭. crop_targets 가 비어 있을 때만 사용한다.
    crop_rois: List[CropTarget] = Field(default_factory=list)

    callback_url: Optional[str] = None

    def targets(self) -> List[CropTarget]:
        return self.crop_targets or self.crop_rois  # crop_targets 우선, 없으면 구버전 별칭 사용


class EnforceAccepted(BaseModel):
    status: str = "ACCEPTED"
    event_no: str
    message: str = "Queued for review."


class ReassignRequest(BaseModel):
    """
    POST /api/v1/event/{event_no}/reassign — 허브가 중계하는 이의제기.

    허브 명세는 {"reason": ..., "trace_id": ...} 이지만,
    허브 앱 API 는 appeal_reason 이라는 이름을 쓰므로 중계 과정에서 그대로 넘어올 수 있다.
    둘 다 받아들여 422 로 이의제기 경로가 끊기는 것을 막는다.
    """

    model_config = ConfigDict(populate_by_name=True)

    reason: str = Field(
        ...,
        validation_alias=AliasChoices("reason", "appeal_reason"),
        min_length=1,
        max_length=2000,
    )
    trace_id: Optional[str] = None


class EventDetail(BaseModel):
    """
    GET /api/v1/event/{event_no} — 심의 내역.
    허브가 이 응답을 앱으로 그대로 중계하므로 시민에게 보여도 되는 내용만 담는다.
    """

    event_no: str
    trace_id: Optional[str] = None
    region_code: str
    status: str
    status_text: str                     # 앱에 그대로 노출 가능한 한국어 문구
    violation_types: List[str] = Field(default_factory=list)
    decision_reason: Optional[str] = None
    reviewer: Optional[str] = None
    reported_at: Optional[str] = None
    reviewed_at: Optional[str] = None    # ISO8601 UTC (Z 접미사)
    vlm_summary: Optional[Dict[str, Any]] = None


class SimpleResult(BaseModel):
    status: str
    message: Optional[str] = None
