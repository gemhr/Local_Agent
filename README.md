# LocalAgent

LocalAgent 是一个本地桌面智能体项目。PyQt6 客户端通过 FastAPI 后端进行流式对话；后端组合本地或远程 LLM、RAG 知识库、SQLite Memory、本地工具，以及默认启用的 Coordinated Runtime。

> **安全提示**：API Key、Cookie、真实内网端点和用户私有绝对路径只能保存在环境变量或本机未提交配置中，不得写入仓库、日志、截图或运行证据。

## 1. 当前架构

```text
PyQt6 client (main.py)
  → POST /api/chat
FastAPI application (server.py::lifespan)
  → ChatService / AgentRouter
  → CoordinatedRuntimeFactory
  → planning / scheduler / parallel executor
  → model / tool / retrieval contracts
  → RuntimeEventChannel
  → SQLite Journal / observability / trace
  → text/plain streaming response
```

`server.py::lifespan()` 是唯一生产 Composition Root。应用级模型、Memory、Journal、可观测性、Trace、worker 与 shutdown 服务在进程生命周期内共享；每个请求创建独立的 Run scope、`AgentState`、Budget、事件 Channel 和取消源。

默认 `CHAT_RUNTIME_MODE=COORDINATED`。`LEGACY` 只作为显式回滚路径，修改配置并重启后才影响新请求；失败或已经开始的 Run 不会跨 Runtime fallback。

## 2. 当前能力

以下摘要来自当前源码、测试和 `docs/runtime/runtime_capability_matrix.md`，不是根据模块名推测。

### 2.1 已支持

- **动态多 Agent 规划**：`core_router` 可通过受约束的 Planner 生成计划；显式选择专业 Agent 时走确定性入口；多步结果通过受控 Synthesis 生成唯一 final。
- **调度与执行**：不可变 Plan、DAG 校验、Scheduler、受限并发执行、Fail-fast、资源并发控制和 step result 边界。
- **运行控制**：状态机、Run/Step 生命周期、Budget、Deadline、Timeout、Cancellation、客户端断连处理和主动取消 API。
- **模型策略**：基于 Profile 能力与成本元数据的选择、同 Profile Retry、受策略约束的候选模型 fallback、Circuit Breaker。模型能力不按 Qwen、DeepSeek 等名称推断。
- **Tool / Retrieval 合同**：类型化调用、预算与超时、幂等/副作用 evidence、安全输出限制、RAG 检索阶段事件和 provenance。
- **事件与持久证据**：Journal-first 事件发布、per-run 单调 sequence、单一 terminal、SQLite Journal v2（reader 兼容 v1/v2）。
- **最终交付**：`OutputGate` at-most-once；只有已交付 final 可以通过 write-once writer 写入业务 Memory。
- **可观测性**：结构化安全日志、进程内指标 recorder、Trace/span、健康快照和故障隔离。
- **生命周期**：Run Registry、worker tracking、admission gate、断连 drain、优雅关闭和保守资源关闭报告。
- **确定性 Fault Injection**：仅用于 `TEST_SCOPE` 或显式测试 seam，生产请求不能激活。

### 2.2 部分支持或未实现

| 能力 | 当前状态 | 边界 |
| --- | --- | --- |
| Snapshot | `PARTIALLY_SUPPORTED` | 默认关闭；显式启用后使用严格校验的 Snapshot v1 |
| Recovery | `PARTIALLY_SUPPORTED` | 只读 validation，不启动执行、不 replay、不写回 `AgentState` |
| 标准 SSE / WebSocket | `NOT_IMPLEMENTED` | 当前为自定义 `text/plain` 增量文本与 control line 协议 |
| 跨进程 Registry / Durable Execution | `NOT_IMPLEMENTED` | Registry、Circuit state 和 worker ownership 均为单进程 |
| 全系统 exactly-once | `NOT_IMPLEMENTED` | 只有局部 at-most-once、幂等与持久 evidence，不能提升为全系统保证 |
| 自动补偿 | `NOT_IMPLEMENTED` | Runtime 可记录补偿 evidence，但不自动执行通用补偿策略 |
| 生产 Chaos / Fault 激活 | `NOT_IMPLEMENTED` | 无 Settings、HTTP、Prompt 或 Tool 参数激活入口 |
| 生产指标 Exporter | `NOT_IMPLEMENTED` | 当前只有进程内 recorder/snapshot，不等于 Prometheus/Grafana 接入 |

完整状态和限制分别见 `docs/runtime/runtime_capability_matrix.md` 与 `docs/runtime/stage2_known_limitations_and_next_stage.md`。

## 3. 环境要求与安装

- Windows PowerShell。
- Python `>=3.12,<3.13`，当前应使用 Python 3.12 x64。
- `uv` 负责依赖和虚拟环境。
- 本地模型模式需要可被 `llama-cpp-python` 加载的 GGUF。
- RAG 需要本地 embedding 模型和可读写的 Chroma 目录。

在仓库根目录执行：

```powershell
py -3.12 --version
uv --version
uv sync
```

`pyproject.toml` 当前引用仓库所在机器上的 `llama-cpp-python` cp312 Windows wheel。`uv sync` 找不到该 wheel 时，应把它作为明确的依赖配置问题处理，不要静默下载、改写或提交另一个人的本机路径。

## 4. 启动

环境变量由不可变 `Settings.load()` 在进程启动时读取；修改后必须重启（无运行时 reload，也无需 `.env` 文件）。所有显式配置严格解析：非法 bool/int/float、未知 Profile/backend 在启动前 fail closed，不会静默纠正。解析与校验只由 `core/settings.py` 执行。

### 4.1 远程 OpenAI-compatible 后端

默认 `LOCAL_AGENT_LLM_BACKEND=remote`，因此启动前必须提供远程地址。地址可以是 API 根地址、以 `/v1` 结尾的地址，或完整 `/chat/completions` 地址。

```powershell
$env:LOCAL_AGENT_LLM_BACKEND="remote"
$env:LOCAL_AGENT_REMOTE_PROVIDER_KIND="openai_compatible"
$env:LOCAL_AGENT_REMOTE_API_BASE_URL="<provider-api-base-or-chat-completions-url>"
$env:LOCAL_AGENT_REMOTE_MODEL_NAME="<provider-model-id>"
$env:LOCAL_AGENT_REMOTE_VERIFY_TLS="1"
# Provider 需要鉴权时，在当前终端从本机 secret source 注入：
# $env:LOCAL_AGENT_REMOTE_API_KEY=...

uv run python server.py
```

`LOCAL_AGENT_REMOTE_PROVIDER_KIND=deepseek` 只用于显式选择对应的 thinking payload 合同；其他 OpenAI-compatible Provider 使用默认值。不要根据 URL 或模型名隐式判断 Provider。HTTPS 端点应显式设置 `LOCAL_AGENT_REMOTE_VERIFY_TLS=1`。

### 4.2 本地 GGUF 后端

```powershell
$env:LOCAL_AGENT_LLM_BACKEND="local"
$env:LOCAL_AGENT_MODEL_PATH="data\models\model.gguf"
$env:LOCAL_AGENT_MODEL_CONTEXT="4096"
$env:LOCAL_AGENT_MODEL_MAX_TOKENS="1024"
$env:LOCAL_AGENT_MODEL_GPU_LAYERS="0"

uv run python server.py
```

`LOCAL_AGENT_MODEL_GPU_LAYERS=0` 表示 CPU 推理；其他值必须与本机 llama.cpp 构建和资源能力匹配。

### 4.3 Hybrid 后端

`hybrid` 同时装配 `LOCAL_FAST` 和 `REMOTE_ADVANCED`，并由 Runtime policy 选择候选 Profile：

```powershell
$env:LOCAL_AGENT_LLM_BACKEND="hybrid"
$env:LOCAL_AGENT_MODEL_PATH="data\models\model.gguf"
$env:LOCAL_AGENT_REMOTE_API_BASE_URL="<provider-api-base-or-chat-completions-url>"
$env:LOCAL_AGENT_REMOTE_MODEL_NAME="<provider-model-id>"
$env:LOCAL_AGENT_REMOTE_VERIFY_TLS="1"

uv run python server.py
```

Hybrid 当前为 eager loading：本地或远程装配失败都会使启动失败，不会自动退化为 local-only 或 remote-only。运行中的模型 fallback 仍必须遵守 `ModelRoutingPolicy`；partial output、安全失败或证据发布失败等情形不会透明 fallback。

### 4.4 启动桌面端

后端启动后，在能访问相同配置的 PowerShell 中运行：

```powershell
uv run python main.py
```

后端默认监听 `http://127.0.0.1:8000`。桌面端默认使用 `LOCAL_AGENT_API_BASE_URL` 连接；跨机器部署时该地址必须是客户端实际可达的地址。

也可以仅启动后端：

```powershell
uv run uvicorn server:app --host 127.0.0.1 --port 8000
```

### 4.5 部署边界

当前唯一 certified 部署目标为 **Windows Native**（Windows 11 / Windows Server + Python 3.12 + uv），正式 server 入口是 `uv run python server.py`，每个部署实例**只能有一个 server application process**。禁止 `uvicorn --workers N`、gunicorn、multi-process Runtime。不支持 Docker / Compose / WSL2 部署。完整 Windows 部署、单进程合同、持久化数据、Secret、Proxy、Shutdown 与 Rollback 见 `docs/runtime/runtime_deployment_runbook.md`。

## 5. 关键配置

下表只列常用入口；完整字段、默认值、类型、安全分类和 failure behavior 以 `docs/runtime/runtime_configuration_reference.md` 为准。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `CHAT_RUNTIME_MODE` | `COORDINATED` | 可选 `COORDINATED` / `LEGACY`；非法值启动失败 |
| `LOCAL_AGENT_ENVIRONMENT_PROFILE` | `LOCAL` | 可选 `LOCAL` / `TEST` / `PRODUCTION`；未知或空显式值启动失败 |
| `LOCAL_AGENT_LLM_BACKEND` | `remote` | 可选 `local` / `remote` / `hybrid`；未知值启动失败 |
| `LOCAL_AGENT_MODEL_PROFILE` | `balanced` | `fast` / `balanced` / `deep`；未知值启动失败 |
| `LOCAL_AGENT_API_HOST` / `LOCAL_AGENT_API_PORT` | `127.0.0.1` / `8000` | 后端监听地址与端口 |
| `LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS` | `LOCAL`=项目根；`TEST`=空；`PRODUCTION`=必填 | `;` 分隔的 existing Windows 本地目录；`list_files` / `analyze_excel` 只允许读取 canonical root 内资源 |
| `LOCAL_AGENT_API_BASE_URL` | 由 host/port 派生 | 桌面端访问后端的根地址（client-only） |
| `LOCAL_AGENT_REMOTE_API_BASE_URL` | 空 | `remote` / `hybrid` 的 SERVER role 必填；PRODUCTION 必须 HTTPS |
| `LOCAL_AGENT_REMOTE_API_KEY` | 空 | 仅在非空时发送 Bearer Authorization |
| `LOCAL_AGENT_REMOTE_PROVIDER_KIND` | `openai_compatible` | 显式选择远程 payload 合同 |
| `LOCAL_AGENT_REMOTE_VERIFY_TLS` | Profile 默认：`LOCAL=0`、`TEST=1`、`PRODUCTION=1` | 严格布尔（`1`/`0`/`true`/`false`）；PRODUCTION 不可显式关闭 |
| `LOCAL_AGENT_REMOTE_TRUST_ENV` | Profile 默认：`LOCAL=1`、`TEST=0`、`PRODUCTION=0` | 严格布尔；是否让远程 model Session 继承系统 proxy |
| `LOCAL_AGENT_CLIENT_TRUST_ENV` | `1`（所有 Profile 一致） | 严格布尔；是否让 Desktop Client → LocalAgent Server Session 继承系统 proxy；与 `LOCAL_AGENT_REMOTE_TRUST_ENV` 独立 |
| `LOCAL_AGENT_EVENT_JOURNAL_DB_PATH` | `data/database/runtime_event_journal.db` | Coordinated Runtime SQLite Journal |
| `LOCAL_AGENT_SNAPSHOT_ENABLED` | `false` | 严格布尔值；启用 Snapshot 与 Recovery validation |
| `LOCAL_AGENT_MEMORY_DB_PATH` | `data/database/agent_memory.db` | 业务 Memory SQLite 路径 |
| `LOCAL_AGENT_CHROMA_DIR` | `chroma_db` | Chroma 持久化目录 |
| `LOCAL_AGENT_KB_REQUIRED` | Profile 默认：`LOCAL=0`、`TEST=0`、`PRODUCTION=1` | PRODUCTION 默认 KB 失败阻止启动；显式 `false` 才允许 degraded |
| `LOCAL_AGENT_EMBEDDING_MODEL_PATH` | `data/models/bge-large-zh-v1.5` | 本地 embedding 模型目录 |
| `LOCAL_AGENT_BLOCKING_MAX_WORKERS` / `LOCAL_AGENT_BLOCKING_MAX_PENDING_TASKS` | `4` / `8` | 三个 lifespan 有界 executor 的统一容量 |
| `LOCAL_AGENT_EVENT_CHANNEL_CAPACITY` | `32` | per-run RuntimeEventChannel 容量 |
| `LOCAL_AGENT_PLANNING_TIMEOUT_SECONDS` | `15.0` | planner 独立超时 |
| `RUNTIME_DISCONNECT_GRACE_SECONDS` | `0.75` | 客户端断连后的有界 drain 时间 |
| `RUNTIME_SHUTDOWN_GRACE_SECONDS` | `5.0` | shutdown Run drain 时间 |

远程 HTTP 的 `requests.Session` 由 `LOCAL_AGENT_REMOTE_TRUST_ENV` 显式控制是否继承进程系统 proxy：为 True 时使用 operator 批准的受控代理，Test/Production 默认 False 不继承宿主 proxy；项目不记录 proxy URL 或凭据。Desktop Client 的 `requests.Session` 由 `LOCAL_AGENT_CLIENT_TRUST_ENV` 独立控制（默认 `True` 继承系统 proxy，保持既有行为）；两个 transport scope 完全分离。`LOCAL_AGENT_OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS` 已标记 DEPRECATED（无行为），replacement 为 `RUNTIME_COMPONENT_CLOSE_TIMEOUT_SECONDS`。

## 6. API 速查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/chat` | `text/plain` 流式聊天；请求含 `agent_id`、`query`、可选 `file_path` / `run_id`；响应头返回 `X-Run-Id` |
| `POST` | `/api/runtime/runs/{run_id}/cancel` | 幂等请求取消 active Run |
| `GET` | `/api/history/{agent_id}?limit=10&offset=0` | 分页读取 Agent 历史 |
| `GET` | `/api/search?keyword=...` | 搜索持久化消息 |
| `GET` | `/api/memory` | 获取 Memory 管理数据 |
| `DELETE` | `/api/memory` | 删除指定 `message_ids` 或使用 `delete_all` 清空 |

聊天请求示例：

```powershell
$body = @{
  agent_id = "core_router"
  query = "请总结当前任务"
  run_id = [guid]::NewGuid().ToString()
} | ConvertTo-Json

Invoke-WebRequest `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/chat" `
  -ContentType "application/json" `
  -Body $body
```

当前协议不是标准 SSE；客户端必须按项目的纯文本增量与 control line 合同解析，不能假设 `data:` framing。

## 7. Agent、工具与知识库

允许作为聊天入口的 Agent：

- `core_router`：通用回答和受控规划。
- `data_analyst`：CSV / Excel 分析。
- `code_expert`：代码审查、问题排查与架构分析。
- `knowledge_expert`：基于本地知识库回答；知识库不可用或证据不足时 fail closed。

`synthesis_agent` 是 Runtime 内部 Synthesis 身份，不允许作为 API entry。

已注册本地工具：

- `list_files`
- `analyze_excel`
- `get_system_status`
- `complex_workflow_simulator`

这些工具可能读取本机路径或模拟副作用边界。只向可信用户开放服务，并将模型生成的路径、参数和外部文档视为不可信输入。

本地知识库脚本：

```powershell
uv run python scripts/bootstrap_local_kb.py
uv run python scripts/query_local_kb.py "检索问题"
```

## 8. 数据与安全边界

- `.env*`（示例模板除外）、模型、wheel、数据库、日志、Chroma 数据和知识库业务数据不得提交。
- Runtime Event、Journal、Snapshot、Report、Metric、Span 和结构化日志只保存 allowlist 安全事实或 digest，不保存 Prompt、Tool 原始参数/结果、Provider 原始异常、路径或密钥。
- 正常聊天 Wire 会承载面向用户的输出，Memory 和知识库有各自的业务持久化边界；“Runtime 安全投影不保存正文”不等于“任何业务面都不保存正文”。
- 不得手工修改 Runtime SQLite row、digest、event sequence 或 terminal 事实。
- Fault Injection 只能从测试或显式 operation seam 注入，生产配置和 API 没有启用入口。
- File Tool 调用按 `Tool Governance -> ResourceAuthorizationService -> ToolExecutionService` 顺序执行；相对路径、UNC、device/extended path、越界、nonexistent 与类型不匹配均在业务访问前拒绝。
- `PRODUCTION` 仅认证同机 loopback Desktop Client + Server：`LOCAL_AGENT_API_HOST` 与 `LOCAL_AGENT_API_BASE_URL` 必须使用 numeric loopback（IPv4 loopback 或 `::1`）。当前无 authenticated human IAM、inbound TLS、request-size limit 或完整 Sandbox；LOCAL/TEST 的非 loopback 配置只属于开发边界。

## 9. 测试与验证

```powershell
# 定向测试
uv run python -m pytest tests/<target>.py -q

# 验证测试可收集
uv run python -m pytest --collect-only -q

# 完整测试
uv run python -m pytest -q

# 语法与 Diff 检查
uv run python -m compileall main.py server.py core tests
git diff --check
```

Release Gate 必须由当前测试和 `tests/_runtime_release_gate.py` 重新派生，不能读取旧 Markdown 中的 PASS。Gate 是 code-level 证据，不等于真实模型、Embedding、Vector Store、网络、容量、Soak 或渗透测试已经完成。

## 10. 常见问题

### remote / hybrid 启动失败并提示缺少远程地址

设置非空的 `LOCAL_AGENT_REMOTE_API_BASE_URL`，然后重启后端。不要把真实内网地址写回仓库。

### local / hybrid 无法加载模型

检查 `LOCAL_AGENT_MODEL_PATH`、GGUF 格式、Python/llama-cpp ABI、内存/显存和 GPU layers。Hybrid 不会因本地加载失败自动退化为 remote-only。

### 桌面端无法连接后端

确认后端已启动，并使 `LOCAL_AGENT_API_BASE_URL` 与客户端实际可达的监听地址一致。跨机器访问时不能继续使用客户端本机的 `127.0.0.1`。

### `uv sync` 找不到本地 wheel

确认 `pyproject.toml` 当前引用的 `llama-cpp-python` wheel 存在，并匹配 `cp312` 与 `win_amd64`。对该引用的修改属于依赖变更，应单独审查。

### Snapshot 已启用但 Run 没有自动恢复

这是当前合同：Snapshot 提供版本化持久证据，Recovery 只进行 validation。项目尚未实现 Recovery execution、Replay 或 step result rehydration。

## 11. 正式文档

- 架构与合同：`docs/runtime/runtime_architecture_v1.md`
- Owner 边界：`docs/runtime/runtime_owner_matrix.md`
- 能力状态：`docs/runtime/runtime_capability_matrix.md`
- 配置参考：`docs/runtime/runtime_configuration_reference.md`
- 安全边界：`docs/runtime/runtime_security_boundary.md`
- 错误码：`docs/runtime/runtime_error_code_catalog.md`
- 运维与恢复：`docs/runtime/runtime_operations_runbook.md`、`docs/runtime/runtime_recovery_runbook.md`
- 部署：`docs/runtime/runtime_deployment_runbook.md`（Windows Native 单进程部署合同）
- Release Gate：`docs/runtime/runtime_release_gate.md`、`docs/runtime/runtime_release_checklist.md`
- 已知限制：`docs/runtime/stage2_known_limitations_and_next_stage.md`
