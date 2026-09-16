"""Tests for ProjectionSummaryService, using a hand-written fake chat
client instead of a LangChain mock.

Was against `FineProjectionSummaryService` (business-string `order_id`,
per-`prompt_version` caching via a separate `projection_summary` table);
rewritten against `ProjectionSummaryService` and the merged
`penalties.penalty_summary` table, keyed by `(purchase_order_id,
summary_type, as_of_date)` -- `agent_id` is now a reuse-eligibility filter,
not a cache key component, so `test_cache_miss_on_prompt_version_bump`
(a scenario that can no longer occur) is dropped rather than adapted; see
`app/repositories/penalties/summary.py`'s module docstring. Was
`tests/unit/services/test_fine_projection_summary.py` (`fine`/`fines` ->
`penalty`/`penalties` rename).
"""

from contextlib import suppress
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from langchain_core.messages import ToolMessage

from app.agents.penalties.projection import PenaltyProjectionSummaryOutput
from app.agents.penalties.projection.prompts.v1 import PROMPT_VERSION
from app.core.exceptions import BusinessRuleError, ExternalServiceError, NotFoundError, ValidationError
from app.models.enums import SummaryType
from app.services.penalties.projection import ProjectionResult, ViolationProjection
from app.services.penalties.projection.service import ProjectionService
from app.services.penalties.projection.summary_service import ProjectionSummaryService
from tests.conftest import make_retailer_agreement


class _FakeAIMessage:
    def __init__(self, content: str | None, tool_calls: list[dict]) -> None:
        self.content = content
        self.tool_calls = tool_calls


class FakeChatClient:
    """`tool_call_plan` is a list of tool-call lists, one per round the loop
    offers tools for -- an empty list means "no more calls, give the final
    answer". `final_content` is the tools-less final round's return value;
    `None` simulates the model returning nothing (a failure case)."""

    model_name = "fake-model"

    def __init__(
        self,
        tool_call_plan: list[list[dict]] | None = None,
        final_content: str | None = "Fine, all clear.",
        fail_invoke_on_round: int | None = None,
    ):
        self.tool_call_plan = tool_call_plan or []
        self.final_content = final_content
        # Index into ALL invoke() calls, not just tool-offering ones -- lets
        # a test target a provider failure on any specific round.
        self.fail_invoke_on_round = fail_invoke_on_round
        self.invocations: list[dict] = []
        self._optional_round = 0

    def invoke(self, messages, *, tools=None):
        invocation_index = len(self.invocations)
        self.invocations.append({"messages": messages, "tools": tools})
        if invocation_index == self.fail_invoke_on_round:
            raise RuntimeError("simulated upstream failure (e.g. openai.APITimeoutError)")

        if not tools:
            return _FakeAIMessage(content=self.final_content, tool_calls=[])

        calls = (
            self.tool_call_plan[self._optional_round]
            if self._optional_round < len(self.tool_call_plan)
            else []
        )
        self._optional_round += 1
        return _FakeAIMessage(content=None, tool_calls=calls)


def _seed_flat_rule_order(repos, po_number: str = "ORD-EXP"):
    """A flat-rule PO with one OTIF_LATE/FLAT_FEE violation, no actual
    penalties. Returns (purchase_order_id, retailer_id, rule_id)."""
    retailer = repos.master_data.add_retailer(f"RET-{po_number}", "Retailer Exp", None, "SUM")
    material = repos.master_data.add_material(f"MAT-{po_number}", None)
    plant = repos.master_data.add_plant(f"PLANT-{po_number}", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number=po_number,
        retailer_id=retailer["id"],
        order_date=date(2026, 8, 1),
        requested_delivery_date=date(2026, 8, 10),
        required_ship_date=date(2026, 8, 8),
    )
    repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=1000,
        unit_price=10.0,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    rule = repos.penalty_rules.add_rule(
        rule_code=f"RULE-{po_number}-FLAT",
        retailer_id=retailer["id"],
        violation_type="OTIF_LATE",
        penalty_category="OTIF_LATE",
        retailer_agreement_id=make_retailer_agreement(repos, retailer["id"]),
        calc_type="FLAT_FEE",
        rate=50.0,
    )
    repos.penalty_projections.save_result(
        purchase_order["id"],
        ProjectionResult(
            order_id=str(purchase_order["id"]),
            projection_date=date(2026, 8, 5),
            days_to_delivery=3,
            shortage_probability=0.05,
            delay_probability=0.05,
            violations=[
                ViolationProjection(
                    violation_type="OTIF_LATE",
                    rule_id=str(rule["id"]),
                    probability=0.05,
                    penalty_amount=50.0,
                    expected_penalty_amount=2.5,
                )
            ],
            total_expected_penalty_amount=2.5,
            stacking_mode="SUM",
        ),
    )
    return purchase_order["id"], retailer["id"], rule["id"]


def _seed_identical_projection(
    repos,
    purchase_order_id,
    rule_id,
    projection_date: date,
    *,
    days_to_delivery: int = 0,
) -> None:
    """Same violations/total/stacking as _seed_flat_rule_order's own
    projection -- with no confirmation/production/appointment rows ever
    recorded, ProjectionService.build_snapshot returns identical defaults
    for any date, so together this produces a content_fingerprint
    identical to the one _seed_flat_rule_order's day produces.
    `days_to_delivery` defaults to a value deliberately different from
    _seed_flat_rule_order's (3) -- it's excluded from the fingerprint, so
    that alone must never gate reuse."""
    repos.penalty_projections.save_result(
        purchase_order_id,
        ProjectionResult(
            order_id=str(purchase_order_id),
            projection_date=projection_date,
            days_to_delivery=days_to_delivery,
            shortage_probability=0.05,
            delay_probability=0.05,
            violations=[
                ViolationProjection(
                    violation_type="OTIF_LATE",
                    rule_id=str(rule_id),
                    probability=0.05,
                    penalty_amount=50.0,
                    expected_penalty_amount=2.5,
                )
            ],
            total_expected_penalty_amount=2.5,
            stacking_mode="SUM",
        ),
    )


def _seed_different_projection(repos, purchase_order_id, rule_id, projection_date: date) -> None:
    """A projection whose outputs genuinely differ from
    _seed_flat_rule_order's day -- a different content_fingerprint."""
    repos.penalty_projections.save_result(
        purchase_order_id,
        ProjectionResult(
            order_id=str(purchase_order_id),
            projection_date=projection_date,
            days_to_delivery=0,
            shortage_probability=0.55,
            delay_probability=0.55,
            violations=[
                ViolationProjection(
                    violation_type="OTIF_LATE",
                    rule_id=str(rule_id),
                    probability=0.55,
                    penalty_amount=50.0,
                    expected_penalty_amount=27.5,
                )
            ],
            total_expected_penalty_amount=27.5,
            stacking_mode="SUM",
        ),
    )


def _enable_reuse(monkeypatch, *, max_reuse_days: int = 7) -> None:
    monkeypatch.setattr(
        "app.services.penalties._summary_base.get_settings",
        lambda: SimpleNamespace(
            summary=SimpleNamespace(reuse_enabled=True, max_reuse_days=max_reuse_days),
            job_queue=SimpleNamespace(max_attempts=5),
        ),
    )


def _build_service(repos, fake_llm) -> ProjectionSummaryService:
    projection_service = ProjectionService(
        purchase_orders=repos.purchase_orders,
        fulfillment=repos.fulfillment,
        rules=repos.penalty_rules,
        master_data=repos.master_data,
        projections=repos.penalty_projections,
    )
    return ProjectionSummaryService(
        purchase_orders=repos.purchase_orders,
        summaries=repos.penalty_summaries,
        agent_registry=repos.agent_registry,
        job_queue=repos.job_queue,
        job_context=repos.penalty_job_item_context,
        llm=fake_llm,
        rules=repos.penalty_rules,
        master_data=repos.master_data,
        projections=repos.penalty_projections,
        actual_penalties=repos.actual_penalties,
        projection_service=projection_service,
    )


def _schedule_and_run(
    service: ProjectionSummaryService,
    purchase_order_id,
    as_of_date: date | None = None,
    force_regenerate: bool = False,
):
    """Runs get_or_schedule, then run_generation inline on a PENDING result --
    what a background worker does, minus an actual background thread.

    run_generation re-raises on failure (see the "run_generation re-raises
    on failure" tests below) after persisting the FAILED ledger row --
    swallow it here exactly as the intended job-runner guard does, since
    this helper's callers care about the resulting ledger state (via
    get_status), not about propagating the exception themselves.
    """
    job = service.get_or_schedule(purchase_order_id, as_of_date=as_of_date, force_regenerate=force_regenerate)
    if job.status != "PENDING":
        return job
    with suppress(Exception):
        service.run_generation(job.purchase_order_id, job.as_of_date)
    return service.get_status(job.purchase_order_id, as_of_date=job.as_of_date)


def test_order_not_found_raises_order_not_found_error(repos):
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)
    missing_id = uuid4()

    with pytest.raises(NotFoundError, match=str(missing_id)):
        service.get_or_schedule(missing_id)


def test_no_projection_exists_error_when_no_projected_penalty_rows(repos):
    retailer = repos.master_data.add_retailer("RET-EMPTY", "Retailer Empty", None, "SUM")
    material = repos.master_data.add_material("MAT-EMPTY", None)
    plant = repos.master_data.add_plant("PLANT-EMPTY", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number="ORD-EMPTY",
        retailer_id=retailer["id"],
        order_date=date(2026, 8, 1),
        requested_delivery_date=date(2026, 8, 10),
        required_ship_date=date(2026, 8, 8),
    )
    repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=100,
        unit_price=5.0,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    with pytest.raises(BusinessRuleError, match=str(purchase_order["id"])):
        service.get_or_schedule(purchase_order["id"])


def test_get_or_schedule_returns_pending_and_persists_a_pending_row_on_cache_miss(repos):
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    job = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))

    assert job.status == "PENDING"
    assert job.output is None
    # The LLM must never be called from get_or_schedule itself -- only
    # run_generation (the background job) calls it.
    assert fake_llm.invocations == []

    persisted = repos.penalty_summaries.get_by_key(
        purchase_order_id, SummaryType.PROJECTION, date(2026, 8, 5)
    )
    assert persisted["status"] == "PENDING"
    assert persisted["summary"] is None
    assert persisted["model_name"] is None


def test_mandatory_context_always_present(repos):
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    job = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))

    assert job.status == "READY"
    # 1 optional round (breaks immediately, empty tool_call_plan) + 1 final round.
    assert len(fake_llm.invocations) == 2
    content = fake_llm.invocations[0]["messages"][1].content
    assert content.startswith("<DATA>")
    assert content.endswith("</DATA>")
    for expected_key in (
        "order",
        "current_projection_date",
        "stacking_mode",
        "active_rules",
        "daily_history",
        "shared_production_line",
        "other_open_orders_same_sku_location",
    ):
        assert f'"{expected_key}"' in content


def test_daily_history_is_bounded_to_as_of_date_not_the_full_table(repos):
    """A later projection_date row for the same PO must never leak into a
    summary requested for an earlier as_of_date -- otherwise "today" is
    ambiguous and the response can describe events from the future."""
    purchase_order_id, _, rule_id = _seed_flat_rule_order(repos)
    repos.penalty_projections.save_result(
        purchase_order_id,
        ProjectionResult(
            order_id=str(purchase_order_id),
            projection_date=date(2026, 8, 9),
            days_to_delivery=0,
            shortage_probability=0.92,
            delay_probability=0.92,
            violations=[
                ViolationProjection(
                    violation_type="OTIF_LATE",
                    rule_id=str(rule_id),
                    probability=0.92,
                    penalty_amount=50.0,
                    expected_penalty_amount=46.0,
                )
            ],
            total_expected_penalty_amount=46.0,
            stacking_mode="SUM",
        ),
    )
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))

    content = fake_llm.invocations[0]["messages"][1].content
    assert '"2026-08-05"' in content
    assert '"2026-08-09"' not in content
    assert "46.0" not in content


def test_optional_tools_never_called_when_unrequested(repos):
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    with patch.object(
        repos.actual_penalties,
        "list_for_purchase_order",
        wraps=repos.actual_penalties.list_for_purchase_order,
    ) as spy_penalties:
        _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))

    # list_for_purchase_order is called by mandatory-context assembly itself
    # only when order_status == DELIVERED (not here) -- proves the optional
    # actual-penalties tool was genuinely never reached, not just unobserved.
    spy_penalties.assert_not_called()
    # The model was offered tools on its one optional round and chose not
    # to use any (empty tool_call_plan); the final round never offers tools.
    assert len(fake_llm.invocations) == 2
    assert fake_llm.invocations[0]["tools"]
    assert fake_llm.invocations[1]["tools"] is None


def test_cache_hit_skips_llm(repos):
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    first = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))
    assert first.status == "READY"
    assert len(fake_llm.invocations) == 2

    second = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))

    # Cache hit: READY immediately, no new LLM call, no PENDING row created.
    assert second.status == "READY"
    assert len(fake_llm.invocations) == 2
    assert second.output == first.output


def test_force_regenerate_skips_the_cache_check_and_calls_the_llm(repos):
    """force_regenerate=True skips get_cached entirely -- shown here against
    a date with no prior cached row, so the LLM call is unambiguously
    because of force_regenerate, not an incidental cache miss."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    job = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5), force_regenerate=True)

    assert job.status == "READY"
    assert len(fake_llm.invocations) == 2


def test_force_regenerate_against_an_already_cached_key_replaces_the_row_in_place(repos, db_session):
    """force_regenerate=True against an exact-match key re-arms the existing
    row to PENDING rather than raising -- the new output is returned, and
    there's still exactly one row under that key afterwards, not two."""
    from sqlalchemy import func, select

    from app.models import PenaltySummary

    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient(final_content="Flat $2.50 expected delay penalty.")
    service = _build_service(repos, fake_llm)

    first_job = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))
    assert first_job.status == "READY"

    fake_llm.final_content = "Updated: carrier reliability improved, penalty now $0."

    result = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5), force_regenerate=True)

    assert result.status == "READY"
    assert result.output.summary == fake_llm.final_content
    # 2 invocations per full generation (1 optional round + 1 final), x2 generations.
    assert len(fake_llm.invocations) == 4

    row_count = db_session.scalar(
        select(func.count())
        .select_from(PenaltySummary)
        .where(
            PenaltySummary.purchase_order_id == purchase_order_id,
            PenaltySummary.summary_type == SummaryType.PROJECTION,
            PenaltySummary.as_of_date == date(2026, 8, 5),
        )
    )
    assert row_count == 1

    cached = repos.penalty_summaries.get_cached(purchase_order_id, SummaryType.PROJECTION, date(2026, 8, 5))
    assert cached["summary"] == fake_llm.final_content


# Note: `test_cache_miss_on_prompt_version_bump` (old suite) is dropped --
# `penalty_summary`'s cache key is `(purchase_order_id, summary_type,
# as_of_date)` only; agent_id/prompt_version are no longer part of the
# uniqueness constraint, so there is no "stale prompt_version row" case
# left to reproduce. See app/repositories/penalties/summary.py's docstring.


def test_as_of_date_in_the_future_is_rejected(repos):
    """Closes the unbounded-cache-key abuse vector: as_of_date is
    client-supplied, so it must be bounded to real projection history
    rather than accepted verbatim as a fresh cache key."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    with pytest.raises(ValidationError, match="future"):
        service.get_or_schedule(purchase_order_id, as_of_date=date(2099, 1, 1))

    # Rejected before any LLM call or cache write happens.
    assert fake_llm.invocations == []
    assert (
        repos.penalty_summaries.get_cached(purchase_order_id, SummaryType.PROJECTION, date(2099, 1, 1))
        is None
    )


def test_as_of_date_before_earliest_projection_is_rejected(repos):
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    with pytest.raises(ValidationError, match="earliest"):
        service.get_or_schedule(purchase_order_id, as_of_date=date(2020, 1, 1))

    assert fake_llm.invocations == []


def test_as_of_date_validation_is_not_bypassed_by_force_regenerate(repos):
    """The abuse path the fix closes: varying as_of_date with
    force_regenerate=True to always miss cache and trigger a real LLM
    call. force_regenerate must not skip this check."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    with pytest.raises(ValidationError):
        service.get_or_schedule(purchase_order_id, as_of_date=date(2099, 1, 1), force_regenerate=True)

    assert fake_llm.invocations == []


def test_get_actual_penalties_for_purchase_order_tool_cannot_be_pointed_at_a_different_order(repos):
    """A model supplying a different purchase_order_id in the tool call
    must still get actual penalties for the PO under summarization --
    GetActualPenaltiesForPurchaseOrderInput doesn't accept an id field at all."""
    purchase_order_id, retailer_id, _ = _seed_flat_rule_order(repos)
    # get_actual_penalties_for_purchase_order is now DELIVERED-only server-side.
    repos.purchase_orders.set_order_status(purchase_order_id, "DELIVERED")
    other_material = repos.master_data.add_material("MAT-OTHER", None)
    other_plant = repos.master_data.add_plant("PLANT-OTHER", None, None)
    other_po = repos.purchase_orders.create_purchase_order(
        purchase_order_number="ORD-OTHER",
        retailer_id=retailer_id,
        order_date=date(2026, 8, 1),
        requested_delivery_date=date(2026, 8, 10),
        required_ship_date=date(2026, 8, 8),
    )
    repos.purchase_orders.add_line(
        purchase_order_id=other_po["id"],
        line_number="10",
        ordered_quantity=500,
        unit_price=10.0,
        material_id=other_material["id"],
        plant_id=other_plant["id"],
    )

    tool_call_plan = [
        [
            {
                "id": "call-1",
                "name": "get_actual_penalties_for_purchase_order",
                "args": {"order_id": str(other_po["id"])},
            }
        ]
    ]
    fake_llm = FakeChatClient(tool_call_plan=tool_call_plan)
    service = _build_service(repos, fake_llm)

    with patch.object(
        repos.actual_penalties,
        "list_for_purchase_order",
        wraps=repos.actual_penalties.list_for_purchase_order,
    ) as spy_penalties:
        _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))

    # Called both by mandatory-context assembly (order is DELIVERED, so
    # actual_outcomes is populated directly) and by the tool call itself --
    # every call must still resolve to the enclosing PO, never the
    # hallucinated other_po id from the tool call args (the tool's own
    # input schema has no such field at all).
    assert spy_penalties.call_count >= 1
    for call in spy_penalties.call_args_list:
        assert call.args == (purchase_order_id,)


def test_get_tier_bands_for_rule_tool_cannot_be_pointed_at_a_different_retailers_rule(repos):
    """A model pointing at a different retailer's TIERED rule_id must get
    the same "not found" result an invalid rule_id would -- rule_id is
    resolved only within the enclosing order's own retailer, never a
    system-wide scan, so another retailer's rate card never leaks."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    other_retailer = repos.master_data.add_retailer("RET-OTHER", "Retailer Other", None, "SUM")
    other_rule = repos.penalty_rules.add_rule(
        rule_code="RULE-OTHER-TIERED",
        retailer_id=other_retailer["id"],
        violation_type="SHORT_SHIP",
        penalty_category="SHORT_SHIP",
        retailer_agreement_id=make_retailer_agreement(repos, other_retailer["id"]),
        calc_type="TIERED",
        rate=0.0,
        threshold_pct=0.0,
        tiers=[
            {"band_min": 0.0, "band_max": 0.1, "rate": 0.02},
            {"band_min": 0.1, "band_max": 1.0, "rate": 0.05},
        ],
    )

    tool_call_plan = [
        [{"id": "call-1", "name": "get_tier_bands_for_rule", "args": {"rule_id": str(other_rule["id"])}}]
    ]
    fake_llm = FakeChatClient(tool_call_plan=tool_call_plan)
    service = _build_service(repos, fake_llm)

    _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))

    tool_messages = [m for m in fake_llm.invocations[-1]["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert '"found": false' in tool_messages[0].content
    assert str(other_rule["id"]) in tool_messages[0].content
    # The other retailer's rate-card data must never reach the model.
    assert "0.02" not in tool_messages[0].content
    assert "0.05" not in tool_messages[0].content


def test_bounded_loop_marks_the_job_failed_after_max_tool_rounds(repos):
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    # Every optional round asks for the same tool, forever -- the model
    # never settles down to a final answer within the round budget.
    always_call_a_tool = [
        [{"id": f"call-{i}", "name": "get_carrier_reliability_detail", "args": {"carrier_id": "CAR-EXP"}}]
        for i in range(3)
    ]
    fake_llm = FakeChatClient(tool_call_plan=always_call_a_tool, final_content=None)
    service = _build_service(repos, fake_llm)

    job = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))

    # run_generation never raises -- a background job with nothing
    # awaiting it must persist the failure, not throw it away.
    assert job.status == "FAILED"
    assert job.error_message == "Penalty projection summary generation failed upstream"
    # 3 optional tool-calling rounds + the 1 final (empty-content) round.
    assert len(fake_llm.invocations) == 4


def test_provider_failure_on_an_early_tool_round_results_in_a_failed_job_not_a_crash(repos):
    """Regression test found live against a real deployment: a transient
    provider failure on the FIRST optional round used to propagate as a
    raw exception instead of landing as a client-safe FAILED status."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient(fail_invoke_on_round=0)
    service = _build_service(repos, fake_llm)

    job = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))

    assert job.status == "FAILED"
    assert job.error_message == "Penalty projection summary generation failed upstream"
    # The raw exception text must never reach the persisted, client-facing
    # error_message -- only the generic client-safe message does.
    assert "simulated upstream failure" not in job.error_message
    # Failed on the very first invocation -- never even reached the final round.
    assert len(fake_llm.invocations) == 1


def test_force_regenerate_against_a_failed_row_can_recover(repos):
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient(fail_invoke_on_round=0)
    service = _build_service(repos, fake_llm)

    first = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))
    assert first.status == "FAILED"

    fake_llm.fail_invoke_on_round = None
    recovered = _schedule_and_run(
        service, purchase_order_id, as_of_date=date(2026, 8, 5), force_regenerate=True
    )

    assert recovered.status == "READY"
    assert recovered.error_message is None


def test_get_status_raises_when_no_job_was_ever_scheduled(repos):
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    with pytest.raises(NotFoundError, match=str(purchase_order_id)):
        service.get_status(purchase_order_id, as_of_date=date(2026, 8, 5))


def test_get_status_reports_pending_before_generation_runs(repos):
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))
    job = service.get_status(purchase_order_id, as_of_date=date(2026, 8, 5))

    assert job.status == "PENDING"
    assert job.output is None
    assert job.error_message is None


def test_ready_output_reconstructed_from_the_rows_own_columns(repos):
    """PenaltyProjectionSummaryOutput is rebuilt from the row's own columns, never
    parsed out of the persisted summary text (it's plain text, not JSON)."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient(final_content="The current total is $2.50 because of a flat OTIF fee.")
    service = _build_service(repos, fake_llm)

    job = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))

    assert job.status == "READY"
    assert isinstance(job.output, PenaltyProjectionSummaryOutput)
    assert job.output.order_id == str(purchase_order_id)
    assert job.output.as_of_date == date(2026, 8, 5)
    assert job.output.prompt_version == PROMPT_VERSION
    assert job.output.model_name == "fake-model"
    assert job.output.summary == "The current total is $2.50 because of a flat OTIF fee."


# ---------------------------------------------------------------------------
# get_status nearest-prior-date fallback (repository + service)
# ---------------------------------------------------------------------------


def test_get_latest_not_after_returns_the_prior_ready_row(repos):
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)
    _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))

    found = repos.penalty_summaries.get_latest_not_after(
        purchase_order_id, SummaryType.PROJECTION, date(2026, 8, 6)
    )

    assert found is not None
    assert found["as_of_date"] == date(2026, 8, 5)
    assert found["status"] == "READY"


def test_get_latest_not_after_also_returns_a_still_pending_row(repos):
    """Status-agnostic on purpose -- a PENDING row (never picked up by a
    worker) dated before as_of_date must still be surfaced as PENDING, not
    treated as if no job exists just because it never reached READY."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)
    # PENDING only -- never generated, so nothing READY exists yet.
    service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))

    found = repos.penalty_summaries.get_latest_not_after(
        purchase_order_id, SummaryType.PROJECTION, date(2026, 8, 6)
    )

    assert found is not None
    assert found["as_of_date"] == date(2026, 8, 5)
    assert found["status"] == "PENDING"


def test_get_latest_not_after_never_looks_ahead(repos):
    """A row dated after the requested date must never be returned -- this
    fallback is nearest-prior-date only, not nearest overall."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)
    _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 10))

    found = repos.penalty_summaries.get_latest_not_after(
        purchase_order_id, SummaryType.PROJECTION, date(2026, 8, 5)
    )

    assert found is None


def test_get_status_exact_match_wins_over_fallback(repos):
    """A PENDING row dated exactly on the requested date must be reported
    as-is -- a caller polling an in-flight job must see its real status,
    not get silently redirected to an older READY summary."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)
    _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))
    service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 6))  # left PENDING, not run

    job = service.get_status(purchase_order_id, as_of_date=date(2026, 8, 6))

    assert job.status == "PENDING"
    assert job.as_of_date == date(2026, 8, 6)
    assert job.output is None


def test_get_status_falls_back_to_the_nearest_prior_ready_summary(repos):
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)
    _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))

    job = service.get_status(purchase_order_id, as_of_date=date(2026, 8, 6))

    assert job.status == "READY"
    # Honest date: the row this actually came from, not the requested date.
    assert job.as_of_date == date(2026, 8, 5)
    assert job.output is not None
    assert job.output.as_of_date == date(2026, 8, 5)


def test_get_status_falls_back_to_a_still_pending_prior_job_not_just_ready(repos):
    """Real bug this guards against: a job requested on an earlier day that
    a worker never picked up (still PENDING) must still be found and
    reported as PENDING when polled on a later day with no as_of_date
    override -- not silently treated as "no job exists" just because it
    never reached READY."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)
    service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))  # left PENDING, never run

    job = service.get_status(purchase_order_id, as_of_date=date(2026, 8, 6))

    assert job.status == "PENDING"
    assert job.as_of_date == date(2026, 8, 5)
    assert job.output is None


def test_get_status_still_raises_when_no_row_exists_at_or_before(repos):
    """A row exists, but only after the requested date -- the fallback
    must not look ahead, so this is still a genuine 404, regardless of
    that later row's status."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)
    _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 10))

    with pytest.raises(NotFoundError, match=str(purchase_order_id)):
        service.get_status(purchase_order_id, as_of_date=date(2026, 8, 5))


# ---------------------------------------------------------------------------
# Reuse (settings.summary.reuse_enabled)
# ---------------------------------------------------------------------------


def test_reuse_disabled_by_default_makes_a_fresh_llm_call(repos):
    """Reuse is opt-in (summary_reuse_enabled defaults to False) -- a
    second day with an identical content_fingerprint must still generate
    a fresh summary when the setting is untouched."""
    purchase_order_id, _, rule_id = _seed_flat_rule_order(repos)
    _seed_identical_projection(repos, purchase_order_id, rule_id, date(2026, 8, 6))
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))
    assert len(fake_llm.invocations) == 2

    second = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 6))

    assert second.status == "READY"
    assert len(fake_llm.invocations) == 4  # a second, fresh generation ran
    assert second.output.is_reused is False


def test_reuse_enabled_with_matching_fingerprint_reuses_without_an_llm_call(repos, monkeypatch):
    _enable_reuse(monkeypatch)
    purchase_order_id, _, rule_id = _seed_flat_rule_order(repos)
    _seed_identical_projection(repos, purchase_order_id, rule_id, date(2026, 8, 6))
    fake_llm = FakeChatClient(final_content="Flat $2.50 expected OTIF fee, unchanged.")
    service = _build_service(repos, fake_llm)

    first = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))
    assert first.status == "READY"
    invocations_after_first = len(fake_llm.invocations)

    job = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 6))

    # READY immediately -- no PENDING step, no new LLM call.
    assert job.status == "READY"
    assert len(fake_llm.invocations) == invocations_after_first
    assert job.output.summary == first.output.summary
    assert job.output.is_reused is True
    assert job.output.generated_for_date == date(2026, 8, 5)
    assert job.output.unchanged_since == date(2026, 8, 5)
    assert job.output.unchanged_for_days == 1


def test_reuse_outside_the_window_does_not_reuse(repos, monkeypatch):
    _enable_reuse(monkeypatch, max_reuse_days=1)
    purchase_order_id, _, rule_id = _seed_flat_rule_order(repos)
    # 3 days later, past the 1-day window, despite an identical fingerprint.
    _seed_identical_projection(repos, purchase_order_id, rule_id, date(2026, 8, 8))
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))
    invocations_after_first = len(fake_llm.invocations)

    job = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 8))

    assert job.status == "PENDING"  # no reuse hit -- scheduled fresh instead
    assert len(fake_llm.invocations) == invocations_after_first


def test_reuse_with_a_changed_fingerprint_does_not_reuse(repos, monkeypatch):
    _enable_reuse(monkeypatch)
    purchase_order_id, _, rule_id = _seed_flat_rule_order(repos)
    _seed_different_projection(repos, purchase_order_id, rule_id, date(2026, 8, 6))
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))
    invocations_after_first = len(fake_llm.invocations)

    job = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 6))

    assert job.status == "PENDING"
    assert len(fake_llm.invocations) == invocations_after_first


def test_force_regenerate_bypasses_reuse_entirely(repos, monkeypatch):
    _enable_reuse(monkeypatch)
    purchase_order_id, _, rule_id = _seed_flat_rule_order(repos)
    _seed_identical_projection(repos, purchase_order_id, rule_id, date(2026, 8, 6))
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))
    invocations_after_first = len(fake_llm.invocations)

    job = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 6), force_regenerate=True)

    # force_regenerate skips reuse (and the cache) just like it always
    # has -- straight to a fresh PENDING job.
    assert job.status == "PENDING"
    assert len(fake_llm.invocations) == invocations_after_first

    service.run_generation(job.purchase_order_id, job.as_of_date)
    assert len(fake_llm.invocations) > invocations_after_first


def test_reuse_chain_preserves_the_original_source_as_of_date(repos, monkeypatch):
    """Reusing FROM an already-reused row must still record the TRUE
    origin date, not the date of the row that was actually matched --
    otherwise a long chain of reuses would drift its "unchanged since"
    date forward by one hop every time."""
    _enable_reuse(monkeypatch, max_reuse_days=30)
    purchase_order_id, _, rule_id = _seed_flat_rule_order(repos)
    _seed_identical_projection(repos, purchase_order_id, rule_id, date(2026, 8, 6))
    _seed_identical_projection(repos, purchase_order_id, rule_id, date(2026, 8, 7))
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))
    invocations_after_first = len(fake_llm.invocations)

    day2 = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 6))
    assert day2.status == "READY"
    assert day2.output.unchanged_since == date(2026, 8, 5)

    day3 = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 7))

    assert day3.status == "READY"
    assert len(fake_llm.invocations) == invocations_after_first  # still no new LLM call
    assert day3.output.unchanged_since == date(2026, 8, 5)  # the ORIGIN, not day2
    assert day3.output.unchanged_for_days == 2


# ---------------------------------------------------------------------------
# run_generation re-raises on failure
# ---------------------------------------------------------------------------


def test_run_generation_reraises_after_persisting_the_failed_ledger_row(repos):
    """The worker-blocking fix: a caller that awaits run_generation
    directly must be able to tell success from failure, so a background
    worker can classify retry-vs-dead -- the old contract (swallow and
    return None) made every failure look identical to success."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient(fail_invoke_on_round=0)
    service = _build_service(repos, fake_llm)

    job = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))
    assert job.status == "PENDING"

    with pytest.raises(ExternalServiceError):
        service.run_generation(job.purchase_order_id, job.as_of_date)

    # The FAILED ledger row was still written before the re-raise.
    persisted = repos.penalty_summaries.get_by_key(
        purchase_order_id, SummaryType.PROJECTION, date(2026, 8, 5)
    )
    assert persisted["status"] == "FAILED"
    assert persisted["error_message"] == "Penalty projection summary generation failed upstream"


def test_run_generation_reraise_leaves_ledger_write_failure_handling_intact(repos):
    """_safe_mark_failed's own guarantee (a ledger-write failure never
    masks the original error) still holds after the re-raise change."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient(fail_invoke_on_round=0)
    service = _build_service(repos, fake_llm)

    job = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))

    with (
        patch.object(repos.penalty_summaries, "mark_failed", side_effect=RuntimeError("db is down")),
        pytest.raises(ExternalServiceError, match="Penalty projection summary generation failed upstream"),
    ):
        service.run_generation(job.purchase_order_id, job.as_of_date)


def test_dependencies_job_runner_swallows_the_reraise_and_still_marks_failed(repos):
    """Regression test for the intended job-runner guard: it must absorb
    run_generation's re-raise (a background job has no retry contract of
    its own) while the ledger row it reads back is already FAILED."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient(fail_invoke_on_round=0)
    service = _build_service(repos, fake_llm)

    job = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))

    def _run(purchase_order_id, as_of_date: date) -> None:
        """The exact guard shape the intended job-runner uses."""
        with suppress(Exception):
            service.run_generation(purchase_order_id, as_of_date)

    _run(job.purchase_order_id, job.as_of_date)  # must not raise

    status = service.get_status(purchase_order_id, as_of_date=date(2026, 8, 5))
    assert status.status == "FAILED"


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_is_invoked_once_per_tool_round(repos):
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    tool_call_plan = [
        [{"id": "call-1", "name": "get_carrier_reliability_detail", "args": {"carrier_id": "CAR-EXP"}}],
        [],  # second round: no more tool calls, breaks out of the loop
    ]
    fake_llm = FakeChatClient(tool_call_plan=tool_call_plan)
    service = _build_service(repos, fake_llm)

    job = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))
    heartbeats: list[None] = []

    service.run_generation(
        job.purchase_order_id,
        job.as_of_date,
        heartbeat=lambda: heartbeats.append(None),
    )

    # 2 tool rounds ran (round 1 called a tool, round 2 broke the loop) --
    # one heartbeat per round, not one per llm.invoke() call (the final,
    # tools-less call is outside the round loop and gets no heartbeat).
    assert len(heartbeats) == 2


def test_heartbeat_defaults_to_none_and_does_not_change_existing_behavior(repos):
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    job = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))
    service.run_generation(job.purchase_order_id, job.as_of_date)  # no heartbeat kwarg

    status = service.get_status(purchase_order_id, as_of_date=date(2026, 8, 5))
    assert status.status == "READY"


def test_a_raising_heartbeat_does_not_break_generation(repos):
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient(final_content="All clear despite a flaky heartbeat.")
    service = _build_service(repos, fake_llm)

    job = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))

    def _flaky_heartbeat() -> None:
        raise RuntimeError("lease renewal transiently failed")

    service.run_generation(
        job.purchase_order_id,
        job.as_of_date,
        heartbeat=_flaky_heartbeat,
    )

    status = service.get_status(purchase_order_id, as_of_date=date(2026, 8, 5))
    assert status.status == "READY"
    assert status.output.summary == "All clear despite a flaky heartbeat."


# ---------------------------------------------------------------------------
# Response schema backward compatibility
# ---------------------------------------------------------------------------


def test_non_reused_output_carries_backward_compatible_reuse_defaults(repos):
    """A freshly-generated (non-reused) summary's additive fields must be
    exactly the documented defaults, and every pre-existing field must be
    byte-identical to what it was before this feature existed."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient(final_content="Flat $2.50 expected OTIF fee.")
    service = _build_service(repos, fake_llm)

    job = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))

    assert job.status == "READY"
    assert job.output.order_id == str(purchase_order_id)
    assert job.output.as_of_date == date(2026, 8, 5)
    assert job.output.prompt_version == PROMPT_VERSION
    assert job.output.model_name == "fake-model"
    assert job.output.summary == "Flat $2.50 expected OTIF fee."
    assert job.output.is_reused is False
    assert job.output.generated_for_date == date(2026, 8, 5)
    assert job.output.unchanged_since is None
    assert job.output.unchanged_for_days is None


def test_penalty_projection_summary_response_schema_additive_fields_default():
    """Schema-level check: a response schema validated against a plain
    (non-reuse-aware) PenaltyProjectionSummaryOutput -- the shape existing
    clients built against -- still resolves the new fields to their
    documented defaults instead of raising.

    `app.schemas.fine_projection.summaries.ProjectionSummaryResponse` is
    API-layer (`app/schemas/`), out of this phase's scope and still keyed
    on the pre-restructure model names -- this asserts the same contract
    directly against `PenaltyProjectionSummaryOutput`'s own reuse subclass
    instead of importing that schema.
    """
    from app.services.penalties.projection.summary_service import PenaltyProjectionSummaryOutputWithReuse

    plain_output = PenaltyProjectionSummaryOutput(
        order_id="ORD-EXP",
        as_of_date=date(2026, 8, 5),
        prompt_version=PROMPT_VERSION,
        model_name="fake-model",
        summary="Flat $2.50 expected OTIF fee.",
    )

    response = PenaltyProjectionSummaryOutputWithReuse.model_validate(plain_output.model_dump())

    assert response.order_id == "ORD-EXP"
    assert response.as_of_date == date(2026, 8, 5)
    assert response.prompt_version == PROMPT_VERSION
    assert response.model_name == "fake-model"
    assert response.summary == "Flat $2.50 expected OTIF fee."
    assert response.is_reused is False
    assert response.generated_for_date is None
    assert response.unchanged_since is None
    assert response.unchanged_for_days is None


# ---------------------------------------------------------------------------
# v1 prompt registration
# ---------------------------------------------------------------------------


def test_v1_prompt_version_is_registered_on_first_use(repos):
    """_ensure_registered must insert a new `process.agent` row for
    `penalty_projection_summary`/"v1" the first time this service runs
    against a fresh registry."""
    purchase_order_id, _, _ = _seed_flat_rule_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    assert PROMPT_VERSION == "v1"

    job = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))

    assert job.status == "READY"
    assert job.output.prompt_version == "v1"

    registered = repos.agent_registry.get_active("penalty_projection_summary")
    assert registered is not None
    assert registered["prompt_version"] == "v1"

    persisted = repos.penalty_summaries.get_by_key(
        purchase_order_id, SummaryType.PROJECTION, date(2026, 8, 5)
    )
    assert persisted is not None
    assert persisted["status"] == "READY"
