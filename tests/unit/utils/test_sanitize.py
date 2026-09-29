"""Tests for app.utils.sanitize."""

from app.utils.sanitize import strip_nul_bytes


def test_strip_nul_bytes_removes_nul_from_a_plain_string():
    assert strip_nul_bytes("hello\x00world") == "helloworld"


def test_strip_nul_bytes_recurses_through_nested_dicts_lists_and_tuples():
    tainted = {
        "input_snapshot": {
            "messages": [{"role": "assistant", "content": "answer\x00: 42"}],
            "tags": ("ok\x00", "clean"),
        },
        "clean_key": "clean_value",
        "count": 3,
    }

    cleaned = strip_nul_bytes(tainted)

    assert cleaned == {
        "input_snapshot": {
            "messages": [{"role": "assistant", "content": "answer: 42"}],
            "tags": ("ok", "clean"),
        },
        "clean_key": "clean_value",
        "count": 3,
    }


def test_strip_nul_bytes_sanitizes_dict_keys_too():
    cleaned = strip_nul_bytes({"bad\x00key": "value"})

    assert cleaned == {"badkey": "value"}


def test_strip_nul_bytes_recurses_through_a_set():
    assert strip_nul_bytes({"clean", "dirty\x00"}) == {"clean", "dirty"}
