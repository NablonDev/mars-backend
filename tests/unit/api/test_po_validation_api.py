"""API tests for `app/api/v1/po_validation.py` (`POST /po-validation/
purchase-order-lines`, `GET /purchase-order-lines`,
`GET /purchase-orders/{purchase_order_id}/lines`) -- Phase 7b route redesign.

Was `tests/unit/api/test_po_validation_api.py`'s `unittest.TestCase` against
the pre-restructure `/ingest/po-lines`/`/po-lines`/`/threads/{id}/*` routes
and the since-removed `app.core.exceptions.ServiceError`; rewritten against
the new routes, the collapsed `AppError` hierarchy, and the
`{success, message, data, error}` envelope. Thread-lifecycle routes
(`qty-mismatch-decision`/`manual-cmir-entry`, now generic `decisions`) and
`GET /po-lines/{id}/errors` (now `GET /processing-errors`) moved to
`app/api/v1/workflow_threads.py`/`app/api/v1/processing_errors.py` -- see
`tests/unit/api/test_workflow_threads_api.py`/`test_processing_errors_api.py`.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ValidationError


class FakePoValidationService:
    """Implements the `PoValidationService` methods `app/api/v1/po_validation.py` calls."""

    def __init__(self) -> None:
        self.ingested: list[list[dict]] = []

    def ingest_po_lines(self, lines):
        self.ingested.append(lines)
        return {
            "batch_id": "batch_po_01",
            "total_lines": len(lines),
            "lines": [
                {
                    "po_line_id": "22222222-2222-2222-2222-222222222222",
                    "batch_id": "batch_po_01",
                    "po_number": line["po_number"],
                    "po_line_number": line["po_line_number"],
                    "status": "READY_FOR_SO_CREATION",
                    "thread_id": None,
                    "updated_at": None,
                }
                for line in lines
            ],
        }

    def list_ready_lines(self, *, status=None, limit=50, cursor=None):
        raise ValidationError(
            code="VIEW_NOT_SUPPORTED",
            message="Cross-purchase-order line listing has no repository support.",
            details={"status": status},
        )


@pytest.fixture
def po_validation_service():
    return FakePoValidationService()


def test_create_purchase_order_lines_returns_accepted_batch(client):
    response = client.post(
        "/api/v1/po-validation/purchase-order-lines",
        json={
            "lines": [
                {
                    "po_number": "PO-1",
                    "po_line_number": "10",
                    "retailer_code": "CUST-1",
                    "retailer_material_code": "ACME-MAT-1",
                    "plant": "1000",
                    "order_quantity": 100,
                }
            ]
        },
    )

    assert response.status_code == 202, response.text
    body = response.json()["data"]
    assert body["batch_id"] == "batch_po_01"
    assert body["lines"][0]["status"] == "READY_FOR_SO_CREATION"
    assert body["lines"][0]["thread_id"] is None


def test_list_purchase_order_lines_flat_view_not_supported(client):
    response = client.get("/api/v1/purchase-order-lines", params={"status": "READY_FOR_SO_CREATION"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VIEW_NOT_SUPPORTED"


@pytest.fixture
def retailer(client):
    return client.post(
        "/api/v1/retailers", json={"retailer_code": "RET-POV", "retailer_name": "PO Validation Co"}
    ).json()["data"]


@pytest.fixture
def purchase_order_with_line(client, retailer):
    resp = client.post(
        "/api/v1/purchase-orders",
        json={
            "purchase_order_number": "PO-POV-001",
            "retailer_id": retailer["id"],
            "order_date": "2026-08-01",
            "requested_delivery_date": "2026-08-10",
            "required_ship_date": "2026-08-08",
            "lines": [{"line_number": "10", "ordered_quantity": 100, "unit_price": 5.0}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def test_list_purchase_order_lines_for_order_returns_the_line(client, purchase_order_with_line):
    purchase_order_id = purchase_order_with_line["id"]

    response = client.get(f"/api/v1/purchase-orders/{purchase_order_id}/lines")

    assert response.status_code == 200, response.text
    lines = response.json()["data"]
    assert len(lines) == 1
    assert lines[0]["line_number"] == "10"


def test_list_purchase_order_lines_for_unknown_order_returns_404(client):
    response = client.get("/api/v1/purchase-orders/00000000-0000-0000-0000-000000000000/lines")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PO_NOT_FOUND"


def test_po_audit_trail_returns_the_most_recent_line_first(client, purchase_order_with_line):
    response = client.get("/api/v1/po-audit-trail", params={"limit": 5})

    assert response.status_code == 200, response.text
    items = response.json()["data"]
    assert len(items) >= 1
    assert items[0]["po_number"] == "PO-POV-001"
    assert items[0]["quantity"] == 100
    assert items[0]["status"] == "OPEN"
    assert items[0]["delivery_date"] == "2026-08-10"
