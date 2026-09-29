"""CMIR completeness validation."""

from __future__ import annotations

from app.schemas.cmir import MANDATORY_FIELDS, Cmir, CmirStatus


class CmirValidator:
    """Single source of truth for the "is this CMIR complete?" business rule.

    The LLM extractor is intentionally not trusted to self-report status:
    a validator that doesn't depend on the model's own judgement is easier
    to test, audit and change independently of prompt/model choices
    (Single Responsibility + Open/Closed).
    """

    def __init__(self, mandatory_fields: tuple = MANDATORY_FIELDS) -> None:
        self._mandatory_fields = mandatory_fields

    def validate(self, cmir: Cmir) -> Cmir:
        """Populate `missing_fields` and set `status` in place, then return the same `Cmir`.

        A CMIR with any mandatory field blank goes to PENDING_HUMAN_ACTION;
        otherwise it goes to PENDING_REVIEW. Mutates and returns the same
        instance rather than a copy, so callers can chain this on the object
        they already hold.
        """
        missing = [
            field_name
            for field_name in self._mandatory_fields
            if not str(getattr(cmir, field_name, "")).strip()
        ]

        cmir.missing_fields = missing
        cmir.status = CmirStatus.PENDING_HUMAN_ACTION.value if missing else CmirStatus.PENDING_REVIEW.value
        return cmir
