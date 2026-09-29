"""API tests for `common.purchase_order`/`purchase_order_line` and their
fulfillment facts (confirmations, shipments, demand exceptions). Actual
penalties are also exercised here for historical grouping, even though
their routes now live flat under `/penalties/actual-penalties` (see
`app/api/v1/penalties/actual_penalties.py`) rather than nested under
`/purchase-orders/{purchase_order_id}/...` like the other facts above."""

from __future__ import annotations

import pytest


@pytest.fixture
def retailer(client):
    return client.post(
        "/api/v1/retailers", json={"retailer_code": "RET-PO", "retailer_name": "PO Co"}
    ).json()["data"]


def _create_purchase_order(client, retailer_id: str, *, with_line: bool = True) -> dict:
    lines = (
        [
            {
                "line_number": "10",
                "ordered_quantity": 100,
                "unit_price": 5.0,
            }
        ]
        if with_line
        else []
    )
    resp = client.post(
        "/api/v1/purchase-orders",
        json={
            "purchase_order_number": "PO-TEST-001",
            "retailer_id": retailer_id,
            "order_date": "2026-08-01",
            "requested_delivery_date": "2026-08-10",
            "required_ship_date": "2026-08-08",
            "lines": lines,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def test_create_purchase_order_with_line(client, retailer):
    po = _create_purchase_order(client, retailer["id"])

    assert po["purchase_order_number"] == "PO-TEST-001"
    assert po["order_status"] == "OPEN"
    assert len(po["lines"]) == 1
    assert po["lines"][0]["line_number"] == "10"
    assert po["lines"][0]["ordered_quantity"] == 100


def test_list_purchase_orders_filters_by_status(client, retailer):
    _create_purchase_order(client, retailer["id"])

    open_orders = client.get("/api/v1/purchase-orders", params={"order_status": "OPEN"}).json()["data"]
    assert any(po["purchase_order_number"] == "PO-TEST-001" for po in open_orders)

    delivered_orders = client.get("/api/v1/purchase-orders", params={"order_status": "DELIVERED"}).json()[
        "data"
    ]
    assert not any(po["purchase_order_number"] == "PO-TEST-001" for po in delivered_orders)


def test_add_confirmation_and_list_it(client, retailer):
    po = _create_purchase_order(client, retailer["id"])
    line_id = po["lines"][0]["id"]

    resp = client.post(
        f"/api/v1/purchase-orders/{po['id']}/confirmations",
        json={
            "confirmation_number": "CONF-001",
            "confirmation_date": "2026-08-02T00:00:00",
            "lines": [{"purchase_order_line_id": line_id, "confirmed_quantity": 80}],
        },
    )
    assert resp.status_code == 201, resp.text

    listed = client.get(f"/api/v1/purchase-orders/{po['id']}/confirmations").json()["data"]
    assert len(listed) == 1
    assert listed[0]["confirmed_quantity"] == 80


def test_record_shipment_and_list_it(client, retailer):
    po = _create_purchase_order(client, retailer["id"])

    resp = client.post(
        f"/api/v1/purchase-orders/{po['id']}/shipments",
        json={
            "shipment_number": "SHIP-001",
            "recorded_at": "2026-08-03T00:00:00",
            "expected_ship_date": "2026-08-04",
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()["data"]
    assert created["shipment_number"] == "SHIP-001"

    listed = client.get(f"/api/v1/purchase-orders/{po['id']}/shipments").json()["data"]
    assert len(listed) == 1


def test_add_demand_exception_defaults_to_first_line(client, retailer):
    po = _create_purchase_order(client, retailer["id"])

    resp = client.post(
        f"/api/v1/purchase-orders/{po['id']}/demand-exceptions",
        json={"exception_id": "EXC-001", "flagged_date": "2026-08-03"},
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()["data"]
    assert created["purchase_order_line_id"] == po["lines"][0]["id"]

    listed = client.get(f"/api/v1/purchase-orders/{po['id']}/demand-exceptions").json()["data"]
    assert len(listed) == 1


def test_add_demand_exception_with_no_lines_returns_422(client, retailer):
    po = _create_purchase_order(client, retailer["id"], with_line=False)

    resp = client.post(
        f"/api/v1/purchase-orders/{po['id']}/demand-exceptions",
        json={"exception_id": "EXC-002", "flagged_date": "2026-08-03"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PO_HAS_NO_LINES"


def test_add_and_list_actual_penalty(client, retailer):
    po = _create_purchase_order(client, retailer["id"])

    resp = client.post(
        "/api/v1/penalties/actual-penalties",
        json={
            "purchase_order_id": po["id"],
            "actual_penalty_number": "PEN-001",
            "violation_type": "SHORT_SHIP",
            "actual_penalty_amount": 123.45,
            "invoice_or_deduction_date": "2026-08-15",
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()["data"]

    listed = client.get("/api/v1/penalties/actual-penalties", params={"purchase_order_id": po["id"]}).json()[
        "data"
    ]
    assert len(listed) == 1
    assert listed[0]["actual_penalty_amount"] == 123.45

    fetched = client.get(f"/api/v1/penalties/actual-penalties/{created['id']}").json()["data"]
    assert fetched["id"] == created["id"]


def test_list_actual_penalties_across_purchase_orders(client, retailer):
    po = _create_purchase_order(client, retailer["id"])
    client.post(
        "/api/v1/penalties/actual-penalties",
        json={
            "purchase_order_id": po["id"],
            "actual_penalty_number": "PEN-002",
            "violation_type": "SHORT_SHIP",
            "actual_penalty_amount": 50.0,
            "invoice_or_deduction_date": "2026-08-15",
        },
    )

    listed = client.get("/api/v1/penalties/actual-penalties").json()["data"]
    assert any(row["purchase_order_id"] == po["id"] for row in listed)


def test_get_actual_penalty_unknown_id_returns_404(client):
    unknown_id = "00000000-0000-0000-0000-000000000000"

    resp = client.get(f"/api/v1/penalties/actual-penalties/{unknown_id}")

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "ACTUAL_PENALTY_NOT_FOUND"
    # Regression: must render the UUID's clean str() form, never the raw
    # `UUID('...')` repr (a bare `!r` in the f-string used to leak that).
    assert body["message"] == f"No actual penalty found with actual_penalty_id={unknown_id}"
    assert "UUID(" not in body["message"]


def test_facts_against_unknown_purchase_order_return_404(client):
    unknown_id = "00000000-0000-0000-0000-000000000000"

    resp = client.get(f"/api/v1/purchase-orders/{unknown_id}/shipments")

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "PO_NOT_FOUND"
    # Regression: must render the UUID's clean str() form, never the raw
    # `UUID('...')` repr (a bare `!r` in the f-string used to leak that).
    assert body["message"] == f"No purchase order found with purchase_order_id={unknown_id}"
    assert "UUID(" not in body["message"]
