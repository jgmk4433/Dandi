"""
services/yolo_service.py
------------------------
허브 측 YOLO 재검증 담당 (모델 래퍼).

[현재 상태] 
           settings.VERIFY_MODE="auto" 에서는 가중치가 없으면 검증을 건너뛰고
           앱의 1차 판정을 그대로 통과시킨다(yolo_result=UNVERIFIED).
           학습이 끝나면 **models/final.pt 에 파일을 넣고 서버만 재시작**하면
           코드 수정 없이 검증이 활성화된다.

[클래스 구성 변경 대응]
           학습 클래스: 0 person / 1 escooter / 2 cycle / 3 helmet / 4 bare head
           클래스 이름이 바뀌어도 models/labels.json 의 role 매핑만 맞추면 동작한다.
           판정 논리는 services/violation_rules.py 에 분리돼 있다.

[의존성]  ultralytics/torch 는 검증을 켤 때만 필요한 선택 패키지다.
          미설치 상태에서도 서버는 정상 구동된다(임포트를 지연시켜 처리).
"""

import json
import logging
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from app.config import BASE_DIR, settings
from app.services.violation_rules import (
    ROLE_BARE_HEAD,
    ROLE_CYCLE,
    ROLE_ESCOOTER,
    ROLE_HELMET,
    ROLE_PERSON,
    Detection,
    Verdict,
    evaluate,
)

log = logging.getLogger("hub.yolo")

# labels.json 이 없을 때 사용할 기본 매핑 (소문자, 공백/하이픈은 _ 로 정규화해 비교)
# 학습 클래스: 0 person / 1 escooter / 2 cycle / 3 helmet / 4 bare head
DEFAULT_ROLE_MAP = {
    "person": ROLE_PERSON,
    "rider": ROLE_PERSON,
    "escooter": ROLE_ESCOOTER,
    "e_scooter": ROLE_ESCOOTER,
    "scooter": ROLE_ESCOOTER,
    "kickboard": ROLE_ESCOOTER,
    "cycle": ROLE_CYCLE,
    "bicycle": ROLE_CYCLE,
    "motorcycle": ROLE_CYCLE,
    "motercycle": ROLE_CYCLE,   # 라벨 오타 대비
    "bike": ROLE_CYCLE,
    "helmet": ROLE_HELMET,
    "bare_head": ROLE_BARE_HEAD,
    "barehead": ROLE_BARE_HEAD,
    "head": ROLE_BARE_HEAD,
    "no_helmet": ROLE_BARE_HEAD,
}


def _normalize(name: str) -> str:
    """'bare head', 'Bare-Head' 처럼 공백/하이픈이 섞인 클래스명을 통일한다."""
    return str(name).strip().lower().replace(" ", "_").replace("-", "_")


def _resolve(path_str: str) -> Path:
    """상대 경로는 프로젝트 루트 기준으로 해석한다."""
    path = Path(path_str)
    return path if path.is_absolute() else BASE_DIR / path


def model_file() -> Path:
    return _resolve(settings.YOLO_MODEL_PATH)


def model_available() -> bool:
    """학습된 가중치가 배치되어 있는지 확인."""
    return model_file().is_file()


def verification_enabled() -> bool:
    """현재 설정에서 재검증을 수행해야 하는지."""
    mode = (settings.VERIFY_MODE or "auto").lower()
    if mode == "off":
        return False
    if mode == "strict":
        return True
    return model_available()  # auto


def load_role_map() -> dict:
    """labels.json 을 읽어 '모델 클래스명 -> 표준 역할' 매핑을 만든다."""
    role_map = dict(DEFAULT_ROLE_MAP)
    labels_path = _resolve(settings.YOLO_LABELS_FILE)
    if labels_path.is_file():
        try:
            data = json.loads(labels_path.read_text(encoding="utf-8"))
            for name, role in (data.get("roles") or {}).items():
                role_map[_normalize(name)] = str(role).upper()  # 사용자 정의 매핑으로 덮어쓰기
        except Exception as exc:
            log.warning("labels.json 읽기 실패(기본 매핑 사용): %s", exc)
    return role_map


class YoloInferenceService:
    """가중치는 무거우므로 최초 호출 시 한 번만 로드한다(지연 로딩)."""

    def __init__(self):
        self._model = None
        self._model_names: dict = {}
        self._role_map = load_role_map()
        self._load_error: Optional[str] = None
        self._lock = threading.Lock()

    # ---------- 모델 ----------
    def _ensure_model(self):
        if self._model is not None or self._load_error:
            return self._model  # 이미 로드됐거나 이전 로드 실패 -> 재시도하지 않음
        with self._lock:
            if self._model is not None or self._load_error:
                return self._model  # 락 획득 대기 중 다른 스레드가 이미 처리했는지 재확인
            path = model_file()
            if not path.is_file():
                self._load_error = f"가중치 파일 없음: {path}"
                return None
            try:
                from ultralytics import YOLO  # 선택 의존성 (검증 활성 시에만 필요)

                self._model = YOLO(str(path))
                self._model_names = dict(getattr(self._model, "names", {}) or {})
                log.info("YOLO 모델 로드 완료: %s (클래스 %d개)", path.name, len(self._model_names))
            except ImportError:
                self._load_error = "ultralytics 미설치 (pip install ultralytics)"
                log.error(self._load_error)
            except Exception as exc:
                self._load_error = f"모델 로드 실패: {exc}"
                log.exception(self._load_error)
            return self._model

    def describe_state(self) -> str:
        """UI/상태 API 표시용 한 줄 설명."""
        mode = (settings.VERIFY_MODE or "auto").lower()
        if mode == "off":
            return "검증 OFF (앱 판정 신뢰)"
        if not model_available():
            suffix = "strict -> 오류 처리" if mode == "strict" else "auto -> 통과 처리"
            return f"모델 학습 대기중: {model_file().name} 없음 ({suffix})"
        if self._load_error:
            return f"모델 오류: {self._load_error}"
        if self._model is None:
            return f"검증 준비됨: {model_file().name} (첫 요청 시 로드)"
        return f"검증 활성: {model_file().name} / 클래스 {len(self._model_names)}개"

    def class_roles(self) -> dict:
        """모델 클래스가 어떤 역할로 매핑되는지 (설정 점검용)."""
        if not self._model_names:
            return {}
        return {
            name: self._role_map.get(_normalize(name), "UNKNOWN")
            for name in self._model_names.values()
        }

    # ---------- 추론 ----------
    def detect(self, image_path: str, image_size: Optional[Tuple[int, int]] = None) -> Optional[Verdict]:
        """
        이미지를 추론하고 규칙 엔진으로 판정한다.
        반환 None = 검증 불가(모델 없음/오류). 호출측이 VERIFY_MODE 에 따라 처리한다.
        """
        model = self._ensure_model()
        if model is None:
            return None

        try:
            results = model.predict(
                source=image_path,
                imgsz=settings.YOLO_IMG_SIZE,   # 앱 전송 해상도(800)와 동일하게
                conf=settings.YOLO_CONF_THRESHOLD,
                device=settings.YOLO_DEVICE,
                verbose=False,
            )
        except Exception as exc:
            log.exception("YOLO 추론 실패: %s", exc)
            return None

        detections = self._to_detections(results)
        verdict = evaluate(detections, image_size=image_size)
        verdict.detail["model"] = model_file().name
        verdict.detail["detected"] = [
            {"label": d.label, "role": d.role, "conf": round(d.conf, 3)} for d in detections
        ]  # 판정 근거로 검출 목록 전체를 기록
        return verdict

    def _to_detections(self, results) -> List[Detection]:
        """ultralytics 결과를 규칙 엔진이 쓰는 Detection 목록으로 변환."""
        detections: List[Detection] = []
        for result in results:
            names = dict(getattr(result, "names", {}) or self._model_names)
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            for box in boxes:
                try:
                    class_id = int(box.cls.item())
                    label = str(names.get(class_id, class_id))
                    conf = float(box.conf.item())
                    x1, y1, x2, y2 = (float(v) for v in box.xyxy[0].tolist())
                except Exception:
                    continue  # 파싱 실패한 박스는 건너뜀
                detections.append(
                    Detection(
                        role=self._role_map.get(_normalize(label), "UNKNOWN"),
                        label=label,
                        conf=conf,
                        bbox=(x1, y1, x2, y2),
                    )
                )
        return detections


_service: Optional[YoloInferenceService] = None
_service_lock = threading.Lock()


def get_yolo_service() -> YoloInferenceService:
    """전역 단일 인스턴스 (모델을 중복 로드하지 않기 위함)."""
    global _service
    with _service_lock:
        if _service is None:
            _service = YoloInferenceService()
        return _service
