"""Tests for `MaterialMasterService` -- the only thing allowed to call
`MasterDataRepository`'s `MaterialMaster` write method. Uses the real
SQLite-backed `repos` fixture (`tests/conftest.py`), same as
`tests/unit/repositories/test_master_data_repository.py`, since this
service's whole job is orchestrating that repository correctly, not
standing in for it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.exceptions import ConflictError, NotFoundError
from app.services.common.material_master_service import MaterialMasterService


@pytest.fixture
def service(repos) -> MaterialMasterService:
    return MaterialMasterService(master_data_repository=repos.master_data)


def test_create_material_master_creates_all_three_rows(repos, service):
    created = service.create_material_master(
        material_code="MAT-3000", plant_code="P100", sap_material_number="SAP-3000"
    )

    material = repos.master_data.get_material_by_code("MAT-3000")
    plant = repos.master_data.get_plant_by_code("P100")
    assert material is not None
    assert plant is not None
    assert created["material_id"] == str(material["id"])
    assert created["plant_id"] == str(plant["id"])

    masters = repos.master_data.list_material_masters_for_material(material["id"])
    assert len(masters) == 1
    assert masters[0]["sap_material_number"] == "SAP-3000"
    assert masters[0]["plant_id"] == plant["id"]
    assert created["material_master_id"] == str(masters[0]["id"])


def test_create_material_master_reuses_an_existing_plant(repos, service):
    existing_plant = repos.master_data.add_plant("P100", None, None)

    created = service.create_material_master(
        material_code="MAT-3000", plant_code="P100", sap_material_number="SAP-3000"
    )

    assert created["plant_id"] == str(existing_plant["id"])
    assert repos.master_data.get_plant_by_code("P100")["id"] == existing_plant["id"]


def test_create_material_master_rejects_a_duplicate_material_code(repos, service):
    repos.master_data.add_material("MAT-3000", None)

    with pytest.raises(ConflictError) as exc_info:
        service.create_material_master(
            material_code="MAT-3000", plant_code="P100", sap_material_number="SAP-3000"
        )

    assert exc_info.value.code == "MATERIAL_ALREADY_EXISTS"
    assert repos.master_data.get_plant_by_code("P100") is None


def test_create_material_master_rejects_a_duplicate_sap_number_at_the_same_plant(repos, service):
    plant = repos.master_data.add_plant("P100", None, None)
    other_material = repos.master_data.add_material("MAT-OTHER", None)
    repos.master_data.add_material_master(
        material_id=other_material["id"], sap_material_number="SAP-3000", plant_id=plant["id"]
    )

    with pytest.raises(ConflictError) as exc_info:
        service.create_material_master(
            material_code="MAT-3000", plant_code="P100", sap_material_number="SAP-3000"
        )

    assert exc_info.value.code == "MATERIAL_MASTER_ALREADY_EXISTS"
    # Nothing else got written either -- the new Material was never created.
    assert repos.master_data.get_material_by_code("MAT-3000") is None


def test_update_replacement_material_sets_follow_up_material_id(repos, service):
    material = repos.master_data.add_material("MAT-DISC-1001", None)
    replacement = repos.master_data.add_material("MAT-REPL-1001", None)
    plant = repos.master_data.add_plant("1000", None, None)
    master = repos.master_data.add_material_master(
        material_id=material["id"], sap_material_number="SAP-1", plant_id=plant["id"]
    )

    updated = service.update_replacement_material(
        material_master_id=master["id"], replacement_material_id=replacement["id"]
    )

    assert updated["follow_up_material_id"] == replacement["id"]


def test_update_replacement_material_rejects_a_nonexistent_replacement(repos, service):
    material = repos.master_data.add_material("MAT-DISC-1001", None)
    plant = repos.master_data.add_plant("1000", None, None)
    master = repos.master_data.add_material_master(
        material_id=material["id"], sap_material_number="SAP-1", plant_id=plant["id"]
    )

    with pytest.raises(NotFoundError) as exc_info:
        service.update_replacement_material(material_master_id=master["id"], replacement_material_id=uuid4())

    assert exc_info.value.code == "MATERIAL_NOT_FOUND"
    # Nothing was written -- re-reading confirms follow_up_material_id is
    # still unset, not silently pointed at a UUID with no real row.
    rows = repos.master_data.list_material_masters_for_material(material["id"])
    assert rows[0]["follow_up_material_id"] is None


def test_update_replacement_material_rejects_a_nonexistent_material_master(repos, service):
    replacement = repos.master_data.add_material("MAT-REPL-1001", None)

    with pytest.raises(NotFoundError) as exc_info:
        service.update_replacement_material(
            material_master_id=uuid4(), replacement_material_id=replacement["id"]
        )

    assert exc_info.value.code == "MATERIAL_MASTER_NOT_FOUND"
