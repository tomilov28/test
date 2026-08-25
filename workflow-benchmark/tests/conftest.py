import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def require_engine_fixture(engine_name: str, probe_url: str):
    """Module-scoped autouse fixture that FAILS (never skips) when the target
    engine is unreachable (audit A08: a down target engine must fail the target,
    not silently skip its tests)."""

    @pytest.fixture(scope="module", autouse=True)
    def _require_engine():
        import time

        import httpx

        deadline = time.time() + 15.0
        reachable = False
        while time.time() < deadline:
            try:
                r = httpx.get(probe_url, timeout=3.0)
                if r.status_code < 500:
                    reachable = True
                    break
            except Exception:
                pass
            time.sleep(1.0)
        if not reachable:
            pytest.fail(
                f"{engine_name} engine not reachable at {probe_url} - "
                "target engine down must FAIL, not skip"
            )

    return _require_engine
