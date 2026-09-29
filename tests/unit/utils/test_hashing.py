"""Tests for app.utils.hashing."""

import hashlib

from app.utils.hashing import content_sha256


def test_content_sha256_matches_a_direct_hashlib_call():
    text = "## Penalties\nRetailer may assess a $50 fee per short-shipped case."
    assert content_sha256(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_content_sha256_differs_for_different_text():
    assert content_sha256("a") != content_sha256("b")
