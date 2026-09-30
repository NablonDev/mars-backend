"""Unit tests for `TimelineMasterData`'s agreement/rule get-or-create bug fixes."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.seeding.timeline.master_data import TimelineMasterData

AS_OF = date(2026, 6, 30)


@pytest.fixture
def master_data(repos) -> TimelineMasterData:
    return TimelineMasterData(
        master_data=repos.master_data,
        retailer_agreements=repos.retailer_agreements,
        rules=repos.penalty_rules,
    )


def test_two_retailers_get_distinct_agreements_with_distinct_sha256(master_data: TimelineMasterData) -> None:
    master_data.setup(AS_OF)

    amazon_agreement = master_data.retailer_agreements.get_by_contract_code("TL-AMAZON-AGREEMENT")
    walmart_agreement = master_data.retailer_agreements.get_by_contract_code("TL-WALMART-AGREEMENT")

    assert amazon_agreement is not None
    assert walmart_agreement is not None
    assert amazon_agreement["id"] != walmart_agreement["id"]
    assert amazon_agreement["document_sha256"] != walmart_agreement["document_sha256"]
    assert amazon_agreement["document_sha256"] != "0" * 64


def test_setup_is_idempotent(master_data: TimelineMasterData) -> None:
    master_data.setup(AS_OF)
    first_agreement = master_data.retailer_agreements.get_by_contract_code("TL-AMAZON-AGREEMENT")
    first_rule = master_data.rules.get_by_rule_code("TL-RULE-AMZ-OTIF")
    assert first_agreement is not None
    assert first_rule is not None

    master_data.setup(AS_OF)

    second_agreement = master_data.retailer_agreements.get_by_contract_code("TL-AMAZON-AGREEMENT")
    assert second_agreement is not None
    assert second_agreement["id"] == first_agreement["id"]
    assert len(master_data.retailer_agreements.list_for_retailer(master_data.retailer_ids["TL-AMAZON"])) == 1


def test_reused_retailer_with_real_agreement_keeps_it_and_gets_no_synthetic_rules(
    repos, master_data: TimelineMasterData
) -> None:
    retailer = repos.master_data.add_retailer("RET-AMAZON-REAL", "Amazon", "TIER_1", "SUM")
    real_agreement = repos.retailer_agreements.add_retailer_agreement(
        retailer_id=retailer["id"],
        contract_code="AMZN-REAL-2026",
        title="Amazon real vendor agreement",
        document_sha256="1" * 64,
        effective_date=date(2020, 1, 1),
    )
    repos.penalty_rules.add_rule(
        rule_code="REAL-RULE-AMZ-OTIF",
        violation_type="OTIF_LATE",
        calc_type="PERCENT_OF_PO",
        rate=0.05,
        penalty_category="LATE",
        retailer_agreement_id=real_agreement["id"],
        effective_start_date=date(2020, 1, 1),
        is_engine_priceable=True,
    )

    master_data.setup(AS_OF)

    unchanged_agreement = repos.retailer_agreements.get(real_agreement["id"])
    assert unchanged_agreement is not None
    assert unchanged_agreement["contract_code"] == "AMZN-REAL-2026"

    assert repos.penalty_rules.get_by_rule_code("TL-RULE-AMZ-OTIF") is None
    assert repos.penalty_rules.get_by_rule_code("TL-RULE-AMZ-SHORT") is None

    seeder_agreement = repos.retailer_agreements.get_by_contract_code("TL-AMAZON-AGREEMENT")
    assert seeder_agreement is not None
    assert seeder_agreement["id"] != real_agreement["id"]
