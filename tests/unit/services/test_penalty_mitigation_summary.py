"""Tests for MitigationSummaryService, mirroring
tests/unit/services/test_penalty_projection_summary.py's structure for the
mitigation-summary feature.

Was against `FineMitigationSummaryService`/`MitigationResultRepository`/
`FineMitigationSummaryRepository` (business-string `order_id`, separate
`mitigation_summary` table); rewritten against `MitigationSummaryService`
and the merged `penalties.penalty_summary` table. Was
`tests/unit/services/test_fine_mitigation_summary.py` (`fine`/`fines` ->
`penalty`/`penalties` rename).
"""

from __future__ import annotations

from contextlib import suppress
from datetime import date
from uuid import uuid4

import pytest

from app.agents.penalties.mitigation.prompts.v2 import PROMPT_VERSION
from app.core.exceptions import BusinessRuleError, ExternalServiceError, NotFoundError, ValidationError
from app.models.enums import SummaryType
from app.services.penalties.mitigation.summary_service import MitigationSummaryService
from app.services.penalties.mitigation.types import MitigationOption
from app.services.penalties.projection.service import ProjectionService


class _FakeAIMessage:
    def __init__(self, content: str | None, tool_calls: list[dict]) -> None:
        self.content = content
        self.tool_calls = tool_calls


class FakeChatClient:
    model_name = "fake-model"

    def __init__(
        self,
        tool_call_plan: list[list[dict]] | None = None,
        final_content: str | None = "Accepting the penalty is currently the best option.",
        fail_invoke_on_round: int | None = None,
    ):
        self.tool_call_plan = tool_call_plan or []
        self.final_content = final_content
        self.fail_invoke_on_round = fail_invoke_on_round
        self.invocations: list[dict] = []
        self._optional_round = 0

    def invoke(self, messages, *, tools=None):
        invocation_index = len(self.invocations)
        self.invocations.append({"messages": messages, "tools": tools})
        if invocation_index == self.fail_invoke_on_round:
            raise RuntimeError("simulated upstream failure")

        if not tools:
            return _FakeAIMessage(content=self.final_content, tool_calls=[])

        calls = (
            self.tool_call_plan[self._optional_round]
            if self._optional_round < len(self.tool_call_plan)
            else []
        )
        self._optional_round += 1
        return _FakeAIMessage(content=None, tool_calls=calls)


def _seed_order(repos, po_number: str = "ORD-MITSUM"):
    retailer = repos.master_data.add_retailer(f"RET-{po_number}", "Retailer MitSum", None, "SUM")
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
    return purchase_order["id"]


def _seed_options(repos, purchase_order_id, projection_date: date) -> None:
    repos.mitigation_options.save_results(
        purchase_order_id,
        projection_date,
        [
            MitigationOption(
                action="ACCEPT",
                projected_penalty_after=100.0,
                action_cost=0.0,
                net_saving=0.0,
                risk_level="HIGH",
                confidence="CONFIRMED",
                rationale="Pay the projected penalty as-is.",
            ),
            MitigationOption(
                action="SPEED_UP_PRODUCTION",
                projected_penalty_after=20.0,
                action_cost=30.0,
                net_saving=50.0,
                risk_level="LOW",
                confidence="CONFIRMED",
                rationale="Closes the shortfall with extra capacity.",
            ),
        ],
    )


def _build_service(repos, fake_llm) -> MitigationSummaryService:
    projection_service = ProjectionService(
        purchase_orders=repos.purchase_orders,
        fulfillment=repos.fulfillment,
        rules=repos.penalty_rules,
        master_data=repos.master_data,
        projections=repos.penalty_projections,
    )
    return MitigationSummaryService(
        purchase_orders=repos.purchase_orders,
        summaries=repos.penalty_summaries,
        agent_registry=repos.agent_registry,
        job_queue=repos.job_queue,
        job_context=repos.penalty_job_item_context,
        llm=fake_llm,
        master_data=repos.master_data,
        mitigation_options=repos.mitigation_options,
        actual_penalties=repos.actual_penalties,
        projection_service=projection_service,
    )


def _schedule_and_run(
    service: MitigationSummaryService,
    purchase_order_id,
    as_of_date: date | None = None,
    force_regenerate: bool = False,
):
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


def test_no_mitigation_options_exist_error(repos):
    purchase_order_id = _seed_order(repos)
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    with pytest.raises(BusinessRuleError, match=str(purchase_order_id)):
        service.get_or_schedule(purchase_order_id)


def test_get_or_schedule_returns_pending_on_cache_miss(repos):
    purchase_order_id = _seed_order(repos)
    _seed_options(repos, purchase_order_id, date(2026, 8, 5))
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    job = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))

    assert job.status == "PENDING"
    assert fake_llm.invocations == []
    persisted = repos.penalty_summaries.get_by_key(
        purchase_order_id, SummaryType.MITIGATION, date(2026, 8, 5)
    )
    assert persisted["status"] == "PENDING"


def test_mandatory_context_contains_ranked_options(repos):
    purchase_order_id = _seed_order(repos)
    _seed_options(repos, purchase_order_id, date(2026, 8, 5))
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    job = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))

    assert job.status == "READY"
    content = fake_llm.invocations[0]["messages"][1].content
    assert content.startswith("<DATA>")
    for expected_key in (
        "order",
        "current_projection_date",
        "current_total_expected_penalty_amount",
        "stacking_mode",
        "mitigation_options",
    ):
        assert f'"{expected_key}"' in content
    assert '"SPEED_UP_PRODUCTION"' in content


def test_cache_hit_skips_llm(repos):
    purchase_order_id = _seed_order(repos)
    _seed_options(repos, purchase_order_id, date(2026, 8, 5))
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    first = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))
    assert first.status == "READY"
    assert len(fake_llm.invocations) == 2

    second = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))
    assert second.status == "READY"
    assert len(fake_llm.invocations) == 2
    assert second.output == first.output


def test_force_regenerate_replaces_the_row_in_place(repos):
    purchase_order_id = _seed_order(repos)
    _seed_options(repos, purchase_order_id, date(2026, 8, 5))
    fake_llm = FakeChatClient(final_content="First narrative.")
    service = _build_service(repos, fake_llm)

    first = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))
    assert first.status == "READY"

    fake_llm.final_content = "Regenerated narrative."
    second = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5), force_regenerate=True)

    assert second.status == "READY"
    assert second.output.summary == "Regenerated narrative."
    assert second.output.summary != first.output.summary


def test_run_generation_persists_failed_status_and_reraises_on_tool_loop_exhaustion(repos):
    purchase_order_id = _seed_order(repos)
    _seed_options(repos, purchase_order_id, date(2026, 8, 5))
    fake_llm = FakeChatClient(fail_invoke_on_round=0)
    service = _build_service(repos, fake_llm)

    job = service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))
    assert job.status == "PENDING"

    with pytest.raises(ExternalServiceError):
        service.run_generation(job.purchase_order_id, job.as_of_date)

    row = repos.penalty_summaries.get_by_key(purchase_order_id, SummaryType.MITIGATION, date(2026, 8, 5))
    assert row["status"] == "FAILED"
    assert row["error_message"] == "Penalty mitigation summary generation failed upstream"


def test_get_status_raises_no_mitigation_summary_job_exists_when_never_posted(repos):
    purchase_order_id = _seed_order(repos)
    _seed_options(repos, purchase_order_id, date(2026, 8, 5))
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    with pytest.raises(NotFoundError):
        service.get_status(purchase_order_id, as_of_date=date(2026, 8, 5))


def test_get_status_falls_back_to_a_still_pending_prior_job_not_just_ready(repos):
    """Shared `SummaryServiceBase.get_status` fallback -- see the mirroring
    test in test_penalty_projection_summary.py. A job requested on an
    earlier day that a worker never picked up (still PENDING) must still be
    found and reported as PENDING when polled on a later day with no
    as_of_date override."""
    purchase_order_id = _seed_order(repos)
    _seed_options(repos, purchase_order_id, date(2026, 8, 5))
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)
    service.get_or_schedule(purchase_order_id, as_of_date=date(2026, 8, 5))  # left PENDING, never run

    job = service.get_status(purchase_order_id, as_of_date=date(2026, 8, 6))

    assert job.status == "PENDING"
    assert job.as_of_date == date(2026, 8, 5)
    assert job.output is None


def test_as_of_date_in_the_future_raises_invalid_as_of_date(repos):
    purchase_order_id = _seed_order(repos)
    _seed_options(repos, purchase_order_id, date(2026, 8, 5))
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    with pytest.raises(ValidationError):
        service.get_or_schedule(purchase_order_id, as_of_date=date(2099, 1, 1))


def test_as_of_date_before_earliest_options_date_raises_invalid_as_of_date(repos):
    purchase_order_id = _seed_order(repos)
    _seed_options(repos, purchase_order_id, date(2026, 8, 5))
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    with pytest.raises(ValidationError):
        service.get_or_schedule(purchase_order_id, as_of_date=date(2000, 1, 1))


def test_v1_prompt_version_is_registered_on_first_use(repos):
    """Mirrors
    test_penalty_projection_summary.py::test_v1_prompt_version_is_registered_on_first_use
    for this feature's own agent/prompt identity."""
    purchase_order_id = _seed_order(repos)
    _seed_options(repos, purchase_order_id, date(2026, 8, 5))
    fake_llm = FakeChatClient()
    service = _build_service(repos, fake_llm)

    assert PROMPT_VERSION == "v2"

    job = _schedule_and_run(service, purchase_order_id, as_of_date=date(2026, 8, 5))

    assert job.status == "READY"
    assert job.output.prompt_version == PROMPT_VERSION
    registered = repos.agent_registry.get_active("penalty_mitigation_summary")
    assert registered is not None
    assert registered["prompt_version"] == PROMPT_VERSION
