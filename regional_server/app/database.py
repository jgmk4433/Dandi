"""
database.py
-----------
SQLAlchemy 엔진/세션. 허브와 동일한 구성이다.

SQLite 를 웹 요청 스레드와 워커 스레드가 동시에 쓰므로 WAL 모드가 필요하다.
이 설정이 없으면 처리가 몰릴 때 "database is locked" 가 발생한다.
"""

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False, "timeout": 15} if _is_sqlite else {}

engine = create_engine(settings.DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _apply_sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def utcnow() -> datetime:
    """tz 정보 없는 UTC 시각. 프로젝트 전체에서 naive UTC 로 통일한다."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@contextmanager
def session_scope():
    """워커 스레드용 세션 헬퍼."""
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
    """FastAPI 의존성."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401  (테이블 등록용)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """컬럼이 추가돼도 별도 마이그레이션 도구 없이 반영되도록 하는 최소 구현."""
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                try:
                    connection.execute(
                        text(
                            f"ALTER TABLE {table.name} ADD COLUMN "
                            f"{column.name} {column.type.compile(engine.dialect)}"
                        )
                    )
                except Exception:
                    pass
