"""Shared normalization for CMIR customer/material identity fields."""

from __future__ import annotations

import re


def normalize_identity_key(value: str) -> str:
    """Canonical match key for an identity field: alphanumerics only, upper-cased."""
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
