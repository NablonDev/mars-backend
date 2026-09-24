"""Tests for RetailerAgreementRepository and the extracted-rule/rule-publication repositories
(app.repositories.common.retailer_agreement, app.repositories.penalties.rule_extraction)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.core.exceptions import ValidationError
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule_extraction import (
    ExtractedPenaltyRuleRepository,
    ExtractedPenaltyRuleRevisionRepository,
    RulePublicationRepository,
    _stale_cutoff,
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


@pytest.fixture
def revisions(db_session) -> ExtractedPenaltyRuleRevisionRepository:
    return ExtractedPenaltyRuleRevisionRepository(db_session)


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


def test_get_with_attributes_reads_plain_explanation_and_computed_summary_from_extra(
    repos, retailer_agreements, extracted_rules, db_session
):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "24" * 32)
    agent_run_id = _make_agent_run(db_session)
    computed_summary = {"when": "As stated in the clause.", "charge": "2% of shortfall value", "how": None}
    rule = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Supplier shall pay 2% of shortfall value.",
        clause_fingerprint="25" * 16,
        penalty_category="FILL_RATE_SHORTFALL",
        calc_type="PERCENT_OF_PO",
        pricing_readiness="READY",
        confidence=0.9,
        extra={
            "plain_explanation": "If shortfall occurs, the vendor pays 2% of the shortfall's value.",
            "computed_summary": computed_summary,
        },
    )

    result = extracted_rules.get_with_attributes(rule.id)

    assert result is not None
    assert result["plain_explanation"] == "If shortfall occurs, the vendor pays 2% of the shortfall's value."
    assert result["computed_summary"] == computed_summary


def test_get_with_attributes_returns_none_for_plain_explanation_and_computed_summary_when_absent(
    repos, retailer_agreements, extracted_rules, db_session
):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "26" * 32)
    agent_run_id = _make_agent_run(db_session)
    rule = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Supplier shall pay 2% of shortfall value.",
        clause_fingerprint="27" * 16,
        penalty_category="FILL_RATE_SHORTFALL",
        calc_type="PERCENT_OF_PO",
        pricing_readiness="READY",
        confidence=0.9,
    )

    result = extracted_rules.get_with_attributes(rule.id)

    assert result is not None
    assert result["plain_explanation"] is None
    assert result["computed_summary"] is None


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


RETAILER_AGREEMENT_ID = UUID("22222222-2222-2222-2222-222222222222")


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
        "engine_family": "SHORTAGE",
        "penalty_category": "SHORT_SHIP",
        "retailer_agreement_id": str(RETAILER_AGREEMENT_ID),
        **overrides,
    }
    return PublishedRule(**fields)


def test_published_rule_insert_kwargs_shapes_decimals_to_floats():
    kwargs = published_rule_insert_kwargs(_published_rule())

    assert kwargs == {
        "rule_code": "WMT-SHORT_SHIP-a1b2c3d4",
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
        "engine_family": "SHORTAGE",
        "penalty_category": "SHORT_SHIP",
        "retailer_agreement_id": RETAILER_AGREEMENT_ID,
        "extracted_rule_id": EXTRACTED_RULE_ID,
        "metric_code": None,
        "metric_denominator": None,
        "measurement_window_type": None,
        "measurement_window_length": None,
        "measurement_window_unit": None,
        "rounding_convention": None,
        "is_engine_priceable": True,
    }


def test_published_rule_insert_kwargs_shapes_tiers_and_a_none_cap():
    published = _published_rule(
        calc_type="TIERED",
        cap_amount=None,
        tiers=[
            PublishedTier(
                tier_code="T1",
                band_min=Decimal(0),
                band_max=Decimal("0.1"),
                rate=Decimal("0.01"),
                tier_application="CLIFF",
                tier_basis="SHORTFALL_PCT",
            ),
            PublishedTier(
                tier_code="T2",
                band_min=Decimal("0.1"),
                band_max=None,
                rate=Decimal("0.02"),
                tier_application="CLIFF",
                tier_basis="SHORTFALL_PCT",
            ),
        ],
    )

    kwargs = published_rule_insert_kwargs(published)

    assert kwargs["cap_amount"] is None
    assert kwargs["tiers"] == [
        {
            "band_min": 0.0,
            "band_max": 0.1,
            "rate": 0.01,
            "tier_application": "CLIFF",
            "tier_basis": "SHORTFALL_PCT",
        },
        {
            "band_min": 0.1,
            "band_max": None,
            "rate": 0.02,
            "tier_application": "CLIFF",
            "tier_basis": "SHORTFALL_PCT",
        },
    ]


def test_published_rule_insert_kwargs_carries_basis_currency_and_applies_per():
    # Regression test for the gap where these three fields reached the publisher's output
    # but were dropped before the insert, leaving basis_type NULL.
    published = _published_rule(basis_type="SHORTFALL_VALUE", currency_code="EUR", applies_per="DAY")

    kwargs = published_rule_insert_kwargs(published)

    assert kwargs["basis_type"] == "SHORTFALL_VALUE"
    assert kwargs["currency_code"] == "EUR"
    assert kwargs["applies_per"] == "DAY"
    assert kwargs["extracted_rule_id"] == EXTRACTED_RULE_ID


# ---------------------------------------------------------------------------
# ExtractedPenaltyRuleRepository.apply_revision
# ---------------------------------------------------------------------------


def test_apply_revision_replaces_attributes_and_resets_status_to_pending_review(
    repos, retailer_agreements, extracted_rules, db_session
):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "b1" * 32)
    agent_run_id = _make_agent_run(db_session)
    rule = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Supplier shall pay 2% of shortfall value.",
        clause_fingerprint="b1" * 16,
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
    extracted_rules.set_review_decision(rule.id, "APPROVED")

    updated = extracted_rules.apply_revision(
        rule.id,
        penalty_category="LATE_DELIVERY",
        calc_type="PER_UNIT",
        pricing_readiness="AWAITING_DATA",
        confidence=0.7,
        po_shortage_flag=False,
        po_delay_flag=True,
        review_notes="Reviewer instructed a re-read.",
        extra={"plain_explanation": "Late delivery fee."},
        attributes=[
            {
                "branch_no": 0,
                "attribute_role": "RATE",
                "value": 25.0,
                "value_unit": "USD",
                "value_status": "PRESENT",
                "basis_type": "UNIT_COST",
                "source_text": "$25 late fee.",
                "confidence": 0.8,
            }
        ],
    )

    assert updated.status == "PENDING_REVIEW"
    assert updated.penalty_category == "LATE_DELIVERY"
    assert updated.calc_type == "PER_UNIT"
    assert updated.po_delay_flag is True

    result = extracted_rules.get_with_attributes(rule.id)
    assert len(result["attributes"]) == 1
    assert result["attributes"][0].attribute_role == "RATE"
    assert float(result["attributes"][0].value) == 25.0


# ---------------------------------------------------------------------------
# ExtractedPenaltyRuleRevisionRepository
# ---------------------------------------------------------------------------


def _make_staged_rule(repos, retailer_agreements, extracted_rules, db_session, sha256: str):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, sha256)
    agent_run_id = _make_agent_run(db_session)
    rule = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Retailer may assess a $50 fee per short-shipped case.",
        clause_fingerprint=sha256[:32],
        penalty_category="SHORT_SHIP",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.9,
    )
    return retailer_agreement, rule


def test_create_numbers_revisions_sequentially_per_rule(
    repos, retailer_agreements, extracted_rules, revisions, db_session
):
    _, rule = _make_staged_rule(repos, retailer_agreements, extracted_rules, db_session, "c1" * 32)

    first = revisions.create(rule.id, instruction="Recheck the rate.", requested_by=None, before_snapshot={})
    second = revisions.create(rule.id, instruction="Recheck again.", requested_by=None, before_snapshot={})

    assert first["revision_no"] == 1
    assert second["revision_no"] == 2
    assert first["status"] == "QUEUED"


def test_latest_for_rules_returns_the_highest_revision_no_per_rule(
    repos, retailer_agreements, extracted_rules, revisions, db_session
):
    retailer_agreement = _make_retailer_agreement(repos, retailer_agreements, "c2" * 32)
    agent_run_id = _make_agent_run(db_session)
    rule_one = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Retailer may assess a $50 fee per short-shipped case.",
        clause_fingerprint="c2" * 16,
        penalty_category="SHORT_SHIP",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.9,
    )
    rule_two = extracted_rules.add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=agent_run_id,
        clause_text="Retailer may assess a late delivery fee.",
        clause_fingerprint="c3" * 16,
        penalty_category="LATE_DELIVERY",
        calc_type="PER_UNIT",
        pricing_readiness="READY",
        confidence=0.9,
    )
    revisions.create(rule_one.id, instruction="First.", requested_by=None, before_snapshot={})
    revisions.create(rule_one.id, instruction="Second.", requested_by=None, before_snapshot={})
    revisions.create(rule_two.id, instruction="Only one.", requested_by=None, before_snapshot={})

    latest = revisions.latest_for_rules([rule_one.id, rule_two.id])

    assert latest[rule_one.id]["revision_no"] == 2
    assert latest[rule_two.id]["revision_no"] == 1


def test_open_for_rule_returns_none_once_the_revision_is_terminal(
    repos, retailer_agreements, extracted_rules, revisions, db_session
):
    _, rule = _make_staged_rule(repos, retailer_agreements, extracted_rules, db_session, "c4" * 32)
    created = revisions.create(rule.id, instruction="Recheck.", requested_by=None, before_snapshot={})

    assert revisions.open_for_rule(rule.id)["id"] == created["id"]

    revisions.mark_completed(created["id"], agent_reply="Done.", after_snapshot={})

    assert revisions.open_for_rule(rule.id) is None


def test_fail_stale_flips_only_old_open_rows(
    repos, retailer_agreements, extracted_rules, revisions, db_session
):
    from app.models.penalties import ExtractedPenaltyRuleRevision

    _, rule = _make_staged_rule(repos, retailer_agreements, extracted_rules, db_session, "c5" * 32)
    stale = revisions.create(rule.id, instruction="Stale.", requested_by=None, before_snapshot={})
    fresh = revisions.create(rule.id, instruction="Fresh.", requested_by=None, before_snapshot={})

    # Backdate the stale row's `updated_at` directly on the ORM row, not through a raw
    # UPDATE: setting it explicitly here overrides `onupdate=func.now()` for this flush.
    stale_row = db_session.get(ExtractedPenaltyRuleRevision, stale["id"])
    stale_row.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
    db_session.flush()

    count = revisions.fail_stale(rule.id, older_than=datetime.now(UTC) - timedelta(minutes=10))

    assert count == 1
    assert revisions.get(stale["id"])["status"] == "FAILED"
    assert revisions.get(stale["id"])["error"] == "Revision timed out."
    assert revisions.get(fresh["id"])["status"] == "QUEUED"


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeBind:
    def __init__(self, dialect_name: str) -> None:
        self.dialect = _FakeDialect(dialect_name)


class _FakeSession:
    """Stands in for a `Session` bound to a given dialect, for `_stale_cutoff` alone."""

    def __init__(self, dialect_name: str) -> None:
        self._bind = _FakeBind(dialect_name)

    def get_bind(self):
        return self._bind


def test_stale_cutoff_strips_tzinfo_only_when_bound_to_sqlite():
    aware = datetime.now(UTC) - timedelta(minutes=10)

    sqlite_cutoff = _stale_cutoff(_FakeSession("sqlite"), aware)
    assert sqlite_cutoff.tzinfo is None
    assert sqlite_cutoff == aware.replace(tzinfo=None)


def test_stale_cutoff_binds_postgres_tz_aware_as_is():
    aware = datetime.now(UTC) - timedelta(minutes=10)

    postgres_cutoff = _stale_cutoff(_FakeSession("postgresql"), aware)
    assert postgres_cutoff is aware
    assert postgres_cutoff.tzinfo is not None


def test_stale_cutoff_leaves_an_already_naive_value_untouched_regardless_of_dialect():
    naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10)

    assert _stale_cutoff(_FakeSession("postgresql"), naive) is naive
    assert _stale_cutoff(_FakeSession("sqlite"), naive) is naive
