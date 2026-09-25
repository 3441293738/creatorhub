# 03-review · 代码复审（第二轮 · 只读）

复审对象：`app/notifier/__init__.py`（修改）、`tests/test_notifier.py`（新增），对照 `.ai-workflow/01-plan.md` 与第一轮 03-review 的 M1/M2/低危项。
复审方式：仅静态阅读，未运行任何命令、未修改任何代码。

## 整体结论

**通过**（可合并）。第一轮 M1、M2 及低危项 L1–L4 均已按意见修复并同步方案；仅剩两处非阻塞残留（见下「仍存在的问题」）。

---

## 第一轮问题逐项复核

### M1. 钉钉时间戳 `round` vs `int` 漂移 —— 通过
- 实现保持 `round`：`app/notifier/__init__.py:83` 仍为 `ts = str(round(time.time() * 1000))`。
- 方案已同步为 `round`：`01-plan.md:103` 为 `ts = str(round(time.time() * 1000))`，与实现一致。
- 方案第 3 步不再要求 `int`，实现/方案/测试三处语义一致（测试用 `fixed_ms / 1000` 打补丁后 `round` 回整数，见 `tests/test_notifier.py:110`）。通过。

### M2. mock httpx 的 `_send_dingtalk` 集成测试 —— 通过
- `tests/test_notifier.py:81-119` 新增 `test_dingtalk_webhook_url_contains_timestamp_and_sign`：
  - 用 `patch("app.notifier.time.time", return_value=fixed_ms / 1000)` 固定时间戳；
  - 用 `patch("app.notifier.httpx.AsyncClient", FakeClient)` mock 客户端，捕获 POST URL；
  - 断言 `&timestamp={fixed_ms}` 在 URL 中，且 `&sign=` 等于 `_dingtalk_sign(secret, str(fixed_ms))` 的独立重算结果。
- 满足验收标准 4 与方案第 5 节「必选」项。通过。

### L1. `test_dingtalk_sign_is_url_safe` 断言偏弱 —— 通过
- `tests/test_notifier.py:70-78` 已强化：除不含裸 `+`/`/`/`=` 外，新增 `base64.b64decode(unquote_plus(sign))` 并与独立 `hmac` digest 逐字节比较。通过。

### L2. `validate_channel_config` 分支覆盖缺口 —— 通过
- 新增 `test_validate_bark_invalid_server`（`tests/test_notifier.py:29-33`），覆盖 bark 非法 `server`；
- `test_validate_dingtalk_requires_http_webhook` 增加 `http://` 合法 webhook 断言（`tests/test_notifier.py:20-22`）；
- `test_validate_telegram_requires_token_chat_id_and_api_base` 增加合法 `api_base` 通过路径（`tests/test_notifier.py:39-41`）。
- 三个缺口均已补齐。通过。

### L3. `notify_all` 实现与方案第 4 步不一致 —— 通过
- 方案第 4 步已更新为带 `try/except` 的实现（`01-plan.md:111-123`），与 `app/notifier/__init__.py:122-132` 一致。通过。

### L4. `_dingtalk_sign` 类型标注与方案不一致 —— 通过
- 方案第 3 步已统一为 `timestamp_ms: str`（`01-plan.md:93`），与实现 `app/notifier/__init__.py:58` 及测试传参一致。通过。

---

## 仍存在的问题（均不阻塞合并）

### R1. 方案第 6 节仍将 URL 集成断言标为「可选」，与第 5 节「必选」矛盾
- 文件：`01-plan.md:153`
- 现状：第 5 节表格（`01-plan.md:149`）已把 `test_dingtalk_webhook_url_contains_timestamp_and_sign` 标为「必选」，但第 6 节风险缓解仍写「与（可选）URL 集成断言把行为钉死」。
- 影响：纯文档措辞不一致，无代码影响；M2 本身已实现并通过。
- 建议：将 `01-plan.md:153` 的「（可选）」改为「必选」，或删去括注，消除遗留矛盾。

### R2. 工作区仍有未跟踪的 `debug.log`
- 文件：工作区根目录 `debug.log`（untracked，本次仍存在）
- 影响：非代码改动，不破坏逻辑；若被误提交会污染仓库。
- 建议：确认其为本地调试产物，加入 `.gitignore` 或在合并前清理（本复审未做任何改动）。

---

## 逐项检查结论（第二轮）

1. **是否越界改动方案禁止的文件**：通过。仍仅 `app/notifier/__init__.py`（修改）与 `tests/test_notifier.py`（新增）；核心流程、API/前端、配置与工程文件未改动。（另见 R2 的 `debug.log` 提示。）
2. **钉钉签名抽取后是否与原逻辑一致**：通过。算法一致，且时间戳已统一为 `round`，实现/方案/测试一致（M1）。
3. **`validate_channel_config` 边界与返回信息**：通过。bark/dingtalk/telegram 的必填与可选 http(s) 校验、`config=None` 等价均已覆盖（L1/L2）。
4. **`notify_all` 返回逐渠道结果是否破坏现有调用方语义**：通过。实现与方案第 4 步一致（L3），调用方忽略返回值不受影响。
5. **测试是否有遗漏或脆弱断言**：通过。已补 mock httpx 的集成测试（M2），并强化 URL 安全断言（L1）。剩余仅 R1 方案措辞与 R2 工作区文件两项非阻塞残留。
