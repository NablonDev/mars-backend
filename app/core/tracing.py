"""LangGraph node wrapper that records each node execution to agent_traces."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from langgraph.errors import GraphInterrupt

from app.repositories.process.agent_registry import AgentTraceRepository
from app.utils.clock import utc_now
from app.utils.sanitize import strip_nul_bytes

# Deliberately Dict[str, Any], not the CMIR-specific GraphState: LangGraph reads a
# wrapped node function's parameter annotation to decide which state keys to pass
# it, so annotating this shared wrapper with GraphState would silently strip any
# key not in that schema (discovered while reusing this wrapper for the PO
# Validation graph, whose POGraphState has different keys).
NodeFn = Callable[[dict[str, Any]], dict[str, Any]]


def traced(node_name: str, fn: NodeFn, trace_repo: AgentTraceRepository) -> NodeFn:
    """Wrap a node function so every execution is written to agent_traces.

    Three outcomes are logged, distinguished by `status`:
    - "completed" - the node returned normally
    - "paused"    - the node hit interrupt() and the graph is waiting on a human
    - "failed"    - the node raised a real exception

    Note: because of how LangGraph's interrupt() replay works, a node that
    pauses gets called twice across the run - once up to the pause
    ("paused"), and once again on resume, from the top, this time running
    to completion ("completed"). That's expected, not a bug: the gap
    between those two trace rows is effectively "how long did the human
    take to answer."

    GraphInterrupt is LangGraph's internal signal for a pause. If a
    langgraph upgrade moves/renames it, this import is the one line to
    fix - nothing else in the project touches LangGraph internals.
    """

    def wrapped(state: dict[str, Any]) -> dict[str, Any]:
        """Run the wrapped node, logging its outcome (completed, paused, or failed) to agent_traces."""
        run_id = state.get("run_id")
        started_at = utc_now()
        t0 = time.perf_counter()

        try:
            result = fn(state)
        except GraphInterrupt:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            if run_id is not None:
                trace_repo.log(
                    run_id,
                    node_name,
                    "paused",
                    started_at,
                    utc_now(),
                    duration_ms,
                    input_snapshot=strip_nul_bytes(state),
                    output_snapshot=None,
                    error=None,
                )
            raise
        except Exception as exc:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            if run_id is not None:
                trace_repo.log(
                    run_id,
                    node_name,
                    "failed",
                    started_at,
                    utc_now(),
                    duration_ms,
                    input_snapshot=strip_nul_bytes(state),
                    output_snapshot=None,
                    error=strip_nul_bytes(str(exc)),
                )
            raise
        else:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            if run_id is not None:
                trace_repo.log(
                    run_id,
                    node_name,
                    "completed",
                    started_at,
                    utc_now(),
                    duration_ms,
                    input_snapshot=strip_nul_bytes(state),
                    output_snapshot=strip_nul_bytes(result),
                    error=None,
                )
            return result

    return wrapped
