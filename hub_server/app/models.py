"""
models.py
---------
DB 테이블 정의.

1) CentralEventLog : 신고 1건의 최종 상태를 기록하는 이력 테이블 (모니터 UI 가 조회)
2) EnforcementJob  : 백그라운드 처리 대기열. Redis 대신 DB 테이블을 큐로 사용하므로
                     서버가 갑자기 종료돼도 작업이 사라지지 않는다.
"""

from sqlalchemy import Column, DateTime, Integer, String, Text, Index

from app.database import Base, utcnow


class Counter(Base):
    """
    순번 관리용 단순 카운터.
    trace_id 를 000001 부터 순서대로 발급하기 위해 사용한다.
    (SQLite AUTOINCREMENT 는 삭제 후 재사용 규칙이 달라 별도 카운터를 둔다)
    """

    __tablename__ = "counters"

    TRACE_SEQ = "trace_id"  # 카운터 이름 상수

    name = Column(String(32), primary_key=True)
    value = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class EventStatus:
    """CentralEventLog.status 에 들어가는 값 모음 (문자열 오타 방지용)."""

    RECEIVED = "RECEIVED"        # 업로드 접수 완료, 처리 대기
    PROCESSING = "PROCESSING"    # 워커가 처리 중
    DISCARDED = "DISCARDED"      # 허브 재검증 결과 위반 아님 -> 폐기
    PENDING = "PENDING"          # 위반 확정, 지역 서버 전송 전
    ROUTED = "ROUTED"            # 지역 서버 전송 성공
    RETRYING = "RETRYING"        # 전송 실패, 재시도 대기
    DLQ_FAILED = "DLQ_FAILED"    # 재시도 모두 실패 (수동 확인 필요)
    APPEALED = "APPEALED"        # 이의제기 접수 -> 지역 서버 재배정
    ERROR = "ERROR"              # 처리 중 예외 발생


class JobState:
    """EnforcementJob.state 에 들어가는 값 모음."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class JobType:
    """작업 종류. 하나의 큐에서 종류별로 다른 처리기가 실행된다."""

    ENFORCE = "ENFORCE"                # 신고 처리 -> 검증 -> 지역 서버 전송
    FEEDBACK_FETCH = "FEEDBACK_FETCH"  # 판정 불일치 건의 이미지를 지역 서버에서 회수


class CentralEventLog(Base):
    """신고 건별 처리 이력 (trace_id 기준 1행)."""

    __tablename__ = "central_event_logs"

    # 접수 순번. 000001 형식 문자열 (core/trace.py 참고)
    trace_id = Column(String(64), primary_key=True, index=True)
    # 사건 번호. PM + 지역번호 + 무작위 4자리 (예: PM02A1BC)
    event_no = Column(String(64), unique=True, index=True, nullable=True)
    region_code = Column(String(16), nullable=False, index=True)

    # VIOLATION    : 허브에서 위반 확정 (bare head 또는 2인 이상 탑승)
    # VLM_REQUIRED : 헬멧 착용으로 보임 -> 지역 서버 VLM 이 헬멧/모자 2차 판별
    # PASSED       : 단속 대상 아님 (전송하지 않고 폐기)
    # UNVERIFIED   : 허브 재검증 없이 앱 판정으로 통과 (모델 학습 중일 때)
    yolo_result = Column(String(20), nullable=False)
    status = Column(String(24), nullable=False, index=True)
    violation_types = Column(Text, nullable=True)     # 최종 위반 종류 JSON (예: ["HELMET_NO"])
    # 앱은 위반을 판정하지 않는다. person+escooter 공존만 확인하고 보낸다.
    # 이 컬럼은 앱이 무엇을 검출했는지 참고용으로 남기는 값이며 판정 근거가 아니다.
    app_detected = Column(Text, nullable=True)        # 예: ["person","escooter"]
    verify_detail = Column(Text, nullable=True)       # 허브 판정 근거 JSON (박스 수, 규칙 결과 등)

    # 원본 이미지는 DB 가 아니라 디스크에 저장하고 경로만 남긴다.
    # (대용량 바이너리를 DB 에 넣으면 파일이 급격히 커지고 조회가 느려진다)
    image_path = Column(String(512), nullable=True)
    image_bytes = Column(Integer, nullable=True)      # 원본 용량(byte)
    image_width = Column(Integer, nullable=True)      # 앱이 800px 로 줄여 보내는지 확인용
    image_height = Column(Integer, nullable=True)

    error_origin = Column(String(64), nullable=True)  # 예: REGIONAL_FORWARDING
    error_message = Column(Text, nullable=True)       # 예외 상세 메시지

    created_at = Column(DateTime, default=utcnow, index=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class EnforcementJob(Base):
    """백그라운드 처리 대기열 1건."""

    __tablename__ = "enforcement_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 한 신고(trace_id)에 대해 처리 작업과 사후 회수 작업이 각각 생길 수 있으므로
    # unique 제약을 두지 않는다.
    trace_id = Column(String(64), nullable=False, index=True)
    job_type = Column(String(24), nullable=False, default=JobType.ENFORCE, index=True)
    region_code = Column(String(16), nullable=False)
    reported_at = Column(String(40), nullable=True)   # 앱이 보낸 촬영 시각 문자열 원본
    image_path = Column(String(512), nullable=True)   # 회수 작업에는 없음
    context = Column(Text, nullable=True)             # 작업 종류별 부가 정보 JSON

    state = Column(String(16), nullable=False, default=JobState.QUEUED, index=True)
    attempts = Column(Integer, nullable=False, default=0)
    # 이 시각 이후에 처리 대상이 된다 (재시도 백오프에 사용)
    next_attempt_at = Column(DateTime, default=utcnow, index=True)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


# 워커가 "처리 가능한 작업"을 찾을 때 쓰는 복합 인덱스 (건수가 많아질 때 속도 확보)
Index("ix_jobs_pickup", EnforcementJob.state, EnforcementJob.next_attempt_at)
