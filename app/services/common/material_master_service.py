"""Owns business validation/orchestration for writes to `MaterialMaster`
(and the `Material`/`Plant` rows an insert creates alongside it). Callers:
the ontology-update POC's `execute_update` node
(`app/agents/ontology_update/nodes.py`) and the ontology-insert POC's
`execute_insert` node (`app/agents/ontology_insert/nodes.py`) -- the Agent
boundary this exists to enforce is that an Agent calls this service, never
`MasterDataRepository` directly, for any write:

    LangGraph Agent -> MaterialMasterService -> MasterDataRepository -> PostgreSQL

No `MaterialService`/`MaterialMasterService` existed anywhere in this
codebase before the ontology-update POC (confirmed during that POC's
Checkpoint 1 inspection -- writes were previously called directly from an
API route or seeding module, with no service layer at all for
`common.material_master`). This stays the smallest service that satisfies
the Agent/Repository separation both POCs require, not a general
master-data service -- extending it to other fields/operations beyond
update-replacement and create is further next-phase generalization work.
"""

from __future__ import annotations

from uuid import UUID

from app.core.exceptions import ConflictError, NotFoundError
from app.repositories.common.master_data import MasterDataRepository


class MaterialMasterService:
    def __init__(self, *, master_data_repository: MasterDataRepository) -> None:
        self._master_data_repository = master_data_repository

    def create_material_master(
        self, *, material_code: str, plant_code: str, sap_material_number: str
    ) -> dict:
        """Create a new `Material` + `MaterialMaster` row together, at
        `plant_code` (reusing that plant if it already exists -- the
        ontology-insert POC's confirmed scope: only the Material/
        MaterialMaster pair is genuinely new on every call, not the Plant).

        Re-validates for conflicts at write time, not just at proposal
        time, exactly like `update_replacement_material` does -- the HITL
        approval gap can be long enough for another request to race in a
        duplicate `material_code` or `(sap_material_number, plant)` pair
        since the agent's own `check_for_conflicts` node last looked.
        """
        if self._master_data_repository.get_material_by_code(material_code) is not None:
            raise ConflictError(
                code="MATERIAL_ALREADY_EXISTS",
                message=f"A Material with material_code={material_code!r} already exists.",
            )

        plant = self._master_data_repository.get_or_create_plant(plant_code)

        if self._master_data_repository.find_material_master(sap_material_number, plant["id"]) is not None:
            raise ConflictError(
                code="MATERIAL_MASTER_ALREADY_EXISTS",
                message=(
                    f"A MaterialMaster with sap_material_number={sap_material_number!r} "
                    f"already exists at plant {plant_code!r}."
                ),
            )

        material = self._master_data_repository.add_material(material_code)
        material_master = self._master_data_repository.add_material_master(
            material_id=material["id"], sap_material_number=sap_material_number, plant_id=plant["id"]
        )
        return {
            "material_id": str(material["id"]),
            "plant_id": str(plant["id"]),
            "material_master_id": str(material_master["id"]),
        }

    def update_replacement_material(
        self, *, material_master_id: UUID, replacement_material_id: UUID
    ) -> dict:
        """Set `material_master_id`'s `succeededBy` target (physically,
        `follow_up_material_id`) to `replacement_material_id`.

        Re-validates both ends exist before writing -- deliberately, even
        though the calling graph has already resolved both earlier in its
        own run (`resolve_target`/`resolve_replacement`): state can be
        stale by the time a human actually approves (the interrupt may sit
        open for an arbitrary amount of time), so this service does not
        trust the caller's word for it. This is the "business validation"
        half of the Service/Repository split -- the repository call right
        after it is the "persistence" half, and does no validation of its
        own.
        """
        replacement = self._master_data_repository.get_material_by_id(replacement_material_id)
        if replacement is None:
            raise NotFoundError(
                code="MATERIAL_NOT_FOUND",
                message=f"Replacement material {replacement_material_id} does not exist.",
            )

        updated = self._master_data_repository.update_material_master_follow_up(
            material_master_id=material_master_id, follow_up_material_id=replacement_material_id
        )
        if updated is None:
            raise NotFoundError(
                code="MATERIAL_MASTER_NOT_FOUND",
                message=f"MaterialMaster {material_master_id} does not exist.",
            )
        return updated
