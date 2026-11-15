import os
from typing import Generator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from apps.api.config import load_settings


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""
    pass


_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker[Session]] = None


def get_engine(database_url: Optional[str] = None) -> Engine:
    """
    Get or create the shared SQLAlchemy engine.
    If database_url is provided, creates and returns a new engine for that URL.
    Otherwise, uses the DATABASE_URL from application configuration.
    """
    global _engine
    if database_url:
        return create_engine(database_url, pool_pre_ping=True)

    if _engine is None:
        url = load_settings().require_postgres_url()
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
        )
    return _engine


def get_session_factory(engine: Optional[Engine] = None) -> sessionmaker[Session]:
    """Get or create the sessionmaker factory bound to the engine."""
    global _session_factory
    if engine is not None:
        return sessionmaker(autocommit=False, autoflush=False, bind=engine)

    if _session_factory is None:
        eng = get_engine()
        _session_factory = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    return _session_factory


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for yielding database sessions."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def reset_engine() -> None:
    """Dispose current engine and reset cached engine/session_factory (useful in tests)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
