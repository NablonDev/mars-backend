"""API tests for `app/api/v1/po_validation.py` (`POST /po-validation/
purchase-order-lines`, `GET /purchase-order-lines`) -- Phase 7b route
redesign, then consolidated further (this pass) into the one
`GET /purchase-order-lines?purchase_order_id=&status=` route, folding in
what used to be the separate nested `GET /purchase-orders/{purchase_order_id}
/lines`.

Was `tests/unit/api/test_po_validation_api.py`'s `unittest.TestCase` against
the pre-restructure `/ingest/po-lines`/`/po-lines`/`/threads/{id}/*` routes
and the since-removed `app.core.exceptions.ServiceError`; rewritten against
the new routes, the collapsed `AppError` hierarchy, and the
`{success, message, data, error}` envelope. Thread-lifecycle routes
(`qty-mismatch-decision`/`manual-cmir-entry`, now generic `decisions`) and
`GET /po-lines/{id}/errors` (now `GET /processing-errors`) moved to
`app/api/v1/workflow_threads.py`/`app/api/v1/processing_errors.py` -- see
`tests/unit/api/test_workflow_threads_api.py`/`test_processing_errors_api.py`.

`GET /purchase-order-lines` was `VIEW_NOT_SUPPORTED` (no repository query
existed) until Gap 3's `PurchaseOrderRepository.list_lines_by_status` --
`FakePoValidationService.list_ready_lines` now returns a real-shaped
listing rather than raising, matching `PoValidationService`'s current
behavior; see `tests/unit/services/test_po_validation_service.py` and
`tests/unit/repositories/test_purchase_order_line_listing.py` for coverage
of the real repository/service logic this fake stands in for.
"""

from __future__ import annotations

from uuid import UUID

import pytest


class FakePoValidationService:
    """Implements the `PoValidationService` methods `app/api/v1/po_validation.py` calls."""

    def __init__(self) -> None:
        self.ingested: list[list[dict]] = []
        self.list_calls: list[dict] = []

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

    def list_ready_lines(self, *, purchase_order_id=None, status=None, limit=50, cursor=None):
        self.list_calls.append({"purchase_order_id": purchase_order_id, "status": status})
        return {
            "items": [
                {
                    "id": "33333333-3333-3333-3333-333333333333",
                    "purchase_order_id": str(purchase_order_id)
                    if purchase_order_id is not None
                    else "44444444-4444-4444-4444-444444444444",
                    "line_number": "10",
                    "ordered_quantity": 100.0,
                    "unit_price": 0.0,
                    "line_status": status or "READY_FOR_SO_CREATION",
                }
            ],
            "next_cursor": None,
        }


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


def test_list_purchase_order_lines_flat_view_returns_items(client):
    response = client.get("/api/v1/purchase-order-lines", params={"status": "READY_FOR_SO_CREATION"})

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["items"][0]["line_status"] == "READY_FOR_SO_CREATION"
    assert body["next_cursor"] is None


def test_list_purchase_order_lines_flat_view_defaults_with_no_status(client):
    response = client.get("/api/v1/purchase-order-lines")

    assert response.status_code == 200, response.text


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


def test_list_purchase_order_lines_scoped_to_one_purchase_order(
    client, purchase_order_with_line, po_validation_service
):
    """`GET /purchase-order-lines?purchase_order_id=` replaces the old
    nested `GET /purchase-orders/{purchase_order_id}/lines` route -- the
    router still 404s via `require_purchase_order` before ever calling
    `PoValidationService.list_ready_lines` (see
    `tests/unit/services/test_po_validation_service.py` for the real
    `purchase_order_id`-scoping behavior this fake stands in for)."""
    purchase_order_id = purchase_order_with_line["id"]

    response = client.get("/api/v1/purchase-order-lines", params={"purchase_order_id": purchase_order_id})

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["items"][0]["purchase_order_id"] == purchase_order_id
    assert po_validation_service.list_calls[-1] == {
        "purchase_order_id": UUID(purchase_order_id),
        "status": None,
    }


def test_list_purchase_order_lines_for_unknown_order_returns_404(client):
    response = client.get(
        "/api/v1/purchase-order-lines",
        params={"purchase_order_id": "00000000-0000-0000-0000-000000000000"},
    )

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
