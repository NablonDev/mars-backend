"""API tests for `app/api/v1/processing_errors.py`
(`GET /processing-errors?purchase_order_line_id={id}`) -- was
`GET /po-lines/{id}/errors` on the pre-restructure `po_validation` router,
Phase 7b route redesign."""

from __future__ import annotations

from uuid import UUID

import pytest

_KNOWN_LINE_ID = UUID("33333333-3333-3333-3333-333333333333")
_UNKNOWN_LINE_ID = UUID("44444444-4444-4444-4444-444444444444")


class FakePoValidationService:
    def get_errors(self, purchase_order_line_id):
        if purchase_order_line_id == _UNKNOWN_LINE_ID:
            return {"items": []}
        return {
            "items": [
                {
                    "id": "55555555-5555-5555-5555-555555555555",
                    "job_item_id": None,
                    "agent_run_id": "00000000-0000-0000-0000-000000001042",
                    "purchase_order_line_id": str(_KNOWN_LINE_ID),
                    "error_type": "MATERIAL_NOT_FOUND",
                    "error_code": "MATERIAL_NOT_FOUND",
                    "error_message": "No material_master row.",
                    "node_name": "check_material_master",
                    "occurred_at": "2026-08-09T10:00:00+00:00",
                    "resolved": False,
                    "resolved_at": None,
                    "resolved_by": None,
                }
            ]
        }


@pytest.fixture
def po_validation_service():
    return FakePoValidationService()


def test_list_processing_errors_returns_items_for_line(client):
    response = client.get("/api/v1/processing-errors", params={"purchase_order_line_id": str(_KNOWN_LINE_ID)})

    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["error_type"] == "MATERIAL_NOT_FOUND"


def test_list_processing_errors_requires_purchase_order_line_id(client):
    response = client.get("/api/v1/processing-errors")

    assert response.status_code == 422
