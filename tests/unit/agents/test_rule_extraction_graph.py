"""Integration tests for the penalty rule extraction graph
(app.agents.penalties.rule_extraction.graph.build_graph).

Runs the full `Send` fan-out/fan-in topology end to end against the shared in-memory
SQLite `database` fixture, with a `FakeLLM` standing in for the three provider calls.
No live LLM call is ever reached. The graph ends after `stage_rules`: it never
interrupts, so every test here asserts on the final state and the staged rows, not on
a paused run.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from app.agents.penalties.rule_extraction.graph import build_graph
from app.agents.penalties.rule_extraction.nodes import RuleExtractionNodes
from app.agents.penalties.rule_extraction.schema import (
    CandidateClause,
    CandidateClauseList,
    PenaltyFact,
    PenaltyFactList,
    PenaltyRuleExtraction,
)
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule_extraction import ExtractedPenaltyRuleRepository
from app.repositories.process.agent_registry import AgentRegistryRepository, AgentRunRepository

CLAUSE_TEXT = "Retailer may assess a $50 fee per short-shipped case."
RETAILER_AGREEMENT_WITH_CLAUSE = f"## Shortages\n{CLAUSE_TEXT}\n"
RETAILER_AGREEMENT_WITHOUT_CLAUSE = "## Definitions\nThis agreement is between the parties.\n"


class FakeTraceRepo:
    """No-op double for `AgentTraceRepository`; the graph traces every node through it."""

    def log(self, *args, **kwargs):
        pass


class FakeLLM:
    """Test double for `RuleExtractionLLM`, driven by one fixed screen/classify/facts result."""

    def __init__(self, *, has_clause: bool) -> None:
        self._has_clause = has_clause

    def screen(self, context):
        if not self._has_clause:
            return CandidateClauseList(clauses=[])
        return CandidateClauseList(
            clauses=[
                CandidateClause(
                    section_title="Shortages",
                    excerpt=CLAUSE_TEXT,
                    reason="short shipment leads to a fee",
                )
            ]
        )

    def classify(self, context):
        return PenaltyRuleExtraction(
            is_penalty_rule=True,
            penalty_category="SHORT_SHIP",
            calc_type="PER_UNIT",
            economic_effect_type="CHARGEBACK",
            confidence=0.9,
            plain_explanation="Vendor short-ships; retailer charges a flat fee per case.",
        )

    def extract_facts(self, context):
        return PenaltyFactList(
            facts=[
                PenaltyFact(
                    attribute_role="RATE",
                    basis_type="UNIT_COST",
                    value=50.0,
                    value_status="PRESENT",
                    source_text=CLAUSE_TEXT,
                    confidence=0.9,
                )
            ]
        )


def _make_retailer_agreement(db_session, sha256: str, markdown_text: str):
    retailer_id = MasterDataRepository(db_session).add_retailer(f"R{sha256[:6]}", "Retailer", None, "SUM")[
        "id"
    ]
    return RetailerAgreementRepository(db_session).add_retailer_agreement(
        retailer_id=retailer_id,
        contract_code=f"C-{sha256[:8]}",
        title="Example Retailer Agreement",
        document_sha256=sha256,
        markdown_text=markdown_text,
    )


def _make_agent_run(db_session, code: str):
    agent_id = AgentRegistryRepository(db_session).ensure_registered(
        agent_code=code,
        prompt_version="v1",
        system_prompt="Extract penalty clauses from retailer agreement markdown.",
        agent_name="Test Rule Extraction",
        domain="penalties",
    )
    return AgentRunRepository(db_session).start(agent_id=agent_id, run_type="PENALTY_RULE_EXTRACTION")


def _build_graph(database, *, has_clause: bool):
    fake_llm = FakeLLM(has_clause=has_clause)
    nodes = RuleExtractionNodes(
        screen=fake_llm.screen,
        classify=fake_llm.classify,
        extract_facts=fake_llm.extract_facts,
        database=database,
    )
    return build_graph(nodes, MemorySaver(), FakeTraceRepo())


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def test_graph_ends_at_stage_rules_with_no_review_nodes(database):
    graph = _build_graph(database, has_clause=True)
    node_names = set(graph.get_graph().nodes)

    assert "human_review" not in node_names
    assert "apply_decisions" not in node_names
    assert "stage_rules" in node_names


def test_a_run_with_no_candidate_clauses_stages_nothing_and_completes(database, db_session):
    retailer_agreement = _make_retailer_agreement(db_session, "1" * 64, RETAILER_AGREEMENT_WITHOUT_CLAUSE)
    run_id = _make_agent_run(db_session, "penalty_rule_extractor_1")
    db_session.commit()

    graph = _build_graph(database, has_clause=False)
    state = graph.invoke(
        {
            "retailer_agreement_id": retailer_agreement["id"],
            "run_id": run_id,
            "retailer_agreement_text": RETAILER_AGREEMENT_WITHOUT_CLAUSE,
        },
        config=_config("empty-run"),
    )

    assert state["staged_rule_ids"] == []
    with database.session() as session:
        assert (
            ExtractedPenaltyRuleRepository(session).list_for_retailer_agreement(retailer_agreement["id"])
            == []
        )


def test_a_candidate_clause_is_staged_as_pending_review_and_the_run_ends(database, db_session):
    retailer_agreement = _make_retailer_agreement(db_session, "2" * 64, RETAILER_AGREEMENT_WITH_CLAUSE)
    run_id = _make_agent_run(db_session, "penalty_rule_extractor_2")
    db_session.commit()

    graph = _build_graph(database, has_clause=True)
    state = graph.invoke(
        {
            "retailer_agreement_id": retailer_agreement["id"],
            "run_id": run_id,
            "retailer_agreement_text": RETAILER_AGREEMENT_WITH_CLAUSE,
        },
        config=_config("staged-run"),
    )

    [staged_rule_id] = state["staged_rule_ids"]
    with database.session() as session:
        [staged] = ExtractedPenaltyRuleRepository(session).list_for_retailer_agreement(
            retailer_agreement["id"]
        )
        assert staged.id == staged_rule_id
        assert staged.status == "PENDING_REVIEW"
