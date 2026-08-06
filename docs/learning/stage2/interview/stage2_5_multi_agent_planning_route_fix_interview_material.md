# LocalAgent Stage 2.5 Multi-Agent 规划路由修复面试材料（“查数据库…csv” 被 PLANNING_FAILED 拒绝）

> 适用范围：Stage 2.5 Multi-Agent 规划层的一次真实缺陷修复——用户输入
> “查数据库，mock_test_results.csv这个表的第四列的表头是什么” 被
> `INVALID_CAPABILITY` / `PLANNING_FAILED` 拒绝且前端只显示“运行失败”。
> 修复组合为：确定性路由增强（数据查询直接委派 data_analyst）+ Planner
> 提示词 capability 白名单约束 + 前端规划错误文案映射。
>
> 真实性声明：本文中的“真实发现”指本地项目源码审查、真实用户复现日志、
> 实施或测试中实际观察到的问题，不等同于线上生产事故；“假设构造”只用于
> 风险推演，不描述成真实事故。

## 1. 一句话项目定义

这个修复解决一个真实的用户可感问题：明确的数据分析请求（“查数据库…
csv 表头”）在默认 Coordinated 入口被规划层拒绝，用户只看到笼统的
“运行失败”。修复目标是让常见数据查询在解析层确定性路由到
`data_analyst`（零模型调用），同时降低 Planner 模型发明未注册
capability 的概率，并让规划类失败在前端给出可读、可安全重试的文案。

修复后的诚实表述：未知 capability 仍然 fail-closed（编译/解析合同未
放宽）；深度收紧（禁止 Planner 声明 capabilities 字段）未纳入本次，
留作后续选项；不新增 Agent 类型、不新增数据库能力。

## 2. 真实用户场景与故障证据

用户在主 Agent 入口输入：

```text
查数据库，mock_test_results.csv这个表的第四列的表头是什么
```

期望：

```text
入口 -> 识别为数据分析任务 -> data_analyst 执行 -> 唯一 final 交付
```

实际（真实前端复现 + 运行日志，run_id=`cfc9f1861e074bca811b173236dd68cb`）：

```text
RUN_STARTED
PLANNING_STARTED
MODEL_STARTED(profile=remote_advanced)
MODEL_COMPLETED(SUCCEEDED, 2750ms)      # Planner 模型调用本身成功
ERROR(INVALID_CAPABILITY, fatal=true)
RUN_COMPLETED(FAILED, stop_reason=PLANNING_FAILED, 2766ms)
```

前端显示“运行失败”；日志无原始 Planner 输出（按安全合同不落盘），
错误码为 `INVALID_CAPABILITY`。

## 3. 旧行为与根因链

### 3.1 确定性路由为什么没接住

`core/runtime/multi_agent_planning.py` 的 `_deterministic_core_decision`：

- `_DELEGATION_VERB = (?:调用|使用|请让|让|交给|委派)`：query 以“查”开头，
  但“查”不在动词集合中；
- `_DOCUMENT_REFERENCE` 扩展名集合只有 `md|txt|pdf|docx?`：“.csv”不匹配；
- data_analyst 的 `deterministic_aliases` 只有
  `("data_analyst", "数据分析师", "数据专家")`：“数据库”不匹配。

结论：`_deterministic_core_decision` 返回 None，请求落入模型路径。

### 3.2 模型路径为什么编译失败

1. Planner 模型（remote_advanced，2.75s）返回 DELEGATE JSON；
2. `StrictPlanningDecisionParser` 解析通过（agent_id 在白名单内，
   capabilities 字段在 schema 允许范围内）；
3. `PlanCompiler._compile_delegated` 校验
   `task.required_capabilities <= registration.capabilities` 失败：
   模型声明的 capability 不在 data_analyst 的 `{"data_analysis"}` 白名单中，
   抛 `INVALID_CAPABILITY`（`plan_compiler.py:270` “Agent 不具备所需 capability”）；
4. RunCoordinator 按合同映射 `StopReason.PLANNING_FAILED`；
5. 前端 `safe_error_text` 没有 `INVALID_CAPABILITY` 映射，回落为“运行失败。”。

这说明：用户“被安全拦截”的理解方向正确，但更准确的说法是
**规划/编译合同的 fail-closed**——模型无权发明 Registry 白名单之外的
capability，编译器必须拒绝；这是设计行为，不是新增的敏感词安全扫描。

## 4. 方案讨论与取舍

| 方案 | 优点 | 风险或不足 | 结论 |
| --- | --- | --- | --- |
| 确定性路由增强（动词/扩展名/别名） | 常见数据查询零模型调用，直接消化；可加负例防过度路由 | 只覆盖“别名+动词/文件引用”组合，不能覆盖所有数据表达 | 采纳（主修复） |
| Planner 提示词 capability 白名单约束 | 低风险、不改合同，降低模型发明概率 | 模型不保证遵守提示词，编译仍可能 fail-closed | 采纳（辅助） |
| 前端规划错误文案映射 | 让用户看到“换一种说法再试”而不是“运行失败” | 只改可读性，不改根因 | 采纳（体验） |
| 深度收紧：禁止 Planner 声明 capabilities 字段（能力由 Registry 推导） | 从根上消除模型发明能力通道 | 改变 WP1 schema 合同，波及约 23 个测试文件；需另行评审 | 拒绝（本期），记录为后续选项 |
| 编译器忽略/归一化未知 capability | 不再报错 | 违反“未知 capability fail-closed”与“不得放宽断言”规则，掩盖模型错误 | 拒绝 |

关键取舍：不牺牲编译期 fail-closed 换取通过率；用“路由层消化常见输入 +
提示词降低发明概率”的组合，把错误率降下来，同时保留合同防线。

## 5. 核心状态机与路由行为

修复后的确定性判定（`_deterministic_core_decision`）：

```text
selected = 文本中命中 deterministic_aliases 的 specialist
if selected 为空 且 命中文档引用(md/txt/pdf/docx/csv/xlsx):
    走 knowledge_expert（既有文档检索 fallback，不变）
explicit = selected 非空 且 (命中委派动词(含查/查询) 或 文档引用)
if not explicit: 落入模型路径（不变）
else: DELEGATE(selected) -> Compiler 校验 -> typed Plan
```

“查数据库，mock_test_results.csv这个表的第四列的表头是什么”：

```text
selected = [data_analyst]      # 命中别名 “数据库”“csv”
explicit = true                # 命中动词 “查” + 文档引用 “.csv”
-> DELEGATE(task=data_analyst, synthesis_required=true)
-> Shape 2：task-data + synthesis
-> planning_calls == 0，模型无机会发明 capability
```

防过度路由（仍走模型）：`查看这个代码仓库结构`（无数据别名）、
`数据库索引原理是什么`（有别名但无委派动词/文件引用）、
`查一下天气`（无数据别名）。

## 6. 实际实现

### 6.1 确定性路由（`core/runtime/multi_agent_planning.py` + `agent_registry.py`）

- `_DELEGATION_VERB` 增加 `查询|查`；
- `_DOCUMENT_REFERENCE` 扩展名集合增加 `csv|xlsx`；
- data_analyst `deterministic_aliases` 增加 `"数据库", "csv", "excel"`。

### 6.2 Planner 提示词（`core/agent_router.py` `complete_planning_decision`）

system prompt 追加：

```text
task.capabilities 只能为空数组，或与 agent 对应：
knowledge_expert→rag，code_expert→code_reasoning，data_analyst→data_analysis；
不确定时不要声明 capabilities。
```

### 6.3 前端文案（`core/runtime/multi_agent_status.py`）

`SAFE_ERROR_TEXT` 新增：

- `INVALID_CAPABILITY` → “规划结果包含未支持的专家能力，请换一种说法再试。”
- `PLANNING_MODEL_FAILED` → “规划模型调用失败，请重试或换个说法。”
- `PLANNER_SCHEMA_INVALID` → “规划结果格式不被支持，请换个说法再试。”

规划失败无任何副作用，可安全重试，因此文案允许提示重试；这与
delivery unknown 类“绝不鼓励立即重试”的文案规则不冲突。

## 7. 安全、失败与兼容合同

- 未知 capability 继续 fail-closed：不因本次修复放宽编译/解析断言；
- 不新增 Agent 类型、不新增数据库/SQL 能力；`data_analyst` 仍是唯一
  数据分析 Agent；
- 确定性路由属于既有合同路径（deterministic rules），不是新架构能力；
- 不改 planner schema 版本、不改 Journal/Event/Trace/Metrics 合同；
- 裸文件引用（如“查询 exports.xlsx”但无数据别名）仍走
  knowledge_expert 文档检索 fallback，行为不变并已用测试固化。

## 8. Bad Cases

### Bad Case 1：“查数据库…csv 表头” 被 PLANNING_FAILED 拒绝且前端只显示“运行失败”

- 类型：真实发现（用户前端复现 + 运行日志，不是生产事故）
- 触发条件：默认 Coordinated 入口输入
  “查数据库，mock_test_results.csv这个表的第四列的表头是什么”。
- 故障表现：前端显示“运行失败”；日志
  `ERROR(INVALID_CAPABILITY)` → `RUN_COMPLETED(FAILED, PLANNING_FAILED)`；
  Planner 模型调用成功（2750ms）后编译拒绝。
- 根因分析：确定性路由未命中（“查”不在委派动词、“.csv”不在文档扩展、
  “数据库”不是 data_analyst 别名）→ 落入模型路径 → 模型声明 Registry
  白名单外的 capability → `PlanCompiler` 按合同抛
  `INVALID_CAPABILITY`；前端无该错误码文案，回落为笼统“运行失败”。
- 修复方案：确定性路由增强（动词加 `查询|查`、扩展名加 `csv|xlsx`、
  data_analyst 别名加 `数据库/csv/excel`）+ Planner 提示词 capability
  白名单 + 前端规划错误文案映射。
- 回归测试：`test_multi_agent_planning.py`（正/负例路由）、
  `test_stage2_5_wp6_e2e.py`（真实 E2E：planning_calls==0、data_analyst
  与 synthesis 各 1 次、唯一 OUTPUT、Memory 完整；prompt 白名单断言）、
  `test_frontend_multi_agent_status.py`（新文案与渲染）。
- 对应知识点：fail-closed 合同与用户体验的边界；确定性路由是常见输入的
  第一道防线；模型输出必须经 Registry/Compiler 校验。
- 面试表达：这不是新增安全扫描，而是规划/编译合同的 fail-closed——模型
  无权发明未注册能力；修复不是放宽校验，而是让常见数据查询根本不经过
  模型，并让失败原因对用户可读。
- 当前状态：已修复。

### Bad Case 2（风险推演）：“查询 exports.xlsx” 裸文件引用会路由到哪里

- 类型：假设构造（实施测试中发现并固化的行为，不是事故）
- 触发条件：无任何数据别名（数据库/csv/excel）但带 `.xlsx` 文件引用的
  query。
- 故障表现/行为：走既有文档检索 fallback，确定性委派
  `knowledge_expert` 单透传（`planning_source=DETERMINISTIC_RULE`），
  不调用模型。
- 根因分析：`_DOCUMENT_REFERENCE` 现在能匹配 `.xlsx`，`selected` 为空时
  按既有 rag fallback 规则选择 knowledge_expert。
- 修复方案：不修改；在 `test_multi_agent_planning.py` 固化该行为，
  防止后续把“任何文件引用”都误判为数据路由。
- 对应知识点：确定性路由的多信号组合（别名 + 动词/文件引用）与既有
  fallback 的优先级。
- 当前状态：行为不变，测试固化。

## 9. 测试和验收证据

| 测试文件 | 重点 | 结果 |
| --- | --- | --- |
| `tests/test_multi_agent_planning.py` | 精确 query 确定性委派 data_analyst、`model.calls==0`；excel+xlsx 变体；裸 xlsx 引用走 knowledge_expert；负例（查看代码仓库/数据库索引原理/查天气）仍走模型 | 通过 |
| `tests/test_stage2_5_wp6_e2e.py` | 真实 Run 执行精确 query：planning_calls==0、data_analyst/synthesis 各 1 次、唯一 OUTPUT、Memory exchange 完整；planner system prompt 含 capability 白名单 | 通过 |
| `tests/test_frontend_multi_agent_status.py` | INVALID_CAPABILITY/PLANNING_MODEL_FAILED/PLANNER_SCHEMA_INVALID 文案与 RUN_COMPLETED 渲染 | 通过 |

回归与全仓：

| 命令 | 结果 |
| --- | --- |
| 修改前全仓基线 | 1412 passed, 42 subtests passed |
| `uv run pytest -q`（修改后） | 1417 passed, 42 subtests passed |
| `uv run python -m compileall -q core tests server.py main.py` | PASS |
| `git diff --check` | PASS（仅 Git LF/CRLF 提示，无 whitespace error） |

未执行 git commit/push（等待授权）。

## 10. 当前边界与下一步

已解决：

- 用户原 query 现在确定性路由到 data_analyst（零模型调用），不再触发
  INVALID_CAPABILITY；
- 同类“查/查询 + 数据库/csv/excel + 文件引用”表达被路由层消化；
- 模型路径的 capability 发明概率因提示词白名单下降；
- 规划类失败在前端有可读、可安全重试的文案。

未解决 / 后续选项：

- 深度收紧（禁止 Planner 声明 capabilities 字段，能力由 Registry 推导）
  未实施，留作后续评审选项；
- 模型仍可能对非数据表达发明未知 capability，届时仍会 fail-closed
  （设计行为）；
- Stage 2.5 既有 Known Limitations 不变（跨进程不恢复、不承诺
  exactly-once、DELIVERED 非用户确认等）。

## 11. 面试表达版本

### 11.1 30 秒版本

用户输入“查数据库，mock_test_results.csv这个表的第四列的表头是什么”被
拒绝，原因是确定性路由没接住，模型声明了 Registry 白名单外的 capability，
编译器按合同 fail-closed 抛 `INVALID_CAPABILITY`，前端只显示“运行失败”。
修复是三层组合：路由层给 data_analyst 加“数据库/csv/excel”别名并把
“查/查询”和 csv/xlsx 纳入确定性判定（该 query 零模型调用）；Planner
提示词加 capability 白名单；前端补规划错误文案。全仓 1417 passed，
未知 capability 仍 fail-closed，没有放宽合同。

### 11.2 2 分钟版本

先讲根因链：`_deterministic_core_decision` 有三处没接住——“查”不在委派
动词集合、“.csv”不在文档引用扩展名集合、“数据库”不是 data_analyst 别名，
所以请求落入模型路径；模型 DELEGATE 声明的 capability 不在 data_analyst
的 `{"data_analysis"}` 白名单里，`PlanCompiler` 抛 `INVALID_CAPABILITY`，
映射为 `PLANNING_FAILED`；前端没有该错误码文案，显示“运行失败”。

再讲取舍：备选方案里有“深度收紧 planner schema 禁止 capabilities 字段”，
能根除模型发明能力，但要改 WP1 合同和约 23 个测试文件；也有“编译器忽略
未知 capability”，那会违反 fail-closed 合同。最终选择组合拳：路由层消化
常见数据表达（零模型调用）、提示词白名单降低发明概率、前端文案可读化，
同时保留编译期防线。

最后给证据：单元测试覆盖正例与负例（负例仍走模型），E2E 证明原 query
现在 `planning_calls==0` 且成功交付，前端文案断言通过；全仓从 1412 升到
1417 passed，compileall 与 diff check 通过；未提交，等待授权。

### 11.3 深入追问主线（含参考答案）

1. 为什么说“被安全拦截”不完全准确？
   - 回答：这是规划/编译合同的 fail-closed，不是敏感词安全扫描。
     `INVALID_CAPABILITY` 由 `PlanCompiler._compile_delegated` 在
     `plan_compiler.py:270` 抛出，因为模型声明的 capability 不在
     `data_analyst` 的注册能力集合 `{"data_analysis"}` 中。合同要求模型
     无权发明 Agent 或能力，所以拒绝是设计行为；问题在于常见数据请求本
     不该落到模型路径。
2. 为什么加“查”进委派动词不会把无关请求都委派出去？
   - 回答：确定性判定要求“命中数据别名”与“命中委派动词/文件引用”同时
     成立（`explicit = bool(selected) and (verb or docref)`）。只有“查”
     而没有数据库/csv/excel 别名（如“查看这个代码仓库结构”“查一下天气”）
     时 selected 为空，仍然走模型。负例已写进
     `test_multi_agent_planning.py`。
3. 为什么不做“禁止 Planner 声明 capabilities”的深度收紧？
   - 回答：那是更彻底的修复，但改变 WP1 的 typed schema 合同，并波及
     共享 fixture 与约 23 个测试文件，需要单独评审。本次在不放宽校验的
     前提下用“路由消化 + 提示词约束”降低触发概率；深度收紧作为后续
     选项记录，未擅自扩大范围。
4. Planner 提示词改了之后，编译失败还会发生吗？
   - 回答：可能。提示词是降低概率的手段，模型不保证遵守；`capabilities`
     字段仍在 schema 允许范围内，编译器仍会在越权时 fail-closed。合同
     防线没有因提示词而放宽，这也是“推荐组合”而不是“只改提示词”的原因。
5. 这次修复改了什么合同、没改什么？
   - 回答：改了确定性路由规则（动词/扩展名/别名，属于既有 deterministic
     rules 路径）、Planner 提示词、前端安全文案映射；没改 planner schema
     version、没改 `PlanCompiler`/`StrictPlanningDecisionParser` 校验、
     没改 Registry 能力集合、没改 Journal/Event/Trace/Metrics 合同。
6. 为什么规划失败文案可以提示“换一种说法再试”，而 delivery unknown 不行？
   - 回答：规划失败发生在任何 Step 启动前，无正文、无 Memory 副作用，
     安全重试成立；delivery unknown 表示正文可能已 journaled/部分送达，
     重试会重复用户可见文本，所以文案必须“先检查当前对话，避免重复执行”。
     两类文案规则在 `multi_agent_status.py` 中分开维护。

## 12. 最终验收结论

```text
本次修复 status: PASS
真实用户场景复现: 已定位并复现（run_id=cfc9f1861e074bca811b173236dd68cb）
根因: 确定性路由未命中 -> 模型声明未注册 capability -> 编译 fail-closed(INVALID_CAPABILITY)
修复组合: deterministic routing + planner prompt whitelist + frontend copy
未知 capability 仍 fail-closed: YES
新增 Agent/数据库能力: NO
架构偏差: 0
专项测试: 3 文件 41 passed
全仓测试: 1417 passed, 42 subtests passed
compileall: PASS
git diff --check: PASS
已提交: NO（等待授权）
Ready for GPT review: YES
```
