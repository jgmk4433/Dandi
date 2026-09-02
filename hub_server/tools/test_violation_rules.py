"""
tools/test_violation_rules.py
-----------------------------
YOLO 학습이 끝나기 전에 **판정 규칙만** 시험하는 스크립트.
모델 없이 가상의 박스 좌표를 넣어 규칙이 의도대로 동작하는지 확인한다.

실행:  python tools/test_violation_rules.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # app 패키지 임포트를 위해 상위 경로 추가

from app.services.violation_rules import (  # noqa: E402
    DECISION_CONFIRMED,
    DECISION_DISCARD,
    DECISION_VLM_REQUIRED,
    ROLE_BARE_HEAD,
    ROLE_CYCLE,
    ROLE_ESCOOTER,
    ROLE_HELMET,
    ROLE_PERSON,
    VIOLATION_HELMET_NO,
    VIOLATION_MULTI_RIDER,
    Detection,
    evaluate,
)

IMAGE = (800, 600)  # 앱이 보내는 800px 기준


def det(role, bbox, conf=0.9, label=None):
    return Detection(role=role, label=label or role.lower(), conf=conf, bbox=bbox)  # 테스트용 가상 검출 객체 생성


# 기본 배치: 킥보드 (300,400)-(420,520), 탑승자 (290,180)-(420,460)
SCOOTER = det(ROLE_ESCOOTER, (300, 400, 420, 520))
RIDER = det(ROLE_PERSON, (290, 180, 420, 460))

CASES = [  # (설명, 입력 Detection 목록, 기대 판정, 기대 위반유형, 기대 크롭 건수)
    (
        "헬멧 탑승자 1인 -> VLM 판별 요청 (헬멧 박스 좌표 전달)",
        [SCOOTER, RIDER, det(ROLE_HELMET, (320, 180, 390, 240))],
        DECISION_VLM_REQUIRED, [VIOLATION_HELMET_NO], 1,
    ),
    (
        "맨머리 탑승자 1인 -> 위반 확정 (VLM 불필요)",
        [SCOOTER, RIDER, det(ROLE_BARE_HEAD, (320, 180, 390, 240))],
        DECISION_CONFIRMED, [VIOLATION_HELMET_NO], 0,
    ),
    (
        "2인 밀착 탑승 + 둘 다 헬멧 -> 위반 확정 (VLM 불필요)",
        [
            det(ROLE_ESCOOTER, (300, 400, 460, 520)),
            det(ROLE_PERSON, (290, 180, 400, 460)),
            det(ROLE_HELMET, (310, 180, 380, 240)),
            det(ROLE_PERSON, (360, 190, 470, 470)),
            det(ROLE_HELMET, (385, 190, 455, 250)),
        ],
        DECISION_CONFIRMED, [VIOLATION_MULTI_RIDER], 0,
    ),
    (
        "2인 탑승 + 한 명 맨머리 -> 두 위반 모두 확정",
        [
            det(ROLE_ESCOOTER, (300, 400, 460, 520)),
            det(ROLE_PERSON, (290, 180, 400, 460)),
            det(ROLE_BARE_HEAD, (310, 180, 380, 240)),
            det(ROLE_PERSON, (360, 190, 470, 470)),
            det(ROLE_HELMET, (385, 190, 455, 250)),
        ],
        DECISION_CONFIRMED, [VIOLATION_HELMET_NO, VIOLATION_MULTI_RIDER], 0,
    ),
    (
        "자전거 탑승자(cycle 겹침 우세) -> 단속 대상 배제",
        [
            det(ROLE_CYCLE, (280, 350, 460, 540)),
            det(ROLE_ESCOOTER, (700, 480, 780, 560)),
            det(ROLE_PERSON, (300, 180, 430, 470)),
            det(ROLE_BARE_HEAD, (330, 180, 400, 240)),
        ],
        DECISION_DISCARD, [], 0,
    ),
    (
        "킥보드 미검출 -> 폐기",
        [RIDER, det(ROLE_BARE_HEAD, (320, 180, 390, 240))],
        DECISION_DISCARD, [], 0,
    ),
    (
        "옆에 선 보행자는 탑승자로 보지 않음 -> 헬멧 탑승자만 VLM",
        [
            SCOOTER, RIDER, det(ROLE_HELMET, (320, 180, 390, 240)),
            det(ROLE_PERSON, (60, 150, 170, 430)),
            det(ROLE_BARE_HEAD, (85, 150, 145, 205)),
        ],
        DECISION_VLM_REQUIRED, [VIOLATION_HELMET_NO], 1,
    ),
    (
        "같은 킥보드지만 떨어져 선 사람 -> 2인 탑승 아님",
        [
            det(ROLE_ESCOOTER, (300, 400, 520, 520)),
            det(ROLE_PERSON, (290, 180, 400, 460)),
            det(ROLE_HELMET, (310, 180, 380, 240)),
            det(ROLE_PERSON, (470, 200, 560, 470)),
            det(ROLE_HELMET, (490, 200, 550, 255)),
        ],
        DECISION_VLM_REQUIRED, [VIOLATION_HELMET_NO], 1,
    ),
    (
        "머리 박스 미검출 -> 사람 박스로 VLM 확인 요청",
        [SCOOTER, RIDER],
        DECISION_VLM_REQUIRED, [VIOLATION_HELMET_NO], 1,
    ),
    (
        "저신뢰도 박스만 존재 -> 폐기",
        [
            det(ROLE_ESCOOTER, (300, 400, 420, 520), conf=0.10),
            det(ROLE_PERSON, (290, 180, 420, 460), conf=0.12),
        ],
        DECISION_DISCARD, [], 0,
    ),
]


def main() -> int:
    failed = 0
    for name, detections, expected_decision, expected_types, expected_crops in CASES:
        verdict = evaluate(detections, image_size=IMAGE)  # 규칙 엔진 실행
        ok = (
            verdict.decision == expected_decision
            and sorted(verdict.violation_types) == sorted(expected_types)
            and len(verdict.crop_targets) == expected_crops
        )  # 판정/위반유형/크롭건수가 모두 기대값과 일치하는지 검사
        if not ok:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        print(f"       결정={verdict.decision} 위반={verdict.violation_types or '없음'} "
              f"크롭={len(verdict.crop_targets)}건")
        for target in verdict.crop_targets:
            print(f"         - {target['purpose']} {target['padded_bbox']} "
                  f"crop={target['crop_px']} low_res={target['low_resolution']}")  # 크롭 대상 상세 출력
        if not ok:
            print(f"       기대={expected_decision}/{expected_types}/크롭{expected_crops}")
            print(f"       근거={verdict.detail}")  # 실패 시 판정 근거 출력

    print()
    print("전체 통과" if failed == 0 else f"{failed}건 실패")
    return 1 if failed else 0  # 실패 건수가 있으면 비정상 종료 코드 반환


if __name__ == "__main__":
    sys.exit(main())