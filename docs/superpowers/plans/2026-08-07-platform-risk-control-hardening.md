# Platform Risk Control Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the confirmed risk-control bypasses and accounting gaps without changing successful API contracts or replacing the existing SQLite migration strategy.

**Architecture:** Keep `RiskController` as the policy and persistence boundary and `MonitorEngine` as the operation orchestrator. Preserve structured platform exceptions until the controller records them, route account-bound API reads through existing engine guards, and use explicit lifecycle cleanup for SQLite compatibility.

**Tech Stack:** Python 3, FastAPI, SQLModel/SQLite, asyncio, unittest/pytest.

## Global Constraints

- Preserve existing successful API payloads; deferred reads add only `skipped`, `reason`, and optional `next_allowed_at`.
- Keep risk/auth/network write failures queued and deterministic business failures failed.
- `conservative` remains the safe default; only exact normalized `custom` disables hard floors.
- Do not add a new migration framework or change public endpoint paths.
- Every production change follows RED → GREEN with a focused regression test.

---

### Task 1: Harden Core Policy Resolution and Upgrade Accounting

**Files:**
- Modify: `app/config.py:127-146`
- Modify: `app/risk.py:9-12,188-202,274-355`
- Test: `tests/test_risk_control.py`

**Interfaces:**
- Consumes: `load_config(path)`, `RiskController.preflight()`, `PublishTask.created_at/done_at`.
- Produces: normalized `RiskControlConfig.mode`, UTC+8 fallback timezone, and effective historical publish completion timestamps.

- [ ] **Step 1: Add failing configuration, timezone, and legacy-publish tests**

Add these cases to `RiskControlTests`:

```python
def test_unknown_risk_mode_falls_back_to_conservative(self):
    config_path = Path(self.tmp.name) / "config.yaml"
    config_path.write_text(
        "risk_control:\n  mode: Conservativ\n"
        "  publish_min_gap_seconds: 0\n"
        "  network_group_concurrency: 99\n",
        encoding="utf-8",
    )
    cfg = load_config(str(config_path))
    controller = RiskController(cfg)
    self.assertEqual(cfg.risk_control.mode, "conservative")
    self.assertEqual(controller._limits(OperationKind.PUBLISH), (7200, 1, 3))
    self.assertEqual(controller._network_concurrency(), 1)

def test_explicit_custom_risk_mode_is_normalized(self):
    config_path = Path(self.tmp.name) / "config.yaml"
    config_path.write_text(
        "risk_control:\n  mode: ' CUSTOM '\n  publish_min_gap_seconds: 0\n",
        encoding="utf-8",
    )
    cfg = load_config(str(config_path))
    self.assertEqual(cfg.risk_control.mode, "custom")
    self.assertEqual(RiskController(cfg)._limits(OperationKind.PUBLISH)[0], 0)

def test_invalid_timezone_falls_back_to_shanghai_day_boundary(self):
    account_id = self._account(timezone_id="invalid/timezone")
    account = None
    with db.get_session() as session:
        account = session.get(DouyinAccount, account_id)
    start = RiskController(self.cfg)._local_day_start_utc(
        account, datetime(2026, 8, 7, 1, 0, 0))
    self.assertEqual(start, datetime(2026, 8, 6, 16, 0, 0))

def test_upgrade_day_counts_legacy_publish_created_at_when_done_at_missing(self):
    account_id = self._account()
    self.cfg.risk_control.mode = "custom"
    self.cfg.risk_control.publish_min_gap_seconds = 0
    self.cfg.risk_control.publish_hourly_cap = 0
    self.cfg.risk_control.publish_daily_cap = 1
    self.cfg.risk_control.shared_write_gap_seconds = 0
    self.cfg.engine.quiet_hours_enabled = False
    now = datetime(2026, 8, 7, 3, 0, 0)
    with db.get_session() as session:
        session.add(PublishTask(
            account_id=account_id, status="done",
            created_at=now - timedelta(hours=1), done_at=None))
        session.commit()
    decision = RiskController(self.cfg).preflight(
        account_id, OperationKind.PUBLISH, now=now)
    self.assertFalse(decision.allowed)
    self.assertEqual(decision.signal, "daily_cap")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_risk_control.py -q
```

Expected: the three new behaviors fail because unknown modes remain custom-like, invalid zones use UTC, and null `done_at` rows are ignored.

- [ ] **Step 3: Normalize mode during configuration loading**

After filtering the `risk_control` mapping in `load_config`, normalize it before constructing the dataclass:

```python
risk_values = {
    k: v for k, v in risk.items()
    if k in RiskControlConfig.__dataclass_fields__
}
mode = str(risk_values.get("mode", "conservative") or "").strip().lower()
risk_values["mode"] = mode if mode in {"conservative", "custom"} else "conservative"
cfg.risk_control = RiskControlConfig(**risk_values)
```

- [ ] **Step 4: Add cached Shanghai timezone fallback**

In `app/risk.py`, add `logging` and `functools.lru_cache`, then resolve zones through:

```python
log = logging.getLogger("creatorhub.risk")

@lru_cache(maxsize=128)
def _resolve_timezone(timezone_id: str) -> tzinfo:
    try:
        return ZoneInfo(timezone_id)
    except ZoneInfoNotFoundError:
        log.warning("无效账号时区 %s，已回退 Asia/Shanghai", timezone_id)
        try:
            return ZoneInfo("Asia/Shanghai")
        except ZoneInfoNotFoundError:
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
```

Make `RiskController._timezone()` return `_resolve_timezone(account.timezone_id or "Asia/Shanghai")`.

- [ ] **Step 5: Count legacy publish completion with `coalesce`**

Import `func` from SQLAlchemy and use:

```python
publish_completed_at = func.coalesce(PublishTask.done_at, PublishTask.created_at)
```

Select and order by this expression in `_latest_success`; use it in the `>= since` predicate in `_count_successes`.

- [ ] **Step 6: Verify GREEN and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_risk_control.py -q
git add app/config.py app/risk.py tests/test_risk_control.py
git commit -m "fix: harden risk policy defaults and upgrade counts"
```

Expected: `tests/test_risk_control.py` passes.

---

### Task 2: Preserve Platform Failures Through Nested Reads

**Files:**
- Modify: `app/engine/monitor.py:261-322,953-1088,1811-1866,2226-2425,2450-2626`
- Test: `tests/test_xhs_risk_classification.py`

**Interfaces:**
- Consumes: `classify_platform_error(error)` and structured `XhsApiError` fields.
- Produces: nested read functions that return or raise the first controlled platform failure and never record it as success.

- [ ] **Step 1: Add failing nested-read regression tests**

Extend `XhsRiskClassificationTests`:

```python
def test_xhs_comment_discovery_returns_risk_instead_of_empty_success(self):
    calls = 0

    class RiskClient:
        async def note_comments(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise XhsApiError(
                "challenge", category="risk", status_code=429,
                signal="http_429")

    engine = MonitorEngine(self.cfg, _BrowserStub())
    engine._xhs_client = lambda *_args, **_kwargs: RiskClient()
    rule = {
        "platform": "xhs", "mode": "auto_reply", "target_kind": "work",
        "aweme_id": "note-1", "xsec_token": "token", "keyword": "",
        "sec_uid": "", "has_creator": False, "account_uid": "",
    }
    candidates, error = asyncio.run(engine._discover_targets(
        rule, "state", "", "", "", _Identity()))
    self.assertEqual(candidates, [])
    self.assertIsInstance(error, XhsApiError)
    self.assertEqual(calls, 1)

def test_guarded_read_records_structured_nested_risk_not_success(self):
    account_id = self._account_with_xhs_state()
    engine = MonitorEngine(self.cfg, _BrowserStub())
    error = XhsApiError(
        "challenge", category="risk", status_code=429,
        signal="http_429")

    async def operation():
        return {"ok": False, "error": error}

    result = asyncio.run(engine._guarded_read_dict(
        account_id, OperationKind.READ_HEAVY, "nested-risk", operation))
    self.assertFalse(result["ok"])
    with db.get_session() as session:
        state = session.get(AccountRiskState, account_id)
        events = session.exec(select(RiskEvent).where(
            RiskEvent.account_id == account_id)).all()
    self.assertEqual(state.risk_level, 1)
    self.assertEqual([event.outcome for event in events], ["risk"])
```

Add this fixture helper to the test class:

```python
def _account_with_xhs_state(self):
    with db.get_session() as session:
        account = DouyinAccount(
            platform="xhs", nickname="fixture", status="active",
            storage_state='{"cookies": [{"name": "a1", "value": "fixture"}]}',
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        return account.id
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_xhs_risk_classification.py -q
```

Expected: discovery returns an empty error, and/or structured errors are not safely normalized after recording.

- [ ] **Step 3: Preserve errors until accounting completes**

In `_guarded_read_dict`, pass the original `result["error"]` object to `record_failure()`, then replace it with `str(error)` before returning an API-facing dictionary. Apply the same normalization to any direct result path returning an exception object.

In `_discover_targets`, return the caught `XhsApiError` from per-note loops instead of `continue`. For non-XHS loop errors, call `classify_platform_error`; stop and return the error for `RISK`, `AUTH`, or `NETWORK`, while retaining existing business-error behavior.

In `_xhs_fetch_comments` and `_cw_xhs_creator`, re-raise `XhsApiError` instead of converting it into an empty collection. Let `_guarded_read_dict` perform the single persistent classification.

In `_scan_xhs_target_locked`, keep the first controlled exception object, break the detail loop, write `str(error)` to `MonitorTarget.last_error`, and return the object to `scan_target`; normalize it only after `record_failure()`.

- [ ] **Step 4: Verify GREEN and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_xhs_risk_classification.py tests/test_risk_control.py -q
git add app/engine/monitor.py tests/test_xhs_risk_classification.py
git commit -m "fix: preserve nested platform risk failures"
```

Expected: both focused suites pass and only one risk event is written per failed logical read.

---

### Task 3: Route Account-Bound API Reads Through Unified Gates

**Files:**
- Modify: `app/main.py:237-323,681-703,4875-4998`
- Create: `tests/test_risk_api_gates.py`

**Interfaces:**
- Consumes: `MonitorEngine.guarded_read_pair(account_id, kind, fallback_key, operation, empty_result=...)`.
- Produces: `_run_account_read(...)` in `app/main.py` plus guarded profile, published-list, media, and comment endpoints.

- [ ] **Step 1: Add failing cooldown-bypass tests**

Create `tests/test_risk_api_gates.py` with a temporary database setup matching other suites. Use a real `MonitorEngine` with an asyncio-lock browser stub, create an XHS account with an `a1` cookie, and record a risk failure before each endpoint call.

Core test shape:

```python
class RiskApiGateTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.previous_main_engine = main.engine
        self.previous_browser = main.browser
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "api-risk.db"))
        self.cfg = Config()
        self.browser = _BrowserStub()
        main.browser = self.browser
        main.engine = MonitorEngine(self.cfg, self.browser)
        with db.get_session() as session:
            account = DouyinAccount(
                platform="xhs", nickname="fixture", status="active",
                storage_state='{"cookies":[{"name":"a1","value":"fixture"}]}')
            session.add(account)
            session.commit()
            session.refresh(account)
            self.account_id = account.id

    def tearDown(self):
        main.engine = self.previous_main_engine
        main.browser = self.previous_browser
        db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _cooldown(self):
        main.engine.risk.record_failure(
            self.account_id, OperationKind.READ_HEAVY,
            XhsApiError("challenge", category="risk", status_code=429))

    def test_note_media_does_not_call_platform_during_cooldown(self):
        self._cooldown()
        with patch("app.platforms.xhs.XhsApiClient") as client_cls:
            result = asyncio.run(main.publish_note_media(
                self.account_id, "note-1", "token", "pc_feed"))
        self.assertTrue(result["skipped"])
        client_cls.assert_not_called()
```

Add the remaining endpoint cases explicitly:

```python
def test_profile_refresh_does_not_call_platform_during_cooldown(self):
    self._cooldown()
    with patch("app.main._enrich_account_profile") as enrich:
        result = asyncio.run(main.refresh_account_profile(self.account_id))
    self.assertTrue(result["skipped"])
    enrich.assert_not_called()

def test_published_list_does_not_call_platform_during_cooldown(self):
    self._cooldown()
    with patch("app.main._xhs_account_uid") as account_uid, \
            patch("app.browser.fetch_xhs_notes") as fetch_notes, \
            patch("app.browser.fetch_creator_published") as fetch_creator:
        result = asyncio.run(main.list_published_notes(self.account_id))
    self.assertTrue(result["skipped"])
    account_uid.assert_not_called()
    fetch_notes.assert_not_called()
    fetch_creator.assert_not_called()

def test_note_comments_do_not_call_platform_during_cooldown(self):
    self._cooldown()
    with patch("app.platforms.xhs.XhsApiClient") as client_cls:
        result = asyncio.run(main.publish_note_comments(
            self.account_id, "note-1", "token", "pc_feed"))
    self.assertTrue(result["skipped"])
    client_cls.assert_not_called()
```

- [ ] **Step 2: Run the new suite and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_risk_api_gates.py -q
```

Expected: current endpoints call their platform dependencies or raise HTTP errors instead of returning a deferred result.

- [ ] **Step 3: Add the shared account-read wrapper**

Add to `app/main.py`:

```python
async def _run_account_read(account_id: int, kind: OperationKind, key: str,
                            operation, *, empty_result):
    if engine is None:
        raise HTTPException(503, "引擎未就绪")
    payload, error = await engine.guarded_read_pair(
        account_id, kind, key, operation, empty_result=empty_result)
    if str(error or "").startswith("risk_deferred:"):
        return None, {
            "ok": True,
            "skipped": True,
            "reason": str(error).split(":", 1)[-1],
        }
    return (payload, None) if not error else (payload, str(error))
```

- [ ] **Step 4: Wrap each endpoint as one logical read**

Move all platform calls and their existing fallback calls into one inner coroutine per endpoint. Use `READ_LIGHT` for profile refresh and published-list reads, and `READ_HEAVY` for note media/comments. Return the deferred dictionary immediately; otherwise preserve each endpoint's current successful response and HTTP 400 mapping.

Ensure `_enrich_account_profile()` does not create a second guard when called from the guarded refresh endpoint; login enrichment remains inside the existing login workflow.

- [ ] **Step 5: Verify GREEN and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_risk_api_gates.py tests/test_identity_mode.py tests/test_xhs_risk_classification.py -q
git add app/main.py tests/test_risk_api_gates.py
git commit -m "fix: gate account-bound platform reads"
```

Expected: the protected endpoints skip all platform calls during cooldown and existing identity/login tests remain green.

---

### Task 4: Clean Risk Data on Account Deletion

**Files:**
- Modify: `app/main.py:68-79,660-678`
- Test: `tests/test_risk_api_gates.py`

**Interfaces:**
- Consumes: `AccountRiskState`, `RiskEvent`, `del_account(account_id)`.
- Produces: idempotent transactional cleanup of account-bound risk rows.

- [ ] **Step 1: Add a failing ID-reuse regression test**

Add:

```python
def test_delete_account_removes_risk_rows_before_id_reuse(self):
    main.engine.risk.record_failure(
        self.account_id, OperationKind.COMMENT,
        XhsApiError("challenge", category="risk", status_code=429))
    asyncio.run(main.del_account(self.account_id))

    with db.get_session() as session:
        self.assertIsNone(session.get(AccountRiskState, self.account_id))
        self.assertEqual(session.exec(select(RiskEvent).where(
            RiskEvent.account_id == self.account_id)).all(), [])
        replacement = DouyinAccount(nickname="replacement")
        session.add(replacement)
        session.commit()
        session.refresh(replacement)
        replacement_id = replacement.id

    decision = main.engine.risk.preflight(
        replacement_id, OperationKind.READ_LIGHT)
    self.assertTrue(decision.allowed)
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_risk_api_gates.py::RiskApiGateTests::test_delete_account_removes_risk_rows_before_id_reuse -q
```

Expected: the old risk state remains and the replacement account is blocked.

- [ ] **Step 3: Delete risk rows in the account transaction**

Import `RiskEvent` in `app/main.py`. Before deleting `DouyinAccount`, delete its `AccountRiskState` and every selected `RiskEvent` with the same `account_id`, then commit once. Keep the existing profile-directory cleanup after the transaction.

- [ ] **Step 4: Verify GREEN and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_risk_api_gates.py -q
git add app/main.py tests/test_risk_api_gates.py
git commit -m "fix: remove risk state when deleting accounts"
```

Expected: risk rows are absent and an ID-reused account starts clean.

---

### Task 5: Prune Risk Events Daily and Remove Dead Helpers

**Files:**
- Modify: `app/engine/monitor.py:169-190,364-407,409-424`
- Test: `tests/test_risk_control.py`

**Interfaces:**
- Consumes: `RiskController.prune_events(now=...)`.
- Produces: `MonitorEngine._prune_risk_events_if_due(now=None) -> int`, called once per UTC day from `_loop()`.

- [ ] **Step 1: Add failing once-per-day maintenance tests**

Add:

```python
def test_risk_event_pruning_runs_at_most_once_per_day(self):
    engine = MonitorEngine(self.cfg, _BrowserStub())
    calls = []
    engine.risk.prune_events = lambda **kwargs: calls.append(kwargs["now"]) or 2
    day_one = datetime(2026, 8, 7, 1, 0, 0)
    day_two = datetime(2026, 8, 8, 1, 0, 0)
    engine._last_risk_prune_day = None
    self.assertEqual(engine._prune_risk_events_if_due(day_one), 2)
    self.assertEqual(engine._prune_risk_events_if_due(day_one + timedelta(hours=2)), 0)
    self.assertEqual(engine._prune_risk_events_if_due(day_two), 2)
    self.assertEqual(calls, [day_one, day_two])

def test_risk_event_pruning_failure_does_not_escape_scheduler(self):
    engine = MonitorEngine(self.cfg, _BrowserStub())
    engine._last_risk_prune_day = None
    engine.risk.prune_events = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("db"))
    self.assertEqual(
        engine._prune_risk_events_if_due(datetime(2026, 8, 7, 1, 0, 0)), 0)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_risk_control.py -q
```

Expected: `_prune_risk_events_if_due` does not exist.

- [ ] **Step 3: Implement daily maintenance**

Initialize `_last_risk_prune_day = datetime.utcnow().date()` in `MonitorEngine.__init__`. Implement the method so it marks the attempted UTC date before pruning, catches/logs exceptions, and returns the removed count or zero. Call it once at the beginning of each `_loop()` iteration.

Remove `_is_write_risk_error()` and `_pause_account_writes()` after confirming no call sites remain.

- [ ] **Step 4: Verify GREEN and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_risk_control.py tests/test_write_gates.py -q
git add app/engine/monitor.py tests/test_risk_control.py
git commit -m "fix: maintain risk event retention during runtime"
```

Expected: maintenance tests and write-gate regressions pass.

---

### Task 6: Full Verification and Final Review

**Files:**
- Review: `app/config.py`
- Review: `app/risk.py`
- Review: `app/engine/monitor.py`
- Review: `app/main.py`
- Review: `tests/test_risk_control.py`
- Review: `tests/test_xhs_risk_classification.py`
- Review: `tests/test_risk_api_gates.py`

**Interfaces:**
- Consumes: all fixes from Tasks 1-5.
- Produces: verified branch state and a concise residual-improvement report.

- [ ] **Step 1: Run all automated verification**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe selftest.py
.\.venv\Scripts\python.exe -m compileall -q app tests selftest.py
```

Expected: all commands exit zero with no test failures.

- [ ] **Step 2: Inspect the final diff and repository state**

```powershell
git diff origin/main...HEAD --check
git status --short --branch
git diff origin/main...HEAD --stat
```

Confirm there are no debug prints, temporary files, exception objects escaping API responses, or uncommitted changes.

- [ ] **Step 3: Re-run targeted behavioral reproductions**

Run the ID-reuse, nested-429, unknown-mode, and legacy-publish tests by exact node ID. Confirm each passes independently so the full-suite result is not masking shared state.

- [ ] **Step 4: Request code review**

Use `superpowers:requesting-code-review` to check the final diff against the written specification. Resolve any high- or medium-severity findings before reporting completion.
