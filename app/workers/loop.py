"""Concurrent claim/execute/settle loop for the batch worker."""

from __future__ import annotations

import logging
import random
import signal
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from sqlalchemy.exc import DBAPIError, OperationalError

from app.agents.providers.azure_openai import AzureOpenAIChatClient
from app.core.config import Settings
from app.core.exceptions import (
    AppError,
    BusinessRuleError,
    ExternalServiceError,
    NotFoundError,
    ValidationError,
)
from app.core.rate_limit import RateLimitGate, looks_like_rate_limit
from app.db.session import Database
from app.queue.interfaces import JobSource
from app.queue.types import ClaimedJob
from app.workers import dispatch

logger = logging.getLogger(__name__)

# Small jitter prevents concurrent workers from entering the LLM at once.
_DEFAULT_STARTUP_JITTER_SECONDS = 2.0
_DEFAULT_SHUTDOWN_GRACE_SECONDS = 30.0

# Classification is by `AppError` category, not by leaf type, so adding a new `code=`
# value under an existing category needs no change here.
_NON_RETRYABLE_APP_ERRORS: tuple[type[AppError], ...] = (
    NotFoundError,
    ValidationError,
    BusinessRuleError,
)


class _ItemDeadlineExceededError(Exception):
    """Raised when an item's wall-clock deadline expires."""


class _OwnershipLostError(Exception):
    """Raised by the heartbeat when the worker loses item ownership."""


class Classification(Enum):
    """Outcome of `classify_failure`: whether a job failure is terminal or worth retrying."""

    DEAD_LETTER = "dead_letter"
    NACK = "nack"


def classify_failure(exc: BaseException) -> Classification:
    """Classify a job failure as terminal or retryable.

    Bad input can never succeed on a retry, so it dead-letters. Everything else,
    unrecognized exceptions included, is NACK'd and retried up to `job.max_attempts`.
    """
    if isinstance(exc, _NON_RETRYABLE_APP_ERRORS):
        return Classification.DEAD_LETTER
    if isinstance(exc, ValueError) and not isinstance(exc, AppError):
        return Classification.DEAD_LETTER
    return Classification.NACK


def _is_recognized_retryable(exc: BaseException) -> bool:
    """Identify known retryable failures for log severity only.

    Retry and dead-letter behavior belong to `classify_failure`. This only picks
    between a `warning` and an `error` with a traceback; both outcomes still NACK.
    """
    return isinstance(
        exc,
        (
            ExternalServiceError,
            OperationalError,
            DBAPIError,
            _ItemDeadlineExceededError,
        ),
    )


def _error_code_for(exc: BaseException) -> str:
    """Derive the `job_item.last_error_code` value to persist for a failure."""
    if isinstance(exc, AppError):
        return exc.code
    if isinstance(exc, _ItemDeadlineExceededError):
        return "ITEM_DEADLINE_EXCEEDED"
    return type(exc).__name__


def compute_backoff_seconds(
    attempt_count: int,
    *,
    base: int,
    cap: int,
    jitter: int,
) -> int:
    """Return capped exponential backoff with optional jitter.

    Jitter keeps many workers retrying the same failure from all waking at once.
    """
    exponent = max(attempt_count - 1, 0)
    backoff = min(cap, base * (2**exponent))
    jitter_amount = random.uniform(0, jitter) if jitter > 0 else 0.0
    return round(backoff + jitter_amount)


@dataclass
class WorkerLoopSummary:
    """Counters collected during one worker-loop invocation."""

    succeeded: int = 0
    nacked: int = 0
    dead_lettered: int = 0
    dead_via_exhaustion: int = 0
    abandoned: int = 0
    released: int = 0
    rate_limit_hits: int = 0

    @property
    def dead_total(self) -> int:
        """Return items that ended DEAD during this run."""
        return self.dead_lettered + self.dead_via_exhaustion


@dataclass
class _HeartbeatState:
    """Mutable flag a heartbeat closure uses to signal ownership loss to `_process_job`."""

    lost: bool = False


def _make_heartbeat(
    job_source: JobSource,
    job: ClaimedJob,
    worker_id: str,
    state: _HeartbeatState,
) -> Callable[[], None]:
    """Build the heartbeat callback threaded through to `execute_job_fn`.

    Domain code calls it during long work (between LLM tool-calling rounds) to prove it
    still owns the item. A missed heartbeat raises `_OwnershipLostError` and flips
    `state.lost`, so `_process_job` abandons the job rather than nacking it.
    """

    def _heartbeat() -> None:
        """Renew ownership once, raising `_OwnershipLostError` if the renewal was rejected."""
        if not job_source.heartbeat(job, worker_id):
            state.lost = True
            raise _OwnershipLostError(f"job_item_id={job.job_item_id} lost ownership (worker_id={worker_id})")

    return _heartbeat


def _run_with_deadline(
    execute_job_fn: Callable[
        [ClaimedJob, Database, Settings, AzureOpenAIChatClient, Callable[[], None] | None],
        None,
    ],
    job: ClaimedJob,
    database: Database,
    settings: Settings,
    llm: AzureOpenAIChatClient,
    heartbeat: Callable[[], None],
    deadline_seconds: float,
) -> None:
    """Run a job on a daemon thread and enforce a wall-clock deadline.

    Python cannot forcibly stop the worker thread after timeout.
    """
    outcome: dict[str, BaseException] = {}

    def _target() -> None:
        """Run the job body on the daemon thread, capturing any exception for the caller to re-raise."""
        try:
            execute_job_fn(job, database, settings, llm, heartbeat)
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(
        target=_target,
        name=f"job-exec-{job.job_item_id}",
        daemon=True,
    )
    thread.start()
    thread.join(timeout=deadline_seconds)

    if thread.is_alive():
        raise _ItemDeadlineExceededError(
            f"job_item_id={job.job_item_id} exceeded the "
            f"{deadline_seconds}s item deadline "
            f"(job_queue.item_deadline_seconds)"
        )

    if "error" in outcome:
        raise outcome["error"]


def _process_job(
    job: ClaimedJob,
    *,
    job_source: JobSource,
    database: Database,
    settings: Settings,
    llm: AzureOpenAIChatClient,
    worker_id: str,
    rate_limit_gate: RateLimitGate,
    execute_job_fn: Callable[
        [ClaimedJob, Database, Settings, AzureOpenAIChatClient, Callable[[], None] | None],
        None,
    ],
    startup_jitter_max_seconds: float,
    deadline_seconds: float,
    shutdown_event: threading.Event,
) -> Literal[
    "succeeded",
    "nacked",
    "nacked_dead",
    "dead_lettered",
    "abandoned",
]:
    """Execute and settle one claimed job without raising.

    Runs `execute_job_fn` under a wall-clock deadline, heartbeating to prove ownership.
    Success acks; failure either dead-letters or nacks with backoff per
    `classify_failure`, and a nack that exhausts `max_attempts` returns "nacked_dead"
    so the summary counts it as both. Lost ownership settles nothing and returns
    "abandoned", since another worker may already own the item. Every exit path returns
    a status rather than raising, keeping a bug here from killing the worker loop.
    """
    if startup_jitter_max_seconds > 0:
        time.sleep(random.uniform(0, startup_jitter_max_seconds))

    rate_limit_gate.wait_if_paused(shutdown_event)

    heartbeat_state = _HeartbeatState()
    heartbeat = _make_heartbeat(
        job_source,
        job,
        worker_id,
        heartbeat_state,
    )

    try:
        _run_with_deadline(
            execute_job_fn,
            job,
            database,
            settings,
            llm,
            heartbeat,
            deadline_seconds,
        )
    except Exception as exc:
        if heartbeat_state.lost:
            logger.info(
                "Abandoning job_item_id=%s: ownership lost mid-execution (worker_id=%s)",
                job.job_item_id,
                worker_id,
            )
            return "abandoned"

        if looks_like_rate_limit(exc):
            rate_limit_gate.note_rate_limit_hit(settings.llm.rate_limit_backoff_seconds)
            logger.warning(
                "Rate-limit signal for job_item_id=%s; pausing shared LLM gate for %ss",
                job.job_item_id,
                settings.llm.rate_limit_backoff_seconds,
            )

        error_message = str(exc)
        error_code = _error_code_for(exc)
        classification = classify_failure(exc)

        if classification is Classification.DEAD_LETTER:
            job_source.dead_letter(
                job,
                worker_id,
                error=error_message,
                error_code=error_code,
            )
            logger.error(
                "job_item_id=%s dead-lettered (%s): %s",
                job.job_item_id,
                error_code,
                error_message,
            )
            return "dead_lettered"

        if _is_recognized_retryable(exc):
            logger.warning(
                "job_item_id=%s retryable failure (%s): %s",
                job.job_item_id,
                error_code,
                error_message,
            )
        else:
            logger.error(
                "job_item_id=%s unclassified exception; retrying within max_attempts=%s",
                job.job_item_id,
                job.max_attempts,
                exc_info=exc,
            )

        retry_in_seconds = compute_backoff_seconds(
            job.attempt_count,
            base=settings.job_queue.backoff_base_seconds,
            cap=settings.job_queue.backoff_cap_seconds,
            jitter=settings.job_queue.backoff_jitter_seconds,
        )
        job_source.nack(
            job,
            worker_id,
            error=error_message,
            error_code=error_code,
            retry_in_seconds=retry_in_seconds,
        )
        return "nacked_dead" if job.attempt_count >= job.max_attempts else "nacked"

    if heartbeat_state.lost:
        logger.info(
            "Abandoning job_item_id=%s: ownership lost at completion (worker_id=%s)",
            job.job_item_id,
            worker_id,
        )
        return "abandoned"

    job_source.ack(job, worker_id)
    return "succeeded"


def _record_result(
    summary: WorkerLoopSummary,
    future: Future,
    job: ClaimedJob,
) -> None:
    """Tally a completed `_process_job` future's outcome into the run summary.

    `_process_job` is designed never to raise, so an exception surfacing
    here means it broke that contract; it is logged and swallowed rather
    than propagated, since one bad future must not abort the whole batch
    loop or leave the remaining futures unrecorded.
    """
    try:
        outcome = future.result()
    except Exception:
        logger.exception(
            "job_item_id=%s: _process_job raised unexpectedly",
            job.job_item_id,
        )
        return

    if outcome == "succeeded":
        summary.succeeded += 1
    elif outcome == "nacked":
        summary.nacked += 1
    elif outcome == "nacked_dead":
        summary.nacked += 1
        summary.dead_via_exhaustion += 1
    elif outcome == "dead_lettered":
        summary.dead_lettered += 1
    elif outcome == "abandoned":
        summary.abandoned += 1


def _check_pool_headroom(settings: Settings) -> None:
    """Warn at startup if worker concurrency could exceed the DB connection pool's capacity.

    Each concurrently executing job holds at least one DB session, so
    `worker_concurrency` above `pool_size + max_overflow` means some workers
    will block waiting for a connection rather than run in parallel; this
    only logs the misconfiguration; it does not change the settings.
    """
    pool_capacity = settings.database.pool_size + settings.database.max_overflow
    if settings.job_queue.worker_concurrency > pool_capacity:
        logger.warning(
            "job_queue.worker_concurrency=%s exceeds DB capacity=%s; "
            "workers may block waiting for a DB session",
            settings.job_queue.worker_concurrency,
            pool_capacity,
        )


def _install_sigterm_handler(flag: threading.Event) -> Callable[[], None]:
    """Install a SIGTERM handler and return a function that restores it."""
    previous = signal.getsignal(signal.SIGTERM)

    def _handler(signum: int, frame: object) -> None:
        """Set the shutdown flag; `process_jobs` reads it to stop claiming new work and start draining."""
        logger.warning("SIGTERM received; no longer claiming new work, draining in-flight items")
        flag.set()

    signal.signal(signal.SIGTERM, _handler)

    def _restore() -> None:
        """Reinstall the SIGTERM handler that was active before this call."""
        signal.signal(signal.SIGTERM, previous)

    return _restore


def process_jobs(
    job_source: JobSource,
    database: Database,
    settings: Settings,
    llm: AzureOpenAIChatClient,
    *,
    worker_id: str | None = None,
    mode: Literal["drain", "service"] = "drain",
    reclaim_stale_first: bool = True,
    execute_job_fn: Callable[
        [ClaimedJob, Database, Settings, AzureOpenAIChatClient, Callable[[], None] | None],
        None,
    ] = dispatch.execute_job,
    startup_jitter_max_seconds: float = _DEFAULT_STARTUP_JITTER_SECONDS,
    shutdown_grace_seconds: float = _DEFAULT_SHUTDOWN_GRACE_SECONDS,
    shutdown_event: threading.Event | None = None,
    install_signal_handler: bool = True,
) -> WorkerLoopSummary:
    """Claim and execute jobs until the queue is drained or shutdown starts.

    `service` mode keeps polling an empty queue; `drain` exits when no work remains.
    `execute_job_fn` is injectable for tests.
    """
    _check_pool_headroom(settings)

    worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
    shutdown_event = shutdown_event or threading.Event()
    rate_limit_gate = RateLimitGate()
    summary = WorkerLoopSummary()

    restore_signal_handler: Callable[[], None] | None = None
    if install_signal_handler:
        restore_signal_handler = _install_sigterm_handler(shutdown_event)

    try:
        if reclaim_stale_first:
            reclaimed = job_source.reclaim_stale(settings.job_queue.visibility_timeout_seconds)
            if reclaimed:
                logger.info(
                    "Reclaimed %s stale job_item(s) before claiming new work",
                    reclaimed,
                )

        idle_backoff = float(settings.job_queue.poll_interval_seconds)
        futures: dict[Future, ClaimedJob] = {}

        # Avoid context-manager shutdown here: its implicit wait could
        # exceed the explicit shutdown grace period below.
        executor = ThreadPoolExecutor(max_workers=settings.job_queue.worker_concurrency)
        try:
            while not shutdown_event.is_set():
                batch: list[ClaimedJob] = job_source.claim_batch(
                    worker_id,
                    settings.job_queue.batch_size,
                )

                if batch:
                    idle_backoff = float(settings.job_queue.poll_interval_seconds)
                    for job in batch:
                        future = executor.submit(
                            _process_job,
                            job,
                            job_source=job_source,
                            database=database,
                            settings=settings,
                            llm=llm,
                            worker_id=worker_id,
                            rate_limit_gate=rate_limit_gate,
                            execute_job_fn=execute_job_fn,
                            startup_jitter_max_seconds=startup_jitter_max_seconds,
                            deadline_seconds=float(settings.job_queue.item_deadline_seconds),
                            shutdown_event=shutdown_event,
                        )
                        futures[future] = job
                elif mode == "drain" and not futures:
                    break

                for future in [f for f in futures if f.done()]:
                    job = futures.pop(future)
                    _record_result(summary, future, job)

                if not batch:
                    if shutdown_event.wait(timeout=idle_backoff):
                        break
                    idle_backoff = min(
                        float(settings.job_queue.idle_poll_max_seconds),
                        idle_backoff * 2,
                    )

            if shutdown_event.is_set() and futures:
                _, pending = wait(
                    list(futures),
                    timeout=shutdown_grace_seconds,
                )
                for future in list(futures):
                    job = futures.pop(future)
                    if future in pending:
                        # Release explicitly instead of waiting for stale-item
                        # reclamation.
                        job_source.release(job, worker_id)
                        summary.released += 1
                        logger.warning(
                            "Released job_item_id=%s to PENDING after %ss shutdown grace period",
                            job.job_item_id,
                            shutdown_grace_seconds,
                        )
                    else:
                        _record_result(summary, future, job)
            else:
                wait(list(futures))
                for future in list(futures):
                    job = futures.pop(future)
                    _record_result(summary, future, job)
        finally:
            executor.shutdown(wait=False)
    finally:
        if restore_signal_handler is not None:
            restore_signal_handler()

    summary.rate_limit_hits = rate_limit_gate.rate_limit_hits
    return summary
