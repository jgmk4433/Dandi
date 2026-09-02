"""
ui/region_utils.py
------------------
지역 코드 표시 규칙.

endpoints.ini 의 원본 키(DAEGU 등)는 그대로 두고, 화면에서만
지정한 순서로 정렬하고 두 자리 숫자 코드를 붙인다.
주소 설정 패널과 지역 카드 패널이 같은 규칙을 써야 하므로 여기에 모았다.
"""

from typing import Dict, Iterable, List

from app.ui.theme import REGION_PRIORITY


def order_regions(codes: Iterable[str]) -> List[str]:
    """REGION_PRIORITY 순서로 앞에 두고, 나머지는 가나다순으로 뒤에 붙인다."""
    keys = list(codes)
    head = [code for code in REGION_PRIORITY if code in keys]  # 우선순위 지정 지역 먼저
    tail = sorted(code for code in keys if code not in REGION_PRIORITY)  # 나머지는 가나다순
    return head + tail


# 지역 번호는 화면에서 매기지 않는다.
# 사건번호(PM02A1BC)에 들어가는 값과 같아야 하므로
# endpoints.ini 의 [REGION_NUMBERS] 를 registry.region_numbers() 로 읽어 쓴다.


def label(code: str, number_map: Dict[str, str]) -> str:
    """화면 표시용 이름. 예: [02] DAEGU"""
    return f"[{number_map.get(code, '00')}] {code}"
