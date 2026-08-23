# Runtime State Machine

## State owner
AgentState 是 Run 与 Step 运行状态的唯一 owner。Plan 和 PlanStep 是不可变定义，不保存 runtime status。

## Event sequence
RuntimeEventChannel 拥有单个 Run 的 event sequence。sequence 单调递增，已经消费的序号不能复用。

## Recovery
RecoveryValidator 只读 Snapshot、Plan 与 Journal，执行 validation-only 检查，不 replay，也不写回 AgentState。
