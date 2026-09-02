"""
services/feedback_collector.py
------------------------------
재학습용 오탐 데이터 보관.

허브는 평소 이미지를 갖고 있지 않으므로, 지역 서버에서 되돌려 받은 바이트를
그대로 파일로 저장한다. 이미지와 메타데이터(json)를 같은 이름으로 한 쌍으로 남겨
나중에 라벨링 도구에서 바로 열 수 있게 한다.

저장 위치: data/dataset_feedback/YYYYMMDD_HHMMSS_<event_no>.{jpg,json}
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import settings

log = logging.getLogger("hub.feedback")


class YoloFeedbackCollector:
    def __init__(self, feedback_dir=None):
        self.feedback_dir = Path(feedback_dir or settings.FEEDBACK_DIR)
        self.feedback_dir.mkdir(parents=True, exist_ok=True)

    def save_from_bytes(
        self,
        trace_id: str,
        event_no: Optional[str],
        image_bytes: bytes,
        reason: str = "",
        extra: Optional[dict] = None,
    ) -> Optional[str]:
        """이미지 바이트와 메타데이터를 한 쌍으로 저장한다. 반환: 저장한 파일 이름."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = f"{stamp}_{event_no or trace_id}"
        suffix = ".png" if image_bytes[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"  # 매직바이트로 PNG 여부 판별

        try:
            (self.feedback_dir / f"{prefix}{suffix}").write_bytes(image_bytes)
        except OSError as exc:
            log.warning("재학습 이미지 저장 실패: %s", exc)
            return None

        meta = {
            "trace_id": trace_id,
            "event_no": event_no,
            "reason": reason,
            "image_file": f"{prefix}{suffix}",
            "recorded_at": stamp,
        }
        meta.update(extra or {})  # 호출측이 넘긴 부가 정보(불일치 종류 등) 병합
        with open(self.feedback_dir / f"{prefix}.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        log.info("재학습 데이터 저장: %s", prefix)
        return f"{prefix}{suffix}"


feedback_collector = YoloFeedbackCollector()  # 전역 단일 인스턴스
