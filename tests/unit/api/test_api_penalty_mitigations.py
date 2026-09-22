"""API tests for `penalties.mitigation_option`: compute/list ranked
mitigation options for a projection, and the mitigation-summary
trigger/poll contract."""

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
def projected_purchase_order(client, repos) -> dict:
    """A fresh PO with one persisted OPEN penalty projection, anchored on
    real "today" (see test_api_penalty_projections.py's summary-trigger
    test for why the seeded scenario's forward-looking dates don't work
    for this)."""
    retailer = client.post(
        "/api/v1/retailers", json={"retailer_code": "RET-MIT", "retailer_name": "Mitigation Co"}
    ).json()["data"]
    retailer_agreement = _create_retailer_agreement(repos, retailer["id"])
    client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-MIT",
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
            "purchase_order_number": "PO-MITIGATION-001",
            "retailer_id": retailer["id"],
            "order_date": "2026-01-01",
            "requested_delivery_date": "2026-01-10",
            "required_ship_date": "2026-01-08",
            "lines": [{"line_number": "10", "ordered_quantity": 100, "unit_price": 5.0}],
        },
    ).json()["data"]
    client.post("/api/v1/penalties/projections", json={"purchase_order_id": po["id"]})
    return po


@pytest.fixture
def projection_id(client, projected_purchase_order) -> str:
    history = client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": projected_purchase_order["id"]}
    ).json()["data"]
    return history[0]["id"]


@pytest.fixture
def ready_mitigation_summary(database):
    """Directly persists a READY `penalty_summary` row for one
    `(purchase_order_id, as_of_date)`, bypassing LLM generation entirely --
    see `test_api_penalty_projections.py`'s sibling fixture for why this is
    the only way an API test can observe a genuine READY `?include=summary`
    response in this test configuration (no worker drains the job queue,
    the default `get_llm_client` override raises on any call)."""
    from app.agents.penalties.mitigation.prompts.v2 import PROMPT_VERSION, SYSTEM_PROMPT
    from app.models.enums import SummaryType
    from app.repositories.penalties.summary import PenaltySummaryRepository
    from app.repositories.process.agent_registry import AgentRegistryRepository

    def _make(
        purchase_order_id: str, as_of_date: str, summary_text: str = "Test mitigation summary."
    ) -> None:
        with database.session() as session:
            agent_id = AgentRegistryRepository(session).ensure_registered(
                agent_code="penalty_mitigation_summary",
                prompt_version=PROMPT_VERSION,
                system_prompt=SYSTEM_PROMPT,
                agent_name="Penalty Mitigation Summary",
                domain="penalties",
            )
            PenaltySummaryRepository(session).mark_ready(
                purchase_order_id=UUID(purchase_order_id),
                summary_type=SummaryType.MITIGATION,
                as_of_date=date.fromisoformat(as_of_date),
                agent_id=agent_id,
                model_name="test-model",
                summary=summary_text,
            )

    return _make


def test_run_and_list_penalty_mitigations_via_projection_id(client, projection_id):
    resp = client.post("/api/v1/penalties/mitigations", json={"projection_id": projection_id})

    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert len(body["options"]) > 0

    listed = client.get("/api/v1/penalties/mitigations", params={"projection_id": projection_id}).json()[
        "data"
    ]
    assert len(listed["options"]) == len(body["options"])


def test_run_and_list_penalty_mitigations_via_direct_purchase_order_and_date(
    client, projected_purchase_order, projection_id
):
    """The new direct shape -- no `projection_id` round-trip needed when the
    caller already knows `(purchase_order_id, projection_date)`."""
    history = client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": projected_purchase_order["id"]}
    ).json()["data"]
    projection_date = history[0]["projection_date"]

    resp = client.post(
        "/api/v1/penalties/mitigations",
        json={"purchase_order_id": projected_purchase_order["id"], "projection_date": projection_date},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["purchase_order_id"] == projected_purchase_order["id"]
    assert body["projection_date"] == projection_date
    assert len(body["options"]) > 0

    listed = client.get(
        "/api/v1/penalties/mitigations",
        params={"purchase_order_id": projected_purchase_order["id"], "projection_date": projection_date},
    ).json()["data"]
    assert len(listed["options"]) == len(body["options"])


def test_run_penalty_mitigations_rejects_both_shapes_provided(
    client, projected_purchase_order, projection_id
):
    resp = client.post(
        "/api/v1/penalties/mitigations",
        json={
            "projection_id": projection_id,
            "purchase_order_id": projected_purchase_order["id"],
            "projection_date": "2026-01-01",
        },
    )

    assert resp.status_code == 422, resp.text


def test_run_penalty_mitigations_rejects_neither_shape_provided(client):
    resp = client.post("/api/v1/penalties/mitigations", json={})

    assert resp.status_code == 422, resp.text


def test_run_penalty_mitigations_rejects_partial_direct_pair(client, projected_purchase_order):
    resp = client.post(
        "/api/v1/penalties/mitigations",
        json={"purchase_order_id": projected_purchase_order["id"]},
    )

    assert resp.status_code == 422, resp.text


def test_list_penalty_mitigations_rejects_both_shapes_provided(
    client, projected_purchase_order, projection_id
):
    resp = client.get(
        "/api/v1/penalties/mitigations",
        params={
            "projection_id": projection_id,
            "purchase_order_id": projected_purchase_order["id"],
            "projection_date": "2026-01-01",
        },
    )

    assert resp.status_code == 422, resp.text


def test_list_penalty_mitigations_rejects_neither_shape_provided(client):
    resp = client.get("/api/v1/penalties/mitigations")

    assert resp.status_code == 422, resp.text


def test_run_penalty_mitigations_without_include_has_no_summary_fields(client, projection_id):
    """Regression: the default (no `?include=`) POST response shape must
    stay unchanged now that `summary_status`/`summary` are wired up for
    this route."""
    resp = client.post("/api/v1/penalties/mitigations", json={"projection_id": projection_id})

    assert resp.status_code == 201, resp.text
    options = resp.json()["data"]["options"]
    assert len(options) > 0
    assert all(o["summary_status"] is None and o["summary"] is None for o in options)


def test_run_penalty_mitigations_with_include_summary_query_param_is_ignored(client, projection_id):
    """`POST /penalties/mitigations` no longer accepts `include=` -- passing
    `?include=summary` neither schedules a mitigation-summary job nor
    changes the response shape; `summary_status`/`summary` stay unset
    exactly as the no-`include=` case does."""
    resp = client.post(
        "/api/v1/penalties/mitigations",
        json={"projection_id": projection_id},
        params={"include": "summary"},
    )

    assert resp.status_code == 201, resp.text
    options = resp.json()["data"]["options"]
    assert len(options) > 0
    assert all(o["summary_status"] is None and o["summary"] is None for o in options)


def test_run_penalty_mitigations_with_include_summary_query_param_ignored_even_when_ready(
    client, projected_purchase_order, projection_id, ready_mitigation_summary
):
    """Mirrors `test_run_penalty_mitigations_with_include_summary_query_param_is_ignored`,
    but with a READY mitigation summary already on hand -- `POST
    /penalties/mitigations` still never attaches it, `include=` or not,
    since the route no longer accepts that parameter at all."""
    first_run = client.post("/api/v1/penalties/mitigations", json={"projection_id": projection_id})
    assert first_run.status_code == 201, first_run.text
    projection_date = first_run.json()["data"]["projection_date"]

    ready_mitigation_summary(
        projected_purchase_order["id"], projection_date, summary_text="Mitigations look solid."
    )

    resp = client.post(
        "/api/v1/penalties/mitigations",
        json={"projection_id": projection_id},
        params={"include": "summary"},
    )

    assert resp.status_code == 201, resp.text
    options = resp.json()["data"]["options"]
    assert len(options) > 0
    for option in options:
        assert option["summary_status"] is None
        assert option["summary"] is None


def test_list_penalty_mitigations_against_unknown_projection_returns_404(client):
    resp = client.get(
        "/api/v1/penalties/mitigations",
        params={"projection_id": "00000000-0000-0000-0000-000000000000"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PROJECTION_NOT_FOUND"


def test_list_penalty_mitigations_against_unknown_purchase_order_returns_404(client):
    """Real bug this guards against: an unknown `purchase_order_id` used to
    fall straight through to an empty `options: []`, `success: true` --
    indistinguishable from a real PO with no mitigations computed yet."""
    resp = client.get(
        "/api/v1/penalties/mitigations",
        params={
            "purchase_order_id": "00000000-0000-0000-0000-000000000000",
            "projection_date": "2026-01-01",
        },
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PO_NOT_FOUND"


def test_list_penalty_mitigations_for_real_purchase_order_with_none_computed_is_empty_success(
    client, projected_purchase_order
):
    """The legitimate empty-list case must keep working: a real
    `purchase_order_id` with no mitigation options computed yet for that
    `projection_date` is `success: true`, `options: []` -- not an error."""
    history = client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": projected_purchase_order["id"]}
    ).json()["data"]
    projection_date = history[0]["projection_date"]

    resp = client.get(
        "/api/v1/penalties/mitigations",
        params={"purchase_order_id": projected_purchase_order["id"], "projection_date": projection_date},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["options"] == []


def test_get_single_mitigation_by_id(client, projection_id):
    client.post("/api/v1/penalties/mitigations", json={"projection_id": projection_id})
    options = client.get("/api/v1/penalties/mitigations", params={"projection_id": projection_id}).json()[
        "data"
    ]["options"]
    mitigation_id = options[0]["id"]

    resp = client.get(f"/api/v1/penalties/mitigations/{mitigation_id}")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["id"] == mitigation_id
    assert body["summary_status"] is None


def test_get_unknown_mitigation_returns_404(client):
    resp = client.get("/api/v1/penalties/mitigations/00000000-0000-0000-0000-000000000000")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MITIGATION_OPTION_NOT_FOUND"


def test_list_penalty_mitigations_without_include_has_no_summary_status(client, projection_id):
    client.post("/api/v1/penalties/mitigations", json={"projection_id": projection_id})

    resp = client.get("/api/v1/penalties/mitigations", params={"projection_id": projection_id})

    assert resp.status_code == 200
    options = resp.json()["data"]["options"]
    assert len(options) > 0
    assert all(o["summary_status"] is None and o["summary"] is None for o in options)


def test_list_penalty_mitigations_with_include_summary_attaches_pending_status(
    client, projected_purchase_order, projection_id
):
    client.post("/api/v1/penalties/mitigations", json={"projection_id": projection_id})
    history = client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": projected_purchase_order["id"]}
    ).json()["data"]
    as_of_date = history[0]["projection_date"]
    trigger = client.post(
        "/api/v1/penalties/mitigations/summary",
        json={"purchase_order_id": projected_purchase_order["id"], "as_of_date": as_of_date},
    )
    assert trigger.status_code == 202, trigger.text

    resp = client.get(
        "/api/v1/penalties/mitigations",
        params={"projection_id": projection_id, "include": "summary"},
    )

    assert resp.status_code == 200
    options = resp.json()["data"]["options"]
    assert len(options) > 0
    assert all(o["summary_status"] == "PENDING" for o in options)


def test_list_penalty_mitigations_rejects_an_unknown_include_value(client, projection_id):
    resp = client.get(
        "/api/v1/penalties/mitigations",
        params={"projection_id": projection_id, "include": "bogus"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_INCLUDE"


def test_trigger_penalty_mitigation_summary_queues_a_job(client, projected_purchase_order, projection_id):
    client.post("/api/v1/penalties/mitigations", json={"projection_id": projection_id})
    history = client.get(
        "/api/v1/penalties/projections", params={"purchase_order_id": projected_purchase_order["id"]}
    ).json()["data"]
    as_of_date = history[0]["projection_date"]

    resp = client.post(
        "/api/v1/penalties/mitigations/summary",
        json={"purchase_order_id": projected_purchase_order["id"], "as_of_date": as_of_date},
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()["data"]
    assert body["status"] == "PENDING"


def test_get_penalty_mitigation_summary_returns_ready_summary_when_one_exists(
    client, projected_purchase_order, projection_id, ready_mitigation_summary
):
    run_resp = client.post("/api/v1/penalties/mitigations", json={"projection_id": projection_id})
    projection_date = run_resp.json()["data"]["projection_date"]
    ready_mitigation_summary(
        projected_purchase_order["id"], projection_date, summary_text="All clear via GET."
    )

    resp = client.get(
        "/api/v1/penalties/mitigations/summary",
        params={"purchase_order_id": projected_purchase_order["id"], "as_of_date": projection_date},
    )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "READY"
    assert body["summary"]["summary"] == "All clear via GET."


def test_get_penalty_mitigation_summary_is_null_success_when_none_requested_yet(
    client, projected_purchase_order, projection_id
):
    run_resp = client.post("/api/v1/penalties/mitigations", json={"projection_id": projection_id})
    projection_date = run_resp.json()["data"]["projection_date"]

    resp = client.get(
        "/api/v1/penalties/mitigations/summary",
        params={"purchase_order_id": projected_purchase_order["id"], "as_of_date": projection_date},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["status"] is None
    assert body["data"]["summary"] is None


def test_get_penalty_mitigation_summary_still_404s_for_an_unknown_purchase_order(client):
    """The convergence to a null success only applies to "no job exists
    yet for a real PO" -- an unknown `purchase_order_id` is still a
    genuine 404, same `PO_NOT_FOUND` convention as everywhere else."""
    resp = client.get(
        "/api/v1/penalties/mitigations/summary",
        params={"purchase_order_id": "00000000-0000-0000-0000-000000000000"},
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PO_NOT_FOUND"
