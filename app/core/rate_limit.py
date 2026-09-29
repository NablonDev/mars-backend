"""Process-local rate-limit backoff gate shared across LLM callers."""

from __future__ import annotations

import re
import threading
import time

_RATE_LIMIT_SIGNAL_RE = re.compile(
    r"\b(429|rate[ -]?limit|too many requests|quota)\b",
    re.IGNORECASE,
)
_GATE_POLL_INTERVAL_SECONDS = 0.5


def looks_like_rate_limit(exc: BaseException) -> bool:
    """Return whether an exception chain contains a recognizable rate-limit signal."""
    seen: set[int] = set()
    current: BaseException | None = exc

    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _RATE_LIMIT_SIGNAL_RE.search(str(current)):
            return True

        # Follow the explicit cause first, then the implicit context, while
        # guarding against malformed or cyclic exception chains.
        current = current.__cause__ or current.__context__

    return False


class RateLimitGate:
    """Blocks new LLM calls after a rate-limit error until the cooldown expires."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paused_until = 0.0
        self.rate_limit_hits = 0

    def note_rate_limit_hit(self, backoff_seconds: float) -> None:
        """Record a rate-limit hit and extend the pause window by backoff_seconds."""
        with self._lock:
            self.rate_limit_hits += 1
            self._paused_until = max(
                self._paused_until,
                time.monotonic() + backoff_seconds,
            )

    def wait_if_paused(self, shutdown_event: threading.Event) -> None:
        """Block until the gate clears or shutdown is requested."""
        while not shutdown_event.is_set():
            with self._lock:
                remaining = self._paused_until - time.monotonic()

            if remaining <= 0:
                return

            shutdown_event.wait(timeout=min(_GATE_POLL_INTERVAL_SECONDS, remaining))
