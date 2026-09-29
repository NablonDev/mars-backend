"""API tests for the generic `process.job_run`/`job_item` batch trigger/status/items
endpoints (`/job-runs`, `/job-runs/{id}`, `/job-runs/{id}/items`) -- covers
`job_type=PENALTY_PROJECTION_BATCH` (mapped internally to `JobTaskType.ORDER_RUN`),
`job_type=PENALTY_MITIGATION_BATCH` (mapped internally to
`JobTaskType.MITIGATION_RUN`), and `job_type=PENALTY_FULL_RUN_BATCH` (mapped
internally to `JobTaskType.PENALTY_FULL_RUN`); the CMIR follow-up pass adds
coverage for its own job types."""

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
        "/api/v1/retailers", json={"retailer_code": "RET-BATCH", "retailer_name": "Batch Co"}
    ).json()["data"]


@pytest.fixture
def open_purchase_order(client, retailer):
    resp = client.post(
        "/api/v1/purchase-orders",
        json={
            "purchase_order_number": "PO-BATCH-001",
            "retailer_id": retailer["id"],
            "order_date": "2026-08-01",
            "requested_delivery_date": "2026-08-10",
            "required_ship_date": "2026-08-08",
            "lines": [{"line_number": "10", "ordered_quantity": 100, "unit_price": 5.0}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def test_trigger_job_run_with_no_open_orders_returns_zero_items(client):
    resp = client.post("/api/v1/job-runs", json={})

    assert resp.status_code == 202, resp.text
    body = resp.json()["data"]
    assert body["requested_item_count"] == 0
    assert body["execution_note"]


def test_trigger_job_run_enqueues_one_item_per_open_order(client, open_purchase_order):
    resp = client.post("/api/v1/job-runs", json={})

    assert resp.status_code == 202, resp.text
    body = resp.json()["data"]
    assert body["requested_item_count"] == 1
    assert body["job_run_id"]
    assert body["dispatch_mode"]


def test_get_job_run_status_reports_counts_and_completeness(client, open_purchase_order):
    job_run_id = client.post("/api/v1/job-runs", json={}).json()["data"]["job_run_id"]

    resp = client.get(f"/api/v1/job-runs/{job_run_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["job_run_id"] == job_run_id
    assert body["requested_item_count"] == 1
    assert body["total_items"] == 1
    assert isinstance(body["is_complete"], bool)
    assert set(body["counts"].keys()) >= {"PENDING", "RUNNING", "SUCCEEDED", "DEAD"}


def test_get_job_run_status_for_unknown_run_returns_404(client):
    resp = client.get("/api/v1/job-runs/00000000-0000-0000-0000-000000000000")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "JOB_RUN_NOT_FOUND"


def test_list_job_run_items_returns_the_enqueued_item(client, open_purchase_order):
    job_run_id = client.post("/api/v1/job-runs", json={}).json()["data"]["job_run_id"]

    resp = client.get(f"/api/v1/job-runs/{job_run_id}/items")

    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["job_run_id"] == job_run_id
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["item_type"] == "ORDER_RUN"
    assert item["attempt_count"] >= 0


def test_list_job_run_items_supports_status_filter_and_pagination(client, open_purchase_order):
    job_run_id = client.post("/api/v1/job-runs", json={}).json()["data"]["job_run_id"]

    resp = client.get(f"/api/v1/job-runs/{job_run_id}/items", params={"status": "DEAD", "limit": 5})

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["items"] == []


# ---------------------------------------------------------------------
# job_type=PENALTY_MITIGATION_BATCH (JobTaskType.MITIGATION_RUN)
# ---------------------------------------------------------------------


@pytest.fixture
def projected_open_purchase_order(client, repos, retailer):
    """An OPEN purchase order with a persisted penalty projection --
    eligible for PENALTY_MITIGATION_BATCH, mirroring
    tests/unit/api/test_api_penalty_mitigations.py's `projected_purchase_order`
    fixture (kept local here rather than shared, since that file's fixture
    also creates its own retailer)."""
    retailer_agreement = _create_retailer_agreement(repos, retailer["id"])
    client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-BATCH-MIT",
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
            "purchase_order_number": "PO-BATCH-MIT-001",
            "retailer_id": retailer["id"],
            "order_date": "2026-08-01",
            "requested_delivery_date": "2026-08-10",
            "required_ship_date": "2026-08-08",
            "lines": [{"line_number": "10", "ordered_quantity": 100, "unit_price": 5.0}],
        },
    ).json()["data"]
    resp = client.post("/api/v1/penalties/projections", json={"purchase_order_id": po["id"]})
    assert resp.status_code == 201, resp.text
    return po


def test_trigger_penalty_mitigation_batch_with_no_open_orders_returns_zero_items(client):
    resp = client.post("/api/v1/job-runs", json={"job_type": "PENALTY_MITIGATION_BATCH"})

    assert resp.status_code == 202, resp.text
    body = resp.json()["data"]
    assert body["requested_item_count"] == 0
    assert body["execution_note"]


def test_trigger_penalty_mitigation_batch_skips_open_orders_with_no_projection(client, open_purchase_order):
    """`open_purchase_order` (this file's existing fixture, above) has no
    projection -- not eligible, mirroring the exact NO_PROJECTION_EXISTS
    check `MitigationService.run_for_purchase_order` performs for a single
    PO. Skipped, not errored: the batch as a whole still returns 202."""
    resp = client.post("/api/v1/job-runs", json={"job_type": "PENALTY_MITIGATION_BATCH"})

    assert resp.status_code == 202, resp.text
    body = resp.json()["data"]
    assert body["requested_item_count"] == 0
    assert "projection" in body["execution_note"].lower()


def test_trigger_penalty_mitigation_batch_enqueues_one_item_per_eligible_order(
    client, projected_open_purchase_order
):
    resp = client.post("/api/v1/job-runs", json={"job_type": "PENALTY_MITIGATION_BATCH"})

    assert resp.status_code == 202, resp.text
    body = resp.json()["data"]
    assert body["requested_item_count"] == 1
    assert body["job_run_id"]

    items = client.get(f"/api/v1/job-runs/{body['job_run_id']}/items").json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["item_type"] == "MITIGATION_RUN"


def test_trigger_penalty_mitigation_batch_status_reports_counts(client, projected_open_purchase_order):
    job_run_id = client.post("/api/v1/job-runs", json={"job_type": "PENALTY_MITIGATION_BATCH"}).json()[
        "data"
    ]["job_run_id"]

    resp = client.get(f"/api/v1/job-runs/{job_run_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["requested_item_count"] == 1
    assert body["total_items"] == 1


# ---------------------------------------------------------------------
# job_type=PENALTY_FULL_RUN_BATCH (JobTaskType.PENALTY_FULL_RUN)
# ---------------------------------------------------------------------


def test_penalty_full_run_batch_rejects_empty_steps(client):
    resp = client.post(
        "/api/v1/job-runs",
        json={"job_type": "PENALTY_FULL_RUN_BATCH", "steps": []},
    )

    assert resp.status_code == 422, resp.text


def test_penalty_full_run_batch_rejects_both_scope_shapes(client):
    resp = client.post(
        "/api/v1/job-runs",
        json={
            "job_type": "PENALTY_FULL_RUN_BATCH",
            "steps": ["projection"],
            "scope": {
                "purchase_order_status": "OPEN",
                "purchase_order_ids": ["00000000-0000-0000-0000-000000000000"],
            },
        },
    )

    assert resp.status_code == 422, resp.text


def test_penalty_full_run_batch_allows_purchase_order_ids_alone(client):
    """Omitting `purchase_order_status` entirely while supplying
    `purchase_order_ids` is not a conflict -- the default only becomes a
    conflict when the caller *explicitly* sets it alongside `purchase_order_ids`
    (see PenaltyFullRunScope's own validator)."""
    resp = client.post(
        "/api/v1/job-runs",
        json={
            "job_type": "PENALTY_FULL_RUN_BATCH",
            "steps": ["projection"],
            "scope": {"purchase_order_ids": ["00000000-0000-0000-0000-000000000000"]},
        },
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()["data"]
    assert body["requested_item_count"] == 0


def test_penalty_full_run_batch_with_no_open_orders_returns_zero_items(client):
    resp = client.post(
        "/api/v1/job-runs",
        json={"job_type": "PENALTY_FULL_RUN_BATCH", "steps": ["projection"]},
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()["data"]
    assert body["requested_item_count"] == 0
    assert body["execution_note"]


def test_penalty_full_run_batch_skips_open_orders_with_no_projection_when_projection_not_requested(
    client, open_purchase_order
):
    resp = client.post(
        "/api/v1/job-runs",
        json={"job_type": "PENALTY_FULL_RUN_BATCH", "steps": ["mitigation"]},
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()["data"]
    assert body["requested_item_count"] == 0
    assert "projection" in body["execution_note"].lower()


def test_penalty_full_run_batch_enqueues_one_item_per_open_order_when_projection_requested(
    client, open_purchase_order
):
    """`open_purchase_order` has no projection yet, but `"projection"` is
    itself in `steps` -- every OPEN purchase order is eligible since this
    run will produce one."""
    resp = client.post(
        "/api/v1/job-runs",
        json={
            "job_type": "PENALTY_FULL_RUN_BATCH",
            "steps": ["projection", "projection_summary", "mitigation", "mitigation_summary"],
        },
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()["data"]
    assert body["requested_item_count"] == 1
    assert body["job_run_id"]

    items = client.get(f"/api/v1/job-runs/{body['job_run_id']}/items").json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["item_type"] == "PENALTY_FULL_RUN"


def test_penalty_full_run_batch_enqueues_one_item_per_eligible_order_when_projection_not_requested(
    client, projected_open_purchase_order
):
    resp = client.post(
        "/api/v1/job-runs",
        json={"job_type": "PENALTY_FULL_RUN_BATCH", "steps": ["mitigation", "mitigation_summary"]},
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()["data"]
    assert body["requested_item_count"] == 1

    items = client.get(f"/api/v1/job-runs/{body['job_run_id']}/items").json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["item_type"] == "PENALTY_FULL_RUN"
