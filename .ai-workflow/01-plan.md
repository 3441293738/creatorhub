# 01-plan · 通知渠道健壮性增强（notifier 小功能）

## 1. 背景与目标

`app/notifier/__init__.py` 是唯一负责 Bark / 钉钉 / Telegram 三渠道推送的模块，但它存在三个明显缺口：

1. **配置缺少前置校验**：`app/main.py` 的 `add_channel` 只校验 `type` 是否在 `CHANNEL_TYPES` 内（`app/main.py:6810`），`config` 里的 `key` / `webhook` / `bot_token` / `chat_id` 等必填字段完全不做校验，错误只能等到「发送/测试」时才以 `(False, "缺少 xxx")` 的运行时字符串暴露，且前端无法区分「配置错误」与「发送失败」。
2. **钉钉加签不可单测**：`_send_dingtalk` 把 `hmac` + `base64` + `quote_plus` 全部内联在异步函数里（`app/notifier/__init__.py:40-44`），`time.time()` 直接写在函数中，签名逻辑无法确定性单测。
3. **批量发送结果被吞掉**：`notify_all` 逐个 `await send_one(...)` 后丢弃返回值（`app/notifier/__init__.py:82-85`），调用方（`app/engine/monitor.py` 多处）无法感知部分渠道失败。

目标：在不触碰抓取、发布、登录、风控任何核心流程的前提下，为该模块补齐**可 pytest 验证的纯函数能力**——配置校验、钉钉加签、逐渠道结果汇总。三者均为纯增量、向后兼容，1–2 小时可完成。

## 2. 验收标准

以下全部由新增的 `tests/test_notifier.py` 覆盖，`pytest tests/test_notifier.py` 应全绿，且不产生任何网络请求：

1. `validate_channel_config(ch_type, config)` 对合法配置返回 `[]`：
   - bark：含非空 `key`；
   - dingtalk：含 `http(s)://` 的 `webhook`；
   - telegram：同时含非空 `bot_token` 与 `chat_id`。
2. 对非法配置返回非空问题列表，且信息可读：
   - bark 缺 `key`；
   - dingtalk 缺 `webhook` 或 `webhook` 不是 http(s)；
   - telegram 缺 `bot_token` / `chat_id`，或 `api_base` 存在但不是 http(s)；
   - 未知渠道类型返回 `["未知渠道类型: xxx"]`。
3. `_dingtalk_sign(secret, timestamp_ms)` 输出与「`hmac.new(secret, f"{ts}\n{secret}", sha256)` → `base64` → `quote_plus`」独立重算结果逐字节一致；且结果不含裸 `+`、`/`、`=`（URL 安全）。
4. `_send_dingtalk` 继续使用 `_dingtalk_sign`，签名行为与改动前完全一致（用固定 `timestamp_ms` 的 mock 验证拼出的 webhook 尾部参数）。
5. `notify_all(channels, title, text)` 返回 `list[dict]`，每个元素为 `{"type", "ok", "detail"}`，与 `send_one` 返回值一一对应；单个渠道失败/抛异常不中断其它渠道（保持现有语义）。
6. 不新增第三方依赖，不修改 `send_one` 的对外签名与返回值结构（仍为 `(bool, str)`）。

## 3. 涉及文件

- **修改**：`app/notifier/__init__.py`
  - 新增纯函数：`_is_http_url`、`validate_channel_config`、`_dingtalk_sign`。
  - `_send_dingtalk` 内部改用 `_dingtalk_sign`（行为不变）。
  - `notify_all` 改为返回逐渠道结果列表。
- **新增**：`tests/test_notifier.py`
  - pytest 风格测试（异步场景用 `asyncio.run`，不引入 pytest-asyncio）。
- **只读参考（不改）**：
  - `app/main.py:6808-6853`（通知渠道 CRUD 与测试接口，用于确认当前校验缺口与接口约定）。
  - `tests/test_reporting.py`、`tests/test_web_status_labels.py`（现有测试风格与 `pytest.ini` 约定）。

## 4. 分步实现方案

### 第 1 步：新增 URL 校验纯函数

在 `app/notifier/__init__.py` 顶部（`TIMEOUT` 之后）新增：

```python
def _is_http_url(value: str) -> bool:
    parts = urllib.parse.urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)
```

说明：只判断协议与 host，不做网络访问。

### 第 2 步：新增配置校验函数

```python
def validate_channel_config(ch_type: str, config: dict | None) -> list[str]:
    """返回渠道配置的问题列表；空列表表示可用。纯函数，不触发网络。"""
    cfg = config or {}
    if ch_type not in CHANNEL_TYPES:
        return [f"未知渠道类型: {ch_type}"]

    errors: list[str] = []
    if ch_type == "bark":
        if not (cfg.get("key") or "").strip():
            errors.append("缺少 bark key")
        server = (cfg.get("server") or "").strip()
        if server and not _is_http_url(server):
            errors.append("bark server 必须是 http(s) 地址")
    elif ch_type == "dingtalk":
        webhook = (cfg.get("webhook") or "").strip()
        if not webhook:
            errors.append("缺少 webhook")
        elif not _is_http_url(webhook):
            errors.append("webhook 必须是 http(s) 地址")
    elif ch_type == "telegram":
        if not (cfg.get("bot_token") or "").strip():
            errors.append("缺少 bot_token")
        if not (cfg.get("chat_id") or "").strip():
            errors.append("缺少 chat_id")
        api = (cfg.get("api_base") or "").strip()
        if api and not _is_http_url(api):
            errors.append("api_base 必须是 http(s) 地址")
    return errors
```

### 第 3 步：抽取钉钉加签纯函数并替换内联实现

```python
def _dingtalk_sign(secret: str, timestamp_ms: str) -> str:
    """按钉钉加签规范返回 URL 编码后的 sign。"""
    message = f"{timestamp_ms}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest))
```

`_send_dingtalk` 中删除原来的三行内联（`ts` / `sign` / `webhook +=`），替换为：

```python
    ts = str(round(time.time() * 1000))
    webhook += f"&timestamp={ts}&sign={_dingtalk_sign(secret, ts)}"
```

注意：`quote_plus(bytes)` 与旧实现 `quote_plus(base64.b64encode(...))` 完全等价，保持既有签名结果不变。

### 第 4 步：`notify_all` 返回逐渠道结果

```python
async def notify_all(channels: list[dict], title: str, text: str) -> list[dict]:
    """channels: [{type, config(dict)} ...]。逐个发送，失败不影响其它。"""
    results: list[dict] = []
    for ch in channels:
        ch_type = ch.get("type")
        try:
            ok, detail = await send_one(ch_type, ch.get("config") or {}, title, text)
        except Exception as e:
            ok, detail = False, repr(e)
        results.append({"type": ch_type, "ok": ok, "detail": detail})
    return results
```

调用方 `monitor.py` 现有 `await notify_all(...)` 忽略返回值，不受影响；后续如需 UI 展示部分失败，可直接消费返回值（本次不接入）。

### 第 5 步：编写测试

新增 `tests/test_notifier.py`，见第 5 节测试计划。

## 5. 测试计划

文件 `tests/test_notifier.py`，全部为同步 pytest 函数；涉及异步的用 `asyncio.run(...)` 包装（仓库现有测试即此风格，见 `tests/test_editable_configs.py`）。

| 用例 | 验证点 |
|---|---|
| `test_validate_bark_valid_and_missing_key` | 合法 bark 返回 `[]`；缺 key 返回含「缺少 bark key」 |
| `test_validate_dingtalk_requires_http_webhook` | 合法 webhook 通过；缺 webhook / `ftp://...` / `javascript:...` 失败 |
| `test_validate_telegram_requires_token_chat_id_and_api_base` | 缺 token、缺 chat_id、非法 api_base 分别命中对应错误 |
| `test_validate_unknown_channel` | 未知类型返回 `["未知渠道类型: xxx"]` |
| `test_validate_config_accepts_none` | `config=None` 与 `{}` 行为一致，不抛异常 |
| `test_dingtalk_sign_matches_hmac_sha256` | 用 `hmac`/`base64`/`quote_plus` 独立重算，断言与 `_dingtalk_sign` 相等 |
| `test_dingtalk_sign_is_url_safe` | 结果不含裸 `+`、`/`、`=`，且可被 `unquote_plus` 还原为 base64 |
| `test_notify_all_returns_per_channel_results` | `unittest.mock.patch` 替换 `app.notifier.send_one` 为受控异步假函数，断言返回列表长度与逐项 `{type, ok, detail}` |
| `test_notify_all_continues_when_one_channel_fails` | 假函数对其中一个抛异常，断言仍返回全部结果且不中断 |
| `test_send_one_unknown_channel_no_network` | `send_one("unknown", ...)` 返回 `(False, ...)`，不触发网络 |
| `test_send_one_swallows_sender_exception` | 临时替换 `_SENDERS["telegram"]` 为抛异常的异步函数，断言 `send_one` 捕获并返回 `(False, repr(...))` |

（必选）`test_dingtalk_webhook_url_contains_timestamp_and_sign`：mock `httpx.AsyncClient` 捕获 POST URL，断言尾部含 `&timestamp=` 与 `&sign=`，且 sign 等于 `_dingtalk_sign` 的结果。

## 6. 风险与回滚点

- **签名行为漂移风险**：`_dingtalk_sign` 抽取后若与旧实现有细微差异，会影响钉钉推送。缓解：测试用 `test_dingtalk_sign_matches_hmac_sha256` 与必选 URL 集成断言把行为钉死；回滚点 = 将 `_send_dingtalk` 恢复为原三行内联。
- **`notify_all` 返回值变更**：属纯增量（旧代码忽略返回值），无调用方破坏；回滚点 = 恢复为不返回。
- **`validate_channel_config` 未接入 API**：本次只提供纯函数，不改 `main.py`，故不会改变线上行为；若后续接入需单独评审。
- **依赖/环境风险**：不新增依赖，测试仅用标准库 `hmac`/`base64`/`urllib`/`unittest.mock`，避免新环境问题。

## 7. 明确禁止改动范围

- **核心流程一律不碰**：`app/platforms/**`、`app/browser/**`、`app/engine/monitor.py`、`app/engine/collection.py`、`app/engine/downloader.py`、`app/engine/share_downloader.py`、`app/engine/compose.py`、`app/engine/im_receiver.py`、`app/risk.py`、`app/risk_admin.py`、`app/netfp.py`、`app/windowing.py`。
- **API/前端不碰**：`app/main.py`（含通知渠道 CRUD 与测试接口）、`app/web/app.js`、`app/web/index.html` 本次不做任何改动；`validate_channel_config` 是否接入 API 留待后续。
- **配置与工程文件不碰**：`config.example.yaml`、`config.yaml`、`requirements.txt`、`package.json`、`pytest.ini`、`start.cmd` / `start.sh`、`README.md`。
- **不新增依赖、不迁移数据库、不改模型**：`app/models.py`、`app/db.py` 保持原样。
- 允许改动的仅两处：`app/notifier/__init__.py` 与新增 `tests/test_notifier.py`。

---

**结论**：我选择给 `app/notifier/__init__.py` 增加「配置校验 + 钉钉加签纯函数 + `notify_all` 逐渠道结果」三项小改进。理由是它完全落在通知辅助层，不触碰抓取/发布/登录/风控任何核心流程，且目前该模块零测试、`notify_all` 结果被静默丢弃是真实缺口。三项改动都是纯增量、可回滚，用标准库即可完成确定性 pytest 验证，约 1–2 小时即可交付。
