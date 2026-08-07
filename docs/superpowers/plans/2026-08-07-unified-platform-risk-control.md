# Unified Platform Risk Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve CreatorHub's existing platform features while routing account activity through shared limits, network-group serialization, risk cooldowns, and gradual recovery.

**Architecture:** Add a focused `RiskController` that owns persistent platform-risk state, operation counters, platform-error classification, and network-group locks. `MonitorEngine` remains the business orchestrator and calls the controller before and after platform operations; existing task tables remain the source of task state while new risk tables provide cross-feature accounting.

**Tech Stack:** Python 3.10+, FastAPI, asyncio, SQLModel/SQLite, Playwright, unittest/pytest.

## Global Constraints

- Keep login, monitoring, downloading, publishing, commenting, following, direct messaging, and health monitoring available.
- Deferred work remains queued; risk controls never silently discard a task.
- Existing account profiles and fingerprints do not migrate automatically.
- Same-account and same-network operations are serialized.
- Existing signed read paths remain available but share the new budgets and cooldowns.
- All implementation follows red-green-refactor: each production change starts with a failing test.
- Work occurs on `feat/platform-risk-control` in `.worktrees/platform-risk-control`.

## File Structure

- Create `app/risk.py`: operation types, decisions, error classification, persistent counters, cooldowns, and network locks.
- Modify `app/config.py`: typed `RiskControlConfig` and YAML loading.
- Modify `app/models.py`: `identity_mode`, `AccountRiskState`, `RiskEvent`, and `PublishTask.done_at`.
- Modify `app/profiles.py`: unique usable-proxy assignment and native identity initialization.
- Modify `app/browser/identity.py`: carry `identity_mode` through `Identity`.
- Modify `app/browser/manager.py`: native mode omits spoofed browser properties while legacy mode is unchanged.
- Modify `app/engine/monitor.py`: operation guards, write gates, result recording, deferral, read classification, and network serialization.
- Modify `app/platforms/xhs/client.py`: structured platform-error categories.
- Modify `app/main.py`: new-account native identity, publish run-now through the same gate, proxy probing, and risk status fields.
- Modify `config.example.yaml` and `README.md`: conservative risk-control settings and behavior.
- Create `tests/test_risk_control.py`: controller, cooldown, timezone, recovery, and network-lock tests.
- Extend `tests/test_write_gates.py`: publish gates and cross-feature write spacing.
- Create `tests/test_proxy_assignment.py`: unique allocation and bad-proxy exclusion.
- Create `tests/test_identity_mode.py`: legacy stability and native launch behavior.

---

### Task 1: Persistent risk primitives and configuration

**Files:**
- Create: `app/risk.py`
- Modify: `app/config.py`
- Modify: `app/models.py`
- Create: `tests/test_risk_control.py`

**Interfaces:**
- Produces: `OperationKind`, `RiskCategory`, `RiskDecision`, `FailureDecision`, `RiskController`, `network_key()`, and `classify_platform_error()`.
- Consumes: `get_session()`, `DouyinAccount`, `AccountRiskState`, `RiskEvent`, and `Config.risk_control`.

- [ ] **Step 1: Write failing model/config tests**

Add tests that instantiate `RiskControlConfig`, initialize a temporary SQLite database, and assert that `AccountRiskState`, `RiskEvent`, `PublishTask.done_at`, and `DouyinAccount.identity_mode` exist with conservative defaults.

```python
def test_risk_config_uses_conservative_defaults(self):
    cfg = RiskControlConfig()
    self.assertTrue(cfg.enabled)
    self.assertEqual(cfg.mode, "conservative")
    self.assertEqual(cfg.network_group_concurrency, 1)
    self.assertEqual(cfg.publish_daily_cap, 3)

def test_new_risk_models_persist(self):
    with db.get_session() as session:
        account = DouyinAccount(nickname="fixture")
        session.add(account)
        session.commit()
        session.refresh(account)
        session.add(AccountRiskState(account_id=account.id, risk_level=2))
        session.add(RiskEvent(account_id=account.id, operation_kind="comment",
                              outcome="risk", signal="http_429"))
        session.commit()
        self.assertEqual(session.get(AccountRiskState, account.id).risk_level, 2)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pytest tests/test_risk_control.py -q
```

Expected: import/attribute failures for missing risk models and configuration.

- [ ] **Step 3: Add configuration and models**

Implement `RiskControlConfig` with explicit conservative values:

```python
@dataclass
class RiskControlConfig:
    enabled: bool = True
    mode: str = "conservative"
    network_group_concurrency: int = 1
    read_light_gap_seconds: int = 20
    read_heavy_gap_seconds: int = 60
    shared_write_gap_seconds: int = 300
    comment_min_gap_seconds: int = 600
    comment_hourly_cap: int = 3
    comment_daily_cap: int = 10
    social_min_gap_seconds: int = 900
    social_hourly_cap: int = 2
    social_daily_cap: int = 8
    dm_min_gap_seconds: int = 900
    dm_hourly_cap: int = 2
    dm_daily_cap: int = 8
    publish_min_gap_seconds: int = 7200
    publish_hourly_cap: int = 1
    publish_daily_cap: int = 3
    combined_action_hourly_cap: int = 3
    combined_action_daily_cap: int = 10
    cooldown_steps_seconds: list[int] = field(
        default_factory=lambda: [1800, 7200, 21600, 86400])
    recovery_successes: int = 3
    recovery_probe_gap_seconds: int = 600
    event_retention_days: int = 30
```

Add `risk_control: RiskControlConfig` to `Config` and load the top-level YAML group using known dataclass fields only.

Add SQLModel tables with indexed account/time fields. `PublishTask.done_at` is nullable. `DouyinAccount.identity_mode` defaults to `legacy` so SQLite migration preserves existing identities.

- [ ] **Step 4: Implement controller decision types and utilities**

`app/risk.py` must expose these exact signatures:

```python
class OperationKind(str, Enum):
    READ_LIGHT = "read_light"
    READ_HEAVY = "read_heavy"
    DOWNLOAD = "download"
    PUBLISH = "publish"
    COMMENT = "comment"
    SOCIAL = "social"
    DM = "dm"
    LOGIN = "login"

@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = ""
    next_allowed_at: datetime | None = None
    signal: str = ""

def network_key(proxy: str) -> str: ...
def classify_platform_error(error: object, status_code: int | None = None,
                            payload: object = None) -> tuple[RiskCategory, str]: ...
```

Implement `RiskController.preflight()`, `record_success()`, `record_failure()`, `clear_account()`, `prune_events()`, and `network_guard()` against real SQLite sessions. Use `zoneinfo.ZoneInfo` for local-day calculations and a controller-level lock to make preflight/accounting atomic in the single-process runtime.

- [ ] **Step 5: Add RED tests for decisions, cooldown, timezone, and recovery**

Tests cover:

- HTTP `403/429/461/471`, captcha text, and empty successful payload classify as risk.
- Explicit login-expired text classifies as auth.
- Proxy/DNS/timeout text classifies as network.
- Risk levels yield 30-minute, 2-hour, 6-hour, and 24-hour cooldowns.
- A local Asia/Shanghai daily bucket starts at the correct UTC instant.
- Three spaced light-read successes reduce one risk level.
- A write during cooldown is denied with `next_allowed_at`.

- [ ] **Step 6: Run focused tests until GREEN**

Run:

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pytest tests/test_risk_control.py -q
```

Expected: all risk-control tests pass.

- [ ] **Step 7: Commit Task 1**

```powershell
git add app/risk.py app/config.py app/models.py tests/test_risk_control.py
git commit -m "feat: add persistent platform risk controller"
```

---

### Task 2: Correct proxy allocation and health classification

**Files:**
- Modify: `app/profiles.py`
- Modify: `app/main.py`
- Create: `tests/test_proxy_assignment.py`

**Interfaces:**
- Consumes: `network_key()` for shared-network behavior in later tasks.
- Produces: `assign_proxy_from_pool()` returning only an unoccupied usable proxy or `""`.

- [ ] **Step 1: Write failing proxy-allocation tests**

```python
def test_auto_assignment_never_reuses_an_occupied_proxy(self):
    # two enabled proxies, both occupied -> empty result
    self.assertEqual(assign_proxy_from_pool(session, self.cfg), "")

def test_auto_assignment_skips_bad_disabled_and_auth_error_entries(self):
    # only the unknown/ok enabled row is returned
    self.assertEqual(assign_proxy_from_pool(session, self.cfg), usable_url)

def test_proxy_probe_rejects_407(self):
    self.assertFalse(asyncio.run(_probe_status_ok(407)))
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pytest tests/test_proxy_assignment.py -q
```

Expected: occupied proxies are currently reused and no status helper exists.

- [ ] **Step 3: Implement unique allocation and strict status handling**

- Normalize pool URLs before comparison.
- Exclude database rows with `enabled=False` or `status` in `bad`, `auth_error`, `blocked`.
- Exclude every URL already assigned to a `DouyinAccount`.
- Return `""` when no candidate remains.
- Add `_proxy_status_ok(status_code)` returning true only for `200 <= code < 400`.
- Map `407` to `auth_error`; map platform `403/429` to `blocked`; keep transport failures as `bad`.
- Keep manual duplicate proxy binding available.

- [ ] **Step 4: Run proxy and existing tests until GREEN**

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pytest tests/test_proxy_assignment.py tests/test_browser_cookie_bridge.py -q
```

- [ ] **Step 5: Commit Task 2**

```powershell
git add app/profiles.py app/main.py tests/test_proxy_assignment.py
git commit -m "fix: keep automatic proxy assignments unique"
```

---

### Task 3: Apply one write gate to comments, actions, and publishing

**Files:**
- Modify: `app/engine/monitor.py`
- Modify: `app/main.py`
- Extend: `tests/test_write_gates.py`

**Interfaces:**
- Consumes: `RiskController.preflight/record_success/record_failure`.
- Produces: `_operation_guard()`, `_defer_*()` helpers, and risk-aware publish execution.

- [ ] **Step 1: Write failing publish and cross-feature tests**

Add tests that create real `PublishTask`, `CommentTask`, and `AccountActionTask` rows with a browser stub.

Required assertions:

- Publish outside the active window remains `pending`.
- Publish with `write_paused_until` remains `pending`.
- Publish on an invalid account fails without opening a browser.
- Publish using a bad proxy remains queued for recovery.
- A successful comment prevents an immediate social, DM, or publish operation through the shared write gap.
- `run-now` calls the same gate and does not force status to an executable state after denial.
- A risk response returns comment/action/publish tasks to `pending` with a future `scheduled_at`.

- [ ] **Step 2: Run write-gate tests and verify RED**

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pytest tests/test_write_gates.py -q
```

Expected: publish currently bypasses the account pause, active window, proxy status, and quotas.

- [ ] **Step 3: Instantiate and integrate `RiskController`**

In `MonitorEngine.__init__` create `self.risk = RiskController(cfg)`.

Replace `_account_guard` with an operation-aware guard that always acquires locks in this order:

```python
@asynccontextmanager
async def _operation_guard(self, account_id, kind: OperationKind,
                           fallback_key: str = ""):
    account = self._load_account(account_id)
    key = f"acc:{account_id}" if account_id else (fallback_key or "anon")
    async with self._active_sem:
        async with self.risk.network_guard(account):
            async with self.browser.lock_for(key):
                yield account
```

Keep `_account_guard` as a compatibility wrapper that delegates to `READ_LIGHT` until all call sites are converted.

- [ ] **Step 4: Add risk-aware write preflight and deferral**

Inside each account lock:

- Comments call `preflight(account, COMMENT)` before changing status to `doing`.
- Follow/unfollow calls `preflight(account, SOCIAL)`; DM calls `preflight(account, DM)`.
- Publishing validates account existence/status/proxy, then calls `preflight(account, PUBLISH)` before changing status to `publishing`.
- A denied task stays `pending`; set `scheduled_at` to the later of its current schedule and `decision.next_allowed_at`.
- Success records one `RiskEvent(success)` and completion timestamp.
- Risk/auth/network failures use `record_failure()` and defer appropriately.
- Business failures preserve existing `failed` behavior.

- [ ] **Step 5: Keep legacy gates as stricter compatibility checks**

Existing configured comment/action caps remain effective when lower than the conservative hard ceilings. Convert hardcoded UTC day/hour helpers to account-timezone-aware controller queries. `_pause_account_writes()` delegates to `RiskController.record_failure()` for risk signals and keeps compatibility fields synchronized.

- [ ] **Step 6: Run write-focused tests until GREEN**

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pytest tests/test_write_gates.py tests/test_editable_configs.py -q
```

- [ ] **Step 7: Commit Task 3**

```powershell
git add app/engine/monitor.py app/main.py tests/test_write_gates.py
git commit -m "feat: apply unified risk gates to platform writes"
```

---

### Task 4: Serialize network groups and budget read operations

**Files:**
- Modify: `app/engine/monitor.py`
- Modify: `app/main.py`
- Extend: `tests/test_risk_control.py`

**Interfaces:**
- Consumes: `_operation_guard()` and controller read decisions.
- Produces: account-rotating due queues and risk-aware read deferral.

- [ ] **Step 1: Write failing concurrency and read-budget tests**

Tests use real `asyncio` tasks and controller locks:

- Two direct accounts never overlap inside `network_guard`.
- Two accounts with distinct proxy URLs may overlap up to the global limit.
- Two accounts sharing one proxy URL never overlap.
- A heavy read performed for an account prevents another heavy read before 60 seconds.
- A risk cooldown blocks heavy reads but permits the single light recovery probe after cooldown.
- Due monitor IDs are interleaved by account rather than grouped into a same-account burst.

- [ ] **Step 2: Run tests and verify RED**

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pytest tests/test_risk_control.py -q
```

- [ ] **Step 3: Convert read entry points**

Apply `READ_LIGHT` to account checks, profile refreshes, and work-list reads. Apply `READ_HEAVY` to comment watches, danmaku watches, DM history, work health, and comment-rule discovery. Monitor scans that fetch a creator feed use `READ_LIGHT`; their nested comment/detail loops stop when the controller enters cooldown.

When preflight denies a read, return a normal skipped result with reason and next time. Do not modify `last_scan_at`, because no platform read occurred; the controller prevents repeated network access while the scheduler may cheaply re-evaluate the task.

- [ ] **Step 4: Rotate due work by account**

Build a stable round-robin list keyed by `account_id`/fallback key. In each engine cycle run at most one heavy read per account and leave remaining due work for the next cycle. Replace bulk same-account `asyncio.gather` behavior with this ordered queue while retaining cross-account concurrency through semaphores.

- [ ] **Step 5: Record read outcomes**

Successful logical reads call `record_success`. Explicit risk results call `record_failure`. Network errors use short deferral without increasing platform risk level. Business-empty results remain successful only when the platform response explicitly represents an empty collection; ambiguous empty responses classify as risk.

- [ ] **Step 6: Run read, monitor, and reporting tests until GREEN**

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pytest tests/test_risk_control.py tests/test_douyin_monitor.py tests/test_douyin_danmaku.py tests/test_monitor_metadata.py tests/test_reporting.py -q
```

- [ ] **Step 7: Commit Task 4**

```powershell
git add app/engine/monitor.py app/main.py tests/test_risk_control.py
git commit -m "feat: serialize shared network activity"
```

---

### Task 5: Preserve legacy identities and default new accounts to native mode

**Files:**
- Modify: `app/browser/identity.py`
- Modify: `app/browser/manager.py`
- Modify: `app/profiles.py`
- Modify: `app/main.py`
- Create: `tests/test_identity_mode.py`

**Interfaces:**
- Produces: `Identity.identity_mode` and native/legacy launch branches.
- Consumes: `DouyinAccount.identity_mode` from Task 1.

- [ ] **Step 1: Write failing identity-mode tests**

Required assertions:

- `Identity.from_account()` preserves explicit `legacy` and `native` modes.
- Existing fixture accounts default to `legacy` after migration.
- New login/cookie accounts are explicitly created with `identity_mode="native"`.
- Native launch kwargs omit `user_agent`, `geolocation`, and pre-authorized geolocation permissions.
- Native contexts do not call `set_extra_http_headers` or `add_init_script`.
- Legacy contexts continue applying the existing UA, Client Hints, geolocation, and fingerprint script.

- [ ] **Step 2: Run tests and verify RED**

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pytest tests/test_identity_mode.py -q
```

- [ ] **Step 3: Implement native mode**

Add `identity_mode: str = "legacy"` to `Identity`. In `_launch_persistent` build common kwargs first. Only legacy mode adds custom UA, geolocation, permissions, Client Hints, and `fingerprint_script`; native mode lets Chromium supply native values.

For fresh QR and cookie accounts, explicitly store `identity_mode="native"`. `ensure_identity()` must not generate a random cross-OS UA for native accounts; it still assigns stable profile path, viewport defaults, timezone, locale, and seed needed by legacy compatibility data.

- [ ] **Step 4: Run identity and login tests until GREEN**

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pytest tests/test_identity_mode.py tests/test_browser_cookie_bridge.py tests/test_channels_login.py tests/test_xhs_login_flow.py -q
```

- [ ] **Step 5: Commit Task 5**

```powershell
git add app/browser/identity.py app/browser/manager.py app/profiles.py app/main.py tests/test_identity_mode.py
git commit -m "feat: use native browser identity for new accounts"
```

---

### Task 6: Structured platform errors and crash-safe recovery

**Files:**
- Modify: `app/platforms/xhs/client.py`
- Modify: `app/engine/monitor.py`
- Modify: `app/main.py`
- Extend: `tests/test_write_gates.py`
- Extend: `tests/test_xhs_login_flow.py`

**Interfaces:**
- Produces: `XhsApiError.category`, `.status_code`, and `.signal`.
- Consumes: `classify_platform_error()` and task deferral helpers.

- [ ] **Step 1: Write failing error-classification tests**

Verify that `461/471` produce category `risk`, explicit login-expired responses produce `auth`, and account health checks do not mark a risk response as logged out. Add startup recovery tests that reset stale `doing`/`publishing` rows to `pending` with a short future schedule.

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pytest tests/test_xhs_login_flow.py tests/test_write_gates.py -q
```

- [ ] **Step 3: Implement structured errors and recovery**

Extend `XhsApiError` constructor with category/status/signal fields. `_unwrap` assigns them for known HTTP and response codes. `_check_accounts` handles risk by recording cooldown and leaving account status unchanged; only `auth` marks invalid.

At engine startup, recover stale transient task states:

- `CommentTask.doing -> pending`
- `AccountActionTask.doing -> pending`
- `PublishTask.publishing -> pending`

Set `scheduled_at` to current UTC plus 5 minutes so a restart does not trigger an immediate burst.

- [ ] **Step 4: Run focused and full tests until GREEN**

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pytest tests/test_xhs_login_flow.py tests/test_write_gates.py -q
```

- [ ] **Step 5: Commit Task 6**

```powershell
git add app/platforms/xhs/client.py app/engine/monitor.py app/main.py tests/test_xhs_login_flow.py tests/test_write_gates.py
git commit -m "fix: classify platform risk responses without relogin loops"
```

---

### Task 7: Configuration, documentation, and complete verification

**Files:**
- Modify: `config.example.yaml`
- Modify: `README.md`
- Modify: `selftest.py`
- Test: all files under `tests/`

**Interfaces:**
- Consumes: all previous task behavior.
- Produces: documented conservative defaults and a clean verified branch.

- [ ] **Step 1: Add configuration parsing regression test**

Extend `tests/test_risk_control.py` with a temporary YAML file that overrides one risk value and confirms unspecified values retain conservative defaults.

- [ ] **Step 2: Run it and verify RED if loading is incomplete**

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pytest tests/test_risk_control.py -q
```

- [ ] **Step 3: Document exact settings and behavior**

Add the full `risk_control` group to `config.example.yaml`. README states:

- same-exit accounts are serialized;
- immediate actions do not bypass cooldowns;
- deferred tasks stay queued;
- existing profiles stay legacy;
- new accounts use native identity;
- conservative hard ceilings apply when mode is conservative.

Update `selftest.py` to import `RiskController`, instantiate default config, and verify cooldown steps and network keys without network access.

- [ ] **Step 4: Run formatting/static verification**

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m compileall -q app creatorhub.py selftest.py
& 'C:\Program Files\Git\cmd\git.exe' diff --check
```

- [ ] **Step 5: Run the complete suite**

```powershell
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pytest -q
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' selftest.py
& 'D:\pyProjects\douyin\creatorhub\.venv\Scripts\python.exe' -m pip check
```

Expected: all tests pass, selftest passes, and pip reports no broken requirements.

- [ ] **Step 6: Commit Task 7**

```powershell
git add config.example.yaml README.md selftest.py tests app
git commit -m "docs: describe conservative platform risk controls"
```

- [ ] **Step 7: Review branch scope**

```powershell
git status --short
git log --oneline main..HEAD
git diff --stat main...HEAD
```

Expected: clean worktree and only platform-risk-control changes plus their tests and documentation.
