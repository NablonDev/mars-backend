"""Operational CLI tool to normalize and validate retailer agreement markdown files."""

from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.penalties.rule_extraction.agreement_normalizer import (
    normalize_agreement_markdown,
)
from app.services.penalties.rule_extraction.agreement_validator import (
    validate_agreement_markdown,
)


def _collect_files(target_path: str) -> list[str]:
    """Collect Markdown files from target path."""
    if os.path.isfile(target_path):
        return [target_path]
    if os.path.isdir(target_path):
        return sorted(glob.glob(os.path.join(target_path, "**/*.md"), recursive=True))
    matches = sorted(glob.glob(target_path, recursive=True))
    return [m for m in matches if os.path.isfile(m) and m.endswith(".md")]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize and validate agreement markdown heading hierarchy."
    )
    parser.add_argument(
        "--path",
        default="data/retailer_agreements/",
        help="Path to agreement file or directory (default: data/retailer_agreements/)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check validity without modifying files; exit with code 1 if invalid.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write normalized content back to files.",
    )
    args = parser.parse_args()

    files = _collect_files(args.path)
    if not files:
        print(f"No markdown files found matching: {args.path}")
        return 1

    total_files = len(files)
    invalid_files = 0
    modified_files = 0

    print(f"Processing {total_files} agreement file(s)...")

    for path in files:
        rel_path = os.path.relpath(path)
        with open(path, encoding="utf-8") as f:
            original = f.read()

        if args.check:
            result = validate_agreement_markdown(original)
            if not result.is_valid:
                invalid_files += 1
                print(f"[FAIL] {rel_path} ({len(result.errors)} errors):")
                for err in result.errors[:5]:
                    print(f"       L{err.line_number} [{err.rule}]: {err.message}")
                if len(result.errors) > 5:
                    print(f"       ... and {len(result.errors) - 5} more error(s)")
            else:
                print(f"[OK]   {rel_path}")
        elif args.write:
            normalized = normalize_agreement_markdown(original)
            validation = validate_agreement_markdown(normalized)

            if not validation.is_valid:
                invalid_files += 1
                print(
                    f"[ERROR] Normalized output for {rel_path} is invalid ({len(validation.errors)} errors):"
                )
                for err in validation.errors[:5]:
                    print(f"        L{err.line_number} [{err.rule}]: {err.message}")
                continue

            if normalized != original:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(normalized)
                modified_files += 1
                print(f"[MODIFIED] {rel_path}")
            else:
                print(f"[UNCHANGED] {rel_path}")
        else:
            # Default preview mode
            result = validate_agreement_markdown(original)
            status = "VALID" if result.is_valid else f"INVALID ({len(result.errors)} errors)"
            print(f"[{status}] {rel_path}")

    if args.check:
        if invalid_files > 0:
            print(f"\nValidation failed: {invalid_files}/{total_files} file(s) have errors.")
            return 1
        print(f"\nAll {total_files} file(s) pass canonical markdown validation.")
        return 0

    if args.write:
        print(f"\nComplete: {modified_files} file(s) normalized and updated.")
        if invalid_files > 0:
            print(f"Warning: {invalid_files} file(s) failed normalization validation.")
            return 1
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
