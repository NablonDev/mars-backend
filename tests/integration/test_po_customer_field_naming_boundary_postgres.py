"""Regression test protecting the FINAL naming refactor decision: the
PO-validation API/LangGraph-state layer uses `retailer_code`/
`retailer_material_code` (was `customer_id`/`customer_material_code`),
matching the DB/domain vocabulary (common.retailer.retailer_code /
common.purchase_order_line.retailer_material_code) -- no DB column was
renamed, only the application-level contract.

Proves, against REAL Postgres and the REAL production PoValidationService
(via app.api.dependencies.build_po_validation_service(), the same
Container-backed po_validation_unit_of_work() factory the live API route
uses -- not a standalone repository/session or a fake graph):

1. payload["retailer_code"] -> common.retailer.retailer_code
2. payload["retailer_material_code"] -> common.purchase_order_line.retailer_material_code
3. The same values reach LangGraph state and are what
   validate_against_cmir's REAL Postgres CMIR lookup actually uses -- proven
   by a genuine crosswalk-miss (new retailer/material -> interrupts at
   manual_cmir_entry), followed by a manual entry creating the crosswalk,
   followed by a SECOND ingest with the SAME retailer_code/retailer_material_code
   resolving touchlessly (the "found" branch) through check_material_master.
4. A LEGACY checkpoint -- one whose `po_line` state still has the OLD
   customer_id/customer_material_code keys (as any thread that interrupted
   before this rename would) -- can still resume without KeyError, via
   `app.agents.po_validation.nodes._normalize_po_line`.

Mirrors tests/integration/test_po_ingestion_concurrency_postgres.py's
`_connect_or_none`/skip-cleanly-if-unreachable pattern.
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
from langgraph.types import Command
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.db.session import Database


def _connect_or_none() -> Database | None:
    settings = get_settings()
    if not settings.database.url.startswith("postgresql"):
        return None

    db = Database(settings.database.url)
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        db.dispose()
        return None
    return db


@pytest.fixture(scope="module")
def pg_database():
    db = _connect_or_none()
    if db is None:
        pytest.skip(
            "No reachable Postgres DATABASE_URL configured -- skipping the retailer-field "
            "naming-boundary integration test."
        )
    yield db
    db.dispose()


def test_retailer_code_and_material_code_map_to_db_and_state_correctly(pg_database: Database) -> None:
    from app.api.dependencies import build_po_validation_service

    service = build_po_validation_service()

    unique = uuid.uuid4().hex[:10]
    retailer_code = f"NAMING-CUST-{unique}"
    retailer_material_code = f"NAMING-MAT-{unique}"
    plant = f"NAMING-PLANT-{unique}"
    po_number_1 = f"NAMING-PO1-{unique}"

    verify = pg_database.new_session()
    try:
        # Precondition: genuinely no existing crosswalk for this brand-new pair.
        miss_count = verify.execute(
            text(
                "SELECT count(*) FROM cmir.cmir_record "
                "WHERE customer_identity = :c AND target_customer_material_ref = :m AND is_current = true"
            ),
            {"c": retailer_code, "m": retailer_material_code},
        ).scalar()
        assert miss_count == 0
    finally:
        verify.close()

    result1 = service.ingest_po_lines(
        [
            {
                "po_number": po_number_1,
                "po_line_number": "10",
                "retailer_code": retailer_code,
                "retailer_material_code": retailer_material_code,
                "plant": plant,
                "order_quantity": 5,
            }
        ]
    )
    thread_id_1 = result1["lines"][0]["thread_id"]
    assert thread_id_1 is not None, "a genuine crosswalk-miss must create a workflow_thread, not resolve touchlessly"
    thread_id_1 = UUID(thread_id_1)

    verify = pg_database.new_session()
    try:
        # 1: payload["retailer_code"] -> common.retailer.retailer_code
        db_retailer_code = verify.execute(
            text(
                "SELECT r.retailer_code FROM common.retailer r "
                "JOIN common.purchase_order po ON po.retailer_id = r.id "
                "WHERE po.purchase_order_number = :n"
            ),
            {"n": po_number_1},
        ).scalar()
        assert db_retailer_code == retailer_code

        # 2: payload["retailer_material_code"] -> purchase_order_line.retailer_material_code
        db_retailer_material_code = verify.execute(
            text(
                "SELECT l.retailer_material_code FROM common.purchase_order_line l "
                "JOIN common.purchase_order po ON po.id = l.purchase_order_id "
                "WHERE po.purchase_order_number = :n"
            ),
            {"n": po_number_1},
        ).scalar()
        assert db_retailer_material_code == retailer_material_code

        # Confirms the real node code actually took the crosswalk-miss branch
        # (human_manual_cmir_entry), not some other/error path.
        thread_status = verify.execute(
            text("SELECT status FROM process.workflow_thread WHERE id = :id"), {"id": thread_id_1}
        ).scalar()
        assert thread_status == "waiting_manual_cmir_entry"
    finally:
        verify.close()

    # Seed a real material_master row so the manual entry resolves cleanly
    # (no API exists for this -- real repository methods, per established precedent).
    sap_material_number = f"NAMING-SAP-{unique}"
    with service._unit_of_work_factory() as uow:
        plant_row = uow.master_data.get_or_create_plant(plant)
        material = uow.master_data.add_material(sap_material_number, "naming boundary test material")
        uow.master_data.add_material_master(
            material_id=material["id"],
            sap_material_number=sap_material_number,
            plant_id=plant_row["id"],
            available_quantity=1000,
        )

    stage = service.get_stage(thread_id_1)
    manual_entry_result = service.submit_manual_cmir_entry(
        thread_id_1,
        actor="naming-boundary-test",
        sap_material_number=sap_material_number,
        description="",
        expected_updated_at=stage["updated_at"].isoformat(),
    )
    assert manual_entry_result["status"] == "ready_for_so_creation"

    verify = pg_database.new_session()
    try:
        crosswalk = verify.execute(
            text(
                "SELECT customer_identity, target_customer_material_ref FROM cmir.cmir_record "
                "WHERE customer_identity = :c AND target_customer_material_ref = :m AND is_current = true"
            ),
            {"c": retailer_code, "m": retailer_material_code},
        ).first()
        assert crosswalk is not None, "create_cmir_record must have written the crosswalk using the state values"
    finally:
        verify.close()

    # 3: second ingest, SAME retailer_code/retailer_material_code -> must now
    # resolve touchlessly (the "found" branch through check_material_master),
    # which is only possible if validate_against_cmir read these exact state
    # values on BOTH ingests -- proving the state->CMIR-lookup mapping, not
    # just the DB write mapping proven above.
    po_number_2 = f"NAMING-PO2-{unique}"
    result2 = service.ingest_po_lines(
        [
            {
                "po_number": po_number_2,
                "po_line_number": "10",
                "retailer_code": retailer_code,
                "retailer_material_code": retailer_material_code,
                "plant": plant,
                "order_quantity": 5,
            }
        ]
    )
    assert result2["lines"][0]["thread_id"] is None, (
        "the crosswalk hit must resolve touchlessly -- if this instead interrupted again, "
        "the state->CMIR lookup mapping would be broken"
    )
    assert result2["lines"][0]["status"] == "READY_FOR_SO_CREATION"


def test_legacy_checkpoint_with_old_field_names_resumes_without_keyerror(pg_database: Database) -> None:
    """Simulates a thread whose checkpoint was written BEFORE the
    customer_id/customer_material_code -> retailer_code/retailer_material_code
    rename -- its `po_line` state still has the old key names, exactly like
    the 48 real open `manual_cmir_entry` interrupts found in this local DB
    during the Phase 1 investigation. Proves `_normalize_po_line` lets such
    a thread resume all the way through `create_cmir_record` without a
    KeyError, using the REAL, unstubbed PO-validation graph (no LLM
    dependency) -- not a fake/mocked node.
    """
    from app.core.container import Container

    container = Container.build()
    unique = uuid.uuid4().hex[:10]
    retailer_code = f"LEGACY-CUST-{unique}"
    retailer_material_code = f"LEGACY-MAT-{unique}"
    plant_code = f"LEGACY-PLANT-{unique}"
    sap_material_number = f"LEGACY-SAP-{unique}"

    with container.po_validation_unit_of_work() as uow:
        retailer = uow.master_data.get_or_create_retailer(retailer_code, retailer_code, None)
        plant = uow.master_data.get_or_create_plant(plant_code)
        material = uow.master_data.add_material(sap_material_number, "legacy checkpoint test material")
        uow.master_data.add_material_master(
            material_id=material["id"],
            sap_material_number=sap_material_number,
            plant_id=plant["id"],
            available_quantity=1000,
        )
        purchase_order = uow.purchase_orders.get_or_create_purchase_order(
            f"LEGACY-PO-{unique}", retailer_id=retailer["id"], order_date=__import__("datetime").date.today()
        )
        line = uow.purchase_orders.get_or_create_line(
            purchase_order["id"],
            "10",
            5,
            retailer_material_code=retailer_material_code,
            plant_id=plant["id"],
            unit_price=0.0,
            line_status="NEW",
        )

        checkpoint_thread_id = f"legacy-thread-{unique}"
        agent_id = uow.agent_registry.ensure_registered(
            agent_code="po_validation_agent",
            prompt_version="v1",
            system_prompt="",
            agent_name="PO Validation Agent",
            domain="cmir",
        )
        run_id = uow.agent_runs.start(agent_id=agent_id, run_type="PO_VALIDATION")

        # OLD-shaped po_line state, exactly as a pre-rename checkpoint would
        # have stored it -- customer_id/customer_material_code, not
        # retailer_code/retailer_material_code.
        legacy_initial_state = {
            "batch_id": "legacy-checkpoint-test",
            "run_id": run_id,
            "po_line_id": line["id"],
            "thread_id": checkpoint_thread_id,
            "po_line": {
                "po_number": f"LEGACY-PO-{unique}",
                "po_line_number": "10",
                "customer_id": retailer_code,
                "customer_material_code": retailer_material_code,
                "plant": plant_code,
                "plant_id": plant["id"],
                "order_quantity": 5,
                "uom": None,
            },
        }

        config = {"configurable": {"thread_id": checkpoint_thread_id}}
        state = uow.graph.invoke(legacy_initial_state, config=config)
        assert "__interrupt__" in state, "expected a genuine crosswalk-miss interrupt (manual_cmir_entry)"
        interrupt_payload = state["__interrupt__"][0].value
        assert interrupt_payload["reason"] == "manual_cmir_entry"

        # Resume -- this is the step that would KeyError without
        # _normalize_po_line, since `human_manual_cmir_entry` re-reads
        # state["po_line"] on resume, and `create_cmir_record` reads it again
        # after that -- both still only have the OLD key names in this
        # checkpoint.
        final_state = uow.graph.invoke(
            Command(resume={"sap_material_number": sap_material_number, "description": ""}), config=config
        )
        assert "__interrupt__" not in final_state, f"expected no further interrupt, got state: {final_state}"
        assert final_state.get("error") is None, f"legacy checkpoint resume raised an error: {final_state.get('error')}"

    # Independent verification: the crosswalk was written using the OLD
    # checkpoint's values, correctly bridged through _normalize_po_line.
    verify = pg_database.new_session()
    try:
        crosswalk = verify.execute(
            text(
                "SELECT customer_identity, target_customer_material_ref FROM cmir.cmir_record "
                "WHERE customer_identity = :c AND target_customer_material_ref = :m AND is_current = true"
            ),
            {"c": retailer_code, "m": retailer_material_code},
        ).first()
        assert crosswalk is not None, "legacy-checkpoint resume must still write the crosswalk correctly"

        line_status = verify.execute(
            text("SELECT line_status FROM common.purchase_order_line WHERE id = :id"), {"id": line["id"]}
        ).scalar()
        assert line_status == "READY_FOR_SO_CREATION"
    finally:
        verify.close()
