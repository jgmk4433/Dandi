"""
api/admin.py
------------
수동 검수 화면과 처리 API.

  GET  /admin/review              검수 대기 목록 (간단한 HTML 화면)
  GET  /admin/image/{event_no}    검수 화면에서 이미지 표시
  POST /admin/review/{event_no}   검수 결과 확정 -> 허브 콜백 등록

자동 판별이 확신하지 못한 건(PENDING_MANUAL)과 이의제기 건(APPEALED)이 대상이다.
자동 판정의 오류를 바로잡는 최종 회복 경로이므로, 여기서 내린 결정이 최종이다.

[브라우저 접근]
  브라우저 주소창은 X-API-Key 헤더를 붙일 수 없다.
  http://localhost:8001/admin/review?key=<API_KEY> 로 한 번 열면
  쿠키가 심어져 이후 이미지 표시와 버튼 동작이 모두 통과한다.

[XSS]
  이의제기 사유는 외부에서 들어온 문자열이다.
  이스케이프 없이 HTML 에 넣으면 담당자 브라우저에서 스크립트가 실행된다.
  아래 모든 삽입 지점에 html.escape 를 적용한다.

[운영 시] 이 화면은 담당 직원만 접근해야 한다. 현재는 API Key 로만 보호되므로
         내부망 제한이나 별도 로그인 도입을 검토할 것.
"""

import json
import logging
from html import escape
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.core import job_queue
from app.database import get_db, utcnow
from app.models import EventRecord, EventStatus, JobType
from app.schemas import SimpleResult
from app.security import api_key_guard
from app.services.review_processor import VIOLATION_HELMET_NO, VIOLATION_MULTI_RIDER

log = logging.getLogger("regional.api.admin")

router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(api_key_guard)])

# 검수 대상 상태
REVIEW_TARGETS = (EventStatus.PENDING_MANUAL, EventStatus.APPEALED)

ALLOWED_VIOLATIONS = (VIOLATION_HELMET_NO, VIOLATION_MULTI_RIDER)


def _pretty(raw: str | None) -> str:
    try:
        return json.dumps(json.loads(raw or "{}"), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return raw or "-"


def _hub_types(row: EventRecord) -> list:
    try:
        value = json.loads(row.hub_violation_types or "[]")
        return [v for v in value if v in ALLOWED_VIOLATIONS]
    except (TypeError, ValueError):
        return []


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, db: Session = Depends(get_db)):
    """검수 대기 목록을 간단한 HTML 로 보여준다(별도 프런트엔드 없이 바로 사용)."""
    rows = (
        db.query(EventRecord)
        .filter(EventRecord.status.in_(REVIEW_TARGETS))
        .order_by(EventRecord.created_at.asc())
        .limit(50)
        .all()
    )

    cards = []
    for row in rows:
        event_no = escape(row.event_no, quote=True)
        multi_checked = "checked" if VIOLATION_MULTI_RIDER in _hub_types(row) else ""

        appeal_block = (
            f'<p class="appeal"><b>이의제기 사유</b><br>{escape(row.appeal_reason)}</p>'
            if row.appeal_reason else ""
        )
        hub_block = (
            f'<p class="meta">허브 판정: {escape(", ".join(_hub_types(row)) or "없음")} '
            f'/ 신고시각: {escape(row.reported_at or "-")}</p>'
        )

        # [XSS 방지] 사용자/허브가 넣은 값(event_no, decision_reason, appeal_reason 등)은
        # 전부 escape() 를 거쳐야만 HTML 에 삽입한다.
        cards.append(f"""
        <div class="card">
          <h3>{escape(row.event_no)} <span class="badge">{escape(row.status)}</span></h3>
          {hub_block}
          <img src="/admin/image/{event_no}" alt="사건 이미지" loading="lazy">
          <p><b>판정 사유</b><br>{escape(row.decision_reason or '-')}</p>
          {appeal_block}
          <details><summary>VLM 응답</summary><pre>{escape(_pretty(row.vlm_result))}</pre></details>
          <div class="actions" data-event="{event_no}">
            <label class="chk">
              <input type="checkbox" class="multi" {multi_checked}> 2인 이상 탑승
            </label>
            <div>
              <button class="confirm" data-decision="CONFIRMED">위반 확정</button>
              <button class="reject" data-decision="REJECTED">위반 아님</button>
            </div>
          </div>
        </div>""")

    body = "".join(cards) if cards else "<p class='empty'>검수 대기 건이 없습니다.</p>"

    html = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>수동 검수</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 24px; background: #f5f5f5; color: #222; }}
  h1 {{ font-size: 20px; }}
  .card {{ background: #fff; border-radius: 8px; padding: 16px; margin-bottom: 20px;
           box-shadow: 0 1px 3px rgba(0,0,0,.12); max-width: 720px; }}
  .card h3 {{ margin: 0 0 6px; font-size: 16px; }}
  .badge {{ font-size: 12px; background: #eee; padding: 2px 8px; border-radius: 10px;
            margin-left: 8px; color: #666; }}
  .meta {{ font-size: 12px; color: #777; margin: 0 0 12px; }}
  img {{ max-width: 100%; border-radius: 4px; }}
  pre {{ background: #fafafa; padding: 10px; font-size: 12px; overflow-x: auto;
         white-space: pre-wrap; word-break: break-all; }}
  .appeal {{ background: #fff8e1; padding: 10px; border-radius: 4px; }}
  .actions {{ margin-top: 12px; display: flex; align-items: center;
              justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
  .chk {{ font-size: 13px; color: #555; }}
  button {{ padding: 8px 18px; margin-left: 8px; border: 0; border-radius: 4px;
            color: #fff; cursor: pointer; font-size: 14px; }}
  button[disabled] {{ opacity: .5; cursor: default; }}
  .confirm {{ background: #d32f2f; }}
  .reject  {{ background: #2e7d32; }}
  .empty {{ color: #777; }}
</style></head><body>
<h1>수동 검수 대기 ({len(rows)}건)</h1>
{body}
<script>
// 사건번호는 data 속성으로만 전달한다.
// onclick 문자열에 값을 끼워 넣으면 따옴표가 섞였을 때 스크립트가 깨지거나 주입될 수 있다.
document.querySelectorAll(".actions button").forEach(function (btn) {{
  btn.addEventListener("click", function () {{
    const box = btn.closest(".actions");
    decide(box.dataset.event, btn.dataset.decision, box.querySelector(".multi"));
  }});
}});

async function decide(eventNo, decision, multiBox) {{
  const reason = prompt("검수 사유를 입력하세요",
                        decision === "CONFIRMED" ? "수동 확인: 미착용" : "수동 확인: 착용");
  if (reason === null) return;

  const violations = [];
  if (decision === "CONFIRMED") {{
    violations.push("{VIOLATION_HELMET_NO}");
    if (multiBox && multiBox.checked) violations.push("{VIOLATION_MULTI_RIDER}");
  }}

  const params = new URLSearchParams({{
    decision: decision,
    reason: reason,
    violations: violations.join(",")
  }});

  const buttons = document.querySelectorAll("button");
  buttons.forEach(b => b.disabled = true);
  try {{
    const res = await fetch("/admin/review/" + encodeURIComponent(eventNo) + "?" + params.toString(),
                            {{ method: "POST", credentials: "same-origin" }});
    if (res.ok) {{ location.reload(); return; }}
    alert("처리 실패: " + await res.text());
  }} catch (e) {{
    alert("요청 실패: " + e);
  }}
  buttons.forEach(b => b.disabled = false);
}}
</script>
</body></html>"""

    response = HTMLResponse(html)

    # ?key=... 로 들어왔으면 쿠키로 옮겨 담아 이후 요청(이미지/버튼)이 통과하게 한다.
    provided = (request.query_params.get("key") or "").strip()
    if provided and settings.API_KEY:
        response.set_cookie(
            settings.ADMIN_COOKIE_NAME,
            provided,
            max_age=settings.ADMIN_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
    return response


@router.get("/image/{event_no}")
def admin_image(event_no: str, db: Session = Depends(get_db)):
    """검수 화면에서 사건 이미지를 표시한다."""
    row = db.get(EventRecord, event_no)
    if row is None or not row.image_path or not Path(row.image_path).is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "이미지가 없습니다.")
    path = Path(row.image_path)
    media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(path, media_type=media_type)


@router.post("/review/{event_no}", response_model=SimpleResult)
def submit_review(
    event_no: str,
    decision: str,
    reason: str = "",
    violations: str = "",
    db: Session = Depends(get_db),
):
    """
    담당자의 검수 결과를 확정하고 허브 콜백을 등록한다.

    decision   : CONFIRMED(위반 확정) 또는 REJECTED(위반 아님)
    violations : 쉼표 구분 위반 코드. 비우면 CONFIRMED 시 허브 판정 또는 HELMET_NO 를 승계한다.
    """
    decision = decision.upper()
    if decision not in (EventStatus.CONFIRMED, EventStatus.REJECTED):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "decision 은 CONFIRMED 또는 REJECTED 여야 합니다.",
        )

    row = db.get(EventRecord, event_no)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "사건을 찾을 수 없습니다.")

    if decision == EventStatus.CONFIRMED:
        selected = [v.strip().upper() for v in violations.split(",") if v.strip()]
        selected = [v for v in selected if v in ALLOWED_VIOLATIONS]
        if not selected:
            # 담당자가 지정하지 않았으면 허브 판정을 승계하고, 그것도 없으면 헬멧 미착용.
            selected = _hub_types(row) or [VIOLATION_HELMET_NO]
        final_types = list(dict.fromkeys(selected))  # 순서 유지하며 중복 제거
    else:
        final_types = []

    row.status = decision
    row.violation_types = json.dumps(final_types, ensure_ascii=False)
    row.decision_reason = (reason or "수동 검수")[:1000]
    row.reviewer = "MANUAL"
    row.callback_done = 0
    row.error_message = None
    row.updated_at = utcnow()

    # 검수 결과를 허브에 다시 통보한다(이의제기로 판정이 뒤집힌 경우 포함).
    job_queue.enqueue_once(db, event_no, JobType.HUB_CALLBACK)
    db.commit()

    log.info("[%s] 수동 검수 확정: %s %s", event_no, decision, final_types)
    return SimpleResult(
        status="SUCCESS",
        message=f"{decision} 처리 및 허브 통보 예약 완료 ({', '.join(final_types) or '위반 없음'})",
    )
