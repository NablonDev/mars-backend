"""LangGraph node functions for the ontology-context-driven update POC.

The proof this graph exists to demonstrate (see `docs/ontology/
semantic-context-layer-design.md` §23 and the Phase 2 POC task): a request
like "update the replacement material for MAT-DISC-1001 to MAT-REPL-1001"
never has "replacement material" -> `follow_up_material_id` hardcoded
anywhere in this file. `load_semantic_context` discovers which
relationship means "replacement" by asking
`OntologyContextService.get_relationships(...)` and matching against the
*business descriptions* Phase 1's vocabulary already carries
(`mars_ontology.ttl`'s `rdfs:comment` for `succeededBy`: "...the Material
that replaces a discontinued MaterialMaster row."), never against a
hardcoded relationship name. If that vocabulary comment or the underlying
column changes, this file does not need to.

Deterministic parsing, not an LLM, for `interpret_request`: this POC's
request shape is a single fixed sentence pattern, and the fact being
proven -- "the Agent asks the ontology for meaning instead of hardcoding
it" -- does not depend on how the sentence was tokenized. An LLM-based
interpreter (matching `AzureOpenAICmirExtractor`'s
`.with_structured_output(...)` pattern, see `app/services/cmir/
extractor.py`) is a reasonable later follow-up, not a requirement of this
proof.

Read-only through `resolve_replacement`/`build_proposal`/`human_approval` --
the only node that writes anything is `execute_update`, and it is only
ever reachable after `human_approval`'s `interrupt()` has already paused
the graph and been resumed with `{"decision": "approve"}` (see graph.py's
edges). `execute_update` itself never touches `MasterDataRepository`
directly -- it calls `MaterialMasterService`, which owns the actual
repository write and its own business validation. This is the confirmed
Checkpoint 1 boundary: `Agent -> MaterialMasterService ->
MasterDataRepository -> PostgreSQL`, never `Agent -> MasterDataRepository`
for a write.

Dependencies (`OntologyContextService`, `MasterDataRepository`,
`MaterialMasterService`) are injected at construction, matching
`WorkflowNodes`/`PoValidationNodes`'s convention -- no repository/service
is ever constructed inside a node.
"""

from __future__ import annotations

import re
from typing import Any

from langgraph.types import interrupt

from app.agents.ontology_update.state import OntologyUpdateState
from app.core.exceptions import AppError
from app.repositories.common.master_data import MasterDataRepository
from app.schemas.ontology.update_proposal import OperationProposal, ProposalMaterialRef, ProposalRelationship
from app.services.common.material_master_service import MaterialMasterService
from app.services.ontology.context_service import OntologyContextService

# This POC supports exactly one request shape and one entity -- see the
# task's own explicit scope ("ONE existing entity and ONE existing row",
# "DO NOT implement all CRUD operations"). Extending the parser to more
# phrasings/entities is next-phase generalization work, not this POC's job.
_REQUEST_PATTERN = re.compile(
    r"update\s+the\s+replacement\s+material\s+for\s+(?P<source>[\w-]+)\s+to\s+(?P<replacement>[\w-]+)",
    re.IGNORECASE,
)

# Keyword used to discover which relationship means "replacement" from the
# ontology's own business descriptions -- not the relationship's name.
# `mars:succeededBy`'s rdfs:comment already contains "replaces"; if that
# wording or the relationship it describes ever changes, this constant is
# the one place reflecting *what word a request maps to*, not *which
# column/predicate implements it*.
_REPLACEMENT_KEYWORD = "replac"

_ONTOLOGY_ENTITY = "MaterialMaster"


class OntologyUpdateNodes:
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

    # ---- Checkpoint 2: interpret -> context -> resolve -> propose -> interrupt ----

    def interpret_request(self, state: OntologyUpdateState) -> OntologyUpdateState:
        match = _REQUEST_PATTERN.search(state["user_request"])
        if match is None:
            return {
                "error": {
                    "error_type": "unsupported_operation",
                    "error_message": (
                        "Could not interpret the request as a supported replacement-material update."
                    ),
                }
            }
        return {
            "operation": "UPDATE",
            "source_material_code": match.group("source"),
            "replacement_material_code": match.group("replacement"),
        }

    def load_semantic_context(self, state: OntologyUpdateState) -> OntologyUpdateState:
        try:
            relationships = self._context_service.get_relationships(_ONTOLOGY_ENTITY)
        except Exception as exc:  # noqa: BLE001 - surfaced as a routed state error, not raised
            return {
                "error": {
                    "error_type": "semantic_context_unavailable",
                    "error_message": f"Could not load semantic context for {_ONTOLOGY_ENTITY!r}: {exc}",
                }
            }

        match = next(
            (r for r in relationships if _REPLACEMENT_KEYWORD in (r.description or "").lower()),
            None,
        )
        if match is None:
            return {
                "error": {
                    "error_type": "semantic_context_unavailable",
                    "error_message": (
                        f"No relationship on {_ONTOLOGY_ENTITY!r} describes a replacement concept "
                        f"(none of {[r.name for r in relationships]} matched {_REPLACEMENT_KEYWORD!r})."
                    ),
                }
            }

        return {"semantic_context": match.model_dump(mode="json")}

    def resolve_target(self, state: OntologyUpdateState) -> OntologyUpdateState:
        source_code = state["source_material_code"]

        source_material = self._master_data_repository.get_material_by_code(source_code)
        if source_material is None:
            return {
                "error": {
                    "error_type": "unknown_source_identifier",
                    "error_message": f"No Material found with material_code={source_code!r}.",
                }
            }

        masters = self._master_data_repository.list_material_masters_for_material(source_material["id"])
        if not masters:
            return {
                "error": {
                    "error_type": "unknown_source_identifier",
                    "error_message": f"{source_code!r} has no MaterialMaster (plant) rows to update.",
                },
                "resolved_material": source_material,
            }
        if len(masters) > 1:
            plant_ids = [str(m["plant_id"]) for m in masters]
            return {
                "resolved_material": source_material,
                "clarification_required": True,
                "clarification_reason": (
                    f"{source_code!r} has {len(masters)} MaterialMaster rows (plants {plant_ids}); "
                    "specify which plant's row to update rather than guessing one."
                ),
            }

        return {"resolved_material": source_material, "resolved_material_master": masters[0]}

    def resolve_replacement(self, state: OntologyUpdateState) -> OntologyUpdateState:
        replacement_code = state["replacement_material_code"]
        replacement_material = self._master_data_repository.get_material_by_code(replacement_code)
        if replacement_material is None:
            return {
                "error": {
                    "error_type": "unknown_replacement_identifier",
                    "error_message": f"No Material found with material_code={replacement_code!r}.",
                }
            }
        return {"resolved_replacement_material": replacement_material}

    def build_proposal(self, state: OntologyUpdateState) -> OntologyUpdateState:
        material = state["resolved_material"]
        material_master = state["resolved_material_master"]
        replacement = state["resolved_replacement_material"]
        relationship = state["semantic_context"]

        current_follow_up_id = material_master.get("follow_up_material_id")
        if current_follow_up_id is not None:
            current_material = self._master_data_repository.get_material_by_id(current_follow_up_id)
            current_value = ProposalMaterialRef(
                material_code=current_material["material_code"] if current_material else None,
                material_id=str(current_follow_up_id),
            )
        else:
            current_value = ProposalMaterialRef()

        proposal = OperationProposal(
            operation=state["operation"],
            entity=_ONTOLOGY_ENTITY,
            target=ProposalMaterialRef(
                material_code=material["material_code"],
                material_master_id=str(material_master["id"]),
            ),
            relationship=ProposalRelationship(
                name=relationship["name"],
                target_entity=relationship["target_entity"],
                kind=relationship["kind"],
            ),
            current_value=current_value,
            new_value=ProposalMaterialRef(
                material_code=replacement["material_code"], material_id=str(replacement["id"])
            ),
            summary=(
                f"Set {replacement['material_code']} as the replacement material for "
                f"{material['material_code']}"
            ),
        )
        return {"proposal": proposal.model_dump(mode="json")}

    def human_approval(self, state: OntologyUpdateState) -> OntologyUpdateState:
        answer: dict[str, Any] = interrupt(
            {
                "reason": "material_master_update_approval",
                "proposal": state["proposal"],
            }
        )
        return {"approval_status": answer.get("decision")}

    # ---- Checkpoint 3: the write, strictly after a resumed, approved interrupt ----

    def execute_update(self, state: OntologyUpdateState) -> OntologyUpdateState:
        """Only reachable once `human_approval` has returned
        `approval_status == "approve"` (see graph.py's conditional edge --
        the "rejected" branch never reaches this node at all). Calls
        `MaterialMasterService`, never `MasterDataRepository` directly --
        the Agent/Service/Repository boundary this whole checkpoint exists
        to prove.
        """
        material_master = state["resolved_material_master"]
        replacement = state["resolved_replacement_material"]

        try:
            updated = self._material_master_service.update_replacement_material(
                material_master_id=material_master["id"],
                replacement_material_id=replacement["id"],
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

        return {
            "execution_result": {
                "material_master_id": str(updated["id"]),
                "follow_up_material_id": (
                    str(updated["follow_up_material_id"]) if updated["follow_up_material_id"] else None
                ),
            }
        }

    # ---- routing ----

    def route_after_interpret(self, state: OntologyUpdateState) -> str:
        return "error" if state.get("error") else "continue"

    def route_after_context(self, state: OntologyUpdateState) -> str:
        return "error" if state.get("error") else "continue"

    def route_after_resolve_target(self, state: OntologyUpdateState) -> str:
        if state.get("error"):
            return "error"
        if state.get("clarification_required"):
            return "clarification_required"
        return "continue"

    def route_after_resolve_replacement(self, state: OntologyUpdateState) -> str:
        return "error" if state.get("error") else "continue"

    def route_after_approval(self, state: OntologyUpdateState) -> str:
        return "approved" if state.get("approval_status") == "approve" else "rejected"
