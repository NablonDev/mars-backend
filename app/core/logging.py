"""Structured JSON logging with request IDs and sensitive-data redaction."""

from __future__ import annotations

import json
import logging
import logging.config
import re
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from http import HTTPStatus

REQUEST_ID_HEADER = "X-Request-ID"
UNKNOWN_REQUEST_ID = "-"

_request_id: ContextVar[str] = ContextVar(
    "request_id",
    default=UNKNOWN_REQUEST_ID,
)

_EXTRA_FIELDS = ("method", "path", "status", "duration_ms", "error_code")

_URL_CREDENTIALS = re.compile(r"([a-zA-Z][\w+.-]*://[^:/?#\s@]+:)[^@\s/]*(@)")

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(api[-_]?key|password|passwd|pwd|secret|token|authorization)"
    r"(?![A-Za-z0-9])"
    r"(\s*[=:]\s*)"
    r"""[^\s,;="'&)]+"""
)

_BEARER_TOKEN = re.compile(r"(?i)\b(bearer\s+)[\w\-._~+/]+=*")

_REDACTED = "***"


def get_request_id() -> str:
    """Return the request ID bound to the current context, or the unknown-request sentinel."""
    return _request_id.get()


def set_request_id(request_id: str) -> Token[str]:
    """Bind request_id to the current context and return a token for the matching reset."""
    return _request_id.set(request_id)


def reset_request_id(token: Token[str]) -> None:
    """Restore the context var to its value before the paired set_request_id call."""
    _request_id.reset(token)


def _redact(text: str) -> str:
    """Return text with bearer tokens, URL credentials, and secret-like assignments masked."""
    text = _BEARER_TOKEN.sub(rf"\1{_REDACTED}", text)
    text = _URL_CREDENTIALS.sub(rf"\1{_REDACTED}\2", text)
    return _SECRET_ASSIGNMENT.sub(rf"\1\2{_REDACTED}", text)


class RequestIdFilter(logging.Filter):
    """Add the current request ID to records that do not already have one."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Stamp the record with the current request ID if it doesn't already have one."""
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        return True


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Render a log record as a single-line, redacted JSON object."""
        payload: dict[str, object] = {
            "request_id": getattr(record, "request_id", UNKNOWN_REQUEST_ID),
            "timestamp": datetime.fromtimestamp(
                record.created,
                tz=UTC,
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }

        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        if record.exc_info:
            payload["exception"] = _redact(self.formatException(record.exc_info))
        elif record.exc_text:
            payload["exception"] = _redact(record.exc_text)

        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Format log records as clean, human-readable text with request IDs and secret redaction."""

    def format(self, record: logging.LogRecord) -> str:
        req_id = getattr(record, "request_id", UNKNOWN_REQUEST_ID)
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        base = f"{ts} [{record.levelname:<7}] [{record.name}] [req:{req_id}] {_redact(record.getMessage())}"
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                base += f" {field}={value}"
        if record.exc_info:
            base += f"\n{_redact(self.formatException(record.exc_info))}"
        elif record.exc_text:
            base += f"\n{_redact(record.exc_text)}"
        return base


# ANSI escape sequences for local development pretty formatting
_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_DIM = "\033[2m"
_ANSI_ITALIC = "\033[3m"

_ANSI_GRAY = "\033[90m"
_ANSI_RED = "\033[31m"
_ANSI_GREEN = "\033[32m"
_ANSI_YELLOW = "\033[33m"
_ANSI_BLUE = "\033[34m"
_ANSI_MAGENTA = "\033[35m"
_ANSI_CYAN = "\033[36m"
_ANSI_WHITE = "\033[37m"

_ANSI_BOLD_RED = "\033[1;31m"
_ANSI_BOLD_GREEN = "\033[1;32m"
_ANSI_BOLD_YELLOW = "\033[1;33m"
_ANSI_BOLD_BLUE = "\033[1;34m"
_ANSI_BOLD_MAGENTA = "\033[1;35m"
_ANSI_BOLD_CYAN = "\033[1;36m"
_ANSI_BOLD_WHITE = "\033[1;37m"
_ANSI_ITALIC_CYAN = "\033[3;36m"
_ANSI_DIM_GREEN = "\033[2;32m"
_ANSI_BRIGHT_GREEN = "\033[92m"

_METHOD_COLORS: dict[str, str] = {
    "GET": _ANSI_BOLD_GREEN,
    "POST": _ANSI_BOLD_BLUE,
    "PUT": _ANSI_BOLD_YELLOW,
    "PATCH": _ANSI_BOLD_YELLOW,
    "DELETE": _ANSI_BOLD_RED,
    "HEAD": _ANSI_CYAN,
    "OPTIONS": _ANSI_CYAN,
}


class PrettyFormatter(logging.Formatter):
    """Format log records for local development with ANSI colors, timestamps, and clean access logs.

    Omits request IDs and raw HTTP key-value attributes (method=..., path=..., status=...) in favor of
    a clear, colored, FastAPI/Uvicorn-style HTTP request line. Preserves secret redaction.
    """

    def __init__(self, *, no_color: bool = False, no_bold: bool = False) -> None:
        super().__init__()
        self.no_color = no_color
        self.no_bold = no_bold

    def _c(self, code: str, text: str) -> str:
        """Wrap text in an ANSI escape sequence respecting no_color and no_bold."""
        if self.no_color or not code:
            return text
        if self.no_bold:
            code = code.replace("1;", "").replace("\033[1m", "")
            if not code or code == "\033[m":
                return text
        return f"{code}{text}{_ANSI_RESET}"

    def _format_level(self, levelname: str) -> str:
        """Format the log level in a 10-char fixed-width field with color."""
        label = f"{levelname}:"
        padded = f"{label:<10}"
        if self.no_color:
            return padded
        color_map = {
            "DEBUG": _ANSI_BOLD_CYAN,
            "INFO": _ANSI_BOLD_GREEN,
            "WARNING": _ANSI_BOLD_YELLOW,
            "ERROR": _ANSI_BOLD_RED,
            "CRITICAL": _ANSI_BOLD_MAGENTA,
        }
        code = color_map.get(levelname, _ANSI_BOLD_WHITE)
        return f"{self._c(code, label)}{padded[len(label) :]}"

    def _format_timestamp(self, created: float) -> str:
        """Format the record creation timestamp in UTC."""
        ts = datetime.fromtimestamp(created, tz=UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return self._c(_ANSI_GRAY, ts)

    def _format_location(self, record: logging.LogRecord) -> str:
        """Format the file and line number where the log record originated."""
        fn = getattr(record, "filename", "unknown") or "unknown"
        lineno = getattr(record, "lineno", 0) or 0
        loc = f"[{fn}:{lineno}]"
        return self._c(_ANSI_CYAN, loc)

    def _format_status(self, status: object) -> str:
        """Format HTTP status code with its standard reason phrase and color."""
        status_str = str(status)
        phrase = ""
        status_int: int | None = None
        try:
            status_int = int(status_str)
            phrase = f" {HTTPStatus(status_int).phrase}"
        except (ValueError, TypeError):
            status_int = None

        text = f"{status_str}{phrase}"
        if self.no_color or status_int is None:
            return text

        if status_int < 300:
            color = _ANSI_BOLD_GREEN
        elif status_int < 400:
            color = _ANSI_BOLD_CYAN
        elif status_int < 500:
            color = _ANSI_BOLD_YELLOW
        else:
            color = _ANSI_BOLD_RED
        return self._c(color, text)

    def _format_method(self, method: str) -> str:
        """Format HTTP method name with distinct color."""
        m_upper = method.upper()
        color = _METHOD_COLORS.get(m_upper, _ANSI_BOLD_WHITE)
        return self._c(color, m_upper)

    def _format_duration(self, duration_ms: object) -> str:
        """Format request duration in milliseconds."""
        if duration_ms is None:
            return ""
        if isinstance(duration_ms, (int, float)):
            val = float(duration_ms)
            dur_str = f"{val:.2f}ms" if isinstance(duration_ms, float) else f"{val:.0f}ms"
            if self.no_color:
                return f"- {dur_str}"
            if val < 300:
                color = _ANSI_DIM_GREEN
            elif val < 1000:
                color = _ANSI_YELLOW
            else:
                color = _ANSI_RED
            dash = self._c(_ANSI_GRAY, "-")
            dur = self._c(f"{color}{_ANSI_ITALIC}", dur_str)
            return f"{dash} {dur}"
        dur_str = f"{duration_ms}ms"
        if self.no_color:
            return f"- {dur_str}"
        dash = self._c(_ANSI_GRAY, "-")
        dur = self._c(f"{_ANSI_GRAY}{_ANSI_ITALIC}", dur_str)
        return f"{dash} {dur}"

    def format(self, record: logging.LogRecord) -> str:
        """Render a log record with colors, source file, timestamp, and formatted HTTP access info."""
        ts_str = self._format_timestamp(getattr(record, "created", 0.0))
        level_str = self._format_level(record.levelname)
        loc_str = self._format_location(record)

        method = getattr(record, "method", None)
        path = getattr(record, "path", None)
        status = getattr(record, "status", None)
        duration_ms = getattr(record, "duration_ms", None)

        if (
            not (method and path)
            and record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) >= 5
        ):
            method = record.args[1]
            path = record.args[2]
            status = record.args[4]

        if method and path:
            method_str = self._format_method(str(method))
            path_str = _redact(str(path))
            request_part = f'"{method_str} {path_str}"'

            parts = [ts_str, level_str, loc_str, request_part]
            if status is not None:
                parts.append(self._format_status(status))
            dur_str = self._format_duration(duration_ms)
            if dur_str:
                parts.append(dur_str)

            error_code = getattr(record, "error_code", None)
            if error_code:
                parts.append(self._c(_ANSI_BOLD_RED, f"[{error_code}]"))

            line = " ".join(parts)
        else:
            msg = _redact(record.getMessage())
            parts = [ts_str, level_str, loc_str, msg]
            error_code = getattr(record, "error_code", None)
            if error_code:
                parts.append(self._c(_ANSI_BOLD_RED, f"[{error_code}]"))
            line = " ".join(parts)

        if record.exc_info:
            exc_text = _redact(self.formatException(record.exc_info))
            line += f"\n{self._c(_ANSI_RED, exc_text)}"
        elif record.exc_text:
            exc_text = _redact(record.exc_text)
            line += f"\n{self._c(_ANSI_RED, exc_text)}"

        return line


_configured = False


def configure_logging(
    level: str = "INFO",
    log_format: str = "json",
    *,
    no_color: bool = False,
    no_bold: bool = False,
    force: bool = False,
) -> None:
    """Configure standard JSON, clean text, or pretty colored logging."""
    global _configured

    if _configured and not force:
        return

    fmt = log_format.strip().lower()
    formatter_cls: type[logging.Formatter]
    formatter_kwargs: dict[str, object] = {}
    if fmt == "pretty":
        formatter_cls = PrettyFormatter
        formatter_kwargs = {"no_color": no_color, "no_bold": no_bold}
    elif fmt == "text":
        formatter_cls = TextFormatter
    else:
        formatter_cls = JsonFormatter

    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "request_id": {"()": RequestIdFilter},
        },
        "formatters": {
            "active": {
                "()": formatter_cls,
                **formatter_kwargs,
            },
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "active",
                "filters": ["request_id"],
            }
        },
        "root": {
            "handlers": ["stdout"],
            "level": level,
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["stdout"],
                "level": level,
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["stdout"],
                "level": level,
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["stdout"],
                "level": "WARNING",
                "propagate": False,
            },
        },
    }
    logging.config.dictConfig(logging_config)
    _configured = True
