# 1. 一句话项目 / 工作包定义

WP4-A 的目标不是“把 Trace（追踪）发出去”，而是先为 LocalAgent 已有 Runtime Trace 建立一个**稳定、安全、可版本化、可被外部系统消费的兼容合同边界**：

```text
内部 Trace Contract v1 / SpanRecord
        ↓
严格安全投影
        ↓
Trace Export Contract
        ↓
TraceExportEnvelope
        ↓
Trace Contract Fingerprint
```

最终实现了：

- Trace Contract v1（追踪合同 v1）正式 `PUBLIC_VERSIONED`；
- consumer-neutral Trace Export Contract（消费者中立追踪导出合同）；
- immutable TraceExportEnvelope（不可变追踪导出信封）；
- strict category / field / value-domain validation（严格类别 / 字段 / 值域校验）；
- deterministic Trace Contract Fingerprint（确定性追踪合同指纹）；
- fail-closed compatibility evaluation（失败关闭式兼容性判定）。

独立 Final Re-Gate（最终重新门禁）最终 PASS，`P0=0 / P1=0 / P2=2`，`CAPABILITY_GAP=0 / TEST_GAP=0 / DOC_DRIFT=0 / ENVIRONMENT_BLOCKED=0`。

------

# 2. 为什么要做 WP4-A

Stage 2.5 已经有 Trace Contract v1 和六类稳定 Span（跨度）：

```text
runtime.run
runtime.planning
runtime.step
runtime.synthesis
runtime.output_delivery
runtime.final_memory_commit
```

但最初 Scout（侦察审计）发现，它只解决了**Runtime 内部如何记录 Trace**，还没有回答“未来 AgentEvalOps 或其他消费者如何可靠消费 Trace”。

当时主要缺口有：

```text
Trace Contract Fingerprint      ABSENT
Export Envelope                 ABSENT
External compatibility rules   ABSENT
Canonical export serializer     ABSENT
```

而且完整 `SpanRecord` 只是 memory-only（仅内存），Journal（日志账本）只持久化 RuntimeEvent（运行时事件）的安全投影和 span correlation IDs，并不是完整 Trace Store（追踪存储）。

所以 WP4-A 本质是在解决：

> **“内部 Trace 数据存在”与“它已经成为稳定的跨系统数据合同”是两回事。**

这也是面试时最值得强调的工程判断。

------

# 3. 真实性与完成边界

这一 WP 必须严格区分不同来源。

## 已真实实现

当前真实实现：

- `RunCoordinator` root Span 生命周期 cleanup；
- `TraceExportEnvelope`；
- strict export projection（严格导出投影）；
- `_validate_envelope_semantics()` 单一语义校验 Owner；
- `TraceCompatibilityEvaluator`；
- category-specific schemas（分类特定 schema）；
- public value-domain validation（公共值域校验）；
- `export_contract_semantic_descriptor()`；
- `TraceContractFingerprinter`；
- `SHA-256 + canonical_json_v1`；
- fingerprint compatibility rejection；
- metadata-first（元数据优先）安全边界。

## 已真实测试

最终全量：

```text
2305 passed
42 subtests passed
0 failed
0 skipped
0 xfail
```

并有独立直接对抗探针，而不只是单元测试。

## 实施 / 审计真实发现

四个 P1：

```text
P1-01 RunCoordinator Trace Context cleanup leak
P1-02 TraceExportEnvelope validation bypass
P1-03 export attribute domain not strict
P1-04 fingerprint domain coverage gap
```

四项最终全部 CLOSED。

## 不是生产事故

尤其注意：

- P1-01：源码审查 + direct probe（直接探针）；
- P1-02：源码审查 + direct probe；
- P1-03：源码审查 + direct probe；
- P1-04：源码审查 + direct synthetic probe（直接合成探针）。

**都不能说成线上生产事故。**

其中 P1-02/03 发现时 WP4-B exporter（导出器）甚至还没有实现，因此没有真实数据已经通过 exporter 泄漏出去。最初 Final Gate 也明确这样分类。

------

# 4. 修改前架构与根因

WP4-A 之前的核心问题不是“Trace 不存在”，而是：

```text
Runtime Trace
≠
Public Export Contract
```

内部 `SpanRecord` 带有：

- Runtime 生命周期属性；
- 高基数 ID；
- 内部安全字段；
- 运行时 outcome；
- timestamps；
- duration；
- Internal-only attributes。

其中“内部可以安全记录”并不意味着“适合长期对外公开”。

所以一个核心原则是：

```text
SAFE_SPAN_ATTRIBUTES
!=
PUBLIC EXPORT ATTRIBUTE SET
```

内部记录安全集合只是最大内部边界，而公共合同必须更严格、更稳定。

如果直接把 `SpanRecord` 作为外部 Payload（载荷），就会把：

```text
Runtime 内部模型演进
```

和：

```text
外部消费者兼容性
```

强耦合。

因此 Architecture Decision（架构决策）明确拒绝：

```text
SpanRecord -> 直接公开
```

而采用：

```text
SpanRecord
  ↓
safe projection
  ↓
TraceExportEnvelope
```



------

# 5. 方案讨论与取舍

## 方案 A：直接公开 SpanRecord

优点：

- 实现简单；
- 几乎不用额外 schema。

问题：

- Runtime 内部字段变化直接影响外部；
- Internal-safe 字段可能被误公开；
- 无法稳定 required / optional / conditional；
- 无稳定兼容机制；
- transport 与 Runtime model 容易耦合。

最终拒绝。

------

## 方案 B：重新设计 Trace Contract v2

问题更大。

Stage 2.5 的 Trace Contract v1 已经存在六个稳定 operation，并且已有大量测试，没有证据要求推翻重做。

因此 WP4-A 明确采取：

> **不重写内部 Trace，而是在外面新增 export-facing compatibility layer（面向导出的兼容层）。**

这使 Stage 2.5 frozen boundary（冻结边界）不受破坏。

------

## 最终方案

```text
Trace Contract v1
        ↓
Internal SpanRecord
        ↓
strict projection
        ↓
Trace Export Contract
        ↓
TraceExportEnvelope
        ↓
Trace Contract Fingerprint
```

并保持：

```text
RuntimeEvent → event order
Journal      → durable event facts
Metrics      → aggregation
Trace export → topology/timing/status/safe metadata
```

没有制造新的 All-Runtime SSoT（全运行时唯一事实来源）。

------

# 6. 最终架构

最终合同分类：

| Surface                                  | 最终状态                       |
| ---------------------------------------- | ------------------------------ |
| Trace Contract v1                        | `PUBLIC_VERSIONED / SUPPORTED` |
| `TraceContext / SpanHandle / SpanRecord` | `INTERNAL`                     |
| Trace Export Contract                    | `PUBLIC_VERSIONED / SUPPORTED` |
| Trace Contract Fingerprint               | `PUBLIC_VERSIONED / SUPPORTED` |
| Exporter Interface                       | `NOT_IMPLEMENTED`              |
| AgentEvalOps Adapter                     | `NOT_IMPLEMENTED`              |
| Trace Instance Fingerprint               | `NOT_IMPLEMENTED`              |
| Run Configuration Fingerprint            | `NOT_IMPLEMENTED`              |
| OpenTelemetry / OTLP                     | `NOT_IMPLEMENTED`              |



当前最重要的 Owner（所有者）关系：

```text
trace_contract.py / tracing.py
        ↓
Runtime Trace semantics

trace_export_contract.py
        ↓
Public Export Contract semantics
        ↓
export_contract_semantic_descriptor()

trace_contract_fingerprint.py
        ↓
canonicalization + hash
        ↓
TraceContractFingerprinter

TraceCompatibilityEvaluator
        ↓
compatibility decision
```

注意这里的设计演进：

**Fingerprint Owner 不应该再拥有一份独立的合同描述。**

合同语义只能有一个 Owner。

------

# 7. 核心状态机和时序

这一 WP 没有新增大的 Runtime 状态机，但有三个关键时序。

## 7.1 Span 生命周期

```text
start root span
    ↓
install TraceContext
    ↓
install SpanRecorder
    ↓
execute Run
    ↓
finally
    ├─ finalize root span
    ├─ reset TraceContext token
    └─ reset SpanRecorder token
```

P1-01 修复后的关键不是“发生错误时调用一次 cleanup”，而是：

> cleanup 被结构性放入 `finally`，使所有 root Span 安装后的传播路径都无法绕过。

最终独立探针验证：

- registration failure；
- nested ContextVar；
- budget snapshot failure；
- sequential Run；
- first-end-wins。

全部通过。

------

## 7.2 Export projection

```text
completed SpanRecord
        ↓
operation/category resolve
        ↓
approved attribute projection
        ↓
_validate_envelope_semantics()
        ↓
TraceExportEnvelope
```

------

## 7.3 Compatibility

```text
Envelope
  ↓
identity
  ↓
version
  ↓
fingerprint
  ↓
full envelope semantics
  ↓
ACCEPT / REJECT
```

这是 R1 后非常关键的变化。

原先的错误逻辑实际上相当于：

```text
正确 fingerprint
=
合法 envelope
```

但 fingerprint 只证明：

> “你声称自己遵循哪个合同。”

它不能证明：

> “这份具体 payload 真的符合这个合同。”

所以最终：

```text
known fingerprint + invalid envelope
→ REJECT(ENVELOPE_INVALID)
```



------

# 8. 数据 / 权限 / Owner 边界

WP4-A 没有改变 Runtime authority（运行时权威）。

## Trace Export Contract 不拥有

- Run terminal；
- AgentState；
- Event sequence；
- Journal durability；
- Tool permission；
- Resource Authorization；
- OutputGate；
- Memory commit。

它只拥有：

```text
哪些 Trace facts 可以公开
这些 facts 的字段规则
这些 facts 的值域规则
兼容语义
```

这是一种非常典型的**Data Contract Owner（数据合同 Owner）**。

------

## Event / Journal 边界

```text
RuntimeEventChannel
= authoritative event sequence owner

Journal
= durable event facts owner

Trace export
= topology / timing / status / safe span metadata
```

因此不能因为 Trace Export 需要顺序，就把 Event sequence 搬进 Envelope，形成两个顺序 Owner。

------

# 9. 兼容策略

WP4-A 的兼容策略是面试核心。

## Contract Version 与 Fingerprint 分工

```text
Contract Version
= compatibility family / major semantic version

Fingerprint
= 这个版本下精确的 schema + semantic contract identity
```

例如：

```text
TRACE_EXPORT_CONTRACT_VERSION = 1
```

但 v1 里增加一个新的 optional field，即使仍然可以认为属于 v1：

```text
fingerprint 仍然必须改变
```

consumer（消费者）必须明确支持这个 fingerprint，而不是看到 version=1 就盲目接受。

------

## 当前 fingerprint

```text
6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab
```

算法：

```text
SHA-256
```

Canonical encoding（规范编码）：

```text
canonical_json_v1
```

旧 fingerprint：

```text
3e19161d...
```

最终明确：

```text
old fingerprint
→ REJECT(FINGERPRINT_UNSUPPORTED)
```

没有增加 legacy alias（遗留别名）。

为什么不升 v2？

因为旧 v1 从未通过 Final Gate，也没有 WP4-B exporter、没有真实外部 AgentEvalOps consumer，因此修复的是**尚未发布的 v1 实现缺陷**，不是发布后的 breaking migration（破坏性迁移）。

------

# 10. Bad Cases / 真实发现

这是 WP4-A 最有面试价值的一节。

## P1-01：Trace cleanup leak

### 真实性

```text
SOURCE_AUDIT_FINDING
+
DIRECT_PROBE_FINDING
```

不是生产事故。

### 问题

某些 `RunCoordinatorError` 在 root Span 和 ContextVar 已安装后直接重新抛出，会绕过尾部 cleanup。

结果：

```text
active_span_count = 1
TraceContext 泄留
SpanRecorder ContextVar 泄留
```

### 根因

cleanup 在正常尾部，不是结构性 `finally`。

### 修复

统一 outer `finally`。

### 知识点

- Resource lifecycle（资源生命周期）；
- ContextVar Token restoration；
- first-end-wins；
- exceptional control-flow ownership。

------

## P1-02：TraceExportEnvelope validation bypass

### 真实性

```text
SOURCE_AUDIT_FINDING
+
DIRECT_PROBE_FINDING
```

不是生产事故。

### 原问题

`project_span()` 很严格。

但：

```text
TraceExportEnvelope(...)
```

可以直接构造。

原 `frozen=True` 只是防字段重新赋值，却不能阻止：

```text
attributes = {"x": [mutable list]}
```

而且 shallow `MappingProxyType`（映射只读代理）不能冻结嵌套对象。

更严重的是：

```text
known identity/version/fingerprint
+
invalid envelope
→ compatibility ACCEPTED
```

原 Final Gate 因此 FAIL。

### 修复

统一 `_validate_envelope_semantics()`：

```text
direct constructor
project_span
compatibility evaluator
        ↓
same validation owner
```

### 知识点

> `frozen dataclass` ≠ structural immutability（结构不可变）。

------

## P1-03：Key 合法但 Value Domain 非法

典型案例：

```text
publish_attempt_count = 2
delivery_status = FABRICATED_SAFE_TOKEN
```

类型都“看起来合法”：

```text
2
→ non-negative int

FABRICATED_SAFE_TOKEN
→ safe identifier
```

但业务语义不合法。

这说明：

```text
Type validation
!=
Semantic domain validation
```

最终：

```text
delivery_status
→ DeliveryStatus public subset

gate_terminal_state
→ OutputGateState terminal subset

publish_attempt_count
→ 0..1
```

等 17 个 category-field pair 都有明确值域；独立 Re-Gate 共验证 62 个合法值接受、19 个伪造/越界值拒绝。

------

## P1-04：Fingerprint 没覆盖 Value Domain

这是本 WP 最适合高级面试追问的问题。

R1 修复 P1-03 后：

```text
delivery_status
只能是
DELIVERED / FAILED / OUTCOME_UNKNOWN
```

但是旧 fingerprint descriptor 仍只知道：

```text
delivery_status
= SAFE_IDENTIFIER
```

于是：

```text
Contract A:
publish_attempt_count ∈ {0,1}

Contract B:
publish_attempt_count ∈ {0,1,2}
```

可能拥有**同一个 fingerprint**。

这说明 fingerprint 虽然 deterministic，却没有真正表示完整语义合同。

Architecture Re-entry 最终确认：

```text
VALUE_DOMAINS_MUST_BE_FINGERPRINTED
```



------

# 11. 实际测试与 Gate

不要只说“全仓测试过了”。

更好的面试表达是：

> 我们先用独立 Final Gate 的 direct probe 把原实现打失败，再进行两轮 remediation，最后重新用独立 Re-Gate 攻击同样边界。

最终：

```text
Targeted P1:
113 passed

Existing Trace:
59 passed + 4 subtests

Event / Journal:
87 passed

Delivery / Memory:
29 passed

WP3 Security:
318 passed

Default Entry / Lifespan / Shutdown:
16 passed

Formal Docs:
18 passed

Full:
2305 passed
42 subtests
0 failed
```

Final Re-Gate 同时做了 direct probes：

- raw attribute；
- mutable nested attributes；
- unknown keys；
- step correlation；
- invalid current-fingerprint envelope；
- value-domain；
- fingerprint semantic sensitivity；
- old fingerprint rejection；
- cross-process determinism。

因此不是“单测绿所以 PASS”，而是**合同对抗测试 + regression gate（回归门禁）共同 PASS**。

------

# 12. Known Limitations

WP4-A 完成后仍有两个保留 P2。

## P2-02：Delivery / Memory negative-state asymmetry

某些：

```text
NOT_STARTED
rejected
not-attempted
no memory write
```

不会为了“可观察对称”强行制造：

```text
runtime.output_delivery
runtime.final_memory_commit
```

这是刻意接受的。

因为：

> **不能为了 Trace 看起来完整而伪造没有真实发生的业务执行。**

RuntimeEvent / Journal 已经拥有既有 layered facts（分层事实）。

------

## P2-03：Planning / Step error taxonomy

某些异常仍可能折叠成：

```text
UNHANDLED_ERROR
```

因此当前 Trace 不能声称：

```text
可以精确重建所有 typed root cause
```

这是 `DEFERRED_VERSIONED_ENHANCEMENT`。

------

此外仍然明确没有：

- exporter transport；
- AgentEvalOps Adapter；
- OpenTelemetry / OTLP；
- durable Trace Store；
- Trace recovery；
- Trace Instance Fingerprint；
- Run Configuration Fingerprint。



------

# 13. 体现了哪些工程能力

WP4-A 最能体现的不是“会写 Trace”，而是下面这些能力。

### 1. Contract Engineering（合同工程）

从内部模型拆出稳定外部接口，而不是直接暴露内部对象。

### 2. Compatibility Engineering（兼容性工程）

区分：

```text
version
fingerprint
runtime instance
```

### 3. Schema 与 Semantic Validation（语义校验）

认识到：

```text
type valid
!=
domain valid
```

### 4. Immutable Data Boundary（不可变数据边界）

认识到：

```text
frozen dataclass
!=
deep structural immutability
```

### 5. Single Source of Truth（单一事实来源）

最终形成：

```text
Export Contract Owner
→ semantic descriptor

Fingerprinter
→ hash only
```

而不是两个模块各维护一份 schema。

### 6. Failure-Oriented Review（失败导向审查）

Final Gate 真正把代码打 FAIL，然后继续修，而不是为了项目“好看”强行 PASS。

------

# 14. 30 秒面试回答

> 我在 LocalAgent 的生产化阶段做过一个 Trace Contract / Fingerprint 工作包。原来 Runtime 内部已经有六类稳定 Span，但还不能直接给外部评估平台消费，因为内部 `SpanRecord` 和公共数据合同是两层东西。我增加了 consumer-neutral 的 Trace Export Contract，通过严格的 category schema 和 value-domain 校验，把内部完成 Span 投影成不可变 `TraceExportEnvelope`，再用 canonical JSON + SHA-256 对合同语义生成 fingerprint。过程中独立 Final Gate 发现了直接构造 Envelope 绕过校验、合法类型但非法业务值域，以及 fingerprint 没覆盖值域三个 P1，我们做了两轮修复和架构回退，最终 Re-Gate 2305 个测试全部通过，P0/P1 都清零。这样后面的 exporter 和 AgentEvalOps 可以依赖稳定合同，而不需要依赖 Runtime 内部结构。

------

# 15. 2 分钟面试回答

> Stage 2.5 时我们已经有 Trace Contract v1，有 `runtime.run`、`planning`、`step`、`synthesis`、`output_delivery` 和 `final_memory_commit` 六类 Span，但完整 Span 只存在内存里，而且 `SpanRecord` 是 Runtime 内部模型，所以我在 Stage 3 做 WP4-A 时没有直接把它作为对外 payload。
>
> 我的设计是再增加一层 consumer-neutral Trace Export Contract：只有完成的 Span 才能经过严格投影成为 `TraceExportEnvelope`。内部 `SAFE_SPAN_ATTRIBUTES` 和公共导出集合明确分开，公共合同对 operation、step correlation、字段类型、字段 presence 和 value-domain 都做严格限制，同时所有 raw prompt、Tool Result、RAG、Memory、路径、异常正文等都不能进入公共 Envelope。
>
> 兼容方面我把 contract version 和 fingerprint 分开。Version 表示大的兼容族，fingerprint 表示当前 schema 和 semantic contract 的精确身份。Fingerprint 用 canonical JSON + SHA-256，但真正关键不是哈希算法，而是“哈希什么”。最开始我们就踩过这个坑：R1 修复了 `DeliveryStatus` 和 `publish_attempt_count` 的值域，但是 fingerprint descriptor 还没包含这些规则，所以两个接受边界不同的合同仍可能有同一 fingerprint。后来做了 Architecture Re-entry，把 export contract 变成唯一 semantic descriptor Owner，fingerprinter 只负责 canonicalize 和 hash。
>
> Final Gate 期间还发现过直接构造 `TraceExportEnvelope` 绕过 `project_span` 校验的问题，所以最后统一了 `_validate_envelope_semantics`，constructor、projection 和 compatibility evaluator 全走同一规则。
>
> 最终独立 Re-Gate P0=0、P1=0，只保留两个明确 P2，全量 2305 个测试通过。WP4-A 只冻结数据合同，不实现网络 exporter，后面的 WP4-B 再基于这个稳定 Envelope 实现传输。

------

# 16. 深入版本：Fingerprint 到底是什么

这部分很容易被问深。

## Fingerprint 不是数据哈希

错误理解：

```text
fingerprint = sha256(一次 Trace 的 JSON)
```

这其实是：

```text
Trace Instance Fingerprint
```

WP4-A **没有实现**这个东西。

真正实现的是：

```text
Trace Contract Fingerprint
```

它 hash 的是：

```text
什么字段存在
哪些 required / optional / conditional
字段类型是什么
字段值域是什么
有哪些 stable operations
哪些 operation 需要 step_id
哪些 enum 值合法
未知字段怎么处理
compatibility 怎么判
security policy 是什么
```

------

## Instance Fact 与 Contract Fact

这是必须熟练的概念。

### Instance Fact

```text
delivery_status = DELIVERED
publish_attempt_count = 1
status = ERROR
duration = 153ms
```

这是一次运行发生了什么。

**不进入 fingerprint。**

### Contract Fact

```text
delivery_status
∈ {DELIVERED, FAILED, OUTCOME_UNKNOWN}

publish_attempt_count
∈ [0,1]

status
∈ {OK, ERROR, CANCELLED, TIMED_OUT}
```

这是“什么数据算合法”。

**必须进入 fingerprint。**

P1-04 就是因为这两个概念最初混淆。

------

# 17. 高频追问

## Q1：为什么不能直接用 schema version？

因为同一个 major contract version 内仍可能发生精确语义变化。

例如增加一个真正 optional 字段：

```text
version 仍可能 = 1
fingerprint 必须改变
```

consumer 必须明确知道自己支持哪个精确合同。

------

## Q2：为什么不是直接 hash Python dataclass？

因为 Python Runtime object 里包含：

- runtime values；
- IDs；
- timestamps；
- mutable representation；
- dict/set ordering；
- 内部实现细节。

这些都会导致 fingerprint 与语义兼容性无关。

所以先构造：

```text
finite canonical semantic descriptor
```

再 hash。

------

## Q3：为什么选择 SHA-256？

不是因为“SHA-256 最先进”，而是：

- 项目已有 canonical JSON + SHA-256 precedent；
- 不需要新依赖；
- 这里需要 deterministic identity，而不是密码存储；
- 算法不是难点，**semantic source coverage 才是难点**。

这一句话很重要。

------

## Q4：为什么 CompatibilityEvaluator 还要重新验证 Envelope？Fingerprint 不够吗？

因为：

```text
fingerprint
= 合同身份

envelope validation
= 具体实例是否符合合同
```

比如某人伪造：

```text
contract_fingerprint = 正确值
delivery_status = FABRICATED
```

fingerprint 本身不会验证这个实例。

所以最终必须：

```text
identity/version/fingerprint valid
+
envelope semantics valid
→ ACCEPT
```

------

## Q5：为什么不直接使用 OpenTelemetry？

当前 WP4-A 的目标是冻结 LocalAgent 自己的 vendor-neutral contract（厂商中立合同），不是接入观测后端。

现阶段：

```text
OpenTelemetry / OTLP = NOT_IMPLEMENTED
```

已有 OTel-shaped adapter（形似 OTel 的适配器）也不能说“已经实现 OpenTelemetry”。

------

## Q6：为什么不把 exporter 一起做？

职责分离：

```text
WP4-A
= What can be exported?

WP4-B
= How is it exported?

WP4-C
= How AgentEvalOps consumes/maps it?
```

如果同时做，schema、transport、consumer 三层会一起变化，很难判断问题到底在哪层。

------

# 18. 面试中最容易夸大 / 答错的地方

## 错误 1

> “我做了完整的分布式 Trace 系统。”

错误。

正确：

> 做了 LocalAgent 内部 Trace 到稳定 export contract 的边界与 fingerprint。

没有：

- distributed propagation；
- OTLP；
- Collector；
- Trace backend。

------

## 错误 2

> “Trace 已经持久化。”

错误。

完整 `SpanRecord` 当前仍是 memory-only。

Journal 持久的是 RuntimeEvent safe facts，不是完整 Trace Store。

------

## 错误 3

> “已经接入 AgentEvalOps。”

错误。

```text
AgentEvalOps Adapter = NOT_IMPLEMENTED
```

这是 WP4-C。

------

## 错误 4

> “Fingerprint 是一次 Run 的唯一 ID。”

错误。

那是 instance fingerprint / run identity 的概念。

WP4-A fingerprint 是：

```text
contract compatibility identity
```

------

## 错误 5

> “Final Gate 发现了生产数据泄漏。”

错误。

P1-02 是 direct probe 发现的潜在公共合同绕过；当时 exporter transport 尚不存在。

必须说：

```text
SOURCE_AUDIT_FINDING + DIRECT_PROBE_FINDING
NOT production incident
```

------

## 错误 6

> “`frozen=True` 就是完全不可变。”

错误。

嵌套 `list/dict/set` 依然可能 mutable。

这正是 P1-02 的一部分。

------

# 19. P0 / P1 / P2 复习

## P0

最终：

```text
P0 = 0
```

没有出现已部署严重安全泄漏、Runtime authority 被破坏等情况。

------

## P1-01

```text
RunCoordinator Trace Context Cleanup Leak
```

核心知识：

```text
exception path
resource cleanup
ContextVar token restoration
finally
```

------

## P1-02

```text
Trace Export Envelope Validation Bypass
```

核心知识：

```text
factory validation
!=
public object invariant
```

------

## P1-03

```text
Attribute Domain Not Strict
```

核心知识：

```text
type validation
!=
semantic validation
```

------

## P1-04

```text
Fingerprint Domain Coverage Gap
```

核心知识：

```text
deterministic hash
!=
correct compatibility fingerprint
```

如果 semantic descriptor 不完整，一个完美确定性的 SHA-256 仍然会给出**错误的兼容身份**。

------

## P2-02

```text
Delivery / Memory negative-state asymmetry
```

接受。

不为了观测完整性制造假 Span。

------

## P2-03

```text
Planning / Step typed error taxonomy
```

Deferred。

当前不能声称精确 typed root-cause reconstruction。

------

# 20. 面试速查表

| 题目                                | 速答                                                         |
| ----------------------------------- | ------------------------------------------------------------ |
| WP4-A 做什么                        | 把内部 Trace 转成稳定、安全、版本化的公共 export contract    |
| 为什么不直接公开 SpanRecord         | 内部 Runtime model 与外部兼容合同必须解耦                    |
| 六类稳定 Span                       | run / planning / step / synthesis / output_delivery / final_memory_commit |
| Export Contract                     | `PUBLIC_VERSIONED`                                           |
| Internal Span model                 | `INTERNAL`                                                   |
| Export envelope                     | `TraceExportEnvelope`                                        |
| Validation Owner                    | `_validate_envelope_semantics()`                             |
| Semantic Owner                      | `export_contract_semantic_descriptor()`                      |
| Compatibility Owner                 | `TraceCompatibilityEvaluator`                                |
| Fingerprint Owner                   | `TraceContractFingerprinter`                                 |
| 算法                                | SHA-256                                                      |
| Canonical encoding                  | `canonical_json_v1`                                          |
| Export version                      | 1                                                            |
| Runtime Trace version               | 1                                                            |
| 当前 fingerprint                    | `6fc033bb...390ab`                                           |
| Fingerprint 表示什么                | schema + semantic compatibility                              |
| 不表示什么                          | 某次 Run/Span/Trace 实例                                     |
| P1-01                               | root Span / ContextVar cleanup                               |
| P1-02                               | direct envelope validation bypass                            |
| P1-03                               | value-domain 不严格                                          |
| P1-04                               | fingerprint 未覆盖 value-domain                              |
| `frozen dataclass` 是否够           | 不够，嵌套对象仍可能 mutable                                 |
| `SAFE_SPAN_ATTRIBUTES` 是否全公开   | 否                                                           |
| 未知内部字段                        | projection 时 omit                                           |
| direct unknown public attr          | invalid                                                      |
| invalid envelope + 正确 fingerprint | `REJECT(ENVELOPE_INVALID)`                                   |
| `publish_attempt_count`             | 0..1                                                         |
| DeliveryStatus public subset        | DELIVERED / FAILED / OUTCOME_UNKNOWN                         |
| OTel                                | NOT_IMPLEMENTED                                              |
| AgentEvalOps                        | NOT_IMPLEMENTED，WP4-C                                       |
| Exporter transport                  | NOT_IMPLEMENTED，WP4-B                                       |
| 完整 Trace 持久化                   | 没有，SpanRecord memory-only                                 |
| 最终 Gate                           | PASS                                                         |
| 全仓                                | 2305 passed + 42 subtests                                    |
| 最终 P0/P1/P2                       | 0 / 0 / 2                                                    |
| WP4 完成了吗                        | 没有，只完成 WP4-A                                           |
| Stage 3 PASS 了吗                   | 没有                                                         |

------

## 这一 WP 最值得你真正掌握的 5 句话

面试前至少把下面五句话理解到可以自己展开：

1. **内部 Runtime 数据结构不是天然的外部数据合同。**
2. **`frozen dataclass` 只能保证浅层字段不可赋值，不代表结构不可变。**
3. **类型合法不等于业务值域合法。**
4. **Fingerprint 的难点不是 SHA-256，而是 canonical semantic source 是否真的覆盖了全部兼容语义。**
5. **Contract fingerprint 描述“什么数据算合法”，而不是“这一次运行的数据是什么”。**

这五条基本就是 WP4-A 从架构设计、Final Gate FAIL、R1/R2 修复到最终 Re-Gate PASS 的核心学习价值。