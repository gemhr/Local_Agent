# LocalAgent

LocalAgent 是一个本地桌面智能体项目：PyQt6 客户端通过 FastAPI 后端进行流式对话，后端包含本地/远程 LLM、RAG 知识库、SQLite Memory、本地工具和多 Agent 编排。阶段二 Runtime 已在知识专家最终回答路径接入本地轻量与远程高级模型的确定性选择策略。

> **安全提示**：请使用环境变量或本机未提交的启动脚本保存 API Key、Cookie 和本地绝对路径。不要将这些值写入仓库、README、日志或截图。

## 1. 运行架构

```text
PyQt6（main.py）
  → HTTP 流式请求
FastAPI（server.py）
  → ChatService / AgentRouter
  → Memory、RAG、Tool、Agent 编排
  → LOCAL_FAST / REMOTE_ADVANCED 模型调用
```

后端聊天接口为 `POST /api/chat`，返回 `text/event-stream` 增量文本；桌面端默认使用 `LOCAL_AGENT_API_BASE_URL + /api/chat` 连接后端。

## 2. 环境要求

- Python `>=3.12,<3.13`（见 `pyproject.toml`）。
- 本地模型模式需要可供 `llama-cpp-python` 加载的 GGUF 模型。
- 知识库模式需要本地 embedding 模型、Chroma 持久化目录和可用的依赖。
- 本项目当前引用 Windows 本地 `llama-cpp-python` wheel；请使用 Python 3.12 x64 和匹配的 Windows wheel。

## 3. 安装

本项目统一使用 **Windows PowerShell + uv**；不要手动创建 venv、执行 `source`，或使用裸 `python server.py`。请在项目根目录执行：

```powershell
Set-Location D:\PythonProject\Local_Agent
py -3.12 --version
uv --version
uv sync
```

`uv sync` 会创建并管理 `.venv`。当前 `pyproject.toml` 引用了 Windows 本机的 `llama_cpp_python-0.2.90-cp312-cp312-win_amd64.whl`；请确保该 wheel 与 Python 3.12 x64 匹配且路径可用。不要修改或提交真实 API Key。

## 4. 快速启动

先启动后端，再启动桌面端。**本节所有命令均为 Windows PowerShell，并通过 `uv run` 启动。** 环境变量只在当前 PowerShell 窗口有效；新开桌面端窗口时，需要重新设置相同的环境变量。

### 4.1 模型部署方案与参数原则

项目当前约定以下三种模型方案。`LOCAL_FAST` 固定对应本地 Qwen 7B GGUF；远程服务均通过 OpenAI-compatible Chat Completions 接口装配为 `REMOTE_ADVANCED`。模型选择策略不会根据 Qwen 或 DeepSeek 名称判断能力，而是使用 Settings 装配的 Profile。

| 使用位置 | 实际模型与访问方式 | 推荐 backend | 关键参数原则 |
| --- | --- | --- | --- |
| 本地 | Qwen 7B GGUF，经 `llama-cpp-python` 加载 | `local` | 保持较小窗口和输出，适合简单、短上下文请求。 |
| 公司网络 | 公司内部 IP 部署的 Qwen 27B，OpenAI-compatible，无 API Key | `remote` | 使用实际 IP/端口和服务端注册的模型名；API Key 留空；窗口值必须与公司部署值一致。 |
| 家中网络 | DeepSeek 官方 `v4-flash`，OpenAI-compatible，需要 API Key | `remote` | 使用 HTTPS、API Key 和官方控制台确认的模型 ID；可采用更大的 context、输出和 timeout，但不得超过 Provider 实际限制。 |

> `LOCAL_AGENT_REMOTE_CONTEXT_WINDOW` 是模型选择的能力声明，不是请求参数；虚报过大会让策略选择无法实际执行的远程模型。应以公司部署配置或 DeepSeek 官方控制台的实际窗口为准。

### 4.2 local：本地 Qwen 7B GGUF

```powershell
$env:LOCAL_AGENT_LLM_BACKEND="local"
$env:LOCAL_AGENT_MODEL_PROFILE="balanced"
$env:LOCAL_AGENT_MODEL_PATH="D:\PythonProject\Local_Agent\data\models\qwen2.5-7b-instruct-q4_k_m.gguf"
$env:LOCAL_AGENT_MODEL_CONTEXT="4096"
$env:LOCAL_AGENT_MODEL_MAX_TOKENS="1024"
$env:LOCAL_AGENT_MODEL_THREADS="10"
$env:LOCAL_AGENT_MODEL_GPU_LAYERS="0"

uv run python server.py
# 新 PowerShell 窗口中重新设置上述变量后：
uv run python main.py
```

`LOCAL_AGENT_MODEL_GPU_LAYERS=0` 表示 CPU 推理；如本机已正确配置 llama.cpp GPU wheel，可按显存容量提高该值。Qwen 7B 的实际 GGUF 文件名和盘符可不同，但 `LOCAL_AGENT_MODEL_PATH` 必须指向存在的 `.gguf` 文件。

### 4.3 公司：内部 IP 的 Qwen 27B（无 API Key）

以下示例假定公司服务使用 HTTP、地址为 `http://10.0.0.20:8000`，且注册模型名为 `qwen-27b`；请替换成公司实际地址、端口和模型 ID。为空的 API Key 会使客户端**不发送** `Authorization` 请求头。

```powershell
$env:LOCAL_AGENT_LLM_BACKEND="remote"
$env:LOCAL_AGENT_MODEL_PROFILE="balanced"
$env:LOCAL_AGENT_REMOTE_API_BASE_URL="http://10.0.0.20:8000/v1"
$env:LOCAL_AGENT_REMOTE_API_KEY=""
$env:LOCAL_AGENT_REMOTE_MODEL_NAME="qwen-27b"
$env:LOCAL_AGENT_REMOTE_CONTEXT_WINDOW="32768"
$env:LOCAL_AGENT_MODEL_MAX_TOKENS="2048"
$env:LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS="180"
$env:LOCAL_AGENT_REMOTE_VERIFY_TLS="0"
$env:LOCAL_AGENT_REMOTE_ENABLE_THINKING="0"

uv run python server.py
# 新 PowerShell 窗口中重新设置上述变量后：
uv run python main.py
```

远程地址可配置为 API 根地址、以 `/v1` 结尾的地址，或完整 `/chat/completions` 地址；客户端会规范化为 Chat Completions 请求地址。内部服务若实际使用 HTTPS 且证书可信，应将 `LOCAL_AGENT_REMOTE_VERIFY_TLS` 设为 `1`。

### 4.4 家中：DeepSeek 官方 `v4-flash`（需要 API Key）

以下配置为更适合家中远程模型的较大默认值：64K context window、4K 输出和 300 秒 timeout。`LOCAL_AGENT_REMOTE_MODEL_NAME` 必须使用 DeepSeek 官方控制台当前显示的模型 ID；若控制台名称不是 `deepseek-v4-flash`，请替换为实际值。

```powershell
$env:LOCAL_AGENT_LLM_BACKEND="remote"
$env:LOCAL_AGENT_MODEL_PROFILE="deep"
$env:LOCAL_AGENT_REMOTE_API_BASE_URL="https://api.deepseek.com/v1"
$env:LOCAL_AGENT_REMOTE_API_KEY="在此粘贴仅保存于本机的 DeepSeek API Key"
$env:LOCAL_AGENT_REMOTE_MODEL_NAME="deepseek-v4-flash"
$env:LOCAL_AGENT_REMOTE_CONTEXT_WINDOW="65536"
$env:LOCAL_AGENT_MODEL_MAX_TOKENS="4096"
$env:LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS="300"
$env:LOCAL_AGENT_REMOTE_VERIFY_TLS="1"
$env:LOCAL_AGENT_REMOTE_ENABLE_THINKING="0"

uv run python server.py
# 新 PowerShell 窗口中重新设置上述变量后：
uv run python main.py
```

绝不能把真实 Key 写入 README、`.env` 以外的提交文件、终端截图或日志。官方 HTTPS 端点应保持 `LOCAL_AGENT_REMOTE_VERIFY_TLS="1"`。当前远程引擎仅在地址或模型名包含 `deepseek` 时发送 DeepSeek thinking 兼容字段；Flash 场景默认关闭 thinking，确认 Provider 支持后才设为 `1`。

### 4.5 hybrid：本地 Qwen 7B + 当前网络的远程模型

hybrid 同时需要第 4.2 节的**全部本地变量**，以及第 4.3 节或第 4.4 节中**一组远程变量**；唯一差异是把 backend 改为：

```powershell
$env:LOCAL_AGENT_LLM_BACKEND="hybrid"
uv run python server.py
```

hybrid 目前是 **eager loading**：启动时同时加载本地 Qwen 7B GGUF 并创建远程客户端。本地模型文件不存在或加载失败会阻止后端启动，不会自动退化为 remote-only。知识专家最终回答会按上下文与能力选择一次本地或远程调用，不做失败 fallback。

### 4.6 仅运行后端

```powershell
uv run python server.py
```

默认监听 `http://127.0.0.1:8000`；可使用 `LOCAL_AGENT_API_HOST`、`LOCAL_AGENT_API_PORT` 修改。也可以通过 `uv run uvicorn server:app --host 127.0.0.1 --port 8000` 启动。

## 5. 配置总览

除非特别说明，所有值都由 `core/settings.py` 在进程启动时读取。

### 5.1 服务与客户端

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LOCAL_AGENT_API_HOST` | `127.0.0.1` | FastAPI 监听地址。 |
| `LOCAL_AGENT_API_PORT` | `8000` | FastAPI 监听端口。 |
| `LOCAL_AGENT_API_BASE_URL` | `http://{host}:{port}` | 桌面端访问后端的根地址。部署到其他机器时需与后端可达地址一致。 |
| `LOCAL_AGENT_MODEL_PROFILE` | `balanced` | 预设：`fast`、`balanced`、`deep`；影响未显式覆盖的本地窗口、输出、History、Summary 与 RAG 默认值。 |

### 5.2 模型与模型选择

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LOCAL_AGENT_LLM_BACKEND` | `remote` | 可选 `local`、`remote`、`hybrid`。 |
| `LOCAL_AGENT_MODEL_PATH` | `data/models/qwen2.5-7b-instruct-q4_k_m.gguf` | 本地 Qwen 7B GGUF 路径；`local`/`hybrid` 实际需要有效文件。 |
| `LOCAL_AGENT_MODEL_CONTEXT` | 随预设变化 | LOCAL_FAST（本地 Qwen 7B）的 context window；推荐从 `4096` 开始。 |
| `LOCAL_AGENT_MODEL_THREADS` | 随预设变化 | 本地推理线程数。 |
| `LOCAL_AGENT_MODEL_GPU_LAYERS` | `0` | 本地卸载到 GPU 的层数；`0` 表示纯 CPU。 |
| `LOCAL_AGENT_MODEL_MAX_TOKENS` | 随预设变化 | 单次最大输出 Token，也是 Profile 的输出预留；公司 Qwen 27B 可从 `2048` 起，家中 DeepSeek 可从 `4096` 起。 |
| `LOCAL_AGENT_REMOTE_API_BASE_URL` | 空 | 远程 OpenAI-compatible API 根地址；`remote`/`hybrid` 必填。公司填写内网 IP 地址，DeepSeek 填写官方 HTTPS 地址；可接受根地址、`/v1` 或完整 chat completions 地址。 |
| `LOCAL_AGENT_REMOTE_API_KEY` | 空 | 公司 Qwen 27B 无鉴权时显式设为空字符串；家中 DeepSeek 必须设置本机 API Key，客户端仅在非空时发送 Bearer Header。 |
| `LOCAL_AGENT_REMOTE_MODEL_NAME` | `Qwen3.5-27B` | 传递给远程兼容接口的模型 ID；公司使用部署服务登记的 Qwen 27B ID，DeepSeek 使用官方控制台显示的 `v4-flash` 实际 ID。 |
| `LOCAL_AGENT_REMOTE_CONTEXT_WINDOW` | `32768` | REMOTE_ADVANCED 的明确 context window 声明。公司按实际部署填写；家中可先用 `65536`，但必须以 DeepSeek 实际套餐/模型限制为准。 |
| `LOCAL_AGENT_REMOTE_TIMEOUT_SECONDS` | `120` | 远程 HTTP 超时秒数；公司内网建议 `180`，家中 DeepSeek 可用 `300`。 |
| `LOCAL_AGENT_REMOTE_VERIFY_TLS` | `0` | `1` 启用 TLS 证书校验；公司 HTTP IP 服务使用 `0`，DeepSeek 官方 HTTPS 必须使用 `1`。 |
| `LOCAL_AGENT_REMOTE_ENABLE_THINKING` | `0` | `1` 请求远程 Provider 开启 thinking；DeepSeek Flash 默认保持 `0`，仅在确认模型支持时开启。 |

模型选择目前只接入 `knowledge_expert` 的最终回答。`LOCAL_FAST` 当前保守声明不支持工具、结构化输出、代码推理和长推理；`REMOTE_ADVANCED` 声明支持这些能力。AUTO 简单任务优先本地；窗口、硬能力、复杂度或高风险条件满足时可选择远程。不会在一次调用失败后 fallback 到另一个模型。

上下文选择预留 10% 安全余量：`ceil(minimum_context_window × 1.10)`。其中 minimum 已包含输出 Token 预留。hybrid Builder 以两个可用 Profile 的最大安全窗口构建上下文，避免先按本地窗口裁剪再决定远程；裁剪前 raw 需求用于信息保留偏好，最终 messages 需求用于实际执行校验。

### 5.3 Memory、RAG 与知识库

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LOCAL_AGENT_MEMORY_DB_PATH` | `data/database/agent_memory.db` | SQLite Memory 数据库路径。 |
| `LOCAL_AGENT_CHROMA_DIR` | `chroma_db` | Chroma 持久化目录。 |
| `LOCAL_AGENT_EMBEDDING_MODEL_PATH` | `data/models/bge-large-zh-v1.5` | 本地 embedding 模型目录。 |
| `LOCAL_AGENT_EMBEDDING_QUERY_PROMPT_NAME` | 空 | 可选 embedding 查询 Prompt 名。 |
| `LOCAL_AGENT_EMBEDDING_BATCH_SIZE` | `8` | embedding 批量大小，最小为 1。 |
| `LOCAL_AGENT_KB_COLLECTION` | `huawei_wiki_collection` | 知识库集合名称。 |
| `LOCAL_AGENT_RAG_TOP_K` | 随预设变化 | RAG 返回的文档数量。 |
| `LOCAL_AGENT_RAG_MIN_SCORE` | `0.55` | RAG 最低分数，限制在 0 到 1。 |
| `LOCAL_AGENT_RAG_DOC_MAX_CHARS` | 随预设变化 | 单个 RAG 文档字符上限。 |
| `LOCAL_AGENT_RAG_CONTEXT_MAX_CHARS` | 随预设变化 | RAG 上下文总字符上限。 |
| `LOCAL_AGENT_LOCAL_KB_DIR` | `data/knowledge_base` | 本地知识库源文件目录。 |
| `LOCAL_AGENT_WIKI_COOKIE` | 空 | Wiki 同步 Cookie；敏感信息，不要提交。 |
| `LOCAL_AGENT_SYNC_ENABLED` | `0` | 是否启用同步相关能力，`1` 开启。 |

### 5.4 History、摘要与编排

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LOCAL_AGENT_HISTORY_WINDOW_SIZE` | 随预设变化 | 每次请求保留的 History 条数。 |
| `LOCAL_AGENT_SUMMARY_TRIGGER_MESSAGES` | 随预设变化 | 达到此消息量后触发摘要。 |
| `LOCAL_AGENT_SUMMARY_KEEP_RECENT` | 随预设变化 | 摘要后仍原样保留的最新消息数。 |
| `LOCAL_AGENT_SUMMARY_MAX_CHARS` | 随预设变化 | 摘要最大字符数。 |
| `LOCAL_AGENT_ORCHESTRATION_ENABLED` | `1` | 是否允许多 Agent 编排。 |
| `LOCAL_AGENT_ORCHESTRATION_MAX_AGENTS` | `3` | 单次编排最多专业 Agent 数。 |

## 6. API 速查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/chat` | 流式聊天；JSON：`agent_id`、`query`、可选 `file_path`。 |
| `GET` | `/api/history/{agent_id}?limit=10&offset=0` | 分页获取某 Agent 的历史。 |
| `GET` | `/api/search?keyword=...` | 搜索持久化消息。 |
| `GET` | `/api/memory` | 获取 Memory 管理数据。 |
| `DELETE` | `/api/memory` | JSON：`message_ids` 或 `delete_all`。 |

示例：

```powershell
$body = @{ agent_id = "core_router"; query = "你好" } | ConvertTo-Json
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/chat" `
  -ContentType "application/json" `
  -Body $body
```

可用 Agent 包括 `core_router`、`data_analyst`、`code_expert` 与 `knowledge_expert`。知识专家在本地知识库不可用或没有足够相关信源时会失败关闭，不会用通用知识补写本地事实。

## 7. 内置工具与文件权限提醒

已注册本地工具：列目录、分析 CSV/Excel、读取系统状态。它们可能读取本机路径或文件；仅向可信用户开放服务，并谨慎处理模型生成的路径参数。

## 8. 测试与检查

在已具备依赖的 Python 环境执行：

```powershell
uv run python -m pytest tests/test_planning.py tests/test_model_selection.py tests/test_knowledge_routing.py -q
uv run python -m unittest `
  tests.test_runtime_context `
  tests.test_agent_state `
  tests.test_agent_loop `
  tests.test_state_machine `
  tests.test_model_context `
  tests.test_planning `
  tests.test_model_selection `
  -q
uv run python -m compileall core tests
git diff --check
```

完整测试：

```powershell
uv run python -m pytest -q
```

## 9. 常见故障

### remote/hybrid 启动提示缺少远程地址

设置 `LOCAL_AGENT_REMOTE_API_BASE_URL`。remote 和 hybrid 都要求该值非空。

### local/hybrid 无法加载模型

检查 `LOCAL_AGENT_MODEL_PATH` 是否存在、Python/llama-cpp 版本是否匹配、模型是否为可用 GGUF，以及本机内存/显存是否足够。hybrid 不会因为本地失败自动改用远程。

### 桌面端连不上后端

确认后端已启动，并使 `LOCAL_AGENT_API_BASE_URL` 与实际监听地址一致；远程部署时不要保留默认 `127.0.0.1`。

### `uv sync` 找不到本地 `llama-cpp-python` wheel

当前项目依赖固定引用 Windows 本地 wheel。请确认 `pyproject.toml` 中的 wheel 路径存在，并且 wheel 同时匹配 Python `cp312` 与 `win_amd64`；修正本机路径后重新执行 `uv sync`。

## 10. 当前边界

- Model Selection 目前只接入知识专家的最终回答路径。
- 不包含 Scheduler、DAG 环检测、Retry、Fallback、Circuit Breaker、模型健康检查或 Budget。
- 当前 hybrid 使用 eager loading；后续可在保持无 fallback 语义的前提下评估 lazy loading。
- Token 统计为确定性近似估算，不是 Provider 精确 tokenizer。
