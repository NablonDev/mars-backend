"""API tests for `penalties.penalty_dispute`: open/list/get, the synchronous
analyze endpoint, resolve/override, and the summary trigger/poll contract
(mocked LLM only -- no live API calls).

Seeds its own fixture data directly via the repository layer against the
shared `database` fixture (same "write through the same SQLite connection
the app reads from" pattern `test_api_penalty_projections.py`'s
`ready_projection_summary` fixture uses) rather than a chain of HTTP round
trips, since `open_dispute`/`analyze` need an already-recorded
`actual_penalty` charge, penalty rule, and real post-delivery facts.

Real, post-delivery facts are seeded via `delivery`/`delivery_line` -- never
`order_confirmation`, the pre-delivery promise a dispute must not use (see
`app.services.penalties.dispute.types`'s module docstring).
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

ORDER_QTY = 100
UNIT_PRICE = 10.0
CHARGE_DATE = "2026-06-05"


@pytest.fixture
def dispute_fixture(database) -> dict:
    """Seeds one PO with a SHORT_SHIP/PER_UNIT rule, a real 10-unit
    shortfall (delivered_qty=90) as of `CHARGE_DATE`, and one
    `actual_penalty` charge of $80 against a $50 real computed amount
    (PAY_PARTIAL once analyzed). Returns the ids the tests need."""
    from app.repositories.common.fulfillment import FulfillmentRepository
    from app.repositories.common.master_data import MasterDataRepository
    from app.repositories.common.purchase_order import PurchaseOrderRepository
    from app.repositories.common.retailer_agreement import RetailerAgreementRepository
    from app.repositories.penalties.projection import ActualPenaltyRepository
    from app.repositories.penalties.rule import PenaltyRuleRepository

    with database.session() as session:
        master_data = MasterDataRepository(session)
        purchase_orders = PurchaseOrderRepository(session)
        fulfillment = FulfillmentRepository(session)
        rules = PenaltyRuleRepository(session)
        actual_penalties = ActualPenaltyRepository(session)
        retailer_agreements = RetailerAgreementRepository(session)

        retailer = master_data.add_retailer("RET-API-DSP", "API Dispute Retailer", None, "SUM")
        material = master_data.add_material("MAT-API-DSP", None)
        plant = master_data.add_plant("PLANT-API-DSP", None, None)
        retailer_agreement = retailer_agreements.add_retailer_agreement(
            retailer_id=retailer["id"],
            contract_code="TEST-API-DSP",
            title="Test retailer agreement",
            document_sha256="0" * 64,
        )
        rules.add_rule(
            rule_code="RULE-API-DSP",
            violation_type="SHORT_SHIP",
            penalty_category="SHORT_SHIP",
            retailer_agreement_id=retailer_agreement["id"],
            calc_type="PER_UNIT",
            rate=5.0,
            effective_start_date=date(2026, 1, 1),
        )
        purchase_order = purchase_orders.create_purchase_order(
            purchase_order_number="ORD-API-DSP",
            retailer_id=retailer["id"],
            order_date=date(2026, 5, 1),
            requested_delivery_date=date(2026, 6, 10),
            required_ship_date=date(2026, 6, 8),
            order_status="DELIVERED",
        )
        line = purchase_orders.add_line(
            purchase_order_id=purchase_order["id"],
            line_number="10",
            ordered_quantity=ORDER_QTY,
            unit_price=UNIT_PRICE,
            material_id=material["id"],
            plant_id=plant["id"],
        )
        delivery = fulfillment.add_delivery(
            delivery_number="DELIV-API-DSP",
            purchase_order_id=purchase_order["id"],
            actual_delivery_date=date.fromisoformat(CHARGE_DATE),
        )
        fulfillment.add_delivery_line(
            delivery_id=delivery["id"],
            purchase_order_line_id=line["id"],
            delivered_quantity=90,
        )
        actual_penalty = actual_penalties.add_actual_penalty(
            actual_penalty_number="AP-API-DSP",
            purchase_order_id=purchase_order["id"],
            violation_type="SHORT_SHIP",
            actual_penalty_amount=80.0,
            invoice_or_deduction_date=date.fromisoformat(CHARGE_DATE),
        )
        session.commit()

    return {"purchase_order_id": str(purchase_order["id"]), "actual_penalty_id": str(actual_penalty["id"])}


def test_open_list_get_dispute(client, dispute_fixture):
    resp = client.post(
        "/api/v1/penalties/disputes",
        json={
            "actual_penalty_id": dispute_fixture["actual_penalty_id"],
            "reason_code": "AMOUNT_INCORRECT",
            "claimed_amount": 80.0,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["dispute_status"] == "OPEN"
    dispute_id = body["id"]

    listed = client.get(
        "/api/v1/penalties/disputes", params={"purchase_order_id": dispute_fixture["purchase_order_id"]}
    )
    assert listed.status_code == 200
    assert [d["id"] for d in listed.json()["data"]] == [dispute_id]

    fetched = client.get(f"/api/v1/penalties/disputes/{dispute_id}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["id"] == dispute_id


def test_open_dispute_accepts_valid_claim_facts(client, dispute_fixture):
    resp = client.post(
        "/api/v1/penalties/disputes",
        json={
            "actual_penalty_id": dispute_fixture["actual_penalty_id"],
            "reason_code": "AMOUNT_INCORRECT",
            "claimed_amount": 80.0,
            "claim_facts": {"defect_units": 12},
        },
    )
    assert resp.status_code == 201, resp.text


def test_open_dispute_rejects_out_of_bounds_claim_facts(client, dispute_fixture):
    """Fail closed at the Pydantic layer: claim_facts is attacker-controlled input that
    directly drives a monetary verdict, so an out-of-range value never reaches the handler."""
    resp = client.post(
        "/api/v1/penalties/disputes",
        json={
            "actual_penalty_id": dispute_fixture["actual_penalty_id"],
            "reason_code": "AMOUNT_INCORRECT",
            "claimed_amount": 80.0,
            "claim_facts": {"defect_rate_pct": 1.5},  # a rate, must be within [0, 1]
        },
    )
    assert resp.status_code == 422


def test_open_dispute_rejects_mars_derived_field_in_claim_facts(client, dispute_fixture):
    """`ClaimFacts.model_config = ConfigDict(extra="forbid")` structurally enforces the
    claim-supplied/Mars-derived boundary: a Mars-derived field name can never enter
    claim_facts through this schema, even under a plausible-looking value."""
    resp = client.post(
        "/api/v1/penalties/disputes",
        json={
            "actual_penalty_id": dispute_fixture["actual_penalty_id"],
            "reason_code": "AMOUNT_INCORRECT",
            "claimed_amount": 80.0,
            "claim_facts": {"actual_purchase_quantity": 999999.0},
        },
    )
    assert resp.status_code == 422


def test_open_dispute_unknown_actual_penalty_returns_404(client):
    resp = client.post(
        "/api/v1/penalties/disputes",
        json={
            "actual_penalty_id": str(UUID(int=0)),
            "reason_code": "OTHER",
            "claimed_amount": 10.0,
        },
    )
    assert resp.status_code == 404


def test_analyze_and_resolve_with_override_full_flow(client, dispute_fixture):
    opened = client.post(
        "/api/v1/penalties/disputes",
        json={
            "actual_penalty_id": dispute_fixture["actual_penalty_id"],
            "reason_code": "AMOUNT_INCORRECT",
            "claimed_amount": 80.0,
        },
    ).json()["data"]

    analyzed_resp = client.post(f"/api/v1/penalties/disputes/{opened['id']}/analyze")
    assert analyzed_resp.status_code == 200, analyzed_resp.text
    analyzed = analyzed_resp.json()["data"]
    assert analyzed["dispute_status"] == "ANALYZED"
    assert analyzed["verdict"] == "PAY_PARTIAL"
    assert analyzed["computed_amount"] == 50.0
    assert analyzed["delta_amount"] == 30.0

    # resolve without override_reason but with override_verdict is rejected
    bad = client.post(
        f"/api/v1/penalties/disputes/{opened['id']}/resolve",
        json={"resolved_by": "ops@mars.test", "override_verdict": "PAY_FULL"},
    )
    assert bad.status_code == 422

    resolved_resp = client.post(
        f"/api/v1/penalties/disputes/{opened['id']}/resolve",
        json={
            "resolved_by": "ops-lead@mars.test",
            "override_verdict": "PAY_FULL",
            "override_reason": "Ops discretion.",
        },
    )
    assert resolved_resp.status_code == 200, resolved_resp.text
    resolved = resolved_resp.json()["data"]
    assert resolved["dispute_status"] == "OVERRIDDEN"
    assert resolved["override_verdict"] == "PAY_FULL"
    assert resolved["resolved_by"] == "ops-lead@mars.test"


def test_resolve_before_analyze_rejected(client, dispute_fixture):
    opened = client.post(
        "/api/v1/penalties/disputes",
        json={
            "actual_penalty_id": dispute_fixture["actual_penalty_id"],
            "reason_code": "AMOUNT_INCORRECT",
            "claimed_amount": 80.0,
        },
    ).json()["data"]

    resp = client.post(
        f"/api/v1/penalties/disputes/{opened['id']}/resolve", json={"resolved_by": "ops@mars.test"}
    )
    assert resp.status_code == 422


class _FakeAIMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content
        self.tool_calls: list = []


class _FakeChatClient:
    model_name = "fake-model"

    def invoke(self, messages, *, tools=None):
        return _FakeAIMessage(content="This dispute was resolved because...")


def test_dispute_summary_trigger_and_poll(client, app, dispute_fixture):
    from app.api.dependencies import get_llm_client

    app.dependency_overrides[get_llm_client] = lambda: _FakeChatClient()

    opened = client.post(
        "/api/v1/penalties/disputes",
        json={
            "actual_penalty_id": dispute_fixture["actual_penalty_id"],
            "reason_code": "AMOUNT_INCORRECT",
            "claimed_amount": 80.0,
        },
    ).json()["data"]

    # Cannot request a narrative before a verdict exists.
    too_early = client.post(f"/api/v1/penalties/disputes/{opened['id']}/summary", json={})
    assert too_early.status_code == 409

    client.post(f"/api/v1/penalties/disputes/{opened['id']}/analyze")

    triggered = client.post(f"/api/v1/penalties/disputes/{opened['id']}/summary", json={})
    assert triggered.status_code in (200, 202), triggered.text
    status_value = triggered.json()["data"]["status"]
    assert status_value in ("PENDING", "READY")

    polled = client.get(f"/api/v1/penalties/disputes/{opened['id']}/summary")
    assert polled.status_code == 200
