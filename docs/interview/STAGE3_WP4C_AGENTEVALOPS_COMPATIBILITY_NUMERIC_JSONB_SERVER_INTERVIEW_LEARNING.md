# WP4-C 前半程长期改造学习 / 面试总结

> **学习范围说明**
>
> 本次总结只覆盖已经真实完成并通过独立 Gate 的：
>
> **LocalAgent Trace Export Contract → Serializer → AgentEvalOps Compatibility Endpoint → Numeric Contract → PostgreSQL authoritative persistence → Huge Integer JSONB → Shared Engine Isolation**
>
> 不包含尚未实现的 PycURL transport、完整 LocalAgent Adapter、production wiring 和最终 cross-system E2E。

------

## 1. 一句话项目 / 工作包定义

我把 LocalAgent 的 Trace Export（追踪导出）合同接入 AgentEvalOps，设计了一条**版本化、指纹校验、严格安全投影、跨项目 ownership（所有权）隔离、可重复提交判定、PostgreSQL commit-before-ack（提交后应答）且数值语义无损**的兼容摄取链路，并解决了 Python、JSON、PostgreSQL、SQLAlchemy 与 asyncpg 在极端数值和 JSONB 场景下的合同漂移问题。

最终 AgentEvalOps 服务端已经达到：

```text
IMPLEMENTATION_READY_FOR_LOCALAGENT
```

但还没有完成 LocalAgent 网络 Adapter，因此不能说 WP4-C 已完成。

------

# 2. 为什么要做

WP4-A 已经冻结了 LocalAgent 的 Trace Contract（追踪合同），WP4-B 也完成了 producer-side exporter interface（生产端导出接口）和异步 dispatcher（分发器）。

但是那时：

```text
LocalAgent
   ↓
TraceExportEnvelope
   ↓
TraceExportDispatcher
   ↓
???
   ↓
AgentEvalOps
```

中间仍然没有真实跨项目合同。

而 AgentEvalOps 原来的 `/traces`、`/spans` 接口不能直接承担这个职责，主要原因包括：

- 没有 LocalAgent Trace Contract fingerprint；
- 没有 frozen envelope 的严格 DTO（数据传输对象）；
- 缺乏完整 ownership 隔离；
- 无法表达 exact replay（完全相同重放）和 semantic conflict（语义冲突）；
- 原 Trace/Span 模型不足以作为完整 frozen envelope truth（冻结信封事实）；
- LocalAgent 和 AgentEvalOps 对数值的语义边界没有真正冻结。

因此最终不是简单：

```text
POST /traces
```

而是建立了独立兼容入口：

```text
POST /integrations/localagent/v1/trace-envelopes
```

------

# 3. 真实性与完成边界

这是面试时最重要的一节。

### 已真实实现

目前真实实现了：

```text
LocalAgent
TraceExportEnvelope contract
Trace fingerprint
standalone serializer
exact JSON numeric wire semantics
```

以及 AgentEvalOps：

```text
versioned LocalAgent DTO
strict body framing
bounded streaming receiver
authentication / project resolution
Redis admission
token-aware JSON decoder
duplicate-key rejection
numeric semantic normalization
canonical digest
ownership-safe ingestion
immutable sidecar
PostgreSQL NUMERIC duration
huge-int exact JSONB attributes
column-local JSONB TypeDecorator
commit-before-2xx
201 / 200 / 409 semantics
```

### 已真实测试

最新 AgentEvalOps 独立 Gate：

```text
481 unit passed
355 integration passed
P0 = 0
P1 = 0
```

真实使用了：

- PostgreSQL；
- Redis；
- FastAPI / ASGI；
- SQLAlchemy ORM；
- real LocalAgent serializer；
- fresh-session DB readback。

### 只完成设计、尚未实现

以下仍未完成：

```text
AgentEvalOpsTraceExporter
PycURL HTTP transport
LocalAgent Settings
server.py lifespan wiring
real production-like LocalAgent → HTTP → AgentEvalOps final E2E
WP4-C Final Gate
```

### 不是生产事故

P1-01～P1-07，以及后面的 global engine JSON codec 问题，都是：

> 源码审计 / 独立 Gate / adversarial probe（对抗探针）发现的问题。

**不能说成线上事故。**

------

# 4. 修改前架构与根因

最初的问题不是一个 Bug，而是多层 semantic ownership（语义所有权）没有对齐。

原先可以简化成：

```text
LocalAgent TraceExportEnvelope
        ↓
可能未来 JSON
        ↓
AgentEvalOps DTO
        ↓
Python numeric
        ↓
digest
        ↓
PostgreSQL
```

看起来很简单。

但实际上这里有五个不同 domain（域）：

```text
A. Python annotation

B. LocalAgent constructor / validator semantic domain

C. Runtime actual-produced value domain

D. JSON wire domain

E. AgentEvalOps persisted authoritative domain
```

最经典的问题是：

```text
duration_ms: float
```

看起来像“就是 float”。

但 LocalAgent validator 实际允许：

```text
int | float
```

包括：

```text
2**53 + 1
```

这样精确的 Python int。

所以如果 transport 做：

```text
float(value)
```

就直接丢精度。

这也是整个长期改造中最重要的一课：

> **类型标注不一定等于运行时合同。实际 validator 的 accepted set（接受集合）才可能是真正的语义合同。**

------

# 5. 方案讨论与取舍

整个长期改造其实经历了几次非常典型的工程方案选择。

## 方案一：直接复用旧 `/traces`

拒绝。

因为它不能表达 LocalAgent frozen contract、fingerprint、ownership、exact duplicate 等完整语义。

------

## 方案二：duration 全部转 float

拒绝。

因为：

```text
2**53 + 1
```

是合法 envelope 值，而：

```text
float(2**53 + 1)
==
float(2**53)
```

导致 semantic narrowing（语义收窄）。

------

## 方案三：duration 用 JSON string

拒绝。

这会把：

```json
"duration_ms": 1.5
```

变成：

```json
"duration_ms": "1.5"
```

属于 wire type change（线级类型变化），可能要求 fingerprint / contract version 重入。

------

## 方案四：只允许 <=2**53

拒绝。

没有 frozen LocalAgent source 支持这个限制，会 retroactively narrowing（追溯式收窄）已有合同。

------

## 方案五：PostgreSQL float8

最初实施过，但独立 Gate 证明失败。

真实案例：

```text
HTTP input:
9007199254740993

PostgreSQL float8 fresh read:
9007199254740992.0
```

digest 能区分，但 digest 不可逆，不能代替一等数据存储。

最终改成：

```text
PostgreSQL NUMERIC
```

------

## 方案六：全局 SQLAlchemy JSON codec

R3 实现后发现能解决 huge int，但独立 Gate 又把它否掉了。

原因是它改变整个 AgentEvalOps 26 个 JSONB 列的语义：

```text
{True: "x"}
"true" → "True"

{None: "x"}
"null" → "None"

mixed int/string keys
accepted → TypeError

tuple key
rejected → accepted/stringified
```

于是做了 Shared Infrastructure Architecture Re-entry（共享基础设施架构重入）。

最终选择：

```text
OPTION_B_COLUMN_LOCAL_CODEC
```

只在：

```text
localagent_trace_envelope_sidecars.attributes
```

使用特殊 JSONB bridge。

------

# 6. 最终架构

目前服务端完成后的真实架构是：

```text
LocalAgent
│
├─ Trace SpanRecord
│
├─ project_span()
│
├─ TraceExportEnvelope v1
│
├─ validate_trace_export_envelope_semantics()
│
├─ serialize_trace_export_envelope()
│
│   ├─ exact integer JSON token
│   ├─ binary64 float round-trip
│   ├─ UTF-8
│   └─ <= 16384 bytes
│
└─ [Full HTTP Adapter 尚未实现]
             ↓
────────────────────────────────────
AgentEvalOps
────────────────────────────────────
             ↓
POST /integrations/localagent/v1/trace-envelopes
             ↓
Framing / Content-Type / Content-Length
             ↓
bounded request.stream()
             ↓
Project / API-Key auth
             ↓
Redis admission
             ↓
decode_envelope_body()
             │
             ├─ one json.loads
             ├─ custom parse_int
             ├─ duplicate-key reject
             └─ NaN / Infinity reject
             ↓
Strict DTO / Semantic validation
             ↓
Canonical semantic value
             ↓
Canonical SHA-256 digest
             ↓
Ownership / Duplicate classification
             ↓
ONE PostgreSQL transaction
      ├─ external identity binding
      ├─ immutable sidecar
      │    ├─ duration_ms → NUMERIC
      │    └─ attributes → JSONB
      │          └─ LocalAgentAttributesJSONB
      └─ legacy Trace / Span projection
             ↓
COMMIT
             ↓
201 / 200
```

最关键的数据结构边界：

```text
duration_ms
→ PostgreSQL NUMERIC

attributes
→ PostgreSQL JSONB
→ column-local exact codec
```

而不是全局修改 JSON runtime。

------

# 7. 核心状态机和时序

HTTP 摄取的核心时序：

```text
REQUEST
  ↓
Framing validation
  ↓
Bounded body receive
  ↓
Authentication
  ↓
Redis admission
  ↓
Decode
  ↓
Semantic validation
  ↓
Compatibility check
  ↓
Ownership check
  ↓
Duplicate classification
  ↓
Persistence transaction
  ↓
COMMIT
  ↓
ACK
```

### 首次合法写入

```text
new external identity
→ persist
→ commit
→ 201 PERSISTED
```

### 完全相同重放

```text
same identity
+
same canonical digest
→ no duplicate write
→ 200 DUPLICATE_ACCEPTED
```

### 同 identity，不同语义

```text
same identity
+
different digest
→ 409 LOCALAGENT_ENVELOPE_CONFLICT
```

### Redis 不可用

```text
Redis admission unavailable
→ 503 INGESTION_CAPACITY_UNAVAILABLE
→ PostgreSQL zero mutation
```

### DB commit 失败

```text
transaction failure
→ no 2xx
→ rollback
```

------

# 8. 数据 / 权限 / Owner

## Trace Contract Owner

LocalAgent：

```text
trace_export_contract.py
```

拥有：

- contract identity；
- version；
- fingerprint；
- semantic schema；
- duration 合同。

------

## Wire Serializer Owner

LocalAgent：

```text
trace_export_serialization.py::
serialize_trace_export_envelope()
```

唯一 production wire Owner。

------

## Decode Owner

AgentEvalOps：

```text
decoder.py::
decode_envelope_body()
```

唯一 JSON decode Owner。

------

## Ownership Owner

AgentEvalOps compatibility persistence。

负责：

```text
external trace/span identity
→ project ownership
```

不同 project 不能抢占已有 external identity。

------

## Authoritative Persistence Owner

```text
localagent_trace_envelope_sidecars
```

而不是 legacy Trace/Span。

其中：

```text
duration_ms = NUMERIC
attributes = JSONB
```

------

## Redis Owner

Redis 只负责 admission / rate limiting。

不是：

- persistence owner；
- ACK owner；
- queue owner。

------

# 9. 兼容策略

核心策略不是“Consumer 尽量宽松”，而是：

> **Producer / Consumer exact semantic parity（精确语义对齐）。**

例如 duration：

```text
Producer valid integer:
0 .. MAX_V1_DURATION_INT

Consumer:
必须完全接受同样范围
```

不能：

```text
consumer > producer
```

也不能：

```text
consumer < producer
```

fingerprint 使用 exact match：

```text
6fc033bb4310...
```

没有 compatibility window。

Legacy Trace/Span 则保留为：

```text
projection / read model
```

而不是 authoritative frozen envelope。

------

# 10. Bad Cases

这是这段经历最适合面试的部分。

## Bad Case 1：Pydantic 自动 coercion

**真实性：实施 Gate 真实发现，不是生产事故。**

例如：

```text
contract_version = "1"
```

本来应该拒绝，却被转换成 `1`。

根因：

> validation 发生在 coercion 之后。

修复：

> strict raw type validation。

------

## Bad Case 2：16 KiB body 限制是假限制

最初：

```python
await request.body()
```

之后才：

```text
if len(body) > 16384
```

实际上已经把完整 body 放进应用内存。

修复：

```text
request.stream()
→ bounded retained buffer
→ crossing immediately fail
```

------

## Bad Case 3：digest 无损，但数据库有损

```text
9007199254740993
```

digest 可以区分。

但 float8：

```text
9007199254740992.0
```

于是：

> **哈希正确 ≠ 权威数据正确。**

这是非常值得面试讲的点。

------

## Bad Case 4：`Decimal.normalize()` 也会丢语义

默认 Decimal context precision（上下文精度）是 28。

因此类似：

```text
10**100 + 1
10**100 + 2
```

可能在 normalize 后发生 collision（碰撞）。

修复：

```text
Decimal.as_tuple()
→ manual exact canonical form
```

------

## Bad Case 5：Python 4300-digit 限制

LocalAgent serializer 可以发合法 5000 位整数。

Consumer：

```python
json.loads(...)
```

默认：

```text
parse_int = int
```

在 >4300 位直接：

```text
ValueError
→ 500
```

修复：

```text
compatibility-local custom parse_int
```

并且**没有**：

```python
sys.set_int_max_str_digits(0)
```

不会关闭进程全局安全限制。

------

## Bad Case 6：局部问题被错误修成全局行为变化

R3 为支持 huge-int JSONB，把 exact JSON codec 安装到了 shared engine。

局部问题解决了。

但是 26 个 JSONB 列全部改变行为。

这是一个非常经典的：

> **Local requirement leaked into shared infrastructure（局部需求泄漏到共享基础设施）。**

最终通过：

```text
column-local TypeDecorator(JSONB)
```

修复。

------

# 11. Tests / Gate：只写真实执行过的

最终 R4 独立 Gate：

```text
Targeted:
269 passed

Full Unit:
481 passed

Full Integration:
355 passed

P0:
0

P1:
0

P2:
1
```

静态 Gate：

```text
ruff                     PASS
compileall               PASS
uv lock --check          PASS
alembic heads            PASS
git diff --check         PASS
```

单 Alembic head：

```text
7ca7dbab5b86
```



另外必须记住：

```text
TEST_GAP = 2
DOC_DRIFT = 2
```

只是当前 Gate 用 direct probes（直接探针）补足了 release evidence，所以不构成 P1 blocker。

------

# 12. Known Limitations

目前最重要的限制：

### 1. WP4-C 还没有完成

没有：

```text
AgentEvalOpsTraceExporter
PycURL
Settings
lifespan wiring
final cross-system E2E
```

### 2. Compatibility Dependency 仍不是 COMPLETE

目前只是：

```text
AgentEvalOps Server Implementation
= IMPLEMENTATION_READY_FOR_LOCALAGENT
```

### 3. Test Gap 仍有两个

最终 Gate 明确记录：

- repository tests 未覆盖 `session.refresh / expire-reload` 生命周期；
- R4 integration test 使用 fixture 重建 body，没有直接运行 real serializer。

但 Gate 自己已经通过 direct probes 验证两者。

### 4. Legacy Delete divergence

仍然：

```text
ACCEPTED_P2
```

删除 legacy row 后：

```text
sidecar still exists
exact replay → 200
legacy row 不重建
```

不修。

### 5. 尚不能说 production ready

当前 AgentEvalOps 服务端 integration dependency 已 ready。

但：

```text
production ready = NO
```

------

# 13. 体现出的工程能力

这一段项目非常适合体现的不只是 Python 编码能力，而是：

### Contract Engineering（合同工程）

能区分：

```text
annotation
validator semantics
runtime actual value
wire semantics
database semantics
```

------

### Defensive API Design（防御性 API 设计）

包含：

- bounded body；
- strict DTO；
- duplicate keys；
- Redis fail closed；
- ownership；
- secret isolation。

------

### Persistence Semantics（持久化语义）

理解：

```text
digest ≠ authoritative storage
```

以及：

```text
float8
NUMERIC
JSONB
```

的精度差异。

------

### Cross-layer Debugging（跨层调试）

实际问题跨越：

```text
Python
json
Pydantic
FastAPI
SQLAlchemy
asyncpg
PostgreSQL
Redis
```

------

### Blast Radius Control（影响面控制）

最典型的就是：

```text
global engine codec
→ FAIL
```

最终收敛到：

```text
column-local TypeDecorator
```

------

### Independent Gate Mindset（独立门禁思维）

多次出现：

```text
Implementation PASS
→ Independent Gate FAIL
→ Re-entry / remediation
```

这不是“反复返工”，而是在证明：

> **实现者的测试不能代替独立的合同验证。**

------

# 14. 30 秒面试版本

> 我在 LocalAgent 的 Trace Export 接入 AgentEvalOps 时，没有直接复用旧 Trace API，而是设计了版本化 compatibility endpoint。这个过程中我重点解决了跨项目 ownership、严格 wire contract、重复提交判定、commit-before-ack，以及 Python int/float、JSON number 和 PostgreSQL 的无损数值语义。
>
> 比较典型的问题是 `2**53+1` 在 float8 中会丢精度，以及 Python 默认 JSON parser 对 4300 位以上整数会报错。后面还发现为了支持超大整数而设置全局 SQLAlchemy JSON codec 会影响其它 26 个 JSONB 列，所以最终把特殊 codec 收敛到 LocalAgent sidecar 的单一 JSONB 列。最终 AgentEvalOps 服务端独立 Gate 达到 P0=0、P1=0，481 个 Unit 和 355 个 Integration 测试通过；目前服务端已 ready，下一步是 LocalAgent 的 PycURL Adapter 和最终 E2E。

------

# 15. 2 分钟面试版本

> WP4-C 的目标是把 LocalAgent 已冻结的 Trace Export Contract 接到 AgentEvalOps。最开始我发现不能直接复用原来的 `/traces`，因为原接口没有 fingerprint、ownership 和 frozen envelope 的完整语义，所以增加了独立的 `/integrations/localagent/v1/trace-envelopes`。
>
> 摄取链路按 framing、bounded body、auth、Redis admission、JSON decode、semantic validation、ownership、duplicate classification、PostgreSQL transaction、commit、ACK 组织。
>
> 其中最麻烦的是 numeric contract。LocalAgent 的 `duration_ms` 虽然 annotation 是 float，但实际公共 validator 接受 int 和 float，所以 `2**53+1` 是合法值。如果 transport 或数据库先转 float，就会丢精度。我们最终冻结了 integer exact JSON token 和 binary64 float round-trip，AgentEvalOps 使用 token-aware decoder，duration 用 PostgreSQL NUMERIC，无损保存。
>
> 后来又遇到 Python 默认 4300 位整数解析限制。LocalAgent 的部分 NON_NEGATIVE_INT attribute 可以在 16 KiB envelope 内达到上万位，因此 consumer 需要 custom `parse_int`，但我们没有关闭全局的 `sys.set_int_max_str_digits`。
>
> 一次实现曾把 exact JSON codec 装到整个 SQLAlchemy engine，虽然 LocalAgent 修好了，却改变了其他 JSONB 的 bool key、None key、mixed key 和异常语义。独立 Gate 把这个问题抓出来后，我重新做架构收敛，最终用 column-local `TypeDecorator(JSONB)`，只作用于 LocalAgent sidecar.attributes，shared engine 恢复默认行为。
>
> 最终独立 Gate P0=0、P1=0，481 Unit、355 Integration 通过。目前 AgentEvalOps 服务端已经 `IMPLEMENTATION_READY_FOR_LOCALAGENT`，但完整 LocalAgent HTTP Adapter 和最终跨系统 E2E 还没完成。

------

# 16. 深入版本：真正应该掌握的技术主线

面试深入追问时，不要按 R1/R2/R3/R4 流水账讲。

最好按四条技术主线讲。

## 主线 A：合同一致性

```text
Producer semantic domain
=
Wire semantic domain
=
Consumer accepted domain
=
Authoritative persisted domain
```

------

## 主线 B：可靠 ACK

```text
Redis admission
≠ persistence

flush
≠ persistence

digest
≠ persistence

2xx
⇐ PostgreSQL commit
```

------

## 主线 C：Scope Isolation（作用域隔离）

```text
LocalAgent-specific requirement
→ LocalAgent-specific column adapter

NOT
→ global engine semantic change
```

------

## 主线 D：Evidence（证据）

```text
Implementation report
<
Independent Gate
<
direct source/probe
```

所以我们多次允许：

```text
implementation PASS
```

被：

```text
independent Gate FAIL
```

推翻。

------

# 17. 高频追问

### Q1：为什么 `2**53+1` 是关键测试？

因为 IEEE-754 binary64（双精度浮点）在这个区间不能表示所有相邻整数，是发现隐式 int→float conversion 的经典边界。

------

### Q2：为什么 digest 正确还不够？

digest 只能证明两个输入不同。

它不能把：

```text
SHA256(...)
```

逆向恢复成原始 duration。

所以 authoritative persistence 必须自己无损。

------

### Q3：为什么 duration 用 NUMERIC，attributes 还用 JSONB？

两者责任不同：

```text
duration_ms
= 一等 numeric field
→ NUMERIC

attributes
= bounded typed mapping
→ JSONB
```

attribute 仍需要保持 JSON object 语义。

------

### Q4：为什么不用 `sys.set_int_max_str_digits(0)`？

因为它是 process-global（进程全局）修改。

LocalAgent compatibility 的需求不能改变整个 AgentEvalOps 的 Python integer parsing security boundary。

------

### Q5：为什么 global codec 不行？

因为它影响 shared engine 的所有 JSONB。

实际已经证明会改变 bool/None dictionary keys、mixed keys、tuple keys 和错误边界。

------

### Q6：为什么 TypeDecorator 合适？

因为它可以把特殊 persistence semantics 限制到：

```text
one column
```

而仍然：

```text
same engine
same session
same PostgreSQL transaction
```

------

### Q7：为什么不单独开一个 engine？

会破坏：

```text
binding
sidecar
legacy projection
```

同一个事务原子性。

------

# 18. 最容易夸大 / 答错的地方

### 错误 1

> “AgentEvalOps 已经完整接入 LocalAgent。”

不对。

现在是：

```text
Server ready for LocalAgent
```

不是：

```text
cross-system complete
```

------

### 错误 2

> “我们实现了 exactly-once。”

没有。

LocalAgent WP4-B 的语义仍是：

```text
BEST_EFFORT
+
AT_MOST_ONE_TRANSPORT_ATTEMPT_PER_ACCEPTED_ENVELOPE
```

服务端 exact replay 返回 200，也不等于 exactly-once delivery。

------

### 错误 3

> “线上出现过 5000 位整数问题。”

没有证据。

这是 independent Gate 构造并真实验证的边界场景。

------

### 错误 4

> “15901 位是合同规定的最大整数。”

错误。

最终 Gate 已纠正：

```text
15901
= 具体 envelope shape 下的 near-limit case
```

真正 universal limit 是：

```text
complete envelope <=16384 bytes
```



------

### 错误 5

> “我们所有 JSONB 都支持超大整数了。”

错误，而且这正是我们拒绝的设计。

只有：

```text
LocalAgent sidecar.attributes
```

拥有扩展行为。

------

### 错误 6

> “P1 全部没有问题。”

应该说：

> 最终独立 Gate 已经达到 P1=0。

历史上真实发现过多个 P1。

------

# 19. P0 / P1 / P2 复习

最终这一段的状态：

```text
P0 = 0

P1 = 0

P2 = 1
└─ Legacy Delete Divergence
   = ACCEPTED_P2
```



历史关键 P1：

```text
P1-01
Wire type coercion

P1-02
Body limit after full buffering

P1-03
Producer / consumer numeric domain mismatch

P1-04
Canonical digest numeric semantics / totality

P1-05
Redis failure contract bypass

P1-06
float8 sidecar numeric truth loss

P1-07
>4300-digit producer-valid integer wire parity

Parser Failure Boundary
raw parser exception → 500

GLOBAL_ENGINE_JSON_CODEC_NON_REGRESSION
Local fix polluted shared JSON semantics
```

最终全部 CLOSED。

这是非常漂亮的一条面试主线：

> **每个 P1 本质上都对应一个 Owner 或 semantic boundary 没有定义清楚。**

------

# 20. 速查表

| 主题                      | 最终方案                                      |
| ------------------------- | --------------------------------------------- |
| Endpoint                  | `/integrations/localagent/v1/trace-envelopes` |
| Contract                  | version 1                                     |
| Fingerprint               | `6fc033bb...390ab`                            |
| Producer serializer       | LocalAgent code-owned                         |
| Body                      | streaming + app-retained ≤16384               |
| Auth                      | existing project + API key                    |
| Redis                     | admission only                                |
| JSON decode               | one-pass token-aware                          |
| Duplicate keys            | reject                                        |
| Duration int              | exact                                         |
| Duration float            | binary64 semantics                            |
| Duration DB               | PostgreSQL NUMERIC                            |
| Attribute DB              | PostgreSQL JSONB                              |
| Huge int parser           | compatibility-local `parse_int`               |
| Global Python digit limit | unchanged                                     |
| Huge int JSONB            | column-local                                  |
| Shared engine JSON        | default                                       |
| TypeDecorator usage       | exactly one column                            |
| Digest                    | canonical SHA-256                             |
| Digest role               | duplicate classifier                          |
| Persistence truth         | sidecar                                       |
| First write               | 201                                           |
| Exact replay              | 200                                           |
| Conflict                  | 409                                           |
| ACK owner                 | PostgreSQL commit                             |
| Cross-project collision   | 409                                           |
| Transaction               | one PostgreSQL transaction                    |
| P0                        | 0                                             |
| P1                        | 0                                             |
| P2                        | 1                                             |
| Server readiness          | `IMPLEMENTATION_READY_FOR_LOCALAGENT`         |
| Full Adapter              | 未实现                                        |
| WP4-C                     | 未完成                                        |

