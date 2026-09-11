"""The three Azure OpenAI calls the penalty rule extraction graph makes: screening,
classification, and fact extraction.

Each function opens its own scoped database session to read its active prompt, since a
`Send` fan-out can call these from more than one lane in the same run and a shared
session is not safe to use concurrently.
"""

from __future__ import annotations

from app.agents.penalties.rule_extraction.context import (
    ClauseClassificationContext,
    RuleFactContext,
    ScreeningUnitContext,
    build_classification_messages,
    build_fact_extraction_messages,
    build_screening_messages,
)
from app.agents.penalties.rule_extraction.schema import (
    CandidateClauseList,
    PenaltyFactList,
    PenaltyRuleExtraction,
)
from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.exceptions import ExternalServiceError
from app.db.session import Database
from app.repositories.process.agent_registry import AgentRegistryRepository

# One agent_code per LLM call: screening, classification and fact extraction are
# graded and versioned independently, so they cannot share one process.agent row the
# way app.services.cmir.extractor's single "cmir_extractor" code does.
_SCREENING_AGENT_CODE = "penalty_rule_screening"
_CLASSIFICATION_AGENT_CODE = "penalty_rule_classification"
_FACT_EXTRACTION_AGENT_CODE = "penalty_rule_fact_extraction"


def screen(
    client: AzureOpenAIChatClient, database: Database, context: ScreeningUnitContext
) -> CandidateClauseList:
    """Find the clauses in one screening unit that look like penalty terms."""
    messages = build_screening_messages(_active_prompt(database, _SCREENING_AGENT_CODE), context)
    return client.invoke_structured(messages, CandidateClauseList)


def classify(
    client: AzureOpenAIChatClient, database: Database, context: ClauseClassificationContext
) -> PenaltyRuleExtraction:
    """Classify one verified clause into a penalty rule."""
    messages = build_classification_messages(_active_prompt(database, _CLASSIFICATION_AGENT_CODE), context)
    return client.invoke_structured(messages, PenaltyRuleExtraction)


def extract_facts(
    client: AzureOpenAIChatClient, database: Database, context: RuleFactContext
) -> PenaltyFactList:
    """Extract the thresholds, rates, and caps one classified rule depends on."""
    messages = build_fact_extraction_messages(_active_prompt(database, _FACT_EXTRACTION_AGENT_CODE), context)
    return client.invoke_structured(messages, PenaltyFactList)


def _active_prompt(database: Database, agent_code: str) -> str:
    """The active system prompt for one stage, read fresh so a registry edit needs no deploy.

    Opens its own session rather than holding one, since a `Send` fan-out can call
    this from more than one lane in the same run and a shared session is not safe
    to use concurrently.
    """
    with database.session() as session:
        active = AgentRegistryRepository(session).get_active(agent_code)
    if active is None:
        raise ExternalServiceError(
            code="RULE_EXTRACTION_AGENT_NOT_REGISTERED",
            message=f"No active process.agent row registered for agent_code={agent_code!r}.",
        )
    return active["system_prompt"]
