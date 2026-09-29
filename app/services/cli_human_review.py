"""Console-based human-in-the-loop port for the legacy CLI CMIR ingest job."""

from __future__ import annotations


class CLIHumanReviewPort:
    """Console-based human review for the legacy CLI ingest job."""

    def request_missing_fields(self, payload: dict) -> dict[str, str]:
        """Prompt the operator on stdin for each mandatory field the extractor left empty.

        `payload` carries the in-progress CMIR dict under "cmir" and the list of
        field names still missing under "missing_fields"; the returned mapping
        feeds straight back into the CLI workflow as the field values to merge in.
        """
        cmir = payload["cmir"]
        missing = payload["missing_fields"]

        print("\n--- Human action required: missing mandatory fields ---")
        print(f"Customer : {cmir.get('customer_identity') or '(unknown)'}")
        print(f"Material : {cmir.get('material_identity') or '(unknown)'}")

        answers: dict[str, str] = {}
        for field_name in missing:
            answers[field_name] = input(f"Enter value for '{field_name}': ").strip()

        return answers

    def request_approval(self, payload: dict) -> dict[str, str]:
        """Print the full CMIR draft and prompt the operator to approve or reject it.

        Loops on stdin until the operator answers 'y'/'yes' or 'n'/'no'; a
        rejection also prompts for a free-text reason, returned alongside the
        decision.
        """
        cmir = payload["cmir"]

        print("\n--- Review CMIR before writing to the database ---")
        for key, value in cmir.items():
            if key in ("status", "missing_fields"):
                continue
            print(f"  {key}: {value}")

        while True:
            choice = input("Approve and insert into DB? [y/n]: ").strip().lower()
            if choice in ("y", "yes"):
                return {"decision": "approve", "reason": ""}
            if choice in ("n", "no"):
                reason = input("Reason for rejection: ").strip()
                return {"decision": "reject", "reason": reason}
            print("Please answer 'y' or 'n'.")
