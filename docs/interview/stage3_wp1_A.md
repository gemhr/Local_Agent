# LocalAgent Stage 3 WP1-A — Configuration Foundation 工程面试总结与学习材料

## 材料冲突与最终裁决

开始前先明确几处材料演进过程中出现过的冲突，以下均按照最终源码、最终测试和 Final Gate（最终门禁）裁决：

1. 初始审计时 `00_task.md` 尚不存在，后续已补充。这属于时间点差异，不属于架构冲突。
2. 旧正式文档曾写默认并发 `max_concurrency=1`，当前真实 Runtime 链路实际为 `2`；WP1-A 最终只修正文档和回归断言，**没有修改 Runtime 并发语义**。
3. Architecture Decision（架构决策）最初把部分 remote endpoint / TLS 约束描述在 Semantic Validation（语义校验），最终实现把 SERVER-only 的 endpoint / TLS 校验放入 `validate_role_configuration(SERVER)`，从而避免 CLIENT/SCRIPT 因服务端配置缺失失败。Final Gate 已正式接受这一排布。
4. GPU layers 合同经历过两次修正：最初 remediation 错误限制为 `>=0`，后续根据当前 `llama-cpp-python==0.2.90` 的真实 backend 合同修正为 `>=-1`；最后又修复了正式配置文档总则仍写 `>=0` 的冲突。最终代码、字段表、总则和 backend 四方一致。
5. 最终 Gate 为 **PASS：P0=0、P1=0、P2=4**，全仓 **1560 passed + 42 subtests passed**。WP1-A 完成，但不代表 WP1 完成、Stage 3 Production Ready（生产就绪）或 Stage 3.5 Contract Freeze（合同冻结）完成。

------

# 1. 一句话项目 / 工作包定义

我这次解决的是 **LocalAgent 原有配置系统虽然已经集中，但缺少生产环境语义、严格校验和明确启动边界，导致错误配置可能被静默纠正、延迟到运行期失败，甚至存在安全默认不明确的问题**。

最终方案是在不重构 Runtime 的前提下，把 `Settings` 建成唯一 Configuration Owner（配置所有者），补齐 Environment Profile（环境配置档）、严格配置解析、启动前语义校验、Production TLS 基线、受控降级策略和配置契约测试，使错误配置尽量在资源创建前确定性失败。

------

# 2. 为什么要做这次修改

## 2.1 原来的系统是什么状态

WP1-A 开始前，项目其实已经有一个比较好的基础：

- `core/settings.py::Settings` 已经集中读取环境变量；
- `Settings` 已经是 frozen dataclass（冻结数据类）；
- `server.py::lifespan()` 已经是唯一生产 Composition Root（组合根）；
- Server 和 Client 启动时各读取一次 Settings；
- Runtime 内部没有到处直接读取环境变量。

所以问题并不是“完全没有配置系统”。

真正的问题是：**配置读取集中，但配置合同没有生产化。**

初始源码审计确认，当时：

- 没有 LOCAL / TEST / PRODUCTION 环境层级；
- `LOCAL_AGENT_MODEL_PROFILE=fast/balanced/deep` 只是模型资源 preset，不是部署环境；
- 没有 `environment_id`；
- 没有 Settings 级 `service_version`；
- 不存在 runtime reload；
- 部分 bool 用 `value == "1"`；
- 部分数值使用 silent clamp（静默截断）；
- 部分非法配置只有到了 lifespan 甚至请求期才失败；
- `remote_verify_tls` 默认 False；
- 一批 Runtime 容量参数还是代码硬编码。

## 2.2 有没有真实用户故障触发这次修改

**没有材料证明存在一个用户真实复现的线上事故作为 WP1-A 起点。**

这是非常重要的真实性边界。

这次工作的主要触发方式属于：

> **源码审查发现生产化缺口。**

而不是：

> “线上用户因为配置错误导致系统崩溃，所以我紧急修复。”

面试时不能把它包装成真实生产事故。

## 2.3 为什么不是简单 Bug

因为这些问题跨越了多个工程层：

```text
Raw Environment
      ↓
Configuration Parsing
      ↓
Semantic Validation
      ↓
Process Role Validation
      ↓
Application Composition
      ↓
Resource Construction
      ↓
READY
```

如果只改一个 `if`，最多修复一个配置值。

真正需要解决的是：

- 谁可以读取环境变量；
- 谁决定默认值；
- 环境 Profile 与模型 Profile 谁优先；
- 什么属于配置错误；
- 什么属于资源不可用；
- 什么错误必须阻止 READY；
- 什么依赖允许 degraded startup（降级启动）；
- Production 能不能关闭 TLS；
- Client 和 Server 哪些配置可以不同；
- 哪些 Runtime 参数允许 Operator（运维人员）配置；
- 哪些参数必须继续作为 Runtime Contract（运行时合同）常量。

所以这是 **Configuration Contract + Startup Lifecycle（启动生命周期）治理问题**，而不是单点 Bug。

------

# 3. 真实性与完成边界

| 内容                                                  | 类型                      | 当前状态     | 证据                                      |
| ----------------------------------------------------- | ------------------------- | ------------ | ----------------------------------------- |
| 原系统没有 Environment Profile                        | 源码审查发现              | 已修复       | `10_zcode_audit.md`、`EnvironmentProfile` |
| 部分 bool 使用 `== "1"`，例如 `true` 会错误得到 False | 源码审查发现              | 已修复并测试 | `tests/test_settings_validation.py`       |
| 部分数值配置 silent clamp                             | 源码审查发现              | 已修复并测试 | Settings strict parser                    |
| Production TLS / HTTPS 缺少明确安全不变量             | 源码审查发现              | 已实现       | role validation + TLS tests               |
| 7 个 Runtime capacity/timeout/result-limit 参数硬编码 | 源码审查发现              | 已配置化     | Settings → server → existing consumer     |
| `REMOTE_TIMEOUT_SECONDS=0` 能越过 startup gate        | Final Gate 实施后真实发现 | 已修复       | Codex Final Gate 最小复现                 |
| Settings 唯一 env reader 缺少自动化 Guard             | Final Gate 真实发现       | 已修复       | `test_configuration_contract.py`          |
| env reader AST scanner 可被 `import os as _os` 绕过   | Re-Gate 真实发现          | 已修复       | scanner alias tests                       |
| `MODEL_GPU_LAYERS=-1` 被 remediation 错误拒绝         | Re-Gate 真实发现          | 已修复       | backend contract + boundary test          |
| 正式配置文档总则仍写 GPU `>=0`，字段表已是 `>=-1`     | Re-Gate 真实发现          | 已修复       | 文档合同测试                              |
| LOCAL/TEST/PRODUCTION Profile                         | 项目真实实现              | 已测试       | Environment Profile tests                 |
| Settings strict parsing                               | 项目真实实现              | 已测试       | Settings validation tests                 |
| SettingsValidationError taxonomy                      | 项目真实实现              | 已测试       | error catalog + tests                     |
| Process Role：SERVER/CLIENT/SCRIPT                    | 项目真实实现              | 已测试       | startup configuration tests               |
| KB Production required / Local-Test optional          | 项目真实实现              | 已测试       | lifespan KB integration tests             |
| `remote_trust_env`                                    | 项目真实实现              | 已测试       | RemoteLLMEngine + TLS/proxy tests         |
| Application Metadata                                  | 项目真实实现              | 已测试       | environment/service/instance tests        |
| Docker / Compose                                      | 后续规划                  | 未实现       | WP1-A Out of Scope                        |
| Health / Readiness Endpoint                           | 后续规划                  | 未实现       | P2                                        |
| Client/Server startup handshake                       | 后续规划                  | 未实现       | P2                                        |
| Version Fingerprint                                   | 后续规划                  | 未实现       | WP4                                       |
| Migration Runner                                      | 后续规划                  | 未实现       | WP1-A Out of Scope                        |
| Planning executor starvation                          | 既有 Known Limitation     | 未解决       | Accepted P2                               |
| Production Chaos Platform                             | 不存在                    | 未实现       | Fault 仍保持生产隔离                      |
| Stage 3 Contract Freeze                               | 后续阶段                  | 未完成       | Stage 3.5 才做                            |

Final Gate 最终明确：**P0=0、P1=0、P2=4，WP1-A completed。**

------

# 4. 修改前架构与根因

## 4.1 修改前核心调用链

初始源码审计还原出来的实际结构大致是：

```text
server.py import
    ↓
Settings.load()
    ↓
server.py::lifespan()
    ↓
RuntimeInitializationStack
    ↓
Memory / Executors / Model / Router / Tools
    ↓
Journal / Snapshot / Observability
    ↓
RuntimeFactory / ChatService
    ↓
READY
```

`Settings.load()` 实际发生在 `server.py` 模块加载阶段，而不是 lifespan 中。

## 4.2 原来的 Owner 没错，合同不完整

一个很有价值的面试点是：

> **我没有因为看到配置问题，就先把 Settings 推翻重写。**

源码审计反而证明：

```text
core.settings.Settings
```

已经是合理的单一配置 Owner。

问题是：

```text
Owner 正确
≠
Contract 完整
```

原系统存在四类职责没有完全拆开：

### ① Parse 和 Semantic Validation 混杂

例如：

- 非整数能失败；
- 但部分越界值被 clamp；
- `true` 对某些 bool 反而会解析成 False；
- unknown model profile 会 fallback balanced。

这意味着：

> “有配置”与“有效配置”没有统一语义。

### ② Application Config 与 Runtime Contract 边界不清

例如：

- worker 数；
- queue capacity；
- planning timeout；
- StepResult 容量；

实际上应该允许部署侧调整。

但：

```text
Runtime max_concurrency=2
```

属于执行语义，不应该为了“配置化”顺手放进 env。

### ③ Startup Configuration Error 与 Resource Failure 混杂

有些错误应该：

```text
Settings.load()
→ fail
```

但有些资源可用性必须等真正 construction。

典型例子：

> 不应该在 Settings 中先 `exists(path)` 再让 resource constructor open。

否则既重复 Owner，也可能产生 TOCTOU（检查时与使用时状态变化）问题。

### ④ Environment 与 Model Profile 混淆风险

已有：

```text
LOCAL_AGENT_MODEL_PROFILE
fast / balanced / deep
```

它只是模型和 RAG 资源配置。

如果直接拿它扩展为 production profile，就会把：

```text
模型资源选择
```

和：

```text
部署安全策略
```

混成一个维度。

------

# 5. 方案讨论与技术取舍

## 5.1 最关键的真实方案选择：保留 Settings

最终决定：

```text
core.settings.Settings
= 唯一 Application Configuration Owner
```

并继续使用：

```text
@dataclass(frozen=True)
```

而不是换成 Pydantic Settings。

### 为什么不换 Pydantic Settings

不是因为 Pydantic 不好。

而是因为当前系统已经有：

- 单一 env reader；
- immutable Settings；
- 明确调用关系；
- 大量既有配置字段。

此时替换框架会增加：

- 默认值迁移风险；
- error semantics 变化；
- 第二套配置模型；
- 不必要的 Diff。

面试表达：

> 我当时判断问题不是缺配置框架，而是缺配置合同，所以没有为了生产化而重写一个已经职责清晰的 Settings Owner。

------

## 5.2 为什么不引入 dotenv

真实决策是 REJECTED。

原因：

- 隐式文件加载会改变配置 precedence；
- 增加 secret 落盘面；
- 当前部署并没有必须依赖 `.env` 的需求。

------

## 5.3 为什么不拆成 Settings + RuntimeSettings

因为 WP1-A 中批准配置化的 Runtime knob 本质仍然是 Application-level operator config。

如果再增加：

```text
Settings
RuntimeSettings
```

两个配置合并层，反而会产生：

> “到底谁拥有环境变量到 Runtime 参数的映射？”

最终保持：

```text
Environment
   ↓
Settings
   ↓
Composition Root
   ↓
Runtime Factory / Executor
```

------

## 5.4 为什么不实现 Runtime Reload

明确 REJECTED。

所有配置保持：

```text
startup snapshot
restart required
```

原因主要是：

- Runtime 内有大量 Application-scope / Run-scope 状态；
- 动态配置会引入“一个 Run 使用旧值还是新值”的一致性问题；
- 当前 Stage 3 目标只是最小必要生产化，不需要配置中心。

------

## 5.5 为什么 KB 可以 degrade，但其他组件不行

最终只允许：

```text
Knowledge Base / Vector DB
```

作为业务组件 degraded startup。

LOCAL / TEST 默认 optional；

PRODUCTION 默认 required，但允许 Operator 显式：

```text
LOCAL_AGENT_KB_REQUIRED=false
```

选择 degraded。

核心原因：

- 没有 KB，基础非 RAG Chat 仍可能提供服务；
- 没有模型、Journal、Memory 等关键资源，Runtime 基础合同无法成立。

这实际上是在做：

> **Required Dependency 与 Optional Dependency 的显式分类。**

------

## 5.6 当前方案的代价

当前方案牺牲的是灵活性：

- 没有 runtime reload；
- 没有配置中心；
- 只有 LOCAL / TEST / PRODUCTION；
- Production proxy URL / CA bundle / mTLS 尚未实现；
- client/server 没有 handshake。

换来的则是：

- Owner 清晰；
- startup deterministic；
- failure 可测试；
- Scope 可控制。

对于当前阶段更合适。

------

# 6. 最终架构

## 6.1 Configuration Single Source of Truth（单一事实来源）

最终形成：

```text
Raw Environment
       │
       ▼
core.settings.Settings.load()
       │
       ├─ Parse
       ├─ Profile resolution
       ├─ Precedence
       ├─ Semantic validation
       └─ immutable Settings
               │
               ▼
       Process Role Validation
               │
               ▼
      server.py::lifespan()
        Composition Root
               │
      ┌────────┼─────────┐
      ▼        ▼         ▼
 Executors   Model     RuntimeFactory
```

最终 Final Gate 还增加了 AST-based Contract Test（基于抽象语法树的合同测试）：

> production source 中 raw environment reader 只能存在于 `core/settings.py`。

并且通过 `FrozenInstanceError` 测试锁定 Settings 不可变。

------

## 6.2 Configuration Precedence

最终唯一规则：

```text
code safe default
<
environment profile default
<
model resource preset
<
explicit environment variable
<
derived value
```

不过这里有一个重要限制：

> explicit env 优先级高，不等于可以覆盖 Production Security Invariant（生产安全不变量）。

例如：

```text
PRODUCTION
REMOTE_VERIFY_TLS=false
```

不会作为合法 override 接受，而是直接失败。

------

## 6.3 Environment Profile

最终：

```text
LOCAL
TEST
PRODUCTION
```

主要控制四类默认：

| Setting          | LOCAL | TEST  | PRODUCTION |
| ---------------- | ----- | ----- | ---------- |
| TLS Verify       | False | True  | True       |
| Remote trust_env | True  | False | False      |
| KB Required      | False | False | True       |
| environment_id   | local | test  | 必须显式   |

`LOCAL_AGENT_MODEL_PROFILE` 仍独立存在。

因此：

```text
Environment Profile
≠
Model Profile
```

------

## 6.4 Role Boundary

最终引入：

```text
SERVER
CLIENT
SCRIPT
```

一个非常值得面试讲的例子：

Remote API endpoint 是 SERVER-only requirement。

所以：

```text
SERVER
→ 必须验证 remote endpoint

CLIENT
→ 不应该因为没有 remote endpoint 而启动失败
```

这也是为什么最终把 endpoint/TLS 检查放到：

```text
validate_role_configuration(role=SERVER)
```

而不是所有进程共享的通用检查里。

------

## 6.5 7 个批准配置化的 Runtime Knob

只有以下几个进入 Settings：

```text
blocking_max_workers
blocking_max_pending_tasks
event_channel_capacity
planning_timeout_seconds
step_result_per_result_chars
step_result_run_total_chars
step_result_max_entries
```

但：

```text
max_concurrency = 2
```

继续保持 Contract Constant（合同常量）。

这是一个很典型的工程边界：

> **可调资源参数不等于执行语义也应该可调。**

------

# 7. 核心状态机和时序

WP1-A 主要涉及的是 **Application Startup State（应用启动状态）**，不是 Agent Run State。

## 7.1 Success Path

最终逻辑可理解为：

```text
Environment
    ↓
Settings Parse
    ↓
Semantic Validation
    ↓
Process Role Validation
    ↓
Application Metadata
    ↓
STARTING
    ↓
Resource Construction
    ↓
Optional Dependency Decision
    ↓
READY
```

Architecture Decision 将它定义为：

```text
Parse
→ Semantic Validation
→ Role Startup Validation
→ Resource Construction
→ Allowlisted Degradation
→ READY
```

------

## 7.2 Parse Failure

例如：

```text
REMOTE_VERIFY_TLS=tru
```

会产生：

```text
SETTINGS_PARSE_ERROR
```

不会静默变成 False。

------

## 7.3 Semantic Failure

例如：

```text
REMOTE_TIMEOUT_SECONDS=0
```

最终在 Settings 阶段得到：

```text
SETTINGS_VALIDATION_ERROR
reason=below_minimum
```

不会进入 RemoteLLMEngine，更不会等第一个请求才由 requests 报错。

------

## 7.4 Security Policy Failure

例如：

```text
Profile = PRODUCTION
backend = remote
verify_tls = false
```

由 SERVER role validation 拒绝：

```text
SETTINGS_SECURITY_POLICY_ERROR
```

核心目标：

> 在创建远端 Client 和进入 READY 前失败。

------

## 7.5 Resource Failure

真正的资源 availability（可用性）仍由 resource constructor 负责。

例如：

- SQLite 打不开；
- Local Model 无法创建；
- Snapshot 启用后初始化失败。

由已有：

```text
RuntimeInitializationStack
```

负责 rollback。

WP1-A **没有重新发明第二套资源恢复机制**。

------

## 7.6 Optional KB Failure

```text
KB init failure
```

分情况：

### LOCAL / TEST

```text
knowledge_base_required=False
→ degraded
→ 允许继续 READY
```

### PRODUCTION 默认

```text
knowledge_base_required=True
→ startup fail
```

### PRODUCTION 显式 opt-in

```text
KB_REQUIRED=false
→ degraded startup
```

注意：

> degraded 是 Application Startup Fact，不进入 AgentState。

------

## 7.7 Cancellation / Retry / Fallback / Recovery

**WP1-A 没有修改这些 Runtime 状态机。**

Final Gate 特别确认：

- RunCoordinator terminal Owner 不变；
- AgentState SSoT 不变；
- Scheduler / StepClaim 不变；
- OutputGate 不变；
- Retry 不变；
- ToolExecutionService 不变；
- Recovery 仍然只是 validation-only；
- no cross-Runtime fallback；
- shutdown ownership 不变。

因此面试时不要把 WP1-A 描述成“我重新设计了 cancellation / retry / recovery”。

------

# 8. 数据、权限与 Owner 边界

## 本次真正修改的 Owner

| 对象                                           | Owner                                |
| ---------------------------------------------- | ------------------------------------ |
| Raw Env → typed configuration                  | `Settings.load()`                    |
| Configuration precedence                       | `Settings.load()`                    |
| Settings semantic validation                   | `core/settings.py`                   |
| SERVER/CLIENT/SCRIPT required-field validation | role validator                       |
| Application resource construction              | `server.py::lifespan()`              |
| Resource rollback                              | `RuntimeInitializationStack`         |
| KB required/degraded decision                  | lifespan + resolved Settings         |
| environment/service config metadata            | Settings                             |
| instance_id                                    | Application startup identity factory |
| real event-channel capacity value              | Settings → RuntimeFactory            |
| Runtime execution semantics                    | 原 Runtime Owner，不归 Settings      |

------

## 既有 Runtime Owner，本次明确没有夺权

Final Gate 验证了：

| 职责                | 既有 Owner                               |
| ------------------- | ---------------------------------------- |
| Run terminal        | RunCoordinator                           |
| Run/Step state      | AgentState                               |
| Execution authority | Scheduler / StepClaim                    |
| Final publish       | OutputGate                               |
| Final Memory        | DELIVERED-only writer                    |
| Event sequence      | RuntimeEventChannel                      |
| Persistence facts   | Journal                                  |
| Tool execution      | ToolExecutionService                     |
| Recovery            | Validation-only Recovery                 |
| Fault activation    | Test/explicit scope，Production 不可配置 |

------

## 谁决定 Agent / Plan？

**WP1-A 材料没有重新审计 Agent Selection（Agent 选择）与 Plan Resolution（计划解析）的完整 Owner 链。**

当前材料只能证明：

- Plan freeze once 没被破坏；
- AgentState SSoT 没被破坏；
- Scheduler / StepClaim authority 没被破坏。

如果面试官继续追问“到底哪个组件决定 agent 和 plan”，应该切换到 Stage 2.5 多 Agent 架构材料回答，而不要从 WP1-A 反推。

------

# 9. 兼容策略

## 9.1 Existing Environment Variables

全部既有 env 名保留。

默认：

```text
Environment Profile = LOCAL
```

所以：

> 没有新增环境变量的已有合法本地部署，默认行为尽量保持原样。

------

## 9.2 非法配置兼容性

这里是**有意不兼容**：

原来：

```text
VERIFY_TLS=tru
→ False
```

现在：

```text
→ startup failure
```

原来：

```text
RAG_MIN_SCORE=1.5
→ clamp 1.0
```

现在：

```text
→ startup failure
```

这是故意选择：

> **兼容合法配置，不兼容非法配置的静默纠正。**

------

## 9.3 Deprecated Surface

两个旧 surface 没直接删除：

### observability shutdown timeout

保留：

```text
LOCAL_AGENT_OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS
```

但标记 DEPRECATED（弃用），不接线到 shutdown。

### ChatService.event_channel_capacity

保留 constructor shim，但 ignored。

真正 Owner 是：

```text
Settings
→ CoordinatedRuntimeFactory
→ RuntimeEventChannel
```

不为了“让旧参数继续有作用”重新制造第二 Owner。

------

## 9.4 Legacy / Static Plan / Single Step / Snapshot / Streaming

WP1-A **没有修改这些执行合同**。

最终回归证明既有 Runtime 不变量仍保持：

- 默认 COORDINATED；
- Legacy 仍是显式路径；
- no cross-Runtime fallback；
- Snapshot 既有边界不变；
- Streaming / Journal / OutputGate 未修改；
- Static Plan / Multi-Agent execution 未被本 WP 重构。

所以正确的面试表达不是：

> “WP1-A 重新兼容了 Legacy 和 Streaming。”

而是：

> “我在生产化配置改造时把这些已有执行合同作为保护边界，确保没有被配置治理改坏。”

------

# 10. Bad Cases

## Bad Case 1：`REMOTE_TIMEOUT_SECONDS=0` 能进入请求期

- **类型：实施 / Final Gate 真实发现**
- **触发条件：**

```text
PRODUCTION
environment_id=prod-a
remote endpoint=https://...
REMOTE_TIMEOUT_SECONDS=0
```

- **故障表现：**

最初：

```text
Settings.load()              PASS
Role validation              PASS
RemoteLLMEngine construction PASS
```

直到 requests 真正发送请求时才 ValueError。

- **根因分析：**

Settings 只做 lexical parse，没有按 consumer contract 对 timeout 做正数语义校验。

也就是说：

```text
type correct
≠
semantic valid
```

- **修复方案：**

审计 `_env_strict_int/_env_strict_float` 的真实 consumer contract，把 timeout 等确定需要正数的字段放到 Semantic Validation fail closed。

- **回归测试：**

```
test_remote_timeout_zero_fails_closed_before_engine_construction
```

Final Gate 最终确认：

```text
REMOTE_TIMEOUT_SECONDS=0
→ SettingsValidationError
```

- **对应知识点：**

Fail Fast（快速失败）、Semantic Validation、Configuration Contract。

- **面试表达：**

> 我后来发现仅仅做类型校验还不够，比如 timeout=0 类型上是合法整数，但下游 requests 根本不接受。如果这个值一直到首个请求才失败，服务会出现“能启动但不可用”的假 Ready，所以我把 consumer 的数值合同上移到了 startup semantic validation。

- **当前状态：已修复并覆盖。**

------

## Bad Case 2：配置 Guard 只能检查现在，不能防止未来绕过

- **类型：Re-Gate 真实发现**
- **触发条件：**

最初的自动化 scanner 可以检测：

```python
os.getenv(...)
```

但不能检测：

```python
import os as _os
_os.getenv(...)
```

- **故障表现：**

当前代码依然只有一个 env reader，但未来开发者可以轻易引入第二个配置 Owner，而 Contract Test 不报警。

- **根因：**

Guard 测的是语法字符串，而不是 import binding（导入绑定）语义。

- **修复：**

改成 AST scanner：

```text
第一遍收集 import os / import os as alias
第二遍识别 alias.getenv / alias.environ
```

同时覆盖：

- `os.getenv`
- `os.environ`
- `from os import getenv`
- `from os import environ`
- `import os as alias`
- **回归测试：**

```
tests/test_configuration_contract.py
```

Final Gate 确认 production raw env reader 仍且仅有 `core/settings.py`。

- **知识点：**

Architecture Guard（架构护栏）、Contract Test、Static Analysis（静态分析）。

- **面试表达：**

> 这个问题让我意识到架构规则如果只靠文档是不够的。Settings 是唯一配置 Owner 属于架构不变量，所以我最后把它做成 AST 级合同测试，而不是只靠 code review。

- **当前状态：已修复。**

------

## Bad Case 3：Remediation 反而拒绝了 Backend 合法值

- **类型：实施后 Re-Gate 真实发现**
- **触发条件：**

第一次修复 numeric ranges 时：

```text
MODEL_GPU_LAYERS >= 0
```

- **故障表现：**

`llama-cpp-python==0.2.90` 实际支持：

```text
-1 = all layers GPU offload
```

但 Settings 把 `-1` 拒绝了。

- **根因：**

把“常见合理范围”当成了真实 consumer contract。

这是一个非常典型的：

> **Validation Overreach（校验过度）**

问题。

- **修复：**

根据真实 backend 合同改成：

```text
<-1 reject
-1 accept
0 accept
positive accept
```

- **回归测试：**

```
test_model_gpu_layers_backend_contract
```

覆盖：

```text
-2 reject
-1 accept
0 accept
1 accept
```

- **知识点：**

Consumer Contract、Compatibility、Boundary Value Testing（边界值测试）。

- **面试表达：**

> 我第一次做严格校验时也踩了一个坑：严格不等于越严格越好。GPU layers 的 -1 是底层 backend 明确定义的 sentinel，所以必须以真实 consumer contract 为准，而不是按直觉写 >=0。

- **当前状态：已修复。**

------

## Bad Case 4：代码正确，但正式 Contract 自相矛盾

- **类型：Re-Gate 真实发现**
- **触发条件：**

实现和字段表已经：

```text
GPU layers >= -1
```

但 Configuration Reference 总则仍写：

```text
GPU layers >= 0
```

- **故障表现：**

同一份正式 Configuration Contract 给出两个合法范围。

- **根因：**

实现修复后，合同文档的多个表达位置没有同步形成防漂移测试。

- **修复：**

修正文档总则，并增加：

```
test_gpu_layers_contract_is_consistent_between_rules_and_table
```

只锁定必要合同事实，而不是整篇 Markdown snapshot。

- **知识点：**

Documentation as Contract（文档即合同）、Contract Drift（合同漂移）。

- **面试表达：**

> 最后一轮 Gate 没有再发现代码 Bug，而是发现正式配置合同自己的总则和字段表矛盾。我仍然把它当 blocker，因为生产配置文档也是 Operator 的接口。如果代码和合同不一致，后续部署仍会产生错误。

- **当前状态：已修复，Final Gate PASS。**

------

## Bad Case 5：非法 bool 被静默解析

- **类型：源码审查真实发现，不是用户事故**
- **触发条件：**

旧实现部分 bool：

```text
value == "1"
```

所以：

```text
VERIFY_TLS=true
```

会得到 False。

- **风险：**

对于普通 feature flag 可能只是行为错误；对于 TLS 就可能直接改变安全语义。

- **修复：**

统一 strict bool，只接受：

```text
1 / 0 / true / false
```

其他显式输入 fail closed。

- **回归：**

Settings strict parsing tests。

- **知识点：**

Fail Closed（失败关闭）、Secure Default（安全默认）。

- **当前状态：已修复。**

------

## Bad Case 6：Production 使用 HTTP Remote Endpoint

- **类型：假设构造 Bad Case**
- **触发条件：**

```text
Environment = PRODUCTION
backend = remote
endpoint = http://...
```

- **故障表现：**

应该在启动前拒绝。

- **根因：**

如果只验证字符串非空，就无法表达 Production transport security policy。

- **修复：**

SERVER role validation：

```text
Production remote/hybrid
→ HTTPS required
→ verify_tls=True required
```

- **回归测试：**

Production HTTP / TLS-disabled negative tests。

- **知识点：**

Security Policy Validation。

- **当前状态：假设负向场景已测试覆盖，不是实际事故。**

------

# 11. 测试与验收

## 11.1 单元 / Contract Test

WP1-A 新增了多类测试：

- Settings strict parsing；
- Environment Profile；
- Application Metadata；
- Startup role boundary；
- Configuration ownership AST guard；
- Configuration Reference contract consistency。

Phase 3 初始新增约 108 项测试；后续 remediation 继续增加 numeric boundary、scanner self-test 和文档一致性测试，最终全仓收集数增加到 1560。

------

## 11.2 Integration Test

真实 lifespan 集成测试验证：

- Local KB failure → degraded；
- Test KB failure → degraded；
- Production KB default required → fail；
- Production 显式 KB optional → degraded；
- role validation 在首个 resource construction 之前执行。

------

## 11.3 E2E / Regression

最终全量：

```text
uv run python -m pytest -q
```

结果：

```text
1560 passed
3 warnings
42 subtests passed
```

Final Gate 独立重新执行，而不是引用 ZCode 历史结果。

------

## 11.4 最终 Gate 实际执行

```text
uv run python -m pytest -q tests/test_runtime_configuration_reference.py
→ 4 passed

uv run python -m pytest -q
  tests/test_settings_validation.py
  tests/test_configuration_contract.py
→ 75 passed

uv run python -m pytest --collect-only -q
→ 1560 collected

uv run python -m pytest -q
→ 1560 passed + 42 subtests
```

静态检查：

```text
python -m compileall main.py server.py core tests
PASS

uv lock --check
PASS

git diff --check
PASS

git diff -- pyproject.toml uv.lock
empty
```

------

## 11.5 Fault Injection

WP1-A **没有建设新的 Fault Injection（故障注入）能力**。

实际验证的是：

```text
test_runtime_fault_production_isolation.py
```

即：

> 新 Settings / API 改造没有意外增加 Production Fault Controller 激活面。

不能说：

> “WP1-A 做了生产 Chaos 测试。”

------

## 11.6 未执行 / 未证明

材料明确说明：

- 没有真实外部模型调用；
- 没有真实网络调用；
- 没有真实向量数据库外部依赖测试；
- 没有统一 lint/type-check gate。

所以：

> **1560 passed 是仓库自动化回归通过，不等于真实生产环境认证。**

------

# 12. 当前 Known Limitations

## 12.1 Planning Executor Starvation

仍是 Accepted P2。

虽然：

```text
blocking_max_workers
blocking_max_pending_tasks
```

已经可配置，

但这**不代表 starvation 已解决**。

面试不能说：

> “我通过 worker 配置化解决了 Planning 饥饿问题。”

正确说法：

> “配置化只是增加运维调节能力，原有 Planning 与 specialist 共享 executor 的容量风险仍然作为 P2 保留。”

------

## 12.2 Client / Server Startup Handshake

未实现。

当前 client/server：

```text
各自 Settings.load()
各自 startup snapshot
```

没有自动验证：

```text
service version
environment_id
schema compatibility
```

是否一致。

后续生产化才需要 handshake。

------

## 12.3 Health / Readiness

未实现 HTTP：

```text
/health
/readiness
```

也没有正式：

```text
READY_DEGRADED
```

对外投影。

当前只有应用内部 startup/degraded fact。

所以不能说：

> “已经有 Kubernetes readiness。”

------

## 12.4 Deprecated Fixture Warning

最终全量仍有：

```text
3 DeprecationWarning
```

来自测试 fixture 继续向：

```text
ChatService.event_channel_capacity
```

传递已经 ignored 的 compatibility shim。

属于 P2，不影响真实 capacity Owner。

------

## 12.5 Docker / Compose

未实现。

WP1-A 是 Configuration Foundation，不是 Deployment 完成。

------

## 12.6 Migration Runner

未实现。

没有建立生产 DB Schema Migration（数据库模式迁移）能力。

------

## 12.7 Version Fingerprint

只完成：

- `environment_id`
- `service_version`
- `instance_id`

这些基础 metadata。

完整：

```text
runtime / prompt / model / toolset / KB fingerprint
```

属于后续 WP4。

------

## 12.8 Recovery

WP1-A 没有改变 Recovery。

整个项目在当前合同里仍是：

```text
Recovery Validation
```

不是：

```text
Automatic Recovery / Durable Resume
```

不能说自动恢复。

------

## 12.9 分布式能力

这轮配置生产化仍然是单机应用语义。

没有：

- distributed config；
- distributed locks；
- distributed exactly-once；
- distributed runtime state。

------

## 12.10 Production Ready

Final Gate 自己明确：

> WP1-A completed ≠ WP1 completed ≠ Stage 3 production ready ≠ Production certified。

------

# 13. 这次修改真正体现了哪些工程能力

## 13.1 Configuration Contract Design

证据：

- Environment Profile；
- precedence；
- strict parsing；
- numeric consumer contract；
- role-specific required fields。

体现的不是“会读 env”，而是：

> 能定义配置值从输入到运行时的完整语义。

------

## 13.2 Single Source of Truth / Owner 设计

证据：

```text
core.settings.Settings
```

保持唯一 Configuration Owner。

最终还增加 AST Contract Test 防止未来第二 env reader。

这是很典型的架构治理能力。

------

## 13.3 Composition Root 设计意识

没有让：

- Agent；
- Runtime；
- Tool；
- Health；
- Model；

自己读取并解释环境变量。

所有 wiring 继续集中到：

```text
server.py::lifespan()
```

说明你理解：

> “组件应该接收依赖，不应该自己发现应用级配置。”

------

## 13.4 Fail Fast / Fail Closed

典型例子：

```text
REMOTE_TIMEOUT_SECONDS=0
```

从：

```text
首个请求才失败
```

变成：

```text
startup Settings validation 失败
```

------

## 13.5 Security Baseline

证据：

- Production HTTPS；
- Production TLS verification；
- `remote_trust_env`；
- Settings error 不保存 secret/raw value/provider URL/path。

注意这是配置和 transport security baseline，不是完整 Security Sandbox。

------

## 13.6 Compatibility Engineering

没有追求“一次性删干净”：

- existing env names 保留；
- LOCAL 默认保持本地兼容；
- dead surface 用 deprecated shim；
- Runtime execution semantics 不顺手修改。

------

## 13.7 Contract / Boundary Testing

这轮最有价值的一点之一。

不仅测试功能：

> “Settings 能不能 load。”

还测试架构规则：

> “未来有没有人绕过 Settings 直接读 env。”

最终甚至对 formal documentation 增加合同一致性测试。

------

## 13.8 Gate-Driven Engineering

第一次 Phase 3：

```text
1525 passed
```

并没有直接认为完成。

Codex Gate 连续发现：

1. timeout range；
2. architecture guard 不够；
3. GPU sentinel；
4. scanner alias；
5. formal contract drift。

最后才到：

```text
P0=0
P1=0
1560 passed
```

这非常适合面试强调：

> **测试全绿不代表架构 Gate 就应该 PASS。**

------

# 14. 30 秒面试表达

我在 LocalAgent 的 Stage 3 里做过一轮配置和启动链的生产化。原系统其实已经有集中 Settings，但没有 Local/Test/Production 环境语义，而且有些 bool 会静默解析错误、数值会 clamp，甚至非法 timeout 能一直到首个请求才失败。我没有重写 Runtime，而是保留 Settings 作为唯一配置 Owner，补了严格解析、Environment Profile、SERVER/CLIENT role 校验、Production TLS 和 KB 降级策略，并把几个资源型 Runtime 参数接入配置。最后通过多轮 Final Gate 把 timeout、GPU sentinel、配置 Owner Guard 和正式文档漂移几个问题都修掉，全仓最终 1560 个测试通过，P0/P1 清零。不过这只代表 WP1-A 配置基础完成，还不是整个 Stage 3 Production Ready。

------

# 15. 2 分钟面试表达

这次工作的背景是 LocalAgent 已经完成了 Runtime 和多 Agent 主链路，我开始做 Stage 3 最小生产化。源码审查时发现配置系统有一个特点：它已经有统一的 `Settings`，这点其实是好的，但配置合同还比较偏开发阶段。

比如当时没有 Local、Test、Production 的环境 Profile；部分 bool 是用 `value == "1"` 解析，所以配置成 `true` 反而会得到 False；有些非法数字会被静默 clamp，还有些配置错误会拖到 lifespan 或请求期才暴露。另外 TLS、KB 是否允许降级、哪些 Runtime 参数允许运维调整，也没有一个统一边界。

我最后没有换成 Pydantic Settings，也没有引入 dotenv 或动态配置中心，因为问题不是没有框架，而是 Owner 和合同没定义完整。我继续让 `Settings.load()` 做唯一 env reader，在这里完成 profile、precedence、strict parse 和 semantic validation，然后再做 SERVER、CLIENT、SCRIPT 的 role validation。真正的资源可用性仍由 `server.py::lifespan()` 和 `RuntimeInitializationStack` 负责，这样不会把配置校验和资源 Owner 混在一起。

Production Profile 下我加了 HTTPS 和 TLS verification 的安全不变量，KB 默认 required；Local/Test 可以 degraded。然后只把 worker、event capacity、planning timeout 和 StepResult limit 这些资源型参数配置化，没有把 Runtime `max_concurrency=2` 暴露成 env，因为那属于执行合同。

这个阶段比较有意思的是，Phase 3 全量测试已经通过，但 Codex Final Gate 还是连续卡了几轮：先发现 timeout=0 能拖到 requests 才失败；修数值范围时又误伤了 llama.cpp 支持的 `GPU_LAYERS=-1`；配置 Owner 的 AST Guard 一开始还会被 `import os as _os` 绕过；最后甚至发现正式配置文档总则和字段表不一致。全部关闭后最终是 1560 passed、42 个 subtests，P0=0、P1=0、P2=4。

我会把这个工作描述成配置与启动生命周期的生产化，而不会说整个项目已经 Production Ready，因为 Docker、Health/Readiness、handshake 和后续部署工作都还没完成。

------

# 16. 深入版本

如果让我深入讲，我觉得这次最关键的不是新增了多少环境变量，而是重新划清了三个 Owner。

第一个是 Configuration Owner。原来 `Settings` 已经集中读取 env，所以我没有重新做一套配置框架，而是把它真正变成 raw env 到 immutable typed configuration 的唯一事实来源。Profile default、Model preset、显式 env 和 derived value 的 precedence 都只能在这里发生一次。为了防止以后架构退化，我甚至做了 AST 级 contract test，扫描 production source，发现第二个 raw env reader 就失败。

第二个是 Composition Root。`Settings` 只负责配置是否合法，不负责证明 SQLite、模型文件或者向量库此刻一定能打开。真正的资源 construction 仍留在 `server.py::lifespan()`，失败后由 `RuntimeInitializationStack` rollback。这样配置语义和资源生命周期不会混成两个 Owner。像路径如果先在 Settings `exists()`，之后再 open，其实还有 TOCTOU，而且会重复资源 Owner。

第三个是 Runtime Contract。我们当时发现 RuntimeFactory 默认 `max_concurrency=2`，但旧文档写成 1。我没有趁生产化把它变成一个环境变量，因为并发度不是普通部署参数，它会影响调度和执行语义。我只配置化 worker、pending queue、planning timeout、event capacity 和 StepResult limit 这些容量类参数。

失败路径也做了明确分层。词法错误是 `SETTINGS_PARSE_ERROR`，合法类型但范围错误是 `SETTINGS_VALIDATION_ERROR`，Production TLS 这类安全不变量是 `SETTINGS_SECURITY_POLICY_ERROR`，SERVER-only required field 用 startup role validation。真正资源失败仍然是 RuntimeInitializationError。只有 KB 是 allowlisted optional dependency；Local/Test 默认可以 degrade，Production 默认 required。

Trade-off 是整个系统还是 startup snapshot，没有 runtime reload，也没有配置中心。这会少一些灵活性，但当前 Runtime 有很多 application-scope 和 run-scope 状态，动态配置反而会产生一个请求到底使用旧配置还是新配置的一致性问题，所以在最小生产化阶段我选择确定性。

这次 Final Gate 也给我一个很明显的经验：测试全绿只是必要条件，不是充分条件。比如 Phase 3 全量已经过了，但架构 Review 还是发现 timeout=0 会到请求期才失败；第一次修 range 又把 backend 合法的 `GPU_LAYERS=-1` 拒绝掉；架构 Guard 也有 alias 绕过；最后代码已经正确，正式 Contract 还自相矛盾。所以生产化 Gate 其实还要检查 Owner、兼容性、negative case 和文档合同，而不只是 pytest。

------

# 17. 高频追问与参考答案

## Q1：既然 Python 项目有 Pydantic，为什么不用 Pydantic Settings？

**参考答案：**

我当时先看的是现有 Owner，而不是先选框架。当前 `core.settings.Settings` 已经是 frozen dataclass，而且 production raw env read 本身已经集中在 `core/settings.py`。问题主要是 strict parsing、environment profile、precedence 和 startup validation 不完整。如果这个时候换 Pydantic，会扩大迁移面，而且有一段时间很容易出现两套默认值和错误语义。所以 Architecture Decision 明确保留原 Settings，只增强合同。最终还用 `test_configuration_contract.py` 锁定唯一 env reader。

------

## Q2：为什么配置错误一定要在启动阶段失败？

**参考答案：**

不是所有错误都应该 startup fail，但“确定性的非法配置”应该尽早失败。

最典型的是 `REMOTE_TIMEOUT_SECONDS=0`。Final Gate 实际复现过，它最初能通过 Settings、role validation 和 RemoteLLMEngine construction，直到第一个 requests 调用才 ValueError。这样服务可能已经处于 READY，但第一笔业务一定失败。

所以修复后 timeout 的 contract range 在 Settings semantic validation 中校验。但像数据库能不能打开这种 availability，我没有放到 Settings，而是继续让 resource constructor 判断。

------

## Q3：那为什么不在 Settings 里检查所有文件存在？

**参考答案：**

因为配置合法和资源当前可用是两个 Owner。路径字符串是否合法可以属于 Settings，但文件实际能不能打开应该由资源 constructor 决定。如果在 Settings 里先 `exists()`，后面 constructor 再 open，一是重复校验，二是有 TOCTOU。当前 `server.py::lifespan()` 和 `RuntimeInitializationStack` 已经有资源初始化和 rollback 语义，所以我没有复制第二套。

------

## Q4：Profile 和 Model Profile 有什么区别？

**参考答案：**

Environment Profile 是部署语义，目前只有 LOCAL、TEST、PRODUCTION，决定 TLS、trust_env、KB required 和 environment_id 这类默认。

`LOCAL_AGENT_MODEL_PROFILE=fast/balanced/deep` 是资源 preset，决定模型和 RAG 资源参数。

它们管理的字段集合必须不重叠，否则 precedence Owner 会变得不清晰。

------

## Q5：为什么 Production Profile 默认 TLS=True，但 LOCAL 还是 False？

**参考答案：**

这是兼容性和生产安全之间的取舍。LOCAL 默认 False 是为了保持已有本地开发环境兼容；TEST/PRODUCTION 默认 True。Production 还额外有安全不变量：remote/hybrid 必须 HTTPS，并且显式配置 `verify_tls=false` 也会失败，所以不能通过普通 env override 绕过。

我不会说 LOCAL=False 是推荐的生产安全实践，它只是本地兼容默认。

------

## Q6：`trust_env` 是什么？为什么要配置？

**参考答案：**

这里控制的是 `requests.Session` 是否自动继承系统的 proxy 环境变量。

之前项目没有显式控制，所以行为由 requests 默认决定。现在 Remote LLM Session 的 `remote_trust_env` 进入 Settings：LOCAL 默认 True，TEST/PRODUCTION 默认 False，Production 仍允许 operator 显式开启。

我们没有把 proxy URL 或 credential 本身放入项目 Settings，这部分部署 secret 边界还在后续工作。

------

## Q7：为什么 max_concurrency 没有一起配置化？

**参考答案：**

因为我对参数做了分类，不是看到常量就都做成 env。

worker 数、pending queue、planning timeout、StepResult limits 更偏资源和容量，可以交给部署配置。

但 `max_concurrency=2` 会直接影响 Scheduler 和 ParallelExecutor 的执行语义。当时还发现旧文档写 1、源码实际 effective 是 2，我只修正文档和 regression test，没有借 WP1 改 Runtime 并发。

`20_codex_decision.md` 对这条链路有完整核查。

------

## Q8：为什么 KB 可以 degraded，Model 不行？

**参考答案：**

因为 KB 对当前核心非 RAG Chat 不是绝对必要资源。Local/Test 下 KB 失败可以继续提供基础能力。

Production 默认把 KB 设为 required，但允许 operator 显式 opt-in degraded。

模型、Journal、ApplicationRuntimeServices 等资源如果缺失，基础 Runtime 合同就无法成立，所以不能把任意 constructor exception 都吞掉然后 READY。

------

## Q9：SettingsValidationError 为什么不能直接保存原始 value，排障不是更方便吗？

**参考答案：**

因为配置里可能有 API key、provider URL、内部路径等敏感信息。

我们把 error projection 限制成：

```text
safe_error_code
field / env name
reason_code
```

而不是 raw value/raw exception。运维可以知道“哪个配置、哪类错误”，但不会把密钥或内部地址带入 Event、Journal、Trace 或日志。

------

## Q10：你这次最大的 Bug 是什么？

**参考答案：**

如果从工程价值看，我比较愿意讲 `REMOTE_TIMEOUT_SECONDS=0`。

因为它体现了“类型正确不代表语义正确”。配置能成功加载、程序能构造 Client，但真正业务请求必然失败，相当于 Ready 是假的。Final Gate 发现后，我不是只修 timeout，而是沿 consumer contract 审计所有 numeric env reader，再做边界测试。

如果想讲更难一点的，我会讲后续 `GPU_LAYERS=-1`：第一次 remediation 太严格，反而破坏了 backend 的合法 sentinel，这个说明配置校验必须以真实 consumer contract 为准。

------

## Q11：为什么配置 Owner 要做 AST Test？Code Review 不够吗？

**参考答案：**

因为这是长期架构不变量。

如果只靠文档说“所有 env 都要从 Settings 读”，未来很容易有人在某个 tool 或 script 里顺手写 `os.getenv()`。

所以最终测试扫描：

```text
core/
tools/
ui/
scripts/
server.py
main.py
```

并要求 raw env read 只允许 `core/settings.py`。

第一次 scanner 还被 `import os as _os` 绕过，Re-Gate 又逼着我们把 alias binding 也补上。这个案例非常能说明 Architecture Test 本身也需要 negative test。

------

## Q12：为什么没有 Runtime Reload？

**参考答案：**

因为当前配置是 Application-scope startup snapshot。Runtime 下面还有 Run-scope 状态，如果在运行中修改 worker、timeout、模型或安全策略，就必须定义已经开始的 Run 使用哪个版本、资源是否重建、如何回滚等语义。

Stage 3 当前目标是最小生产化，所以选择 restart-required，避免引入一个没有真实需求支撑的动态配置系统。

------

## Q13：这个阶段算 Production Ready 吗？

**参考答案：**

不算。

准确边界是：

> WP1-A Configuration Foundation completed。

最终 Gate 虽然 P0/P1 已清零，但仍有 Planning starvation、无 client/server handshake、无 Health/Readiness 等 P2，而且 Docker、Migration、后续 Deployment 也没有完成。所以我不会说整个系统已经 Production Ready。

------

## Q14：你们是不是实现了 Recovery？

**参考答案：**

不是这次。

现有 Runtime 的 Recovery 仍是 validation-only，Final Gate 还专门确认没有变成 replay、writeback 或 live recovery。

所以不能把它说成 automatic recovery，更不能说 durable execution 已经完成。

------

## Q15：1560 个测试是不是说明生产一定没问题？

**参考答案：**

不是。

它只能证明当前仓库定义的自动化场景全部通过，而且 Final Gate 还独立重跑了一次。

材料明确说明没有真实外部模型、真实网络和真实向量库环境验证，也没有统一 lint/type-check。所以它是很强的回归证据，但不是生产认证或性能认证。

------

# 18. 容易答错或夸大的问题

## 问题：这是不是一次线上事故整改？

**容易出现的错误回答：**

“生产环境遇到错误配置导致服务崩溃，所以我重构了配置。”

**为什么错误：**

现有材料没有用户真实生产事故。

**推荐回答：**

“这是 Stage 3 生产化过程中通过源码审查发现的配置与启动合同缺口，后续 Gate 在实现过程中又真实发现了多个边界 Bug。”

------

## 问题：你把配置系统重构成 Pydantic 了吗？

**错误回答：**

“对，我把 Settings 生产化了，所以换成了 Pydantic Settings。”

**为什么错误：**

实际明确 REJECTED。

**推荐回答：**

“我反而保留了 frozen dataclass Settings，因为现有 Owner 已经正确，重点是补合同而不是换框架。”

------

## 问题：Runtime 并发可以在线配置了吗？

**错误回答：**

“可以，max_concurrency 现在也能从环境变量配置。”

**为什么错误：**

明确没有。

**推荐回答：**

“资源型 worker/queue/timeout 做了配置化，但 Runtime effective `max_concurrency=2` 仍然是合同常量。”

------

## 问题：Planning starvation 修了吗？

**错误回答：**

“worker 数可以配置，所以解决了。”

**为什么错误：**

Final Gate 仍将其列为 P2。

**推荐回答：**

“没有，配置化只能调容量，不能消除架构上的共享 executor 饥饿风险。”

------

## 问题：你们有 Health / Readiness 了吗？

**错误回答：**

“有，Startup Validation 就是 readiness。”

**为什么错误：**

Startup Validation 和 HTTP Readiness 是两层不同概念。

**推荐回答：**

“启动链已经能判断是否可以进入 READY，但正式 Health/Readiness endpoint 和 degraded projection 还没实现。”

------

## 问题：KB 故障会自动恢复吗？

**错误回答：**

“会 degraded，然后 Recovery 自动恢复。”

**为什么错误：**

Degraded Startup 不等于 Recovery。

**推荐回答：**

“KB optional 时可以带 degraded fact 启动，但没有自动恢复链；Recovery 仍然是 validation-only。”

------

## 问题：是不是实现 Exactly-once 配置应用？

**错误回答：**

“Settings 是 immutable，所以 exactly-once。”

**为什么错误：**

这是完全不同的语义。

**推荐回答：**

“配置是每进程 startup snapshot，不做 runtime reload；这里不涉及分布式 exactly-once。”

------

## 问题：你们生产安全已经完成了吗？

**错误回答：**

“Production Profile 强制 TLS，所以安全生产化已经完成。”

**为什么错误：**

只是最小配置 / transport security baseline。

**推荐回答：**

“这轮只完成 Production TLS、proxy inheritance 和 safe config error 等基础边界，Workspace、Permission、完整 Sandbox 等属于后续安全 WP。”

------

## 问题：1560 个测试是不是 1560 个新测试？

**错误回答：**

“这次写了 1560 个测试。”

**为什么错误：**

1560 是最终全仓测试数量。

**推荐回答：**

“最终全仓 1560 passed；这轮新增的是 Settings、Environment、Startup、Metadata、Contract Guard 等测试，前后分几轮增加。”

------

# 19. 本次需要重点复习的知识点

## P0：必须掌握

### 1. Configuration Owner / Single Source of Truth

必须能解释：

- 为什么只能有一个 raw env reader；
- 为什么每组件自行 getenv 会导致 precedence 和测试失控；
- 为什么 immutable Settings 有价值。

项目证据：

`core.settings.Settings` + AST ownership guard。

------

### 2. Parse Validation vs Semantic Validation

必须能举例：

```text
"abc" 作为 timeout
→ parse error

0 作为 timeout
→ type correct，semantic invalid
```

这是本次最重要的知识点之一。

------

### 3. Composition Root

至少掌握：

> 应用级依赖和配置在哪组装，以及为什么组件内部不应该自行发现环境。

项目：

`server.py::lifespan()`。

------

### 4. Fail Fast / Fail Closed

必须能解释：

- 哪些配置错误应该 startup fail；
- 为什么 Production security policy 不应该 fallback；
- 为什么 invalid env 不能当成 env 缺失。

------

### 5. Owner 与 Scope

要能区分：

```text
Application Scope
Run Scope
Step Scope
```

本 WP 最重要的是：

Settings = Application Scope；

不要让它成为 Run State Owner。

------

### 6. Configuration Precedence

必须能背清：

```text
code default
< Environment Profile
< Model preset
< explicit env
< derived
```

以及 Production invariant 可以拒绝非法 override。

------

## P1：很可能被追问

### 7. Environment Profile vs Feature/Model Profile

为什么不能混。

### 8. Role-based Configuration Validation

SERVER/CLIENT/SCRIPT 为什么需要不同 required fields。

### 9. TOCTOU

为什么 Settings 不先验证文件存在，再让 resource constructor 打开。

### 10. Secure Error Projection

为什么 error 只暴露：

- safe code；
- field；
- reason；

而不是 raw value。

### 11. Contract Test / Architecture Test

为什么 AST scanner 是合同测试，而不是普通单元测试。

### 12. Compatibility vs Strictness

核心理解：

> strict validation 不是“越严格越好”。

GPU `-1` Bad Case 是最佳例子。

### 13. Required vs Optional Dependency

KB degraded startup 是最佳例子。

------

## P2：扩展知识

### 14. Dynamic Configuration / Hot Reload

了解：

- versioning；
- atomic swap；
- in-flight request consistency；
- resource recreation；

即可。

项目目前没实现。

### 15. Secret Store

了解 Kubernetes Secret、Vault、Cloud Secret Manager 等概念即可。

项目当前没有实现这些。

### 16. Readiness / Liveness

理解区别：

- Liveness：进程是不是活着；
- Readiness：是不是应该接受流量；
- Degraded：部分功能不可用是否算 ready。

后续很可能会用到。

### 17. Config Schema Migration

了解配置字段 rename/deprecation/version migration。

当前未实现 Migration Runner。

------

# 20. 最终面试速查表

| 维度             | 我需要记住的核心内容                                         |
| ---------------- | ------------------------------------------------------------ |
| 问题             | Settings 已集中，但缺 Environment Profile、严格语义校验、Production 安全和启动边界 |
| 根因             | Configuration Owner 正确，但 Configuration Contract 不完整   |
| 核心方案         | Settings 单一 Owner + strict parsing + profile + role validation + startup fail-closed |
| 最大设计取舍     | 不换 Pydantic、不做 runtime reload、不顺手修改 Runtime 执行语义 |
| 最难 Bad Case    | timeout=0 请求期才失败；修 range 后又误伤 GPU `-1` sentinel  |
| 核心状态机       | Parse → Semantic → Role → Resource → Optional Degrade → READY |
| 核心 Owner       | Settings / lifespan / RuntimeInitializationStack 各自职责分离 |
| 配置化范围       | worker、pending、event capacity、planning timeout、StepResult limits |
| 没配置化         | Runtime `max_concurrency=2`、Retry、Tool、Recovery           |
| 安全             | Production HTTPS + verify TLS；safe Settings errors；trust_env 显式 |
| KB               | Local/Test 默认 optional；Production 默认 required，可显式 opt-in degraded |
| 测试             | Final Gate 全仓 1560 passed + 42 subtests，compileall/lock/diff PASS |
| Final Gate       | P0=0、P1=0、P2=4                                             |
| Known Limitation | Planning starvation、3 warnings、无 handshake、无 Health/Readiness |
| 不能说           | Production Ready、自动 Recovery、Exactly-once、分布式配置、生产 Chaos |
| 30 秒关键词      | 配置合同、唯一 Owner、Fail Fast、Environment Profile、Role Validation、Gate |
| 最容易被追问     | 为什么不换 Pydantic、为什么 timeout=0 必须 startup fail、为什么 GPU -1 合法、为什么 KB 可 degrade、为什么 max_concurrency 不配置化 |