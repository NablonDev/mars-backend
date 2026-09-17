"""Lifespan-level tests for app/main.py's job-queue wiring.

Uses a real `with TestClient(app) as client:` block (not the `app`/`client`
fixtures from conftest.py, which bypass lifespan entirely via dependency
overrides -- see conftest.py's module docstring) specifically so lifespan
startup/shutdown actually run.
"""

from uuid import UUID

import pytest

import app.main as main_module
from app.core.config import Settings
from app.main import create_app
from tests.conftest import TEST_INTERNAL_API_KEY


class _FakeJobQueue:
    """Minimal JobDispatcher+JobSource double -- only `close()` matters here."""

    def __init__(self) -> None:
        self.close_calls = 0

    def dispatch(self, job_item_id: UUID, *, delay_seconds: int = 0) -> None:
        raise AssertionError("dispatch should not be called in this test")

    def close(self) -> None:
        self.close_calls += 1


class _FakeCmirService:
    """Stand-in passed to `create_app(service=...)` so lifespan's `if
    app.state.service is None: build_service()` branch is skipped --
    `build_service()` -> `Container.build()` opens a real Postgres-backed
    LangGraph checkpointer (`PostgresSaver.from_conn_string(...).setup()`),
    which is unrelated to what these tests verify (job-queue lifecycle) and
    would otherwise crash with `psycopg.OperationalError` whenever no
    Postgres is reachable.
    """


class _FakePoValidationService:
    """Same purpose as `_FakeCmirService`, for `app.state.po_service` /
    `build_po_validation_service()`."""


def test_lifespan_closes_the_job_queue_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_queue = _FakeJobQueue()
    monkeypatch.setattr(main_module, "build_job_queue", lambda settings, database: (fake_queue, fake_queue))

    from fastapi.testclient import TestClient

    app = create_app(_FakeCmirService(), _FakePoValidationService())

    with TestClient(app) as client:
        assert app.state.job_queue == (fake_queue, fake_queue)
        assert fake_queue.close_calls == 0
        # Not exercising any route here -- just proving lifespan wiring;
        # HTTP-level behavior is covered by the API test suite.
        del client

    assert fake_queue.close_calls == 1


def test_lifespan_builds_the_queue_once_and_reuses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    build_calls: list[object] = []

    def _spy_build_job_queue(settings, database):
        fake_queue = _FakeJobQueue()
        build_calls.append(fake_queue)
        return fake_queue, fake_queue

    monkeypatch.setattr(main_module, "build_job_queue", _spy_build_job_queue)

    from fastapi.testclient import TestClient

    app = create_app(_FakeCmirService(), _FakePoValidationService())

    with TestClient(app) as client:
        client.get("/api/v1/health")
        client.get("/api/v1/health")

    # Built exactly once for the whole process lifetime, not once per request.
    assert len(build_calls) == 1


def test_docs_enabled_defaults_to_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_DOCS_ENABLED", raising=False)

    assert Settings(_env_file=None).app.docs_enabled is True


def test_create_app_does_not_mount_docs_routes_by_default() -> None:
    """No `with` block, so lifespan never runs -- this only checks the
    FastAPI app object's own docs/redoc/openapi routing, not a live server."""
    app = create_app(
        settings=Settings(
            _env_file=None, app={"internal_api_key": TEST_INTERNAL_API_KEY, "docs_enabled": False}
        )
    )

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_create_app_mounts_docs_routes_when_explicitly_enabled() -> None:
    app = create_app(
        settings=Settings(
            _env_file=None, app={"internal_api_key": TEST_INTERNAL_API_KEY, "docs_enabled": True}
        )
    )

    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"
