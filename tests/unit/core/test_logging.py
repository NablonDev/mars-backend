"""
The JSON formatter, the request-id filter, and the redaction pass -- unit
tests against `logging.LogRecord` directly, so nothing here depends on
`dictConfig` having run or on pytest's own capture handlers.
"""

import json
import logging

from app.core.logging import (
    UNKNOWN_REQUEST_ID,
    JsonFormatter,
    PrettyFormatter,
    RequestIdFilter,
    TextFormatter,
    configure_logging,
    get_request_id,
    reset_request_id,
    set_request_id,
)


def _record(message: str, *, args=(), exc_info=None, **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=exc_info,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _formatted(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


def test_every_line_is_one_json_object_with_the_request_id_first():
    line = JsonFormatter().format(_record("hello", request_id="req-1"))

    assert "\n" not in line
    payload = json.loads(line)
    assert next(iter(payload)) == "request_id"
    assert payload["request_id"] == "req-1"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["message"] == "hello"
    assert payload["timestamp"].endswith("+00:00")


def test_allowlisted_extras_are_emitted():
    payload = _formatted(
        _record("GET /x 200", method="GET", path="/x", status=200, duration_ms=1.5, error_code="NOPE")
    )

    assert payload["method"] == "GET"
    assert payload["path"] == "/x"
    assert payload["status"] == 200
    assert payload["duration_ms"] == 1.5
    assert payload["error_code"] == "NOPE"


def test_non_allowlisted_extras_are_dropped():
    """The formatter never dumps `record.__dict__`, so an `extra=` that
    happens to carry a credential can't reach the log stream by accident."""
    payload = _formatted(_record("done", api_key="s3cret", request_body={"password": "hunter2"}))

    assert "api_key" not in payload
    assert "request_body" not in payload
    assert "s3cret" not in json.dumps(payload)


def test_url_credentials_are_redacted_from_the_message():
    payload = _formatted(_record("could not connect to postgresql://svc:sup3rs3cret@db.internal:5432/mars"))

    assert "sup3rs3cret" not in payload["message"]
    assert "postgresql://svc:***@db.internal:5432/mars" in payload["message"]


def test_secret_assignments_are_redacted_from_the_message():
    payload = _formatted(_record("call failed: api-key=abc123def, password: hunter2, token=zzz"))

    assert "abc123def" not in payload["message"]
    assert "hunter2" not in payload["message"]
    assert "zzz" not in payload["message"]
    assert payload["message"].count("***") == 3


def test_secret_assignments_with_underscore_prefixed_names_are_redacted():
    """Regression test: `\\b...\\b` alone never matches inside
    `DB_PASSWORD=...` because `_` is a word character, so there's no
    boundary between "_" and "PASSWORD" -- this app's own env-var naming
    convention (`AZURE_OPENAI_API_KEY`, `DB_PASSWORD`) was exactly the
    case that leaked a real secret unredacted."""
    payload = _formatted(_record("startup failed: DB_PASSWORD=SuperSecret123"))

    assert "SuperSecret123" not in payload["message"]
    assert "***" in payload["message"]

    payload = _formatted(_record("azure call failed: AZURE_OPENAI_API_KEY=abc123def"))

    assert "abc123def" not in payload["message"]
    assert "***" in payload["message"]


def test_bearer_tokens_are_redacted_from_the_message():
    payload = _formatted(_record("upstream rejected Authorization: Bearer eyJhbGciOi.J9.abc-_123"))

    assert "eyJhbGciOi" not in payload["message"]


def test_redaction_applies_to_interpolated_args():
    payload = _formatted(_record("dsn=%s", args=("postgresql://svc:sup3rs3cret@db/mars",)))

    assert "sup3rs3cret" not in payload["message"]


def test_tracebacks_are_included_but_redacted():
    try:
        raise RuntimeError("bad dsn postgresql://svc:sup3rs3cret@db.internal/mars")
    except RuntimeError:
        import sys

        payload = _formatted(_record("boom", exc_info=sys.exc_info()))

    assert "Traceback" in payload["exception"]
    assert "RuntimeError" in payload["exception"]
    assert "sup3rs3cret" not in payload["exception"]


def test_filter_stamps_the_current_request_id():
    token = set_request_id("req-from-contextvar")
    try:
        record = _record("hello")
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "req-from-contextvar"
    finally:
        reset_request_id(token)

    assert get_request_id() == UNKNOWN_REQUEST_ID


def test_filter_does_not_overwrite_an_explicit_request_id():
    """The exception handlers pass the id explicitly, because a 500 is
    handled outside the middleware that owns the ContextVar."""
    token = set_request_id("from-contextvar")
    try:
        record = _record("hello", request_id="explicit")
        RequestIdFilter().filter(record)
    finally:
        reset_request_id(token)

    assert record.request_id == "explicit"


def test_records_without_a_request_id_fall_back_to_a_placeholder():
    payload = _formatted(_record("startup, outside any request"))

    assert payload["request_id"] == UNKNOWN_REQUEST_ID


def test_pretty_formatter_removes_req_id_and_raw_extra_labels():
    record = _record(
        "GET /api/v1/rules 200 3.38ms",
        request_id="req-secret-123",
        method="GET",
        path="/api/v1/rules",
        status=200,
        duration_ms=3.38,
    )
    formatted = PrettyFormatter(no_color=True).format(record)

    assert "req-secret-123" not in formatted
    assert "[req:" not in formatted
    assert "method=" not in formatted
    assert "path=" not in formatted
    assert "status=" not in formatted
    assert "duration_ms=" not in formatted


def test_pretty_formatter_access_log_structure_and_coloring():
    record = _record(
        "GET /api/v1/rules 200 3.38ms",
        method="GET",
        path="/api/v1/rules",
        status=200,
        duration_ms=3.38,
    )
    colored = PrettyFormatter(no_color=False).format(record)
    plain = PrettyFormatter(no_color=True).format(record)

    # Plain output verification: timestamp, level, location, method/path, status, duration
    assert 'INFO:      [test_logging.py:1] "GET /api/v1/rules" 200 OK - 3.38ms' in plain

    # Colored output verification: ANSI escape sequences
    assert "\033[1;32mINFO:\033[0m" in colored  # bold green level
    assert "\033[36m[test_logging.py:1]\033[0m" in colored  # cyan file
    assert "\033[1;32mGET\033[0m" in colored  # bold green GET
    assert "\033[1;32m200 OK\033[0m" in colored  # bold green 200 OK


def test_pretty_formatter_no_bold_removes_bold_codes():
    record = _record(
        "GET /api/v1/rules 200 3.38ms",
        method="GET",
        path="/api/v1/rules",
        status=200,
        duration_ms=3.38,
    )
    no_bold = PrettyFormatter(no_color=False, no_bold=True).format(record)

    assert "\033[1;" not in no_bold  # no bold ANSI codes like \033[1;32m
    assert "\033[1m" not in no_bold
    assert "\033[32mINFO:\033[0m" in no_bold  # regular unbolded green level
    assert "\033[32mGET\033[0m" in no_bold  # regular unbolded green GET
    assert "\033[32m200 OK\033[0m" in no_bold  # regular unbolded green status


def test_pretty_formatter_status_codes_and_phrases():
    for status_code, expected_phrase, expected_color in [
        (200, "200 OK", "\033[1;32m"),
        (201, "201 Created", "\033[1;32m"),
        (304, "304 Not Modified", "\033[1;36m"),
        (404, "404 Not Found", "\033[1;33m"),
        (422, "422 Unprocessable Entity", "\033[1;33m"),
        (500, "500 Internal Server Error", "\033[1;31m"),
    ]:
        record = _record("HTTP call", method="POST", path="/endpoint", status=status_code)
        plain = PrettyFormatter(no_color=True).format(record)
        colored = PrettyFormatter(no_color=False).format(record)

        assert expected_phrase in plain
        assert f"{expected_color}{expected_phrase}\033[0m" in colored


def test_pretty_formatter_non_access_log_with_redaction():
    record = _record("connected to postgresql://svc:supersecret@db:5432/mars")
    plain = PrettyFormatter(no_color=True).format(record)

    assert "supersecret" not in plain
    assert "postgresql://svc:***@db:5432/mars" in plain
    assert "[test_logging.py:1]" in plain
    assert "INFO:" in plain


def test_pretty_formatter_error_code_and_exception_handling():
    try:
        raise ValueError("failed with api_key=secretkey123")
    except ValueError:
        import sys

        record = _record("operation failed", exc_info=sys.exc_info(), error_code="DB_ERROR")

    plain = PrettyFormatter(no_color=True).format(record)
    colored = PrettyFormatter(no_color=False).format(record)

    assert "[DB_ERROR]" in plain
    assert "secretkey123" not in plain
    assert "api_key=***" in plain
    assert "ValueError: failed with api_key=***" in plain
    assert "\033[1;31m[DB_ERROR]\033[0m" in colored


def test_configure_logging_activates_pretty_formatter():
    configure_logging(log_format="pretty", no_color=True, no_bold=True, force=True)
    root_handler = logging.getLogger().handlers[0]
    assert isinstance(root_handler.formatter, PrettyFormatter)
    assert root_handler.formatter.no_color is True
    assert root_handler.formatter.no_bold is True

    configure_logging(log_format="text", force=True)
    root_handler = logging.getLogger().handlers[0]
    assert isinstance(root_handler.formatter, TextFormatter)

    configure_logging(log_format="json", force=True)
    root_handler = logging.getLogger().handlers[0]
    assert isinstance(root_handler.formatter, JsonFormatter)


def test_app_settings_parses_no_color_and_no_bold_flags():
    from app.core.config.app import AppSettings

    settings = AppSettings(
        APP_INTERNAL_API_KEY="x" * 64,
        NO_COLOR="1",
        NO_BOLD="true",
    )
    assert settings.no_color is True
    assert settings.no_bold is True

    settings_empty = AppSettings(
        APP_INTERNAL_API_KEY="x" * 64,
        NO_COLOR="",
        NO_BOLD="false",
    )
    assert settings_empty.no_color is False
    assert settings_empty.no_bold is False


def test_pretty_formatter_response_badge_empty():
    record = _record(
        "GET /api/v1/rules 200 3.38ms",
        method="GET",
        path="/api/v1/rules",
        status=200,
        duration_ms=3.38,
        response_badge="empty",
    )
    plain = PrettyFormatter(no_color=True).format(record)
    colored = PrettyFormatter(no_color=False).format(record)

    assert 'INFO:      [test_logging.py:1] "GET /api/v1/rules" 200 OK - 3.38ms [empty]' in plain
    assert "\033[33m[empty]\033[0m" in colored


def test_pretty_formatter_response_badge_n_items():
    record = _record(
        "GET /api/v1/rules 200 12.50ms",
        method="GET",
        path="/api/v1/rules",
        status=200,
        duration_ms=12.5,
        response_badge="12 items",
    )
    plain = PrettyFormatter(no_color=True).format(record)
    colored = PrettyFormatter(no_color=False).format(record)

    assert 'INFO:      [test_logging.py:1] "GET /api/v1/rules" 200 OK - 12.50ms [12 items]' in plain
    assert "\033[36m[12 items]\033[0m" in colored


def test_json_and_text_formatter_response_badge():
    record = _record(
        "GET /api/v1/rules 200 3.38ms",
        method="GET",
        path="/api/v1/rules",
        status=200,
        duration_ms=3.38,
        response_badge="empty",
    )
    payload = _formatted(record)
    assert payload["response_badge"] == "empty"

    text = TextFormatter().format(record)
    assert "response_badge=empty" in text


def test_determine_response_badge():
    from app.core.middleware.logging import determine_response_badge

    # Empty list envelope
    body_empty_list = json.dumps({"success": True, "message": "OK", "data": []}).encode()
    badge, err = determine_response_badge(200, True, body_empty_list)
    assert badge == "empty"
    assert err is None

    # Null data envelope
    body_null_data = json.dumps({"success": True, "message": "OK", "data": None}).encode()
    badge, err = determine_response_badge(200, True, body_null_data)
    assert badge == "empty"
    assert err is None

    # Single item list envelope
    body_1_item = json.dumps({"success": True, "message": "OK", "data": [{"id": 1}]}).encode()
    badge, err = determine_response_badge(200, True, body_1_item)
    assert badge == "1 item"
    assert err is None

    # Multiple items list envelope
    body_multi = json.dumps({"success": True, "message": "OK", "data": [1, 2, 3]}).encode()
    badge, err = determine_response_badge(200, True, body_multi)
    assert badge == "3 items"
    assert err is None

    # Single object envelope
    body_obj = json.dumps(
        {"success": True, "message": "OK", "data": {"id": 100, "status": "PENDING"}}
    ).encode()
    badge, err = determine_response_badge(200, True, body_obj)
    assert badge == "1 item"
    assert err is None

    # Paginated response dict with items key
    body_paginated_empty = json.dumps(
        {"success": True, "message": "OK", "data": {"items": [], "total": 0}}
    ).encode()
    badge, err = determine_response_badge(200, True, body_paginated_empty)
    assert badge == "empty"
    assert err is None

    body_paginated_items = json.dumps(
        {"success": True, "message": "OK", "data": {"items": ["a", "b"]}}
    ).encode()
    badge, err = determine_response_badge(200, True, body_paginated_items)
    assert badge == "2 items"
    assert err is None

    # 204 No Content
    badge, err = determine_response_badge(204, False, b"")
    assert badge == "empty"
    assert err is None

    # 304 Not Modified
    badge, err = determine_response_badge(304, False, b"")
    assert badge is None
    assert err is None

    # 404 error envelope
    body_404 = json.dumps(
        {
            "success": False,
            "message": "Not found",
            "data": None,
            "error": {"code": "RULE_NOT_FOUND"},
        }
    ).encode()
    badge, err = determine_response_badge(404, True, body_404)
    assert badge is None
    assert err == "RULE_NOT_FOUND"


def test_access_log_middleware_end_to_end():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from app.core.middleware.logging import AccessLogMiddleware

    records: list[logging.LogRecord] = []

    class CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    access_logger = logging.getLogger("app.access")
    handler = CapturingHandler()
    access_logger.addHandler(handler)
    access_logger.setLevel(logging.INFO)

    try:
        routes = [
            Route("/empty-list", lambda req: JSONResponse({"success": True, "message": "OK", "data": []})),
            Route(
                "/has-items",
                lambda req: JSONResponse({"success": True, "message": "OK", "data": [{"id": 1}, {"id": 2}]}),
            ),
            Route("/no-content", lambda req: Response(status_code=204)),
            Route(
                "/not-found",
                lambda req: JSONResponse(
                    {"success": False, "message": "Nope", "error": {"code": "NOT_FOUND"}}, status_code=404
                ),
            ),
        ]
        app = Starlette(routes=routes)
        app.add_middleware(AccessLogMiddleware)

        client = TestClient(app)

        client.get("/empty-list")
        assert getattr(records[-1], "response_badge", None) == "empty"
        assert getattr(records[-1], "status", None) == 200

        client.get("/has-items")
        assert getattr(records[-1], "response_badge", None) == "2 items"
        assert getattr(records[-1], "status", None) == 200

        client.get("/no-content")
        assert getattr(records[-1], "response_badge", None) == "empty"
        assert getattr(records[-1], "status", None) == 204

        client.get("/not-found")
        assert getattr(records[-1], "response_badge", None) is None
        assert getattr(records[-1], "error_code", None) == "NOT_FOUND"
        assert getattr(records[-1], "status", None) == 404
    finally:
        access_logger.removeHandler(handler)
