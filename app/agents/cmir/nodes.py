"""LangGraph node implementations for the CMIR resolution workflow.

Implements discrete steps in the email extraction, validation, approval, and
persistence pipeline. Each method is a single, atomic workflow node that reads
from and updates the shared GraphState.
"""

from __future__ import annotations

from typing import Literal

from langgraph.types import interrupt

from app.agents.cmir.state import GraphState
from app.repositories.cmir.action_log import ActionLogRepository
from app.repositories.cmir.cmir_record import CmirRecordRepository, CmirVersionConflict
from app.repositories.cmir.email import EmailRepository
from app.schemas.cmir import Cmir, CmirStatus, EmailMessage
from app.services.cmir.extractor import AzureOpenAICmirExtractor
from app.services.cmir.merge import merge_with_active
from app.services.cmir.validation import CmirValidator
from app.services.email_reader import GmailImapReader

ACTOR = "AI Agent"


class WorkflowNodes:
    """LangGraph node functions for the CMIR workflow, one atomic step per method.

    Nodes reach IMAP, Postgres and Azure OpenAI only through the injected
    collaborators. `process.workflow_thread` rows are created lazily by
    `CmirService._handle_graph_state` at the first human interrupt, so no node
    holds a workflow-thread repository.
    """

    def __init__(
        self,
        email_reader: GmailImapReader,
        extractor: AzureOpenAICmirExtractor,
        validator: CmirValidator,
        email_repository: EmailRepository,
        cmir_repository: CmirRecordRepository,
        action_log_repository: ActionLogRepository,
    ) -> None:
        self._email_reader = email_reader
        self._extractor = extractor
        self._validator = validator
        self._email_repository = email_repository
        self._cmir_repository = cmir_repository
        self._action_log_repository = action_log_repository

    # ---- extraction / persistence steps ---- #

    def persist_email(self, state: GraphState) -> GraphState:
        """Save the incoming email if it is not already persisted, and log receipt."""
        email = EmailMessage(**state["email"])
        email_id = state.get("email_id")
        if email_id is None:
            email_id = self._email_repository.save(
                sender=email.sender,
                subject=email.subject,
                raw_content=email.body,
                source_message_id=email.source_message_id,
                source_imap_id=email.imap_id,
            )
        self._action_log_repository.log(email_id, "Email Received", ACTOR, {"sender": email.sender})
        return {"email_id": email_id}

    def extract_cmir(self, state: GraphState) -> GraphState:
        """Extract CMIR fields from the email body using Azure OpenAI."""
        email = EmailMessage(**state["email"])
        cmir = self._extractor.extract(email.body)
        return {"cmir": cmir.model_dump()}

    def identify_existing_cmir(self, state: GraphState) -> GraphState:
        """Look up the current active cmir_record row for this entity, if any.

        Re-runs on every pass, including the loop back from collect_missing_fields,
        because a mandatory field such as customer_identity may only become known once
        the human supplies it; prepare_diff would otherwise diff a blank identity.
        """
        cmir = Cmir(**state["cmir"])
        existing = self._cmir_repository.get_current(
            cmir.customer_identity, cmir.target_customer_material_ref
        )
        return {"existing_cmir": existing}

    def prepare_diff(self, state: GraphState) -> GraphState:
        """Merge the proposed draft onto the active record (if any) and stash the diff.

        Runs for both creates (existing_cmir is None, so the diff is from blank) and
        updates, keeping human_approval's interrupt payload uniform. The existing
        record's id becomes the version token persist_cmir uses to detect a conflicting
        concurrent write.
        """
        existing = state.get("existing_cmir")
        proposed = Cmir(**state["cmir"])
        merged, diff = merge_with_active(existing, proposed)
        return {
            "cmir": merged.model_dump(),
            "cmir_diff": diff,
            "cmir_version_token": existing["id"] if existing else None,
        }

    def validate_cmir(self, state: GraphState) -> GraphState:
        """Validate the CMIR against business rules, populating missing_fields and status."""
        cmir = Cmir(**state["cmir"])
        cmir = self._validator.validate(cmir)
        return {"cmir": cmir.model_dump()}

    def persist_ai_result(self, state: GraphState) -> GraphState:
        """Store the AI extraction result on the email row and log the outcome."""
        cmir = Cmir(**state["cmir"])
        self._email_repository.update_extraction(
            state["email_id"], cmir.model_dump(), cmir.missing_fields, cmir.status
        )

        action = (
            "Pending Human Action"
            if cmir.status == CmirStatus.PENDING_HUMAN_ACTION.value
            else "Extraction Complete"
        )
        self._action_log_repository.log(
            state["email_id"], action, ACTOR, {"missing_fields": cmir.missing_fields}
        )
        return {}

    # ---- human-in-the-loop steps ---- #

    def collect_missing_fields(self, state: GraphState) -> GraphState:
        """Interrupt to collect mandatory fields from a human operator.

        Resume values are merged field-by-field onto the current CMIR, which then
        loops back through identify_existing_cmir for re-validation.
        """
        cmir_dict = state["cmir"]
        answers = interrupt(
            {
                "reason": "missing_mandatory_fields",
                "email_id": state["email_id"],
                "cmir": cmir_dict,
                "missing_fields": cmir_dict["missing_fields"],
            }
        )
        merged = Cmir(**cmir_dict)
        for field_name, value in answers.items():
            setattr(merged, field_name, value)
        return {"cmir": merged.model_dump()}

    def human_approval(self, state: GraphState) -> GraphState:
        """Interrupt to request human approval of the merged CMIR.

        The interrupt payload carries the proposed CMIR, the current record and the
        field-level diff; the resume value supplies the decision and its reason.
        """
        decision = interrupt(
            {
                "reason": "approval_required",
                "email_id": state["email_id"],
                "cmir": state["cmir"],
                "existing_cmir": state.get("existing_cmir"),
                "diff": state.get("cmir_diff", {}),
            }
        )
        return {
            "decision": decision.get("decision"),
            "decision_reason": decision.get("reason", ""),
        }

    # ---- outcome steps ---- #

    def persist_cmir(self, state: GraphState) -> GraphState:
        """Attempt to commit the approved draft as the new current version.

        A version conflict does not propagate: CmirVersionConflict is caught and
        reported through cmir_write_result, letting route_after_persist_cmir divert
        the graph to handle_version_conflict. cmir_version_token is the active
        record's id as captured by prepare_diff; supersede_and_insert re-checks it at
        commit time, backed by the database's partial unique index.
        """
        cmir = Cmir(**state["cmir"])
        try:
            self._cmir_repository.supersede_and_insert(
                customer_identity=cmir.customer_identity,
                target_customer_material_ref=cmir.target_customer_material_ref,
                merged=cmir.model_dump(),
                expected_current_id=state.get("cmir_version_token"),
            )
        except CmirVersionConflict:
            return {"cmir_write_result": "conflict"}
        self._action_log_repository.log(state["email_id"], "CMIR Created", ACTOR, {"status": cmir.status})
        return {"cmir_write_result": "committed"}

    def persist_rejection(self, state: GraphState) -> GraphState:
        """Log the rejection with the approver's reason, leaving the active record alone."""
        self._action_log_repository.log(
            state["email_id"],
            "CMIR Rejected",
            ACTOR,
            {"reason": state.get("decision_reason", "")},
        )
        return {}

    def handle_version_conflict(self, state: GraphState) -> GraphState:
        """Record that persist_cmir's write was rejected by a concurrent update.

        Reached only by routing after persist_cmir catches CmirVersionConflict.
        """
        cmir = state["cmir"]
        self._action_log_repository.log(
            state["email_id"],
            "CMIR Version Conflict",
            ACTOR,
            {
                "customer_identity": cmir.get("customer_identity"),
                "target_customer_material_ref": cmir.get("target_customer_material_ref"),
            },
        )
        return {}

    def mark_email_read(self, state: GraphState) -> GraphState:
        """Mark the email read in IMAP unless the caller set mark_read=False."""
        if not state["email"].get("mark_read", True):
            return {}
        self._email_reader.mark_as_read(state["email"]["imap_id"])
        return {}

    # ---- routing functions ---- #

    def route_after_validation(self, state: GraphState) -> Literal["needs_input", "ready"]:
        """Route to collect_missing_fields when mandatory fields are missing, else approval."""
        missing = state["cmir"].get("missing_fields") or []
        return "needs_input" if missing else "ready"

    def route_after_approval(self, state: GraphState) -> Literal["approved", "rejected"]:
        """Route to persistence on an "approve" decision, otherwise to rejection logging."""
        return "approved" if state.get("decision") == "approve" else "rejected"

    def route_after_persist_cmir(self, state: GraphState) -> Literal["committed", "conflict"]:
        """Route on persist_cmir's outcome, sending "conflict" to handle_version_conflict."""
        return state.get("cmir_write_result", "committed")
