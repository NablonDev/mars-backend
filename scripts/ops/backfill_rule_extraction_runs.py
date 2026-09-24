#!/usr/bin/env python3
"""
One-off, idempotent repair of legacy `PENALTY_RULE_EXTRACTION` runs from before this
run started tagging `agent_run.metadata` with `retailer_agreement_id`/`prompt_version`
and before the graph dropped its `human_review` interrupt (see
`docs/architecture/penalty-rule-extraction.md`).

For every distinct `(agent_run_id, retailer_agreement_id)` pair found on
`penalties.extracted_penalty_rule`:

- Tags the run's `agent_run.metadata` with `retailer_agreement_id` (if missing) and
  `prompt_version` (`"v1"`, if missing -- the version every legacy run predates `v2`
  under).
- A run still parked `running` or `waiting_rule_review` is marked `completed`, since
  review is a database status now, not a graph pause the run is waiting on; its
  `completed_at` backfills to the newest `created_at` among its own staged rules when it
  has none of its own.

Then, independently of which runs were touched above: every `process.workflow_thread`
row whose `metadata->>'agent_run_id'` names a run in that set and is still
`waiting_rule_review` closes to `completed`/`RULE_REVIEW_CLOSED`; every
`process.human_action` row with `interrupt_type = 'rule_review_required'` and
`status = 'open'` anywhere is cancelled, since the interrupt itself no longer exists.

Idempotent: a second run reports zero changes in every category, so this is safe to run
more than once and does not need to track which rows it already touched.

Dry run by default -- prints every change it would make, prefixed `DRY RUN:`, and makes
none. Pass `--apply` to commit them.

Usage:
    python scripts/ops/backfill_rule_extraction_runs.py
    python scripts/ops/backfill_rule_extraction_runs.py --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import Database
from app.models import AgentRun, ExtractedPenaltyRule, HumanAction, WorkflowThread

logger = logging.getLogger(__name__)

_RUN_TYPE = "PENALTY_RULE_EXTRACTION"
_STALE_RUN_STATUSES = ("running", "waiting_rule_review")
_LEGACY_PROMPT_VERSION = "v1"
_INTERRUPT_TYPE = "rule_review_required"
_CANCEL_REASON = "Rule review interrupt removed; review is a database status."


def backfill(session: Session, *, apply: bool) -> dict[str, int]:
    """Repair legacy extraction runs, in one session. Returns a count per change category.

    `apply=False` (the default the caller should use for a dry run) reads and prints only
    -- no ORM attribute is mutated, so the session has nothing to roll back. `apply=True`
    mutates the session's objects in place; the caller is responsible for committing (or
    rolling back on error).
    """
    prefix = "" if apply else "DRY RUN: "
    counts = {
        "runs_tagged_retailer_agreement": 0,
        "runs_tagged_prompt_version": 0,
        "runs_completed": 0,
        "threads_closed": 0,
        "actions_cancelled": 0,
    }

    run_agreement_pairs = session.execute(
        select(ExtractedPenaltyRule.agent_run_id, ExtractedPenaltyRule.retailer_agreement_id).distinct()
    ).all()
    touched_run_ids: set = set()

    for agent_run_id, retailer_agreement_id in run_agreement_pairs:
        run = session.get(AgentRun, agent_run_id)
        if run is None or run.run_type != _RUN_TYPE:
            continue
        touched_run_ids.add(run.id)

        metadata = dict(run.metadata_json or {})
        metadata_changed = False
        if "retailer_agreement_id" not in metadata:
            metadata["retailer_agreement_id"] = str(retailer_agreement_id)
            metadata_changed = True
            counts["runs_tagged_retailer_agreement"] += 1
            print(f"{prefix}agent_run={run.id}: tag metadata.retailer_agreement_id={retailer_agreement_id}")
        if "prompt_version" not in metadata:
            metadata["prompt_version"] = _LEGACY_PROMPT_VERSION
            metadata_changed = True
            counts["runs_tagged_prompt_version"] += 1
            print(f"{prefix}agent_run={run.id}: tag metadata.prompt_version={_LEGACY_PROMPT_VERSION!r}")
        if metadata_changed and apply:
            run.metadata_json = metadata

        if run.status in _STALE_RUN_STATUSES:
            counts["runs_completed"] += 1
            print(f"{prefix}agent_run={run.id}: status {run.status!r} -> 'completed'")
            if apply:
                run.status = "completed"
                if run.completed_at is None:
                    max_created_at = session.scalar(
                        select(func.max(ExtractedPenaltyRule.created_at)).where(
                            ExtractedPenaltyRule.agent_run_id == run.id
                        )
                    )
                    if max_created_at is not None:
                        run.completed_at = max_created_at

    if touched_run_ids:
        touched_run_ids_str = {str(run_id) for run_id in touched_run_ids}
        for thread in session.scalars(select(WorkflowThread)).all():
            if (
                thread.status == "waiting_rule_review"
                and str((thread.metadata_json or {}).get("agent_run_id")) in touched_run_ids_str
            ):
                counts["threads_closed"] += 1
                print(f"{prefix}workflow_thread={thread.id}: status 'waiting_rule_review' -> 'completed'")
                if apply:
                    thread.status = "completed"
                    thread.stage = "RULE_REVIEW_CLOSED"
                    thread.completed_at = datetime.now(UTC)

    open_actions = session.scalars(
        select(HumanAction).where(
            HumanAction.interrupt_type == _INTERRUPT_TYPE,
            HumanAction.status == "open",
        )
    ).all()
    for action in open_actions:
        counts["actions_cancelled"] += 1
        print(f"{prefix}human_action={action.id}: status 'open' -> 'cancelled'")
        if apply:
            action.status = "cancelled"
            action.responded_at = datetime.now(UTC)
            action.reason = _CANCEL_REASON

    if apply:
        # `Database`'s sessionmaker runs with autoflush=False, so a second call against
        # this same session (the SQLite test fixture calls `backfill` more than once to
        # prove idempotency) would otherwise still see these rows through its own
        # `select()` queries above, since nothing has flushed them to the connection yet.
        session.flush()

    print(f"{prefix}summary: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    return counts


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the repairs. Without this flag, prints what would change and rolls back.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    configure_logging(
        settings.app.log_level,
        settings.app.log_format,
        no_color=settings.app.no_color,
        no_bold=settings.app.no_bold,
    )

    database = Database(settings.database.url)
    session = database.new_session()
    try:
        backfill(session, apply=args.apply)
        if args.apply:
            session.commit()
        else:
            session.rollback()
    finally:
        session.close()


if __name__ == "__main__":
    main()
