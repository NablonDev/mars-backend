"""Tests for AgentRegistryRepository -- the source of `process.agent` rows
(one row per (agent_code, prompt_version) pair). Was
tests/unit/repositories/test_agent_registry.py against
`PromptRegistryRepository` (`Agent`/`PromptVersion`, two tables) --
relocated onto the merged `process.agent` table (see
app/models/process/agent.py's docstring)."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import Agent, AgentRun
from app.repositories.process.agent_registry import (
    AgentRegistryRepository,
    AgentRunRepository,
    AgentTraceRepository,
)
from app.utils.clock import utc_now


def test_ensure_registered_creates_an_agent_row(db_session):
    repo = AgentRegistryRepository(db_session)

    agent_id = repo.ensure_registered(
        agent_code="fine_projection_summary",
        prompt_version="v1",
        system_prompt="You are a penalty-projection summarizer.",
        agent_name="Penalty Projection Summary",
        domain="penalties",
        description="Generates the projection summary narrative.",
    )

    agent = db_session.scalars(select(Agent).where(Agent.agent_code == "fine_projection_summary")).first()
    assert agent is not None
    assert agent.id == agent_id
    assert agent.domain == "penalties"
    assert agent.description == "Generates the projection summary narrative."


def test_ensure_registered_defaults_description_to_none(db_session):
    repo = AgentRegistryRepository(db_session)

    repo.ensure_registered(
        agent_code="fine_projection_summary",
        prompt_version="v1",
        system_prompt="You are a penalty-projection summarizer.",
        agent_name="Penalty Projection Summary",
        domain="penalties",
    )

    agent = db_session.scalars(select(Agent).where(Agent.agent_code == "fine_projection_summary")).first()
    assert agent.description is None


def test_ensure_registered_requires_domain(db_session):
    """`domain` has no default -- `process.agent.domain` is a NOT NULL column
    restricted by `ck_agent_domain` to ('cmir', 'penalties'); there is no
    sensible default between the two, so every caller must say which."""
    repo = AgentRegistryRepository(db_session)

    with pytest.raises(TypeError):
        repo.ensure_registered(
            agent_code="fine_projection_summary",
            prompt_version="v1",
            system_prompt="You are a penalty-projection summarizer.",
            agent_name="Penalty Projection Summary",
        )


def test_ensure_registered_is_idempotent(db_session):
    """Called on the service's own read/write path -- must never duplicate
    rows across repeated calls."""
    repo = AgentRegistryRepository(db_session)

    agent_ids = [
        repo.ensure_registered(
            agent_code="fine_projection_summary",
            prompt_version="v1",
            system_prompt="You are a penalty-projection summarizer.",
            agent_name="Penalty Projection Summary",
            domain="penalties",
        )
        for _ in range(3)
    ]

    assert len(set(agent_ids)) == 1
    agent_count = db_session.scalar(select(func.count()).select_from(Agent))
    assert agent_count == 1


def test_ensure_registered_adds_a_new_row_under_the_same_agent_code(db_session):
    repo = AgentRegistryRepository(db_session)
    first_agent_id = repo.ensure_registered(
        agent_code="fine_projection_summary",
        prompt_version="v1",
        system_prompt="v1 prompt",
        agent_name="Penalty Projection Summary",
        domain="penalties",
    )
    second_agent_id = repo.ensure_registered(
        agent_code="fine_projection_summary",
        prompt_version="v2",
        system_prompt="v2 prompt",
        agent_name="Penalty Projection Summary",
        domain="penalties",
    )

    assert first_agent_id != second_agent_id
    agent_count = db_session.scalar(select(func.count()).select_from(Agent))
    assert agent_count == 2


def test_get_active_returns_only_the_active_row(db_session):
    repo = AgentRegistryRepository(db_session)
    repo.ensure_registered(
        agent_code="fine_projection_summary",
        prompt_version="v1",
        system_prompt="v1 prompt",
        agent_name="Penalty Projection Summary",
        domain="penalties",
        is_active=True,
    )

    active = repo.get_active("fine_projection_summary")
    assert active["prompt_version"] == "v1"


def test_get_by_code_version_returns_none_when_missing(db_session):
    repo = AgentRegistryRepository(db_session)
    assert repo.get_by_code_version("does_not_exist", "v1") is None


def test_agent_trace_log_rolls_back_and_reraises_on_a_write_failure(db_session):
    """Regression test for the session-poisoning bug: a failed flush (a NUL byte in
    a snapshot/error field raises this way in production) must not leave the shared
    session in a failed transactional state for later, unrelated callers."""
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="test_agent_trace",
        prompt_version="v1",
        system_prompt="prompt",
        agent_name="Test Agent Trace",
        domain="penalties",
    )
    run_id = AgentRunRepository(db_session).start(agent_id=agent_id, run_type="TEST")
    repo = AgentTraceRepository(db_session)

    def _failing_flush() -> None:
        raise ValueError("A string literal cannot contain NUL (0x00) characters.")

    db_session.flush = _failing_flush

    with pytest.raises(ValueError):
        repo.log(
            run_id,
            "some_node",
            "failed",
            utc_now(),
            utc_now(),
            10,
            input_snapshot={"a": 1},
            output_snapshot=None,
            error="boom",
        )

    del db_session.flush  # restore the bound method now that the fake did its job

    # The session must still be usable afterward: a later, unrelated write on the
    # same session succeeds instead of failing with "PendingRollbackError".
    other_run_id = AgentRunRepository(db_session).start(agent_id=agent_id, run_type="TEST")
    assert other_run_id is not None


def test_update_status_strips_nul_bytes_from_the_error_message(db_session):
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="test_agent_run_status",
        prompt_version="v1",
        system_prompt="prompt",
        agent_name="Test Agent Run Status",
        domain="penalties",
    )
    repo = AgentRunRepository(db_session)
    run_id = repo.start(agent_id=agent_id, run_type="TEST")

    repo.update_status(run_id, "failed", error="boom\x00: NUL from a rejected insert", completed=True)

    assert repo.get(run_id)["error"] == "boom: NUL from a rejected insert"


def test_update_status_rolls_back_only_its_own_savepoint_on_a_write_failure(db_session):
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="test_agent_run_status_failure",
        prompt_version="v1",
        system_prompt="prompt",
        agent_name="Test Agent Run Status Failure",
        domain="penalties",
    )
    repo = AgentRunRepository(db_session)
    run_id = repo.start(agent_id=agent_id, run_type="TEST")

    def _failing_flush() -> None:
        raise ValueError("A string literal cannot contain NUL (0x00) characters.")

    db_session.flush = _failing_flush

    with pytest.raises(ValueError):
        repo.update_status(run_id, "failed", error="boom")

    del db_session.flush  # restore the bound method now that the fake did its job

    other_run_id = repo.start(agent_id=agent_id, run_type="TEST")
    assert other_run_id is not None


def test_start_stores_the_given_metadata(db_session):
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="test_agent_run_metadata",
        prompt_version="v1",
        system_prompt="prompt",
        agent_name="Test Agent Run Metadata",
        domain="penalties",
    )
    repo = AgentRunRepository(db_session)

    run_id = repo.start(agent_id=agent_id, run_type="TEST", metadata={"retailer_agreement_id": "abc"})

    assert repo.get(run_id)["metadata_json"] == {"retailer_agreement_id": "abc"}


def test_start_defaults_metadata_to_an_empty_dict(db_session):
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="test_agent_run_metadata_default",
        prompt_version="v1",
        system_prompt="prompt",
        agent_name="Test Agent Run Metadata Default",
        domain="penalties",
    )
    repo = AgentRunRepository(db_session)

    run_id = repo.start(agent_id=agent_id, run_type="TEST")

    assert repo.get(run_id)["metadata_json"] == {}


def test_list_by_metadata_returns_only_matching_run_type_and_metadata_value_newest_first(db_session):
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="test_agent_run_list_by_metadata",
        prompt_version="v1",
        system_prompt="prompt",
        agent_name="Test Agent Run List By Metadata",
        domain="penalties",
    )
    repo = AgentRunRepository(db_session)

    older_id = repo.start(
        agent_id=agent_id, run_type="PENALTY_RULE_EXTRACTION", metadata={"retailer_agreement_id": "ra-1"}
    )
    newer_id = repo.start(
        agent_id=agent_id, run_type="PENALTY_RULE_EXTRACTION", metadata={"retailer_agreement_id": "ra-1"}
    )
    # Different metadata value: must not match.
    repo.start(
        agent_id=agent_id, run_type="PENALTY_RULE_EXTRACTION", metadata={"retailer_agreement_id": "ra-2"}
    )
    # Different run_type, same metadata value: must not match.
    repo.start(agent_id=agent_id, run_type="OTHER_RUN_TYPE", metadata={"retailer_agreement_id": "ra-1"})
    # SQLite's CURRENT_TIMESTAMP has only second resolution, so the two matching rows
    # above can tie on `created_at`; force them apart to make the ordering assertion
    # deterministic instead of racy.
    now = utc_now()
    db_session.get(AgentRun, older_id).created_at = now - timedelta(minutes=1)
    db_session.get(AgentRun, newer_id).created_at = now
    db_session.flush()

    rows = repo.list_by_metadata("PENALTY_RULE_EXTRACTION", "retailer_agreement_id", "ra-1")

    assert [row["id"] for row in rows] == [newer_id, older_id]


def test_list_by_metadata_excludes_soft_deleted_runs(db_session):
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code="test_agent_run_list_by_metadata_deleted",
        prompt_version="v1",
        system_prompt="prompt",
        agent_name="Test Agent Run List By Metadata Deleted",
        domain="penalties",
    )
    repo = AgentRunRepository(db_session)
    run_id = repo.start(
        agent_id=agent_id, run_type="PENALTY_RULE_EXTRACTION", metadata={"retailer_agreement_id": "ra-3"}
    )
    row = db_session.get(AgentRun, run_id)
    row.deleted_at = utc_now()
    db_session.flush()

    rows = repo.list_by_metadata("PENALTY_RULE_EXTRACTION", "retailer_agreement_id", "ra-3")

    assert rows == []
