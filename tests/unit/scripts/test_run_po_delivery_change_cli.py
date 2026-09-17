"""Tests for scripts/ops/run_po_delivery_change_cli.py's four subcommands
(create/respond/history/expire-sweep) -- exit codes, clean (non-traceback)
error output on the domain exceptions, and output content. Mirrors
tests/unit/services/test_delivery_change_service.py's seeding (dates
computed relative to date.today(), not hardcoded) and
tests/unit/scripts/test_run_daily_batch.py's `_invoke_main` pattern for
asserting on `main()`'s exit code via `sys.exit(main())`.

Was written against the pre-restructure `app.repositories.order`/
`app.repositories.fine_projection.po_delivery_change_request` -- rewritten
against the Phase 2/3 `common`/`penalties` repositories and services (the
`fine`/`fines` -> `penalty`/`penalties` rename, and `Po*` ->
`PurchaseOrder*` naming), keyed by the UUID surrogate `purchase_order_id`.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

from sqlalchemy.pool import StaticPool

import scripts.ops.run_po_delivery_change_cli as cli
from app.db.session import Database
from app.repositories.common.delivery_change_request import PoDeliveryChangeRequestRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule import PenaltyRuleRepository

_ORDER_QTY = 1000
_UNIT_PRICE = 10.0
_TODAY = date.today()  # noqa: DTZ011 -- test date anchor, same convention as the service test module


def _invoke_main() -> int:
    """Mirrors what `sys.exit(main())` actually does at the OS level -- see
    tests/unit/scripts/test_run_daily_batch.py's `_invoke_main` for the full
    rationale."""
    try:
        return cli.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    except BaseException:  # noqa: BLE001 -- deliberately mirrors Python's own default excepthook
        return 1


def _make_database() -> Database:
    database = Database("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    database.create_all_tables()
    return database


def _seed_purchase_order(
    database: Database,
    po_number: str,
    *,
    required_ship_date: date,
    requested_delivery_date: date,
    retailer_code: str = "RET-CLI-EXT",
    extension_min_lead_days: int = 2,
    extension_response_sla_hours: int = 48,
) -> str:
    with database.session() as session:
        master_data = MasterDataRepository(session)
        rules = PenaltyRuleRepository(session)
        purchase_orders = PurchaseOrderRepository(session)
        retailer_agreements = RetailerAgreementRepository(session)

        retailer = master_data.add_retailer(
            retailer_code,
            "CLI Extension Test Retailer",
            None,
            "SUM",
            extension_min_lead_days=extension_min_lead_days,
            extension_response_sla_hours=extension_response_sla_hours,
            extension_penalty_threshold=0.0,
        )
        material = master_data.add_material(f"MAT-{retailer_code}", None)
        plant = master_data.add_plant(f"PLANT-{retailer_code}", None, None)
        retailer_agreement = retailer_agreements.add_retailer_agreement(
            retailer_id=retailer["id"],
            contract_code=f"TEST-{retailer_code}",
            title="Test retailer agreement",
            document_sha256=hashlib.sha256(f"test:{retailer_code}".encode()).hexdigest(),
        )
        rules.add_rule(
            rule_code=f"RULE-{po_number}",
            violation_type="OTIF_LATE",
            penalty_category="OTIF_LATE",
            retailer_agreement_id=retailer_agreement["id"],
            calc_type="FLAT_FEE",
            rate=25.0,
        )
        purchase_order = purchase_orders.create_purchase_order(
            purchase_order_number=po_number,
            retailer_id=retailer["id"],
            order_date=_TODAY,
            requested_delivery_date=requested_delivery_date,
            required_ship_date=required_ship_date,
        )
        purchase_orders.add_line(
            purchase_order_id=purchase_order["id"],
            line_number="10",
            ordered_quantity=_ORDER_QTY,
            unit_price=_UNIT_PRICE,
            material_id=material["id"],
            plant_id=plant["id"],
        )
        return str(purchase_order["id"])


def _create_request(
    database: Database, purchase_order_id: str, reason_code: str, proposed_delivery_date: date
) -> dict:
    with database.session() as session:
        service = cli._build_service(session)
        return service.create_request(cli.UUID(purchase_order_id), reason_code, proposed_delivery_date)


def test_create_succeeds_and_prints_request(monkeypatch, capsys):
    database = _make_database()
    purchase_order_id = _seed_purchase_order(
        database,
        "PO-CLI-1",
        required_ship_date=_TODAY + timedelta(days=10),
        requested_delivery_date=_TODAY + timedelta(days=12),
    )
    monkeypatch.setattr(cli, "Database", lambda *_a, **_k: database)
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_po_delivery_change_cli.py",
            "create",
            "--purchase-order-id",
            purchase_order_id,
            "--reason-code",
            "SHORTAGE",
            "--proposed-delivery-date",
            (_TODAY + timedelta(days=16)).isoformat(),
        ],
    )

    assert _invoke_main() == 0

    out = capsys.readouterr().out
    assert "Created " in out
    assert purchase_order_id in out
    assert "SHORTAGE" in out
    assert "Traceback" not in out


def test_create_insufficient_lead_time_exits_nonzero_without_traceback(monkeypatch, capsys):
    database = _make_database()
    purchase_order_id = _seed_purchase_order(
        database,
        "PO-CLI-2",
        required_ship_date=_TODAY,  # 0 days lead, default extension_min_lead_days=2
        requested_delivery_date=_TODAY + timedelta(days=2),
    )
    monkeypatch.setattr(cli, "Database", lambda *_a, **_k: database)
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_po_delivery_change_cli.py",
            "create",
            "--purchase-order-id",
            purchase_order_id,
            "--reason-code",
            "SHORTAGE",
            "--proposed-delivery-date",
            (_TODAY + timedelta(days=6)).isoformat(),
        ],
    )

    assert _invoke_main() != 0

    out = capsys.readouterr().out
    assert out.startswith("Error:")
    assert "Traceback" not in out


def test_respond_accepted_updates_order_dates(monkeypatch, capsys):
    database = _make_database()
    purchase_order_id = _seed_purchase_order(
        database,
        "PO-CLI-3",
        required_ship_date=_TODAY + timedelta(days=10),
        requested_delivery_date=_TODAY + timedelta(days=12),
        retailer_code="RET-CLI-3",
    )
    request = _create_request(database, purchase_order_id, "DELAY", _TODAY + timedelta(days=16))

    monkeypatch.setattr(cli, "Database", lambda *_a, **_k: database)
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_po_delivery_change_cli.py",
            "respond",
            "--id",
            str(request["id"]),
            "--decision",
            "ACCEPTED",
        ],
    )

    assert _invoke_main() == 0
    out = capsys.readouterr().out
    assert "Recorded ACCEPTED" in out
    assert str(request["id"]) in out


def test_respond_countered_updates_order_dates(monkeypatch, capsys):
    database = _make_database()
    purchase_order_id = _seed_purchase_order(
        database,
        "PO-CLI-4",
        required_ship_date=_TODAY + timedelta(days=10),
        requested_delivery_date=_TODAY + timedelta(days=12),
        retailer_code="RET-CLI-4",
    )
    request = _create_request(database, purchase_order_id, "DELAY", _TODAY + timedelta(days=16))

    monkeypatch.setattr(cli, "Database", lambda *_a, **_k: database)
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_po_delivery_change_cli.py",
            "respond",
            "--id",
            str(request["id"]),
            "--decision",
            "COUNTERED",
            "--countered-delivery-date",
            (_TODAY + timedelta(days=14)).isoformat(),
        ],
    )

    assert _invoke_main() == 0
    out = capsys.readouterr().out
    assert "Recorded COUNTERED" in out
    assert "countered=" in out


def test_respond_rejected_leaves_order_untouched(monkeypatch, capsys):
    database = _make_database()
    purchase_order_id = _seed_purchase_order(
        database,
        "PO-CLI-5",
        required_ship_date=_TODAY + timedelta(days=10),
        requested_delivery_date=_TODAY + timedelta(days=12),
        retailer_code="RET-CLI-5",
    )
    request = _create_request(database, purchase_order_id, "SHORTAGE", _TODAY + timedelta(days=16))

    monkeypatch.setattr(cli, "Database", lambda *_a, **_k: database)
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_po_delivery_change_cli.py",
            "respond",
            "--id",
            str(request["id"]),
            "--decision",
            "REJECTED",
        ],
    )

    assert _invoke_main() == 0
    out = capsys.readouterr().out
    assert "Recorded REJECTED" in out


def test_respond_countered_missing_countered_date_errors_cleanly(monkeypatch, capsys):
    database = _make_database()
    purchase_order_id = _seed_purchase_order(
        database,
        "PO-CLI-6",
        required_ship_date=_TODAY + timedelta(days=10),
        requested_delivery_date=_TODAY + timedelta(days=12),
        retailer_code="RET-CLI-6",
    )
    request = _create_request(database, purchase_order_id, "DELAY", _TODAY + timedelta(days=16))

    monkeypatch.setattr(cli, "Database", lambda *_a, **_k: database)
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_po_delivery_change_cli.py",
            "respond",
            "--id",
            str(request["id"]),
            "--decision",
            "COUNTERED",
        ],
    )

    assert _invoke_main() != 0
    out = capsys.readouterr().out
    assert out.startswith("Error:")
    assert "countered_delivery_date" in out
    assert "Traceback" not in out


def test_history_lists_in_chronological_order(monkeypatch, capsys):
    database = _make_database()
    purchase_order_id = _seed_purchase_order(
        database,
        "PO-CLI-7",
        required_ship_date=_TODAY + timedelta(days=10),
        requested_delivery_date=_TODAY + timedelta(days=12),
        retailer_code="RET-CLI-7",
    )
    first = _create_request(database, purchase_order_id, "DELAY", _TODAY + timedelta(days=16))
    with database.session() as session:
        po_delivery_change_requests = PoDeliveryChangeRequestRepository(session)
        service = cli._build_service(session)
        service.record_response(first["id"], "REJECTED")
    second = _create_request(database, purchase_order_id, "SHORTAGE", _TODAY + timedelta(days=17))

    monkeypatch.setattr(cli, "Database", lambda *_a, **_k: database)
    monkeypatch.setattr(
        "sys.argv",
        ["run_po_delivery_change_cli.py", "history", "--purchase-order-id", purchase_order_id],
    )

    assert _invoke_main() == 0
    out = capsys.readouterr().out
    assert out.index(str(first["id"])) < out.index(str(second["id"]))

    # Confirm against the repository directly, too -- list_history's own
    # ordering contract is already covered by the service-level tests, this
    # asserts the CLI didn't reorder what it got back.
    with database.session() as session:
        history = po_delivery_change_requests.list_history(cli.UUID(purchase_order_id))
    assert [row["id"] for row in history] == [first["id"], second["id"]]


def test_history_unknown_purchase_order_errors_cleanly(monkeypatch, capsys):
    database = _make_database()
    monkeypatch.setattr(cli, "Database", lambda *_a, **_k: database)
    monkeypatch.setattr(
        "sys.argv",
        ["run_po_delivery_change_cli.py", "history", "--purchase-order-id", str(cli.UUID(int=0))],
    )

    assert _invoke_main() != 0
    out = capsys.readouterr().out
    assert out.startswith("Error:")
    assert "Traceback" not in out


def test_expire_sweep_transitions_stale_request_and_reports(monkeypatch, capsys):
    database = _make_database()
    purchase_order_id = _seed_purchase_order(
        database,
        "PO-CLI-8",
        required_ship_date=_TODAY + timedelta(days=10),
        requested_delivery_date=_TODAY + timedelta(days=12),
        retailer_code="RET-CLI-8",
        extension_response_sla_hours=48,
    )
    request = _create_request(database, purchase_order_id, "DELAY", _TODAY + timedelta(days=16))
    as_of = request["requested_at"] + timedelta(hours=49)

    monkeypatch.setattr(cli, "Database", lambda *_a, **_k: database)
    monkeypatch.setattr(
        "sys.argv",
        ["run_po_delivery_change_cli.py", "expire-sweep", "--as-of", as_of.isoformat()],
    )

    assert _invoke_main() == 0
    out = capsys.readouterr().out
    assert "Expired 1 stale PO delivery-change request(s)." in out
    assert str(request["id"]) in out
    assert purchase_order_id in out

    with database.session() as session:
        po_delivery_change_requests = PoDeliveryChangeRequestRepository(session)
        row = po_delivery_change_requests.get_by_id(request["id"])
    assert row["status"] == "EXPIRED"


def test_expire_sweep_no_stale_requests_reports_zero(monkeypatch, capsys):
    database = _make_database()
    monkeypatch.setattr(cli, "Database", lambda *_a, **_k: database)
    monkeypatch.setattr("sys.argv", ["run_po_delivery_change_cli.py", "expire-sweep"])

    assert _invoke_main() == 0
    out = capsys.readouterr().out
    assert "Expired 0 stale PO delivery-change request(s)." in out
