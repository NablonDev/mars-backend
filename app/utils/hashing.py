"""Shared content-hashing helper, so no call site re-derives its own sha256 digest."""

from __future__ import annotations

import hashlib


def content_sha256(text: str) -> str:
    """Hex-encoded sha256 digest of `text`, encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
