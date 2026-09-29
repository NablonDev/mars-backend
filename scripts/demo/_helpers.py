"""Shared auth/error-formatting/poll helpers for the httpx-based demo
scripts.

Was shared by `demo_fine_projection_summary.py` and `run_end_to_end_demo.py`,
which both defined `_error_message`/`_poll_until_ready` verbatim -- kept here
once so the two scripts stop carrying duplicate copies.

`_poll_until_ready` is shared again as of this pass: both domains now expose
a dedicated per-(purchase_order_id, as_of_date) summary-read route
(`GET /penalties/projections/summary`, `GET /penalties/mitigations/summary`)
with the identical `{purchase_order_id, as_of_date, status, summary,
error_message}` response shape, so one function covers both --
`demo_penalty_projection_summary.py`/`demo_penalty_mitigation_summary.py`
each pass their own `summary_path`.
"""

import os
import sys
import time

import httpx

POLL_INTERVAL_SECONDS = 2.0
POLL_TIMEOUT_SECONDS = 120.0


def _auth_headers() -> dict[str, str]:
    """Header for calling this app's own API from outside the app process.

    These scripts run against an already-running server, so this reads the
    raw env var rather than app.core.config.Settings (which they
    deliberately don't import -- they only need httpx).
    """
    key = os.environ.get("APP_INTERNAL_API_KEY")
    if not key:
        print(
            "APP_INTERNAL_API_KEY is not set in this shell -- export the same value "
            "the running server was started with.",
            file=sys.stderr,
        )
        sys.exit(1)
    return {"X-Internal-Api-Key": key}


def _error_message(resp: httpx.Response) -> str:
    """Reads this app's `{success, message, data, error}` envelope
    (app/core/envelope.py) -- every error response (`AppError`, FastAPI's
    own `RequestValidationError`/`HTTPException`, and the unhandled-
    exception catch-all) is shaped through it (app/core/exceptions.py)."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text
    if isinstance(body, dict) and body.get("error"):
        return body["error"].get("details") or body.get("message", resp.text)
    if isinstance(body, dict) and "message" in body:
        return str(body["message"])
    return resp.text


def _poll_until_ready(
    base_url: str, summary_path: str, purchase_order_id: str, as_of_date: str
) -> dict | None:
    """Poll `GET {base_url}/{summary_path}?purchase_order_id=&as_of_date=`
    until its `status` leaves `PENDING` (or the timeout elapses). Shared by
    `demo_penalty_projection_summary.py` (`summary_path="penalties/
    projections/summary"`) and `demo_penalty_mitigation_summary.py`
    (`summary_path="penalties/mitigations/summary"`).

    `status: None` (no summary job on record yet for this exact
    `(purchase_order_id, as_of_date)`) is a normal `200`, not a `404` --
    treated the same as `PENDING` here too: keep polling, since the
    triggering `POST` just enqueued the job this same call is meant to
    observe. Still tolerates a `404` defensively (an unknown
    `purchase_order_id`, or another genuine caller error) by treating it
    the same as PENDING for one cycle rather than raising outright --
    `raise_for_status()` below still surfaces it if it persists past the
    timeout.
    """
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    headers = _auth_headers()
    while time.monotonic() < deadline:
        resp = httpx.get(
            f"{base_url}/{summary_path}",
            params={"purchase_order_id": purchase_order_id, "as_of_date": as_of_date},
            headers=headers,
            timeout=30,
        )
        if resp.status_code != 404:
            resp.raise_for_status()
            body = resp.json()["data"]
            if body["status"] not in (None, "PENDING"):
                return body
        time.sleep(POLL_INTERVAL_SECONDS)
    return None
