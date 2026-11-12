# CreatorHub AI 助手（第一版）

## 范围

- 全局右下角浮窗：在任何页面进行数据问答、手动日报分析、具体任务诊断。没有独立侧栏页面；旧 `#agent` 链接会打开浮窗。
- 分析范围可跟随当前页面平台、选择指定平台或全平台；模型配置与历史记录全局共用。当前采集任务可显式带入诊断，不自动发送分析请求。
- 收起、切换页面或平台不会清空未提交输入、配置编辑或当前分析；已提交分析始终保留其原范围标识。收起只隐藏窗口，不停止后台分析，需停止时使用“停止分析”。
- 聊天式交互：用户消息气泡、连续追问、固定底部输入框、顶部渠道 / 本次模型选择、新建对话、按会话归组和搜索的历史。回复支持基础 Markdown 与复制，执行记录、证据和待确认工作流放在消息内。
- 模型：OpenAI 兼容 Chat Completions、OpenAI Responses、Anthropic Messages；基础地址和模型标识可配置，本地无鉴权接口可不填 Key。仅文本模式不要求工具调用。
- 模型目录：填写基础地址与 Key 后即可按需获取上游模型并选择，无需先保存、启用或填写模型名称；目录外的 ID 仍可手动输入。获取目录不调用生成接口，也不保存输入。
- 最多保存 20 个模型渠道，各自拥有地址、协议、密钥、模型目录和高级设置。渠道列表支持新增、编辑、使用和确认删除；助手页可直接切换当前渠道。新增或编辑非当前渠道不会自动切换，未保存草稿会阻止切换，当前渠道须先切换后再删除。
- 每个渠道的表单依次为显示名称、API 地址、API 协议、API 密钥、模型目录。「添加模型」支持 ID 与可选显示名称，保存后保留目录；「取消」恢复上次保存的表单。启用开关、工具调用、调用限制和 MCP 开关随渠道保存与切换。列表显示配置状态，不伪装成已通过连通测试。
- 获取成功后先打开「选择模型」弹窗：搜索、勾选、全选搜索结果或取消全选，然后显式「更新目录」。首次默认勾选全部返回项；已有目录时保留原选择与别名，未出现在本次上游结果的手动条目也可继续勾选。取消或 Esc 不修改目录。模型行支持编辑显示名称、配置 ID、移除；当前使用的模型独立选择，移除快捷项不清空可直接使用的 ID。
- 工作流：生成最多五步的关键词采集 / 作品监控草稿；查看参数后明确确认创建。视频号支持只读分析，暂不支持生成创建计划。
- MCP：可选的官方 Python SDK stdio 桥接，只开放五个本地查询工具，不调用付费模型、不加载数据库或启动浏览器。
- 不包含定时 AI 巡检 / 日报发送、自动修复配置、发布 / 私信 / 删除、源码修改、多模态上传、外部 MCP Client。这些不是首版的隐式执行能力。

用户操作与 MCP 安装说明见 `guide/index.html#ai-agent`。模型与 MCP 开关均默认关闭；与原有自动评论 AI 配置独立。

## 结构

| 文件 | 职责 |
|---|---|
| `config.py` | 独立配置，API Key 写入后不回显；更换目标地址须重新填写或清除 Key |
| `providers.py` | 有界 HTTP 调用，保留原生工具结果与推理上下文，不自动重试或切换供应商 |
| `streaming.py` | 有界 SSE 解码、三种协议的增量文本 / 推理 / 工具参数组装，完成事件校验 |
| `catalog.py` | 只读获取上游模型目录、协议鉴权、有界解析与不含密钥的错误提示 |
| `tools.py` | 白名单字段查询、平台 / 账号范围约束、规则初判、统计口径 |
| `privacy.py` | 凭据字段、URL 查询串、Cookie / Authorization 头和已知 Key 脱敏 |
| `runtime.py` | 后台分析、证据、步骤、用量、停止、重启恢复、请求去重 |
| `conversations.py` | 服务端会话链、范围 / 渠道隔离、有界历史上下文 |
| `telemetry.py` | 公开推理内容提取、token 明细归一化、用量 / 费用来源与缺失状态 |
| `workflows.py` | 严格计划 schema、不可变内容摘要、明确确认、逐步创建和恢复 |
| `api.py` | 工作台 API、只读 SSE 运行订阅，默认本机与同源访问边界不变 |
| `mcp_server.py` | 独立 stdio 进程，通过只读 API 连接已运行的本地工作台 |

主程序仍是单进程本地工作台；后台运行句柄在内存，状态与证据持久化在 SQLite。
`AgentRun` / `AgentWorkflow` 由现有 `init_db()` 创建；旧业务表和数据不被重建。
Windows 安装包的 AI 助手无新增 Python 依赖。MCP SDK 放在独立环境，避免其 Starlette 等依赖升级影响主程序。

全局浮窗位于 `frontend/agent.jsx`；消息、基础 Markdown 和复制操作位于 `frontend/agent-chat.jsx`；模型选择弹窗和配置行位于 `frontend/agent-models.jsx`。选择弹窗由 Radix Dialog 管理焦点与 Esc，100 条一页；配置行 20 条一页。合并上游与已有条目后仍最多勾选 1000 项，保存由服务端再次校验。
渠道列表和删除确认位于 `frontend/agent-channels.jsx`。正在运行的分析使用启动时的配置快照；切换不改变已提交分析，也不会触发自动重试或向其他渠道发送业务数据。

## 聊天与连续追问

- 输入框固定在底部，消息单独滚动；`Enter` 发送、`Shift+Enter` 换行，中文输入法选词时的回车不发送。发送成功后清空当前输入，分析期间可先写下一条草稿；停止分析不清空它。
- 顶部「本次模型」来自当前渠道保存的模型目录（含目录外的默认 ID），仅覆盖这次请求，不改写渠道默认配置。同渠道换模型可继续追问；切换渠道则提示下一条新建对话，不把旧渠道历史转发到新渠道。
- 「新建对话」只清空当前显示的会话，不删除历史、不调用模型，保留未发送草稿。运行中的分析先等待完成或显式停止；「编辑后重试」只把问题放回输入框，不自动重新计费调用。
- 「历史记录」读取最近 50 条分析，按对话归组，可搜索标题和最近问题；打开后显示该分支最近 30 轮，支持继续追问。更早记录仍在本地数据库中。打开、收起或刷新历史不会重新提交模型请求。
- 连续追问由 `parent_run_id` 显式关联。后端只读取自身保存的链，不接受客户端拼接的历史消息。每次仍重新查询当前证据，历史只携带过去的问题、完成的答复、状态与简短错误；不重放原始工具结果、计划审批或任何写操作。
- 每次最多参考之前 8 轮、约 20,000 字符；每条旧问题最多 2,000 字符、旧答复最多 6,000 字符。历史截断会标记给模型，旧证据编号只属于原消息；历史不是实时数据，也不代表用户已确认执行。
- 平台、账号、模式或诊断任务变化时，界面提示下一条新建对话。服务端进一步校验渠道、协议、目标地址、路径模式与密钥摘要，拒绝跨范围续接；同一渠道内显式换模型可以续接。
- 旧版记录可查看；缺少范围指纹的旧记录会开启新对话，不隐式转发旧历史。启动时现有 SQLite 迁移补充会话列与索引，保留旧消息。
- Markdown 仅支持标题、列表、粗体、行内代码和代码块；内容按 React 文本渲染，不解析 HTML、不加载外部图片、不执行代码。回复和代码复制只由用户点击触发。

交互参考 [Hermes Desktop 的聊天 / 会话布局](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/desktop.md) 与 [会话恢复说明](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/sessions.md)。本项目独立实现这些交互，不引入 Hermes 后端或其终端执行能力。

## 仅流式请求

- 三种推理协议及连通测试统一发送 `stream=true`，只接受 SSE；没有非流式开关、JSON 推理回退或错误后重新生成。模型目录、配置、历史和创建运行回执仍是普通 JSON API，不是推理回退。
- Chat Completions 使用 `delta`，并请求 `stream_options.include_usage=true`；Responses 读取正文、推理摘要增量与最终 `response`；Anthropic 组装内容块、thinking、签名和工具参数。浏览器通过 `frontend/agent-stream.js` 订阅本地 SSE，按真实上游片段刷新正文与公开推理，不用打字动画伪装流式。
- 运行快照包含脱敏后的 `partial_answer`、推理、步骤与用量；正文完成前标明「生成中」，成功后由完整答复替换，不追加两次。后端最多约每 150 毫秒持久化预览，SSE 约每 200 毫秒检查并只推送变更；10 秒心跳，无代理缓冲。
- 工具参数在内存中累积；收到协议终止事件、完整 JSON 和最终参数校验通过后才执行工具。EOF、超时、错误事件或截断不会把未完成参数当作调用，工作流仍须用户确认。
- 上游断流 / 停止 / 服务重启保留已收到的正文和用量，明确标记未完成，不自动重试模型。停止分析会取消并关闭上游连接。浏览器连接断开只重连同一记录的只读 GET 订阅，既不轮询 JSON，也不重新发送生成 POST。
- 推理请求仍受单次配置超时和整项 180 秒限制；流式传输总量最多 8 MB，单事件及最终组装结构最多 1 MB，正文最多 32,000 字符。UTF-8 分包、CRLF、注释心跳、多行 data 均独立解码。

协议参考：[OpenAI 流式响应](https://developers.openai.com/api/docs/guides/streaming-responses)、[Chat Completions 流事件](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)、[Responses 流事件](https://developers.openai.com/api/reference/resources/responses/streaming-events)、[Anthropic 流事件与累计 usage](https://platform.claude.com/docs/en/build-with-claude/streaming)。

## 推理内容、token 与消耗

- 每条消息常显输入、输出和总消耗 token，不再只放在证据折叠区。包含本条消息内所有模型调用（包括工具往返）；逐次明细在「执行记录与证据」。底部另显示当前加载消息的已知总消耗，最多 30 轮，不冒充全部历史账单。
- 仅提取上游公开返回的文本：Chat Completions 的 `message.reasoning_content`（兼容 `reasoning` 字符串）、Responses 的 `reasoning.summary[].text`、Anthropic 的 `thinking.thinking`。单次最多 16,000 字符、整条消息最多 48,000 字符，先脱敏再截断，界面提示截断；不把工具日志当作思考内容。
- `signature`、`encrypted_content` 和 `redacted_thinking.data` 只在原有内存工具往返中原样传回同一服务，不落入公开推理记录、API 输出或界面。仅有受保护内容时给出说明；没有推理文本不代表模型没有进行推理，也不保证能得到完整内部思考过程。
- 渠道「高级设置 → 推理 / 思考请求」默认跟随上游（不添加额外参数）。按模型能力显式选择：Responses 请求 `reasoning.summary=auto`；Anthropic 选择 `adaptive` 或旧版 `enabled` + 预算；DeepSeek 类兼容接口可选 `thinking.type=enabled`。这些参数并非所有模型 / 网关都支持，不按模型名猜测，也不遇错自动重试。预算至少 1,024 且小于输出上限；输出上限包含推理与答复。修改协议时重置为上游默认。
- 正文、公开推理和用量随真实流式事件更新。用量可能仅在结束时返回；同一次调用的累计 usage 覆盖此前快照，不重复累加，收到终止事件后才标为完整，工具往返的不同调用再求和。未收到 usage 时不按文字长度估算实时 token。
- OpenAI / Responses 的输入已包含缓存命中，输出已包含 `reasoning_tokens`；这些明细不再叠加。Anthropic 的总输入按 `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` 归一化。推理 token 数量来自返回明细，不用思考文字长度推算。
- 缺失 / 非法 usage 显示 `—`，部分模型调用缺失时明确标记「仅已返回部分」；上游真实返回的 0 则保留为 0。旧记录标记缺少完整明细。截断、解析失败、停止或重启时，已经返回的用量仍保留；未收到回执的调用消耗保持未知，不断言免费。
- 金额与 token 数量不同。三种协议没有统一的费用字段；目前仅接受网关 `usage.cost={amount,currency}` 或 `usage.cost` 数值配合 `usage.cost_currency` 的明确币种回报，标注「上游回报，账单为准」。未返回币种、余额单位或非法金额不猜测美元；未知显示未返回，不显示费用 0。金额缺失则标记部分，不同币种分列，不自动换汇、不读取账户余额、不按未知渠道价格估算金额。
- `usage_json`、`reasoning_json` 和 `partial_answer` 通过已有 SQLite 增列迁移保存；恢复历史不调用模型，也不把未完成预览、推理记录或费用回放进下一次模型上下文。

字段依据：[OpenAI reasoning summary 与 usage](https://developers.openai.com/api/docs/guides/reasoning)、[DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)、[Anthropic thinking](https://platform.claude.com/docs/en/build-with-claude/thinking) 与 [缓存输入计数](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)。

## API

统一前缀 `/api/agent`：

- `GET/PUT /settings`：兼容接口，读取 / 替换当前使用渠道的配置。空 Key 表示保留；`clear_api_key=true` 明确清除。
- `GET/POST /channels`：获取渠道列表 / 新增渠道；返回的配置不包含密钥原文。
- `PUT/DELETE /channels/{id}`：编辑 / 删除指定渠道。新渠道不继承旧密钥；空 Key 仅保留该 ID 原有的密钥，改变地址或协议仍需重新填写或清除。
- `POST /channels/{id}/activate`、`POST /channels/{id}/test`：显式切换 / 测试指定渠道。测试非当前渠道不会切换，也不发送业务数据。
- `POST /models`：`{provider, base_url, api_key?, clear_api_key?, channel_id?}`，返回 `{models: [{id, name}], count, truncated, skipped}`。使用当前表单连接信息；不保存配置、不创建分析记录。`channel_id` 为空字符串表示未保存的新渠道，不复用任何旧密钥；省略时兼容旧版，读取当前渠道。
- `POST /test`：使用已保存配置进行最小文本连通测试，不携带业务数据。
- `GET /context?platform=...`：本地摘要、账号、最近任务，不调用模型。
- `POST /runs`：`{request_id, prompt, mode, platform, account_id?, task_type?, task_id?, parent_run_id?, channel_id?, model?}`，返回 202。省略新增字段保留旧版单轮行为；`model` 仅覆盖本次请求，`parent_run_id` 显式续接同范围、同渠道的历史。
- `GET /runs`、`GET /runs/{id}`、`POST /runs/{id}/cancel`：历史、证据、停止。
- `GET /runs/{id}/events`：只读 SSE 订阅，先返回当前 `snapshot`，变化时继续推送；终态快照后返回 `done`。重连获取持久化的最新状态，不触发模型调用。不存在返回 404，访问边界与其他本地 API 一致。
- `GET /runs/{id}/conversation`：该消息所在分支最近 30 轮（含证据 / 工作流）、`has_more` 和 `context_limit`；不调用模型。
- `POST /workflows/{id}/apply`：`{confirmed: true, plan_hash}`。没有通用执行 / SQL / shell 工具。
- `GET /tools`、`POST /tools/{name}`：MCP 开关开启后可用的只读工具；没有工作流提交工具。

`mode` 为 `chat / diagnose / workflow`；诊断必须指定 `task_type`（`collections / publishes / comments / actions / monitors`）和数字任务 ID。
工具字段不能扩大已选择的平台 / 账号范围。每次分析单独收集当前证据；只有显式提供合法 `parent_run_id` 才会带上有界、同范围的历史问答。

配置新增可选 `display_name`（最多 80 字符）与 `models: [{id, name}]`（最多 1000 条、ID 唯一）。它们不改变当前 `model` 的语义，目录外 ID 仍可直接使用。旧 JSON 配置缺少这些字段时使用空值，不重建数据库或覆盖旧密钥。
旧 `agent.settings.v1` 只读加载为默认渠道；首次显式修改时写入 `agent.channels.v1`，同一事务维护旧键为当前渠道的兼容视图。仅打开界面不会改写数据库。所有渠道的密钥都仅写入、不回显；单进程修改操作串行化。

## 获取上游模型

1. 在全局浮窗的「模型设置」中填写显示名称、API 地址（支持 `https://HOST` 或 `https://HOST/v1` 等基础地址，不带 `/models` 或查询参数）、协议与该服务的 API 密钥，点击「获取可用模型」。
2. 获取后在「选择模型」中搜索、勾选所需项并点击「更新目录」。逐行编辑显示名称，点击配置按钮编辑 ID 或名称；再从「当前使用的模型」中选择本次使用的模型。也可通过「添加模型」手动录入，或直接填写目录外的模型 ID。最后「保存配置」才会持久化。目录不代表工具调用或文本分析能力已经验证。
3. 密钥留空时只复用相同地址、相同协议的已保存密钥。更换地址或协议时须重新输入或明确清除，避免把旧密钥发送到新服务。

后端请求基础地址下的 `GET /models`。OpenAI 兼容 / Responses 使用 `Authorization: Bearer`；Anthropic 使用 `x-api-key` 与 `anthropic-version`。Responses 不附加查询参数；兼容协议与 Anthropic 使用 `limit=1000`。兼容协议仅在查询返回 400 / 422 时去掉 `limit` 再请求一次；401、403、429、网络错误不自动重试，不跟随重定向或分页链接。

只填域名时优先保留网关的 `/models` 路径；仅在上游返回 404 / 405 时尝试同域名的 `/v1/models`。已有 `/v1` 或自定义前缀时不猜测其他路径，不叠加版本前缀。响应中的 `path_prefix_added` 表示是否触发目录路径兼容；页面会提示它。目录探测不自动修改配置或尝试付费生成请求。

## 推理路径与响应诊断

`api_path_mode=auto` 为默认值：纯域名地址的推理请求补 `/v1`，然后按协议拼接 `/messages`、`/responses` 或 `/chat/completions`；已有路径前缀原样保留。自定义根路径服务可在高级设置选择 `exact`（原样拼接）。规则在发请求前确定，不通过重复付费生成请求猜测路径，也不自动切换协议。

已复现旧问题：某网关根路径 `/messages` 返回 HTTP 200 HTML 页面，旧代码直接解析 JSON，导致笼统的“模型响应格式异常”。现在区分 HTML 页面、非 SSE 响应、无结束事件、事件 JSON 无效、协议结构不匹配、流中错误和非法工具参数。错误不回显上游原文或凭据；解析失败不污染后续消息历史。

推理仅发送 `stream=true`；若网关返回整段 JSON，会提示需要 SSE，不自动回退或重新付费调用。HTTP 403 单独提示模型/接口权限和上游访问策略；成功获取模型目录不代表推理权限、流式或工具能力已通过验证。协议结构依据 [Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create) 和 [OpenAI 流式响应说明](https://developers.openai.com/api/docs/guides/streaming-responses)。

单次发现总限时 20 秒、响应上限 2 MB、最多处理 1000 条；结果仅保留 ID 与名称，去重、忽略无效条目并提示是否截断。获取仅更新候选列表；用户确认勾选后才更新表单目录，仍需显式保存。空结果或失败不覆盖当前模型 ID；刷新失败保留上次成功列表，修改地址、协议或密钥会清空旧目录。移出目录仅删除快捷选项，不清空当前使用的 ID。

`401` 表示上游鉴权未通过，界面会区分「未携带 Key」与「已携带 Key」：前者需填写密钥，后者检查密钥是否有效、是否属于该服务、协议是否匹配。错误不回显上游原文、请求头或密钥，不把鉴权失败误报为「没有模型」。

更新代码后若模型目录提示本地 `404 / Not Found`，先重启后端，再强制刷新网页：旧进程可能没有加载 `POST /api/agent/models`，仅刷新静态前端不会注册新路由。上游目录在路径兼容尝试后仍为 404，则由本地接口返回 502 并显示「上游返回 404」；这时检查自定义路径及上游是否开放目录，而非直接判断密钥无效。

实现对照：[Sub2API 网关路由](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/server/routes/gateway.go) 同时开放根路径与 `/v1` 模型目录；[鉴权中间件](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/server/middleware/api_key_auth.go) 接受 Bearer 与 `x-api-key`。[Hermes 提供商基础类](https://github.com/NousResearch/hermes-agent/blob/main/providers/base.py) 按提供商获取目录，[Anthropic 实现](https://github.com/NousResearch/hermes-agent/blob/main/plugins/model-providers/anthropic/__init__.py) 使用原生鉴权与独立路径处理。本项目借鉴其协议分离思路，不按模型名称前缀擅自过滤目录，继续保留响应大小、限时和不跨域转发密钥的边界。

协议参考：[OpenAI 模型列表](https://developers.openai.com/api/reference/python/resources/models/methods/list)、[OpenAI 鉴权](https://developers.openai.com/api/reference/overview)、[Anthropic 模型列表](https://platform.claude.com/docs/en/api/models/list)。

## 可靠性与数据含义

- 同时最多两项分析；每项总计 180 秒、最多 12 次模型工具调用；单轮最多 4 次，1–6 轮；证据预算约 64k 字符。单个最终响应结构最多 1 MB。
- 使用同一 `request_id` 重试相同请求会返回原记录，改动内容返回 409。前端响应丢失后保留该编号，重连不自动提交。
- 重启不自动重新调用模型。`queued/running` 标为 `interrupted`；创建中的计划保留结果，等待再次确认。
- 计划确认绑定不可变 SHA-256。每步用稳定的 `Idempotency-Key` 调用原创建接口；提交回执与新任务在同一事务。
- 成功创建的前序任务不自动回滚。再次确认仅继续未完成步骤，或通过原回执补齐进度；不是重试平台发布 / 私信。
- AI 只提出建议；任务仍经过原执行引擎、风险控制和账号检查。
- 任务统计按 UTC 创建时间筛选，再统计当前状态，不是历史状态转移或真实成功率。
- 作品排名来自现有已同步记录；部分平台旧数据用 0 表示未获取播放量。每日账号快照按账号时区记日。结果显式附带这些限制。
- 模型仅接收白名单字段与脱敏文本；不读取 Cookie / storage_state / 原始响应 / 任意本地文件。自由文本仍视为不可信数据。
- 模型 Key 与现有 AppSetting 一样存放在本地数据库，并非新增的加密凭据保险库；保护数据库和备份。分析历史也可能包含业务信息。
- 用量取自服务响应；未提供时为未知，真实零值与缺失分别显示。只展示明确币种的上游费用回报，不根据未知价格猜测实际金额。

## 扩展方式

新业务查询首先加入 `tools.py`，写白名单字段 / 范围 / 分页测试，然后选择是否加入 MCP 的独立 allowlist。
新写操作先定义严格参数模型、确认文案、幂等事务和现有业务服务执行器，再加入工作流；不直接暴露路由全集。
新模型协议实现 `ModelSession` 的 request / parse / add_results 与 `NativeStream` 的增量组装，测试完整终止事件、公开预览和工具往返；不要靠替换 URL 假定协议完全兼容。

参考：[OpenAI 工具调用](https://developers.openai.com/api/docs/guides/function-calling)、[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create)、[MCP 官方 SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0)。

## 验证

```powershell
.venv\Scripts\python.exe -m pytest tests/test_agent.py tests/test_agent_catalog.py tests/test_agent_provider_settings.py tests/test_agent_channels.py tests/test_agent_response_formats.py tests/test_agent_conversations.py tests/test_agent_telemetry.py tests/test_agent_streaming.py tests/test_access_and_submissions.py tests/test_monitor_strategy.py tests/test_task_queue.py -q
npm run build:ui
$env:CREATORHUB_RUN_LOCAL_CDP = '1'
.venv\Scripts\python.exe -m pytest tests/test_agent_browser.py tests/test_agent_chat_browser.py tests/test_agent_telemetry_browser.py tests/test_agent_streaming_browser.py -q
```

浏览器测试只启动隔离的本地演示站点和合成 API；不启动业务引擎、不访问真实账号、不调用模型。
可选 `test_agent_mcp_sdk.py` 通过官方 SDK 客户端验证 stdio 发现 / 调用 / 写工具拒绝；只连接本地 HTTP fixture。
