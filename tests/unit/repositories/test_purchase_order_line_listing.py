"""Tests for `PurchaseOrderRepository.list_lines_by_status` -- the
cross-purchase-order `purchase_order_line` listing `PoValidationService.
list_ready_lines` needed (Gap 3: this query didn't exist at all before;
only `list_lines(purchase_order_id)`, scoped to one PO)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.models import PurchaseOrderLine


def _seed_line(repos, po_number: str, line_number: str, line_status: str) -> dict:
    retailer = repos.master_data.add_retailer(f"RET-{po_number}-{line_number}", "Retailer", None, "SUM")
    plant = repos.master_data.add_plant(f"PLANT-{po_number}-{line_number}", None, None)
    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number=po_number, retailer_id=retailer["id"], order_date=date(2026, 1, 1)
    )
    return repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number=line_number,
        ordered_quantity=100,
        unit_price=0.0,
        plant_id=plant["id"],
        line_status=line_status,
    )


def test_list_lines_by_status_filters_across_purchase_orders(repos):
    ready_a = _seed_line(repos, "PO-LIST-A", "10", "READY_FOR_SO_CREATION")
    ready_b = _seed_line(repos, "PO-LIST-B", "10", "READY_FOR_SO_CREATION_PARTIAL")
    _seed_line(repos, "PO-LIST-C", "10", "AWAITING_DECISION")

    items, next_cursor = repos.purchase_orders.list_lines_by_status(
        ("READY_FOR_SO_CREATION", "READY_FOR_SO_CREATION_PARTIAL")
    )

    ids = {row["id"] for row in items}
    assert ready_a["id"] in ids
    assert ready_b["id"] in ids
    assert next_cursor is None


def test_list_lines_by_status_single_value(repos):
    awaiting = _seed_line(repos, "PO-LIST-D", "10", "AWAITING_DECISION")
    _seed_line(repos, "PO-LIST-E", "10", "READY_FOR_SO_CREATION")

    items, _ = repos.purchase_orders.list_lines_by_status("AWAITING_DECISION")

    assert [row["id"] for row in items] == [awaiting["id"]]


def test_list_lines_by_status_none_returns_every_line(repos):
    a = _seed_line(repos, "PO-LIST-F", "10", "NEW")
    b = _seed_line(repos, "PO-LIST-G", "10", "FAILED")

    items, _ = repos.purchase_orders.list_lines_by_status(None)

    ids = {row["id"] for row in items}
    assert a["id"] in ids
    assert b["id"] in ids


def test_list_lines_by_status_scoped_to_one_purchase_order_ignores_status_default(repos):
    """`purchase_order_id` given, `line_status=None`: every line for that PO
    regardless of status -- the old nested `GET /purchase-orders/{id}/lines`
    route's behavior, now folded into this one query (see
    `PoValidationService.list_ready_lines`)."""
    scoped = _seed_line(repos, "PO-LIST-SCOPED", "10", "AWAITING_DECISION")
    _seed_line(repos, "PO-LIST-OTHER", "10", "READY_FOR_SO_CREATION")

    items, _ = repos.purchase_orders.list_lines_by_status(None, purchase_order_id=scoped["purchase_order_id"])

    assert [row["id"] for row in items] == [scoped["id"]]


def test_list_lines_by_status_combines_purchase_order_id_and_status(repos):
    scoped_ready = _seed_line(repos, "PO-LIST-COMBO", "10", "READY_FOR_SO_CREATION")
    repos.purchase_orders.add_line(
        purchase_order_id=scoped_ready["purchase_order_id"],
        line_number="20",
        ordered_quantity=100,
        unit_price=0.0,
        plant_id=scoped_ready["plant_id"],
        line_status="AWAITING_DECISION",
    )

    items, _ = repos.purchase_orders.list_lines_by_status(
        "READY_FOR_SO_CREATION", purchase_order_id=scoped_ready["purchase_order_id"]
    )

    assert [row["id"] for row in items] == [scoped_ready["id"]]


def test_list_lines_by_status_paginates_with_cursor(repos, db_session):
    # Distinct, explicit updated_at values -- SQLite's func.now() only has
    # second resolution, so three rows created in the same test would
    # otherwise tie and make cursor pagination's strict "<" comparison
    # ambiguous (same caveat as WorkflowThreadRepository.get_latest_by_email_event).
    base = datetime(2026, 1, 1, tzinfo=UTC)
    lines = [_seed_line(repos, f"PO-LIST-PAGE-{i}", "10", "READY_FOR_SO_CREATION") for i in range(3)]
    for offset, line in enumerate(lines):
        row = db_session.get(PurchaseOrderLine, line["id"])
        row.updated_at = base + timedelta(seconds=offset)
    db_session.flush()

    first_page, cursor = repos.purchase_orders.list_lines_by_status("READY_FOR_SO_CREATION", limit=2)
    assert len(first_page) == 2
    assert cursor is not None

    second_page, next_cursor = repos.purchase_orders.list_lines_by_status(
        "READY_FOR_SO_CREATION", limit=2, cursor=cursor
    )
    assert len(second_page) == 1
    assert next_cursor is None
    assert {row["id"] for row in first_page}.isdisjoint({row["id"] for row in second_page})
