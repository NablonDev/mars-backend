"""Tests for RetailerAgreementRepository and the extracted-rule/rule-publication repositories
(app.repositories.common.retailer_agreement, app.repositories.penalties.rule_extraction)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.core.exceptions import ValidationError
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule_extraction import (
    ExtractedPenaltyRuleRepository,
    RulePublicationRepository,
    published_rule_insert_kwargs,
)
from app.repositories.process.agent_registry import AgentRegistryRepository, AgentRunRepository
from app.services.penalties.rule_extraction.types import PublishedRule, PublishedTier, StagedRule


@pytest.fixture
def retailer_agreements(db_session) -> RetailerAgreementRepository:
    return RetailerAgreementRepository(db_session)


@pytest.fixture
def extracted_rules(db_session) -> ExtractedPenaltyRuleRepository:
    return ExtractedPenaltyRuleRepository(db_session)


@pytest.fixture
def publications(db_session) -> RulePublicationRepository:
    return RulePublicationRepository(db_session)


def _make_agent_run(db_session) -> UUID:
    """Register the extractor agent and open one run against it."""
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="penalty_rule_extractor",
        prompt_version="v1",
        system_prompt="Extract penalty clauses from retailer agreement markdown.",
        agent_name="Penalty Rule Extractor",
        domain="penalties",
    )
    return AgentRunRepository(db_session).start(agent_id, run_type="EXTRACTION")


def _make_retailer_agreement(repos, retailer_agreements: RetailerAgreementRepository, sha256: str) -> dict:
    retailer = repos.master_data.add_retailer("RET-EXTRACT", "Retailer Extract", None, "SUM")
    return retailer_agreements.add_retailer_agreement(
        retailer_id=retailer["id"],
        contract_code=f"CONTRACT-{sha256[:8]}",
        title="Example Retailer Agreement",
        document_sha256=sha256,
        markdown_text="# Agreement",
    )


def test_get_by_sha256_finds_the_retailer_agreement_uploaded_under_that_hash(repos, retailer_agreements):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "b" * 64)

    found = retailer_agreements.get_by_sha256("b" * 64)

    assert found is not None
    assert found["id"] == retailer_agreement["id"]


def test_get_by_sha256_returns_none_for_an_unseen_hash(retailer_agreements):
    assert retailer_agreements.get_by_sha256("c" * 64) is None


def test_list_all_returns_every_retailer_agreement(repos, retailer_agreements):
    retailer = repos.master_data.add_retailer("RET-LIST-ALL", "Retailer List All", None, "SUM")
    first = retailer_agreements.add_retailer_agreement(
        retailer_id=retailer["id"],
        contract_code="CONTRACT-20",
        title="Example Retailer Agreement",
        document_sha256="20" * 32,
        markdown_text="# Agreement",
    )
    second = retailer_agreements.add_retailer_agreement(
        retailer_id=retailer["id"],
        contract_code="CONTRACT-21",
        title="Example Retailer Agreement",
        document_sha256="21" * 32,
        markdown_text="# Agreement",
    )

    ids = {row["id"] for row in retailer_agreements.list_all()}

    assert first["id"] in ids
    assert second["id"] in ids


def test_get_with_attributes_returns_the_rule_and_its_attribute_rows(
    repos, retailer_agreements, extracted_rules, db_session
):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "22" * 32)
    agent_run_id = _make_agent_run(db_session)
    rule = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Supplier shall pay 2% of shortfall value.",
        clause_fingerprint="23" * 16,
        penalty_category="FILL_RATE_SHORTFALL",
        calc_type="PERCENT_OF_PO",
        pricing_readiness="READY",
        confidence=0.9,
        attributes=[
            {
                "branch_no": 0,
                "attribute_role": "RATE",
                "value": 2.0,
                "value_unit": "PERCENT",
                "value_status": "PRESENT",
                "basis_type": "SHORTFALL_VALUE",
                "source_text": "2% of shortfall value.",
                "confidence": 0.9,
            }
        ],
    )

    result = extracted_rules.get_with_attributes(rule.id)

    assert result is not None
    assert result["id"] == rule.id
    assert result["retailer_agreement_id"] == retailer_agreement["id"]
    assert len(result["attributes"]) == 1
    assert result["attributes"][0].attribute_role == "RATE"


def test_get_with_attributes_returns_none_for_an_unknown_id(extracted_rules):
    assert extracted_rules.get_with_attributes(uuid4()) is None


def test_list_with_attributes_for_retailer_agreement_attaches_each_rules_attributes(
    repos, retailer_agreements, extracted_rules, db_session
):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "24" * 32)
    agent_run_id = _make_agent_run(db_session)
    with_attribute = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Supplier shall pay 2% of shortfall value.",
        clause_fingerprint="25" * 16,
        penalty_category="FILL_RATE_SHORTFALL",
        calc_type="PERCENT_OF_PO",
        pricing_readiness="READY",
        confidence=0.9,
        attributes=[
            {
                "branch_no": 0,
                "attribute_role": "RATE",
                "value": 2.0,
                "value_unit": "PERCENT",
                "value_status": "PRESENT",
                "basis_type": "SHORTFALL_VALUE",
                "source_text": "2% of shortfall value.",
                "confidence": 0.9,
            }
        ],
    )
    without_attribute = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Supplier shall pay a late delivery fee.",
        clause_fingerprint="26" * 16,
        penalty_category="LATE_DELIVERY",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.8,
    )

    rows = {
        row["id"]: row
        for row in extracted_rules.list_with_attributes_for_retailer_agreement(retailer_agreement["id"])
    }

    assert len(rows[with_attribute.id]["attributes"]) == 1
    assert rows[with_attribute.id]["attributes"][0].attribute_role == "RATE"
    assert rows[without_attribute.id]["attributes"] == []


def test_set_review_decision_moves_a_rule_to_approved_or_rejected(
    repos, retailer_agreements, extracted_rules, db_session
):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "d" * 64)
    agent_run_id = _make_agent_run(db_session)

    rule = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Supplier shall pay 2% of shortfall value.",
        clause_fingerprint="f" * 32,
        penalty_category="FILL_RATE_SHORTFALL",
        calc_type="PERCENT_OF_PO",
        pricing_readiness="READY",
        confidence=0.9,
        po_shortage_flag=True,
    )
    assert rule.status == "PENDING_REVIEW"

    approved = extracted_rules.set_review_decision(rule.id, "APPROVED", review_notes="checked against 7.2")
    assert approved.status == "APPROVED"
    assert approved.review_notes == "checked against 7.2"

    rejected = extracted_rules.set_review_decision(rule.id, "REJECTED")
    assert rejected.status == "REJECTED"


def test_set_review_decision_rejects_a_status_outside_approved_or_rejected(
    repos, retailer_agreements, extracted_rules, db_session
):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "e" * 64)
    agent_run_id = _make_agent_run(db_session)

    rule = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Supplier shall pay a late delivery fee.",
        clause_fingerprint="1" * 32,
        penalty_category="LATE_DELIVERY",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.8,
        po_delay_flag=True,
    )

    with pytest.raises(ValidationError):
        extracted_rules.set_review_decision(rule.id, "PENDING_REVIEW")


def test_list_publishable_returns_only_approved_rules_from_the_current_run(
    repos, retailer_agreements, extracted_rules, db_session
):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "2" * 64)
    current_run_id = _make_agent_run(db_session)
    stale_run_id = _make_agent_run(db_session)

    approved_current = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=current_run_id,
        clause_text="Supplier shall pay 2% of shortfall value.",
        clause_fingerprint="3" * 32,
        penalty_category="FILL_RATE_SHORTFALL",
        calc_type="PERCENT_OF_PO",
        pricing_readiness="READY",
        confidence=0.9,
        po_shortage_flag=True,
        attributes=[
            {
                "branch_no": 0,
                "attribute_role": "RATE",
                "value": 2.0,
                "value_unit": "PERCENT",
                "value_status": "PRESENT",
                "basis_type": "PO_VALUE",
                "source_text": "2% of shortfall value.",
                "confidence": 0.9,
            }
        ],
    )
    extracted_rules.set_review_decision(approved_current.id, "APPROVED")

    # Not yet reviewed: must not be publishable even though it's in the current run.
    extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=current_run_id,
        clause_text="Not yet reviewed.",
        clause_fingerprint="4" * 32,
        penalty_category="LATE_DELIVERY",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.7,
        po_delay_flag=True,
    )

    # Approved, but from a superseded run: must not be publishable.
    approved_stale = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=stale_run_id,
        clause_text="From a superseded extraction run.",
        clause_fingerprint="5" * 32,
        penalty_category="LATE_DELIVERY",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.7,
        po_delay_flag=True,
    )
    extracted_rules.set_review_decision(approved_stale.id, "APPROVED")

    staged = extracted_rules.list_publishable(retailer_agreement["id"], current_run_id)

    assert [s.id for s in staged] == [str(approved_current.id)]
    assert isinstance(staged[0], StagedRule)
    assert len(staged[0].facts) == 1
    assert staged[0].facts[0].attribute_role == "RATE"


def test_list_publishable_returns_nothing_for_a_run_id_with_no_approved_rows(
    repos, retailer_agreements, extracted_rules, db_session
):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "6" * 64)
    agent_run_id = _make_agent_run(db_session)
    other_run_id = _make_agent_run(db_session)

    rule = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Approved, but under a different run than the one being published.",
        clause_fingerprint="7" * 32,
        penalty_category="LATE_DELIVERY",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.7,
        po_delay_flag=True,
    )
    extracted_rules.set_review_decision(rule.id, "APPROVED")

    assert extracted_rules.list_publishable(retailer_agreement["id"], other_run_id) == []


def test_reason_histogram_counts_rejection_reasons_only(
    repos, retailer_agreements, extracted_rules, publications, db_session
):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "8" * 64)
    agent_run_id = _make_agent_run(db_session)

    rule_one = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="A liability cap, not a charge.",
        clause_fingerprint="9" * 32,
        penalty_category="LIMITATION_OF_LIABILITY",
        calc_type="LIMIT_ONLY",
        pricing_readiness="NOT_A_CHARGE",
        confidence=0.6,
    )
    rule_two = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Another non-chargeable clause.",
        clause_fingerprint="0" * 32,
        penalty_category="LIMITATION_OF_LIABILITY",
        calc_type="LIMIT_ONLY",
        pricing_readiness="NOT_A_CHARGE",
        confidence=0.6,
    )
    rule_three = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Published rule.",
        clause_fingerprint="a" * 32,
        penalty_category="FILL_RATE_SHORTFALL",
        calc_type="PERCENT_OF_PO",
        pricing_readiness="READY",
        confidence=0.9,
        po_shortage_flag=True,
    )

    publications.record(rule_one.id, agent_run_id, "REJECTED", reason_code="UNSUPPORTED_CALC_TYPE")
    publications.record(rule_two.id, agent_run_id, "REJECTED", reason_code="UNSUPPORTED_CALC_TYPE")
    publications.record(rule_three.id, agent_run_id, "PUBLISHED")

    histogram = publications.reason_histogram(retailer_agreement["id"])

    assert histogram == {"UNSUPPORTED_CALC_TYPE": 2}
    assert len(publications.list_for_retailer_agreement(retailer_agreement["id"])) == 3


# ---------------------------------------------------------------------------
# Dropped defensive-duplicate CHECK constraints: `status`, `attribute_role`, and
# `outcome` are re-validated by their Pydantic gates before a row is ever built
# (app/agents/penalties/rule_extraction/schema.py, and the service's own literals
# for `outcome`); the DB no longer re-guards them. These bypass that gate directly
# through the repository to prove the DB layer itself no longer rejects a bad value.
# ---------------------------------------------------------------------------


def test_extracted_rule_status_has_no_db_level_check_only_the_pydantic_gate_does(
    repos, retailer_agreements, extracted_rules, db_session
):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "f1" * 32)
    agent_run_id = _make_agent_run(db_session)

    rule = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Bypasses the Pydantic gate to prove the DB no longer checks this.",
        clause_fingerprint="f1" * 16,
        penalty_category="LATE_DELIVERY",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.7,
        status="NOT_A_REAL_STATUS",
        po_delay_flag=True,
    )

    assert rule.status == "NOT_A_REAL_STATUS"


def test_extracted_rule_attribute_role_has_no_db_level_check_only_the_pydantic_gate_does(
    repos, retailer_agreements, extracted_rules, db_session
):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "f2" * 32)
    agent_run_id = _make_agent_run(db_session)

    rule = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Bypasses the Pydantic gate to prove the DB no longer checks this.",
        clause_fingerprint="f2" * 16,
        penalty_category="LATE_DELIVERY",
        calc_type="PER_UNIT",
        pricing_readiness="AWAITING_DATA",
        confidence=0.7,
        po_delay_flag=True,
        attributes=[
            {
                "branch_no": 0,
                "attribute_role": "NOT_A_REAL_ROLE",
                "value_status": "NOT_STATED",
                "source_text": "n/a",
                "confidence": 0.5,
            }
        ],
    )

    result = extracted_rules.get_with_attributes(rule.id)
    assert result["attributes"][0].attribute_role == "NOT_A_REAL_ROLE"


def test_rule_publication_outcome_has_no_db_level_check(
    repos, retailer_agreements, extracted_rules, publications, db_session
):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "f3" * 32)
    agent_run_id = _make_agent_run(db_session)
    rule = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Published rule.",
        clause_fingerprint="f3" * 16,
        penalty_category="LATE_DELIVERY",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.9,
        po_delay_flag=True,
    )

    outcome = publications.record(rule.id, agent_run_id, "NOT_A_REAL_OUTCOME")

    assert outcome.outcome == "NOT_A_REAL_OUTCOME"


EXTRACTED_RULE_ID = UUID("11111111-1111-1111-1111-111111111111")


def _published_rule(**overrides) -> PublishedRule:
    fields = {
        "rule_code": "WMT-SHORT_SHIP-a1b2c3d4",
        "retailer_code": "WMT",
        "violation_type": "SHORT_SHIP",
        "calc_type": "PER_UNIT",
        "rate": Decimal("5.00"),
        "threshold_pct": Decimal(0),
        "cap_amount": Decimal("1000.00"),
        "grace_period_days": 0,
        "basis_type": "COST_OF_GOODS",
        "currency_code": "USD",
        "applies_per": None,
        "effective_start_date": date(2026, 1, 1),
        "extracted_rule_id": str(EXTRACTED_RULE_ID),
        **overrides,
    }
    return PublishedRule(**fields)


def test_published_rule_insert_kwargs_shapes_decimals_to_floats_and_carries_retailer_id():
    retailer_id = uuid4()

    kwargs = published_rule_insert_kwargs(_published_rule(), retailer_id)

    assert kwargs == {
        "rule_code": "WMT-SHORT_SHIP-a1b2c3d4",
        "retailer_id": retailer_id,
        "violation_type": "SHORT_SHIP",
        "calc_type": "PER_UNIT",
        "rate": 5.0,
        "threshold_pct": 0.0,
        "cap_amount": 1000.0,
        "grace_period_days": 0,
        "effective_start_date": date(2026, 1, 1),
        "tiers": None,
        "basis_type": "COST_OF_GOODS",
        "applies_per": None,
        "currency_code": "USD",
    }


def test_published_rule_insert_kwargs_shapes_tiers_and_a_none_cap():
    published = _published_rule(
        calc_type="TIERED",
        cap_amount=None,
        tiers=[
            PublishedTier(tier_code="T1", band_min=Decimal(0), band_max=Decimal("0.1"), rate=Decimal("0.01")),
            PublishedTier(
                tier_code="T2", band_min=Decimal("0.1"), band_max=Decimal("Infinity"), rate=Decimal("0.02")
            ),
        ],
    )

    kwargs = published_rule_insert_kwargs(published, uuid4())

    assert kwargs["cap_amount"] is None
    assert kwargs["tiers"] == [
        {"band_min": 0.0, "band_max": 0.1, "rate": 0.01},
        {"band_min": 0.1, "band_max": float("inf"), "rate": 0.02},
    ]


def test_published_rule_insert_kwargs_carries_basis_currency_and_applies_per():
    # Regression test for the gap where these three fields reached the publisher's output
    # but were dropped before the insert, leaving basis_type NULL.
    published = _published_rule(basis_type="SHORTFALL_VALUE", currency_code="EUR", applies_per="DAY")

    kwargs = published_rule_insert_kwargs(published, uuid4())

    assert kwargs["basis_type"] == "SHORTFALL_VALUE"
    assert kwargs["currency_code"] == "EUR"
    assert kwargs["applies_per"] == "DAY"
    assert "extracted_rule_id" not in kwargs
