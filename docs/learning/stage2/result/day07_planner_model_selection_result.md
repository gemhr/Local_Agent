# 阶段二第 7 天改造结果

## 1. 本次任务目标
建立不可变结构化 Plan 与确定性 Model Selection Policy，并最小接入知识专家最终回答调用。

## 2. 修改前 Planner 和模型选择现状
Router 的任务分解入口是 `AgentRouter._plan_orchestration`：它解析 `Delegate:` 文本和显式知识专家委派；没有显式 Plan。工具规划在 `_plan_tool_call`，会额外调用当前 `self.llm`。原模型选择只在 `server.py` 按 `LOCAL_AGENT_LLM_BACKEND` 实例化一个 `LocalLLMEngine` 或 `RemoteLLMEngine`，Router 内所有生成均直接调用 `self.llm`。

## 3. 发现的问题
没有静态计划边界；上下文特征没有进入模型选择；单一 `self.llm` 无法表达能力约束、强制偏好与确定性原因。

## 4. 最终设计方案
新增纯数据 Planner 边界和纯规则 Policy：任务能力 → 单步骤确定性 Plan → 第 6 天完整 `ModelContextRequirements` → 选择 Decision → Resolver → 一个已有模型对象。没有新增规划 LLM 调用。

## 5. 新增文件
- `core/runtime/planning.py`
- `core/runtime/model_selection.py`
- `tests/test_planning.py`
- `tests/test_model_selection.py`
- 本文档。

## 6. 修改文件
- `core/runtime/__init__.py` 导出新增边界。
- `core/agent_router.py` 在知识专家最终回答路径创建确定性 Plan、选择并解析模型。

## 7. Plan 和 PlanStep
`Plan` 字段为 `plan_id/version/task_summary/steps/created_at/source`；`PlanStep` 字段为 `step_id/title/description/depends_on/completion_criteria/preferred_agent/capability_requirements`。二者 frozen；不保存状态、时间、错误、provider 或模型名。

## 8. TaskCapabilityRequirements
字段为全部 `requires_*` bool、`risk_level`、`estimated_steps`。当前来源是既有 agent_id、工具意图规则和任务形态；不保存用户正文。简单 RAG 不会单独强制远程。

## 9. PlanValidator
校验 ID、正整数 version（拒绝 bool）、UTC 时间、非空步骤、唯一 Step ID、依赖存在/非自依赖/无重复、非空完成条件和合法能力需求；不做拓扑排序、环检测、Ready Step 或 Scheduler。

## 10. Model Profile
`ModelProfile` 包含 Profile ID、窗口、输出上限、四项能力、质量/时延层级，且拒绝 bool 数字配置；不含 Secret。`LOCAL_FAST` 和 `REMOTE_ADVANCED` 是抽象 Profile。

## 11. Model Selection Request / Decision
Request 仅有 agent ID、能力需求、上下文需求、偏好、Profile；Decision 有选中 Profile、稳定原因码、中文原因文本、匹配规则和 `fallback_allowed=False`。错误只含安全原因、请求类型、缺失能力和窗口数字。

## 12. 规则优先级
Profile 校验 → FORCE_LOCAL/FORCE_REMOTE → 带 1.10 安全系数的完整 context window → Tool/structured/code/long reasoning 硬能力 → 多 Agent/长推理/≥3 步/高风险优先远程 → 默认本地。

## 13. 用户模型偏好
内部默认 `AUTO`；`FORCE_LOCAL` 在本地不满足时失败，绝不升级远程；`FORCE_REMOTE` 要求远程 Profile 存在且满足硬约束。

## 14. Model Resolver 和真实模型映射
Resolver 只执行 `ModelProfileId → 已有引擎对象`。现有配置中本地映射到 `model_path` 的 llama.cpp 引擎，远程映射到 `remote_model_name` 的 OpenAI-compatible 引擎；远程窗口由显式 `LOCAL_AGENT_REMOTE_CONTEXT_WINDOW` 配置，默认 32768。设置 `LOCAL_AGENT_LLM_BACKEND=hybrid` 时会同时装配两个 Profile；名称和 API 凭据不进入 Policy、Plan 或本文档。

## 15. LocalAgent 真实接入路径
已接入知识专家 `_stream_final_response` 和 `_complete_final_response` 的最终调用：第 6 天 Builder 输出的完整 context requirements 进入选择请求，再由 Resolver 返回一个模型。未接入一般回答、摘要、查询改写、工具规划、编排和 synthesis。最终回答实际使用所选首选模型，每次一个；不实现 fallback，失败不切换。

## 16. 与现有功能兼容方式
messages 内容及 role、Router 路由结果、`[[ORCH]]`、API、Memory Schema、AgentState Schema 和流式协议均未修改。

## 17. 测试命令和结果
- `python -m pytest tests/test_planning.py tests/test_model_selection.py tests/test_knowledge_routing.py -q`：通过。
- 其余项目测试和静态检查见提交前命令记录。

## 18. 设计权衡、未完成事项和面试描述
Plan 尚未驱动 Scheduler；未实现 DAG 环检测、Budget、Deadline 选择、Fallback、健康检查、Decision 持久化或 Trace；Token 仍为近似值；其他 Agent 路径未迁移。面试表达：先用冻结数据模型和可测试的确定性规则切开规划/选择/解析边界，再小范围接入完整上下文主路径。

## 19. 重点 Bad Case
### Bad Case 1：Plan 和 AgentState 同时保存 Step 状态
- 类型：假设构造
- 触发条件：在 PlanStep 增加 status 或执行时间。
- 故障表现：静态计划与 Runtime 状态冲突。
- 根因分析：职责边界混淆。
- 修复方案：PlanStep 仅保存静态定义。
- 回归测试：`test_capability_validation_and_static_plan_fields`。
- 对应知识点：不可变计划边界。
- 面试表达：状态仅属于 AgentState。
- 当前状态：已覆盖。

### Bad Case 2：短问题依赖长上下文，却错误选择本地模型
- 类型：假设构造
- 触发条件：用户问题短而 History/RAG 很长。
- 故障表现：本地窗口溢出。
- 根因分析：按 `len(user_query)` 判断。
- 修复方案：使用完整 ModelContextRequirements 和 1.10 余量。
- 回归测试：`test_remote_rules`。
- 对应知识点：端到端窗口核算。
- 面试表达：按实际 messages，不按请求字符数。
- 当前状态：已覆盖。

### Bad Case 3：用户 FORCE_LOCAL 却静默升级远程
- 类型：假设构造
- 触发条件：本地缺少 Tool 或窗口能力。
- 故障表现：违反隐私数据边界。
- 根因分析：把偏好当软提示。
- 修复方案：明确抛出 ModelSelectionError。
- 回归测试：`test_forced_preferences_and_unsatisfied_profiles_fail_closed`。
- 对应知识点：硬约束。
- 面试表达：Resolver 不会替换选择结果。
- 当前状态：已覆盖。

### Bad Case 4：Planner 直接写死 DeepSeek 或 Qwen
- 类型：假设构造
- 触发条件：Plan 保存具体供应商或模型名。
- 故障表现：计划不可跨环境复用。
- 根因分析：规划与部署映射耦合。
- 修复方案：Plan 只保存能力，Resolver 映射 Profile。
- 回归测试：字段反射与 Resolver 测试。
- 对应知识点：Profile 抽象。
- 面试表达：同一 Plan 可映射到不同部署。
- 当前状态：已覆盖。

## 20. 需要带回 ChatGPT 审查的信息
Planner 入口为 `create_single_step_plan`，选择入口为 `ModelSelectionPolicy.select`，Resolver 为 `ModelResolver.resolve`。上述章节已列出最终字段、规则、1.10 余量、强制偏好、真实接入/未接入路径、无 fallback、未改 API/AgentState/Memory/Stream、测试与 Bad Case。需人工确认生产环境同时提供 LOCAL_FAST 与 REMOTE_ADVANCED 对象的启动装配；后续建议只在第 8 天实现 Scheduler 输入消费，不在本次实施。

## 21. 第 7 天补充审查：上下文与选择顺序
知识专家真实顺序为：先查询一次 RAG/Memory 并由 `ContextBuilder.build()` 生成候选上下文统计，再将 Builder 的**未裁剪** `raw_minimum_context_window` 交给 Policy，最后按已选 Profile 调用模型。当前采用方案 B：Router 的 Builder 窗口为所有可用 Profile `context_window / 1.10` 的最大整数值；local-only 来自 LOCAL_FAST，remote-only 来自 REMOTE_ADVANCED，hybrid 取两者最大值。因此不再固定绑定 `Settings.model_context`，不会先按本地窗口裁剪后再选择。

`raw_estimated_input_tokens/raw_minimum_context_window` 是 Builder 规范化、去重后但 item 裁剪前的需求；`estimated_input_tokens/minimum_context_window` 是最终实际 messages 需求，`was_truncated` 明确标记裁剪。Policy 选择 raw 字段；调用前以最终字段再次验证。安全公式严格为 `ceil(minimum_context_window × 1.10)`，其中 minimum 已包含 reserved output，绝不重复加输出预算，也不放大 Profile 窗口。

LOCAL_FAST 当前保守声明不支持 Tool、structured output、code reasoning、long reasoning；REMOTE_ADVANCED 声明支持这些能力。声明来自 `server.py` 的 Profile 装配，不依赖模型名称，Policy 不检查 DeepSeek/Qwen 字符串；测试使用显式 Fake Profile。hybrid 为 eager loading：启动立即构造远程客户端并加载本地 GGUF；本地加载失败会阻止启动，即使远程配置可用。其代价是更长启动时间和本地内存/显存占用，当前未改为 lazy loading。

### Bad Case 5：上下文先按本地窗口裁剪，导致远程模型永远不会被选择
- 类型：真实发现
- 触发条件：hybrid 使用原 `Settings.model_context` 作为 Builder `max_input_tokens`，而本地窗口小于远程窗口。
- 故障表现：RAG 或 Memory 先被裁剪，裁剪后需求可能满足本地窗口并错误选择 LOCAL_FAST。
- 根因分析：先前 Builder 在 Policy 之前执行且固定使用本地窗口，只有裁剪后统计进入选择。
- 修复方案：Builder 以最大 Profile 安全窗口构建，同时输出 raw 与最终需求；Policy 使用 raw，调用前校验最终需求。
- 回归测试：`test_safety_margin_uses_minimum_window_once_at_exact_boundary`、知识专家远程选择测试。
- 对应知识点：完整上下文窗口核算与选择前需求保真。
- 面试表达：不能让裁剪结果伪装成原始任务只需要小窗口；至少保留 raw 与 final 两套统计。
- 当前状态：已修复，后续可演进为两阶段正式 Build。

## 22. 第 7 天最终语义检查
`raw_minimum_context_window` 不是所有 Profile 的绝对可执行硬约束：执行可行性只由 final `minimum_context_window` 经同一安全函数计算。raw 用于 AUTO 下判断本地是否会造成额外信息损失、优先选择可保留更多内容的远程 Profile、生成 `CONTEXT_WINDOW_REQUIRED`，并在 `matched_rules` 记录 `context_truncated`。即使 raw 超过全部 Profile，只要 final 由非 mandatory 内容裁剪得到且远程 final 校验通过，AUTO 选择远程；不会选择无法承载 raw 的本地。FORCE_LOCAL 在 mandatory 完整、final 满足本地安全窗口且只裁剪可选内容时允许本地，Decision 也带 `context_truncated`，且无 fallback。

安全余量唯一来源是 `ModelSelectionPolicy.context_window_safety_ratio` 及其 `required_context_window()` / `maximum_safe_context_window()`：Builder 安全窗口、Policy final 可行性和最终调用前校验均复用这些方法。

### Bad Case 6：Windows 本地 Wheel 导致 Linux 测试环境无法恢复依赖
- 类型：真实发现
- 触发条件：Linux 环境使用 `uv run` 解析项目中指向 Windows 本地路径的 llama_cpp_python wheel。
- 故障表现：uv 在依赖解析阶段失败，测试未开始。
- 根因分析：平台专用本地 wheel 未使用 platform marker 或 optional dependency group 隔离。
- 修复方案：后续使用平台 Marker 或 optional dependency group；本次不修改依赖体系。
- 回归测试：直接 Python 目标 Planner/Selection/知识专家测试通过，证明不是业务测试失败。
- 对应知识点：跨平台依赖可恢复性。
- 面试表达：先区分依赖解析故障和业务回归，再以平台标记隔离可选 native 依赖。
- 当前状态：已记录，待后续依赖治理处理。
