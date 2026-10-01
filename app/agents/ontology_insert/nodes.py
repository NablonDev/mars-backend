"""LangGraph node functions for the ontology-context-driven insert POC --
sibling to `app.agents.ontology_update.nodes`, proving the same pattern
generalizes from an UPDATE to a CREATE: one approved request creates a new
`Material`, its `MaterialMaster` row, and the `Plant` it's located at
(reusing the plant if the code already exists).

Two things this file deliberately does NOT hardcode:

1. *Which fields are required.* `load_semantic_context` asks
   `OntologyContextService.get_entity_context(...)` for `Material`/`Plant`/
   `MaterialMaster` and reads each property's own `required` flag (derived
   from the physical column's nullability -- see `context_service.py`).
   `check_required_fields` cross-references that against what
   `interpret_request` actually captured, so the set of "fields this
   operation cannot proceed without" comes from the ontology mapping, not
   a literal list written into this file.
2. *Which relationship links MaterialMaster to Plant.* `load_semantic_context`
   asks `get_relationships("MaterialMaster")` and picks the one whose
   `target_entity` is `"Plant"` -- structural discovery, not a hardcoded
   `"locatedAtPlant"` name lookup. (Unlike `succeededBy` in the UPDATE POC,
   `locatedAtPlant` carries no `rdfs:comment` in `mars_ontology.ttl` to key
   a keyword match off of, so target-entity matching is the discovery
   mechanism here instead -- still reading the ontology's own structure,
   never asserting the relationship's name up front.)

Two interrupt points, not one (unlike the UPDATE POC):

- `request_missing_details` pauses and asks a human to supply whichever of
  `material_code`/`plant_code`/`sap_material_number` the original request
  sentence omitted (per the "ask during HITL once it gets back with
  details" requirement) -- resumed with `Command(resume={"details": {...}})`.
  The graph loops back to `check_required_fields` afterward so a partial
  reply is re-checked rather than assumed complete.
- `human_approval` pauses for the final approve/reject decision, exactly
  like the UPDATE POC -- resumed with `Command(resume={"decision": ...})`.

Deterministic parsing, not an LLM, for `interpret_request` -- see
`app.agents.ontology_update.nodes`'s module docstring for the same
rationale; this POC's request shape is a single fixed sentence pattern
with three independently-optional fields, not a general free-text parser.

Read-only through every node except `execute_insert`, which is only ever
reachable after `human_approval`'s interrupt has been resumed with
`{"decision": "approve"}` (see graph.py's edges). `execute_insert` never
touches `MasterDataRepository` directly -- it calls `MaterialMasterService`,
matching the confirmed Agent -> Service -> Repository -> PostgreSQL
boundary from the UPDATE POC.
"""

from __future__ import annotations

import re
from typing import Any, cast

from langgraph.types import interrupt

from app.agents.ontology_insert.state import OntologyInsertState
from app.core.exceptions import AppError
from app.repositories.common.master_data import MasterDataRepository
from app.schemas.ontology.insert_proposal import InsertOperationProposal, NewEntityRef, ProposalRelationship
from app.services.common.material_master_service import MaterialMasterService
from app.services.ontology.context_service import OntologyContextService

# This POC supports exactly one request shape and one entity trio (Material
# + MaterialMaster + Plant), matching the narrow scope of the UPDATE POC
# this is a sibling to. Extending the parser to more phrasings/entities is
# next-phase generalization work, not this POC's job.
_OPERATION_KEYWORDS = re.compile(r"\b(create|insert|add)\b.*\bmaterial\b", re.IGNORECASE | re.DOTALL)
_MATERIAL_CODE_PATTERN = re.compile(r"\bmaterial\s+(?P<material_code>[\w-]+)", re.IGNORECASE)
_PLANT_CODE_PATTERN = re.compile(r"\bplant\s+(?P<plant_code>[\w-]+)", re.IGNORECASE)
_SAP_NUMBER_PATTERN = re.compile(r"\bsap\s+number\s+(?P<sap_material_number>[\w-]+)", re.IGNORECASE)

# Translates an ontology property name (as OntologyContextService reports
# it) to the state field it corresponds to -- necessary glue between the
# vocabulary's naming and this graph's state shape, but it never decides
# *which* fields are required; that still comes from each PropertyContext's
# own `required` flag in check_required_fields.
_PROPERTY_TO_STATE_FIELD = {
    "materialCode": "material_code",
    "plantCode": "plant_code",
    "sapMaterialNumber": "sap_material_number",
}

_MATERIAL_ENTITY = "Material"
_PLANT_ENTITY = "Plant"
_MATERIAL_MASTER_ENTITY = "MaterialMaster"


class OntologyInsertNodes:
    def __init__(
        self,
        *,
        context_service: OntologyContextService,
        master_data_repository: MasterDataRepository,
        material_master_service: MaterialMasterService,
    ) -> None:
        self._context_service = context_service
        self._master_data_repository = master_data_repository
        self._material_master_service = material_master_service

    # ---- interpret -> context -> required-fields (loop) -> conflicts -> propose -> interrupt ----

    def interpret_request(self, state: OntologyInsertState) -> OntologyInsertState:
        message = state["user_request"]
        if _OPERATION_KEYWORDS.search(message) is None:
            return {
                "error": {
                    "error_type": "unsupported_operation",
                    "error_message": "Could not interpret the request as a supported material-insert request.",
                }
            }

        fields: dict[str, Any] = {"operation": "INSERT"}
        if match := _MATERIAL_CODE_PATTERN.search(message):
            fields["material_code"] = match.group("material_code")
        if match := _PLANT_CODE_PATTERN.search(message):
            fields["plant_code"] = match.group("plant_code")
        if match := _SAP_NUMBER_PATTERN.search(message):
            fields["sap_material_number"] = match.group("sap_material_number")
        return cast(OntologyInsertState, fields)

    def load_semantic_context(self, state: OntologyInsertState) -> OntologyInsertState:
        try:
            material_ctx = self._context_service.get_entity_context(_MATERIAL_ENTITY)
            plant_ctx = self._context_service.get_entity_context(_PLANT_ENTITY)
            master_ctx = self._context_service.get_entity_context(_MATERIAL_MASTER_ENTITY)
            relationships = self._context_service.get_relationships(_MATERIAL_MASTER_ENTITY)
        except Exception as exc:  # noqa: BLE001 - surfaced as a routed state error, not raised
            return {
                "error": {
                    "error_type": "semantic_context_unavailable",
                    "error_message": f"Could not load semantic context for the insert entities: {exc}",
                }
            }

        plant_relationship = next((r for r in relationships if r.target_entity == _PLANT_ENTITY), None)
        if plant_relationship is None:
            return {
                "error": {
                    "error_type": "semantic_context_unavailable",
                    "error_message": (
                        f"No relationship on {_MATERIAL_MASTER_ENTITY!r} targets {_PLANT_ENTITY!r} "
                        f"(none of {[r.name for r in relationships]} did)."
                    ),
                }
            }

        return {
            "semantic_context": {
                "required_properties": {
                    _MATERIAL_ENTITY: [p.name for p in material_ctx.properties if p.required],
                    _PLANT_ENTITY: [p.name for p in plant_ctx.properties if p.required],
                    _MATERIAL_MASTER_ENTITY: [p.name for p in master_ctx.properties if p.required],
                },
                "relationship": plant_relationship.model_dump(mode="json"),
            }
        }

    def check_required_fields(self, state: OntologyInsertState) -> OntologyInsertState:
        required_properties = state["semantic_context"]["required_properties"]
        required_state_fields = {
            _PROPERTY_TO_STATE_FIELD[prop]
            for props in required_properties.values()
            for prop in props
            if prop in _PROPERTY_TO_STATE_FIELD
        }
        missing = sorted(field for field in required_state_fields if state.get(field) is None)
        return {"missing_fields": missing}

    def request_missing_details(self, state: OntologyInsertState) -> OntologyInsertState:
        answer: dict[str, Any] = interrupt(
            {
                "reason": "missing_required_fields",
                "missing_fields": state["missing_fields"],
                "message": (f"Please provide a value for each of: {', '.join(state['missing_fields'])}."),
            }
        )
        details = answer.get("details") or {}
        updates = {
            field: value for field, value in details.items() if field in _PROPERTY_TO_STATE_FIELD.values()
        }
        return cast(OntologyInsertState, updates)

    def check_for_conflicts(self, state: OntologyInsertState) -> OntologyInsertState:
        material_code = state["material_code"]
        plant_code = state["plant_code"]
        sap_material_number = state["sap_material_number"]

        if self._master_data_repository.get_material_by_code(material_code) is not None:
            return {
                "error": {
                    "error_type": "material_already_exists",
                    "error_message": f"A Material with material_code={material_code!r} already exists.",
                }
            }

        existing_plant = self._master_data_repository.get_plant_by_code(plant_code)
        if existing_plant is not None:
            existing_master = self._master_data_repository.find_material_master(
                sap_material_number, existing_plant["id"]
            )
            if existing_master is not None:
                return {
                    "error": {
                        "error_type": "material_master_already_exists",
                        "error_message": (
                            f"A MaterialMaster with sap_material_number={sap_material_number!r} "
                            f"already exists at plant {plant_code!r}."
                        ),
                    }
                }
        return {}

    def build_proposal(self, state: OntologyInsertState) -> OntologyInsertState:
        relationship = state["semantic_context"]["relationship"]
        reused_plant = self._master_data_repository.get_plant_by_code(state["plant_code"]) is not None

        proposal = InsertOperationProposal(
            material=NewEntityRef(material_code=state["material_code"]),
            plant=NewEntityRef(plant_code=state["plant_code"], reused_existing=reused_plant),
            material_master=NewEntityRef(sap_material_number=state["sap_material_number"]),
            relationship=ProposalRelationship(
                name=relationship["name"],
                target_entity=relationship["target_entity"],
                kind=relationship["kind"],
            ),
            summary=(
                f"Create material {state['material_code']!r} with a MaterialMaster row "
                f"(SAP number {state['sap_material_number']!r}) at plant {state['plant_code']!r}"
                + (" (existing plant reused)" if reused_plant else " (new plant)")
            ),
        )
        return {"proposal": proposal.model_dump(mode="json")}

    def human_approval(self, state: OntologyInsertState) -> OntologyInsertState:
        answer: dict[str, Any] = interrupt(
            {
                "reason": "material_master_insert_approval",
                "proposal": state["proposal"],
            }
        )
        return {"approval_status": answer.get("decision")}

    # ---- the write, strictly after a resumed, approved interrupt ----

    def execute_insert(self, state: OntologyInsertState) -> OntologyInsertState:
        """Only reachable once `human_approval` has returned
        `approval_status == "approve"` (see graph.py's conditional edge --
        the "rejected" branch never reaches this node at all). Calls
        `MaterialMasterService`, never `MasterDataRepository` directly."""
        try:
            created = self._material_master_service.create_material_master(
                material_code=state["material_code"],
                plant_code=state["plant_code"],
                sap_material_number=state["sap_material_number"],
            )
        except AppError as exc:
            return {
                "error": {
                    "error_type": "service_validation_failure",
                    "error_message": exc.message,
                }
            }
        except Exception as exc:  # noqa: BLE001 - surfaced as a routed state error, not raised
            return {
                "error": {
                    "error_type": "repository_failure",
                    "error_message": str(exc),
                }
            }

        return {"execution_result": created}

    # ---- routing ----

    def route_after_interpret(self, state: OntologyInsertState) -> str:
        return "error" if state.get("error") else "continue"

    def route_after_context(self, state: OntologyInsertState) -> str:
        return "error" if state.get("error") else "continue"

    def route_after_required_fields_check(self, state: OntologyInsertState) -> str:
        return "missing" if state.get("missing_fields") else "continue"

    def route_after_conflict_check(self, state: OntologyInsertState) -> str:
        return "error" if state.get("error") else "continue"

    def route_after_approval(self, state: OntologyInsertState) -> str:
        return "approved" if state.get("approval_status") == "approve" else "rejected"
