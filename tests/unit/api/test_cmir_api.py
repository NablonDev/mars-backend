"""API tests for `app/api/v1/cmir.py` (`POST /cmir/email-events`,
`POST /internal/process-email`) -- Phase 7b route redesign.

Was `tests/unit/api/test_cmir_api.py`'s `unittest.TestCase` against the
pre-restructure `/ingest/emails`/`/threads/{id}/*` routes and the since-removed
`app.core.exceptions.ServiceError`; rewritten against the new routes, the
collapsed `AppError` hierarchy, and the `{success, message, data, error}`
envelope. Thread-lifecycle routes (`missing-fields`/`draft`/`decisions`) now
live on `app/api/v1/workflow_threads.py` -- see
`tests/unit/api/test_workflow_threads_api.py`.
"""

from __future__ import annotations

import pytest


class FakeCmirService:
    """Implements the `CmirService` methods `app/api/v1/cmir.py` calls."""

    def start_email_ingest(self, **kwargs):
        return {
            "batch_id": "11111111-1111-1111-1111-111111111111",
            "status": "ready_for_queue",
            "total_threads": 1,
            "threads": [
                {
                    "batch_id": "11111111-1111-1111-1111-111111111111",
                    "agent_run_id": None,
                    "thread_id": None,
                    "email_id": "9c76f0b3-1e8d-4f31-9d17-15f42ad8f970",
                    "source_message_id": "msg-001",
                    "sender": "customer@example.com",
                    "subject": "CMIR Request 1",
                    "stage": "NEW",
                    "status": "new",
                    "current_node": None,
                    "pending_action_id": None,
                    "updated_at": None,
                }
            ],
        }

    def process_queued_email(self, *, batch_id, email, queue_message_id, email_id=None):
        return {
            "batch_id": batch_id,
            "agent_run_id": "00000000-0000-0000-0000-000000001042",
            "thread_id": None,
            "email_id": str(email_id),
            "stage": "COMPLETED_APPROVED",
            "status": "completed_approved",
            "current_node": None,
            "pending_action_id": None,
            "updated_at": None,
        }

    def get_health_snapshot(self, *, stale_days=180, attention_limit=50):
        return {
            "total_current": 2,
            "healthy_current": 1,
            "unhealthy_breakdown": [
                {"category": "stale_validation", "count": 1},
                {"category": "missing_required_field", "count": 0},
                {"category": "duplicate_row", "count": 0},
            ],
            "creation_trend": [{"month": "2026-09", "count": 2}],
            "needing_attention": [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "customer_identity": "Acme Manufacturing Ltd",
                    "target_customer_material_ref": "ACME-PE200-STD",
                    "target_grd_code": "GRD-1",
                    "brand": "AcmePlast",
                    "site": "Site A",
                    "valid_from": "2026-01-01T00:00:00+00:00",
                    "reasons": ["stale_validation"],
                }
            ],
        }

    def get_health_trend(self, *, months=6, stale_days=180):
        return [
            {"month": "2026-08", "total": 2, "healthy": 1},
            {"month": "2026-09", "total": 3, "healthy": 2},
        ]

    def list_pending_emails(self, *, limit=50):
        return [
            {
                "id": "33333333-3333-3333-3333-333333333333",
                "sender": "customer@example.com",
                "subject": "Update CMIR mapping",
                "raw_content": "Please update our material reference.",
                "queue_status": "queued",
                "queued_at": "2026-09-25T00:00:00+00:00",
                "processing_started_at": None,
                "queue_delivery_count": 0,
                "created_at": "2026-09-25T00:00:00+00:00",
            }
        ]

    def process_pending_email(self, email_id):
        return {"stage": "COMPLETED_APPROVED", "email_id": str(email_id)}

    def get_housekeeping_audit_log(self, *, limit=10):
        return [
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "timestamp": "2026-09-24T00:00:00+00:00",
                "action": "Create",
                "customer_identity": "Acme Manufacturing Ltd",
                "target_grd_code": "GRD-1",
                "target_customer_material_ref": "ACME-PE200-STD",
                "executed_by": None,
                "outcome": None,
            }
        ]


@pytest.fixture
def cmir_service():
    return FakeCmirService()


def test_create_email_events_returns_accepted_batch_payload(client):
    response = client.post("/api/v1/cmir/email-events", json={})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["success"] is True
    assert body["data"]["batch_id"] == "11111111-1111-1111-1111-111111111111"
    assert body["data"]["threads"][0]["email_id"] == "9c76f0b3-1e8d-4f31-9d17-15f42ad8f970"


def test_create_email_events_rejects_non_gmail_source(client):
    response = client.post("/api/v1/cmir/email-events", json={"source": "outlook"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_process_queued_email_wraps_result_in_envelope(client):
    response = client.post(
        "/api/v1/internal/process-email",
        json={
            "batch_id": "11111111-1111-1111-1111-111111111111",
            "email_id": "9c76f0b3-1e8d-4f31-9d17-15f42ad8f970",
            "queue_message_id": "msg-1",
            "email": {"sender": "a@b.com", "subject": "s", "body": "b"},
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["stage"] == "COMPLETED_APPROVED"


def test_get_cmir_health_returns_envelope_wrapped_snapshot(client):
    response = client.get("/api/v1/cmir-records/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert data["total_current"] == 2
    assert data["healthy_current"] == 1
    assert data["needing_attention"][0]["reasons"] == ["stale_validation"]


def test_get_cmir_health_trend_returns_envelope_wrapped_points(client):
    response = client.get("/api/v1/cmir-records/health-trend")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert data[-1]["month"] == "2026-09"
    assert data[-1]["total"] == 3
    assert data[-1]["healthy"] == 2


def test_list_pending_emails_returns_envelope_wrapped_queue(client):
    response = client.get("/api/v1/cmir/email-events/pending")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["data"][0]["sender"] == "customer@example.com"
    assert body["data"][0]["queue_status"] == "queued"


def test_process_pending_email_returns_envelope_wrapped_result(client):
    response = client.post("/api/v1/cmir/email-events/33333333-3333-3333-3333-333333333333/process")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["data"]["stage"] == "COMPLETED_APPROVED"


def test_get_cmir_housekeeping_audit_log_returns_envelope_wrapped_entries(client):
    response = client.get("/api/v1/cmir-records/housekeeping-audit-log")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert data[0]["action"] == "Create"
    assert data[0]["executed_by"] is None
