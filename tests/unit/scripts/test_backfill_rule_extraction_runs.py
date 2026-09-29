"""Tests for `scripts/ops/backfill_rule_extraction_runs.py`'s core `backfill()` function.

Exercises it directly against the SQLite `db_session` fixture, the same way the script's
own `main()` would against a real session: dry run touches nothing, `--apply` repairs
every stale row, and a second `--apply` is a no-op (idempotent).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.models import AgentRun, HumanAction, WorkflowThread
from app.repositories.penalties.rule_extraction import ExtractedPenaltyRuleRepository
from app.repositories.process.agent_registry import AgentRegistryRepository, AgentRunRepository
from scripts.ops.backfill_rule_extraction_runs import backfill


def _seed_legacy_run_and_agreement(repos, db_session) -> tuple[UUID, UUID]:
    """A legacy `PENALTY_RULE_EXTRACTION` run: untagged metadata, stuck `running`, one staged rule."""
    retailer = repos.master_data.add_retailer("RET-BACKFILL", "Retailer Backfill", None, "SUM")
    retailer_agreement = repos.retailer_agreements.add_retailer_agreement(
        retailer_id=retailer["id"],
        contract_code="CONTRACT-BACKFILL-1",
        title="Backfill Agreement",
        document_sha256="ab" * 32,
        markdown_text="# Agreement",
    )
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="penalty_rule_extractor",
        prompt_version="v1",
        system_prompt="Extract penalty clauses from retailer agreement markdown.",
        agent_name="Penalty Rule Extractor",
        domain="penalties",
    )
    run_id = AgentRunRepository(db_session).start(agent_id, run_type="PENALTY_RULE_EXTRACTION", metadata={})
    run = db_session.get(AgentRun, run_id)
    run.status = "running"
    db_session.flush()

    ExtractedPenaltyRuleRepository(db_session).add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=run_id,
        clause_text="Supplier shall pay 2% of shortfall value.",
        clause_fingerprint="a" * 32,
        penalty_category="SHORTAGE_FEE",
        calc_type="PERCENT_OF_PO",
        pricing_readiness="AWAITING_DATA",
        confidence=0.9,
    )
    return run_id, retailer_agreement["id"]


def _seed_open_interrupt_thread(db_session, run_id: UUID) -> UUID:
    thread = WorkflowThread(
        status="waiting_rule_review",
        stage="RULE_REVIEW",
        metadata_json={"agent_run_id": str(run_id)},
    )
    db_session.add(thread)
    db_session.flush()
    return thread.id


def _seed_open_human_action(db_session) -> UUID:
    action = HumanAction(
        interrupt_type="rule_review_required",
        request_payload={},
        status="open",
    )
    db_session.add(action)
    db_session.flush()
    return action.id


@pytest.fixture
def seeded(repos, db_session):
    run_id, retailer_agreement_id = _seed_legacy_run_and_agreement(repos, db_session)
    thread_id = _seed_open_interrupt_thread(db_session, run_id)
    action_id = _seed_open_human_action(db_session)
    return run_id, retailer_agreement_id, thread_id, action_id


def test_dry_run_reports_changes_but_mutates_nothing(db_session, seeded):
    run_id, _retailer_agreement_id, thread_id, action_id = seeded

    counts = backfill(db_session, apply=False)

    assert counts == {
        "runs_tagged_retailer_agreement": 1,
        "runs_tagged_prompt_version": 1,
        "runs_completed": 1,
        "threads_closed": 1,
        "actions_cancelled": 1,
    }

    run = db_session.get(AgentRun, run_id)
    assert run.status == "running"
    assert run.metadata_json == {}
    thread = db_session.get(WorkflowThread, thread_id)
    assert thread.status == "waiting_rule_review"
    assert thread.stage == "RULE_REVIEW"
    action = db_session.get(HumanAction, action_id)
    assert action.status == "open"
    assert action.responded_at is None


def test_apply_repairs_every_stale_row(db_session, seeded):
    run_id, retailer_agreement_id, thread_id, action_id = seeded

    counts = backfill(db_session, apply=True)

    assert counts == {
        "runs_tagged_retailer_agreement": 1,
        "runs_tagged_prompt_version": 1,
        "runs_completed": 1,
        "threads_closed": 1,
        "actions_cancelled": 1,
    }

    run = db_session.get(AgentRun, run_id)
    assert run.status == "completed"
    assert run.metadata_json["retailer_agreement_id"] == str(retailer_agreement_id)
    assert run.metadata_json["prompt_version"] == "v1"
    assert run.completed_at is not None

    thread = db_session.get(WorkflowThread, thread_id)
    assert thread.status == "completed"
    assert thread.stage == "RULE_REVIEW_CLOSED"
    assert thread.completed_at is not None

    action = db_session.get(HumanAction, action_id)
    assert action.status == "cancelled"
    assert action.responded_at is not None
    assert action.reason == "Rule review interrupt removed; review is a database status."


def test_second_apply_is_a_no_op(db_session, seeded):
    backfill(db_session, apply=True)

    counts = backfill(db_session, apply=True)

    assert counts == {
        "runs_tagged_retailer_agreement": 0,
        "runs_tagged_prompt_version": 0,
        "runs_completed": 0,
        "threads_closed": 0,
        "actions_cancelled": 0,
    }


def test_already_tagged_run_is_left_alone(repos, db_session):
    """A run tagged and completed by `start_extraction` needs no repair -- confirms the
    script doesn't touch runs that are already in the current shape."""
    retailer = repos.master_data.add_retailer("RET-BACKFILL-2", "Retailer Backfill 2", None, "SUM")
    retailer_agreement = repos.retailer_agreements.add_retailer_agreement(
        retailer_id=retailer["id"],
        contract_code="CONTRACT-BACKFILL-2",
        title="Backfill Agreement 2",
        document_sha256="cd" * 32,
        markdown_text="# Agreement",
    )
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="penalty_rule_extractor",
        prompt_version="v2",
        system_prompt="Extract penalty clauses from retailer agreement markdown.",
        agent_name="Penalty Rule Extractor",
        domain="penalties",
    )
    run_id = AgentRunRepository(db_session).start(
        agent_id,
        run_type="PENALTY_RULE_EXTRACTION",
        metadata={"retailer_agreement_id": str(retailer_agreement["id"]), "prompt_version": "v2"},
    )
    AgentRunRepository(db_session).update_status(run_id, "completed", completed=True)
    ExtractedPenaltyRuleRepository(db_session).add_extracted_rule(
        retailer_agreement_id=retailer_agreement["id"],
        agent_run_id=run_id,
        clause_text="Supplier shall pay 4% of shortfall value.",
        clause_fingerprint="b" * 32,
        penalty_category="SHORTAGE_FEE",
        calc_type="PERCENT_OF_PO",
        pricing_readiness="AWAITING_DATA",
        confidence=0.9,
    )

    counts = backfill(db_session, apply=True)

    assert counts == {
        "runs_tagged_retailer_agreement": 0,
        "runs_tagged_prompt_version": 0,
        "runs_completed": 0,
        "threads_closed": 0,
        "actions_cancelled": 0,
    }
