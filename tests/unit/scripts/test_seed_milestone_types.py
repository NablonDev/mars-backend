"""Tests for `scripts/seed/seed_milestone_types.py`.

Invariants over `MILESTONE_TYPE_SEEDS` (see the approved fulfillment-timeline
plan): every `depends_on` code exists, the dependency graph is acyclic, every
dependency has a lower `sequence_no`, and exactly one measurement point
applies to each freight term (`ANY` counts for both). Plus idempotency of
`seed_milestone_types()` against the SQLite `db_session` fixture.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.common import MilestoneType
from scripts.seed import seed_milestone_types


def test_every_dependency_code_exists():
    codes = {seed.code for seed in seed_milestone_types.MILESTONE_TYPE_SEEDS}
    for seed in seed_milestone_types.MILESTONE_TYPE_SEEDS:
        for dependency in seed.depends_on:
            assert dependency in codes, f"{seed.code} depends on undefined milestone {dependency!r}"


def test_dependency_graph_is_acyclic():
    by_code = {seed.code: seed for seed in seed_milestone_types.MILESTONE_TYPE_SEEDS}

    def _visit(code: str, stack: set[str]) -> None:
        assert code not in stack, f"cycle detected through {code!r}"
        for dependency in by_code[code].depends_on:
            _visit(dependency, stack | {code})

    for seed in seed_milestone_types.MILESTONE_TYPE_SEEDS:
        _visit(seed.code, set())


def test_every_dependency_has_a_lower_sequence_no():
    by_code = {seed.code: seed for seed in seed_milestone_types.MILESTONE_TYPE_SEEDS}
    for seed in seed_milestone_types.MILESTONE_TYPE_SEEDS:
        for dependency in seed.depends_on:
            assert by_code[dependency].sequence_no < seed.sequence_no, (
                f"{seed.code} (seq {seed.sequence_no}) depends on {dependency} "
                f"(seq {by_code[dependency].sequence_no}), which is not earlier"
            )


def test_exactly_one_measurement_point_per_freight_term():
    measurement_scopes = [
        seed.freight_term_scope
        for seed in seed_milestone_types.MILESTONE_TYPE_SEEDS
        if seed.is_measurement_point
    ]
    for freight_term in ("PREPAID", "COLLECT"):
        applicable = [scope for scope in measurement_scopes if scope in (freight_term, "ANY")]
        assert len(applicable) == 1, (
            f"expected exactly one measurement point for {freight_term}, found {len(applicable)}"
        )


def test_seed_milestone_types_is_idempotent(db_session):
    seed_milestone_types.seed_milestone_types(db_session)
    seed_milestone_types.seed_milestone_types(db_session)

    rows = db_session.scalars(select(MilestoneType)).all()
    assert len(rows) == len(seed_milestone_types.MILESTONE_TYPE_SEEDS)

    delivered = next(row for row in rows if row.code == "DELIVERED")
    assert delivered.sequence_no == 100
    assert delivered.depends_on == ["GOODS_ISSUED", "APPOINTMENT_CONFIRMED"]
    assert delivered.is_measurement_point is True
