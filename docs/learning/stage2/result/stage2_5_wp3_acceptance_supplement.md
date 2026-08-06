# LocalAgent Stage 2.5 Multi-Agent WP3 Acceptance Supplement

## 1. 问题根因

WP3 结果文档此前声明“specialist 调用沿用 `complete_single_agent` 的 history 加载行为，完整‘specialist 不读取 Memory’边界属于 WP5”，这与 Stage 2.5 最终共识冲突：specialist 只应接收当前 Run 为它组装的 instruction/user context，旧 Run Memory 不得成为隐藏输入来源。

真实根因位于 `AgentRouter` 的消息组装链：

```text
AgentRouter.complete_single_agent（core/agent_router.py:1499）
-> _run_agent_once（:1452）
-> _complete_final_response（:1219）
-> _prepare_answer_messages（:1095）
-> _build_messages（:737）
```

`_build_messages` 存在两处不受 `persist` 控制的 Memory 读取：

1. 无条件调用 `memory_manager.get_chat_history(agent_id, ...)`（`core/agent_router.py:758` 附近），把该 Agent scope 的旧 user/assistant 消息拼进模型 messages；
2. 当 `history_scope == DIRECT_MEMORY_SCOPE` 时调用 `_update_summary_if_needed(agent_id)`（`core/agent_router.py:752`），读取并可能写入滚动摘要；knowledge 路径还会把 `summary_text` 作为 `knowledge-memory-summary` context item 注入 RAG 上下文。

`persist=False` 只守卫 `_run_agent_once` 中 user/assistant 的 `memory_manager.add_message`（`:1470-1476`、`:1490-1496`），即“禁止写入”，从未影响“读取”。Tool 规划 `_plan_tool_call(messages, agent_id)`（`:1063`）同样使用含历史的 messages，因此工具意图判定也见过旧历史。

结论：内部 specialist/synthesis 调用此前虽然不写 Memory，但仍会读取各自 Agent scope 的旧 Memory 与滚动摘要，违反最终合同。

## 2. 修改前真实数据流

```text
complete_single_agent(agent_id, instruction, persist=False)
-> _run_agent_once
   -> [persist=False：不写 user/assistant]
   -> _complete_final_response(history_scope=DIRECT_MEMORY_SCOPE)
      -> _prepare_answer_messages
         -> _build_messages
            -> _update_summary_if_needed(agent_id)   # 读取并可能写滚动摘要
            -> get_chat_history(agent_id, ...)       # 无条件读取旧历史
            -> [knowledge] RAG query = 当前 instruction（合法）
                但 memory summary 仍注入 RAG context（非法隐藏输入）
            -> messages = system + history + user(instruction)
         -> _plan_tool_call(messages)                # 工具规划也看到旧历史
```

## 3. 最终 History Policy 合同

新增显式 typed policy（`core/runtime/history_policy.py`）：

```python
class HistoryPolicy(str, Enum):
    AGENT_SCOPE = "AGENT_SCOPE"
    NONE = "NONE"
```

合同要点：

- `AGENT_SCOPE`（默认）：保持原行为——读取 Agent scope 历史并执行滚动摘要维护；Legacy/direct 路径不变。
- `NONE`：不读取任何 Memory 历史，也不触发滚动摘要维护（既不读、也不因摘要维护写入）。
- 与 `persist` 正交：`persist` 控制写入，`history_policy` 控制读取，命名不混为一谈。
- 调用合同：`complete_single_agent(..., persist=False, history_policy=HistoryPolicy.NONE)`。
- internal specialist/synthesis Adapter 显式传 `HistoryPolicy.NONE`，不依赖调用方忘记传参后的偶然行为。
- 禁止通过调用后删除 Memory 实现；禁止读取后仅在 Prompt 中要求模型忽略。

## 4. Specialist 实际输入边界

内部 specialist 实际模型输入只允许来自：

- 当前 `AgentExecutionRequest.instruction`；
- 当前 RunContext；
- 当前 Agent 合法的 Tool/Retrieval 输入；
- 必要的固定 System Prompt。

不得来自：

- 以前 Run 的 user/assistant Memory；
- Legacy orchestration Memory；
- 其他 Agent 历史；
- 全量会话摘要；
- 未显式授权的历史检索结果。

这不禁止 `knowledge_expert` 使用当前请求触发的 RAG：RAG query 来自当前 instruction（`_execute_knowledge_retrieval` 直接使用 `user_query`，`core/agent_router.py:612`），测试已证明其仍可用且不携带旧 Memory。

## 5. 修改文件

| 文件 | 职责 |
| --- | --- |
| `core/runtime/history_policy.py` | 新增 `HistoryPolicy`（AGENT_SCOPE / NONE）。 |
| `core/runtime/__init__.py` | 导出 `HistoryPolicy`。 |
| `core/agent_router.py` | `_build_messages` / `_prepare_answer_messages` / `_complete_final_response` / `_run_agent_once` / `complete_single_agent` 增加 `history_policy` 参数并透传；`_build_messages` 按 policy 守卫历史读取与滚动摘要维护。 |
| `core/runtime/agent_adapter_factory.py` | `AgentRouterSingleAgentAdapter` 显式传 `history_policy=HistoryPolicy.NONE`。 |
| `core/runtime/synthesis.py` | `SynthesisAgentAdapter` 显式传 `history_policy=HistoryPolicy.NONE`。 |
| `tests/test_wp3_history_boundary.py` | 新增 8 个验收补充测试（真实 `AgentRouter` + 真实 `MemoryManager`，仅外部模型 fake）。 |

## 6. 测试和命令

| 命令 | 结果 |
| --- | --- |
| `uv run pytest -q tests/test_wp3_history_boundary.py` | 8 passed |
| WP3 + AgentRouter/Knowledge/Legacy 组合（history boundary、StepResult、Store、Factory、Driver、Synthesis、Committer、Multi-Agent E2E、Security、dynamic lifecycle、knowledge routing、agent loop、runtime mode E2E） | 136 passed, 3 subtests passed |
| `uv run pytest -q` | 1273 passed, 42 subtests passed |
| `uv run python -m compileall -q core tests server.py main.py` | PASS |
| `git diff --check` | 无 whitespace error（仅 Git LF/CRLF 提示） |

## 7. Legacy/direct 兼容证据

- 默认 `AGENT_SCOPE` 仍读取历史：`test_default_agent_scope_still_reads_history` 断言旧标记出现在模型 messages。
- Legacy 回答读取路径：`test_legacy_answer_read_path_keeps_history` 直接调用 `_prepare_answer_messages`，旧标记仍在 messages。
- 显式单 Agent（`code_expert` entry）：`test_direct_single_agent_keeps_history_behavior` 断言历史仍被读取且 `persist=True` 的 user/assistant 写入保持（消息数 1 -> 3）。
- Core direct：`test_core_direct_keeps_history_behavior` 断言历史仍被读取、写入保持、Run SUCCEEDED。
- WP1/WP2/WP3 原测试全部继续通过（全仓 1273 passed）；Shape 2/3 仍真实并行、synthesis 恰好一次、无 `OUTPUT_DELTA`、以 `FINAL_OUTPUT_PIPELINE_NOT_READY` 结束。

## 8. 安全标记测试

向 specialist Agent scope 预先写入：

```text
SECRET_OLD_AGENT_MEMORY_MUST_NOT_BE_READ
```

运行真实 Shape 3 主链（code + data -> synthesis），断言：

- specialist 模型 messages 中没有该标记；
- specialist result 中没有该标记；
- synthesis 输入中没有该标记（只含依赖结果与当前 instruction）；
- Runtime Event、Journal、Trace、structured log、Snapshot 中没有该标记；
- specialist 调用结束后没有新增 raw specialist Memory（各 Agent scope 消息数保持 1）。

另有 router 级测试证明：`HistoryPolicy.NONE` 下当前 Binding instruction 正常进入模型；`AGENT_SCOPE` 默认下旧标记出现（证明策略真实生效）。

## 9. 是否触及 WP4

否。本轮未实现 OutputGate、DeliveryStatus、user-visible multi-agent final、最终 answer Memory 写入、partial publication、Adapter 结果重试、optional dependency、Store 持久化/恢复或前端修改。多 Step Run 仍以 `FINAL_OUTPUT_PIPELINE_NOT_READY` 在 WP4 交付前 fail closed。

## 10. Remaining P0/P1/P2

- P0：0
- P1：0
- P2：1（WP3 已知 Planning 饥饿容量风险：Planning 与 specialist 执行共享 bounded executor，`PLANNING_MODEL` 无保底容量；本轮未新增 P2）

```text
WP3 supplementary review: PASS
Specialist history read disabled: YES
Specialist result persistence disabled: YES
Synthesis full-memory access disabled: YES
Legacy history behavior preserved: YES
Direct single-Agent history behavior preserved: YES
Architecture deviations: 0
P0 findings: 0
P1 findings: 0
P2 findings: 1
Ready to start WP4: YES
```
