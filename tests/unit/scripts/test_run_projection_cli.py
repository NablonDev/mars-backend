"""Regression test for scripts/ops/run_projection_cli.py's penalty-projection-summary guard.

`SummaryServiceBase.run_generation` re-raises after persisting a FAILED
ledger row (see tests/unit/services/test_projection_summary_service.py for
the service-level coverage). This CLI has always printed the resulting
`summary [STATUS]` line regardless of success or failure, reading it back
via get_status -- that must still be true, and a run_generation failure
must NOT be misrouted into the `except AppError` branch, which prints a
different "[summary skipped]" message reserved for get_or_schedule failing
outright.

Was written against the pre-restructure `app.repositories.order`/
`app.services.fine_projection.*` -- rewritten against the Phase 2/3
`common`/`penalties` repositories and services (the `fine`/`fines` ->
`penalty`/`penalties` rename).
"""

from datetime import date

from sqlalchemy.pool import StaticPool

import scripts.ops.run_projection_cli as cli
from app.db.session import Database
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.rule import PenaltyRuleRepository


class _FailingChatClient:
    model_name = "fake-model"

    def __init__(self, *args, **kwargs) -> None:
        pass

    def invoke(self, messages, *, tools=None):
        raise RuntimeError("simulated upstream failure")


def _seed_purchase_order(database: Database) -> str:
    with database.session() as session:
        master_data = MasterDataRepository(session)
        rules = PenaltyRuleRepository(session)
        purchase_orders = PurchaseOrderRepository(session)
        retailer_agreements = RetailerAgreementRepository(session)

        retailer = master_data.add_retailer("RET-CLI", "Retailer CLI", None, "SUM")
        retailer_agreement = retailer_agreements.add_retailer_agreement(
            retailer_id=retailer["id"],
            contract_code="TEST-CLI",
            title="Test retailer agreement",
            document_sha256="0" * 64,
        )
        material = master_data.add_material("MAT-CLI", None)
        plant = master_data.add_plant("PLANT-CLI", None, None)
        purchase_order = purchase_orders.create_purchase_order(
            purchase_order_number="PO-CLI",
            retailer_id=retailer["id"],
            order_date=date(2026, 8, 1),
            requested_delivery_date=date(2026, 8, 10),
            required_ship_date=date(2026, 8, 8),
        )
        purchase_orders.add_line(
            purchase_order_id=purchase_order["id"],
            line_number="10",
            ordered_quantity=100,
            unit_price=5.0,
            material_id=material["id"],
            plant_id=plant["id"],
        )
        rules.add_rule(
            rule_code="RULE-CLI-FLAT",
            violation_type="OTIF_LATE",
            penalty_category="OTIF_LATE",
            retailer_agreement_id=retailer_agreement["id"],
            calc_type="FLAT_FEE",
            rate=50.0,
        )
        return str(purchase_order["id"])


def test_run_generation_failure_still_prints_the_status_line(monkeypatch, capsys):
    database = Database("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    database.create_all_tables()
    purchase_order_id = _seed_purchase_order(database)

    monkeypatch.setattr(cli, "Database", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(cli, "AzureOpenAIChatClient", _FailingChatClient)
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_projection_cli.py",
            "--purchase-order-id",
            purchase_order_id,
            "--date",
            "2026-08-05",
            "--with-summary",
        ],
    )

    cli.main()

    out = capsys.readouterr().out
    assert "summary [FAILED]" in out
    assert "[summary skipped]" not in out


def test_run_generation_success_still_prints_the_status_line(monkeypatch, capsys):
    """Same guard, success path: unaffected by the try/except added around
    run_generation."""

    class _SucceedingChatClient:
        model_name = "fake-model"

        def __init__(self, *args, **kwargs) -> None:
            pass

        def invoke(self, messages, *, tools=None):
            from types import SimpleNamespace

            return SimpleNamespace(content="All clear.", tool_calls=[])

    database = Database("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    database.create_all_tables()
    purchase_order_id = _seed_purchase_order(database)

    monkeypatch.setattr(cli, "Database", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(cli, "AzureOpenAIChatClient", _SucceedingChatClient)
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_projection_cli.py",
            "--purchase-order-id",
            purchase_order_id,
            "--date",
            "2026-08-05",
            "--with-summary",
        ],
    )

    cli.main()

    out = capsys.readouterr().out
    assert "summary [READY]" in out
    assert "[summary skipped]" not in out
