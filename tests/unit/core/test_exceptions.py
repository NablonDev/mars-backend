"""Tests for the collapsed `app.core.exceptions` hierarchy (Phase 6):
`AppError` + its 6 direct subclasses, and `register_exception_handlers`'s
envelope-shaped responses.

Registered against a throwaway `FastAPI()` instance rather than
`app.main.create_app()` -- that factory still imports `app/api/`, which is
out of scope this phase (see `tests/conftest.py`'s module docstring and
`tests/unit/core/test_error_handling.py`, deliberately left broken for the
same reason). This module needs only `register_exception_handlers` itself,
so it builds its own minimal app and test routes.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, model_validator

from app.core.exceptions import (
    AppError,
    BusinessRuleError,
    ConflictError,
    ExternalServiceError,
    NotAuthenticatedError,
    NotFoundError,
    ValidationError,
    register_exception_handlers,
)
from app.core.logging import REQUEST_ID_HEADER

LEAKY_DETAIL = "postgresql://svc_user:sup3rs3cret@db.internal:5432/mars died mid-query"


# ---------------------------------------------------------------------------
# AppError base + the 6 collapsed categories
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "expected_status"),
    [
        (NotFoundError, 404),
        (ConflictError, 409),
        (ValidationError, 422),
        (BusinessRuleError, 409),
        (ExternalServiceError, 502),
        (NotAuthenticatedError, 401),
    ],
)
def test_each_category_declares_its_own_status_code(cls: type[AppError], expected_status: int) -> None:
    exc = cls(code="SOME_CODE", message="something happened")
    assert exc.status_code == expected_status
    assert isinstance(exc, AppError)


def test_app_error_exposes_code_message_and_details() -> None:
    exc = NotFoundError(
        code="PO_NOT_FOUND", message="No purchase order found", details={"purchase_order_id": "1"}
    )
    assert exc.code == "PO_NOT_FOUND"
    assert exc.message == "No purchase order found"
    assert exc.details == {"purchase_order_id": "1"}


def test_app_error_details_default_to_none() -> None:
    exc = ValidationError(code="VALIDATION_ERROR", message="bad input")
    assert exc.details is None


def test_app_error_str_includes_details_when_present() -> None:
    exc = ExternalServiceError(code="X", message="upstream failed", details={"reason": "timeout"})
    assert "upstream failed" in str(exc)
    assert "timeout" in str(exc)


def test_business_rule_error_is_distinct_from_conflict_error_but_shares_409() -> None:
    """Two different categories can share an HTTP status -- they're kept
    distinct because they mean different things (concurrent/duplicate-
    resource conflict vs. a domain-rule violation), not by status code."""
    assert BusinessRuleError.status_code == ConflictError.status_code == 409
    assert BusinessRuleError is not ConflictError


# ---------------------------------------------------------------------------
# register_exception_handlers -- envelope-shaped responses
# ---------------------------------------------------------------------------


@pytest.fixture
def error_client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/not-found")
    def _not_found() -> None:
        raise NotFoundError(code="PO_NOT_FOUND", message="No such purchase order", details={"id": "abc"})

    @app.get("/upstream")
    def _upstream() -> None:
        raise ExternalServiceError(
            code="WORKFLOW_STATE_CORRUPT", message="Upstream call failed", details={"leak": LEAKY_DETAIL}
        )

    @app.get("/bad-request")
    def _bad_request() -> None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    @app.get("/boom")
    def _boom() -> None:
        raise RuntimeError("totally unexpected")

    @app.get("/validated")
    def _validated(count: int) -> dict:
        return {"count": count}

    return TestClient(app, raise_server_exceptions=False)


def test_app_error_4xx_exposes_details_and_the_envelope_shape(error_client: TestClient) -> None:
    response = error_client.get("/not-found")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["message"] == "No such purchase order"
    assert body["data"] is None
    assert body["error"] == {"code": "PO_NOT_FOUND", "details": {"id": "abc"}}
    assert response.headers.get(REQUEST_ID_HEADER)


def test_app_error_5xx_withholds_details_from_the_response(error_client: TestClient) -> None:
    response = error_client.get("/upstream")

    assert response.status_code == 502
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "WORKFLOW_STATE_CORRUPT"
    # Server-side-only detail must never reach the client, even though the
    # call site attached it -- it may contain internal failure information.
    assert body["error"]["details"] is None
    assert LEAKY_DETAIL not in response.text


def test_bare_http_exception_is_reshaped_into_the_same_envelope(error_client: TestClient) -> None:
    response = error_client.get("/bad-request")

    assert response.status_code == 401
    body = response.json()
    assert body["success"] is False
    assert body["message"] == "Not authenticated"
    assert body["data"] is None
    assert body["error"]["code"] == "HTTP_401"


def test_unhandled_exception_returns_an_opaque_500(error_client: TestClient) -> None:
    response = error_client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "totally unexpected" not in response.text
    assert "RuntimeError" not in response.text


def test_request_validation_error_is_reshaped_into_the_envelope(error_client: TestClient) -> None:
    response = error_client.get("/validated", params={"count": "not-a-number"})

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "REQUEST_VALIDATION_ERROR"
    assert body["data"] is None
    assert isinstance(body["error"]["details"], dict)
    assert body["error"]["details"]["errors"]


# ---------------------------------------------------------------------------
# RequestValidationError ctx sanitization -- regression test for the bug
# `test_tiered_rule_without_tiers_is_rejected`
# (tests/unit/api/test_api_penalty_rules.py) used to `xfail` for.
# ---------------------------------------------------------------------------

_TIERED_PAYLOAD_ERROR_MESSAGE = "calc_type=TIERED requires at least one tier band"


class _TieredPayload(BaseModel):
    """Module-level (not nested in the test function) so FastAPI can resolve
    the `payload: _TieredPayload` string annotation against this module's
    globals under `from __future__ import annotations` -- a class local to
    the test function isn't in the route function's `__globals__`, and
    FastAPI silently falls back to treating `payload` as an unresolvable
    query param instead of the request body."""

    calc_type: str
    tiers: list | None = None

    @model_validator(mode="after")
    def _tiered_requires_tiers(self) -> _TieredPayload:
        if self.calc_type == "TIERED" and not self.tiers:
            raise ValueError(_TIERED_PAYLOAD_ERROR_MESSAGE)
        return self


def test_request_validation_error_from_bare_value_error_is_sanitized_not_500() -> None:
    """A Pydantic `model_validator` that raises a bare `ValueError` (the
    pattern `PenaltyRuleRequest._tiered_requires_tiers` uses, and the only
    supported way to express a cross-field rule) packages that `ValueError`
    into `exc.errors()`'s `ctx["error"]` as the raw exception object itself
    -- not JSON-serializable. `JSONResponse.render` has no fallback encoder,
    so `_request_validation_error_handler` used to raise an unhandled
    `TypeError` from inside the handler (surfacing as a 500) instead of
    returning the intended 422.

    Exercised through a real route + `TestClient`, per this project's
    testing-conventions skill, rather than calling the private sanitizer
    helper directly -- this proves the fix end to end: no crash, and the
    original validator message actually reaches the response body (not
    silently dropped, which a blind `jsonable_encoder` pass would do --
    it collapses a bare `ValueError` to `{}`).
    """
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/tiered")
    def _tiered(payload: _TieredPayload) -> dict:
        return {"calc_type": payload.calc_type}

    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/tiered", json={"calc_type": "TIERED"})

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "REQUEST_VALIDATION_ERROR"

    errors = body["error"]["details"]["errors"]
    assert len(errors) == 1
    # The original message survives both where Pydantic already put it ...
    assert _TIERED_PAYLOAD_ERROR_MESSAGE in errors[0]["msg"]
    # ... and in the sanitized `ctx`, proving the raw `ValueError` was
    # converted to its `str()` form rather than dropped or left unencodable.
    assert errors[0]["ctx"] == {"error": _TIERED_PAYLOAD_ERROR_MESSAGE}
