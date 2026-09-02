"""
tools/send_test_event.py
------------------------
허브 없이 지역 서버를 시험한다.
중앙 허브 services/enforcement_processor.py 가 만드는 payload 를 그대로 재현한다.

사용법
  python tools/send_test_event.py sample.jpg
      허브 YOLO 학습 전 상태 (UNVERIFIED). crop_targets 없이 전송 -> VLM 전체 판별
  python tools/send_test_event.py sample.jpg --mode vlm
      VLM_REQUIRED. 헬멧 박스 좌표 전달 -> 크롭 후 HELMET_VERIFY
  python tools/send_test_event.py sample.jpg --mode confirmed
      VIOLATION. 허브에서 확정된 건 -> VLM 없이 즉시 CONFIRMED
  python tools/send_test_event.py sample.jpg --mode unknown
      HEADWEAR_UNKNOWN. 머리 박스 미검출 -> 사람 박스로 판별

  --count 5           같은 이미지를 5건 전송(부하 확인)
  --watch             전송 후 판정이 끝날 때까지 상태를 폴링
  --callback-url URL  콜백 주소 지정 (기본: 없음 = 콜백 생략)
"""

import argparse
import base64
import json
import random
import string
import sys
import time
from pathlib import Path

import httpx

# --- 허브 core/trace.py 와 동일한 규칙 ---
_RANDOM_CHARS = "".join(
    c for c in string.ascii_uppercase + string.digits if c not in "OI01"
)


def make_event_no(region_number: str = "02") -> str:
    """허브 형식: PM + 지역번호(2) + 무작위 4자리  예) PM02A1BC"""
    suffix = "".join(random.choices(_RANDOM_CHARS, k=4))
    return f"PM{region_number}{suffix}"


def image_size(path: Path):
    try:
        from PIL import Image

        with Image.open(path) as img:
            return img.size
    except Exception:
        return (None, None)


def pad_box(bbox, ratio, width, height):
    """허브 violation_rules._pad_box() 와 동일한 계산."""
    x1, y1, x2, y2 = bbox
    px, py = (x2 - x1) * ratio, (y2 - y1) * ratio
    nx1, ny1 = max(0.0, x1 - px), max(0.0, y1 - py)
    nx2, ny2 = x2 + px, y2 + py
    if width and height:
        nx2, ny2 = min(float(width), nx2), min(float(height), ny2)
    return [round(nx1), round(ny1), round(nx2), round(ny2)]


def build_crop_target(roi_id, purpose, bbox, width, height, pad_ratio, label, conf):
    """허브 violation_rules._crop_target() 과 동일한 구조."""
    padded = pad_box(bbox, pad_ratio, width, height)
    crop_w, crop_h = padded[2] - padded[0], padded[3] - padded[1]
    return {
        "roi_id": roi_id,
        "purpose": purpose,
        "label": label,
        "conf": round(conf, 4),
        "bbox": [round(v) for v in bbox],
        "padded_bbox": padded,
        "crop_px": [crop_w, crop_h],
        "low_resolution": min(crop_w, crop_h) < 48,   # 허브 CROP_MIN_EDGE_PX
    }


def build_payload(mode, image_path: Path, region, callback_url):
    width, height = image_size(image_path)
    raw = image_path.read_bytes()
    event_no = make_event_no()

    # 화면 중앙 가이드라인 기준으로 가상의 머리/사람 박스를 만든다.
    if width and height:
        cx, cy = width / 2, height / 2
        head = (cx - width * 0.06, cy - height * 0.22,
                cx + width * 0.06, cy - height * 0.08)
        person = (cx - width * 0.15, cy - height * 0.25,
                  cx + width * 0.15, cy + height * 0.30)
    else:
        head = (350.0, 150.0, 450.0, 250.0)
        person = (300.0, 120.0, 500.0, 600.0)

    violation_types, hub_verified, requires_vlm, targets = [], False, True, []

    if mode == "confirmed":
        # 허브 VIOLATION: bare head 또는 2인 이상 탑승 -> 좌표 없음
        violation_types, hub_verified, requires_vlm = ["HELMET_NO"], True, False
    elif mode == "vlm":
        # 허브 VLM_REQUIRED: 헬멧 박스 좌표 (HELMET_CROP_PAD_RATIO=0.40)
        violation_types = ["HELMET_NO"]
        targets = [build_crop_target("h1", "HELMET_VERIFY", head, width, height,
                                     0.40, "helmet", 0.87)]
    elif mode == "unknown":
        # 머리 박스 미검출 -> 사람 박스 (PERSON_CROP_PAD_RATIO=0.10)
        violation_types = ["HELMET_NO"]
        targets = [build_crop_target("u1", "HEADWEAR_UNKNOWN", person, width, height,
                                     0.10, "person", 0.72)]
    # mode == "full": 허브 UNVERIFIED — 판정 없이 원본만 전달 (현재 실제 상태)

    payload = {
        "trace_id": f"{random.randint(1, 999999):06d}",
        "event_no": event_no,
        "region_code": region,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "violation_types": violation_types,
        "hub_verified": hub_verified,
        "requires_vlm": requires_vlm,
        "image_base64": base64.b64encode(raw).decode("ascii"),
        "image": {
            "width": width,
            "height": height,
            "coord_system": "absolute_xyxy_topleft",
            "apply_exif_rotation": False,
        },
        "crop_targets": targets,
        "crop_rois": targets,          # 허브가 별칭을 함께 보낸다
        "callback_url": callback_url,
    }
    return payload, event_no


def watch(client, base, event_no, timeout=300):
    """판정이 끝날 때까지 상태를 폴링한다."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            res = client.get(f"{base}/api/v1/event/{event_no}")
            if res.status_code != 200:
                print(f"  조회 실패 HTTP {res.status_code}")
                return
            data = res.json()
        except Exception as exc:
            print(f"  조회 오류: {exc}")
            return

        state = data.get("status")
        if state != last:
            print(f"  [{event_no}] {state}")
            last = state
        if state in ("CONFIRMED", "REJECTED", "PENDING_MANUAL", "ERROR"):
            print(f"  사유   : {data.get('decision_reason')}")
            print(f"  위반   : {data.get('violation_types')}")
            summary = data.get("vlm_summary")
            if summary:
                print(f"  VLM    : {json.dumps(summary, ensure_ascii=False)}")
            return
        time.sleep(3)  # 3초 간격으로 재조회
    print(f"  [{event_no}] 시간 내에 판정이 끝나지 않았습니다.")


def main() -> int:
    parser = argparse.ArgumentParser(description="지역 서버 시험 전송")
    parser.add_argument("image", help="전송할 이미지 파일")
    parser.add_argument("--url", default="http://localhost:8001", help="지역 서버 주소")
    parser.add_argument("--key", default="", help="X-API-Key (허브와 동일한 값)")
    parser.add_argument("--region", default="DAEGU")
    parser.add_argument("--mode", default="full",
                        choices=["full", "vlm", "confirmed", "unknown"])
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--watch", action="store_true", help="판정 완료까지 폴링")
    parser.add_argument("--callback-url", default="", help="허브 콜백 주소(비우면 콜백 생략)")
    args = parser.parse_args()

    path = Path(args.image)
    if not path.is_file():
        print(f"[오류] 파일을 찾을 수 없습니다: {path}")
        return 1

    headers = {"X-API-Key": args.key} if args.key else {}
    base = args.url.rstrip("/")

    with httpx.Client(timeout=60.0, headers=headers) as client:
        try:
            health = client.get(f"{base}/health")
            print(f"health: HTTP {health.status_code} {health.text[:120]}")
        except Exception as exc:
            print(f"[오류] 서버에 연결할 수 없습니다: {exc}")
            return 1

        sent = []
        for index in range(args.count):
            payload, event_no = build_payload(
                args.mode, path, args.region, args.callback_url or None
            )
            started = time.time()
            try:
                res = client.post(f"{base}/api/v1/enforce", json=payload)
            except Exception as exc:
                print(f"[{index + 1}/{args.count}] 전송 실패: {exc}")
                continue

            elapsed = time.time() - started
            ok = 200 <= res.status_code < 300
            print(f"[{index + 1}/{args.count}] {event_no} mode={args.mode} "
                  f"-> HTTP {res.status_code} ({elapsed:.2f}s) {res.text[:160]}")
            if ok:
                sent.append(event_no)
            if elapsed > 25:
                print("  [경고] 응답이 25초를 넘었습니다. 허브 타임아웃(30초)에 걸릴 수 있습니다.")

        if args.watch and sent:
            print("\n판정 대기 중...")
            for event_no in sent:
                watch(client, base, event_no)

    return 0


if __name__ == "__main__":
    sys.exit(main())
