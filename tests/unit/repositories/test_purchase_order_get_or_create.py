"""Tests for PurchaseOrderRepository.get_or_create_purchase_order/
get_or_create_line -- the concurrency fix for the check-then-insert race
in PoValidationService._ingest_one_line (see docs/ARCHITECTURE.md).

Only sequential/idempotency behavior is exercised here, against the
SQLite unit-test fixture -- see the module docstring in
tests/unit/repositories/test_master_data_get_or_create.py for why the real
concurrent-race proof must be Postgres-only
(tests/integration/test_po_ingestion_concurrency_postgres.py).

This file adds tests only -- it does not modify any existing test file.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from app.core.exceptions import ConflictError


def _retailer_id(repos) -> UUID:
    return repos.master_data.add_retailer("CUST-PO-GOC", "Customer PO GOC", None)["id"]


def test_get_or_create_purchase_order_creates_on_first_call(repos):
    retailer_id = _retailer_id(repos)
    row = repos.purchase_orders.get_or_create_purchase_order(
        "PO-GOC-1", retailer_id=retailer_id, order_date=date(2026, 1, 1)
    )

    assert row["purchase_order_number"] == "PO-GOC-1"
    assert repos.purchase_orders.get_by_number("PO-GOC-1") is not None


def test_get_or_create_purchase_order_sequential_duplicate_returns_same_row(repos):
    retailer_id = _retailer_id(repos)
    first = repos.purchase_orders.get_or_create_purchase_order(
        "PO-GOC-2", retailer_id=retailer_id, order_date=date(2026, 1, 1)
    )
    second = repos.purchase_orders.get_or_create_purchase_order(
        "PO-GOC-2", retailer_id=retailer_id, order_date=date(2099, 12, 31)
    )

    assert second["id"] == first["id"]
    # Second call's different order_date must not overwrite the first.
    assert second["order_date"] == date(2026, 1, 1)


def test_create_purchase_order_still_raises_po_already_exists(repos):
    """Regression guard: create_purchase_order's existing hard-create
    contract is untouched by get_or_create_purchase_order's existence."""
    retailer_id = _retailer_id(repos)
    repos.purchase_orders.create_purchase_order(
        "PO-REGRESSION", retailer_id=retailer_id, order_date=date(2026, 1, 1)
    )

    with pytest.raises(ConflictError) as raised:
        repos.purchase_orders.create_purchase_order(
            "PO-REGRESSION", retailer_id=retailer_id, order_date=date(2026, 1, 1)
        )
    assert raised.value.code == "PO_ALREADY_EXISTS"


def test_get_or_create_line_creates_on_first_call(repos):
    retailer_id = _retailer_id(repos)
    po = repos.purchase_orders.get_or_create_purchase_order(
        "PO-GOC-3", retailer_id=retailer_id, order_date=date(2026, 1, 1)
    )

    line = repos.purchase_orders.get_or_create_line(
        po["id"], "10", 100.0, retailer_material_code="MAT-1", unit_price=0.0, line_status="NEW"
    )

    assert line["purchase_order_id"] == po["id"]
    assert line["line_number"] == "10"
    assert line["ordered_quantity"] == 100.0


def test_get_or_create_line_sequential_duplicate_does_not_overwrite(repos):
    retailer_id = _retailer_id(repos)
    po = repos.purchase_orders.get_or_create_purchase_order(
        "PO-GOC-4", retailer_id=retailer_id, order_date=date(2026, 1, 1)
    )

    first = repos.purchase_orders.get_or_create_line(
        po["id"], "10", 100.0, retailer_material_code="MAT-1", unit_price=0.0, line_status="NEW"
    )
    second = repos.purchase_orders.get_or_create_line(
        po["id"], "10", 999.0, retailer_material_code="MAT-DIFFERENT", unit_price=0.0, line_status="NEW"
    )

    assert second["id"] == first["id"]
    assert second["ordered_quantity"] == 100.0
    assert second["retailer_material_code"] == "MAT-1"
    assert len(repos.purchase_orders.list_lines(po["id"])) == 1


def test_get_or_create_line_different_line_numbers_create_separate_lines(repos):
    retailer_id = _retailer_id(repos)
    po = repos.purchase_orders.get_or_create_purchase_order(
        "PO-GOC-5", retailer_id=retailer_id, order_date=date(2026, 1, 1)
    )

    line1 = repos.purchase_orders.get_or_create_line(
        po["id"], "10", 100.0, retailer_material_code="MAT-1", unit_price=0.0, line_status="NEW"
    )
    line2 = repos.purchase_orders.get_or_create_line(
        po["id"], "20", 200.0, retailer_material_code="MAT-2", unit_price=0.0, line_status="NEW"
    )

    assert line1["id"] != line2["id"]
    assert len(repos.purchase_orders.list_lines(po["id"])) == 2


def test_add_line_still_works_unchanged(repos):
    """Regression guard: add_line's own unconditional-insert behavior is untouched."""
    retailer_id = _retailer_id(repos)
    po = repos.purchase_orders.create_purchase_order(
        "PO-REGRESSION-LINE", retailer_id=retailer_id, order_date=date(2026, 1, 1)
    )
    line = repos.purchase_orders.add_line(
        purchase_order_id=po["id"],
        line_number="10",
        ordered_quantity=50.0,
        retailer_material_code="MAT-X",
        unit_price=0.0,
        line_status="NEW",
    )
    assert line["ordered_quantity"] == 50.0
