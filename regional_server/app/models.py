"""
models.py
---------
DB 테이블 정의.

1) EventRecord : 단속 건 1건. 허브에서 받은 내용과 최종 판정 결과를 보관한다.
2) ReviewJob   : 백그라운드 작업 대기열. VLM 판별과 허브 콜백을 여기에 넣는다.
                 (Redis/Celery 없이 DB 테이블을 큐로 쓰므로 재시작해도 유실되지 않는다)
"""

from sqlalchemy import Column, DateTime, Index, Integer, String, Text

from app.database import Base, utcnow


class EventStatus:
    """EventRecord.status 값."""

    RECEIVED = "RECEIVED"            # 허브에서 수신, 판별 대기
    REVIEWING = "REVIEWING"          # VLM 판별 중
    CONFIRMED = "CONFIRMED"          # 위반 확정
    REJECTED = "REJECTED"            # 위반 아님
    PENDING_MANUAL = "PENDING_MANUAL"  # 자동 판별 불가 -> 수동 검수 대기
    APPEALED = "APPEALED"            # 이의제기 접수 -> 재심의 대기
    ERROR = "ERROR"                  # 처리 오류


class JobType:
    """ReviewJob.job_type 값. 하나의 큐에서 종류별로 다른 처리기가 실행된다."""

    VLM_REVIEW = "VLM_REVIEW"        # 크롭 -> VLM 판별 -> 판정
    HUB_CALLBACK = "HUB_CALLBACK"    # 판정 결과를 허브로 통보


class JobState:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class EventRecord(Base):
    """단속 건 (event_no 기준 1행)."""

    __tablename__ = "event_records"

    # 허브 core/trace.py 형식: PM + 지역번호(2) + 무작위 4자리  예) PM02A1BC
    event_no = Column(String(64), primary_key=True, index=True)
    # 허브 접수 순번. 000001 형식 문자열 (허브에서는 primary key)
    trace_id = Column(String(64), index=True, nullable=True)
    region_code = Column(String(16), nullable=False)
    reported_at = Column(String(40), nullable=True)            # 앱이 촬영한 시각 문자열

    # ---- 허브가 보낸 판정 정보 ----
    hub_verified = Column(Integer, default=0)                  # 1 = 허브에서 위반 확정
    requires_vlm = Column(Integer, default=1)                  # 1 = VLM 판별 필요
    hub_violation_types = Column(Text, nullable=True)          # JSON 배열
    crop_targets = Column(Text, nullable=True)                 # JSON 배열 (크롭 좌표)
    image_width = Column(Integer, nullable=True)
    image_height = Column(Integer, nullable=True)

    # ---- 이미지 ----
    # 원본은 이 서버가 장기 보관한다(허브는 전송 후 즉시 삭제).
    image_path = Column(String(512), nullable=True)

    # ---- 최종 판정 ----
    status = Column(String(24), nullable=False, index=True)
    violation_types = Column(Text, nullable=True)              # JSON 배열
    vlm_result = Column(Text, nullable=True)                   # VLM 원본 응답 JSON (감사용)
    decision_reason = Column(Text, nullable=True)              # 판정 사유(사람이 읽는 문장)
    reviewer = Column(String(32), nullable=True)               # AUTO / MANUAL
    appeal_reason = Column(Text, nullable=True)                # 이의제기 사유

    callback_url = Column(String(512), nullable=True)          # 허브가 알려준 콜백 주소
    callback_done = Column(Integer, default=0, index=True)     # 1 = 허브 통보 완료

    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, index=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, index=True)


class ReviewJob(Base):
    """백그라운드 작업 1건."""

    __tablename__ = "review_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_no = Column(String(64), nullable=False, index=True)
    job_type = Column(String(24), nullable=False, default=JobType.VLM_REVIEW, index=True)
    context = Column(Text, nullable=True)                      # 작업별 부가 정보 JSON

    state = Column(String(16), nullable=False, default=JobState.QUEUED, index=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, default=utcnow, index=True)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, index=True)


Index("ix_jobs_pickup", ReviewJob.state, ReviewJob.next_attempt_at)
# 콜백 미전송 스윕이 쓰는 인덱스
Index("ix_events_callback_sweep", EventRecord.callback_done, EventRecord.status)
