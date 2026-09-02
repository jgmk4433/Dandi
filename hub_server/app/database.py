"""
database.py
-----------
SQLAlchemy 엔진 / 세션 관리.

[중요] SQLite 를 여러 스레드(웹 요청 + 워커 + UI)에서 동시에 쓰기 때문에
아래 PRAGMA 설정이 반드시 필요하다.
  - journal_mode=WAL : 읽기와 쓰기가 서로를 막지 않게 한다(동시성 확보).
  - busy_timeout     : 락 충돌 시 즉시 실패하지 않고 지정 시간까지 대기.
이 설정이 없으면 트래픽이 몰릴 때 "database is locked" 오류가 발생한다.
"""

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

# SQLite 는 기본적으로 생성 스레드에서만 사용 가능하므로 해제한다.
connect_args = {"check_same_thread": False, "timeout": 15} if _is_sqlite else {}

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,  # 끊어진 커넥션 자동 감지
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _apply_sqlite_pragmas(dbapi_connection, _connection_record):
        """커넥션이 새로 열릴 때마다 동시성 관련 PRAGMA 를 적용한다."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=15000")  # 15초 대기
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)  # 세션 팩토리
Base = declarative_base()  # 모델 클래스들의 공통 베이스


def utcnow() -> datetime:
    """
    tz 정보가 없는 UTC 시각을 반환한다.
    SQLite 는 타임존을 저장하지 않으므로 프로젝트 전체에서 'naive UTC' 로 통일한다.
    (datetime.utcnow() 는 Python 3.12 부터 deprecated 이므로 직접 정의)
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


@contextmanager
def session_scope():
    """
    워커 스레드나 UI 처럼 FastAPI 의존성을 쓸 수 없는 곳에서 사용하는 세션 헬퍼.
    정상 종료 시 commit, 예외 발생 시 rollback 후 항상 close 한다.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db():
    """FastAPI 엔드포인트용 세션 의존성 (Depends(get_db))."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """테이블이 없으면 생성하고, 기존 테이블에 없는 컬럼은 추가한다."""
    from app import models  # 모델 등록을 위한 임포트 (순환 참조 방지용 지연 임포트)

    Base.metadata.create_all(bind=engine)  # 정의된 모델 기준으로 테이블 생성
    _add_missing_columns()


def _add_missing_columns() -> None:
    """
    간단한 자동 마이그레이션.
    버전이 올라가며 컬럼이 추가돼도 create_all 은 기존 테이블을 바꾸지 않으므로,
    누락된 컬럼을 ALTER TABLE 로 채워 넣는다.
    (Alembic 같은 별도 도구를 설치하지 않아도 되게 하기 위한 최소 구현.
     새 컬럼은 반드시 nullable 이어야 한다.)
    """
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue  # 아직 생성되지 않은 테이블은 건너뜀
            existing = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue  # 이미 있는 컬럼은 스킵
                column_type = column.type.compile(engine.dialect)
                try:
                    connection.execute(
                        text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column_type}")
                    )
                except Exception:
                    # 컬럼 추가가 불가능한 경우(NOT NULL 등)는 건너뛴다.
                    pass
