# 1. 一句话项目 / 工作包定义

我在 LocalAgent 和 AgentEvalOps 之间实现了一条**版本化、可验证、失败有界的真实 Trace Export（追踪导出）链路**：

> LocalAgent Runtime 完成 Span 后，通过已有 Trace Dispatcher，以冻结的 Trace Contract 序列化成安全 Envelope，再通过 PycURL 单次 HTTP POST 到 AgentEvalOps；服务端完成 contract/fingerprint 校验、项目所有权判断、重复检测和 PostgreSQL 原子持久化，最终支持首写、幂等重放和冲突识别。

最终已经在本地真实双系统环境完成端到端验证。

------

# 2. 为什么做

WP4-A 和 WP4-B 已经解决：

```text
WP4-A
Trace Contract / Fingerprint

WP4-B
Exporter Interface / Dispatcher
```

但当时链路停在：

```text
Runtime Span
→ TraceExportEnvelope
→ Dispatcher
→ ???
```

没有真正把 LocalAgent Trace 输送到 AgentEvalOps。

而直接复用 AgentEvalOps 旧 `/traces` / `/spans` API 又不够，因为缺少：

- LocalAgent frozen contract（冻结合同）；
- version / fingerprint；
- producer/consumer 数值语义一致性；
- project ownership（项目所有权）；
- exact replay / conflict 语义；
- 完整 Envelope 的权威持久化。

因此 WP4-C 的本质不是“写一个 HTTP Client”，而是：

> **把两个独立系统的 Trace 语义真正对齐，并建立可信的跨系统失败边界。**

------

# 3. 真实性与完成边界

## 已真实实现

LocalAgent：

- `TraceExportEnvelope v1`
- Trace fingerprint
- standalone serializer（独立序列化器）
- `TraceExportDispatcher`
- `AgentEvalOpsTraceExporter`
- PycURL synchronous transport
- Settings
- `server.py::lifespan()` Composition Root（组合根）接线
- bounded shutdown
- disabled mode

AgentEvalOps：

- `/integrations/localagent/v1/trace-envelopes`
- strict DTO（严格数据传输对象）
- bounded body receive
- Redis admission
- ownership-safe persistence
- canonical digest
- PostgreSQL `NUMERIC` duration
- authoritative sidecar
- LocalAgent-specific column-local JSONB codec
- 201 / 200 / 409 语义

## 已真实测试

最终真实 E2E：

```text
LocalAgent real serializer
→ real dispatcher
→ real exporter
→ real PycURL
→ real AgentEvalOps
→ real Redis
→ real PostgreSQL
```

并通过 fresh DB session（全新数据库会话）验证权威数据。

## 没有实现 / 不能宣称

没有：

- exactly-once；
- durable outbox；
- retry / backoff；
- durable delivery；
- distributed trace delivery；
- production deployment verification；
- OTLP / OpenTelemetry Exporter；
- multi-target exporter；
- proxy / custom CA。

这些都不能在面试中说成已经完成。

------

# 4. 修改前架构与根因

最初看起来只是：

```text
LocalAgent JSON
→ HTTP
→ AgentEvalOps
```

但真正的问题横跨了至少六层：

```text
Python semantic domain
        ↓
Trace Contract
        ↓
JSON wire semantics
        ↓
HTTP transport
        ↓
AgentEvalOps consumer semantics
        ↓
PostgreSQL authoritative persistence
```

最大根因是：

> **同一个字段在 Python 类型、Validator、JSON、Database 中看起来都是“数字”，但它们真正能精确表示的集合并不相同。**

例如：

```text
duration_ms annotation = float
```

并不代表公共合同只有 float。

实际 validator 允许合法 Python `int`。

因此：

```text
2**53 + 1
```

也是合法输入。

如果中间任意一层偷偷：

```python
float(value)
```

就会损失合同语义。

------

# 5. 方案讨论与取舍

这整个 WP 最值得讲的是**没有一次把所有问题拍脑袋解决，而是通过 Gate 不断缩小正确边界**。

## 方案 A：直接复用旧 Trace API

拒绝。

因为无法完整表达：

```text
contract identity
version
fingerprint
ownership
exact replay
semantic conflict
full envelope truth
```

------

## 方案 B：所有 duration 都转 float

拒绝。

因为：

```text
2**53
2**53 + 1
```

转 binary64 后可能无法区分。

------

## 方案 C：限制合法 int <= 2**53

拒绝。

Producer 合同没有这个限制。

Consumer 不能为了实现方便擅自缩窄 frozen producer domain。

------

## 方案 D：duration 存 PostgreSQL float8

实际实现后被独立 Gate 否决。

典型结果：

```text
input:
9007199254740993

float8 readback:
9007199254740992.0
```

因此改成 PostgreSQL `NUMERIC`。

------

## 方案 E：digest 能区分就够了

拒绝。

因为：

> digest 是 duplicate classifier（重复分类依据），不是 authoritative value storage（权威值存储）。

哈希不可逆。

------

## 方案 F：全局 SQLAlchemy JSON codec

R3 曾经实现并解决了 huge-int JSONB。

但独立 Gate 发现它改变了整个 AgentEvalOps 其他 JSONB 行为。

于是失败并架构重入。

------

## 最终方案：column-local JSONB

最终只给：

```text
localagent_trace_envelope_sidecars.attributes
```

增加：

```text
LocalAgentAttributesJSONB(TypeDecorator)
```

而共享 SQLAlchemy Engine 恢复默认 JSON 行为。

这是整个 WP 最值得记住的设计原则：

> **特殊合同要求应该尽量由最小作用域的 Owner 承担，而不是污染共享基础设施。**

------

# 6. 最终架构

最终真实链路：

```text
LocalAgent Runtime
│
├─ SpanHandle.end()
│
├─ SpanRecord
│
├─ InMemorySpanRecorder
│       │
│       └─ completion observer
│
├─ TraceExportDispatcher
│       │
│       ├─ bounded queue
│       └─ single worker
│
├─ AgentEvalOpsTraceExporter
│       │
│       ├─ serialize_trace_export_envelope()
│       ├─ one curl.perform()
│       ├─ no retry
│       └─ hard total deadline
│
├──────────── HTTP ────────────────
│
AgentEvalOps
│
├─ framing
├─ bounded body
├─ authentication
├─ Redis admission
├─ strict decode
├─ fingerprint / contract
├─ ownership
├─ canonical digest
├─ duplicate classification
│
└─ ONE PostgreSQL Transaction
       │
       ├─ trace identity binding
       ├─ span identity binding
       ├─ authoritative sidecar
       │     ├─ duration_ms → NUMERIC
       │     └─ attributes → JSONB
       │          └─ column-local codec
       └─ legacy Trace / Span projection
             ↓
           COMMIT
             ↓
       201 / 200 / 409
```

------

# 7. 核心状态机和时序

## 首次写入

```text
new envelope
→ ownership PASS
→ persist
→ PostgreSQL COMMIT
→ 201 PERSISTED
```

最终真实 E2E 验证：

```text
attempted = 1
sent = 1
failed = 0
```



## 精确 replay

```text
same external identity
+
same canonical digest
→ no duplicate mutation
→ 200 DUPLICATE_ACCEPTED
```

真实累计状态：

```text
attempted = 2
sent = 2
failed = 0
```

数据库 truth 与首次写入完全相同。

## 冲突

```text
same external identity
+
different semantic digest
→ 409 LOCALAGENT_ENVELOPE_CONFLICT
```

真实累计：

```text
attempted = 3
sent = 2
failed = 1
```

原 DB truth 保持不变。

------

# 8. 数据 / 权限 / Owner

必须能讲清 Owner，否则很容易被追问崩。

| 数据/能力                        | Owner                                  |
| -------------------------------- | -------------------------------------- |
| Trace Contract                   | LocalAgent Trace Export Contract       |
| Fingerprint                      | LocalAgent frozen contract             |
| Wire serialization               | `serialize_trace_export_envelope()`    |
| Queue / worker                   | `TraceExportDispatcher`                |
| HTTP transport                   | `AgentEvalOpsTraceExporter`            |
| Body bound                       | AgentEvalOps compatibility route       |
| Admission                        | Redis                                  |
| Project authentication           | AgentEvalOps                           |
| External identity ownership      | AgentEvalOps compatibility persistence |
| Duplicate classification         | canonical digest                       |
| Authoritative envelope truth     | sidecar                                |
| `duration_ms`                    | PostgreSQL `NUMERIC`                   |
| `attributes`                     | sidecar JSONB                          |
| Final successful acknowledgement | PostgreSQL commit                      |

其中必须牢记：

```text
Redis admission
≠ persistence

digest
≠ authoritative truth

legacy Trace/Span
≠ full frozen-envelope truth
```

------

# 9. 兼容策略

整体策略是：

> **Producer accepted domain = Wire representable domain = Consumer accepted domain = Persisted authoritative domain。**

例如数字：

```text
Python int
→ exact JSON integer token
→ consumer exact parse
→ exact NUMERIC / JSONB
```

不能在某一层默默降级成 float。

另外 fingerprint 采用 exact compatibility：

```text
contract version = 1

fingerprint =
6fc033bb4310c7671541d7dc9e7297fdf0d0bb32605651b840ccd0fd173390ab
```

最终 Gate 仍未改变。

------

# 10. Bad Cases

这些是这整个 WP 最有价值的面试素材。

## Bad Case 1：Wire coercion

**真实性：实施 / Gate 发现。不是线上事故。**

问题：

```text
"1"
true
1.0
```

可能被 Pydantic 自动 coercion 成目标类型。

风险：

> Wire contract 表面 strict，实际接受非法类型。

修复：

> strict raw wire validation。

------

## Bad Case 2：假的 16 KiB body limit

问题：

```python
body = await request.body()
if len(body) > 16384:
    reject()
```

虽然最终拒绝了，但应用已经把整个 body 收进内存。

修复：

```text
request.stream()
→ crossing immediately stop
→ retained application bytes bounded
```

------

## Bad Case 3：float8 丢失权威数值

问题：

```text
9007199254740993
→ float8
→ 9007199254740992
```

修复：

```text
duration_ms
→ PostgreSQL NUMERIC
```

------

## Bad Case 4：`Decimal.normalize()` 会受 context 影响

问题：

对超长数字规范化时，context precision 可能改变结果甚至导致 digest collision。

修复：

```text
Decimal.as_tuple()
→ manual canonical fixed-point representation
```

------

## Bad Case 5：Python 4300 位整数解析限制

Producer 可以合法产生：

```text
5000-digit integer
```

但默认：

```python
json.loads(...)
```

内部：

```text
parse_int = int
```

会触发 Python digit limit：

```text
ValueError
→ 500
```

修复：

```text
compatibility-local custom parse_int
```

而没有使用：

```python
sys.set_int_max_str_digits(0)
```

避免修改整个进程的安全行为。

------

## Bad Case 6：局部修复污染共享 Engine

为了支持 huge-int JSONB，R3 曾在 shared SQLAlchemy Engine 设置 exact JSON codec。

虽然 LocalAgent 好了，却改变：

```text
bool dict key
None dict key
mixed key
tuple key
NaN / Infinity failure boundary
```

最终独立 Gate FAIL。

修复：

```text
Global codec removed

LocalAgentAttributesJSONB
→ only one column
```

------

## Bad Case 7：HTTP Client 自动重发 POST

**真实性：假设 Bad Case + 真实 transport probe。**

风险场景：

```text
server already received POST body
→ connection resets before response
```

最危险的行为：

```text
client silently POST again
```

这可能制造重复 side effect。

最终真实 probe 证明：

```text
server observed exactly ONE POST
```

没有 automatic resend。

------

# 11. 已真实执行的 Tests / Gates

最终 E2E Gate：

### LocalAgent focused

```text
169 passed
1 deselected
```

### AgentEvalOps focused

```text
143 unit passed

126 integration passed

269 focused total
```

### LocalAgent full

```text
2467 passed
13 deselected
4 warnings
42 subtests passed
exit 0
```

### AgentEvalOps full

```text
481 unit passed

355 integration passed

exit 0
```

### Static

LocalAgent：

```text
compileall PASS
uv lock --check PASS
git diff --check PASS
```

AgentEvalOps：

```text
ruff PASS
compileall PASS
uv lock --check PASS
alembic heads PASS
git diff --check PASS
```



------

# 12. Known Limitations

最终真实保留：

1. **没有 retry / backoff**
2. **没有 batching**
3. **没有 durable outbox**
4. **没有 spool / replay**
5. **single-envelope transport**
6. DNS stall 没有专门稳定复现
7. `metrics_recorder=None`
8. 无 proxy support
9. 无 custom CA
10. legacy delete divergence = `ACCEPTED_P2`
11. column-local huge-int 是 LocalAgent 专用能力
12. 不是 production-proven

这些都没有阻塞 WP4-C。

------

# 13. 体现的工程能力

这一阶段最适合体现六类能力。

## ① Contract Engineering（合同工程）

不是只看 Python annotation，而是明确：

```text
validator accepted set
wire accepted set
DB representable set
```

------

## ② Cross-system Integration（跨系统集成）

实际贯通：

```text
LocalAgent
FastAPI
PycURL/libcurl
Redis
SQLAlchemy
asyncpg
PostgreSQL
AgentEvalOps
```

------

## ③ Failure Semantics（失败语义）

明确区分：

```text
transport success
application persistence
duplicate
conflict
timeout
bounded failure
```

------

## ④ Persistence Correctness（持久化正确性）

掌握：

```text
float8
NUMERIC
JSONB
Decimal
binary64
canonical digest
```

之间真正的工程差异。

------

## ⑤ Blast Radius Control（影响范围控制）

从：

```text
global engine JSON codec
```

最终缩到：

```text
one column-local TypeDecorator
```

这是非常好的生产级设计案例。

------

## ⑥ Independent Gate（独立门禁）

整个 WP 多次出现：

```text
Implementation says PASS
        ↓
Independent Gate finds P1
        ↓
Remediation / Architecture Re-entry
        ↓
Re-Gate
```

说明不是为了“测试通过”，而是为了证明合同成立。

------

# 14. 30 秒面试版本

> 我在 LocalAgent 和 AgentEvalOps 之间实现了一条真实 Trace Export 链路。LocalAgent Runtime 完成 Span 后，经 Dispatcher 生成冻结的 Trace Envelope，再通过 PycURL 单次 POST 到 AgentEvalOps，服务端负责版本和 fingerprint 校验、项目 ownership、重复检测和 PostgreSQL 原子持久化。
>
> 中间比较典型的问题有数值精度和 JSON 边界，比如 `2**53+1` 不能经过 float8，Python 默认 JSON parser 对 4300 位以上整数也有限制。最终 duration 用 NUMERIC，超大整数 JSONB 能力通过 column-local TypeDecorator 只作用于 LocalAgent sidecar，不污染整个数据库引擎。
>
> 最终真实双系统 E2E 首写 201、精确 replay 200、冲突 409，P0=0、P1=0；LocalAgent 2467 个测试、AgentEvalOps 481 Unit + 355 Integration 全部通过。目前是本地真实 E2E verified，但不是 exactly-once，也不是 production-proven。

------

# 15. 2 分钟面试版本

> WP4-C 的目标是把 LocalAgent 已经冻结的 Trace Export Contract 真正接入 AgentEvalOps。
>
> 我没有直接复用 AgentEvalOps 原来的 Trace API，因为原接口没有 LocalAgent fingerprint、完整 Envelope、跨项目 ownership 和 exact replay/conflict 语义，所以单独设计了 compatibility endpoint。
>
> LocalAgent 侧的链路是 SpanRecord → Recorder → TraceExportDispatcher → AgentEvalOpsTraceExporter → PycURL。Exporter 复用唯一 serializer，每个 envelope 只执行一次 `curl.perform()`，没有应用级 retry，hard total deadline 为默认 3 秒，同时关闭 redirect 和 proxy，并限制 response 为 4096 bytes。
>
> 服务端先做 bounded body、auth 和 Redis admission，再做 strict decode、fingerprint、ownership 和 canonical digest，最后在一个 PostgreSQL transaction 里保存 identity binding、authoritative sidecar 和 legacy projection。
>
> 最难的是数值语义。比如合法 `2**53+1` 如果转成 float 就会损精度，所以 duration 最终用 PostgreSQL NUMERIC。另一个问题是 LocalAgent 合法 attribute 可以超过 Python 默认 4300 位整数限制，所以 consumer 增加局部 custom `parse_int`。一度我们把 exact JSON codec 安装到了整个 SQLAlchemy Engine，虽然解决了 LocalAgent 问题，却改变了其他 JSONB 行为，独立 Gate 把它抓出来后，最终改成只作用于 sidecar.attributes 的 column-local TypeDecorator。
>
> 最后真实 LocalAgent → AgentEvalOps E2E 首写 201、相同 envelope replay 200、语义冲突 409，fresh session 验证 sidecar、NUMERIC duration、JSONB attributes 都正确。整个 WP4-C 最终 P0=0、P1=0。

------

# 16. 深入版本：四条主线

不要在面试中按 R1 → R2 → R3 → R4 流水账讲。

按下面四条主线讲更清晰。

## A. Contract

```text
Producer semantics
=
Wire semantics
=
Consumer semantics
=
Persistence semantics
```

------

## B. Delivery

```text
one accepted envelope
→ one transport attempt

BEST_EFFORT
+
AT_MOST_ONE_TRANSPORT_ATTEMPT_PER_ACCEPTED_ENVELOPE
```

注意这里不是：

```text
exactly-once
```

------

## C. Persistence

```text
digest
= duplicate classifier

sidecar
= authoritative truth

PostgreSQL commit
= successful server acknowledgement boundary
```

------

## D. Scope

```text
special requirement
→ special owner
```

而不是：

```text
special requirement
→ global infrastructure change
```

------

# 17. 高频追问

### Q1：为什么不用消息队列？

当前 Stage 3 目标是最小必要生产化和真实面试闭环。

需求只需要：

```text
best effort
single attempt
bounded failure
```

如果增加 Kafka / durable queue / outbox，会显著扩大系统复杂度而没有当前需求支撑。

------

### Q2：为什么不用 httpx？

PycURL 的选择已经在 transport architecture 中冻结，主要需要 libcurl 提供的 hard total deadline、TLS、DNS 等成熟传输能力。

而且 exporter 是 WP4-B single-worker 同步执行，不需要再引入 async HTTP stack。

------

### Q3：为什么关闭 retry？

因为 POST 是否已经被服务端接收，在连接异常时可能未知。

例如：

```text
server received body
→ connection reset before response
```

自动 retry 可能造成第二次 side effect。

所以当前合同选择：

```text
one attempt
```

而不是隐藏 retry。

------

### Q4：那 200 replay 不就是 exactly-once 吗？

不是。

200 replay 只是服务器能够识别同一个 envelope。

LocalAgent transport 仍然可能因为：

```text
process crash
queue loss
network failure
```

丢失 envelope。

没有 durable delivery，也没有 exactly-once delivery。

------

### Q5：为什么 Redis 不能算可靠存储？

Redis 在这里的职责只是：

```text
admission / rate limiting
```

真正 persistence ACK 是 PostgreSQL commit。

------

### Q6：为什么 attributes 不直接变成 TEXT？

因为冻结合同中的 attributes 是 JSON object，数字应保持 JSON number semantics。

TEXT 会把数据库 schema / representation 语义改变得更大。

所以保留物理 JSONB，只在 ORM column 上加适配层。

------

### Q7：为什么 legacy Trace / Span 不是权威数据？

因为旧模型不能完整保存 frozen LocalAgent Envelope 的所有字段和精度。

因此：

```text
sidecar
= authoritative

legacy Trace/Span
= compatibility projection
```

------

# 18. 最容易夸大的地方

## ❌ “已经在生产环境验证”

正确：

> 本地真实双系统环境 E2E verified。

最终 Gate 明确：

```text
Production proven = NO
```



------

## ❌ “支持 exactly-once”

正确：

```text
BEST_EFFORT
+
AT_MOST_ONE_TRANSPORT_ATTEMPT_PER_ACCEPTED_ENVELOPE
```

------

## ❌ “支持 durable delivery”

没有。

进程崩溃时 queue / in-flight envelope 仍可能丢失。

------

## ❌ “所有 AgentEvalOps JSONB 都支持 10000 位整数”

没有。

特殊 huge-int 能力只属于：

```text
localagent_trace_envelope_sidecars.attributes
```

------

## ❌ “15901 位是合同最大整数”

不是。

这是某个具体 envelope shape 下验证的 near-limit。

真正 universal contract：

```text
complete UTF-8 envelope <= 16384 bytes
```

------

## ❌ “这些 P1 都是线上事故”

不是。

绝大部分属于：

```text
source audit
implementation test
independent Gate
constructed adversarial probe
```

发现的问题。

------

# 19. P0 / P1 / P2 复习

最终状态：

```text
P0 = 0

P1 = 0

P2 = 1
└─ legacy delete divergence
   ACCEPTED_P2
```



历史上最值得复习的 P1：

| P1              | 核心问题                                  |
| --------------- | ----------------------------------------- |
| P1-01           | Wire coercion                             |
| P1-02           | Body limit after full buffering           |
| P1-03           | Producer/consumer numeric domain mismatch |
| P1-04           | Canonical digest numeric semantics        |
| P1-05           | Redis failure boundary                    |
| P1-06           | float8 authoritative truth loss           |
| P1-07           | >4300 digit integer parity                |
| Parser Boundary | raw ValueError → 500                      |
| Shared JSON P1  | Local requirement polluted global engine  |
| Transport P1    | 潜在 duplicate POST / deadline 边界       |

最终全部关闭。

------

# 20. 速查表

| 项目                     | 最终结果                                      |
| ------------------------ | --------------------------------------------- |
| Trace Contract           | v1                                            |
| Fingerprint              | `6fc033bb...390ab`                            |
| Producer serializer      | 唯一 code-owned serializer                    |
| Dispatcher               | bounded queue + single worker                 |
| Exporter                 | `AgentEvalOpsTraceExporter`                   |
| Transport                | PycURL / libcurl                              |
| HTTP attempt             | one envelope → one attempt                    |
| Retry                    | 无                                            |
| Connect timeout          | 500 ms                                        |
| Total deadline           | 3000 ms                                       |
| Redirect                 | 禁用                                          |
| Proxy env                | 禁用                                          |
| TLS verify               | 开启                                          |
| Response                 | ≤4096 bytes                                   |
| Endpoint                 | `/integrations/localagent/v1/trace-envelopes` |
| Body                     | streaming bounded ≤16384                      |
| Auth                     | project + API key                             |
| Redis                    | admission                                     |
| Duration DB              | PostgreSQL NUMERIC                            |
| Attributes DB            | PostgreSQL JSONB                              |
| Huge-int JSON            | column-local codec                            |
| First write              | 201                                           |
| Exact replay             | 200                                           |
| Conflict                 | 409                                           |
| Persistence owner        | sidecar                                       |
| ACK boundary             | PostgreSQL commit                             |
| LocalAgent full          | 2467 passed + 42 subtests                     |
| AgentEvalOps Unit        | 481 passed                                    |
| AgentEvalOps Integration | 355 passed                                    |
| P0                       | 0                                             |
| P1                       | 0                                             |
| P2                       | 1                                             |
| Cross-system E2E         | VERIFIED                                      |
| Production proven        | NO                                            |
| Exactly-once             | NO                                            |
| Durable delivery         | NO                                            |
| WP4-C                    | **COMPLETE**                                  |
| Ready for WP5            | **YES**                                       |

