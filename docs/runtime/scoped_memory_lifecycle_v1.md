# Scoped Memory Lifecycle v1

## Authority 与身份

`MemoryAccessContext` 只从 HTTP `request.state.principal` 或受信内部 Composition 获得，独立参数贯通 Application、ChatService、RuntimeFactory；不接受正文 tenant/user/grant 授权。SESSION = tenant/user/session/agent，USER = tenant/user/agent，PROJECT = tenant/project/agent。同 tenant ADMIN 不获得他人私人记忆；SERVICE 只使用已持久配置的精确 PROJECT action grant。

`PostgresMemoryManager.lifecycle` / `MemoryLifecycleRepository` 是既有 PG 表的 scoped 事务入口；MemoryService 复用 strict Formation parser、ContextBuilder 与 ModelInvocationRouter。没有新连接池、Scheduler、Terminal Owner 或 Go 服务。SQLite 留作明确本地/测试存储，不作生产 fallback。

`persist=True` 必须有可信身份。明确 `persist=False` 的 Run 绑定 STATELESS，不读历史/摘要/Memory，不写 Exchange、不 Formation。无可信来源的历史 scoped 字段为 NULL，查询排除，不自动赋予当前用户。

## 存储、事务与恢复

0033 增量建立 `memory_scopes`、`memory_operations`、`runtime_memory_bindings`，扩展原 Memory/Message/Exchange/Summary；Summary 改 scope 唯一身份，但 scoped 路径关闭滚动摘要，读取有界有效 Exchange。Model Attempt 增加 FORMATION purpose/operation FK，与 DECISION、COMPACTION 区分；不更改冻结的三个 Evidence 协议。

Formation 在模型调用前 PREPARED；候选持久 CANDIDATE；Record 和 operation receipt 同事务 COMMITTED。operation/candidate-slot/record identity 稳定；同 digest replay 返回原结果。UNKNOWN 不重新提取；已有真实 COMPLETED 模型结果可复用后继续保存候选，不能再收费。整个批次 CAS 冲突回滚，没有部分候选落库。

USER_ASSERTION 的独立来源按批准例外实现：操作的准入/提交仍验证完整 Run/Exchange 依赖；模型提取只接收 digest 核验的认证用户原话，Final 参数固定为空。仅该代码路径产生的 assertion Record 可以绑定 USER_MESSAGE（role=user、sequence=0、已提交 Exchange、原始消息 digest/version、空派生依赖）及 Scope manifest。模型不能选择该模式或收窄来源。这样明确更正不会因 supersede 旧输入而使新陈述自我失效，也不会把旧 Final 洗成事实；它不证明语义等价或用户陈述真实。提取前的来源模式/版本快照在 operation receipt 保留。

相同 Scope 行串行化空 keyed partition；PG partial unique index 只允许一个 ACTIVE key。record_version CAS 更正/删除；明确更正提示、预提取版本快照和当前用户 UPDATE 权限由代码裁决，模型不能提供版本/Scope。INFERENCE 只允许短 TTL CANDIDATE，默认检索排除；自动 episode lesson 不保存。当前没有启用受信 tool observation extractor，OBSERVATION 候选入口拒绝，不把任意 Tool 文本当事实。

锁序为 control → root → frame/iteration → 排序 Scope → Memory/operation；独立 mutation 不反锁 Run。网络 I/O 在事务外。Run Binding FK 指向 control，可在 Planner 前保存；T0 同事务绑定 Seed digest，后续只验证原绑定，不重读 Memory 或改写 Seed/Snapshot。

## TTL、删除和传播

SQL 在排名和 LIMIT 前过滤授权 Scope、ACTIVE/PRESENT、TTL、source/version/epoch/suppression；来源 manifest 有界且扁平闭包，超过界限拒绝，不截断。会话历史只读本 SESSION，长期 USER 可跨本人 Session。默认 SESSION 24h、USER 30天、INFERENCE 30分钟；已知 Session expiry 进一步夹紧。依赖 Exchange 到期也会阻断长期记录使用，因此实际使用期限可能更短。PROJECT TTL 必须由受信 Composition 显式配置（最长30天）。

DELETE_MEMORY 仅支持 `MEMORY_STORE_AND_FUTURE_USE`：同事务清 canonical/typed payload、相关历史版本和候选正文，保留 digest/版本/抑制身份；依赖记录及会话历史不能再入 Prompt。SESSION Exchange 删除清消息正文和相关候选/派生 canonical，保留 Exchange 抑制身份。REVOKE 阻断使用，可保留受控正文。旧 source 不能通过新 UUID/slot/重放复活。私人 Memory 失效同步抑制其原 SESSION 来源的未来 history/Writer 重用；PROJECT 删除不反向撤销私人上游，新 promote 也必须检查目标 Scope 的 source suppression。

回执 `COMPLETE_FOR_DECLARED_SCOPE` 明示 `all_copies_erased=false`、历史执行正文 `RETAINED_RESTRICTED_NO_REUSE`、备份 `UNKNOWN`。`ALL_COPIES` 在写入前返回 `MEMORY_ERASURE_SCOPE_UNSUPPORTED`。未知 operation receipt 返回 UNKNOWN/PARTIAL，不猜成功。

## 每次新使用资格

原 Model Owner 每次实际 attempt/retry/fallback（含 Planner、Decision、Compaction、Formation）检查 Binding。Step 模型 STARTED 和 Tool STARTED 在原事务内验证；实际 dispatch 前再查。新 Decision 接受、Snapshot 接受和未提交 Final 同样受 guard 约束。撤销在共同 Scope 锁上与新准入串行化；先准入的在途请求仍可能返回或收费，不持锁跨 I/O，不承诺远端擦除。

Planner 在 root 前沿 Binding 保存稳定 preplan admission/completion digest；接管后无可安全恢复的 Planner 正文时 UNVERIFIABLE，禁止重发。此事实不成为新执行器或结果 Authority。STARTED/UNKNOWN Tool 继续原 reconciliation/suspension，完成事实可以保存；STARTED 后 dispatch 前资格拒绝也保守保持 UNKNOWN、单次预算结算，不进入 Tool retry；最终停止由原 Coordinator/Step/Run 路径执行。

已交付 Final receipt/event/cursor 保留；失效后的 Exchange/Formation 可跳过，不改变已交付成功。旧 Run 无 Binding 时不能推断 STATELESS，drain 或 UNVERIFIABLE 停止新使用。

## 接口和 Breaking Change

- 普通 `/api/chat`、`/v1/chat`、`/api/runtime/execute` 接受有界 `session_id`；CONVERSATION 绑定真实 SESSION identity。租户/用户等额外正文断言拒绝。
- `/api/history/{agent_id}?session_id=...` 返回当前用户的该会话有效历史。
- 旧全局 `/api/search`、GET/DELETE `/api/memory` 返回410；原 PG agent-only APIs 返回 `MEMORY_SCOPED_ACCESS_REQUIRED`，agent-only authorizer 不再授权自己。
- GET `/api/memory/scoped/{agent_id}` 读本人及受信 PROJECT grant 下记录；POST `/api/memory/scoped/{agent_id}/mutations` 需要 exact record/Scope、expected_version 和 command_id；Receipt 查询保持同 Scope 授权。PROJECT opaque Scope ID 仍须服务端校验，不能授予权限。
- 聊天 forget 不再由模型按 agent/key 执行旧全局删除，返回 Formation 显式 mutation 要求；使用 scoped mutation API。旧桌面全局记忆弹窗尚未适配新接口。
- Project grant/TTL 配置、显式 promote 和 pending source suppression 仅提供受信内部接口；无企业 IAM 或隐式共享。

## 本地 Evidence

`memory-lifecycle-evidence.v1` 由真实 Scope、operation、record、Run Binding、Snapshot 与实际 Model Invocation 绑定投影。只输出 opaque/digest/枚举/版本/CAS/TTL/传播状态，禁止正文、原 user/tenant、logical key 和自由错误。原 Run 授权及可信绑定控制查询；seal-only，不补写结果/派发/Formation，缺证据或非终态只能 PARTIAL。

核查节点位于 `tests/test_scoped_memory_lifecycle.py`：四组、八个节点，真实隔离 PG 并发及工具 Observation、Formation response 两个精确窗口的进程退出/恢复；实施证据见 WP4 handoff。没有全仓回归或跨仓协议变更。
