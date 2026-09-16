"""API tests for `penalties.penalty_projection`: running/listing/exposure,
single-projection reads, and the projection-summary trigger/poll contract.

Uses `seeded_client` (the four worked-example purchase orders, replayed
day by day) rather than building fixtures from scratch -- every purchase
order already has real projection history and, for one of them, a PO
delivery-change-request negotiation outcome.
"""

from __future__ import annotations

from datetime import date
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
def wmt_purchase_order(seeded_client) -> dict:
    purchase_orders = seeded_client.get("/api/v1/purchase-orders").json()["data"]
    return next(po for po in purchase_orders if po["purchase_order_number"] == "WMT-100234")


@pytest.fixture
def ready_projection_summary(database):
    """Directly persists a READY `penalty_summary` row for one
    `(purchase_order_id, as_of_date)`, bypassing LLM generation entirely --
    no worker drains the job queue in this test configuration (see
    `_summary_base.py`'s module docstring: `get_or_schedule` only ever
    enqueues a PENDING job here, `run_generation` is never called), so this
    is the only way to make an `?include=summary` response come back READY
    instead of the PENDING-after-trigger state the other tests in this
    module exercise. Writes through the same SQLite connection (StaticPool)
    the `client`/`seeded_client` fixtures' app reads from -- `database` is
    the same cached fixture instance those fixtures build `app` from.
    """
    from app.agents.penalties.projection.prompts.v1 import PROMPT_VERSION, SYSTEM_PROMPT
    from app.models.enums import SummaryType
    from app.repositories.penalties.summary import PenaltySummaryRepository
    from app.repositories.process.agent_registry import AgentRegistryRepository

    def _make(
        purchase_order_id: str, as_of_date: str, summary_text: str = "Test projection summary."
    ) -> None:
        with database.session() as session:
            agent_id = AgentRegistryRepository(session).ensure_registered(
                agent_code="penalty_projection_summary",
                prompt_version=PROMPT_VERSION,
                system_prompt=SYSTEM_PROMPT,
                agent_name="Penalty Projection Summary",
                domain="penalties",
            )
            PenaltySummaryRepository(session).mark_ready(
                purchase_order_id=UUID(purchase_order_id),
                summary_type=SummaryType.PROJECTION,
                as_of_date=date.fromisoformat(as_of_date),
                agent_id=agent_id,
                model_name="test-model",
                summary=summary_text,
            )

    return _make


def _create_projected_and_mitigated_purchase_order(client, repos, retailer_code: str, po_number: str) -> dict:
    """Shared setup for the combined-dashboard (`include=mitigations`/
    `mitigation_summary`) tests below: a fresh PO with one persisted
    projection and its ranked mitigation options already computed."""
    retailer = client.post(
        "/api/v1/retailers", json={"retailer_code": retailer_code, "retailer_name": "Combo Co"}
    ).json()["data"]
    retailer_agreement_id = _create_retailer_agreement(repos, retailer["id"])
    client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": f"RULE-{retailer_code}",
            "retailer_id": retailer["id"],
            "retailer_agreement_id": retailer_agreement_id,
            "penalty_category": "SHORT_SHIP",
            "violation_type": "SHORT_SHIP",
            "calc_type": "PER_UNIT",
            "rate": 1.0,
        },
    )
    po = client.post(
        "/api/v1/purchase-orders",
        json={
            "purchase_order_number": po_number,
            "retailer_id": retailer["id"],
            "order_date": "2026-01-01",
            "requested_delivery_date": "2026-01-10",
            "required_ship_date": "2026-01-08",
            "lines": [{"line_number": "10", "ordered_quantity": 100, "unit_price": 5.0}],
        },
    ).json()["data"]
    client.post("/api/v1/penalties/projections", json={"purchase_order_id": po["id"]})
    history = client.get("/api/v1/penalties/projections", params={"purchase_order_id": po["id"]}).json()[
        "data"
    ]
    client.post("/api/v1/penalties/mitigations", json={"projection_id": history[0]["id"]})
    return po


@pytest.fixture
def projected_and_mitigated_purchase_order(client, repos) -> dict:
    return _create_projected_and_mitigated_purchase_order(client, repos, "RET-COMBO", "PO-COMBO-001")


def test_list_penalty_projections_by_purchase_order_id(seeded_client, wmt_purchase_order):
    resp = seeded_client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": wmt_purchase_order["id"]}
    )

    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) > 0
    assert all(row["purchase_order_id"] == wmt_purchase_order["id"] for row in rows)
    assert all(row["summary_status"] is None for row in rows)


def test_penalty_projection_history_row_exposes_raw_amount_separately_from_combined(
    seeded_client, wmt_purchase_order
):
    """`penalty_amount` (raw $, pre-multiplication) must always
    be present on a persisted row, distinct from `expected_penalty_amount`
    (probability-weighted). Together with `failure_probability` they are the
    two decomposed components of the combined figure -- neither the manager's
    complaint nor the API contract is satisfied by the blended number alone.
    """
    resp = seeded_client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": wmt_purchase_order["id"]}
    )

    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) > 0
    for row in rows:
        assert "penalty_amount" in row
        assert isinstance(row["penalty_amount"], float)
        assert row["penalty_amount"] >= 0.0
        # The raw amount is the pre-multiplication figure -- it should never
        # be smaller than the already-probability-weighted combined amount
        # (probability is always <= 1).
        assert row["penalty_amount"] >= row["expected_penalty_amount"] - 0.01


def test_penalty_projection_list_with_include_mitigations_embeds_options_per_row(
    client, projected_and_mitigated_purchase_order
):
    resp = client.get(
        "/api/v1/penalties/projections",
        params={
            "purchase_order_id": projected_and_mitigated_purchase_order["id"],
            "include": "mitigations",
        },
    )

    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) > 0
    for row in rows:
        assert row["mitigations"] is not None
        assert len(row["mitigations"]) > 0
        assert row["mitigations"][0]["purchase_order_id"] == projected_and_mitigated_purchase_order["id"]


def test_penalty_projection_list_without_mitigations_computed_is_null_not_error(client, repos):
    retailer = client.post(
        "/api/v1/retailers", json={"retailer_code": "RET-NOMIT", "retailer_name": "No Mitigation Co"}
    ).json()["data"]
    retailer_agreement = _create_retailer_agreement(repos, retailer["id"])
    client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-NOMIT",
            "retailer_id": retailer["id"],
            "retailer_agreement_id": retailer_agreement,
            "penalty_category": "SHORT_SHIP",
            "violation_type": "SHORT_SHIP",
            "calc_type": "PER_UNIT",
            "rate": 1.0,
        },
    )
    po = client.post(
        "/api/v1/purchase-orders",
        json={
            "purchase_order_number": "PO-NOMIT-001",
            "retailer_id": retailer["id"],
            "order_date": "2026-01-01",
            "requested_delivery_date": "2026-01-10",
            "required_ship_date": "2026-01-08",
            "lines": [{"line_number": "10", "ordered_quantity": 100, "unit_price": 5.0}],
        },
    ).json()["data"]
    client.post("/api/v1/penalties/projections", json={"purchase_order_id": po["id"]})

    resp = client.get(
        "/api/v1/penalties/projections",
        params={"purchase_order_id": po["id"], "include": "mitigations"},
    )

    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) > 0
    assert all(row["mitigations"] is None for row in rows)


def test_penalty_projection_list_with_include_mitigation_summary_is_pending_after_trigger(
    client, projected_and_mitigated_purchase_order
):
    history = client.get(
        "/api/v1/penalties/projections",
        params={"purchase_order_id": projected_and_mitigated_purchase_order["id"]},
    ).json()["data"]
    as_of_date = history[0]["projection_date"]
    trigger = client.post(
        "/api/v1/penalties/mitigations/summary",
        json={"purchase_order_id": projected_and_mitigated_purchase_order["id"], "as_of_date": as_of_date},
    )
    assert trigger.status_code == 202, trigger.text

    resp = client.get(
        "/api/v1/penalties/projections",
        params={
            "purchase_order_id": projected_and_mitigated_purchase_order["id"],
            "include": "mitigation_summary",
        },
    )

    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert any(row["mitigation_summary_status"] == "PENDING" for row in rows)
    assert all(row["mitigation_summary"] is None for row in rows)


def test_penalty_projection_list_combined_include_returns_all_four(
    client, projected_and_mitigated_purchase_order
):
    resp = client.get(
        "/api/v1/penalties/projections",
        params={
            "purchase_order_id": projected_and_mitigated_purchase_order["id"],
            "include": "summary,mitigations,mitigation_summary",
        },
    )

    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) > 0
    for row in rows:
        assert "summary_status" in row
        assert "mitigations" in row
        assert "mitigation_summary_status" in row
        assert row["mitigations"] is not None


def test_penalty_projection_list_rejects_an_unknown_include_value(seeded_client, wmt_purchase_order):
    resp = seeded_client.get(
        "/api/v1/penalties/projections",
        params={"purchase_order_id": wmt_purchase_order["id"], "include": "bogus"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_INCLUDE"


def test_run_penalty_projection_include_query_param_has_no_effect(
    client, projected_and_mitigated_purchase_order
):
    """`POST /penalties/projections` no longer accepts `include=` at all --
    unlike the sibling GET routes, a compute call always returns the bare
    result with `mitigations`/`mitigation_summary_status` unset, regardless
    of what's passed as `?include=`. (`include` isn't a declared parameter
    on this route anymore, so FastAPI silently ignores the query string
    rather than rejecting it.)"""
    resp = client.post(
        "/api/v1/penalties/projections",
        json={"purchase_order_id": projected_and_mitigated_purchase_order["id"]},
        params={"include": "mitigations,mitigation_summary"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["mitigations"] is None
    assert body["mitigation_summary_status"] is None


def test_get_penalty_exposure(seeded_client, wmt_purchase_order):
    resp = seeded_client.get(
        "/api/v1/penalties/exposure", params={"purchase_order_id": wmt_purchase_order["id"]}
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["purchase_order_id"] == wmt_purchase_order["id"]
    assert "total_expected_penalty_amount" in body
    assert len(body["violations"]) > 0
    for violation in body["violations"]:
        assert "penalty_amount" in violation


def test_penalty_exposure_respects_max_stacking_mode(client, repos):
    """Regression test for a real bug: `PenaltyProjectionRepository.
    get_latest` used to hardcode `sum()` over every violation's
    `expected_penalty_amount` regardless of the retailer's actual
    `stacking_mode`, unlike `ProjectionEngine.project`/`MitigationService.
    _build_projection_result`, which both correctly branch SUM/MAX. Two
    SHORTAGE-model rules (`SHORT_SHIP`/`FILL_RATE`) share the same
    probability but price very differently (FLAT_FEE 100 vs. 900), so
    SUM and MAX give clearly different totals -- a MAX retailer's exposure
    must equal the larger single violation, not the sum of both.
    """
    retailer = client.post(
        "/api/v1/retailers",
        json={"retailer_code": "RET-MAX", "retailer_name": "Max Stacking Co", "stacking_mode": "MAX"},
    ).json()["data"]
    assert retailer["stacking_mode"] == "MAX"
    retailer_agreement = _create_retailer_agreement(repos, retailer["id"])

    client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-MAX-LOW",
            "retailer_id": retailer["id"],
            "retailer_agreement_id": retailer_agreement,
            "penalty_category": "SHORT_SHIP",
            "violation_type": "SHORT_SHIP",
            "calc_type": "FLAT_FEE",
            "rate": 100.0,
        },
    )
    client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-MAX-HIGH",
            "retailer_id": retailer["id"],
            "retailer_agreement_id": retailer_agreement,
            "penalty_category": "SHORT_SHIP",
            "violation_type": "FILL_RATE",
            "calc_type": "FLAT_FEE",
            "rate": 900.0,
        },
    )
    po = client.post(
        "/api/v1/purchase-orders",
        json={
            "purchase_order_number": "PO-MAX-STACK-001",
            "retailer_id": retailer["id"],
            "order_date": "2026-01-01",
            "requested_delivery_date": "2026-01-10",
            "required_ship_date": "2026-01-08",
            "lines": [{"line_number": "10", "ordered_quantity": 100, "unit_price": 5.0}],
        },
    ).json()["data"]
    # No confirmation posted -- the full order_qty is an unconfirmed
    # shortfall, so both FLAT_FEE rules price as nonzero and share the same
    # (nonzero) shortage probability.
    run_resp = client.post("/api/v1/penalties/projections", json={"purchase_order_id": po["id"]})
    assert run_resp.status_code == 201, run_resp.text
    violations = run_resp.json()["data"]["violations"]
    assert len(violations) == 2
    expected_by_type = {v["violation_type"]: v["expected_penalty_amount"] for v in violations}
    expected_sum = round(sum(expected_by_type.values()), 2)
    expected_max = round(max(expected_by_type.values()), 2)
    assert expected_max < expected_sum, "test setup needs two distinctly-priced violations"

    resp = client.get("/api/v1/penalties/exposure", params={"purchase_order_id": po["id"]})

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["total_expected_penalty_amount"] == expected_max
    assert body["total_expected_penalty_amount"] != expected_sum


def test_get_penalty_exposure_for_purchase_order_without_projections_returns_404(client):
    retailer = client.post(
        "/api/v1/retailers", json={"retailer_code": "RET-EXP", "retailer_name": "Exposure Co"}
    ).json()["data"]
    po = client.post(
        "/api/v1/purchase-orders",
        json={
            "purchase_order_number": "PO-NO-PROJECTION",
            "retailer_id": retailer["id"],
            "order_date": "2026-08-01",
            "lines": [{"line_number": "10", "ordered_quantity": 10, "unit_price": 1.0}],
        },
    ).json()["data"]

    resp = client.get("/api/v1/penalties/exposure", params={"purchase_order_id": po["id"]})

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NO_PROJECTION_EXISTS"


def test_get_single_projection_by_id(seeded_client, wmt_purchase_order):
    history = seeded_client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": wmt_purchase_order["id"]}
    ).json()["data"]
    projection_id = history[0]["id"]

    resp = seeded_client.get(f"/api/v1/penalties/projections/{projection_id}")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["id"] == projection_id
    assert body["purchase_order_id"] == wmt_purchase_order["id"]
    assert body["summary_status"] is None
    assert body["summary"] is None


def test_get_single_projection_with_include_summary_is_still_a_pure_read(seeded_client, wmt_purchase_order):
    """No summary has ever been requested for this PO -- `include=summary`
    must not schedule one (the `?include=` route contract: pure read,
    never schedules generation)."""
    history = seeded_client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": wmt_purchase_order["id"]}
    ).json()["data"]
    projection_id = history[0]["id"]

    resp = seeded_client.get(f"/api/v1/penalties/projections/{projection_id}", params={"include": "summary"})

    assert resp.status_code == 200
    assert resp.json()["data"]["summary_status"] is None
    assert resp.json()["data"]["summary"] is None


def test_get_single_projection_with_include_mitigations_and_mitigation_summary(
    client, projected_and_mitigated_purchase_order
):
    """The single-projection route's `include` allow-list widens to match
    the list route's (`{"summary", "mitigations", "mitigation_summary"}`)
    instead of staying narrowed to `{"summary"}`."""
    history = client.get(
        "/api/v1/penalties/projections",
        params={"purchase_order_id": projected_and_mitigated_purchase_order["id"]},
    ).json()["data"]
    projection_id = history[0]["id"]

    resp = client.get(
        f"/api/v1/penalties/projections/{projection_id}",
        params={"include": "mitigations,mitigation_summary"},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["mitigations"] is not None
    assert len(body["mitigations"]) > 0
    assert body["mitigation_summary_status"] is None


def test_get_projection_rejects_an_unknown_include_value(seeded_client, wmt_purchase_order):
    history = seeded_client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": wmt_purchase_order["id"]}
    ).json()["data"]
    projection_id = history[0]["id"]

    resp = seeded_client.get(f"/api/v1/penalties/projections/{projection_id}", params={"include": "bogus"})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_INCLUDE"


def test_get_unknown_projection_returns_404(seeded_client):
    resp = seeded_client.get("/api/v1/penalties/projections/00000000-0000-0000-0000-000000000000")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PROJECTION_NOT_FOUND"


def test_list_open_penalty_projections_is_flat_and_cross_po(seeded_client, wmt_purchase_order):
    resp = seeded_client.get("/api/v1/penalties/projections", params={"status": "open"})

    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) > 0
    assert all(row["projection_status"] == "OPEN" for row in rows)
    assert any(row["purchase_order_id"] == wmt_purchase_order["id"] for row in rows)


def test_list_penalty_projections_with_no_filters_defaults_to_cross_po_open(
    seeded_client, wmt_purchase_order
):
    """No `purchase_order_id` and no `status` -- must default to the same
    cross-PO `status=OPEN` list the explicit-`status` case above returns,
    matching the old bare `GET /penalty-projections`'s own default."""
    resp = seeded_client.get("/api/v1/penalties/projections")

    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) > 0
    assert all(row["projection_status"] == "OPEN" for row in rows)
    assert any(row["purchase_order_id"] == wmt_purchase_order["id"] for row in rows)


def test_run_penalty_projection_for_a_purchase_order(seeded_client, wmt_purchase_order):
    history_before = seeded_client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": wmt_purchase_order["id"]}
    ).json()["data"]
    an_existing_date = history_before[0]["projection_date"]

    resp = seeded_client.post(
        "/api/v1/penalties/projections",
        json={"purchase_order_id": wmt_purchase_order["id"], "projection_date": an_existing_date},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["purchase_order_id"] == wmt_purchase_order["id"]
    assert body["projection_date"] == an_existing_date
    assert "total_expected_penalty_amount" in body


def test_run_penalty_projection_without_include_has_no_summary_fields(seeded_client, wmt_purchase_order):
    """Regression: the default (no `?include=`) POST response shape must
    stay unchanged now that `summary_status`/`summary`/`mitigations`/
    `mitigation_summary_status`/`mitigation_summary` all exist on
    `PenaltyProjectionResultResponse`."""
    history_before = seeded_client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": wmt_purchase_order["id"]}
    ).json()["data"]
    an_existing_date = history_before[0]["projection_date"]

    resp = seeded_client.post(
        "/api/v1/penalties/projections",
        json={"purchase_order_id": wmt_purchase_order["id"], "projection_date": an_existing_date},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["summary_status"] is None
    assert body["summary"] is None
    assert body["mitigations"] is None
    assert body["mitigation_summary_status"] is None
    assert body["mitigation_summary"] is None


def test_run_penalty_projection_with_include_summary_query_param_is_ignored(
    seeded_client, wmt_purchase_order
):
    """`POST /penalties/projections` no longer accepts `include=` -- passing
    `?include=summary` neither schedules a summary job nor changes the
    response shape; `summary_status`/`summary` stay unset exactly as the
    no-`include=` case does."""
    history_before = seeded_client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": wmt_purchase_order["id"]}
    ).json()["data"]
    an_existing_date = history_before[0]["projection_date"]

    resp = seeded_client.post(
        "/api/v1/penalties/projections",
        json={"purchase_order_id": wmt_purchase_order["id"], "projection_date": an_existing_date},
        params={"include": "summary"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["summary_status"] is None
    assert body["summary"] is None


def test_run_penalty_projection_with_include_summary_query_param_ignored_even_when_ready(
    client, repos, ready_projection_summary
):
    """Mirrors `test_run_penalty_projection_with_include_summary_query_param_is_ignored`,
    but with a READY summary already on hand -- `POST /penalties/projections`
    still never attaches it, `include=` or not, since the route no longer
    accepts that parameter at all."""
    retailer = client.post(
        "/api/v1/retailers", json={"retailer_code": "RET-SUM-READY", "retailer_name": "Summary Ready Co"}
    ).json()["data"]
    retailer_agreement = _create_retailer_agreement(repos, retailer["id"])
    client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-SUM-READY",
            "retailer_id": retailer["id"],
            "retailer_agreement_id": retailer_agreement,
            "penalty_category": "SHORT_SHIP",
            "violation_type": "SHORT_SHIP",
            "calc_type": "PER_UNIT",
            "rate": 1.0,
        },
    )
    po = client.post(
        "/api/v1/purchase-orders",
        json={
            "purchase_order_number": "PO-SUMMARY-READY-001",
            "retailer_id": retailer["id"],
            "order_date": "2026-01-01",
            "requested_delivery_date": "2026-01-10",
            "required_ship_date": "2026-01-08",
            "lines": [{"line_number": "10", "ordered_quantity": 100, "unit_price": 5.0}],
        },
    ).json()["data"]
    first_run = client.post("/api/v1/penalties/projections", json={"purchase_order_id": po["id"]})
    assert first_run.status_code == 201, first_run.text
    as_of_date = first_run.json()["data"]["projection_date"]

    ready_projection_summary(po["id"], as_of_date, summary_text="Everything is on track.")

    resp = client.post(
        "/api/v1/penalties/projections",
        json={"purchase_order_id": po["id"], "projection_date": as_of_date},
        params={"include": "summary"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["summary_status"] is None
    assert body["summary"] is None


def test_run_penalty_projection_response_includes_each_violations_projection_id(
    seeded_client, wmt_purchase_order
):
    """The confirmed gap this covers: `POST /penalties/projections` computes
    and persists one `penalty_projection` row per violation, but historically
    returned no id for any of them -- a client had no way to call
    `GET /penalties/projections/{projection_id}` or
    `POST /penalties/mitigations` (`projection_id` in the body) against this
    run's own output without a separate list/query round-trip. Every
    violation must carry its own `projection_id`, matching the id of the row
    `save_result` actually persisted for it (never an invented/derived
    value)."""
    history_before = seeded_client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": wmt_purchase_order["id"]}
    ).json()["data"]
    an_existing_date = history_before[0]["projection_date"]

    resp = seeded_client.post(
        "/api/v1/penalties/projections",
        json={"purchase_order_id": wmt_purchase_order["id"], "projection_date": an_existing_date},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert len(body["violations"]) > 0

    history_after = seeded_client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": wmt_purchase_order["id"]}
    ).json()["data"]
    persisted_ids_by_rule_and_date = {
        (row["rule_id"], row["projection_date"]): row["id"] for row in history_after
    }

    for violation in body["violations"]:
        assert violation["projection_id"] is not None
        # Sourced from the row actually persisted for this (rule, date) --
        # not invented -- and independently resolvable via the GET-by-id
        # route.
        assert (
            violation["projection_id"]
            == persisted_ids_by_rule_and_date[(violation["rule_id"], an_existing_date)]
        )
        get_resp = seeded_client.get(f"/api/v1/penalties/projections/{violation['projection_id']}")
        assert get_resp.status_code == 200
        assert get_resp.json()["data"]["violation_type"] == violation["violation_type"]


def test_trigger_penalty_projection_summary_queues_a_job(client, repos):
    """Built fresh (not from `seeded_client`): every worked-example
    scenario's projection dates are forward-looking by design (order_date
    is anchored at real "today", so every tracked day is in the future
    relative to it -- see `app.services.seeding.projection`'s module
    docstring), and `ProjectionSummaryService._validate` rejects a future
    `as_of_date`. This purchase order's projection is run for real "today"
    instead, so its `as_of_date` is always valid."""
    retailer = client.post(
        "/api/v1/retailers", json={"retailer_code": "RET-SUM", "retailer_name": "Summary Co"}
    ).json()["data"]
    retailer_agreement = _create_retailer_agreement(repos, retailer["id"])
    client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-SUM",
            "retailer_id": retailer["id"],
            "retailer_agreement_id": retailer_agreement,
            "penalty_category": "SHORT_SHIP",
            "violation_type": "SHORT_SHIP",
            "calc_type": "PER_UNIT",
            "rate": 1.0,
        },
    )
    po = client.post(
        "/api/v1/purchase-orders",
        json={
            "purchase_order_number": "PO-SUMMARY-001",
            "retailer_id": retailer["id"],
            "order_date": "2026-01-01",
            "requested_delivery_date": "2026-01-10",
            "required_ship_date": "2026-01-08",
            "lines": [{"line_number": "10", "ordered_quantity": 100, "unit_price": 5.0}],
        },
    ).json()["data"]
    client.post("/api/v1/penalties/projections", json={"purchase_order_id": po["id"]})
    history = client.get("/api/v1/penalties/projections", params={"purchase_order_id": po["id"]}).json()[
        "data"
    ]
    as_of_date = history[0]["projection_date"]

    resp = client.post(
        "/api/v1/penalties/projections/summary",
        json={"purchase_order_id": po["id"], "as_of_date": as_of_date},
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()["data"]
    assert body["status"] == "PENDING"
    assert body["summary"] is None

    # `include=summary` now sees the PENDING job -- still a pure read.
    projection_id = history[0]["id"]
    detail = client.get(
        f"/api/v1/penalties/projections/{projection_id}", params={"include": "summary"}
    ).json()["data"]
    assert detail["summary_status"] == "PENDING"

    # The new dedicated GET-summary route sees the same PENDING job.
    summary_resp = client.get(
        "/api/v1/penalties/projections/summary",
        params={"purchase_order_id": po["id"], "as_of_date": as_of_date},
    )
    assert summary_resp.status_code == 200
    summary_body = summary_resp.json()["data"]
    assert summary_body["status"] == "PENDING"
    assert summary_body["summary"] is None


def test_get_penalty_projection_summary_returns_ready_summary_when_one_exists(
    client, repos, ready_projection_summary
):
    retailer = client.post(
        "/api/v1/retailers", json={"retailer_code": "RET-SUM-GET", "retailer_name": "Summary Get Co"}
    ).json()["data"]
    retailer_agreement = _create_retailer_agreement(repos, retailer["id"])
    client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-SUM-GET",
            "retailer_id": retailer["id"],
            "retailer_agreement_id": retailer_agreement,
            "penalty_category": "SHORT_SHIP",
            "violation_type": "SHORT_SHIP",
            "calc_type": "PER_UNIT",
            "rate": 1.0,
        },
    )
    po = client.post(
        "/api/v1/purchase-orders",
        json={
            "purchase_order_number": "PO-SUMMARY-GET-001",
            "retailer_id": retailer["id"],
            "order_date": "2026-01-01",
            "requested_delivery_date": "2026-01-10",
            "required_ship_date": "2026-01-08",
            "lines": [{"line_number": "10", "ordered_quantity": 100, "unit_price": 5.0}],
        },
    ).json()["data"]
    first_run = client.post("/api/v1/penalties/projections", json={"purchase_order_id": po["id"]})
    as_of_date = first_run.json()["data"]["projection_date"]
    ready_projection_summary(po["id"], as_of_date, summary_text="All clear via GET.")

    resp = client.get(
        "/api/v1/penalties/projections/summary",
        params={"purchase_order_id": po["id"], "as_of_date": as_of_date},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "READY"
    assert body["summary"]["summary"] == "All clear via GET."


def test_get_penalty_projection_summary_is_null_success_when_none_requested_yet(client, repos):
    retailer = client.post(
        "/api/v1/retailers", json={"retailer_code": "RET-SUM-404", "retailer_name": "Summary 404 Co"}
    ).json()["data"]
    retailer_agreement = _create_retailer_agreement(repos, retailer["id"])
    client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-SUM-404",
            "retailer_id": retailer["id"],
            "retailer_agreement_id": retailer_agreement,
            "penalty_category": "SHORT_SHIP",
            "violation_type": "SHORT_SHIP",
            "calc_type": "PER_UNIT",
            "rate": 1.0,
        },
    )
    po = client.post(
        "/api/v1/purchase-orders",
        json={
            "purchase_order_number": "PO-SUMMARY-404-001",
            "retailer_id": retailer["id"],
            "order_date": "2026-01-01",
            "requested_delivery_date": "2026-01-10",
            "required_ship_date": "2026-01-08",
            "lines": [{"line_number": "10", "ordered_quantity": 100, "unit_price": 5.0}],
        },
    ).json()["data"]
    run_resp = client.post("/api/v1/penalties/projections", json={"purchase_order_id": po["id"]})
    as_of_date = run_resp.json()["data"]["projection_date"]

    resp = client.get(
        "/api/v1/penalties/projections/summary",
        params={"purchase_order_id": po["id"], "as_of_date": as_of_date},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["status"] is None
    assert body["data"]["summary"] is None


def test_get_penalty_projection_summary_still_404s_for_an_unknown_purchase_order(client):
    """The convergence to a null success only applies to "no job exists
    yet for a real PO" -- an unknown `purchase_order_id` is still a genuine
    404, same `PO_NOT_FOUND` convention as everywhere else."""
    resp = client.get(
        "/api/v1/penalties/projections/summary",
        params={"purchase_order_id": "00000000-0000-0000-0000-000000000000"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PO_NOT_FOUND"
