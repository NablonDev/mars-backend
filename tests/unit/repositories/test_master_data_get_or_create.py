"""Tests for MasterDataRepository.get_or_create_retailer/get_or_create_plant --
the concurrency fix for the check-then-insert race in
PoValidationService._ingest_one_line (see docs/ARCHITECTURE.md).

Only sequential/idempotency behavior is exercised here, against the
SQLite unit-test fixture -- SQLite cannot run the Postgres `ON CONFLICT`
syntax these methods use on a real engine (`MasterDataRepository._is_postgres()`
gates that path off; SQLite falls back to a plain check-then-insert, which
is correct for a single-threaded caller but proves nothing about real
concurrent-session safety). The actual concurrent-race proof is
Postgres-only: see tests/integration/test_po_ingestion_concurrency_postgres.py.

This file adds tests only -- it does not modify
tests/unit/repositories/test_master_data_repository.py or any other
existing test.
"""

from __future__ import annotations


def test_get_or_create_retailer_creates_on_first_call(repos):
    row = repos.master_data.get_or_create_retailer("CUST-GOC-1", "Customer GOC 1", None)

    assert row["retailer_code"] == "CUST-GOC-1"
    assert repos.master_data.get_retailer_by_code("CUST-GOC-1") is not None


def test_get_or_create_retailer_sequential_duplicate_returns_same_row(repos):
    first = repos.master_data.get_or_create_retailer("CUST-GOC-2", "Customer GOC 2", None)
    second = repos.master_data.get_or_create_retailer("CUST-GOC-2", "A Different Name", None)

    assert second["id"] == first["id"]
    # Not overwritten by the second call's different retailer_name.
    assert second["retailer_name"] == "Customer GOC 2"
    assert len([r for r in repos.master_data.list_retailers() if r["retailer_code"] == "CUST-GOC-2"]) == 1


def test_get_or_create_plant_creates_on_first_call(repos):
    row = repos.master_data.get_or_create_plant("PLANT-GOC-1")

    assert row["plant_code"] == "PLANT-GOC-1"
    assert repos.master_data.get_plant_by_code("PLANT-GOC-1") is not None


def test_get_or_create_plant_sequential_duplicate_returns_same_row(repos):
    first = repos.master_data.get_or_create_plant("PLANT-GOC-2", "Original Name")
    second = repos.master_data.get_or_create_plant("PLANT-GOC-2", "A Different Name")

    assert second["id"] == first["id"]
    assert second["plant_name"] == "Original Name"
    assert len([p for p in repos.master_data.list_plants() if p["plant_code"] == "PLANT-GOC-2"]) == 1


def test_add_retailer_and_add_plant_still_work_unchanged(repos):
    """Regression guard: the new get_or_create_* methods must not have
    altered add_retailer/add_plant's own unconditional-insert behavior."""
    retailer = repos.master_data.add_retailer("CUST-REGRESSION", "Regression Customer", None)
    assert retailer["retailer_code"] == "CUST-REGRESSION"

    plant = repos.master_data.add_plant("PLANT-REGRESSION")
    assert plant["plant_code"] == "PLANT-REGRESSION"
