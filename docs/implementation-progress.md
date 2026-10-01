# Implementation Progress — Gap-Fix Task (A.3, A.1, B, D, A.2, UI data-loading)

Working-tree progress record only. Not committed. Do not add secrets here.

---

## PHASE CHECKPOINT

**Phase:** 3+4 — A.2 Implementation + Failure Tests
**Status:** PASSED
**Date/time:** 2026-09-07 (session continuation)

**Root cause:** Phase 1/2 evidence — `PostgresSaver`'s dedicated psycopg3 connection is `autocommit=True`; checkpoint writes durably commit the instant `graph.invoke()`/`graph.update_state()` returns, strictly before the SQLAlchemy DB-persistence step commits. An ordinary DB failure after a successful checkpoint write left the checkpoint durably ahead of the application DB with no retry/detection (approved Option B design).

**Files changed:**
- `app/services/cmir/run_service.py` — new `_retry_persist_or_raise_corrupt` helper; `submit_decision`, `submit_missing_fields`, `update_draft` restructured so a non-`AppError` failure from the DB-persistence step propagates through the *entire* `with self._unit_of_work_factory()` block (correct rollback of the original Session) before being caught and retried once via a fresh `self._repos_factory()` Session — never re-invoking the graph.
- `app/services/po_validation/service.py` — same helper + same restructuring for `submit_manual_cmir_entry`, `submit_qty_mismatch_decision`.
- `tests/integration/test_a2_persistence_retry_postgres.py` (new, 5 tests).

**Implementation:** `_retry_persist_or_raise_corrupt(operation, *, thread_id, pending_action_id, checkpoint_thread_id, operation_name, first_exc)` — logs the first failure (`logger.error`), retries `operation(fresh_uow)` exactly once inside `self._repos_factory()`; `AppError` from the retry propagates unchanged (never re-wrapped); any other exception is logged (`logger.critical`, full context, no PII/secrets) and raised as `ExternalServiceError(code="WORKFLOW_STATE_CORRUPT", status_code=502)`.

**Retry behavior:** exactly one retry, always with a genuinely fresh Session (proven via Session-object-identity assertion in the tests, not just assumed) — the graph is never re-invoked, only the already-obtained `state`/kwargs are replayed against fresh repositories.

**Failure behavior:** retry exhausted → `WORKFLOW_STATE_CORRUPT`/502, never the generic `WORKFLOW_RESUME_FAILED`; no partial/duplicate DB writes (both failed attempts fully roll back — verified via independent-connection DB queries showing 0 completed `human_action` rows and the thread still in its original pre-decision state).

**Residual crash window (documented, not solved):** a hard process crash between the checkpoint commit and the DB commit — no synchronous retry can help here since the process handling the request no longer exists. Recommended (not implemented) follow-up: an on-demand/periodic reconciliation check comparing open `human_action` rows against `graph.get_state()`.

**Tests (Phase 4), all against real Postgres, real service paths, graph/checkpoint never made to fail:**
1. `test_db_persistence_succeeds_consistent_final_state` — baseline.
2. `test_first_db_persistence_fails_retry_succeeds` — first attempt fails (monkeypatched `apply_human_action`), retry (real, unwrapped `repos_factory`) succeeds; asserts exactly one completed `human_action`, correct final status, **and** that the first/retry attempts used genuinely different Session objects (`id(...)` comparison) — folds in the required "fresh session" proof.
3. `test_first_db_persistence_fails_retry_also_fails_raises_workflow_state_corrupt` — both attempts fail; asserts `WORKFLOW_STATE_CORRUPT`/502 (explicitly `!= WORKFLOW_RESUME_FAILED`), zero completed `human_action` rows, thread still `waiting_approval` (no partial/duplicate writes).
4. `test_cmir_real_hitl_flow_db_consistent_after_decision` — plain CMIR reject flow, no retry infrastructure involved, proving A.2 didn't disturb ordinary behavior.
5. `test_po_real_hitl_flow_db_and_checkpoint_consistent` — real, unstubbed PO graph (no LLM dependency), verifies both DB state AND checkpoint state via the existing `graph.get_state(config)` API (`snapshot.next == ()`, no pending interrupt remaining) — genuine checkpoint-level consistency proof, not just DB-level.

**Verified all 5 tests are genuine regression guards:** temporarily forced the retry path to immediately raise (simulating "A.2 fix absent") — `test_first_db_persistence_fails_retry_succeeds` failed exactly as expected (retry could no longer succeed); restored the fix, reran twice consecutively against the same persistent DB — all 5 passed both times (also incidentally caught and fixed a real test-quality bug of my own: the PO test originally used fixed identifiers that collided on a second run against the persistent DB — fixed with a `uuid4()`-based unique suffix, verified idempotent afterward).

**Full regression after Phase 3+4:** 626 passed, 1 xfailed, 1 failed (the already-documented pre-existing flaky `THREAD_STALE`/`THREAD_NOT_WAITING` assertion — Phase 5's own job, not a new regression). Ruff clean.

**Acceptance criteria:** PASS (all Phase 4 required scenarios proven; A.1/B/existing CMIR+PO lifecycle tests all still pass; no protected scope touched — see hash re-check below).

**Protected scope:** `graph.py`, `container.py`, `nodes.py`, migrations, `get_or_create_*` — untouched (no edits made to any of them this phase).

**Next phase:** READY — proceeding to Phase 5 (B test cleanup).

---

## PHASE CHECKPOINT

**Phase:** 5 — B Concurrency Test Cleanup
**Status:** PASSED
**Date/time:** 2026-09-07 (session continuation)

**Root cause (of the flakiness, not a functional bug):** `tests/integration/test_hitl_decision_concurrency_postgres.py` hard-asserted the loser's conflict code was always exactly `THREAD_STALE`. In reality the loser can legitimately hit either `THREAD_STALE` (raced at `apply_human_action`'s guard) or `THREAD_NOT_WAITING` (raced earlier, at `submit_decision`'s own status check, if the winner's transaction fully committed before the loser's check even ran) — both are `ConflictError`/409, just from different code paths depending on timing.

**Files changed:** `tests/integration/test_hitl_decision_concurrency_postgres.py` — module docstring updated to document both legitimate outcomes; both the CMIR and PO concurrency tests' assertions changed from `== "THREAD_STALE"` to `in {"THREAD_STALE", "THREAD_NOT_WAITING"}`, with the reasoning left as an inline comment. No other assertion loosened — `status_code == 409` and the "no `ExternalServiceError`" check are unchanged.

**Tests:** ran the full file **12 times in a row** against real Postgres. Result: **12/12 passed** (previously observed to fail intermittently, ~1-in-5). Zero 5xx observed in any run.

**Full regression after Phase 5:** 626 passed, 1 xfailed, 1 failed (the OTHER, unrelated, already-documented pre-existing flaky test — `test_get_latest_by_email_event_returns_the_most_recently_updated`, a self-documented UUIDv7-ordering issue in a completely different repository/domain, untouched by any of this work). Ruff clean.

**Remaining risk:** none for this phase's scope. The other flaky test is out of scope (unrelated to A.2/B/D/frontend) and remains exactly as previously documented.

**Next phase:** READY — proceeding to Phase 6 (D verification).

---

## PHASE CHECKPOINT

**Phase:** 6 — D (History Contract) Verification
**Status:** PASSED
**Date/time:** 2026-09-07 (session continuation)

**Verification performed (re-confirmation of the earlier-implemented fix, no code changes this phase):**
- Re-read `thread.transformer.ts`/`thread.model.ts`/`ThreadHistoryList.tsx`: confirmed `RawHistoryEntry`/`ThreadHistoryEntry` match the backend's real `SnapshotHistoryItem` field-for-field (`actor`, `action_type`, `decision`, `response_payload` → `responsePayload`, `responded_at` → `respondedAt`) — a semantic mapping (same meaning, camelCase renamed), not a fabricated `field_changes`/`created_at`.
- Frontend tests: 9 passed, type-check clean.
- **Live verification against real data** (backend + BFF still running from Phase 0, real Postgres): fetched a real CMIR thread's snapshot (`completed_rejected`, created during Phase 4's test run) via the BFF — `history` array populated with a genuine reviewer decision: `{"actor":"reviewer@company.com","action_type":"decision","decision":"reject","response_payload":{"reason":"Not a valid CMIR request","decision":"reject"},"responded_at":"2026-09-07T15:02:55.367345Z"}`. Fetched a real PO thread's snapshot (`ready_for_so_creation`, from Phase 5's concurrency test) — `history` populated with `{"actor":"reviewer@company.com","action_type":"manual_entry","decision":null,"response_payload":{"description":"...","sap_material_number":"..."},"responded_at":"..."}`.
- Both shapes render correctly per the current `ThreadHistoryList.tsx` logic: actor shown, action description shown (`"made a decision"`/`"entered a manual mapping"`), `decision` shown inline only when non-null (correctly omitted for the PO manual-entry case, which has none), `responsePayload` rendered as submitted key/value pairs (not a fabricated diff), `respondedAt` renders a relative timestamp. **No blank history for either domain.**

**Files changed:** none this phase (verification only).

**Test result:** frontend 9 passed, type-check clean (unchanged from Phase 4's D implementation, re-confirmed).

**Remaining risk:** none new. Same documented limitation as before — no component/browser-level automated test exists (no jsdom/testing-library in this repo).

**Next phase:** READY — proceeding to Phase 7 (frontend data-loading root cause + fix).

---

## PHASE CHECKPOINT

**Phase:** 7 — Frontend Data-Loading Root Cause + Fix
**Status:** PASSED (no code fix required/appropriate — see conclusion)
**Date/time:** 2026-09-07 (session continuation)

**Root cause, confirmed with full evidence chain (not assumed):**
1. Phase 0 established: backend (8010) and BFF (8020) processes were **not running** while a real browser tab (Chrome, confirmed via `netstat`/process inspection) was already open against the real Vite dev server on port 5173 — meaning every API call that tab made would fail with a network/connection error.
2. This session re-verified (Phase 7): the SAME dev server process (PID 30352) and the SAME Chrome connection (PID 26784) are **still alive** — confirmed via `netstat` again, hours into this session — strongly indicating this is the actual tab the user was looking at.
3. Checked every layer the task asked to inspect, specifically looking for a SEPARATE/additional frontend-code bug, not just accepting the "servers were down" explanation:
   - `src/lib/axios.ts`: `baseURL` from `VITE_API_BASE_URL` (`.env.local` = `http://localhost:8020`) — correct, matches the running BFF.
   - CORS: preflight from `Origin: http://localhost:5173` against the BFF returns `access-control-allow-origin: http://localhost:5173` — correct.
   - `useThreadQueue`/`useThreadSnapshot` (`useThreadQueue.ts`): correct query keys, correct `enabled` gating, no swallowed errors.
   - `queue-facets.ts`: `isOpenStage`/`STAGE_FILTER_OPTIONS`/`APPROVED_STAGES`/`REJECTED_STAGES` all use the exact real backend stage strings (`AWAITING_MANUAL_CMIR_ENTRY`, `COMPLETED_APPROVED`, etc., verified against live responses throughout this session) — no client-side filter mismatch that could silently hide real data.
   - `ThreadQueueList.tsx`/`ThreadDetailPanel.tsx`: both correctly render an explicit "Couldn't load..." + Retry state on `isError` — **this exactly matches the reported symptom**: with the backend/BFF down, the user would have seen precisely this, not a crash or a blank/silent page.
   - `src/lib/query-client.ts`: `refetchOnWindowFocus: import.meta.env.PROD` — **false in dev mode**. This means the already-open tab's queries, once settled into an error state (after their one automatic `retry: 1`), will **not** self-heal just by the servers coming back up — an explicit page reload or a click on the existing "Retry" button is required.
4. **Conclusion: this is a pure environment/process-state issue, not a code defect anywhere in the stack.** The frontend, BFF, and backend code are all already correct (contract, CORS, transformers, stage filtering, error UI all verified correct); the fix is operational (keep backend + BFF running), not a source change. Per the phase's own "fix the FIRST incorrect layer" rule — there is no incorrect layer to fix.

**Files changed:** none. No mock data introduced, no error handling hidden, no frontend workaround added — consistent with the phase's explicit prohibitions, and appropriate here since there is no code-level root cause to fix.

**Before/after:**
- Before (servers down): any real request → network error → React Query settles into `isError: true` after 1 retry → UI correctly shows "Couldn't load the queue."/"Couldn't load this item." + Retry button. This is what "no data loading" looked like.
- After (servers running, confirmed live throughout Phases 0–6): `GET /runs` → 200, real items; `GET /threads/{id}/snapshot` → 200, real CMIR/PO data with correct history. A reload of the existing tab, or a click of the already-present Retry button, will now succeed.

**Remaining risk:** if the *actual* deployed/user environment has some *other* reason the backend/BFF aren't reachable (firewall, wrong port config, a supervisor process not started) that's outside this repo's code, that's an operational matter, not something further code changes here can address.

**Next phase:** READY — proceeding to Phase 8 (frontend E2E, browser tooling unavailable as previously established).

---

## PHASE CHECKPOINT

**Phase:** 8 — Frontend E2E
**Status:** BROWSER VERIFICATION NOT PERFORMED — explicitly reported, not claimed
**Date/time:** 2026-09-07 (session continuation)

**No browser automation/DevTools-inspection tooling is available in this session** (confirmed multiple times across this whole engagement — no Playwright/Puppeteer/Cypress in this environment). I did not, and do not, claim browser E2E success.

**What WAS verified, honestly, as the closest available substitute (all via curl/process/network inspection against the real running backend+BFF, real Postgres, and the real, still-open browser tab's underlying dev server):**
- `GET /runs?view=threads&limit=50` via the real BFF → 200, real CMIR + PO items (Phase 0).
- `GET /threads/{id}/snapshot` via the real BFF → 200, real CMIR data (Phase 0, Phase 6) and real PO data (Phase 6), including populated `history[]` for both domains with the D-fixed contract.
- `GET /threads/{id}/snapshot` for a real qty-mismatch thread → 200 with `candidate.plant` correct (A.3, re-confirmed not regressed).
- CORS preflight from the real frontend origin succeeds (Phase 0).
- The actual Vite dev server process and its real Chrome connection are both still alive (Phase 0, Phase 7).
- Frontend unit tests (9) and type-check pass against the current transformer/model/component code that would render this exact data.

**What was NOT verified (explicitly, not glossed over):** actual browser rendering, browser console errors, browser Network-tab inspection, and clicking through the real UI (runs list → open thread → snapshot → history → editable fields → PO qty-mismatch candidate). None of this was performed because no browser automation tool is available.

**Files changed:** none.

**Next phase:** READY — proceeding to Phase 9 (full system regression).

---

## PHASE CHECKPOINT

**Phase:** 9 — Full System Regression
**Status:** PASSED
**Date/time:** 2026-09-07 (session continuation)

**Backend:** 627 passed, 1 xfailed, 0 failed (this run — the pre-existing, self-documented UUIDv7-ordering flaky test in `test_get_latest_by_email_event_returns_the_most_recently_updated`, unrelated to any of this task's work, happened to pass this run; it has been observed to flip pass/fail intermittently throughout this whole session, always for the same documented reason, never as a new regression). Ruff clean.

**BFF:** 33 passed.

**Frontend:** 9 tests passed, type-check clean, lint 0 errors (2 pre-existing unrelated warnings).

**Frontend build:** `npm run build` still fails on the SAME pre-existing issue — `src/features/csl-demo/pages/PerformanceDashboardPage.tsx(283,17)`, identical Recharts `CategoricalChartFunc` type mismatch, identical error text to before any of this session's work began. **Confirmed pre-existing, not introduced**: `git status` shows this file was never modified (not in the changed-files list) throughout this entire task.

**Files changed:** none this phase (verification only).

**Next phase:** READY — proceeding to Phase 10 (final scope audit).

---

---

## PHASE CHECKPOINT

**Phase:** 0 — Baseline + Reproduction (A.2 task)
**Status:** PASSED
**Date/time:** 2026-09-07 (session continuation)

**Files changed:** none (read-only + process management only: started/stopped dev servers, no repo files touched)

**Baseline re-confirmed:**
- Backend: 621 passed, 1 failed (pre-existing flaky `test_get_latest_by_email_event_returns_the_most_recently_updated`, self-documented UUIDv7-ordering issue, unrelated to any of this task's work), 1 xfailed. Ruff clean.
- BFF: 33 passed.
- Frontend: 9 passed, type-check clean.

**Frontend "no data loading" — reproduction evidence (strong hypothesis, not yet 100% confirmed via browser):**
1. `netstat` showed port 5173 already `LISTENING` (PID 30352) with an `ESTABLISHED` connection from PID 26784.
2. Process inspection: PID 30352 = `node.exe` running `C:\Project\nabdev-ui-kit\...\vite\bin\vite.js` (parent `cmd.exe /c vite`) — i.e. a real, already-running Vite dev server for this exact project. PID 26784 = `chrome.exe` — a real Chrome browser actively connected to it.
3. Before this session started any backend/BFF process, ports 8010/8020 were free (my `uvicorn --port 8010`/`--port 8020` bound successfully with no "address in use" error) — meaning **no backend or BFF was running** while that existing browser tab was open and presumably being used to observe "no data loading".
4. `GET http://localhost:5173/` returns a normal Vite HTML shell (200, no build error) — the app itself loads fine; only API-backed data would have failed.
5. Once backend (8010) + BFF (8020) were started this session: `GET /runs?view=threads&limit=50` via the BFF returned **HTTP 200** with 50 real items (CMIR + PO threads, real data, real `updated_at`/`stage`/`status` values).
6. CORS preflight (`OPTIONS` with `Origin: http://localhost:5173`) against the BFF returns `access-control-allow-origin: http://localhost:5173` correctly — CORS is not blocking this origin.

**Working hypothesis:** the reported "no data loading" was caused by the backend and/or BFF process simply not running at the time — a pure environment/process-state issue, not a code defect. Every API call from the real browser session would have failed with a network/connection error (invisible to CORS, since the request never reaches the CORS-enforcing server at all), and the UI's loading/error state would show no data. Now that both processes are running (started this session, left running for Phase 7), a refresh of the existing browser tab is expected to resolve it.

**Not yet done:** actual browser-console/network confirmation (no browser automation tooling available — same limitation as the prior gap audit). Will attempt further confirmation in Phase 7, and will investigate deeper (frontend code-level issue) if refreshing does NOT resolve it.

**No source/test/config changes made this phase**, consistent with the phase's own instruction to prefer no modifications.

**Next phase:** READY — proceeding to Phase 1 (A.2 investigation).

---

## PHASE CHECKPOINT

**Phase:** 0 — Baseline and Reproduction
**Status:** PASSED
**Timestamp:** 2026-09-07T08:30Z (approx, local investigation session)

**Starting commit:** `2152b2eb43198790a358cd780d942b42e5b4cff2` (branch `refactor/projection-schema-api-standardization`)
**Current commit:** same (no commits made)

**Files changed this phase:** none (read-only)
**Tests added:** none
**Tests executed:** full backend pytest, backend ruff, BFF pytest, frontend tests/type-check/lint
**Tests passed:** see baseline table below
**Tests failed:** 1 apparent failure, resolved as a test-invocation artifact (see below), not a real regression
**E2E executed:** none this phase (Phase 0 is investigation-only)
**E2E result:** N/A

### Baseline established

| Area | Result |
|---|---|
| Backend pytest | 619 passed, 1 xfailed, 0 failed |
| Backend ruff | all checks passed |
| BFF pytest | 33 passed |
| Frontend tests | 6 passed (1 file) |
| Frontend type-check | clean |
| Frontend lint | 0 errors, 2 pre-existing warnings (`react-refresh/only-export-components` in `badge.tsx`/`button.tsx`, unrelated) |

**Note on test-run artifact:** first pytest invocation showed `1 failed, 618 passed` —
`test_internal_api_key.py::test_correct_key_passes_through_to_the_real_handler`. Root
cause: I had set `$env:APP_INTERNAL_API_KEY` in the shell before running pytest, which
pre-empted `tests/conftest.py`'s `os.environ.setdefault("APP_INTERNAL_API_KEY",
TEST_INTERNAL_API_KEY)` (setdefault is a no-op if the var is already set). Re-ran
without the shell-level override: clean `619 passed, 1 xfailed, 0 failed`, matching
the stated baseline exactly. Confirmed as a test-invocation artifact, not a code
regression.

### Protected-scope snapshot (SHA-256, current working tree = Phase-0 baseline)

These files already carry prior, accepted, uncommitted changes from an earlier session
(session-lifecycle fix, PO concurrency fix) — "untouched" for this task means no
further diffs beyond this Phase-0 snapshot, not identical to git HEAD.

| File | SHA-256 (Phase 0) |
|---|---|
| `app/agents/cmir/graph.py` | `9e4adccc0436600203e10b7c139d4d7a944a02fea7f68720b95b42595f1848f0` |
| `app/agents/po_validation/graph.py` | `dbcc1912acc8244469e74bfc61ef01fae813173f2fa88bdfa164022a03a97ed1` |
| `app/core/container.py` | `65873d7911117885a69f6c293e8300faf76cab777aeb114a1ab83ecc8a192e7d` |
| `app/repositories/common/master_data.py` | `fd29f6176cdced5183775ac1cb7dea5a923085b7d0754a76f2c2678cc9baf27a` |
| `app/repositories/common/purchase_order.py` | `517efb55afdc1ffb9bd88e076f2cb961744533a3b90637c7bc95015c14a14be5` |

`get_or_create_retailer`/`get_or_create_plant` (master_data.py:169,344) and
`get_or_create_purchase_order`/`get_or_create_line` (purchase_order.py:136,262)
confirmed present — frozen, not to be modified.

Git status at Phase 0 (unchanged from before this task started):
```
 M README.md
 M app/api/dependencies.py
 M app/core/container.py
 M app/repositories/common/master_data.py
 M app/repositories/common/purchase_order.py
 M app/schemas/cmir/threads.py
 M app/services/cmir/run_service.py
 M app/services/po_validation/service.py
 M tests/unit/api/test_workflow_threads_api.py
 M tests/unit/services/test_cmir_run_service.py
 M tests/unit/services/test_po_validation_service.py
?? docs/ARCHITECTURE.md
?? tests/integration/test_po_ingestion_concurrency_postgres.py
?? tests/unit/repositories/test_master_data_get_or_create.py
?? tests/unit/repositories/test_purchase_order_get_or_create.py
```

### A.3 — key AND value semantics (investigated, understood)

- **Key mismatch confirmed:** `app/agents/po_validation/nodes.py:145` builds the
  `candidate` interrupt payload with key `"plant_id"`; `CandidateInfo`
  (`app/schemas/po_validation/threads.py:20-25`) declares a **required** field
  `plant: str`. Pydantic `model_validate` on `PoValidationThreadSnapshotResponse`
  (`workflow_threads.py:120`) will raise `ValidationError` (uncaught — not
  `NotFoundError`) for any real qty-mismatch snapshot fetch.
- **Value semantics traced end-to-end:**
  - `nodes.py:89-99`: `check_material_master` reads `po_line["plant_id"]` (a UUID FK)
    and calls `find_material_master(sap_material_number, plant_id)`.
  - `app/repositories/common/master_data.py:319-326`: `find_material_master` selects
    `MaterialMaster` by `(sap_material_number, plant_id)` and returns
    `_material_master_to_dict(row)` (`:56-64`), whose `"plant_id"` key is the raw
    `MaterialMaster.plant_id` **UUID column**, not a plant code.
  - `nodes.py:145` copies that same raw UUID into the `candidate` dict under
    `"plant_id"`.
  - Meanwhile, **every other "plant" reference in this domain is a plant_code
    string**: the PO ingestion payload's own `"plant"` field
    (`app/services/po_validation/service.py:171`: `uow.master_data.get_or_create_plant(payload["plant"])`)
    is a human-readable code (confirmed live this session with values like
    `"E2E-PLANT-1"`), and `Plant`/`_plant_to_dict` (`master_data.py:73-79`) expose
    `plant_code` as the canonical human identifier. No `get_plant_by_id` (or any
    UUID→plant_code reverse lookup) exists anywhere in `master_data.py` — only
    `get_plant_by_code` (forward lookup by code).
  - **Conclusion: canonical value semantics = plant_code (string), not plant UUID.**
    The current node output is wrong on both axes — wrong key name AND wrong value
    (a raw UUID where a plant_code is the established domain convention).
  - **UI impact today:** checked `QtyMismatchDecisionForm.tsx` — it does not
    currently render `candidate.plant` anywhere (only `availableQuantity`/
    `shortfall`/`suggestedSubstituteMaterialCode`). So today's fix is not
    "unblocking a visibly broken value," it's "making a required API field
    correct," independent of current UI usage. Not ambiguous — no STOP needed.
  - No `get_plant_by_id`-style method exists; Phase 1 will need to add one
    (a new repository method, not a modification of the frozen `get_or_create_*`
    methods) if the fix requires resolving `plant_id` → `plant_code`.

### A.1 — transaction root cause (fully traced, not delegated)

- `PoValidationService`... n/a, this is CMIR. `CmirRunService.submit_decision`
  (`run_service.py:557`) opens `with self._unit_of_work_factory() as uow:` at
  `:580`, and on the resume path calls `self._handle_graph_state(...)` (`:597`),
  which spans `:618-757+`.
- Inside `_handle_graph_state`, on the conflict branch: `uow.human_actions.apply_human_action(...)`
  (`:729`) and `uow.agent_runs.update_status(run_id, status, completed=True)` (`:742`)
  run first, THEN `if conflict: raise ConflictError(...)` (`:744-756`) — inside the
  same `with` block.
- Traced `self._unit_of_work_factory` → `Container.cmir_unit_of_work()`
  (`container.py:157-178`) → `self.cmir_repos()` (`:167`) → `Container.cmir_repos()`
  (`:136-154`) → `with self.database.session() as session:` (`:141`) → the actual
  `Database.session()` contextmanager (`app/db/session.py:104-116`):
  ```python
  try:
      yield session
      session.commit()
  except Exception:
      session.rollback()
      raise
  ```
- **Confirmed, fully traced, not just asserted:** raising `ConflictError` inside
  `_handle_graph_state` propagates up through `_handle_graph_state` → the
  `with self._unit_of_work_factory()` block → `Database.session()`'s `except
  Exception` clause → `session.rollback()` fires, undoing the just-applied
  `apply_human_action`/`agent_runs.update_status` writes. The code's own comment
  at `:745-746` ("already committed and consistent") is factually wrong given this
  call chain.
- **Design A vs Design B not yet decided** — that is Phase 2's job. Phase 0 only
  requires understanding current behavior, which is now fully evidenced.

### B — `update_if_current` contract (read directly, not assumed)

`app/repositories/process/workflow.py:229-265`:

```python
def update_if_current(
    self, workflow_thread_id: UUID, *, expected_updated_at: datetime,
    status: str, stage: str, current_node: str | None = None,
    metadata: dict[str, Any] | None = None, error: str | None = None,
    completed: bool = False,
) -> bool:
    ...
    result = self._session.execute(
        update(WorkflowThread)
        .where(WorkflowThread.id == workflow_thread_id,
               WorkflowThread.updated_at == expected_updated_at)
        .values(**values)
    )
    self._session.flush()
    return result.rowcount == 1
```

- **Return type:** `bool` — `True` iff exactly one row matched and was updated.
- **Zero-row behavior:** returns `False` (no exception raised). Caller must check
  the bool and raise `ConflictError` themselves.
- **Exceptions:** none specific to staleness; only underlying SQLAlchemy/DB errors
  would propagate.
- **Transaction behavior:** single atomic `UPDATE ... WHERE id=... AND
  updated_at=...` — closes the TOCTOU window that a separate read-then-compare-
  then-write sequence leaves open.
- **Integration note (not a signature change):** `expected_updated_at` here is a
  `datetime`, but the current `_ensure_current`/service-layer comparison works
  against a `str` (the wire-format `expected_updated_at` from the API request,
  produced by the `IsoDatetime` `PlainSerializer`). Wiring this in requires the
  **caller** to parse the incoming string back to a `datetime`
  (`datetime.fromisoformat(...)`) before calling `update_if_current` — no change
  to `update_if_current`'s own signature is needed. This satisfies Global Rule
  "smallest possible change, preserve all existing callers."
- Both CMIR (`workflow.py`, same repository) and PO-validation (uses the same
  `process.workflow_thread` table/repository — confirmed PO-validation has no
  separate `WorkflowThreadRepository`) will interpret this identically since it's
  the same method on the same repository class.

### D — backend/BFF/FE history contract (verified live + in code, matches prior audit)

- Backend `SnapshotHistoryItem` (`app/schemas/cmir/threads.py:74-85`): `actor`,
  `action_type`, `decision`, `response_payload: dict[str, Any] | None`,
  `responded_at: datetime | None`.
- Confirmed via live HTTP this session (prior gap-audit pass): actual BFF-relayed
  response for PO thread history: `{"actor":null,"action_type":null,"decision":null,
  "response_payload":null,"responded_at":null}` (matches schema exactly — an
  all-null row is a still-open `human_action`, per the schema's own docstring).
- Frontend currently expects (`thread.transformer.ts:44-49`, `thread.model.ts:35-40`):
  `actor`, `action_type`, `field_changes: Record<string, unknown>`,
  `created_at: string | null`.
- **`response_payload` vs `field_changes` — NOT assumed equivalent.** `response_payload`
  is `HumanAction`'s raw stored answer payload (whatever the reviewer submitted:
  e.g. `{sap_material_number, description}` for manual-CMIR-entry, or
  `{decision, substitute_material_code}`-shaped for qty-mismatch) — it is not a
  field-level before/after diff. `field_changes` (`{field: {from, to}}`) has no
  equivalent source in the current backend response; **it cannot be legitimately
  reconstructed from `response_payload` in general** (Phase 4 must not fabricate
  it — will need to determine what, if anything, the History UI can actually show
  from `decision`/`response_payload`/`responded_at`, or explicitly display "no
  field-level diff available").
- No browser tooling available this session — browser-level verification will be
  reported as unavailable in Phase 4, per instruction.

### Reproduction attempted honestly

- **A.3 (qty-mismatch 500):** NOT reproduced live this Phase 0 — no
  qty-mismatch-stage thread exists in the current DB, and creating one needs a
  multi-step setup (manual-CMIR-entry crosswalk submission, then a second
  ingestion against a plant with an existing `material_master` row whose
  `available_quantity` is below the new order). Static evidence (required
  Pydantic field vs. actual dict key, both read directly) is conclusive without
  live reproduction. Will attempt live reproduction in Phase 1 as part of its
  required E2E.
- **A.1 (conflict rollback):** NOT reproduced live this Phase 0 (requires two
  concurrent/sequential reviewers against the same CMIR thread — deferred to
  Phase 2's required test, which mandates real-Postgres, real-service-path
  reproduction).
- **B (TOCTOU):** NOT reproduced live this Phase 0 (deferred to Phase 3's required
  concurrency test).
- **D (history mismatch):** reproduced live in the prior gap-audit pass this
  session (real BFF response captured above) — confirmed present.

### Acceptance criteria

- Current git state recorded: PASS
- Baseline established: PASS (619/1/0 backend, 33 BFF, 6 frontend, clean
  type-check, 0 lint errors, clean ruff)
- Protected scope verified (hashed snapshot taken): PASS
- A.3 key AND value semantics understood: PASS (plant_code is canonical; current
  node value is a UUID; no reverse lookup exists yet)
- A.1 transaction behavior understood: PASS (fully traced call chain, root cause
  confirmed, not just asserted)
- B `update_if_current` contract understood: PASS (read directly, contract
  documented above)
- D backend/BFF/FE contract understood: PASS (confirmed live + in code)
- Reproduction attempted honestly: PASS (documented what was/wasn't reproduced
  and why)
- Blockers documented: PASS (see above)

**Known risks:** none blocking.
**Known limitations:** no browser E2E tooling available (affects Phase 4 only);
qty-mismatch/conflict/TOCTOU live reproductions deferred to their respective
phases per the prompt's own structure (each phase owns its E2E).

**Protected scope:**
- `graph.py` (both): untouched
- `container.py` session/UoW structure: untouched
- migrations: untouched
- existing PO `get_or_create_*`: untouched

**Next phase:** READY

---

## PHASE CHECKPOINT

**Phase:** 2 — A.1 CMIR Conflict-Path Rollback
**Status:** PASSED
**Timestamp:** 2026-09-07T08:55Z (approx)

**Starting commit:** `2152b2eb43198790a358cd780d942b42e5b4cff2`
**Current commit:** same (no commits made)

**Files changed:**
- `app/services/cmir/run_service.py` — `_handle_graph_state` return-shape change
  (returns `(result, conflict_error_or_None)` instead of raising internally) +
  its 3 call sites (`_process_one_email`-equivalent, `submit_missing_fields`,
  `submit_decision`) updated to raise the deferred conflict AFTER their
  `with self._unit_of_work_factory()` block exits.

**Selected semantics: Design B**, evidenced directly by the code's own
pre-existing comment (not a guess): `run_service.py`'s conflict branch already
stated the intended behavior — *"The thread bookkeeping above is already
committed and consistent (mirrors approve/reject's own close-out) -- this
raise is purely to give the reviewer who just clicked approve immediate,
honest feedback that their approval did not actually commit"* — i.e., the
reviewer's action (thread bookkeeping: `human_action` completed, `agent_run`
completed, `workflow_thread` moved to `COMPLETED_CONFLICT`/`completed_conflict`)
is a real, separate fact from whether the underlying CMIR content write itself
won its SCD2 race, and must persist regardless. The bug was purely that the
implementation contradicted its own stated intent by raising inside the
transactional block.

**Transaction behavior before:** `raise ConflictError(...)` at the end of
`_handle_graph_state`'s conflict branch happened *inside*
`with self._unit_of_work_factory() as uow:` (traced end-to-end in Phase 0:
`submit_decision` → `Container.cmir_unit_of_work()` → `cmir_repos()` →
`Database.session()`'s `except Exception: session.rollback(); raise`) — so
`apply_human_action`/`agent_runs.update_status`, applied moments earlier in
the same block, were rolled back.

**Transaction behavior after:** `_handle_graph_state` returns the conflict as
data (`ConflictError` instance, not raised); each caller captures
`(result, conflict_error)` inside its `with` block (letting the block exit
normally, with no exception, so `session.commit()` fires exactly as it would
for the non-conflict case), and only raises `conflict_error` in the caller's
own function body, after the `with` block has already closed/committed.

**Exact fix:** see files above; the conflict branch became:
```python
result = self._get_stage(uow.workflow_threads, workflow_thread_id)
if conflict:
    return result, ConflictError(code="CMIR_VERSION_CONFLICT", ...)
return result, None
```
and each caller:
```python
with self._unit_of_work_factory() as uow:
    ...
    result, conflict_error = self._handle_graph_state(...)
if conflict_error is not None:
    raise conflict_error
return result
```

**Tests added:**
`tests/integration/test_cmir_conflict_postgres.py` (new file) —
`test_submit_decision_conflict_commits_bookkeeping_to_real_postgres`: uses the
REAL `Container.build()` wiring (real `Database.session()`, real repositories,
real `cmir_unit_of_work()`), with only the graph's `.invoke()` stubbed to
deterministically produce a `cmir_write_result="conflict"` state (the graph's
own SCD2-conflict-detection logic is a separate, already out-of-scope concern,
consistent with the existing SQLite unit-test file's own stated scope). Not a
repository-only test — exercises the actual `CmirRunService.submit_decision`
service path.

**Verified the test is a genuine regression guard, not a no-op:** temporarily
moved the raise back inside the `with` block (simulating the original bug),
reran — **test failed exactly as expected**, reproducing the original bug
live against real Postgres:
```
AssertionError: assert 'waiting_approval' == 'completed_conflict'
```
(i.e., with the bug present, an independent DB read after the conflict shows
the thread still `waiting_approval`, not closed out.) Restored the fix, reran
— passed. Fix was never left reverted between these two runs.

**Independent DB verification:** the test's assertions run against a second,
independent `Session` (`pg_database.new_session()`, opened directly against
Postgres, NOT the Session `submit_decision` itself used) — proving durability,
not merely reading back an as-yet-uncommitted view from the same connection.
Confirmed: `process.workflow_thread.status == 'completed_conflict'`,
`process.human_action.status == 'completed'`,
`process.agent_run.status == 'completed_conflict'`.

**Checkpoint verification:** not performed for this specific test, and this is
an explicit, evidenced limitation, not an oversight: the test necessarily
stubs the graph (`uow.graph = _StubGraph(...)`) to deterministically produce a
conflict without a real SCD2 collision or a real LLM/Azure OpenAI call — so no
real `PostgresSaver` checkpoint write occurs in this scenario at all, and there
is nothing meaningful to inspect. This is safe to skip because the A.1 fix
itself never touches anything checkpoint-related: it only changes *when*
(relative to `session.commit()`) a Python exception is raised inside
`run_service.py`; it makes zero changes to `graph.invoke()` calls, the
checkpointer, or `container.py`. Per Phase 0's trace, the checkpointer is a
wholly separate connection/transaction (A.2's own subject) that this fix does
not interact with in any way — confirmed by inspection, not assumed.

**E2E result:** covered by the integration test above (real Postgres, real
service path, real transaction boundary) — no live HTTP/API-level conflict
E2E was additionally performed (would require reproducing a genuine two-
reviewer SCD2 race through the real graph's merge logic, a materially larger
setup); the integration test already isolates and proves the exact A.1 defect
(the transaction/control-flow bug), which is the full scope of this phase.

**Remaining A.2 risk:** unchanged, not addressed, not claimed to be addressed.
The checkpoint-vs-app-DB non-atomicity (A.2) remains exactly as documented in
Phase 0 — this fix makes the *application-DB side* internally consistent
(bookkeeping either fully commits or the whole request fails outright; no more
"partially committed, contradicting its own claim" states), but does nothing
about the separate, still-open question of whether the checkpoint and the
application DB can independently diverge on a partial failure elsewhere. No
new A.2 observations arose beyond what Phase 0 already recorded.

**Acceptance criteria:**
- intended conflict semantics established: PASS (Design B, evidenced by the
  code's own pre-existing comment, not a guess)
- transaction behavior matches those semantics: PASS
- real service path tested: PASS (real `Container`/`CmirRunService`, stubbed
  graph only)
- independent DB state verified: PASS
- checkpoint behavior verified: N/A for this test, with an evidenced reason
  the fix cannot affect it (documented above) — no STOP needed, not ambiguous
- no A.2 architecture changed: PASS
- relevant tests pass: PASS (620 passed, 1 xfailed, 0 failed; ruff clean)
- no protected scope changed: PASS (hashes identical to Phase 0/1)

**Known risks:** none new.
**Known limitations:** live HTTP-level real-graph conflict E2E not performed
(see above) — the integration test already covers the actual defect's scope.

**Protected scope:**
- graph.py (both): untouched (hash-verified)
- container.py: untouched (hash-verified)
- migrations: untouched
- existing PO get_or_create_*: untouched (hash-verified)

**Next phase:** READY

---

## PHASE CHECKPOINT

**Phase:** 3 — B (CMIR + PO TOCTOU) — SCOPE REVISED, then implemented
**Status:** PASSED (narrow scope, per explicit user decision)
**Timestamp:** 2026-09-07T09:15Z (approx)

**Starting commit:** `2152b2eb43198790a358cd780d942b42e5b4cff2`
**Current commit:** same (no commits made)

**STOPPED mid-phase and reported to the user** (per rule 15 — "there is
evidence that the audit's root cause is incorrect"): traced every mutating
CMIR/PO resume path (`submit_decision`, `submit_missing_fields`,
`update_draft`, `submit_manual_cmir_entry`, `submit_qty_mismatch_decision`)
and found they all funnel through the shared
`HumanActionRepository.apply_human_action` (`app/repositories/process/workflow.py:366-449`),
which already does a **guarded, atomic** `UPDATE ... WHERE id=... AND
status='open'` on the `human_action` row before ever touching
`workflow_thread`. This means a genuine concurrent double-decision on the
same thread was **already** resolved to exactly one success today — not the
originally-assumed "two silent successes" TOCTOU. The real, narrower defect:
the loser's failure was a bare `ValueError`, converted by every caller's
generic `except Exception` wrapping into `ExternalServiceError`
(`WORKFLOW_RESUME_FAILED`, 5xx) instead of a clean, expected
`ConflictError`/409 — and wiring `update_if_current` into the tail of
`apply_human_action` would not have changed this at all (a red herring for
the real gap), while doing anything about the deeper "both callers still
invoke `graph.invoke()` before either reaches the guard" question edges into
A.2 (checkpoint/graph) territory.

**User decision (asked via structured options, not guessed):** "Narrow fix
only" — fix the error-contract bug (clean 409 for the loser), leave the
double-graph-invoke question for A.2's later HLD work.

**Root cause confirmed:** `apply_human_action`'s `if result.rowcount != 1:
raise ValueError(...)` (`workflow.py:416-419`, now fixed) was reached by the
loser of a race and converted into an opaque 5xx by every caller's blanket
`except Exception` handling, because none of them distinguished "an expected
`AppError`/conflict" from "a genuinely unexpected failure."

**Exact fix:**
1. `app/repositories/process/workflow.py`: `apply_human_action`'s
   `rowcount != 1` branch now raises `ConflictError(code="THREAD_STALE", ...)`
   instead of a bare `ValueError` — reusing the exact same conflict code
   `_ensure_current` already uses for "thread moved since you last saw it",
   not inventing a new one.
2. `app/services/cmir/run_service.py`: `update_draft`'s
   `apply_human_action` call site gained `except AppError: raise` before its
   existing generic `except Exception: raise ExternalServiceError(...)` (the
   other two CMIR call sites, `submit_missing_fields`/`submit_decision`,
   already had this guard from before this phase and needed no change).
3. `app/services/po_validation/service.py`: both
   `submit_manual_cmir_entry` and `submit_qty_mismatch_decision` gained the
   same `except AppError: raise` guard before their generic
   `except Exception` (neither had it before).

**`update_if_current` usage:** NOT wired in for this narrow scope — evidenced
as unnecessary for the actual defect (see above). It remains available,
unused, exactly as found in Phase 0, for any future genuine
read-then-unconditional-write gap that might still exist elsewhere (none
identified in this investigation).

**Files changed:**
- `app/repositories/process/workflow.py` (+ import, exception type change)
- `app/services/cmir/run_service.py` (1 call site: `except AppError: raise`)
- `app/services/po_validation/service.py` (2 call sites: `except AppError:
  raise`, + `AppError` import)
- `tests/unit/repositories/test_workflow_repository.py` — existing test
  `test_human_action_apply_human_action_raises_for_already_closed_action`
  updated from `pytest.raises(ValueError)` to `pytest.raises(ConflictError)`
  + code/status_code assertions (this is an intentional update to match the
  deliberately-changed contract, not a masked regression — `apply_human_action`
  is not on the frozen/protected list).

**Tests added:** `tests/integration/test_hitl_decision_concurrency_postgres.py`
(new file, 2 tests, genuine `threading.Barrier`+`threading.Thread`
concurrency against real Postgres):
- `test_cmir_concurrent_decisions_on_same_pending_action_one_wins_one_conflicts`
  — real `Container`-wired `CmirRunService` (stubbed graph only, per the same
  justification as Phase 2's test), two concurrent `submit_decision` calls
  against the same thread/pending action.
- `test_po_concurrent_manual_cmir_entry_on_same_pending_action_one_wins_one_conflicts`
  — real `build_po_validation_service()` (real, unstubbed PO graph — no LLM
  dependency there), two concurrent `submit_manual_cmir_entry` calls.

**CMIR concurrency test result:** exactly 1 success, exactly 1
`ConflictError(code="THREAD_STALE", status_code=409)`, zero
`ExternalServiceError`s. Independent-connection DB check: exactly 1
`process.human_action` row `status='completed'` for the thread, final
`workflow_thread.status == 'completed_approved'` (single coherent state, not
duplicated/inconsistent).

**PO concurrency test result:** same shape — exactly 1 success, exactly 1
`ConflictError(THREAD_STALE/409)`, independent-connection check confirms
exactly 1 completed `human_action` for the thread.

**Final DB-state verification:** performed separately for CMIR and PO (see
above), via a second independent `pg_database.new_session()`, not the
session either service call used.

**Verified all 3 new/changed tests are genuine regression guards, not
no-ops:** temporarily reverted `apply_human_action`'s raise back to the bare
`ValueError` and reran — **all 3 failed exactly as expected** (the 2 new
concurrency tests plus the updated unit test), reproducing the original
"opaque failure instead of clean conflict" bug live against real Postgres.
Restored the fix, reran — all passed. Fix was never left reverted between
runs.

**Test results:** full backend suite **622 passed, 1 xfailed, 0 failed**
(620 + the 2 new concurrency tests). Ruff: all checks passed.

**Existing test impact:** only the one intentional update described above;
no other existing test needed changes.

**Acceptance criteria (revised for narrow scope):**
- actual service paths use the corrected error contract: PASS
- concurrency tests use independent sessions/real Postgres/real service
  paths: PASS
- exactly one concurrent request succeeds: PASS (both CMIR and PO)
- exactly one receives the expected clean conflict: PASS (both CMIR and PO)
- final DB state correct, no duplicate business action: PASS
- existing tests pass (with the one intentional, evidenced update): PASS
- no protected scope changed: PASS (hashes identical to Phases 0/1/2)

**Known risks:** the deeper "both concurrent callers already invoke
`graph.invoke()` before either reaches the DB-level guard" observation
remains unaddressed — explicitly deferred by the user's own decision, not
silently dropped. Recommend folding it into the eventual A.2 HLD work, since
it's the same class of problem (checkpoint/graph invocation not coordinated
with the application DB's optimistic-concurrency guard).
**Known limitations:** none new for the narrow scope actually implemented.

**Protected scope:**
- graph.py (both): untouched (hash-verified)
- container.py: untouched (hash-verified)
- migrations: untouched
- existing PO get_or_create_*: untouched (hash-verified)

**Next phase:** READY

---

## PHASE CHECKPOINT

**Phase:** 4 — D Frontend History Contract
**Status:** PASSED
**Timestamp:** 2026-09-07T09:35Z (approx)

**Starting commit:** `2152b2eb43198790a358cd780d942b42e5b4cff2` (backend);
`nabdev-ui-kit` has no git repo, no commit tracking available for that repo.
**Current commit:** unchanged (no commits made anywhere)

**Confirmed canonical contract:** the backend (`SnapshotHistoryItem`,
`app/schemas/cmir/threads.py:74-85`: `actor`, `action_type`, `decision`,
`response_payload`, `responded_at`) is canonical — verified via live HTTP
this session (captured real BFF-relayed responses in the earlier read-only
gap audit) and via direct code read. The frontend's prior assumption
(`action_type`, `field_changes`, `created_at`) never matched any real backend
response; not touching the backend schema, only the frontend.

**Key finding preserved from Phase 0:** `response_payload` (the reviewer's
raw submitted answer) is NOT equivalent to a field-level `field_changes`
diff — there is no backend source for such a diff on a history entry. The
fix does not fabricate one.

**Files changed:**
- `src/models/thread.model.ts` — `ThreadHistoryEntry`: replaced
  `actionType`/`fieldChanges`/`createdAt` shape with `actor: string | null`,
  `actionType: string | null`, `decision: string | null`,
  `responsePayload: Record<string, unknown> | null`, `respondedAt: string | null`.
- `src/transformers/thread.transformer.ts` — `RawHistoryEntry` and
  `transformHistoryEntry` updated to map the real backend field names
  1:1, no reconstruction/fabrication.
- `src/features/cmir-intelligence/components/ThreadHistoryList.tsx` —
  renders `actor` (null-safe), `actionType`-derived description, the raw
  `decision` value when present (real data the old UI never showed), the raw
  `responsePayload` as submitted key/value pairs (explicitly NOT a
  `from → to` diff), and `respondedAt` (renamed from `createdAt`).
- `src/transformers/thread.transformer.test.ts` — 3 new tests using the
  REAL backend response shape (including the exact all-null "still-open
  pending action" shape captured live via the BFF this session), not the old
  field names.

**Transformer/model test:** `describe('transformHistoryEntry — real backend
contract (SnapshotHistoryItem)')`, 3 tests:
1. maps a completed `human_action` row field-for-field
2. maps a still-open pending action row (all fields null) — matches the
   exact real response captured earlier this session
3. asserts no `fieldChanges` property is fabricated from `response_payload`

**Verified genuine regression guards, not no-ops:** temporarily reverted
`transformHistoryEntry` to read the old `field_changes`/`created_at` shape —
**2 of 3 new tests failed exactly as expected** (the completed-row mapping
test and the no-fabrication test; the still-open/all-null test still passed
coincidentally, since all-null input produces all-null output either way —
expected and consistent, not a gap in the test). Restored the fix, reran —
all passed. Fix was never left reverted between runs.

**Component/UI test:** NOT added. This repo has no jsdom/React Testing
Library (`@testing-library/react`) dependency at all (`grep` confirmed zero
matches in `package.json`) — adding one would mean introducing new test
infrastructure/dependencies, which is out of this phase's scope (the phase
instructions say to add a component test "if existing test infrastructure
supports it"; it does not). Documented as a limitation, not silently
skipped.

**Browser E2E:** NOT performed. No browser automation tooling (Playwright/
Puppeteer/Cypress) is available in this session — confirmed via the same
check in the earlier read-only gap audit. Reported as unavailable, not
claimed.

**Test results:** frontend `9 passed` (6 baseline + 3 new), type-check
clean, lint `0 errors` (2 pre-existing unrelated warnings). BFF `33 passed`
(unaffected, confirmed to rule out any accidental cross-repo impact).

**Rendering changes:** `ThreadHistoryList` now shows the reviewer's actual
`decision` (e.g. "approve", "use_substitute") inline when present — real
information the old, contract-mismatched UI never displayed at all (every
history entry silently rendered with a blank date and no field list before
this fix, since `createdAt`/`fieldChanges` never existed on any real
response).

**Any information that cannot be represented:** a true field-level
before/after diff for history entries cannot be shown — the backend never
captured one for this table (`response_payload` is the raw submitted answer,
not a diff against the prior value). This is documented in code comments
(`thread.model.ts`, `ThreadHistoryList.tsx`) rather than silently omitted or
faked.

**Acceptance criteria:**
- backend contract confirmed: PASS
- frontend transformer matches it: PASS
- History receives real data: PASS (previously it received none — every
  field was undefined/mismatched)
- no fabricated mappings: PASS
- tests pass, proven to fail without the fix: PASS
- browser E2E: N/A, tooling unavailable, explicitly reported

**Known risks:** none.
**Known limitations:** no component/browser-level rendering test (documented
reason above).

**Protected scope:** N/A for this phase (frontend-only change; no backend
protected files touched — confirmed via hash re-check below).

**Next phase:** FINAL VERIFICATION

---

## PHASE CHECKPOINT

**Phase:** 1 — A.3 PO Qty-Mismatch Plant Contract
**Status:** PASSED
**Timestamp:** 2026-09-07T08:35Z (approx)

**Starting commit:** `2152b2eb43198790a358cd780d942b42e5b4cff2`
**Current commit:** same (no commits made)

**Files changed:**
- `app/agents/po_validation/nodes.py` — 1 dict-key/value fix
- `tests/unit/agents/test_po_validation_graph.py` — fixture value distinguished + regression assertions added

**Root cause confirmed:**
`nodes.py:145` built the qty-mismatch interrupt's `candidate` dict with key
`"plant_id"` holding `material["plant_id"]` — a raw `MaterialMaster.plant_id`
**UUID** (`app/repositories/common/master_data.py:56-64,319-326`). `CandidateInfo`
(`app/schemas/po_validation/threads.py:20-25`) requires `plant: str`, and the
established domain convention for "plant" everywhere else (ingestion payload,
`get_or_create_plant`) is the **plant_code string**, not a UUID. Both the key
name AND the value type/meaning were wrong.

**Canonical field selected:** `plant` (plant_code string) — confirmed unambiguous
by evidence, no STOP needed:
- `CandidateInfo.plant: str` (required) already established in schema/tests/BFF/
  frontend (`CandidateInfo.plant`, `transformCandidate: plant: raw.plant` in
  `thread.transformer.ts`) before this fix.
- `po_line["plant"]` (the ingest payload's original plant_code string) is
  **already present** in `POGraphState` at the exact point `human_qty_mismatch_decision`
  runs (`app/services/po_validation/service.py:236`: `"plant": payload["plant"]`,
  set alongside `"plant_id": plant_id` at `:237`) — no new repository lookup
  method was needed at all; the correct value was already in scope.

**Exact fix** (`nodes.py:143-151`):
```python
"candidate": {
    "sap_material_number": material["sap_material_number"],
    "plant": po_line["plant"],   # was: "plant_id": material["plant_id"]
    "available_quantity": material["available_quantity"],
    ...
}
```
No repository changes, no schema changes, no frontend changes required — the
frontend/BFF/schema were already correct; only the node's producer side was wrong.

**Tests added:**
- `_po_line()` fixture (`test_po_validation_graph.py`) changed to use a plant_code
  (`"PLANT-CODE-1000"`) distinct from `plant_id` (`"1000"`), so an assertion can't
  pass by coincidental equality.
- `test_qty_mismatch_offers_one_hop_substitute_and_proceed_anyway` extended to
  assert `candidate["plant"] == "PLANT-CODE-1000"`, `"plant_id" not in candidate`,
  and `CandidateInfo.model_validate(candidate)` — exercising the REAL node output
  through the real schema, not a hand-built fixture.
- **Verified the test is a genuine regression guard**, not a no-op: temporarily
  reverted the fix, reran — test failed with `KeyError: 'plant'` as expected;
  restored the fix, reran — passed. (Fix was never left in the reverted state
  between these two runs.)

**Test results:** `tests/unit/agents/test_po_validation_graph.py`: 7 passed.
Full backend suite: **618 passed, 1 xfailed, 1 failed** — the 1 failure
(`test_workflow_repository.py::test_get_latest_by_email_event_returns_the_most_recently_updated`)
is a **pre-existing, unrelated flaky test**: reproduced independently 3x with
zero further code changes (2 pass, 1 fail); the method under test
(`get_latest_by_email_event`, `app/repositories/process/workflow.py:149-161`)
has a self-documented tiebreaker comment acknowledging SQLite's second-resolution
`func.now()` and relying on UUIDv7 `id.desc()` ordering as a tiebreaker — which
is not actually guaranteed monotonic for two ids minted within the same
millisecond. Confirmed unrelated: different file, different repository, different
domain (CMIR email-thread ordering, not PO-validation), untouched by this
phase's diff. **Classified: pre-existing, not introduced by this task.**
Ruff: all checks passed.

**E2E reproduction (real HTTP, real Postgres, real BFF):**
1. Ingested PO line 1 (`QTY-E2E-PO-1`, plant `LOC-ATL`, qty 5) → routed to
   `manual_cmir_entry` (no crosswalk yet).
2. Submitted `POST /workflow-threads/{id}/decisions`
   (`decision_type=MANUAL_CMIR_ENTRY`, `sap_material_number=SAP-999888`) →
   established the `(customer_id, customer_material_code)` → `SAP-999888`
   crosswalk; line 1 went touchless (`READY_FOR_SO_CREATION`, qty 5 ≤ available 100).
3. Ingested PO line 2 (`QTY-E2E-PO-2`, same customer/material/plant, qty 150 >
   available 100) → routed to `AWAITING_QTY_MISMATCH_DECISION` via the same
   crosswalk.
4. `GET /workflow-threads/{thread2}?include=snapshot` → **HTTP 200** (not 500).
   `candidate = {"sap_material_number":"SAP-999888","plant":"LOC-ATL",
   "available_quantity":100.0,"shortfall":50.0,"suggested_substitute_material_code":null}`
   — correct key, correct plant_code value (`"LOC-ATL"`, the real seeded plant,
   not a UUID).
5. Same request repeated through the BFF (`GET /threads/{thread2}/snapshot` on
   port 8020) → **HTTP 200**, identical `candidate.plant = "LOC-ATL"`, confirming
   the full backend → BFF → frontend-consumed contract now matches
   `CandidateInfo`/`transformCandidate` exactly, with zero frontend/BFF changes.

**Acceptance criteria:**
- root cause fixed: PASS
- key semantics correct: PASS
- value semantics correct: PASS (plant_code, not UUID)
- regression test added, proven to fail without the fix: PASS
- relevant tests pass: PASS (7/7 graph tests; 1 pre-existing unrelated flaky
  test elsewhere, documented above)
- real E2E passes: PASS (backend 200, BFF 200, real Postgres, real crosswalk)
- no protected scope changed: PASS (hashes identical to Phase 0)

**Known risks:** none.
**Known limitations:** none for this phase.

**Protected scope:**
- graph.py (both): untouched (hash-verified)
- container.py: untouched (hash-verified)
- migrations: untouched
- existing PO get_or_create_*: untouched (hash-verified)

**Next phase:** READY

---
