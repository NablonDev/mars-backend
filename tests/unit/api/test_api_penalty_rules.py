"""API tests for `penalties.penalty_rule`."""

from __future__ import annotations

from uuid import UUID

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


@pytest.fixture
def retailer(client):
    return client.post(
        "/api/v1/retailers", json={"retailer_code": "RET-RULE", "retailer_name": "Rule Co"}
    ).json()["data"]


@pytest.fixture
def retailer_agreement(repos, retailer):
    return _create_retailer_agreement(repos, retailer["id"])


def test_create_flat_rate_rule(client, retailer, retailer_agreement):
    resp = client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-FLAT",
            "retailer_agreement_id": retailer_agreement,
            "penalty_category": "SHORT_SHIP",
            "violation_type": "SHORT_SHIP",
            "calc_type": "PER_UNIT",
            "rate": 2.5,
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()["data"]
    assert created["calc_type"] == "PER_UNIT"
    assert created["is_active"] is True


def test_tiered_rule_without_tiers_is_rejected(client, retailer, retailer_agreement):
    resp = client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-BAD-TIERED",
            "retailer_agreement_id": retailer_agreement,
            "penalty_category": "SHORT_SHIP",
            "violation_type": "SHORT_SHIP",
            "calc_type": "TIERED",
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "REQUEST_VALIDATION_ERROR"
    errors = body["error"]["details"]["errors"]
    assert any("calc_type=TIERED requires at least one tier band" in e["msg"] for e in errors)


def test_list_rules_filters_by_retailer(client, repos, retailer, retailer_agreement):
    other_retailer = client.post(
        "/api/v1/retailers", json={"retailer_code": "RET-OTHER", "retailer_name": "Other"}
    ).json()["data"]
    other_retailer_agreement = _create_retailer_agreement(repos, other_retailer["id"])

    client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-MINE",
            "retailer_agreement_id": retailer_agreement,
            "penalty_category": "OTIF_LATE",
            "violation_type": "OTIF_LATE",
            "calc_type": "FLAT_FEE",
            "rate": 100.0,
        },
    )
    client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-THEIRS",
            "retailer_agreement_id": other_retailer_agreement,
            "penalty_category": "OTIF_LATE",
            "violation_type": "OTIF_LATE",
            "calc_type": "FLAT_FEE",
            "rate": 50.0,
        },
    )

    mine = client.get("/api/v1/penalties/rules", params={"retailer_id": retailer["id"]}).json()["data"]
    assert {r["rule_code"] for r in mine} == {"RULE-MINE"}
