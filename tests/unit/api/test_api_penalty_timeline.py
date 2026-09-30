"""API tests for the fulfillment-timeline projection engine: runs, risks, and plan reads.

Builds its own minimal scenarios via the repository layer (mirroring
`tests/unit/services/penalties/timeline/test_service.py`'s `_build_plan`)
rather than `seeded_client`, since these routes need direct control over
which plans breach and on which projection date to exercise ordering and
the plan-detail projected-milestone merge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid4

import pytest

from app.repositories.common.fulfillment_timeline import FulfillmentTimelineRepository
from app.repositories.penalties.fulfillment_risk import FulfillmentRiskRepository
from scripts.seed.seed_milestone_types import seed_milestone_types
from tests.conftest import make_retailer_agreement


@dataclass
class _Scenario:
    plan_id: UUID
    purchase_order_id: UUID
    retailer_id: UUID


@pytest.fixture
def timeline_repos(repos, db_session):
    seed_milestone_types(db_session)
    return {
        "timeline": FulfillmentTimelineRepository(db_session),
        "risks": FulfillmentRiskRepository(db_session),
    }


def _build_plan(
    repos,
    timeline: FulfillmentTimelineRepository,
    *,
    tag: str,
    window_start: date,
    window_end: date,
    planned_quantity: float = 100.0,
) -> _Scenario:
    retailer = repos.master_data.add_retailer(f"RET-{tag}", f"Retailer {tag}", None, "SUM")
    plant = repos.master_data.add_plant(f"PLANT-{tag}")
    material = repos.master_data.add_material(f"MAT-{tag}")
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number=f"PO-{tag}",
        retailer_id=retailer["id"],
        order_date=date(2026, 1, 1),
        window_start=window_start,
        window_end=window_end,
        cancel_date=None,
        freight_term="PREPAID",
    )
    po_line = repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=planned_quantity,
        unit_price=20.0,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    plan = timeline.create_plan(
        plan_number=f"PLAN-{tag}",
        purchase_order_id=purchase_order["id"],
        freight_term="PREPAID",
        planned_transit_days=2,
    )
    timeline.add_plan_line(plan["id"], po_line["id"], planned_quantity=planned_quantity)
    return _Scenario(plan_id=plan["id"], purchase_order_id=purchase_order["id"], retailer_id=retailer["id"])


def _add_late_rule(repos, retailer_id: UUID, rule_code: str, rate: float = 2.0) -> None:
    agreement_id = make_retailer_agreement(repos, retailer_id)
    repos.penalty_rules.add_rule(
        rule_code=rule_code,
        violation_type="OTIF_LATE",
        penalty_category="LATE",
        retailer_agreement_id=agreement_id,
        calc_type="PER_UNIT",
        rate=rate,
    )


def test_run_penalty_timeline_projects_an_on_track_plan_with_no_risk_rows(client, repos, timeline_repos):
    scenario = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="ONTRACK",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 10),
    )
    timeline_repos["timeline"].upsert_milestone(
        scenario.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 3)
    )

    resp = client.post("/api/v1/penalties/timeline/runs", json={"projection_date": "2026-01-02"})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["plans_evaluated"] >= 1
    assert sum(data["status_counts"].values()) == data["plans_evaluated"]
    assert data["total_projected_penalty"] == 0.0


def test_run_penalty_timeline_skips_a_plan_with_no_delivery_window(client, repos, timeline_repos):
    retailer = repos.master_data.add_retailer("RET-NOWIN", "Retailer NOWIN", None, "SUM")
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="PO-NOWIN",
        retailer_id=retailer["id"],
        order_date=date(2026, 1, 1),
        window_start=None,
        window_end=None,
        cancel_date=None,
        freight_term="PREPAID",
    )
    plan = timeline_repos["timeline"].create_plan(
        plan_number="PLAN-NOWIN", purchase_order_id=purchase_order["id"], freight_term="PREPAID"
    )

    resp = client.post("/api/v1/penalties/timeline/runs", json={"projection_date": "2026-01-02"})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert str(plan["id"]) in data["skipped_plan_ids"]
    assert data["plans_skipped"] >= 1


def test_list_fulfillment_risks_orders_highest_penalty_first_even_when_older(client, repos, timeline_repos):
    """Controller ruling: `projected_penalty_amount desc` is the primary sort key, `projection_date
    desc` only breaks ties -- proven here by giving the higher-penalty plan the *older* projection
    date, so a date-primary sort would (wrongly) rank it second.
    """
    high_penalty = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="HIGHPENALTY",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
    )
    _add_late_rule(repos, high_penalty.retailer_id, "RULE-HIGHPENALTY", rate=5.0)
    timeline_repos["timeline"].upsert_milestone(
        high_penalty.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 10)
    )
    client.post("/api/v1/penalties/timeline/runs", json={"projection_date": "2026-01-02"})
    # Excluded from the next run_for_all_open below, so its only risk row stays on 2026-01-02
    # (run_for_all_open reprojects every OPEN/SHIPPED plan each call, not just newly-changed ones).
    timeline_repos["timeline"].set_plan_status(high_penalty.plan_id, "DELIVERED")

    low_penalty = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="LOWPENALTY",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
    )
    _add_late_rule(repos, low_penalty.retailer_id, "RULE-LOWPENALTY", rate=1.0)
    timeline_repos["timeline"].upsert_milestone(
        low_penalty.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 10)
    )
    client.post("/api/v1/penalties/timeline/runs", json={"projection_date": "2026-01-03"})

    resp = client.get("/api/v1/penalties/timeline/risks")

    assert resp.status_code == 200
    rows = resp.json()["data"]
    ids = [(row["fulfillment_plan_id"], row["projection_date"]) for row in rows]
    assert (str(high_penalty.plan_id), "2026-01-02") in ids
    assert (str(low_penalty.plan_id), "2026-01-03") in ids
    high_index = ids.index((str(high_penalty.plan_id), "2026-01-02"))
    low_index = ids.index((str(low_penalty.plan_id), "2026-01-03"))
    assert high_index < low_index
    assert rows[high_index]["projected_penalty_amount"] > rows[low_index]["projected_penalty_amount"]


def test_list_fulfillment_risks_orders_newest_date_first_as_tiebreaker_on_equal_penalty(
    client, repos, timeline_repos
):
    same_rate_older = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="TIEOLDER",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
    )
    _add_late_rule(repos, same_rate_older.retailer_id, "RULE-TIEOLDER", rate=2.0)
    timeline_repos["timeline"].upsert_milestone(
        same_rate_older.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 10)
    )
    client.post("/api/v1/penalties/timeline/runs", json={"projection_date": "2026-01-02"})
    timeline_repos["timeline"].set_plan_status(same_rate_older.plan_id, "DELIVERED")

    same_rate_newer = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="TIENEWER",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
    )
    _add_late_rule(repos, same_rate_newer.retailer_id, "RULE-TIENEWER", rate=2.0)
    timeline_repos["timeline"].upsert_milestone(
        same_rate_newer.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 10)
    )
    client.post("/api/v1/penalties/timeline/runs", json={"projection_date": "2026-01-03"})

    resp = client.get("/api/v1/penalties/timeline/risks")

    assert resp.status_code == 200
    rows = resp.json()["data"]
    ids = [(row["fulfillment_plan_id"], row["projection_date"]) for row in rows]
    newer_index = ids.index((str(same_rate_newer.plan_id), "2026-01-03"))
    older_index = ids.index((str(same_rate_older.plan_id), "2026-01-02"))
    assert rows[newer_index]["projected_penalty_amount"] == rows[older_index]["projected_penalty_amount"]
    assert newer_index < older_index


def test_list_fulfillment_risks_filters_by_status_and_retailer(client, repos, timeline_repos):
    scenario = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="FILTER",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
    )
    _add_late_rule(repos, scenario.retailer_id, "RULE-FILTER")
    timeline_repos["timeline"].upsert_milestone(
        scenario.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 10)
    )
    client.post("/api/v1/penalties/timeline/runs", json={"projection_date": "2026-01-03"})

    resp = client.get(
        "/api/v1/penalties/timeline/risks",
        params={"status": "PROJECTED_BREACH", "retailer_id": str(scenario.retailer_id)},
    )

    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert rows
    assert all(row["status"] == "PROJECTED_BREACH" for row in rows)
    assert all(row["purchase_order_id"] == str(scenario.purchase_order_id) for row in rows)

    resp_other_retailer = client.get(
        "/api/v1/penalties/timeline/risks",
        params={"status": "PROJECTED_BREACH", "retailer_id": str(uuid4())},
    )
    assert resp_other_retailer.status_code == 200
    assert resp_other_retailer.json()["data"] == []


def test_get_fulfillment_plan_returns_projected_milestones_risks_and_options(client, repos, timeline_repos):
    scenario = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="DETAIL",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
    )
    _add_late_rule(repos, scenario.retailer_id, "RULE-DETAIL")
    timeline_repos["timeline"].upsert_milestone(
        scenario.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 10)
    )
    run_resp = client.post("/api/v1/penalties/timeline/runs", json={"projection_date": "2026-01-03"})
    assert run_resp.status_code == 200

    resp = client.get(f"/api/v1/penalties/timeline/plans/{scenario.plan_id}")

    assert resp.status_code == 200
    plan = resp.json()["data"]
    assert plan["id"] == str(scenario.plan_id)
    assert plan["purchase_order_id"] == str(scenario.purchase_order_id)
    assert len(plan["lines"]) == 1
    assert plan["risks"], "expected at least one persisted risk row on the latest projection date"
    assert all(row["projection_date"] == "2026-01-03" for row in plan["risks"])
    assert plan["mitigation_options"]

    picked = next(m for m in plan["milestones"] if m["code"] == "PICKED")
    assert picked["baseline_date"] == "2026-01-03"
    assert picked["planned_date"] == "2026-01-10"
    assert picked["projected_date"] is not None


def test_get_fulfillment_plan_falls_back_to_planned_date_before_any_projection_run(
    client, repos, timeline_repos
):
    scenario = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="NORUN",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 10),
    )
    timeline_repos["timeline"].upsert_milestone(
        scenario.plan_id, "PICKED", baseline_date=date(2026, 1, 3), planned_date=date(2026, 1, 4)
    )

    resp = client.get(f"/api/v1/penalties/timeline/plans/{scenario.plan_id}")

    assert resp.status_code == 200
    plan = resp.json()["data"]
    assert plan["risks"] == []
    assert plan["mitigation_options"] == []
    picked = next(m for m in plan["milestones"] if m["code"] == "PICKED")
    assert picked["projected_date"] == picked["planned_date"] == "2026-01-04"


def test_get_fulfillment_plan_404_for_unknown_plan(client):
    resp = client.get(f"/api/v1/penalties/timeline/plans/{uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "FULFILLMENT_PLAN_NOT_FOUND"


def test_list_fulfillment_plans_for_purchase_order(client, repos, timeline_repos):
    scenario = _build_plan(
        repos,
        timeline_repos["timeline"],
        tag="POPLANS",
        window_start=date(2026, 1, 1),
        window_end=date(2026, 1, 6),
    )

    resp = client.get(f"/api/v1/purchase-orders/{scenario.purchase_order_id}/fulfillment-plans")

    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) == 1
    assert rows[0]["id"] == str(scenario.plan_id)
    assert rows[0]["purchase_order_id"] == str(scenario.purchase_order_id)


def test_list_fulfillment_plans_for_purchase_order_404_for_unknown_po(client):
    resp = client.get(f"/api/v1/purchase-orders/{uuid4()}/fulfillment-plans")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PO_NOT_FOUND"
