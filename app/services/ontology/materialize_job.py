"""Rebuilds the CMIR <-> Material Master traceability graph from Postgres
and swaps it into the running `Container`.

Runs in-process, on an interval, started as an `asyncio` background task
from `app.main`'s `lifespan` -- see `app/core/container.py`'s docstring for
why the graph itself is process-lifetime state rather than fetched fresh
per request the way every repository is. `rebuild` is deliberately a plain
synchronous function: it does one bulk read plus one in-memory build, no
I/O-bound waiting worth `await`ing, so the caller runs it on a thread (see
`run_forever` below) instead of making every call site here `async`.
"""

from __future__ import annotations

import asyncio
import logging
import time

from rdflib import Graph

from app.core.container import Container
from app.ontology.config.validation import validate_mapping
from app.services.ontology.ontology_builder import OntologyBuilder

logger = logging.getLogger(__name__)

_builder = OntologyBuilder()


def rebuild(container: Container) -> Graph:
    """One full rebuild: validate the DB<->ontology mapping, bulk-read
    current rows, build a fresh `Graph`, and swap it into `container`.
    Returns the new graph (mainly for tests/logging -- callers that only
    care about the side effect can ignore it).

    `validate_mapping()` is deliberately called first, before any DB read
    -- it needs no live connection, so a broken mapping is caught for free
    on every cycle without an extra round trip. If it raises
    `MappingValidationError`, this function raises too (uncaught here on
    purpose): `run_forever` already wraps every call to `rebuild()` in a
    try/except that logs and retries next interval -- exactly the "keep
    serving the last known-good graph, never publish a broken one" behavior
    this needs, with no new state-management mechanism required beyond the
    atomic swap `Container` already does.
    """
    started = time.monotonic()
    validate_mapping()
    with container.ontology_repos() as repos:
        cmir_records = repos.cmir_records.list_current()
        materials = repos.master_data.list_material_rows()
        material_masters = repos.master_data.list_material_master_rows()
        plants = repos.master_data.list_plant_rows()

    graph = Graph()
    _builder.build_rdf_triples(
        graph,
        cmir_records=cmir_records,
        materials=materials,
        material_masters=material_masters,
        plants=plants,
    )
    container.refresh_ontology_graph(graph)

    logger.info(
        "Rebuilt ontology graph: %d cmir_records, %d materials, %d material_masters, "
        "%d plants, %d triples, in %.3fs",
        len(cmir_records),
        len(materials),
        len(material_masters),
        len(plants),
        len(graph),
        time.monotonic() - started,
    )
    return graph


async def run_forever(interval_seconds: int) -> None:
    """Rebuilds the ontology graph once immediately (so the first request
    after startup doesn't see an empty graph), then again every
    `interval_seconds` until cancelled at shutdown. Runs `rebuild` (a
    blocking, synchronous function) on a worker thread via `asyncio.to_thread`
    so it never blocks the event loop the API is also using.

    `Container.build()` is called from inside the loop, not once up front,
    and inside the same try/except as the rebuild itself: like
    `build_service()`/`build_po_validation_service()` in `app.main`, it opens
    a real Postgres-backed checkpointer, which tests deliberately avoid
    triggering by injecting fake services (see
    tests/unit/api/test_main_lifespan.py) -- doing the build here means a
    Postgres-less environment logs and retries next interval instead of
    failing app startup outright.
    """
    while True:
        try:
            container = Container.build()
            await asyncio.to_thread(rebuild, container)
        except Exception:
            logger.exception("Ontology graph rebuild failed; will retry next interval.")
        await asyncio.sleep(interval_seconds)
