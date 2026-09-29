"""Shared lifecycle for penalty-summary LLM features."""

from __future__ import annotations

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import ClassVar
from uuid import UUID

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from app.agents.penalties._summary_output import PenaltySummaryOutputBase
from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError, NotFoundError
from app.models.enums import JobRunType, SummaryStatus
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.penalties.job_context import PenaltyJobItemContextRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.process.agent_registry import AgentRegistryRepository
from app.repositories.process.job_queue import JobQueueRepository

#: `NotFoundError(code=...)` for `get_status` when no summary job row exists at
#: all, keyed by `summary_domain`.
_NO_SUMMARY_JOB_CODES: dict[str, str] = {
    "projection": "NO_PROJECTION_SUMMARY_JOB_EXISTS",
    "mitigation": "NO_MITIGATION_SUMMARY_JOB_EXISTS",
    "dispute": "NO_DISPUTE_SUMMARY_JOB_EXISTS",
}


@dataclass
class SummaryJob[OutputT: BaseModel]:
    """The status and (if ready) output of one penalty-summary generation job."""

    purchase_order_id: UUID
    as_of_date: date
    status: str
    output: OutputT | None = None
    error_message: str | None = None


logger = logging.getLogger(__name__)


class SummaryServiceBase[ContextT: BaseModel, OutputT: BaseModel](ABC):
    """Shared orchestration for a penalty-summary LLM feature.

    Subclasses set the class-level identity attributes below and implement
    the domain-specific hooks; everything else (caching, reuse, the
    PENDING/READY/FAILED lifecycle) lives here, once. The bounded
    tool-calling loop itself lives one layer down, in each subclass's
    `PenaltyProjectionAgent`/`PenaltyMitigationAgent` (see `_generate`).
    """

    #: The `penalty_summary.summary_type` discriminator this instance reads and
    #: writes: PROJECTION, MITIGATION, or DISPUTE.
    summary_type: ClassVar[str]
    #: Keys `_NO_SUMMARY_JOB_CODES` and each agent's upstream-failure-code map:
    #: "projection", "mitigation", or "dispute".
    summary_domain: ClassVar[str]
    #: `process.agent.agent_code` this feature registers/reads under.
    agent_code: ClassVar[str]
    agent_name: ClassVar[str]
    prompt_version: ClassVar[str]
    system_prompt: ClassVar[str]
    #: `process.job_item.item_type` / `penalty_job_item_context.task_type`
    #: used when `get_or_schedule` enqueues a regeneration job.
    job_task_type: ClassVar[str]
    upstream_failure_message: ClassVar[str]

    def __init__(
        self,
        *,
        purchase_orders: PurchaseOrderRepository,
        summaries: PenaltySummaryRepository,
        agent_registry: AgentRegistryRepository,
        job_queue: JobQueueRepository,
        job_context: PenaltyJobItemContextRepository,
        llm: AzureOpenAIChatClient,
    ) -> None:
        self.purchase_orders = purchase_orders
        self.summaries = summaries
        self.agent_registry = agent_registry
        self.job_queue = job_queue
        self.job_context = job_context
        self.llm = llm

    # ------------------------------------------------------------------
    # Hooks every subclass must implement
    # ------------------------------------------------------------------

    @abstractmethod
    def _validate(self, purchase_order_id: UUID, as_of_date: date | None) -> tuple[dict, date, list[dict]]:
        """Resolve `as_of_date` and confirm the purchase order and domain history are in range.

        Returns `(purchase_order, as_of_date, history)`, or raises the domain's
        own not-found, no-history, or invalid-date error.
        """

    @abstractmethod
    def _history_for_generation(self, purchase_order_id: UUID, as_of_date: date) -> list[dict]:
        """Like `_validate`'s history, but for `run_generation`.

        No date-range validation here: the job that reaches this point was
        already validated once, at schedule time.
        """

    @abstractmethod
    def _assemble_mandatory_context(
        self, purchase_order: dict, as_of_date: date, history: list[dict]
    ) -> ContextT:
        """Build the Pydantic context object handed to the LLM as the mandatory (non-tool-fetched) data."""

    @abstractmethod
    def _compute_content_fingerprint(self, context: ContextT) -> str:
        """Hash only the facts that determine the generated narrative."""

    @abstractmethod
    def _build_tools(self, purchase_order: dict, as_of_date: date) -> list[BaseTool]:
        """Build the bounded tool set for this generation call."""

    @abstractmethod
    def _output_with_reuse_cls(self) -> type[OutputT]:
        """The domain's `*Output` class extended with the shared reuse fields."""

    @abstractmethod
    def _generate(
        self,
        context: ContextT,
        *,
        order_id: str,
        as_of_date: date,
        tools: list[BaseTool],
        heartbeat: Callable[[], None] | None,
    ) -> PenaltySummaryOutputBase:
        """Delegate to this feature's agent to run the bounded tool-calling loop and produce the output.

        Returns the plain (non-reuse-aware) output; the reuse fields on `OutputT` are
        assembled later, in `_to_output`, from the persisted row.
        """

    # ------------------------------------------------------------------
    # Shared lifecycle
    # ------------------------------------------------------------------

    def _ensure_registered(self) -> UUID:
        """Idempotently register this feature's agent identity and return its row id.

        Safe to call on every request path: the repository upserts on `agent_code`, so a stale
        `prompt_version`/`system_prompt` from a prior deploy is corrected in place.
        """
        return self.agent_registry.ensure_registered(
            agent_code=self.agent_code,
            prompt_version=self.prompt_version,
            system_prompt=self.system_prompt,
            agent_name=self.agent_name,
            domain="penalties",
        )

    def get_or_schedule(
        self,
        purchase_order_id: UUID,
        as_of_date: date | None = None,
        force_regenerate: bool = False,
    ) -> SummaryJob[OutputT]:
        """Get existing summary or schedule one for generation by a worker.

        Validates the purchase order and as_of_date, then checks (in order):
        (1) Is a READY summary cached for this PO/date/type? Return it immediately.
        (2) Is reuse enabled and a matching narrative in-window? Clone it, return ready.
        (3) Create a PENDING summary row and enqueue a job for a worker to generate
        the summary later via run_generation. Return PENDING status.

        force_regenerate=True skips cache/reuse checks and always enqueues a new job.
        Raises NotFoundError if PO or domain history don't exist, or ValidationError
        if as_of_date is out of range."""
        purchase_order, as_of_date, history = self._validate(purchase_order_id, as_of_date)

        context = self._assemble_mandatory_context(purchase_order, as_of_date, history)
        context_hash = hashlib.sha256(
            json.dumps(context.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        content_fingerprint = self._compute_content_fingerprint(context)
        logger.info(
            "%s summary content fingerprint: purchase_order_id=%s as_of_date=%s fingerprint=%s",
            self.summary_type,
            purchase_order_id,
            as_of_date,
            content_fingerprint,
        )

        if not force_regenerate:
            cached = self.summaries.get_cached(purchase_order_id, self.summary_type, as_of_date)
            if cached is not None:
                return SummaryJob(purchase_order_id, as_of_date, SummaryStatus.READY, self._to_output(cached))

            if get_settings().summary.reuse_enabled:
                reused = self._try_reuse(purchase_order_id, as_of_date, content_fingerprint, context_hash)
                if reused is not None:
                    return SummaryJob(
                        purchase_order_id, as_of_date, SummaryStatus.READY, self._to_output(reused)
                    )

        agent_id = self._ensure_registered()
        self.summaries.create_pending(
            purchase_order_id=purchase_order_id,
            summary_type=self.summary_type,
            as_of_date=as_of_date,
            agent_id=agent_id,
            context_hash=context_hash,
            content_fingerprint=content_fingerprint,
        )
        self._enqueue_regeneration_job(purchase_order_id, as_of_date, force_regenerate)
        self.summaries.commit()

        return SummaryJob(purchase_order_id, as_of_date, SummaryStatus.PENDING)

    def _enqueue_regeneration_job(
        self, purchase_order_id: UUID, as_of_date: date, force_regenerate: bool
    ) -> None:
        """Enqueue a job for a worker to later call `run_generation` against.

        Attaches the matching `penalty_job_item_context` row in the same transaction.
        """
        settings = get_settings()
        run = self.job_queue.create_run(
            job_type=self.job_task_type,
            trigger_type=JobRunType.ON_DEMAND,
            requested_item_count=1,
        )
        dedupe_key = f"{purchase_order_id}:{as_of_date.isoformat()}:{self.summary_type}"
        item = self.job_queue.enqueue(
            run["id"],
            item_type=self.job_task_type,
            dedupe_key=dedupe_key,
            max_attempts=settings.job_queue.max_attempts,
        )
        if item is None:
            return
        # `enqueue()` returns the existing in-flight row, not a new one, when `dedupe_key` is already
        # PENDING/RUNNING; a context row already exists for that job_item_id in that case, and
        # creating a second one would violate its primary key.
        if self.job_context.get(item["id"]) is not None:
            return
        self.job_context.create(
            job_item_id=item["id"],
            purchase_order_id=purchase_order_id,
            projection_date=as_of_date,
            task_type=self.job_task_type,
            force_regenerate_summary=force_regenerate,
        )

    def _try_reuse(
        self,
        purchase_order_id: UUID,
        as_of_date: date,
        content_fingerprint: str,
        context_hash: str,
    ) -> dict | None:
        """Find and reuse a prior narrative if its content fingerprint matches, within `max_reuse_days`.

        Returns the reused row dict, or None if no match is found within the window. Caller must
        already have validated the purchase order and as_of_date.
        """
        settings = get_settings()
        earliest_source_date = as_of_date - timedelta(days=settings.summary.max_reuse_days)
        agent_id = self._ensure_registered()

        candidate = self.summaries.find_reusable(
            purchase_order_id=purchase_order_id,
            summary_type=self.summary_type,
            agent_id=agent_id,
            content_fingerprint=content_fingerprint,
            earliest_source_date=earliest_source_date,
            not_after=as_of_date,
        )
        if candidate is None:
            return None

        original_source_date = candidate["source_as_of_date"] or candidate["as_of_date"]

        reused = self.summaries.create_reused(
            purchase_order_id=purchase_order_id,
            summary_type=self.summary_type,
            as_of_date=as_of_date,
            agent_id=agent_id,
            context_hash=context_hash,
            content_fingerprint=content_fingerprint,
            model_name=candidate["model_name"],
            summary=candidate["summary"],
            source_as_of_date=original_source_date,
        )
        self.summaries.commit()

        logger.info(
            "%s summary reused: purchase_order_id=%s as_of_date=%s source_as_of_date=%s fingerprint=%s",
            self.summary_type,
            purchase_order_id,
            as_of_date,
            original_source_date,
            content_fingerprint,
        )

        return reused

    def get_status(self, purchase_order_id: UUID, as_of_date: date | None = None) -> SummaryJob[OutputT]:
        """Fetch the status and output of a scheduled summary job.

        Resolves as_of_date if omitted, then looks for a summary dated exactly
        as_of_date. If not found, falls back to the nearest prior summary (to
        handle queries after a date when no job was enqueued). Returns job status
        (PENDING/READY/FAILED), output if READY, and error_message if FAILED.
        Raises NotFoundError if no summary exists for the PO/date within the
        historical range."""
        _, as_of_date, _ = self._validate(purchase_order_id, as_of_date)

        row = self.summaries.get_by_key(purchase_order_id, self.summary_type, as_of_date)
        if row is None:
            # No job dated exactly as_of_date: fall back to the nearest prior job of any status.
            # Status-agnostic on purpose, a PENDING/FAILED job dated before as_of_date must still
            # be reported as such, not treated as if it never existed just because it never
            # reached READY (see PenaltySummaryRepository.get_latest_not_after's docstring).
            row = self.summaries.get_latest_not_after(purchase_order_id, self.summary_type, as_of_date)
        if row is None:
            raise NotFoundError(
                code=_NO_SUMMARY_JOB_CODES[self.summary_domain],
                message=(
                    f"No penalty-{self.summary_domain}-summary job found for "
                    f"purchase_order_id={purchase_order_id}, as_of_date={as_of_date.isoformat()}. "
                    f"POST /penalties/{self.summary_domain}s/summary first."
                ),
            )

        output = self._to_output(row) if row["summary"] is not None else None

        return SummaryJob(
            purchase_order_id=purchase_order_id,
            as_of_date=row["as_of_date"],
            status=row["status"],
            output=output,
            error_message=row["error_message"],
        )

    def _to_output(self, row: dict) -> OutputT:
        """Map a persisted `penalty_summary` row onto the reuse-aware output DTO.

        A row is "reused" when its `source_as_of_date` differs from its own `as_of_date`, meaning
        `_try_reuse` cloned an earlier narrative instead of generating a new one.
        """
        source_as_of_date = row.get("source_as_of_date")
        is_reused = source_as_of_date is not None
        as_of_date = row["as_of_date"]

        output_cls = self._output_with_reuse_cls()
        return output_cls(
            order_id=str(row["purchase_order_id"]),
            as_of_date=as_of_date,
            prompt_version=self.prompt_version,
            model_name=row["model_name"],
            summary=row["summary"],
            is_reused=is_reused,
            generated_for_date=source_as_of_date if is_reused else as_of_date,
            unchanged_since=source_as_of_date if is_reused else None,
            unchanged_for_days=(as_of_date - source_as_of_date).days if is_reused else None,
        )

    def run_generation(
        self,
        purchase_order_id: UUID,
        as_of_date: date,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        """Generate the summary and persist success or failure.

        Failures are persisted and re-raised so queue callers can apply
        their retry/dead-letter policy. Heartbeats are best-effort and
        never abort generation.
        """
        purchase_order = self.purchase_orders.get_purchase_order(purchase_order_id)
        if purchase_order is None:
            logger.error(
                "%s summary job: purchase_order %s no longer exists",
                self.summary_type,
                purchase_order_id,
            )
            return

        try:
            history = self._history_for_generation(purchase_order_id, as_of_date)
            context = self._assemble_mandatory_context(purchase_order, as_of_date, history)
            content_fingerprint = self._compute_content_fingerprint(context)
            logger.info(
                "%s summary content fingerprint: purchase_order_id=%s as_of_date=%s fingerprint=%s",
                self.summary_type,
                purchase_order_id,
                as_of_date,
                content_fingerprint,
            )
            tools = self._build_tools(purchase_order, as_of_date)
            output = self._generate(
                context,
                order_id=str(purchase_order_id),
                as_of_date=as_of_date,
                tools=tools,
                heartbeat=heartbeat,
            )
        except ExternalServiceError as exc:
            logger.error(
                "%s summary generation failed: purchase_order_id=%s date=%s: %s",
                self.summary_type,
                purchase_order_id,
                as_of_date,
                exc.details or exc.message,
            )
            self._safe_mark_failed(purchase_order_id, as_of_date, exc.message)
            raise
        except Exception:
            logger.exception(
                "%s summary generation crashed: purchase_order_id=%s date=%s",
                self.summary_type,
                purchase_order_id,
                as_of_date,
            )
            self._safe_mark_failed(purchase_order_id, as_of_date, self.upstream_failure_message)
            raise

        self.summaries.mark_ready(
            purchase_order_id=purchase_order_id,
            summary_type=self.summary_type,
            as_of_date=as_of_date,
            agent_id=self._ensure_registered(),
            model_name=output.model_name,
            summary=output.summary,
            content_fingerprint=content_fingerprint,
        )

    def _safe_mark_failed(self, purchase_order_id: UUID, as_of_date: date, error_message: str) -> None:
        """Persist a FAILED status for this job, swallowing any secondary failure.

        Called from `run_generation`'s exception handlers, which must re-raise the original error
        regardless of whether the failure write succeeds.
        """
        try:
            self.summaries.mark_failed(
                purchase_order_id,
                self.summary_type,
                as_of_date,
                self._ensure_registered(),
                error_message,
            )
        except Exception:
            logger.exception(
                "Failed to persist %s summary failure: purchase_order_id=%s date=%s",
                self.summary_type,
                purchase_order_id,
                as_of_date,
            )

    # The bounded tool-calling loop itself lives in each domain's
    # PenaltyProjectionAgent/PenaltyMitigationAgent (see _generate above).
