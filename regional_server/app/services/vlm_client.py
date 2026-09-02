"""
services/vlm_client.py
----------------------
Ollama 로컬 VLM(qwen3-vl:7b) 호출 담당.

[호출 방식]
  POST {OLLAMA_URL}/api/generate
  {
    "model": "qwen3-vl:7b",
    "prompt": "...",
    "images": ["<base64>", ...],
    "format": "json",        <- JSON 으로만 답하도록 강제
    "stream": false,
    "keep_alive": "30m",     <- 유휴 시 모델 언로드 방지(콜드스타트 제거)
    "options": {"temperature": 0}
  }

[판별 목적별 프롬프트]
  허브가 crop_target 마다 purpose 를 지정해 보내므로, 목적별로 질문이 다르다.

    HELMET_VERIFY     : YOLO 가 helmet 으로 검출한 머리. 정말 안전모가 맞는지 최종 확인.
                        허브 YOLO 는 모자(캡/비니)를 bare head 로 학습하므로
                        "모자냐 헬멧이냐"를 묻는 것이 아니다.
                        동승자·구조물에 가려졌을 때 발생하는 오검출을 걸러내는 것이 목적이다.
    HEADWEAR_UNKNOWN  : 머리 박스를 못 찾은 경우. 사람 박스에서 착용 여부를 판단.
    BARE_HEAD_VERIFY  : 미착용 판정의 역방향 확인(현재 허브 명세에는 없는 예비 항목).
    FULL_ASSESSMENT   : 허브 YOLO 학습 전 단계. 원본 전체를 보고 전부 판단.

[중요] 가려짐이 오검출의 주된 원인이므로, 확신이 없을 때 억지로 결론을 내리게 하면 안 된다.
       판단 불가는 "unknown" 으로 답하게 하고, 그 건은 수동 검수로 넘긴다.
       오단속(헬멧 착용자를 위반 처리)은 놓친 위반보다 비용이 크다.
"""

import json
import logging
import re
import threading
from typing import List, Optional

import httpx

from app.config import settings

log = logging.getLogger("regional.vlm")

_client_lock = threading.Lock()
_client: Optional[httpx.Client] = None


def get_client() -> httpx.Client:
    """Ollama 호출용 공용 클라이언트(커넥션 재사용)."""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(timeout=httpx.Timeout(settings.VLM_TIMEOUT_SEC, connect=10.0))
        return _client


def close_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


# ---------------------------------------------------------------------------
#  프롬프트
#  문구를 바꿀 일이 많은 부분이라 한곳에 모아둔다.
#  모델을 교체하거나 정확도를 조정할 때 이 상수들만 손보면 된다.
# ---------------------------------------------------------------------------
_COMMON_RULES = """
반드시 아래 JSON 형식으로만 답하라. 다른 문장은 쓰지 마라.
{"answer": "<yes|no|unknown>", "confidence": <0.0~1.0>, "reason": "<한국어 한 문장>"}

판단 규칙:
- 사진이 흐리거나, 대상이 다른 사람·물체에 가려져 확신할 수 없으면 반드시 "unknown" 으로 답하라.
- 추측해서 yes 나 no 를 고르지 마라. 잘못된 단정은 부당한 단속으로 이어진다.
- confidence 는 실제 확신 정도를 정직하게 적어라.
""".strip()

PROMPTS = {
    # 안전모 착용이 맞는지 최종 확인 (오검출 배제가 목적)
    "HELMET_VERIFY": """이 사진은 전동킥보드 탑승자의 머리 부분을 확대한 것이다.
질문: 이 사람은 머리에 안전모(헬멧)를 착용하고 있는가?

- 안전모: 충격 보호용 단단한 외피를 가진 헬멧 (자전거·킥보드·오토바이용)
- 안전모가 아님: 야구모자, 비니, 후드, 두건, 머리카락만 있는 상태

구분 요령: 안전모는 챙이 짧거나 없고 머리를 둥글게 감싸며 턱끈이 보이는 경우가 많다.
야구모자는 앞으로 길게 나온 챙이 있고 뒤통수가 천으로 덮여 형태가 무르다.

yes = 안전모를 착용함
no  = 안전모가 아님 (모자이거나 아무것도 쓰지 않음)
"""
    + "\n\n"
    + _COMMON_RULES,

    # 머리 박스 미검출 -> 사람 전체에서 판단
    "HEADWEAR_UNKNOWN": """이 사진은 전동킥보드 탑승자의 모습이다.
질문: 이 사람은 머리에 안전모(헬멧)를 착용하고 있는가?

- 안전모: 충격 보호용 단단한 외피를 가진 헬멧
- 안전모가 아님: 야구모자, 비니, 후드, 두건, 머리카락만 있는 상태

머리가 보이지 않거나 잘려 있으면 추측하지 말고 unknown 으로 답하라.

yes = 안전모를 착용함
no  = 안전모가 아니거나 아무것도 쓰지 않음
"""
    + "\n\n"
    + _COMMON_RULES,

    # 미착용 판정 역방향 확인 (예비)
    "BARE_HEAD_VERIFY": """이 사진은 전동킥보드 탑승자의 머리 부분을 확대한 것이다.
질문: 이 사람은 안전모(헬멧)를 착용하지 않은 상태가 맞는가?

yes = 안전모를 쓰지 않았다 (맨머리이거나 일반 모자)
no  = 실제로는 안전모를 착용하고 있다
"""
    + "\n\n"
    + _COMMON_RULES,
}

DEFAULT_PURPOSE = "HELMET_VERIFY"

# 허브 YOLO 학습 전 단계에서 원본 전체를 판단할 때 사용한다.
# [현재 이 경로가 사실상 유일하게 동작하는 경로다]
#   허브 가중치가 배치되기 전까지 모든 건이 crop_targets 없이 도착하므로,
#   탑승 여부·인원·헬멧을 전부 이 프롬프트 하나가 판단한다.
#   허브의 DISCARD 필터(자전거·오토바이 배제)도 아직 돌지 않으므로 여기서 걸러야 한다.
FULL_ASSESSMENT_PROMPT = """이 사진은 전동킥보드 위반 신고로 접수된 사진이다.
다음을 순서대로 판단하라.

1. 사진 속에 전동킥보드에 탑승한 사람이 있는가?
   - 전동킥보드: 서서 타는 두 바퀴 이동장치. 손잡이 기둥이 수직으로 서 있고 발판이 낮다.
   - 자전거, 오토바이, 전동휠체어는 단속 대상이 아니다. 이 경우 riding 을 "no" 로 답하라.
   - 킥보드가 보여도 아무도 타고 있지 않으면 "no" 다.
2. 전동킥보드에 탑승한 사람이 몇 명인가? (한 대에 함께 타고 있는 인원)
3. 탑승자 중 안전모(헬멧)를 착용하지 않은 사람이 있는가?
   - 안전모: 충격 보호용 단단한 외피를 가진 헬멧
   - 야구모자, 비니, 후드, 두건은 안전모가 아니다

반드시 아래 JSON 형식으로만 답하라. 다른 문장은 쓰지 마라.
{"riding": "<yes|no|unknown>", "rider_count": <정수>,
 "helmet_missing": "<yes|no|unknown>", "confidence": <0.0~1.0>,
 "reason": "<한국어 한 문장>"}

판단 규칙:
- 가려짐이나 화질 때문에 확신할 수 없으면 "unknown" 으로 답하라.
- 추측하지 마라. 잘못된 단정은 부당한 단속으로 이어진다."""


class VlmError(Exception):
    """VLM 호출 실패. 재시도 대상."""


def resolve_purpose(purpose: Optional[str]) -> str:
    """
    허브가 보낸 purpose 를 지원 목록으로 정규화한다.
    미지원 값에 예외를 던지면 영구 오류인데도 재시도만 3회 낭비하고 사건이 미아가 된다.
    """
    key = str(purpose or "").strip().upper()
    if key in PROMPTS:
        return key
    if key:
        log.warning("미지원 purpose=%s -> %s 로 대체", key, DEFAULT_PURPOSE)
    return DEFAULT_PURPOSE


def _extract_json(text: str) -> dict:
    """
    모델 응답에서 JSON 을 뽑아낸다.
    format=json 을 지정해도 코드블록이나 설명 문장이 섞여 나오는 경우가 있다.
    """
    candidates = [text]

    cleaned = text.replace("```json", "").replace("```", "").strip()
    if cleaned != text:
        candidates.append(cleaned)

    # 중첩 없는 최초의 객체. 모델이 객체를 두 개 뱉어도 첫 번째만 집는다.
    flat = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
    if flat:
        candidates.append(flat.group(0))

    # 그래도 안 되면 최외곽(중첩 포함) 시도
    outer = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if outer:
        candidates.append(outer.group(0))

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            continue

    raise VlmError(f"JSON 파싱 실패: {text[:200]}")


def _call_ollama(prompt: str, images_b64: List[str]) -> dict:
    """Ollama 에 요청하고 JSON 응답을 파싱한다."""
    payload = {
        "model": settings.VLM_MODEL,
        "prompt": prompt,
        "images": images_b64,
        "format": "json",          # JSON 이외의 문장을 섞지 않도록 강제
        "stream": False,
        "keep_alive": settings.VLM_KEEP_ALIVE,
        "options": {"temperature": settings.VLM_TEMPERATURE},
    }

    try:
        response = get_client().post(f"{settings.OLLAMA_URL}/api/generate", json=payload)
    except Exception as exc:
        raise VlmError(f"Ollama 연결 실패: {exc}") from exc

    if response.status_code != 200:
        raise VlmError(f"Ollama HTTP {response.status_code}: {response.text[:300]}")

    try:
        body = response.json()
    except ValueError as exc:
        raise VlmError(f"Ollama 응답이 JSON 이 아님: {response.text[:200]}") from exc

    text = (body.get("response") or "").strip()
    if not text:
        raise VlmError("빈 응답")

    return _extract_json(text)


def _clamp_confidence(value) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def verify_crop(purpose: str, image_b64: str, context_image_b64: Optional[str] = None) -> dict:
    """
    크롭 이미지 1건을 판별한다.
    context_image_b64 를 함께 주면 원본 전체를 보조 자료로 제시한다
    (크롭만으로 가려짐 상황을 알기 어려울 때 정확도가 올라간다).

    반환: {"answer": "yes|no|unknown", "confidence": float, "reason": str, "purpose": str}
    """
    resolved = resolve_purpose(purpose)
    prompt = PROMPTS[resolved]

    images = [image_b64]
    if context_image_b64:
        images.append(context_image_b64)
        prompt += "\n\n두 번째 사진은 같은 장면의 전체 모습이다. 가림 여부 판단에 참고하라."

    result = _call_ollama(prompt, images)

    answer = str(result.get("answer", "unknown")).strip().lower()
    if answer not in ("yes", "no", "unknown"):
        answer = "unknown"

    return {
        "answer": answer,
        "confidence": _clamp_confidence(result.get("confidence")),
        "reason": str(result.get("reason", ""))[:300],
        "purpose": resolved,
    }


def assess_full_image(image_b64: str) -> dict:
    """
    원본 전체를 보고 탑승/인원/헬멧을 한 번에 판단한다.
    허브 YOLO 학습이 끝나기 전, 좌표 없이 이미지만 오는 경우에 사용한다.
    """
    result = _call_ollama(FULL_ASSESSMENT_PROMPT, [image_b64])

    def _norm(key: str) -> str:
        value = str(result.get(key, "unknown")).strip().lower()
        return value if value in ("yes", "no", "unknown") else "unknown"

    try:
        rider_count = int(result.get("rider_count", 0))
    except (TypeError, ValueError):
        rider_count = 0

    return {
        "riding": _norm("riding"),
        "rider_count": max(0, min(10, rider_count)),
        "helmet_missing": _norm("helmet_missing"),
        "confidence": _clamp_confidence(result.get("confidence")),
        "reason": str(result.get("reason", ""))[:300],
    }


def health() -> dict:
    """Ollama 와 모델 상태 확인 (기동 점검용)."""
    try:
        response = get_client().get(f"{settings.OLLAMA_URL}/api/tags", timeout=5.0)
        if response.status_code != 200:
            return {"ok": False, "detail": f"HTTP {response.status_code}"}

        names = [m.get("name", "") for m in response.json().get("models", [])]
        target = settings.VLM_MODEL
        family = target.split(":")[0]

        # 태그까지 정확히 일치해야 설치된 것으로 본다.
        # (qwen3-vl:2b 만 있는데 :7b 로 설정된 상태를 "설치됨"으로 보고하면 안 된다)
        exact = target in names
        family_only = [n for n in names if n.split(":")[0] == family]

        return {
            "ok": True,
            "model": target,
            "model_installed": exact,
            "same_family": family_only,
            "available": names,
        }
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}
