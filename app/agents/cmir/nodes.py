from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


class WorkflowNodes:
    """LangGraph node functions.

    Every method is a single, small step. All collaborators are injected
    (Dependency Inversion) so the graph never talks to IMAP, Postgres or
    Azure OpenAI directly - only through the collaborators passed in here.

    No `workflow_thread_repository` collaborator here (unlike the
    pre-restructure version of this class): `process.workflow_thread` rows
    are now created lazily, exactly once per run, at the first human
    interrupt -- exclusively by `CmirRunService._handle_graph_state` (see
    that method's docstring). Nothing in this graph creates one eagerly any
    more, so there is nothing for a node to inject that repository into.
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
        email = EmailMessage(**state["email"])
        cmir = self._extractor.extract(email.body)
        return {"cmir": cmir.model_dump()}

    def identify_existing_cmir(self, state: GraphState) -> GraphState:
        """Look up the current active cmir_record row for this entity, if any.

        Runs on every pass through this part of the graph, including a loop back
        from collect_missing_fields -- a mandatory field (e.g. customer_identity)
        may only become known once the human supplies it, so re-running this lookup
        is what lets prepare_diff/validate_cmir see an accurate existing record and
        diff instead of one computed against a blank identity.
        """
        cmir = Cmir(**state["cmir"])
        existing = self._cmir_repository.get_current(
            cmir.customer_identity, cmir.target_customer_material_ref
        )
        return {"existing_cmir": existing}

    def prepare_diff(self, state: GraphState) -> GraphState:
        """Merge the proposed draft onto the active record (if any) and stash the diff.

        Always runs, whether this turns out to be a create (existing_cmir is None,
        diff is "from blank") or an update -- this keeps human_approval's interrupt
        payload uniform regardless of which case a given email falls into. The
        existing record's id becomes the version token persist_cmir will later use
        to detect a conflicting concurrent write.
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
        cmir = Cmir(**state["cmir"])
        cmir = self._validator.validate(cmir)
        return {"cmir": cmir.model_dump()}

    def persist_ai_result(self, state: GraphState) -> GraphState:
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
        """Attempt to commit the approved (merged) draft as the new current version.

        Does not raise on a version conflict -- it catches CmirVersionConflict and
        reports the outcome through cmir_write_result instead, so
        route_after_persist_cmir can send the graph to handle_version_conflict rather
        than crashing the run. cmir_version_token is whatever prepare_diff captured
        the active record's id as when the diff was computed; supersede_and_insert
        re-checks it (and, as the real backstop, the database's own partial unique
        index) at commit time.
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
        self._action_log_repository.log(
            state["email_id"],
            "CMIR Rejected",
            ACTOR,
            {"reason": state.get("decision_reason", "")},
        )
        return {}

    def handle_version_conflict(self, state: GraphState) -> GraphState:
        """Record that persist_cmir's write was rejected by a concurrent update.

        Reached only via a routing decision after persist_cmir catches
        CmirVersionConflict (see app.repositories.cmir.cmir_record) -- this node
        itself doesn't need to know how that was detected, only that it happened,
        so it stays decoupled from persist_cmir's exact implementation.
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
        """Best-effort only: this runs last on every decision path (approve,
        reject, and the version-conflict path), in the same DB transaction
        as the approval/rejection that already committed in an earlier node
        this same graph run. Marking the source email "read" in Gmail is a
        courtesy side effect, not part of the CMIR decision itself -- an
        IMAP failure here (mailbox state changed since ingest, message
        deleted, transient network issue, ...) must never roll back a
        reviewer's already-recorded decision, so it's caught and logged
        rather than left to propagate and abort the whole transaction."""
        if not state["email"].get("mark_read", True):
            return {}
        try:
            self._email_reader.mark_as_read(state["email"]["imap_id"])
        except Exception:
            logger.warning(
                "Could not mark source email (imap_id=%s) as read in Gmail -- "
                "continuing, since the CMIR decision itself already committed.",
                state["email"].get("imap_id"),
                exc_info=True,
            )
        return {}

    # ---- routing functions ---- #

    def route_after_validation(self, state: GraphState) -> Literal["needs_input", "ready"]:
        missing = state["cmir"].get("missing_fields") or []
        return "needs_input" if missing else "ready"

    def route_after_approval(self, state: GraphState) -> Literal["approved", "rejected"]:
        return "approved" if state.get("decision") == "approve" else "rejected"

    def route_after_persist_cmir(self, state: GraphState) -> Literal["committed", "conflict"]:
        return state.get("cmir_write_result", "committed")
