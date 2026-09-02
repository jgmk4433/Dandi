"""
services/violation_rules.py
---------------------------
위반 판정 규칙 엔진 (YOLO 5클래스 기준).

학습 클래스
    0 person      1 escooter     2 cycle(자전거/오토바이)
    3 helmet      4 bare head

  [클래스 정의] 안전모만 helmet 이고, 맨머리와 **모자(캡/비니 등)는 모두 bare head** 다.
               즉 "모자인지 헬멧인지" 구분은 YOLO 단계에서 이미 끝난다.

판정 순서 (앱이 이미 escooter+person 공존을 확인하고 보낸다는 전제)
    1) 탑승 확인   : 사람이 escooter 에 탑승했는가?
                     - cycle 과 더 많이 겹치는 사람은 단속 대상에서 배제
                     - escooter 미검출 또는 탑승자 없음 -> DISCARD (지역 서버로 보내지 않음)
    2) bare head   : 탑승자 중 한 명이라도 맨머리 -> 위반 확정 (VLM 불필요)
    3) 2인 이상 탑승: 같은 킥보드의 탑승자 박스가 서로 겹침 -> 위반 확정 (VLM 불필요)
    4) helmet      : 위 둘이 아니고 헬멧으로 검출된 탑승자 -> 헬멧 박스 좌표를 지역 서버로.

       [VLM 을 거치는 이유]
         동승자나 장애물에 머리가 일부 가려지면 YOLO 가 helmet 으로 오검출하는 비율이
         높다. helmet 오검출은 곧 실제 위반자를 놓치는 결과가 되므로,
         헬멧으로 검출된 건은 신뢰도가 높아 보이더라도 예외 없이 VLM 최종 확인을 거친다.
         (모자/헬멧 구분이 아니라 "정말로 안전모를 쓰고 있는가"의 재확인이다)
         전체 신고 중 helmet 검출 건에만 해당하므로 VLM 호출량은 크지 않다.

허브는 이미지를 크롭하지 않는다. 원본 이미지 + 절대좌표만 전달한다.

[결정값]
    DISCARD       단속 대상 아님. 전송하지 않음
    CONFIRMED     위반 확정. 전송하되 VLM 크롭 불필요
    VLM_REQUIRED  위반 후보. 전송 + crop_targets 좌표로 2차 판별 요청
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from app.config import settings

# ---------- 표준 역할(role) : models/labels.json 이 클래스명을 여기로 매핑한다 ----------
ROLE_PERSON = "PERSON"
ROLE_ESCOOTER = "ESCOOTER"
ROLE_CYCLE = "CYCLE"           # 자전거/오토바이 : 단속 대상 배제용
ROLE_HELMET = "HELMET"
ROLE_BARE_HEAD = "BARE_HEAD"

# ---------- 위반 코드 ----------
VIOLATION_HELMET_NO = "HELMET_NO"      # 헬멧 미착용
VIOLATION_MULTI_RIDER = "MULTI_RIDER"  # 2인 이상 탑승

# ---------- 판정 결과 ----------
DECISION_DISCARD = "DISCARD"
DECISION_CONFIRMED = "CONFIRMED"
DECISION_VLM_REQUIRED = "VLM_REQUIRED"

# ---------- 크롭 목적 (지역 서버가 목적별 VLM 프롬프트를 고르는 키) ----------
PURPOSE_HELMET_VERIFY = "HELMET_VERIFY"        # 안전모 착용이 맞는지 최종 확인(오검출 배제)
PURPOSE_BARE_HEAD_VERIFY = "BARE_HEAD_VERIFY"  # 미착용 판정이 맞는지 확인(선택 기능)
PURPOSE_HEADWEAR_UNKNOWN = "HEADWEAR_UNKNOWN"  # 머리 박스 미검출 -> 사람 박스로 확인 요청

BBox = Tuple[float, float, float, float]  # (x1, y1, x2, y2) 좌상단 원점 절대좌표


@dataclass
class Detection:
    """모델이 찾아낸 박스 1개."""

    role: str      # 표준 역할 (labels.json 으로 매핑된 값)
    label: str     # 모델이 출력한 원본 클래스명
    conf: float
    bbox: BBox

    @property
    def area(self) -> float:
        x1, y1, x2, y2 = self.bbox
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)

    @property
    def center(self) -> Tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return (x1 + x2) / 2, (y1 + y2) / 2


@dataclass
class Verdict:
    """판정 결과."""

    decision: str                                          # DISCARD / CONFIRMED / VLM_REQUIRED
    violation_types: List[str] = field(default_factory=list)
    crop_targets: List[dict] = field(default_factory=list)  # 지역 서버가 크롭할 좌표
    detail: dict = field(default_factory=dict)              # 판정 근거(로그/디버깅)

    @property
    def is_violation(self) -> bool:
        """확정 위반 여부. VLM_REQUIRED 는 아직 확정이 아니다."""
        return self.decision == DECISION_CONFIRMED

    @property
    def should_forward(self) -> bool:
        """지역 서버로 보내야 하는지."""
        return self.decision in (DECISION_CONFIRMED, DECISION_VLM_REQUIRED)


# ---------- 기하 계산 ----------
def _intersection(a: BBox, b: BBox) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)  # 교차 영역 넓이(겹치지 않으면 0)


def overlap_ratio(a: BBox, b: BBox) -> float:
    """
    작은 박스 기준 겹침 비율.
    사람(큰 박스)과 킥보드(작은 박스)는 크기 차가 커서 IoU 로는 탑승을 놓치기 쉽다.
    """
    inter = _intersection(a, b)
    if inter <= 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    smaller = min(area_a, area_b)
    return inter / smaller if smaller > 0 else 0.0


def _center_inside(inner: BBox, outer: BBox) -> bool:
    cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]  # inner 박스 중심이 outer 안에 있는지


def _pad_box(bbox: BBox, ratio: float, image_size: Optional[Tuple[int, int]]) -> List[int]:
    """
    크롭 여유를 준 박스를 만든다.
    딱 맞게 자르면 VLM 이 맥락(머리 형태, 어깨선)을 잃으므로 주변을 포함시킨다.
    이미지 경계를 넘지 않도록 잘라(clamp) 지역 서버가 그대로 쓸 수 있게 한다.
    """
    x1, y1, x2, y2 = bbox
    pad_x = (x2 - x1) * ratio
    pad_y = (y2 - y1) * ratio
    nx1, ny1 = x1 - pad_x, y1 - pad_y
    nx2, ny2 = x2 + pad_x, y2 + pad_y

    nx1, ny1 = max(0.0, nx1), max(0.0, ny1)  # 좌상단은 0 미만으로 내려가지 않게 clamp
    if image_size:
        nx2 = min(float(image_size[0]), nx2)  # 우하단은 이미지 크기를 넘지 않게 clamp
        ny2 = min(float(image_size[1]), ny2)
    return [round(nx1), round(ny1), round(nx2), round(ny2)]


def _crop_target(
    roi_id: str,
    purpose: str,
    det: Detection,
    image_size: Optional[Tuple[int, int]],
    pad_ratio: float,
) -> dict:
    """지역 서버로 보낼 크롭 좌표 1건."""
    padded = _pad_box(det.bbox, pad_ratio, image_size)
    crop_w, crop_h = padded[2] - padded[0], padded[3] - padded[1]
    x1, y1, x2, y2 = det.bbox
    return {
        "roi_id": roi_id,
        "purpose": purpose,
        "label": det.label,
        "conf": round(float(det.conf), 4),
        "bbox": [round(x1), round(y1), round(x2), round(y2)],  # 원본 검출 박스
        "padded_bbox": padded,                                  # 실제 크롭 권장 영역
        "crop_px": [crop_w, crop_h],
        # 크롭이 너무 작으면 VLM 판별 신뢰도가 떨어진다.
        # 지역 서버는 이 값이 true 면 업스케일하거나 전체 이미지로 판단하는 것이 좋다.
        "low_resolution": min(crop_w, crop_h) < settings.CROP_MIN_EDGE_PX,
    }


# ---------- 판정 ----------
def _select_riders(
    persons: Sequence[Detection],
    escooters: Sequence[Detection],
    cycles: Sequence[Detection],
) -> Tuple[dict, dict]:
    """
    사람을 킥보드 탑승자로 배정한다.
    cycle 과 더 많이 겹치는 사람은 자전거/오토바이 이용자이므로 제외한다.
    반환: (킥보드 index -> 탑승자 목록, 제외 사유 통계)
    """
    riders_by_scooter: dict = {index: [] for index in range(len(escooters))}
    excluded = {"cycle_rider": 0, "not_riding": 0}

    for person in persons:
        best_scooter, best_score = -1, 0.0
        for index, scooter in enumerate(escooters):
            # 사람의 발끝이 킥보드 윗변보다 위에 있으면 탑승이 아니다(뒤쪽 보행자 등)
            if person.bbox[3] < scooter.bbox[1]:
                continue
            score = overlap_ratio(person.bbox, scooter.bbox)
            if score > best_score:
                best_scooter, best_score = index, score

        cycle_score = max((overlap_ratio(person.bbox, c.bbox) for c in cycles), default=0.0)
        if cycle_score > best_score:
            excluded["cycle_rider"] += 1        # 자전거/오토바이 쪽 -> 단속 대상 아님
            continue
        if best_scooter < 0 or best_score < settings.RIDE_OVERLAP_THRESHOLD:
            excluded["not_riding"] += 1         # 지나가는 보행자
            continue
        riders_by_scooter[best_scooter].append(person)

    return riders_by_scooter, excluded


def _resolve_multi_rider(riders: List[Detection]) -> Tuple[bool, List[Detection]]:
    """
    같은 킥보드에 배정된 사람들이 실제 2인 탑승인지 판단한다.
    밀착 탑승이면 사람 박스가 서로 겹친다. 겹치지 않으면 옆에 선 사람으로 보고
    킥보드와 가장 많이 겹친 1명만 탑승자로 남긴다.
    """
    if len(riders) < 2:
        return False, riders

    for i in range(len(riders)):
        for j in range(i + 1, len(riders)):
            if overlap_ratio(riders[i].bbox, riders[j].bbox) >= settings.MULTI_RIDER_OVERLAP_THRESHOLD:
                return True, riders  # 2인 이상 확정 (3인 이상도 동일 처리)
    return False, riders[:1]  # 밀착이 아니면 가장 우선순위 높은 1명만 탑승자로 인정


def _match_head(rider: Detection, heads: List[Detection], used: set) -> Optional[Detection]:
    """
    탑승자의 머리 박스(helmet 또는 bare head)를 찾는다.
    사람 박스 안에 중심이 들어오는 것을 우선하고, 여러 개면 가장 위쪽 것을 택한다.
    (모델이 머리 박스를 직접 출력하므로 사람 박스 상단을 추정하지 않는다)
    """
    candidates = [
        head for head in heads
        if id(head) not in used and _center_inside(head.bbox, rider.bbox)
    ]
    if not candidates:
        candidates = [
            head for head in heads
            if id(head) not in used and overlap_ratio(head.bbox, rider.bbox) >= 0.5
        ]  # 중심 포함이 없으면 겹침 비율로 완화해서 재탐색
    if not candidates:
        return None

    matched = min(candidates, key=lambda h: h.center[1])  # y 중심이 가장 위
    used.add(id(matched))  # 중복 매칭 방지
    return matched


def evaluate(detections: Sequence[Detection], image_size: Optional[Tuple[int, int]] = None) -> Verdict:
    """검출 결과를 받아 판정한다."""
    # 1) 신뢰도 / 최소 크기 필터
    min_area = image_size[0] * image_size[1] * settings.YOLO_MIN_BOX_RATIO if image_size else 0.0
    valid = [d for d in detections if d.conf >= settings.YOLO_CONF_THRESHOLD and d.area >= min_area]

    persons = [d for d in valid if d.role == ROLE_PERSON]
    escooters = [d for d in valid if d.role == ROLE_ESCOOTER]
    cycles = [d for d in valid if d.role == ROLE_CYCLE]
    helmets = [d for d in valid if d.role == ROLE_HELMET]
    bare_heads = [d for d in valid if d.role == ROLE_BARE_HEAD]

    detail: dict = {
        "counts": {
            "person": len(persons), "escooter": len(escooters), "cycle": len(cycles),
            "helmet": len(helmets), "bare_head": len(bare_heads),
        }
    }

    # 2) 탑승 확인 — 앱이 1차로 걸렀어도 허브에서 다시 확인한다
    if not escooters:
        detail["reason"] = "킥보드 미검출 -> 단속 대상 아님"
        return Verdict(decision=DECISION_DISCARD, detail=detail)

    riders_by_scooter, excluded = _select_riders(persons, escooters, cycles)
    detail["excluded"] = excluded

    multi_rider = False
    riders: List[Detection] = []
    for index, group in riders_by_scooter.items():
        is_multi, confirmed = _resolve_multi_rider(group)
        multi_rider = multi_rider or is_multi
        riders.extend(confirmed)

    detail["rider_count"] = len(riders)
    if not riders:
        detail["reason"] = "탑승자 미확인 -> 단속 대상 아님"
        return Verdict(decision=DECISION_DISCARD, detail=detail)

    # 3) 탑승자별 머리 착용 상태 판별
    #    bare head 는 YOLO 가 직접 판정하므로 VLM 이 필요 없다.
    #    helmet 은 실제 헬멧인지 모자인지 알 수 없으므로 VLM 확인 대상이다.
    used_heads: set = set()
    all_heads = bare_heads + helmets
    bare_head_riders: List[Detection] = []
    helmet_riders: List[Tuple[Detection, Detection]] = []  # (탑승자, 헬멧 박스)
    unknown_riders: List[Detection] = []

    for rider in riders:
        head = _match_head(rider, all_heads, used_heads)
        if head is None:
            unknown_riders.append(rider)
        elif head.role == ROLE_BARE_HEAD:
            bare_head_riders.append(head)
        else:
            helmet_riders.append((rider, head))

    detail["headwear"] = {
        "bare_head": len(bare_head_riders),
        "helmet": len(helmet_riders),
        "undetected": len(unknown_riders),
    }

    violation_types: List[str] = []

    # 4) 위반 확정 조건 — 하나라도 걸리면 VLM 2차 판별 없이 바로 확정
    #    bare head 는 YOLO 가 직접 미착용을 판정하므로 기본적으로 즉시 확정한다.
    #    VERIFY_BARE_HEAD_WITH_VLM=True 로 두면 이 건도 VLM 확인을 거친다(아래 5번에서 처리).
    verify_bare_head = settings.VERIFY_BARE_HEAD_WITH_VLM
    if bare_head_riders and not verify_bare_head:
        violation_types.append(VIOLATION_HELMET_NO)
    if multi_rider:
        violation_types.append(VIOLATION_MULTI_RIDER)

    if violation_types:
        detail["confirmed_by"] = (
            "bare_head" if bare_head_riders else ""
        ) + ("+multi_rider" if multi_rider else "")
        return Verdict(decision=DECISION_CONFIRMED, violation_types=violation_types, detail=detail)

    # 5) 헬멧 착용으로 보이는 탑승자 -> 헬멧 박스 좌표를 지역 서버에 전달.
    #    가려짐으로 인한 helmet 오검출을 걸러내는 것이 목적이므로,
    #    신뢰도가 높아도 예외 없이 전달한다.
    crop_targets: List[dict] = []
    for index, (_rider, helmet) in enumerate(helmet_riders, start=1):
        crop_targets.append(
            _crop_target(
                f"h{index}", PURPOSE_HELMET_VERIFY, helmet,
                image_size, settings.HELMET_CROP_PAD_RATIO,
            )
        )

    # (선택) 미착용 판정도 VLM 으로 재확인하는 모드
    if verify_bare_head:
        for index, bare_head in enumerate(bare_head_riders, start=1):
            crop_targets.append(
                _crop_target(
                    f"b{index}", PURPOSE_BARE_HEAD_VERIFY, bare_head,
                    image_size, settings.HELMET_CROP_PAD_RATIO,
                )
            )

    # 6) 머리 박스를 아예 못 찾은 탑승자 — 사람 박스를 넘겨 VLM 이 확인하게 한다
    #    (허브가 임의로 확정하거나 폐기하지 않는다. 설정으로 폐기 전환 가능)
    if unknown_riders and settings.FORWARD_WHEN_HEAD_UNDETECTED:
        for index, rider in enumerate(unknown_riders, start=1):
            crop_targets.append(
                _crop_target(
                    f"u{index}", PURPOSE_HEADWEAR_UNKNOWN, rider,
                    image_size, settings.PERSON_CROP_PAD_RATIO,
                )
            )

    if not crop_targets:
        detail["reason"] = "머리 박스 미검출 및 전달 설정 off -> 폐기"
        return Verdict(decision=DECISION_DISCARD, detail=detail)

    return Verdict(
        decision=DECISION_VLM_REQUIRED,
        violation_types=[VIOLATION_HELMET_NO],  # VLM 이 모자로 판정하면 지역 서버가 취소
        crop_targets=crop_targets,
        detail=detail,
    )
