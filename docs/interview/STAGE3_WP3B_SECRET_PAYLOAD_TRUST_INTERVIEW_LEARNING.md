# Stage 3 WP3-B — Secret / Payload / Trust 工程面试学习材料

推荐文件名：

```
STAGE3_WP3B_SECRET_PAYLOAD_TRUST_INTERVIEW_LEARNING.md
```

------

# 1. 一句话项目 / 工作包定义

WP3-B 的目标，是给 LocalAgent 补齐最小生产安全基线中的 **Secret（敏感凭据）、Payload（请求载荷）和 Trust Boundary（信任边界）**：

> 在请求进入 Runtime（运行时）、Planner（规划器）、Model（模型）和 Tool（工具）之前，先建立明确的 HTTP 请求资源边界，同时保证 Server Secret 不通过错误链路泄漏，并明确 loopback、`agent_id`、Provider 返回等数据到底“能被信任到什么程度”。

最终状态：

```text
WP3-B Final Documentation Re-Gate = PASS

P0 = 0
P1 = 0
P2 = 1 retained Known Limitation cluster

TEST_GAP = 0
DOC_DRIFT = 0
ENVIRONMENT_BLOCKED = 0

WP3-B completed = YES
Allowed to continue WP3-C = YES
```

但：

```text
WP3 Aggregate PASS = NO
Stage 3 PASS = NO
```

因为 WP3-C Injection / Security Gate 尚未完成。

------

# 2. 为什么要做

Scout（侦察审计）发现，当时 LocalAgent 已经有：

- Runtime Budget；
- Tool output cap；
- Model `max_tokens`；
- RuntimeEvent / Journal / Trace / Metric 安全投影；
- credential-safe Settings；
- WP3-A Resource Authorization（资源授权）。

但**没有 inbound HTTP Payload Boundary**。

真实源码审查和 Direct Probe（直接探针）确认：

```text
HTTP body-size protection = NOT_IMPLEMENTED

query max = NONE
file_path max = NONE
agent_id max = NONE

Pydantic extra = ignore
```

并且：

- 256 KiB 的 unknown JSON field 可以被接收和解析后再被忽略；
- 64 KiB 的 `query` 可以通过 HTTP；
- 64 KiB 的 `file_path` 可以通过 HTTP；
- 64 KiB 的 `agent_id` 可以通过 HTTP。

核心风险不是“模型一定崩溃”，而是：

> 请求在 Runtime Budget 生效之前，就已经发生网络接收、内存分配、JSON 解析和 Prompt 拼接。

因此：

```text
Runtime Budget
!= inbound HTTP resource boundary
```

这是 WP3-B 最核心的工程问题。

------

# 3. 真实性与完成边界

## 3.1 真实源码审查 / Direct Probe 发现

### P1-01

```text
Unbounded raw HTTP body
```

属于：

```text
SOURCE_AUDIT_FINDING
+
DIRECT_PROBE_FINDING
```

不是用户生产事故。

### P1-02

```text
Unbounded semantic/resource fields
```

覆盖：

- query
- file_path
- agent_id
- run_id
- search keyword
- history pagination
- message_ids

同样来自源码审查和直接探针，不是用户事故。

### P1-03

第一次 Final Gate 后发现：

```text
history limit default = 10
history offset default = 0
```

虽然行为正确，但：

```text
server.py
```

自己持有 literal `10/0`，

而：

```text
RequestPayloadPolicy
```

没有持有这两个 default。

这是：

```text
SOURCE_CONTRACT_FINDING
```

即 **Single Source of Truth（单一事实来源）合同问题**，不是安全绕过。

### P1-04

第二次 Re-Gate 又发现正式 Runtime 文档没有显式记录：

```text
WAF = NOT_IMPLEMENTED

Prompt Injection protection = NOT_IMPLEMENTED
DEFERRED_TO_WP3_C
```

属于：

```text
DOC_CONTRACT_FINDING
```

也不是生产事故。

------

# 4. 修改前架构与根因

修改前大致是：

```text
HTTP
  ↓
FastAPI / Pydantic
  ↓
Route
  ↓
ChatService
  ↓
Runtime
  ↓
Planner / Model / Tool
```

问题在于 FastAPI / Pydantic 只完成：

```text
JSON parsing
type validation
```

却没有：

```text
raw body resource boundary
semantic field resource boundary
```

而且：

```text
Pydantic extra=ignore
```

意味着攻击者即使不用合法业务字段，也可以发送：

```json
{
  "query": "x",
  "unused": "<very large string>"
}
```

这个 `unused` 最终虽然会被忽略，但已经经历：

```text
network receive
→ memory allocation
→ JSON parse
```

所以仅依赖 Pydantic 字段限制是不够的。

------

# 5. 方案讨论与取舍

## 5.1 Raw Body Gate 放在哪里

讨论了三个方案。

### 方案 A：Pure ASGI Middleware（纯 ASGI 中间件）

优点：

- 在 FastAPI / Pydantic 解析前执行；
- 可以限制实际收到的 raw bytes；
- unknown JSON field 也无法绕过；
- application-wide。

最终采用。

### 方案 B：Route-level Check

拒绝原因：

> Route 被调用时，Request Body 已经被框架读取和解析。

此时再检查大小已经太迟。

### 方案 C：只使用 Pydantic `max_length`

拒绝原因：

> 它只能约束被 schema 识别的字段，不能约束 ignored JSON。

所以最终架构明确：

```text
Transport Payload Boundary
!= Semantic Field Boundary
!= Runtime Budget
```

三层必须分离。

------

## 5.2 为什么不用 `Content-Length` 直接判断

因为：

```text
Content-Length
```

是 caller 提供的声明，不是最终事实。

可能出现：

```text
Content-Length: 10
```

但实际收到：

```text
> 1 MiB
```

因此最终合同是：

```text
Content-Length
= early rejection optimization

actual ASGI receive bytes
= final authority
```

这也是面试中最值得讲的细节之一。

------

## 5.3 为什么没有把 limits 放进 Settings

Codex 最终选择：

```text
hard-coded frozen policy
```

而不是 operator-configurable Settings。

原因：

1. 当前仓库没有 benchmark 能证明合理可调范围；
2. 配置化会扩大生产配置合同；
3. 允许用户随意调大，等于产生 fail-open 风险；
4. 当前目标只是 Minimal Production Baseline。

因此这些数字是：

> 安全 / 运维 Policy Maximum（策略上限），不是系统性能最大值。

------

# 6. 最终架构

最终链路：

```text
HTTP Request
   ↓
RequestBodyLimitMiddleware
   │
   │ raw body bytes
   │ max = 1 MiB
   ↓
FastAPI / Pydantic
   │
   │ semantic field limits
   ↓
Endpoint
   ↓
ChatService / Memory Service
   ↓
Runtime
   ↓
Planner / Model / Tool
```

两个核心 Owner：

```text
RequestPayloadPolicy
→ numeric policy owner

RequestBodyLimitMiddleware
→ raw HTTP body enforcement owner
```

两者均为：

```text
APPLICATION_SCOPE
INTERNAL_RC
```

没有升级为 PUBLIC_STABLE。

------

# 7. 核心状态与时序

实现中没有专门定义一个 Payload StateMachine 类，因此面试时不要说“我实现了 Payload 状态机”。

可以描述其**逻辑时序**：

```text
RECEIVE
  ↓
validate Content-Length
  ↓
buffer bounded ASGI chunks
  ↓
count actual bytes
  ├─ invalid header → 400
  ├─ body > limit → 413
  ├─ disconnect → stop
  └─ valid body
        ↓
     replay_receive
        ↓
     FastAPI / Pydantic
        ↓
     endpoint
```

安全关键点是：

```text
完整确认 body 未超限
之前
不得把任何 prefix 转发给 downstream
```

否则下游 Parser 已经开始处理，前置 Gate 就失去意义。

实际 Final Gate 已验证：

- missing `Content-Length`；
- lying-small `Content-Length`；
- multi-chunk；
- ignored oversized JSON；
- exactly-at-limit；
- bounded buffer；
- no prefix forwarding。

------

# 8. 数据 / 权限 / Owner

这是面试里非常值得强调的一组边界。

## 8.1 Payload Policy

```text
RequestPayloadPolicy
```

拥有所有 numeric policy facts。

最终包括：

```text
HTTP body = 1,048,576 bytes

query = 32,768 chars
file_path = 4,096 chars
agent_id = 64 chars
run_id = 45 chars

search keyword = 1,024 chars

history limit:
default = 10
range = 1..100

history offset:
default = 0
range = 0..100,000

message_ids:
max count = 1,000

message_id:
1..9,223,372,036,854,775,807
```

## 8.2 Pydantic / FastAPI

负责：

```text
field shape / length / range
```

但不负责：

```text
Agent identity validity
Tool permission
Filesystem authorization
```

## 8.3 Agent Registry

继续负责：

```text
agent_id 是否是合法 Agent
```

而不是 Pydantic 复制一套 Registry。

## 8.4 ResourceAuthorizationService

继续负责：

```text
Tool request能不能访问指定 filesystem resource
```

WP3-B 不重做 WP3-A。

------

# 9. 兼容策略

WP3-B 没有为了安全“无脑收紧”所有输入。

几个重要兼容决策：

### `query=""` 保留

因为 UI 已经允许：

```text
empty query + file_path
```

所以没有添加：

```text
min_length=1
```

### whitespace / NUL / Unicode control

没有在 WP3-B 统一禁止。

原因：

> 当前没有证据证明它们形成 security Authority bypass，贸然过滤属于文本 normalization，不是本工作包的资源安全问题。

### unknown JSON

继续：

```text
extra=ignore
```

没有切成：

```text
extra=forbid
```

因为改成 forbid 会改变 API 兼容行为。

raw body limit 已经解决 ignored field 的资源边界问题。

### run_id

没有改成：

```python
UUID
```

类型。

继续：

```text
str
→ max_length=45
→ uuid.UUID() validation
```

从而保持 caller 原始 UUID 表示形式。

------

# 10. Bad Cases

## Bad Case 1：大 unknown JSON 绕过字段限制

真实性：

```text
DIRECT_PROBE_FINDING
```

修改前：

```text
large unused JSON field
→ Pydantic ignore
→ request accepted
```

修复后：

```text
raw body > 1 MiB
→ 413
→ downstream = 0
```

------

## Bad Case 2：伪造偏小 Content-Length

真实性：

最初属于对抗场景，后被 Direct Probe / durable tests 覆盖。

```text
Content-Length: 10
actual body > 1 MiB
```

必须：

```text
413
```

因此不能只信 Header。

------

## Bad Case 3：Runtime Budget 被误当 Request Limit

属于架构错误假设。

Runtime Budget 发生在：

```text
Run creation之后
```

但 HTTP body 已经：

```text
receive
allocate
parse
```

因此：

```text
Runtime Budget
不能替代 Request Payload Gate
```

------

## Bad Case 4：两个 Owner 保存同一 default

真实 Final Gate Finding：

```text
RequestPayloadPolicy
+
server.py literal 10/0
```

行为虽然完全正确，但合同不通过。

为什么重要：

未来如果改成：

```text
policy = 20
route = 10
```

就会出现 silent drift。

修复后：

```text
RequestPayloadPolicy = sole owner
server.py = consumer
```

并增加 introspection owner guard。

------

## Bad Case 5：实现完成但文档把未实现能力说模糊

真实 Final Re-Gate Finding：

正式文档没有明确：

```text
WAF NOT_IMPLEMENTED
Prompt Injection NOT_IMPLEMENTED
```

因此 Re-Gate 再次 FAIL。

最终补齐：

```text
Payload Gate != WAF

Prompt Injection protection = NOT_IMPLEMENTED
DEFERRED_TO_WP3_C
```

------

# 11. Tests / Gate

## 11.1 WP3-B Targeted

P1-03 修复后：

```text
90 passed
```

## 11.2 E2E repeatability

三次独立：

```text
34 passed
34 passed
34 passed
```

无 transport state leak / flakiness。

## 11.3 WP3-A Regression

```text
42 passed
```

## 11.4 Settings / Deployment

```text
180 passed
```

## 11.5 Provider

```text
18 passed
```

## 11.6 Runtime Security

```text
74 passed
```

## 11.7 Full Regression

最终代码修复后的完整回归：

```text
2006 collected
2006 passed
4 warnings
42 subtests passed
0 failed
```

并且：

```text
compileall = PASS
uv lock --check = PASS
git diff --check = PASS
pyproject.toml / uv.lock diff = EMPTY
```

### 非常重要的真实性限制

最终 **Documentation Re-Gate** 没有重新跑 2006 tests。

它只运行了：

```text
49 passed
```

的文档 / Security Boundary 定向测试以及静态检查，并继承上一轮已经真实执行通过的 2006 full regression 证据。

面试时不要说：

> 最终一轮又跑了 2006 个测试。

正确说：

> 代码 Final Re-Gate 时全量 2006 passed；后续只有文档补救，因此最终 Documentation Re-Gate 只做了 49 个定向测试和静态检查。

------

# 12. Known Limitations

WP3-B PASS 后仍然明确没有实现：

```text
Human IAM
Inbound Local API TLS
Inbound API Rate Limit
Generic DLP
WAF
Prompt Injection protection
Full Sandbox
```

同时保留：

- FastAPI 422 会回显 caller invalid input；
- Uvicorn access log 可能记录 URL / query；
- Desktop UI 某些本地异常会打印 raw `agent_id` / exception；
- `client_trust_env` 存在 operator/system proxy residual risk；
- payload limits 是 policy maxima，不是 benchmark maxima；
- unexpected ASGI-message path 是窄实现边界。

------

# 13. 工程能力体现

这个工作包最值得面试表达的不是“我加了几个 max_length”，而是以下能力。

### 1. 分层安全边界

识别出：

```text
raw transport resource bound
semantic field bound
Runtime execution budget
```

是三个不同问题。

### 2. Security Authority 分离

没有把：

```text
Payload validation
Agent identity
Tool permission
Resource authorization
```

混成一层。

### 3. Fail Closed（失败关闭）

- malformed Content-Length → 400；
- oversized body → 413；
- semantic invalid → 422；
- rejection 不创建 fake Run。

### 4. Single Source of Truth

Final Gate 发现行为虽然正确，但 defaults Owner 分裂，仍然拒绝 PASS。

### 5. Contract-driven Gate

2000+ tests 全绿仍不等于系统完成。

Final Gate 继续从：

```text
source ownership
documentation truth
mandatory durable evidence
```

发现问题。

这是非常典型的生产工程思维。

------

# 14. 30 秒回答

> 我在 LocalAgent Stage 3 做过一个 Payload / Secret / Trust 的生产安全基线。最核心的问题是原来的 FastAPI 请求没有 raw body 和字段资源限制，而 Runtime Budget 是 Run 创建之后才生效，挡不住请求解析前的资源消耗。我最后拆成两层：最前面用 pure ASGI middleware 按实际 receive bytes 做 1 MiB body cap，不能只信 Content-Length；然后再用 Pydantic/FastAPI 对 query、file_path、agent_id、history pagination 等做 semantic limits。超限请求在 ChatService 和 Runtime 创建前就被 400/413/422 拒绝，不生成 RuntimeEvent 或 Journal。同时保留已有 credential-safe projection 和 loopback trust boundary。最后全量 2006 tests 通过，P0/P1 清零。

------

# 15. 2 分钟回答

> WP3-B 主要解决三个问题：Secret、Payload 和 Trust。
>
> Audit 时发现 Secret 这一块已有一定基础，比如 `remote_api_key` 和 `wiki_cookie` 不进入 Settings repr，Provider 的 401、timeout、500 等错误也经过 Adapter 转成安全错误，RuntimeEvent、Journal、Trace、Metric 都没有发现 credential 泄漏。
>
> 真正的 blocker 是 Payload。原来没有 request body size limit，query、file_path、agent_id 也没有长度限制，而且 Pydantic 默认 `extra=ignore`。我们实际 probe 过，大 unknown JSON 和 64 KiB 字段都能进入服务。所以我没有把 Runtime Budget 当安全边界，因为 Runtime Budget 生效的时候 HTTP body 已经被接收和解析了。
>
> 最终架构分成 Transport Boundary 和 Semantic Boundary。Transport 层用 pure ASGI middleware，对实际收到的 bytes 做 1 MiB 限制。Content-Length 只用于提前拒绝，最终 authority 是 actual receive bytes，因此 missing header、伪造偏小 header、multi-chunk 都覆盖。合法 body 完整验证后才 replay 给 FastAPI，拒绝前不允许向下游发送 prefix。
>
> Semantic 层用 Pydantic/FastAPI 对 query、file_path、agent_id、run_id、search、history pagination 和 memory delete 数量做限制。所有 rejection 都发生在 ChatService 和 Run 创建之前，所以 RUN_STARTED、Planner、Model、Tool、RuntimeEvent、Journal 都不会发生。
>
> 这个阶段也有两个很有代表性的 Final Gate 问题：第一次虽然功能测试全绿，但 history 默认值 10/0 同时存在于 policy 和 route，违反单一事实源；第二次代码都通过了，文档却没有明确 WAF 和 Prompt Injection 尚未实现，所以 Gate 又拒绝 PASS。最后补齐后 WP3-B 才正式完成。

------

# 16. 深入版本

如果面试官继续追问中间件怎么做，可以回答：

> Middleware 不仅检查 Content-Length，而是直接包 ASGI `receive()`。我会先有限缓冲所有 `http.request` chunk，累计真实 bytes。如果超过 1 MiB，就丢弃 buffer 并直接 413，不调用下游。只有完整 body 确认合法后，才创建 request-local replay receive，把原始 message 顺序和 `more_body` 语义重放给 FastAPI。这样可以保证 Pydantic 在请求确定合法之前完全看不到 body prefix。
>
> 另外这个 middleware 是 application-wide，而不是只挂在 `/api/chat`，所以 `DELETE /api/memory` 这类带 body 的 endpoint 也自动受保护。
>
> semantic validation 仍然由 FastAPI/Pydantic 负责，因为 raw transport gate 不知道业务字段含义。这样 transport Owner 和 semantic Owner 是分开的。

------

# 17. 高频追问

## Q1：为什么不只加 `max_length`？

因为 ignored JSON 可以绕过：

```text
field validation
```

但不能绕过：

```text
raw body cap
```

------

## Q2：为什么不能只检查 Content-Length？

因为它是 caller 声明，可以：

- 缺失；
- 伪造偏小；
- 多 header；
- 与真实 chunk 大小不一致。

最终必须统计实际 ASGI bytes。

------

## Q3：为什么不用 Runtime Budget？

因为 Runtime Budget 太晚。

它解决：

```text
Planner / Model / Tool execution cost
```

而 Payload Gate 解决：

```text
HTTP receive / allocation / parse
```

------

## Q4：为什么 `query` 允许空？

因为已有产品行为允许：

```text
empty query + file_path
```

不能为了安全随意破坏兼容。

------

## Q5：为什么不 `extra=forbid`？

因为 unknown field 是否应该拒绝属于 API compatibility。

raw body Gate 已经解决 resource issue，所以没有必要把安全改造扩大成 API schema tightening。

------

## Q6：为什么 history default 重复也算 P1？

因为生产合同强调单一事实源。

两个 Owner 今天都写 10 不代表未来不会：

```text
policy=20
route=10
```

Final Gate 检查的是长期可维护性，而不仅是当前 output 对不对。

------

## Q7：你做了 Prompt Injection 防护吗？

没有。

当前只证明：

```text
existing deterministic Tool / Resource / Payload policies
不会因为文本输入直接被重新配置
```

但系统化的：

- User Prompt Injection；
- RAG Injection；
- Tool Result Injection；
- Memory Injection；
- Instruction / Data authority separation；

都明确留给 WP3-C。

------

## Q8：你做 WAF 了吗？

没有。

Payload Gate 只是：

```text
single-request resource bounds
```

不包括：

- generic abuse detection；
- bot detection；
- distributed request filtering；
- per-user rate limit；
- WAF rule engine。

------

# 18. 易夸大 / 易答错

### 错误 1

> 我们实现了 DoS 防护。

不准确。

应该说：

> 实现了单请求 Payload Resource Bounding。

------

### 错误 2

> 1 MiB 是 LocalAgent 最大支持的请求。

错误。

应该说：

> 1 MiB 是当前 WP3-B 冻结的生产 Policy Maximum，不是 benchmark 得出的系统极限。

------

### 错误 3

> LocalAgent 已经有完整 Prompt Injection 防护。

错误。

WP3-C 尚未完成。

------

### 错误 4

> `agent_id` 就是登录用户身份。

错误。

```text
agent_id
= routing / executing Agent identity
!= authenticated human identity
```

------

### 错误 5

> 最终 Gate 跑了 2006 tests。

不够准确。

正确：

> 代码 Re-Gate 跑了 2006 full regression；最后纯文档 Re-Gate 只跑了 49 个定向测试和静态检查。

------

### 错误 6

> Health 顺序问题修好了。

错误。

它仍然是：

```text
PRE_EXISTING_ORDER_DEPENDENCY
P2 KNOWN_LIMITATION
```

且未在 WP3-B 修复。

------

# 19. P0 / P1 / P2 复习

## P1-01

```text
Unbounded raw HTTP body
```

来源：

```text
SOURCE_AUDIT_FINDING
DIRECT_PROBE_FINDING
```

最终：

```text
FIXED
```

------

## P1-02

```text
Unbounded semantic/resource fields
```

最终：

```text
FIXED
```

------

## P1-03

```text
History default numeric owner split
```

来源：

```text
SOURCE_CONTRACT_FINDING
```

修复：

```text
RequestPayloadPolicy owns default/range
server.py consumes
durable owner guard
real HTTP omitted-query E2E
```

最终：

```text
FIXED
```

------

## P1-04

```text
Formal docs missing WAF /
Prompt Injection non-capability facts
```

来源：

```text
DOC_CONTRACT_FINDING
```

最终：

```text
FIXED
DOC_DRIFT = 0
```

------

## P2 retained

当前仍保留：

```text
422 caller echo
Uvicorn caller URL/query logging
UI local raw logging
No IAM
No TLS
No inbound rate limit
No DLP
No WAF
No Prompt Injection
No full Sandbox
Health order dependency
```

P2 不阻塞当前 WP3-B loopback-only Minimal Production Baseline。

------

# 20. 速查表

| 问题                       | 面试回答关键词                            |
| -------------------------- | ----------------------------------------- |
| WP3-B 做了什么             | Secret + Payload + Trust Boundary         |
| 最大核心问题               | HTTP input resource boundary 缺失         |
| 为什么 Runtime Budget 不够 | 生效太晚，HTTP 已 receive/parse           |
| Body Gate                  | pure ASGI middleware                      |
| Body Authority             | actual receive bytes                      |
| Content-Length             | early optimization only                   |
| Body cap                   | 1 MiB policy max                          |
| query                      | 32,768 chars                              |
| file_path                  | 4,096                                     |
| agent_id                   | 64                                        |
| run_id                     | 45                                        |
| search keyword             | 1,024                                     |
| history limit              | default 10, 1..100                        |
| history offset             | default 0, 0..100000                      |
| message_ids                | max 1000                                  |
| raw body错误               | 400 / 413                                 |
| semantic错误               | 422                                       |
| rejection位置              | before ChatService / Run                  |
| rejection RuntimeEvent     | 0                                         |
| rejection Journal          | 0                                         |
| Payload Policy Owner       | RequestPayloadPolicy                      |
| Body enforcement Owner     | RequestBodyLimitMiddleware                |
| `agent_id`                 | Agent identity，不是 human auth           |
| Secret production修改      | 无新增，主要做 regression                 |
| Provider error             | fake 401/403/timeout/500/malformed 都安全 |
| WAF                        | NOT_IMPLEMENTED                           |
| Prompt Injection           | NOT_IMPLEMENTED，WP3-C                    |
| Full code regression       | 2006 passed                               |
| Final doc gate             | 49 targeted，不是 2006                    |
| Final状态                  | P0=0, P1=0, TEST_GAP=0, DOC_DRIFT=0       |
| 下一阶段                   | WP3-C Injection / Security Gate           |

------

## 最后应该真正记住的 5 句话

1. **Runtime Budget 不是 HTTP Payload Boundary，因为它生效得太晚。**
2. **Transport Body Limit 和 Semantic Field Limit 必须分层，二者不能互相替代。**
3. **Content-Length 只是声明，actual ASGI receive bytes 才是资源限制的最终事实。**
4. **测试全绿不代表生产合同完成：Single Source of Truth 和正式文档真实性同样可以阻断 Final Gate。**
5. **WP3-B 建立的是 Payload / Secret / Trust 最小基线，不是 WAF、IAM、DLP、Sandbox 或 Prompt Injection 全套安全平台。**