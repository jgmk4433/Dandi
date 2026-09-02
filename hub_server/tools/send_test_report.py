"""
tools/send_test_report.py
-------------------------
앱 없이 허브를 시험하는 스크립트.
이미지 1장을 업로드하고, 처리 상태가 끝날 때까지 조회한다.

사용법:
    python tools/send_test_report.py sample.jpg --region DAEGU
    python tools/send_test_report.py sample.jpg --count 20   # 동시 20건 부하 시험
"""

import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx

BASE = "http://localhost:8000"  # 로컬 허브 서버 주소


def submit_one(image_path: str, region: str, api_key: str) -> str:
    headers = {"X-API-Key": api_key} if api_key else {}  # API 키가 있으면 인증 헤더 추가
    with open(image_path, "rb") as f:
        files = {"file": (image_path, f, "image/jpeg")}
        data = {"region_code": region, "timestamp": datetime.now().isoformat()}
        res = httpx.post(f"{BASE}/api/v1/enforce/submit", data=data, files=files,
                         headers=headers, timeout=60.0)  # 신고 접수 API 호출
    res.raise_for_status()  # 실패 시 예외 발생
    return res.json()["trace_id"]  # 상태 조회용 trace_id 반환


def wait_status(trace_id: str, api_key: str, timeout: int = 60) -> dict:
    headers = {"X-API-Key": api_key} if api_key else {}
    deadline = time.time() + timeout
    final = {"ROUTED", "DISCARDED", "DLQ_FAILED", "ERROR"}  # 처리 종료로 간주하는 상태값 집합
    while time.time() < deadline:
        res = httpx.get(f"{BASE}/api/v1/enforce/status/{trace_id}", headers=headers, timeout=10.0)  # 상태 폴링
        body = res.json()
        if body.get("status") in final or str(body.get("status", "")).startswith("COMPLETED"):
            return body  # 종료 상태 도달 시 즉시 반환
        time.sleep(1.0)  # 1초 간격 재조회
    return {"status": "TIMEOUT", "trace_id": trace_id}  # 제한 시간 초과 처리


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--region", default="DAEGU")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--api-key", default="")
    args = parser.parse_args()

    started = time.time()
    with ThreadPoolExecutor(max_workers=min(args.count, 16)) as pool:  # 최대 16개 스레드로 동시 업로드
        trace_ids = list(pool.map(lambda _: submit_one(args.image, args.region, args.api_key),
                                  range(args.count)))
    print(f"{len(trace_ids)}건 업로드 완료 ({time.time() - started:.1f}s)")

    for trace_id in trace_ids:
        result = wait_status(trace_id, args.api_key)  # 각 건의 최종 처리 결과 대기
        print(f"  {trace_id} -> {result.get('status')} / {result.get('event_no')} "
              f"/ {result.get('error_origin') or '-'}")


if __name__ == "__main__":
    main()