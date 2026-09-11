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
