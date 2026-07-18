# 本地 Mock 知识库迁移到最新 dev 的结果

## 1. 源分支和目标基线

- 源分支：`origin/feat/local-mock-kb-ingestion`
- 目标基线：`origin/dev`
- 迁移分支：`feat/local-mock-kb-on-dev`
- 迁移开始时目标 HEAD：`47d4f4b0c2d9a01fa5e979a1fba0721cbca0194b`
- 迁移方式：以源分支 Diff 为功能参考，在最新 `dev` 架构上手工适配；未执行 merge、rebase 或批量 cherry-pick。

## 2. 检查过的 Commit 和 Diff

执行过：

```text
git log --oneline --decorate origin/dev..origin/feat/local-mock-kb-ingestion
git diff --stat origin/dev...origin/feat/local-mock-kb-ingestion
git diff --name-status origin/dev...origin/feat/local-mock-kb-ingestion
```

源分支 Commit：

- `44e7c19`：本地知识库解析、切片、入库、查询和配置主体。
- `5a8d4a9`：Remote LLM Thinking、知识查询改写 Token 和 KB 启动诊断。
- `8000dda`：桌宠 PNG，不属于 KB 迁移。
- `ee570f1`：桌宠 PNG，不属于 KB 迁移。

本地源分支另有 `4546037` 桌宠资源提交，不属于本次迁移且未带入。

## 3. 已迁移功能

- Settings 增加 Collection、Embedding Batch Size 和 Query Prompt Name 环境变量覆盖。
- `VectorDBManager` 支持可配置 Collection、Embedding 批次、查询 Prompt 和写入批次。
- 入库端与查询端复用同一组 Chroma、Collection 和 Embedding 配置。
- 支持 `.md`、`.txt`、`.rst`、`.html`、`.htm`、`.pdf`、`.docx`、`.csv`、`.xlsx`、`.xls`。
- 保留行首缩进，规范换行与尾部空白，按 Markdown 标题和文本边界切片。
- PDF 按页、Excel 按 Sheet 解析；表格生成字段摘要，不直接展开整张大表。
- Chunk 带来源、文件名、章节、页码、Sheet、顺序、Hash、解析器和批次 Metadata。
- 入库 CLI 支持 Source、Collection、Rebuild、Dry Run、Chunk 参数、分批写入和周期 Flush。
- 查询 CLI 支持 Query、Collection、Top-K、Score、Source、Metadata 和文本摘要。
- DeepSeek 兼容 Provider 在关闭 Thinking 时显式发送 `disabled`，启用时才发送 `reasoning_effort`。
- 非 DeepSeek Provider 不接收 DeepSeek 专属参数，保留 `dev` 的通用模型适配行为。
- 空 `content` 且 `finish_reason=length` 时返回明确截断错误。
- 知识查询改写上限从 24 提升到 128，并对查询改写、工具规划、摘要和编排规划禁用复杂 Thinking。
- 服务启动时通过日志记录 Collection、Chroma 路径、Embedding 标识和当前 Count。

## 4. 未迁移的改动及原因

- 未迁移 PNG 和桌宠资源：与本地 Mock KB 无关。
- 未迁移 `uv.lock`：源分支锁文件包含与当前 `dev` 依赖状态无关的大范围变更。
- 未迁移源分支的 DeepSeek 默认模型、默认 API 地址或备用 Key 环境变量：保留 `dev` 的模型配置和安全边界。
- 未迁移源分支的本地 wheel 绝对路径：禁止提交机器相关路径。
- 未迁移旧 `test/` 路径配置：测试按当前 `dev` 的 `tests/` 结构落地。
- 未复制旧 Settings、Server、AgentRouter 或 LLMEngine 整个文件。
- 未生成或提交本地模型、Chroma 数据库、4180 Chunk 数据、日志、`.env` 或 IDE 配置。

## 5. 修改文件列表

- `.gitignore`
- `core/settings.py`
- `core/knowledge_base/document_loader.py`
- `core/knowledge_base/vector_db_manager.py`
- `core/llm_engine.py`
- `core/agent_router.py`
- `server.py`
- `scripts/bootstrap_local_kb.py`
- `scripts/query_local_kb.py`
- `pyproject.toml`
- `tests/test_kb_settings.py`
- `tests/test_vector_db_manager.py`
- `tests/test_document_loader.py`
- `tests/test_kb_scripts.py`
- `tests/test_remote_llm_engine.py`
- `docs/migration/local_mock_kb_to_dev_result.md`

## 6. 每个文件的修改原因

- `.gitignore`：阻止密钥、IDE、虚拟环境、模型、Chroma、入库日志和本地数据库进入 Git。
- `core/settings.py`：把 KB Collection 与 Embedding 参数纳入 `dev` 的统一配置对象。
- `document_loader.py`：补齐格式、结构化解析、Metadata 和切片能力，同时保留现有 API。
- `vector_db_manager.py`：消除硬编码 Collection，并补齐可测试的配置传递、批量写入和 Collection 管理。
- `llm_engine.py`：按 Provider 能力发送 Thinking 参数并诊断截断响应。
- `agent_router.py`：提高查询改写 Token，并让轻量规划明确关闭 Thinking。
- `server.py`：注入统一 KB 配置并输出一次性启动诊断日志。
- 两个 `scripts/`：提供可复现的入库与直查排障入口。
- `pyproject.toml`：补齐 `.xls` 读取和测试依赖配置，不修改现有本地模型依赖。
- `tests/`：覆盖本次迁移的配置、解析、向量库、CLI 和 Remote LLM 风险点。

## 7. 与 dev 冲突的位置

- `dev` 已有 Settings 预设、Remote Qwen 配置和 Runtime/Harness，源分支基于旧架构修改了同一文件。
- `dev` 的 `VectorDBManager` 接口已被 AgentRouter 和 Server 使用，但 Collection 仍硬编码。
- `dev` 已有 7 种格式 Loader，但会去除代码缩进且缺少结构化页码/Sheet Metadata。
- `dev` 使用 `chat_template_kwargs` 控制非 DeepSeek 模型 Thinking，源分支改为 DeepSeek 专属字段。
- `dev` 的 AgentRouter 已包含编排、RunContext 和知识专家流程，不能用旧文件覆盖。

## 8. 冲突解决方式

- 保留 `dev` 的 Settings 字段、默认模型、Runtime 和依赖注入结构，只增加缺失 KB 字段。
- 扩展现有 `VectorDBManager`，不创建第二套检索实现。
- 在现有 Loader API 上增加 `load_document_file` 和结构化切片，保留 `load_documents` 兼容入口。
- 通过 Provider 判断隔离 DeepSeek 参数；非 DeepSeek 继续使用 `dev` 行为。
- 在现有 AgentRouter 的 `_collect_model_response` 增加每次调用的 Thinking 覆盖，不改变 Runtime/RunContext 流程。

## 9. 配置项和环境变量

```text
LOCAL_AGENT_LOCAL_KB_DIR
LOCAL_AGENT_KB_COLLECTION
LOCAL_AGENT_CHROMA_DIR
LOCAL_AGENT_EMBEDDING_MODEL_PATH
LOCAL_AGENT_EMBEDDING_QUERY_PROMPT_NAME
LOCAL_AGENT_EMBEDDING_BATCH_SIZE
LOCAL_AGENT_REMOTE_ENABLE_THINKING
```

`LOCAL_AGENT_EMBEDDING_BATCH_SIZE` 最小为 1。未设置 Collection 时保留 `dev` 原默认 `huawei_wiki_collection`；设置后完整覆盖默认值。仓库不包含本地绝对路径、Key 或 Cookie 默认值。

## 10. 测试命令

```powershell
uv run --no-project python -m compileall -q core scripts
uv run --no-project --with pytest --with pandas --with langchain-core --with langchain-chroma --with langchain-huggingface python -m pytest -q
uv run --no-project --with pandas python scripts/bootstrap_local_kb.py --help
uv run --no-project python scripts/query_local_kb.py --help
```

## 11. 测试结果

- 编译检查：通过。
- 完整 pytest：`55 passed`。
- `bootstrap_local_kb.py --help`：通过。
- `query_local_kb.py --help`：通过。
- 新增迁移专项测试：22 项，全部通过。

## 12. 未执行测试及原因

- 未使用真实 Embedding 模型执行向量化：Codex 工作区不应下载或提交模型权重。
- 未连接真实 Chroma 数据执行 4180 Chunk 验收：禁止生成或上传该本地数据库。
- 未执行真实远程 LLM 请求：避免依赖或暴露 API Key；使用 Mock HTTP 响应验证请求体和错误处理。
- 未执行 PyCharm 启动配置验收：属于用户本地 IDE 环境，需按第 13 节手工验证。

## 13. 本地人工验收步骤

先在实际终端或 PyCharm Run Configuration 设置所需环境变量，然后执行：

```powershell
python scripts/bootstrap_local_kb.py `
  --source-dir <kb-source-dir> `
  --collection local_agent_mock_v1 `
  --rebuild `
  --chunk-size 1400 `
  --chunk-overlap 180 `
  --ingest-batch-size 32 `
  --flush-chunks 128

python scripts/query_local_kb.py "<verification-query>" `
  --collection local_agent_mock_v1 `
  --top-k 5

python server.py
```

确认 Settings Collection 为 `local_agent_mock_v1`；启动日志显示目标 Collection 和实际 Count；直查结果含正确 Source；前端直接调用 `knowledge_expert` 能检索；`core_router` 委派知识专家后可得到带来源结果；关闭 Thinking 后不再出现仅有 `reasoning_content`、正式 `content` 为空的问题。

## 14. 安全检查结果

- `git diff --check` 通过。
- Diff 未发现 API Key、Cookie、Private Key 或本机绝对路径。
- 未包含 PNG、模型权重、Chroma 数据、知识库原文、入库日志、`.env` 或 IDE 配置。
- `.gitignore` 已覆盖 `chroma_db/`、`data/models/`、`data/ingestion_logs/`、`.env` 和 `.idea/`。

## 15. Commit Hash

实现提交：`3efa709c9d9154ac4c80aef66822f7a4a19f8566`。

## 16. 推送分支

`origin/feat/local-mock-kb-on-dev`

## 17. Draft PR 链接

<https://github.com/gemhr/Local_Agent/pull/10>

## 18. 剩余风险和后续建议

- 不同 SentenceTransformers 模型的 Query Prompt 名称并不统一；留空可保持 `dev` 行为，Qwen3 Embedding 可显式配置 `query`。
- `.xls` 解析依赖 `xlrd`，部署环境需要安装项目依赖。
- DeepSeek 能力目前根据模型名或 API 地址中的 `deepseek` 判定；若公司代理隐藏 Provider 标识，后续可在 Settings 增加显式 Provider 类型。
- 本次只用 Mock 验证 Chroma 调用；真实模型和 4180 Chunk 数据仍需按第 13 节在本地验收。
