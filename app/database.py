"""Database engine, session factory, and FastAPI dependency."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_settings.ensure_dirs()

# check_same_thread=False is required because FastAPI may use the connection
# across threads; SQLAlchemy's session scoping keeps usage safe.
engine = create_engine(
    _settings.database_url,
    connect_args={"check_same_thread": False},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create tables. Models are imported for their side effects."""
    from . import models  # noqa: F401  (registers mappers on Base)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


# Columns added after the initial schema. SQLite's create_all won't ALTER an
# existing table, so we add any missing ones here (idempotent, dev-friendly).
_LIGHTWEIGHT_MIGRATIONS = {
    "pools": {
        "location_name": "VARCHAR(160)",
        "latitude": "FLOAT",
        "longitude": "FLOAT",
        "notes": "TEXT",
    },
    "readings": {
        "ec": "FLOAT",
        "image_path": "VARCHAR(255)",
    },
}


def _add_missing_columns() -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        for table, columns in _LIGHTWEIGHT_MIGRATIONS.items():
            existing = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
