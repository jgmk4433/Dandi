"""
core/image_store.py
-------------------
이미지 저장 / 크롭 / 업스케일.

[좌표 규약 - 매우 중요]
  허브가 보내는 좌표는 전달받은 이미지 픽셀 기준 절대값 [x1, y1, x2, y2] 이며
  원점은 좌상단이다. 앱이 EXIF 회전을 픽셀에 이미 구워 보내므로
  **디코딩할 때 EXIF 회전을 다시 적용하면 안 된다.**
  (PIL 의 ImageOps.exif_transpose() 를 호출하지 말 것. 좌표가 90도 틀어진다)

[크롭 화질]
  앱 전송 해상도 800px 는 YOLO 학습 해상도에 묶여 있어 변경할 수 없다.
  따라서 크롭이 작을 때의 보정은 이 서버가 담당한다.
  허브가 low_resolution=true(최소변 48px 미만)로 표시한 크롭은
    1) 영역을 LOW_RES_EXPAND_RATIO 만큼 넓히고
    2) UPSCALE_MIN_EDGE 까지 업스케일한다.
  업스케일이 없던 정보를 만들지는 못한다. 실질적 개선은 넓은 크롭과
  원본 전체 폴백(FALLBACK_TO_FULL_IMAGE)에서 나온다.
"""

import base64
import io
import logging
import os
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PIL import Image

from app.config import settings

log = logging.getLogger("regional.image")

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class ImageDataError(ValueError):
    """수신 데이터 자체가 잘못된 경우. 재시도해도 결과가 같으므로 400 으로 응답한다."""


def _strip_data_url(image_base64: str) -> str:
    """`data:image/jpeg;base64,....` 형태로 와도 받아들인다."""
    stripped = image_base64.strip()
    if stripped.startswith("data:") and "," in stripped[:128]:
        return stripped.split(",", 1)[1]
    return stripped


def save_base64(event_no: str, image_base64: str) -> Tuple[str, Optional[Tuple[int, int]]]:
    """
    허브가 보낸 base64 이미지를 디스크에 저장한다.
    반환: (저장 경로, (가로, 세로))

    실패 시
      - 데이터가 잘못됨          -> ImageDataError (호출측이 400 으로 변환)
      - 디스크/권한 등 환경 문제 -> OSError        (호출측이 500 으로 변환, 허브가 재시도)
    """
    try:
        raw = base64.b64decode(_strip_data_url(image_base64))
    except Exception as exc:
        raise ImageDataError(f"base64 디코딩 실패: {exc}") from exc

    if len(raw) < settings.MIN_IMAGE_BYTES:
        raise ImageDataError(f"이미지 데이터가 너무 작습니다({len(raw)} bytes). 손상 의심")
    if len(raw) > settings.MAX_IMAGE_BYTES:
        raise ImageDataError(
            f"이미지가 상한을 초과했습니다({len(raw)} bytes > {settings.MAX_IMAGE_BYTES})"
        )

    # 실제로 열리는 이미지인지 먼저 확인한다.
    # 여기서 걸러내지 않으면 저장은 성공하고 몇 분 뒤 워커에서 터져 원인 추적이 어려워진다.
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()                      # verify() 후에는 재사용 불가
        with Image.open(io.BytesIO(raw)) as probe:
            size = probe.size
    except Exception as exc:
        raise ImageDataError(f"유효한 이미지가 아닙니다: {exc}") from exc

    sub_dir = Path(settings.IMAGE_DIR) / time.strftime("%Y%m%d")
    sub_dir.mkdir(parents=True, exist_ok=True)

    suffix = ".png" if raw[:8] == _PNG_MAGIC else ".jpg"
    target = sub_dir / f"{_safe_name(event_no)}{suffix}"

    # 임시 파일에 쓰고 교체한다. 저장 도중 프로세스가 죽어도 반쪽 파일이 남지 않는다.
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, target)

    return str(target), size


def _safe_name(event_no: str) -> str:
    """파일명에 쓸 수 없는 문자를 제거한다(경로 조작 방지 포함)."""
    return "".join(c for c in event_no if c.isalnum() or c in ("-", "_"))[:64] or "unknown"


def load(image_path: str) -> Image.Image:
    """
    이미지를 연다. EXIF 회전을 적용하지 않는 것이 핵심이다.
    (exif_transpose 를 부르면 허브가 준 좌표와 어긋난다)

    [주의] with 로 감싸 원본 핸들을 반드시 닫는다.
           닫지 않으면 Windows 에서 해당 파일을 삭제할 수 없어
           보관기간 정리(cleanup_expired)가 조용히 실패한다.
    """
    with Image.open(image_path) as img:
        return img.convert("RGB")


def expand_bbox(bbox: Sequence[float], ratio: float) -> List[float]:
    """박스 중심을 유지한 채 ratio 비율만큼 넓힌다(경계 클램핑은 crop_region 이 담당)."""
    if not bbox or len(bbox) < 4 or ratio <= 0:
        return list(bbox[:4]) if bbox and len(bbox) >= 4 else []

    x1, y1, x2, y2 = (float(v) for v in bbox[:4])
    dx = (x2 - x1) * ratio / 2.0
    dy = (y2 - y1) * ratio / 2.0
    return [x1 - dx, y1 - dy, x2 + dx, y2 + dy]


def crop_region(
    image: Image.Image,
    bbox: Sequence[float],
    upscale: bool = False,
    expand_ratio: float = 0.0,
) -> Optional[Image.Image]:
    """
    padded_bbox 로 잘라낸다. 허브가 경계 클램핑까지 끝낸 값이지만,
    이미지 해상도 범위를 벗어나지 않도록 클램핑 보완 처리를 적용한다.

    expand_ratio > 0 이면 크롭 전에 영역을 넓힌다(저해상도 건 보정).
    upscale=True 이면 최소변이 UPSCALE_MIN_EDGE 가 되도록 확대한다.
    """
    if not bbox or len(bbox) < 4:
        return None

    box = expand_bbox(bbox, expand_ratio) if expand_ratio > 0 else list(bbox[:4])
    if len(box) < 4:
        return None

    # 좌표가 뒤집혀 오는 경우까지 방어한다.
    x1, x2 = sorted((box[0], box[2]))
    y1, y2 = sorted((box[1], box[3]))

    x1 = max(0, min(image.width, int(round(x1))))
    y1 = max(0, min(image.height, int(round(y1))))
    x2 = max(0, min(image.width, int(round(x2))))
    y2 = max(0, min(image.height, int(round(y2))))

    if x2 - x1 < 2 or y2 - y1 < 2:
        log.warning("크롭 영역이 너무 작습니다: %s (이미지 %dx%d)", bbox, image.width, image.height)
        return None

    cropped = image.crop((x1, y1, x2, y2))

    if upscale:
        shortest = min(cropped.width, cropped.height)
        if 0 < shortest < settings.UPSCALE_MIN_EDGE:
            factor = settings.UPSCALE_MIN_EDGE / shortest
            new_size = (max(1, int(cropped.width * factor)), max(1, int(cropped.height * factor)))
            # LANCZOS 는 확대 시 가장 무난한 품질을 낸다.
            # 초해상도 모델을 붙일 경우 이 부분을 교체하면 된다.
            cropped = cropped.resize(new_size, Image.LANCZOS)
    return cropped


def to_base64(image: Image.Image, quality: int = 92) -> str:
    """VLM 에 넣기 위해 JPEG base64 로 변환한다."""
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def file_to_base64(image_path: str) -> str:
    return base64.b64encode(Path(image_path).read_bytes()).decode("ascii")


def draw_boxes(image: Image.Image, boxes: List[Sequence[float]]) -> Image.Image:
    """
    (선택) 원본 전체를 VLM 에 보낼 때 관심 인물을 사각형으로 표시한다.
    크롭만으로 판별이 어려운 경우, 어디를 봐야 하는지 알려주면 정확도가 올라간다.
    """
    from PIL import ImageDraw

    marked = image.copy()
    drawer = ImageDraw.Draw(marked)
    for box in boxes:
        if box and len(box) >= 4:
            drawer.rectangle([int(box[0]), int(box[1]), int(box[2]), int(box[3])],
                             outline=(255, 0, 0), width=3)
    return marked


def delete(image_path: Optional[str]) -> None:
    if not image_path:
        return
    try:
        Path(image_path).unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_expired() -> int:
    """
    보관 기간이 지난 이미지를 삭제한다.
    [주의] 이의제기 수동 검수와 허브의 재학습 회수가 이 기간에 의존한다.
           허브는 콜백 직후 GET /image 를 호출하므로
           IMAGE_RETENTION_DAYS 를 이의제기 가능 기간보다 짧게 잡지 말 것.
    """
    cutoff = time.time() - settings.IMAGE_RETENTION_DAYS * 86400
    root = Path(settings.IMAGE_DIR)
    if not root.exists():
        return 0

    removed = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            log.warning("이미지 삭제 실패 %s: %s", path, exc)
    return removed


def disk_usage_mb() -> float:
    root = Path(settings.IMAGE_DIR)
    if not root.exists():
        return 0.0
    total = 0
    for p in root.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return round(total / (1024 * 1024), 1)
