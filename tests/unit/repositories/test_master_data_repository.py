"""Tests for MasterDataRepository's material_master lookup. Was
tests/unit/repositories/test_po_validation_repositories.py's
`PostgresMaterialMasterRepository` coverage (`MaterialMasterORM`) --
relocated onto `common.material_master`, now looked up by
`(sap_material_number, plant_id)` since `plant` is a real FK'd master-data
row rather than a bare string column (see app/models/common/material.py).
"""

import uuid
from datetime import date


def test_find_material_master_maps_row_to_dict(repos):
    material = repos.master_data.add_material("MAT-1", None)
    follow_up = repos.master_data.add_material("MAT-SUB", None)
    plant = repos.master_data.add_plant("1000", None, None)
    repos.master_data.add_material_master(
        material_id=material["id"],
        sap_material_number="MAT-1",
        plant_id=plant["id"],
        available_quantity=40,
        follow_up_material_id=follow_up["id"],
        effective_out_date=date(2026, 12, 31),
    )

    record = repos.master_data.find_material_master("MAT-1", plant["id"])

    assert record is not None
    assert record["available_quantity"] == 40
    assert record["follow_up_material_id"] == follow_up["id"]
    assert record["effective_out_date"] == date(2026, 12, 31)


def test_find_material_master_returns_none_when_missing(repos):
    plant = repos.master_data.add_plant("1000", None, None)
    assert repos.master_data.find_material_master("MAT-UNKNOWN", plant["id"]) is None


def test_get_material_by_id_returns_the_material(repos):
    material = repos.master_data.add_material("MAT-1", None)
    assert repos.master_data.get_material_by_id(material["id"]) == material


def test_get_material_by_id_returns_none_when_missing(repos):
    assert repos.master_data.get_material_by_id(uuid.uuid4()) is None


def test_list_material_masters_for_material_returns_every_plant_row(repos):
    material = repos.master_data.add_material("MAT-1", None)
    plant_a = repos.master_data.add_plant("1000", None, None)
    plant_b = repos.master_data.add_plant("2000", None, None)
    repos.master_data.add_material_master(
        material_id=material["id"], sap_material_number="MAT-1-A", plant_id=plant_a["id"]
    )
    repos.master_data.add_material_master(
        material_id=material["id"], sap_material_number="MAT-1-B", plant_id=plant_b["id"]
    )

    rows = repos.master_data.list_material_masters_for_material(material["id"])

    assert {r["plant_id"] for r in rows} == {plant_a["id"], plant_b["id"]}


def test_update_material_master_follow_up_sets_the_column(repos):
    material = repos.master_data.add_material("MAT-DISC-1001", None)
    replacement = repos.master_data.add_material("MAT-REPL-1001", None)
    plant = repos.master_data.add_plant("1000", None, None)
    master = repos.master_data.add_material_master(
        material_id=material["id"], sap_material_number="SAP-1", plant_id=plant["id"]
    )
    assert master["follow_up_material_id"] is None

    updated = repos.master_data.update_material_master_follow_up(
        material_master_id=master["id"], follow_up_material_id=replacement["id"]
    )

    assert updated is not None
    assert updated["follow_up_material_id"] == replacement["id"]
    # Independent read-back through the same repository confirms the write
    # actually persisted to the row, not just to the returned dict.
    reread = repos.master_data.list_material_masters_for_material(material["id"])
    assert reread[0]["follow_up_material_id"] == replacement["id"]


def test_update_material_master_follow_up_returns_none_for_unknown_row(repos):
    assert (
        repos.master_data.update_material_master_follow_up(
            material_master_id=uuid.uuid4(), follow_up_material_id=None
        )
        is None
    )
