"""API tests for the PO delivery-date change request/response lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from tests.conftest import make_retailer_agreement


def _create_retailer_agreement(repos, retailer_id: str) -> str:
    """Create a `retailer_agreement` via the repository layer.

    Not the real `POST /penalties/retailer-agreements` endpoint: that route's dependency
    unconditionally builds `Container`'s real Postgres-backed LangGraph checkpointer (see
    `get_penalty_rule_extraction_service`), which this test environment can't reach.
    `penalty_rule.retailer_agreement_id` is NOT NULL.
    """
    return str(make_retailer_agreement(repos, UUID(retailer_id)))


# Dates relative to "today" rather than a hardcoded calendar date -- the lead-time
# check (extension_min_lead_days) compares required_ship_date against the actual
# clock at test-run time, so a fixed literal date silently goes stale once real
# time passes it (found during Phase 7a verification: a hardcoded 2026-01-18
# required_ship_date read as -224 days from "today" once the calendar moved past it).
_TODAY = datetime.now(tz=UTC).date()
_ORDER_DATE = _TODAY.isoformat()
_REQUIRED_SHIP_DATE = (_TODAY + timedelta(days=17)).isoformat()
_REQUESTED_DELIVERY_DATE = (_TODAY + timedelta(days=19)).isoformat()
_PROPOSED_DATE_1 = (_TODAY + timedelta(days=24)).isoformat()
_PROPOSED_DATE_2 = (_TODAY + timedelta(days=25)).isoformat()
_PROPOSED_DATE_3 = (_TODAY + timedelta(days=29)).isoformat()
_COUNTERED_DATE = (_TODAY + timedelta(days=26)).isoformat()


@pytest.fixture
def purchase_order(client, repos) -> dict:
    retailer = client.post(
        "/api/v1/retailers",
        json={
            "retailer_code": "RET-DCR",
            "retailer_name": "DCR Co",
            "extension_min_lead_days": 1,
            "extension_response_sla_hours": 48,
        },
    ).json()["data"]
    # record_response re-runs the projection engine for this PO (it needs an
    # up-to-date penalty exposure after a delivery-date negotiation resolves) --
    # without an active rule for the retailer, that re-projection legitimately
    # raises NO_ACTIVE_RULES.
    retailer_agreement = _create_retailer_agreement(repos, retailer["id"])
    client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-DCR",
            "retailer_id": retailer["id"],
            "retailer_agreement_id": retailer_agreement,
            "penalty_category": "OTIF_LATE",
            "violation_type": "OTIF_LATE",
            "calc_type": "PER_UNIT",
            "rate": 1.0,
        },
    )
    return client.post(
        "/api/v1/purchase-orders",
        json={
            "purchase_order_number": "PO-DCR-001",
            "retailer_id": retailer["id"],
            "order_date": _ORDER_DATE,
            "requested_delivery_date": _REQUESTED_DELIVERY_DATE,
            "required_ship_date": _REQUIRED_SHIP_DATE,
            "lines": [{"line_number": "10", "ordered_quantity": 100, "unit_price": 5.0}],
        },
    ).json()["data"]


def test_create_and_list_delivery_change_request(client, purchase_order):
    resp = client.post(
        "/api/v1/delivery-change-requests",
        json={
            "purchase_order_id": purchase_order["id"],
            "reason_code": "SHORTAGE",
            "proposed_delivery_date": _PROPOSED_DATE_1,
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()["data"]
    assert created["status"] == "PENDING"
    assert created["proposed_delivery_date"] == _PROPOSED_DATE_1

    listed = client.get(
        "/api/v1/delivery-change-requests", params={"purchase_order_id": purchase_order["id"]}
    ).json()["data"]
    assert len(listed) == 1

    fetched = client.get(f"/api/v1/delivery-change-requests/{created['id']}").json()["data"]
    assert fetched["id"] == created["id"]


def test_list_delivery_change_requests_across_purchase_orders(client, purchase_order):
    client.post(
        "/api/v1/delivery-change-requests",
        json={
            "purchase_order_id": purchase_order["id"],
            "reason_code": "SHORTAGE",
            "proposed_delivery_date": _PROPOSED_DATE_1,
        },
    )

    listed = client.get("/api/v1/delivery-change-requests").json()["data"]
    assert any(row["purchase_order_id"] == purchase_order["id"] for row in listed)


def test_get_delivery_change_request_unknown_id_returns_404(client):
    resp = client.get(f"/api/v1/delivery-change-requests/{uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PO_DELIVERY_CHANGE_REQUEST_NOT_FOUND"


def test_a_second_active_request_is_rejected(client, purchase_order):
    client.post(
        "/api/v1/delivery-change-requests",
        json={
            "purchase_order_id": purchase_order["id"],
            "reason_code": "SHORTAGE",
            "proposed_delivery_date": _PROPOSED_DATE_1,
        },
    )

    resp = client.post(
        "/api/v1/delivery-change-requests",
        json={
            "purchase_order_id": purchase_order["id"],
            "reason_code": "DELAY",
            "proposed_delivery_date": _PROPOSED_DATE_2,
        },
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ACTIVE_PO_DELIVERY_CHANGE_REQUEST_EXISTS"


def test_record_accepted_response_updates_purchase_order_dates(client, purchase_order):
    created = client.post(
        "/api/v1/delivery-change-requests",
        json={
            "purchase_order_id": purchase_order["id"],
            "reason_code": "SHORTAGE",
            "proposed_delivery_date": _PROPOSED_DATE_1,
        },
    ).json()["data"]

    resp = client.post(
        f"/api/v1/delivery-change-requests/{created['id']}/response",
        json={"decision": "ACCEPTED"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["status"] == "ACCEPTED"

    updated_po = next(
        po for po in client.get("/api/v1/purchase-orders").json()["data"] if po["id"] == purchase_order["id"]
    )
    assert updated_po["current_delivery_date"] == _PROPOSED_DATE_1
    assert updated_po["negotiation_status"] == "ACCEPTED"


def test_countered_response_requires_a_countered_date_between_baseline_and_proposed(client, purchase_order):
    created = client.post(
        "/api/v1/delivery-change-requests",
        json={
            "purchase_order_id": purchase_order["id"],
            "reason_code": "DELAY",
            "proposed_delivery_date": _PROPOSED_DATE_3,
        },
    ).json()["data"]

    resp = client.post(
        f"/api/v1/delivery-change-requests/{created['id']}/response",
        json={"decision": "COUNTERED", "countered_delivery_date": _COUNTERED_DATE},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "COUNTERED"
    assert resp.json()["data"]["countered_delivery_date"] == _COUNTERED_DATE


def test_response_to_unknown_id_returns_404(client, purchase_order):
    resp = client.post(
        f"/api/v1/delivery-change-requests/{uuid4()}/response",
        json={"decision": "ACCEPTED"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PO_DELIVERY_CHANGE_REQUEST_NOT_FOUND"
