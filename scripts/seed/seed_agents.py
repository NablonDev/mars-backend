"""Seed `process.agent` with the system prompts already committed to
`app/agents/penalties/*/prompts/*.py`, `app/services/cmir/extractor.py`,
and `app/services/po_validation/service.py`.

Standalone runnable: `python -m scripts.seed.seed_agents`. The "does not
import app/repositories//app/services/, both broken mid-restructure"
constraint was Phase-1-only -- it already imports
`app.services.cmir.extractor` for `_PROMPT_TEMPLATE`, and now also
`app.services.po_validation.service` for `_SYSTEM_PROMPT`, the same way.
Writes against the `Agent` ORM model directly, using its own `Session`
built from `Settings().database.url`.

Idempotent: upserts keyed on `(agent_code, prompt_version)`, so running
this twice leaves exactly 8 rows, not 16.

Security note (carried to the security-reviewer, see the approved Phase 1
plan): `process.agent.system_prompt` is a new persistent store of LLM
system-level instructions -- a future prompt-injection surface once a
later phase wires the runtime load. No live risk here: every row is
seeded only from version-controlled source, never user input. No
user-writable path may ever reach this column and no API endpoint may
expose a write to it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.penalties.mitigation.prompts.v1 import SYSTEM_PROMPT as MITIGATION_V1_PROMPT
from app.agents.penalties.projection.prompts.v1 import SYSTEM_PROMPT as PROJECTION_V1_PROMPT
from app.agents.penalties.rule_extraction.prompts.v1 import (
    PENALTY_CLASSIFICATION_SYSTEM_PROMPT,
    PENALTY_FACT_EXTRACTION_SYSTEM_PROMPT,
    SECTION_SCREENING_SYSTEM_PROMPT,
)
from app.agents.penalties.rule_extraction.prompts.v1 import (
    PROMPT_VERSION as RULE_EXTRACTION_PROMPT_VERSION,
)
from app.core.config import Settings
from app.db.session import Database
from app.models.process.agent import Agent
from app.services.cmir.extractor import _PROMPT_TEMPLATE as CMIR_EXTRACTOR_PROMPT_TEMPLATE
from app.services.po_validation.service import _SYSTEM_PROMPT as PO_VALIDATION_SYSTEM_PROMPT

# D3 (approved Phase 1 plan): the CMIR extractor's `_PROMPT_TEMPLATE` ends
# with a trailing "Email:\n\n{body}" block -- a per-call interpolation
# slot for user-controlled email content, not system-level instruction
# text. Seed everything above that block only; the {body} tail must
# remain a per-call user-message interpolation, never stored system-prompt
# content (see llm-agent-patterns skill's trust-boundary rule).
_CMIR_EXTRACTOR_INTERPOLATION_BOUNDARY = "Email:\n\n{body}"
CMIR_EXTRACTOR_SYSTEM_PROMPT = CMIR_EXTRACTOR_PROMPT_TEMPLATE.split(_CMIR_EXTRACTOR_INTERPOLATION_BOUNDARY)[
    0
].strip()
if "{" in CMIR_EXTRACTOR_SYSTEM_PROMPT:
    # A bare `assert` is stripped under `python -O`, which would silently
    # let a placeholder-carrying prompt (e.g. a stray {body}) reach
    # process.agent.system_prompt -- a system-level instruction store, not
    # somewhere user-controlled interpolation text belongs. Fail loudly,
    # always, module-import time.
    raise ValueError(
        "cmir_extractor._PROMPT_TEMPLATE's shape changed -- the static system-prompt "
        "portion should contain no interpolation placeholders. Re-check the "
        "Email:\\n\\n{body} boundary this script slices on."
    )


class _AgentSeed:
    __slots__ = ("agent_code", "agent_name", "domain", "is_active", "prompt_version", "system_prompt")

    def __init__(
        self,
        *,
        agent_code: str,
        prompt_version: str,
        domain: str,
        agent_name: str,
        system_prompt: str,
        is_active: bool,
    ) -> None:
        self.agent_code = agent_code
        self.prompt_version = prompt_version
        self.domain = domain
        self.agent_name = agent_name
        self.system_prompt = system_prompt
        self.is_active = is_active


AGENT_SEEDS: list[_AgentSeed] = [
    _AgentSeed(
        agent_code="penalty_projection_summary",
        prompt_version="v1",
        domain="penalties",
        agent_name="Penalty Projection Summary",
        system_prompt=PROJECTION_V1_PROMPT,
        is_active=True,
    ),
    _AgentSeed(
        agent_code="penalty_mitigation_summary",
        prompt_version="v1",
        domain="penalties",
        agent_name="Penalty Mitigation Summary",
        system_prompt=MITIGATION_V1_PROMPT,
        is_active=True,
    ),
    _AgentSeed(
        # Seeded for the same reason as po_validation below: the extraction graph opens a
        # process.agent_run before its first model call, so the FK needs a row already
        # there. PenaltyRuleExtractionService._ensure_registered would create it on
        # demand, but only after a first run has been attempted.
        agent_code="penalty_rule_extractor",
        prompt_version=RULE_EXTRACTION_PROMPT_VERSION,
        domain="penalties",
        agent_name="Penalty Rule Extraction",
        system_prompt=PENALTY_CLASSIFICATION_SYSTEM_PROMPT,
        is_active=True,
    ),
    _AgentSeed(
        # app.agents.penalties.rule_extraction.adapter._active_prompt reads this one by agent_code at
        # inference time; nothing else registers it.
        agent_code="penalty_rule_screening",
        prompt_version=RULE_EXTRACTION_PROMPT_VERSION,
        domain="penalties",
        agent_name="Penalty Rule Screening",
        system_prompt=SECTION_SCREENING_SYSTEM_PROMPT,
        is_active=True,
    ),
    _AgentSeed(
        agent_code="penalty_rule_classification",
        prompt_version=RULE_EXTRACTION_PROMPT_VERSION,
        domain="penalties",
        agent_name="Penalty Rule Classification",
        system_prompt=PENALTY_CLASSIFICATION_SYSTEM_PROMPT,
        is_active=True,
    ),
    _AgentSeed(
        agent_code="penalty_rule_fact_extraction",
        prompt_version=RULE_EXTRACTION_PROMPT_VERSION,
        domain="penalties",
        agent_name="Penalty Rule Fact Extraction",
        system_prompt=PENALTY_FACT_EXTRACTION_SYSTEM_PROMPT,
        is_active=True,
    ),
    _AgentSeed(
        agent_code="cmir_extractor",
        prompt_version="v1",
        domain="cmir",
        agent_name="CMIR Extractor",
        system_prompt=CMIR_EXTRACTOR_SYSTEM_PROMPT,
        is_active=True,
    ),
    _AgentSeed(
        # PO-validation has no LLM call of its own (see
        # app/services/po_validation/service.py's module docstring) --
        # this row exists so PoValidationService._ensure_registered's
        # process.agent_run.agent_id FK (NOT NULL) has something real to
        # point at, not for a system-prompt lookup. Domain is "cmir": PO
        # validation has no schema of its own and reuses `cmir` (see that
        # module's docstring, §2/§6); ck_agent_domain only allows
        # ('cmir', 'penalties').
        agent_code="po_validation",
        prompt_version="v1",
        domain="cmir",
        agent_name="PO Validation",
        system_prompt=PO_VALIDATION_SYSTEM_PROMPT,
        is_active=True,
    ),
]


def seed_agents(session: Session) -> None:
    existing_by_key = {(row.agent_code, row.prompt_version): row for row in session.scalars(select(Agent))}

    # First pass: demote every existing row and flush. uq_agent_one_active_per_code
    # is checked immediately (not deferred), so promoting a seed's active row
    # before the previously-active row for that agent_code is demoted would
    # violate the constraint mid-flush -- order-dependent otherwise.
    for row in existing_by_key.values():
        row.is_active = False
    session.flush()

    for seed in AGENT_SEEDS:
        existing = existing_by_key.get((seed.agent_code, seed.prompt_version))
        if existing is not None:
            existing.agent_name = seed.agent_name
            existing.domain = seed.domain
            existing.system_prompt = seed.system_prompt
            existing.is_active = seed.is_active
            continue
        session.add(
            Agent(
                agent_code=seed.agent_code,
                prompt_version=seed.prompt_version,
                domain=seed.domain,
                agent_name=seed.agent_name,
                system_prompt=seed.system_prompt,
                is_active=seed.is_active,
            )
        )
    session.commit()


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]  # see app/core/config/__init__.py::get_settings
    database = Database(settings.database.url)
    with database.session() as session:
        seed_agents(session)
    print(f"Seeded {len(AGENT_SEEDS)} process.agent rows.")


if __name__ == "__main__":
    main()
