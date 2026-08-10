# LocalAgent Stage 3 WP1-D — Migration / Operations Closure 面试学习材料

# 1. 一句话项目 / 工作包定义

我在 LocalAgent 的最小生产化阶段，为本地持久化数据补齐了一套 Migration / Operations Closure（迁移 / 运维闭环）：

> 对 LocalAgent 自己拥有的持久化 Store（存储）在 Server 启动前执行只读兼容性 Preflight（预检），需要修改已有数据时只允许 Operator（运维人员）显式执行 Migration（迁移）；同时冻结停服 Backup / Restore（备份 / 恢复）、forward-only rollback（仅前向迁移回滚）和 Chroma（向量库）重建边界，避免代码升级后静默打开不兼容数据。

最终采用的不是完整数据库迁移平台，而是：

```text
automatic read-only preflight
+
explicit migration
+
manual stopped-server backup/restore
+
forward-only rollback
```

最终 WP1-D Final Semantic Re-Gate（最终语义复验）：

```text
P0 = 0
P1 = 0
P2 = 1
TEST_GAP = 0
DOC_ONLY = 0
ENVIRONMENT_BLOCKED = 0

WP1-D Final Semantic Re-Gate = PASS
WP1-D completed = YES
```

全量回归：

```text
1760 collected
1760 passed
0 skipped
42 subtests passed
4 existing warnings
```

其中 `P2=1` 是此前已经接受的 Planning executor starvation（规划执行器饥饿），不是 WP1-D 新增缺陷。

------

# 2. 为什么要做

## 2.1 “数据已经落盘”不等于“可以安全升级”

WP1-D 开始前，LocalAgent 已经拥有多种持久化数据：

```text
Memory
Runtime Event Journal
Snapshot
Observability Checkpoint
Chroma
```

但真正的 Deployment Migration（部署迁移）能力并不存在：

```text
Migration Runner = NOT_IMPLEMENTED
Automatic backup = NOT_IMPLEMENTED
Deployment restore = NOT_IMPLEMENTED
Rollback data compatibility = NOT_DEFINED
```

当时只有零散 compatibility-on-open（打开时兼容）：

- Memory 缺列时自动 `ALTER TABLE ADD COLUMN`；
- Journal 缺 `span_id / parent_span_id` 时自动 `ALTER`；
- Journal Reader（读取器）能同时读 v1/v2；
- Snapshot 遇到未知版本会 fail closed（失败关闭）；
- Chroma 只能通过重新 ingest（摄入）重建。

问题在于：

```text
CREATE TABLE IF NOT EXISTS
```

不能证明：

```text
schema migration supported
```

同样：

```text
SQLite file exists
```

也不能证明：

```text
current binary can safely read it
```

------

## 2.2 升级失败最大的风险不是“启动报错”

更危险的是：

```text
旧数据其实不兼容
↓
新版本却认为它兼容
↓
Server 进入 READY
↓
业务继续写入
```

这就是典型的 **fail-open（失败开放）**。

WP1-D 最后两轮 Final Gate 真正抓到的就是这种问题：

> malformed（畸形的）SQLite schema 明明已经改变业务约束，但 Preflight 却错误判成 `CURRENT`。

因此 WP1-D 的重点最终从：

```text
“有没有 migration 命令？”
```

深化成：

```text
“我们到底凭什么判断这个数据库是兼容的？”
```

------

# 3. 真实性与完成边界

| 能力 / 事件                                         | 真实性                  | 当前状态               |
| --------------------------------------------------- | ----------------------- | ---------------------- |
| 项目已有 5 类逻辑 Persistent Store                  | 源码审查发现            | 已确认                 |
| 之前无统一 Migration Runner                         | 源码审查发现            | 已确认                 |
| 之前无自动 Backup / Deployment Restore              | 源码/文档审查发现       | 已确认                 |
| Minimal Persistence Migration Coordinator           | 本 WP 真实实现          | 已实现并测试           |
| Server startup read-only Preflight                  | 本 WP 真实实现          | 已实现并测试           |
| 显式 `SCRIPT_ROLE` Migration CLI                    | 本 WP 真实实现          | 已实现并测试           |
| Memory `PRAGMA user_version=1`                      | 本 WP 真实实现          | 已实现并测试           |
| Journal 显式 span-column migration                  | 本 WP 真实实现          | 已实现并测试           |
| Snapshot migration                                  | 架构明确禁止            | 未实现                 |
| Checkpoint recreate-on-incompatibility              | 本 WP 真实实现          | 已实现并测试           |
| Chroma LocalAgent marker                            | 本 WP 真实实现          | 已实现并测试           |
| Chroma internal DB migration                        | Third-party boundary    | NOT_LOCAL_SCHEMA_OWNER |
| 自动 Backup                                         | 明确非目标              | NOT_IMPLEMENTED        |
| 自动 Restore                                        | 明确非目标              | NOT_IMPLEMENTED        |
| Downgrade migration（降级迁移）                     | 明确非目标              | NOT_IMPLEMENTED        |
| Online backup（在线备份）                           | 明确非目标              | NOT_IMPLEMENTED        |
| Runtime automatic recovery                          | Stage2 contract         | validation-only        |
| 第一轮 Final Gate 三个 physical signature P1        | Codex 真实审查发现      | 已修复                 |
| 第一次修复后新的 semantic UNIQUE / canonicalizer P1 | Codex 真实 Re-Gate 发现 | 已修复                 |
| 1760 full tests                                     | Final Re-Gate 实际执行  | PASS                   |

WP1-D 完成只表示：

```text
Migration / Operations Closure completed
```

不等于：

```text
WP1 aggregate completed
Stage 3 completed
Production certified
Disaster recovery completed
High availability
```

------

# 4. 修改前架构与根因

## 4.1 原来每个 Store 自己“顺手兼容”

修改前大致是：

```text
Server startup
     ↓
construct MemoryManager
     ↓
CREATE TABLE IF NOT EXISTS
     ↓
if missing column:
    ALTER TABLE

construct Journal
     ↓
if missing span columns:
    ALTER TABLE
```

这种方式最大的特点是：

> **兼容性检测和兼容性修改混在普通构造函数里。**

这意味着：

```text
Server startup
```

本身可能修改已有数据。

------

## 4.2 没有统一升级边界

以前没有：

```text
Persistence Preflight
Migration Required
Unsupported Schema
Operator Confirmation
```

这些明确阶段。

因此升级路径更接近：

```text
open
→ try to adapt
→ continue
```

而不是：

```text
detect
→ classify
→ operator decides
→ migrate
→ validate
→ start
```

------

## 4.3 Backup 和 Rollback 也只有原则，没有闭环

WP1-B 已经提到：

```text
backup
restore
deployment rollback
```

但 Migration 明确 deferred 到 WP1-D。

当时并没有真正回答：

```text
旧 binary 能不能读新 schema？
迁移成功以后还能不能直接回滚 exe？
backup 要备份哪些文件？
backup 以后怎么证明它能恢复？
```

WP1-D 就是用来把这些答案冻结下来。

------

# 5. 方案讨论与技术取舍

## 5.1 为什么没有做完整 Migration Framework

Codex Architecture Decision（架构决策）比较了三种方案。

### Option A：各 Store 继续 compatibility-on-open

优点：

```text
改动最小
```

缺点：

```text
没有统一 preflight
没有 backup 门槛
升级与 rollback 不透明
Server startup 会隐式改库
```

被拒绝。

### Option B：Minimal Persistence Migration Coordinator

最终选择。

它只负责：

```text
preflight
version / shape detection
ordered migration orchestration
safe result aggregation
```

而真正 SQL 仍属于：

```text
MemoryManager
SQLiteRunEventJournal
SQLiteEventConsumptionCheckpointStore
```

等 Store Owner。

### Option C：完整 Migration Registry / Framework

包括：

```text
version graph
downgrade
cross-store transaction
HA migration
rolling migration
```

对当前 Windows 单机单进程 LocalAgent 明显过度设计，因此拒绝。

------

## 5.2 为什么选择“自动 Preflight + 显式 Migration”

最终不是：

```text
Server startup
→ auto migrate
```

而是：

```text
Server startup
→ automatic READ-ONLY preflight
```

如果发现旧 schema：

```text
MIGRATION_REQUIRED
→ startup fail
```

然后 Operator：

```text
stop server
backup
preflight backup
migrate --backup-confirmed
start server
```

原因是：

> 当前没有自动 Backup 能力，因此 Server 没有证据证明修改数据前已经有可恢复的备份。

如果自动 migration：

```text
startup
→ schema mutation
```

一旦改库以后新版本又启动失败，code rollback 就可能失去安全数据基础。

所以最终选择：

> **自动发现问题，但不自动修改已有持久数据。**

这是一个非常好的生产化设计取舍。

------

## 5.3 为什么 Memory 要正式版本化，但 Journal 不加 DB version

### Memory

Memory：

```text
不可重建
业务价值高
以前没有显式 DB schema version
存在真实 additive migration
```

所以正式引入：

```text
PRAGMA user_version = 1
```

------

### Journal

Journal 已经拥有：

```text
journal_schema_version
```

但这是：

```text
record schema version
```

不是：

```text
SQLite physical schema version
```

当前 Journal 已知 physical delta 只有：

```text
span_id
parent_span_id
```

所以最终不再加一个 DB `user_version`，而是：

```text
exact physical signature
+
record v1/v2 compatibility
```

未来如果再出现第二轮 Journal physical schema evolution（物理模式演进），Architecture Decision 明确要求重新审视 DB-level version，而不是继续堆隐式 `ALTER`。

------

## 5.4 为什么 Snapshot 不迁移

Snapshot 当前：

```text
schema version = v1
opt-in
Recovery = validation-only
```

Unknown version：

```text
fail closed
```

因此第一版没有必要为 Snapshot 发明 migration。

最终：

```text
current exact v1
→ supported

anything incompatible
→ UNSUPPORTED
```

而不是：

```text
try repair
try upgrade
```

------

## 5.5 为什么 Checkpoint 直接 recreate

Observability Checkpoint（可观测性检查点）只保存：

```text
logger_projector
metrics_projector
```

消费事件的幂等 offset。

它属于：

```text
REBUILDABLE_DERIVED_STATE
```

不是业务事实。

所以不值得：

```text
version graph
row migration
```

直接采用：

```text
incompatible
→ explicit RECREATE
```

但注意：

```text
rebuildable
```

不等于：

```text
startup optional
```

当前它仍是 startup-required component。

这是一个很典型的：

> **运行时关键性和备份关键性是两个不同维度。**

------

## 5.6 为什么 Chroma 不做 internal migration

LocalAgent 不拥有：

```text
chroma.sqlite3
```

的内部 schema。

因此：

```text
Chroma internal schema migration
=
NOT_LOCAL_SCHEMA_OWNER
```

LocalAgent 真正拥有的是：

```text
collection contract
chunk schema
embedding compatibility
```

所以 WP1-D 做的是：

```text
LocalAgent collection marker
+
compatibility validation
+
operator rebuild
```

而不是直接 UPDATE Chroma internal tables。

------

# 6. 最终架构

## 6.1 总体架构

```text
                ┌─────────────────────┐
                │       Settings      │
                └──────────┬──────────┘
                           │
                           ▼
              SERVER_ROLE validation
                           │
                           ▼
                    lifecycle STARTING
                           │
                           ▼
        ┌─────────────────────────────────┐
        │ Persistence Migration Coordinator│
        │                                 │
        │      READ-ONLY PREFLIGHT        │
        └───────────────┬─────────────────┘
                        │
         ┌──────────────┼──────────────┐
         ▼              ▼              ▼
      Memory         Journal       Snapshot/
                                   Checkpoint
         │
         ▼
    compatible?
         │
    yes  │   no
         │
         │      MIGRATION_REQUIRED /
         │      UNSUPPORTED / FAILED
         │               ↓
         │          startup FAIL
         ▼
 resource construction
         │
         ▼
   Chroma open
         │
         ▼
 marker validation
         │
         ▼
 remaining Runtime
         │
         ▼
        READY
```

------

## 6.2 显式 Migration 路径

```text
Operator
   │
   ▼
stop Server
   │
   ▼
manual backup
   │
   ▼
FULL preflight on backup copy
   │
   ▼
scripts/manage_persistence.py
migrate --backup-confirmed
   │
   ▼
Coordinator
   │
   ├── Memory migration
   ├── Journal migration
   └── Checkpoint recreate
```

------

## 6.3 Owner Map

| Concern                          | Owner                                           |
| -------------------------------- | ----------------------------------------------- |
| Migration orchestration          | Persistence Migration Coordinator               |
| Memory schema truth              | `MemoryManager`                                 |
| Journal physical schema truth    | `SQLiteRunEventJournal`                         |
| Journal row/digest/version truth | `JournalRecord`                                 |
| Snapshot schema truth            | Snapshot contract / `SQLiteSnapshotStore`       |
| Checkpoint schema truth          | `SQLiteEventConsumptionCheckpointStore`         |
| Chroma internal schema           | Chroma dependency                               |
| LocalAgent Chroma compatibility  | `VectorDBManager` + document schema             |
| Startup / READY                  | `server.py::lifespan()` + WP1-C lifecycle owner |
| Backup / Restore / Rollback      | Operator Runbook                                |

Coordinator **不拥有 Store schema SQL**，只是编排者。

------

# 7. 核心状态机和时序

## 7.1 Preflight 状态

第一版：

```text
NEW
CURRENT
MIGRATION_REQUIRED
REBUILD_REQUIRED
UNSUPPORTED
FAILED
```

基本语义：

```text
NEW
→ Store 不存在，可由 current constructor 初始化

CURRENT
→ 当前 binary 可以安全使用

MIGRATION_REQUIRED
→ 已知旧结构，允许显式迁移

REBUILD_REQUIRED
→ 主要用于 Chroma，需要 Operator rebuild

UNSUPPORTED
→ 结构/版本不属于支持集合

FAILED
→ 无法完成可信预检
```

------

## 7.2 Startup

```text
STARTING
   │
   ▼
read-only preflight
   │
   ├── CURRENT / NEW
   │          ↓
   │       continue
   │
   └── MIGRATION_REQUIRED /
       UNSUPPORTED /
       FAILED
              ↓
        startup failure
              ↓
        never READY
```

`/readyz` 不执行 Migration。

------

## 7.3 Migration

```text
FULL preflight ALL stores
        │
        ├── UNSUPPORTED / FAILED
        │        ↓
        │     stop, zero mutation
        │
        └── only supported migrations
                 ↓
        require --backup-confirmed
                 ↓
       Store 1 transaction
                 ↓
       Store 2 transaction
                 ↓
       Store 3 transaction
```

没有：

```text
cross-store transaction
```

------

## 7.4 单 Store Migration

```text
BEGIN IMMEDIATE
       ↓
revalidate from-state
       ↓
approved schema mutation
       ↓
validate resulting current shape
       ↓
version marker update if applicable
       ↓
COMMIT
```

异常：

```text
ROLLBACK
```

目标是：

```text
safely re-runnable
```

而不是：

```text
exactly-once
```

------

# 8. 数据、权限与 Owner 边界

## 8.1 五个逻辑 Store

最终正式口径：

```text
logical persistent store count = 5
physical persistence units     = 5
```

分别：

1. Memory
2. Event Journal
3. Snapshot
4. Observability Checkpoint
5. Chroma

KB Source（知识库源文件）不是 Runtime Store。

Model artifact（模型制品）也不是 Durable Store。

------

## 8.2 数据价值分类

### 不可重建

```text
Memory
Journal
Snapshot history
```

------

### 可重建 Derived State（派生状态）

```text
Observability Checkpoint
Chroma
```

------

### Source Data（源数据）

```text
KB source
```

------

### Deployment Artifact（部署制品）

```text
GGUF
Embedding model
```

这个分类直接决定：

```text
谁必须 backup
谁可以 rebuild
谁属于 deployment package
```

------

## 8.3 Process Role

```text
SERVER_ROLE
→ automatic read-only preflight

SCRIPT_ROLE
→ explicit migration / rebuild

CLIENT_ROLE
→ no persistence access
```

Desktop Client 不打开 SQLite，不执行 Migration。

------

# 9. 兼容策略

## 9.1 Memory

```text
new DB
→ initialize v1

current v1
→ CURRENT

current shape but user_version=0
→ explicit metadata adoption

known legacy
→ explicit migration

future / ambiguous / malformed
→ UNSUPPORTED
```

------

## 9.2 Journal

```text
record reader:
v1 + v2

writer:
v2

physical current
→ CURRENT

known spanless legacy
→ explicit migration

unknown physical schema
→ UNSUPPORTED

historical rows
→ NEVER REWRITE
```

------

## 9.3 Snapshot

```text
v1
→ supported

unknown
→ fail closed

migration
→ none
```

------

## 9.4 Checkpoint

```text
current
→ CURRENT

incompatible
→ explicit RECREATE
```

------

## 9.5 Chroma

```text
empty unmarked
→ marker initialization allowed

non-empty unmarked
→ REBUILD_REQUIRED

marker mismatch
→ REBUILD_REQUIRED

startup
→ NEVER auto clear/rebuild
```

------

# 10. Bad Cases

这一节是 WP1-D 最值得面试重点讲的部分。

------

## Bad Case 1：Physical Signature 只检查列名

### 真实性

**Codex Final Gate 真实审查发现的 P1。**

不是线上生产事故。

### 触发

构造 malformed Memory：

```text
expected column names all present
```

但：

```text
PK wrong
NOT NULL wrong
DEFAULT wrong
index columns wrong
trigger = no-op
```

旧 detector 仍：

```text
CURRENT
```

Journal：

```text
所有列名都存在
但没有 PK / UNIQUE
```

仍 CURRENT。

Snapshot：

```text
列名存在
approved index 名也存在
但 index 实际建错列
```

仍 CURRENT。

### 根因

把：

```text
schema shape
```

错误简化成：

```text
column/object name set
```

### 修复

使用：

```text
PRAGMA table_info
PRAGMA index_list
PRAGMA index_info
sqlite_schema.sql
```

验证：

```text
type
NOT NULL
DEFAULT
PK
UNIQUE
index columns
partial predicate
FTS
trigger
```

------

## Bad Case 2：Required facts 都存在，但额外 UNIQUE 仍会改变语义

### 真实性

**第一次 Signature Remediation 后，Codex Re-Gate 真实发现的第二轮 P1。**

第一轮修复已经能拒绝：

```text
missing PK
wrong index
no-op trigger
```

但是：

```text
required facts all correct
+
extra UNIQUE(trace_id)
```

仍然被认为：

```text
CURRENT
```

Journal 示例：

```text
UNIQUE(trace_id)
```

会阻止本来合法的两条相同 trace_id Journal row。

Snapshot：

```text
UNIQUE(run_id)
```

会阻止同一个 Run 保存多个 Snapshot。

Memory：

```text
extra UNIQUE(messages.content)
```

同样改变写入语义。

### 根因

实现验证的是：

```text
required constraints ⊆ actual constraints
```

但真正的 semantic contract 要求的是：

```text
supported semantic constraints
==
actual semantic constraints
```

至少对会改变写入合法性的 PK / UNIQUE 来说如此。

### 修复

建立 semantic UNIQUE set：

```text
required UNIQUE exists
+
unsupported UNIQUE absent
```

------

## Bad Case 3：Memory 漏检 `run_id UNIQUE`

### 真实性

**Codex Re-Gate 真实发现。**

Memory 的：

```text
message_exchanges.run_id UNIQUE
```

对 exchange/run idempotency 很关键。

但第一次修复没有验证这个 SQLite autoindex constraint。

结果：

```text
run_id column exists
but not UNIQUE
→ CURRENT
```

甚至真实 Server process 能进入：

```text
READY
```

### 修复

不能依赖：

```text
sqlite_autoindex_message_exchanges_...
```

这种内部名字。

而应该：

```text
PRAGMA index_list
origin='u'
+
PRAGMA index_info
columns=('run_id',)
```

验证 semantic UNIQUE。

最终真实 CLI 和真实 Server malformed smoke 均被拒绝。

------

## Bad Case 4：SQL canonicalizer 把字符串字面量一起 lower-case

### 真实性

**Codex Re-Gate 真实发现。**

第一轮 Memory Trigger 检查使用：

```text
canonical SQL comparison
```

但实现对整个 SQL：

```python
.lower()
```

于是：

```text
'delete'
```

和：

```text
'DELETE'
```

被错误视为一样。

但字符串 literal（字面量）内容可能属于真正执行语义。

### 根因

混淆：

```text
SQL keyword case-insensitive
```

和：

```text
SQL string literal content
```

### 修复

改成：

```text
quote-aware canonicalizer
```

只有单引号字面量之外进行：

```text
keyword case normalization
whitespace normalization
identifier normalization
```

字面量内部保留原字符。

最终：

```text
'delete' != 'DELETE'
```

同时仍能接受：

```text
keyword case
whitespace
identifier quoting
```

等无害格式差异。

------

## Bad Case 5：测试 1751 全绿，Gate 仍失败

### 真实性

**真实 Final Re-Gate 结果。**

第一次 signature remediation 后：

```text
1751 passed
```

但是 Codex 通过 direct counterexample（直接反例）又证明：

```text
missing run_id UNIQUE
extra UNIQUE
literal canonicalization
```

仍 fail open。

### 工程知识点

> 自动测试只能证明“测试覆盖到的合同成立”，不能证明“你忘记定义的合同也成立”。

Final Gate 不是简单重跑 pytest，而是：

```text
Contract
→ adversarial counterexample
→ verify implementation
```

这在面试中非常值得讲。

------

# 11. 测试与验收

最终 Final Semantic Re-Gate 实际执行：

## Semantic Signature Targeted

```text
67 passed
0 failed
0 skipped
```

## Startup

```text
37 passed
```

## WP1-D Targeted

```text
187 passed
```

## WP1-C Regression

```text
88 passed
1 existing warning
```

## Critical Runtime

```text
45 passed
```

## Full Regression

```text
1760 collected
1760 passed
0 failed
0 skipped
42 subtests passed
4 warnings
```

## Static

```text
compileall PASS
uv lock --check PASS
git diff --check PASS
pyproject.toml / uv.lock diff EMPTY
```

------

## 真实 Process Smoke

### Malformed Memory

真实 isolated Server 使用：

```text
Memory missing run_id UNIQUE
```

结果：

```text
Persistence preflight blocked startup
Application startup failed
```

没有：

```text
Application startup complete
```

PASS。

### Fresh Store

真实隔离新 Store：

```text
Application startup complete
/health = 200
/readyz = 200
READY / ACCEPTING
```

说明 strict detector 没有把合法数据库误杀。

------

# 12. Known Limitations

WP1-D PASS 后仍然没有：

## 1. Online Backup

```text
NOT_IMPLEMENTED
```

运行中的 DB 不支持直接 raw copy。

------

## 2. Automatic Backup

没有：

```text
scheduled backup
automatic backup
cloud backup
```

------

## 3. Automatic Restore

Restore 是 Operator Runbook。

------

## 4. Downgrade Migration

```text
NOT_IMPLEMENTED
```

------

## 5. Cross-Store Transaction

Memory / Journal / Checkpoint：

```text
each store own transaction
```

没有：

```text
2PC
distributed transaction
```

------

## 6. Multi-process Migration Lock

当前 single Server process contract。

------

## 7. Zero-Downtime Migration

没有：

```text
rolling migration
blue-green DB migration
```

------

## 8. Chroma Internal Migration

```text
NOT_LOCAL_SCHEMA_OWNER
```

------

## 9. Chroma Rebuild 是 destructive operation

需要 Operator 显式执行。

------

## 10. Runtime Recovery 仍然 validation-only

没有：

```text
automatic replay
resume
automatic restore
```

------

## 11. Windows-only

仍是当前 certified target。

------

## 12. 无 Docker / Windows Service

仍属于其他 Scope。

------

## 13. Continuous Readiness Monitoring

WP1-C 仍然：

```text
startup-only
```

------

## 14. Client/Server Version Fingerprint

仍：

```text
DEFER_TO_WP4
```

------

## 15. Planning Executor Starvation

仍是：

```text
accepted P2
```

------

# 13. 这次修改体现的工程能力

## 13.1 Schema migration 不只是 ALTER TABLE

真正的流程是：

```text
detect
classify
backup
migrate
validate
start
rollback strategy
```

------

## 13.2 Fail Closed

如果不知道数据库到底是不是支持的版本：

```text
UNSUPPORTED
```

而不是：

```text
try it and see
```

------

## 13.3 Schema Compatibility 是语义问题

关键不是：

```text
column names match
```

而是：

```text
does this physical schema preserve
the application invariants?
```

例如：

```text
UNIQUE(trace_id)
```

虽然“只是多了个约束”，但会改变合法数据集合。

------

## 13.4 Record Version 和 DB Version 分层

WP1-D 很重要的一点：

```text
JournalRecord schema version
!=
Journal SQLite physical schema version
```

这个概念在很多工程系统里都非常常见。

------

## 13.5 Migration Authority 和 Data Owner 分离

Coordinator：

```text
orchestration owner
```

Store：

```text
schema truth owner
```

避免中央 migration module 复制全部 SQL，形成第二事实源。

------

## 13.6 Backup 是 rollback 的前置条件

不能：

```text
migration committed
→ rollback exe
```

就认为完成回滚。

正确：

```text
code rollback
+
data compatibility
```

必须一起判断。

------

## 13.7 Rebuildable 不等于 Optional

Checkpoint：

```text
rebuildable
```

但当前：

```text
startup-required
```

这是一个很典型的二维分类思维。

------

## 13.8 Test Gate 不只是跑 Test Suite

这次最重要的能力之一：

```text
1760 tests green
```

之前仍出现过：

```text
1751 tests green
but Final Gate FAIL
```

因为 Codex 使用：

```text
semantic counterexample
```

检查测试未覆盖的合同。

------

# 14. 30 秒面试表达

我在 LocalAgent 最小生产化里做过持久化迁移和运维闭环。项目有 Memory、Journal、Snapshot、Observability Checkpoint 和 Chroma 五类持久化 Store，但以前只有零散的 `ALTER TABLE` compatibility-on-open，没有统一 Migration Contract。我最后设计成 Server 启动只做 read-only preflight，发现旧 schema 就 fail closed，由 Operator 停服、备份后显式执行 migration。Memory 用 SQLite `PRAGMA user_version=1`，Journal 只允许已知 span-column physical migration 且绝不 rewrite 历史事件，Snapshot 不迁移，Checkpoint 不兼容就显式重建，Chroma 只管理 LocalAgent collection marker，不碰第三方 internal DB。Final Gate 还真实抓出过 schema detector 只检查列名、漏检 UNIQUE 约束等问题，最后做到 semantic exact signature，再以 1760 个测试和真实 malformed/fresh Server smoke 验收通过。

------

# 15. 2 分钟面试表达

这个问题最开始看起来像“给 SQLite 加 Migration Runner”，但我后来把它拆成了四个独立问题：兼容性检测、迁移、备份恢复和回滚。

首先我没有允许 Server startup 自动改旧数据库。Server 启动时只做 read-only preflight，包括 SQLite `quick_check`、schema/version/physical signature 检查。如果是 `CURRENT/NEW` 才继续；如果是 `MIGRATION_REQUIRED/UNSUPPORTED/FAILED`，就不能进入 READY。真正修改已有 DB 必须用独立 SCRIPT_ROLE 命令，而且需要 Operator 明确确认已经停服备份。

不同 Store 的策略也不一样。Memory 不可重建而且以前没有 DB version，所以我正式引入 `PRAGMA user_version=1`。Journal 已有 record v1/v2，而且历史事件 digest 和 append-only 语义不能动，所以只做已知 span columns 的 physical migration，绝不 rewrite historical rows。Snapshot 继续 v1 validation-only，不做 migration。Observability Checkpoint 是派生状态，不兼容时显式 recreate。Chroma 的内部 schema 不是我们拥有的，所以只做 LocalAgent collection marker 和 embedding compatibility 检测，不直接修改 Chroma SQLite。

这个 WP 最有价值的部分其实是 Final Gate。第一版 1738 个测试全绿，但 Codex 构造 malformed SQLite 后发现只要列名一样，即使 PK、UNIQUE、index、trigger 都错了还是会判 CURRENT。第一次修完以后 1751 tests 又全绿，但又发现额外 `UNIQUE(trace_id)` 这种 constraint 会改变合法写入集合，同时 Memory 还漏检 `run_id UNIQUE`，SQL canonicalizer 甚至把 `'delete'` 和 `'DELETE'` 当成一样。最后我们把兼容判定收敛成 semantic physical signature：系统真正依赖的 PK、UNIQUE、索引、FTS、Trigger 必须正确，同时无害 non-unique performance index 和 SQL 格式差异仍允许。最终 1760 tests 全绿，并且 malformed Server 真实启动失败、fresh Server 正常 READY。

------

# 16. 深入版本

面试官如果深挖，可以从五层回答。

## 第一层：为什么 Preflight 和 Migration 要分开

因为：

```text
detect
```

和：

```text
mutate
```

是两个风险等级完全不同的操作。

如果 startup 自动 migration：

```text
binary starts
→ data mutates
→ binary later fails
```

此时 code rollback 的成本会立刻变高。

所以：

```text
startup = detect only
operator = mutation
```

------

## 第二层：为什么 Schema Compatibility 是 Semantic Contract

例如数据库多了：

```sql
UNIQUE(trace_id)
```

表面看：

```text
所有原来的列还在
```

但应用语义已经发生变化：

以前允许：

```text
trace_id = X
trace_id = X
```

现在第二条会失败。

所以 compatible 不是：

```text
expected columns ⊆ actual columns
```

而是：

```text
application invariants preserved
```

------

## 第三层：为什么不是所有 extra index 都拒绝

一个普通：

```sql
CREATE INDEX idx_x ON table(col)
```

只影响查询性能，不改变合法写入集合。

但：

```sql
CREATE UNIQUE INDEX ...
```

会改变合法数据集合。

因此 Final Gate 最终冻结的是：

```text
semantic exactness
```

不是：

```text
DDL byte exactness
```

这避免 detector 过拟合。

------

## 第四层：为什么 Journal 不能 rewrite

Journal 保存：

```text
event identity
sequence
payload digest
event digest
terminal evidence
```

而 Stage 2 已冻结：

```text
Journal-first
append-only safe facts
```

如果 Migration 为了“升级版本”去 UPDATE historical rows：

就可能破坏：

```text
digest
historical truth
Recovery validation evidence
```

所以：

```text
physical table migration
```

和：

```text
record data migration
```

严格分离。

------

## 第五层：Backup / Rollback 为什么是一对

采用：

```text
forward-only migration
```

意味着：

```text
new code can migrate old data forward
```

但不承诺：

```text
old code can read new data
```

因此一旦 migration commit：

```text
binary-only rollback
```

就不安全。

真正 rollback：

```text
old code
+
matching pre-migration data
+
matching config/artifacts
```

------

# 17. 高频追问与参考答案

## Q1：为什么不用 Alembic？

项目当前是直接使用 SQLite 的 Store Owner，不是 SQLAlchemy ORM 体系；Migration Scope 也很有限，目前真正需要正式 versioned migration 的核心只有 Memory，再加 Journal 一个已知 physical compatibility 和 Checkpoint recreate。

引入 Alembic 会增加依赖、migration metadata、CLI 和 ownership 复杂度，但并不能自动解决 Chroma、Journal historical digest、backup/rollback 等更关键的问题。

所以 Stage 3 第一版选择了 Minimal Coordinator，而不是通用 Migration Framework。

------

## Q2：`PRAGMA user_version` 是什么？

【通用知识补充】

SQLite 文件头里提供的一个应用自定义整数。

项目中：

```text
Memory user_version = 1
```

用来表达：

```text
Memory SQLite physical schema version
```

它不等于：

```text
Memory data version
Runtime contract version
```

------

## Q3：为什么 Journal 不也用 `user_version`？

因为当前 Journal 已有两个维度：

```text
record v1/v2
physical table shape
```

而 physical table 已知变化只有：

```text
span_id
parent_span_id
```

第一版用 exact physical signature 就足够。

如果未来 Journal 再发生新的 physical schema evolution，现有 Architecture Decision 要求重新评估 DB-level version，而不是继续堆隐式 ALTER。

------

## Q4：为什么 Snapshot 不迁移？

它目前只有 v1，Unknown version 可以严格 fail closed，而且 Runtime Recovery 仍只是 validation-only。

没有真实旧 Snapshot schema 需要升级，因此现在做 Migration 只是在设计未来。

------

## Q5：为什么 Checkpoint 可以直接删掉重建？

它保存的是 Observability consumer offset，不是 Agent 业务事实。

丢失会造成：

```text
diagnostic duplicate/gap risk
```

但不会改：

```text
AgentState
Journal
Memory
final output
```

因此 schema 不兼容时，recreate 比逐行 migration 更符合成本收益。

------

## Q6：Chroma 为什么不迁 SQLite？

因为 Chroma internal schema 属于第三方 dependency。

LocalAgent 不应：

```text
UPDATE chroma.sqlite3
```

我们的 Authority 只到：

```text
collection contract
chunk schema
embedding compatibility
```

所以采取 marker + rebuild。

------

## Q7：Chroma marker 有什么？

当前至少：

```text
localagent_collection_contract_version = 1
chunk_schema_version = kb_chunk_schema_v2
embedding_compatibility_digest
embedding_dimension
```

其中 digest 是：

```text
configured compatibility descriptor digest
```

不是模型文件内容 hash，也不是供应链 attestation。

------

## Q8：为什么非空旧 Chroma 没 marker 时不能直接补 marker？

因为：

```text
marker absent
```

并不能证明：

```text
old vectors were built with current embedding/chunk contract
```

如果直接 adopt：

就可能静默接受语义不匹配的向量。

所以：

```text
non-empty + unmarked
→ REBUILD_REQUIRED
```

------

## Q9：为什么 Backup 必须停 Server？

当前 SQLite 使用 WAL（预写日志）等模式，运行中只复制 `.db` 可能拿不到一致状态。

目前没有 SQLite online backup primitive。

因此第一版 Contract：

```text
graceful shutdown
confirm process exited
copy offline set
```

------

## Q10：为什么 `--backup-confirmed` 不代表备份真的存在？

它只是：

```text
Operator acknowledgement
```

不是：

```text
proof of backup
```

系统没有自动 Backup Manager。

真正备份是否可用，需要：

```text
backup copy
→ FULL preflight
```

------

## Q11：Backup 主要备份哪些？

Correctness（正确性）必备：

```text
Memory
Journal
Snapshot if enabled
KB source
known-good configuration reference
```

Chroma：

```text
optional
```

可以从：

```text
KB source + matching embedding artifact
```

重建。

Checkpoint：

```text
recreate
```

------

## Q12：为什么 Journal 和 Snapshot 要同一个 backup epoch？

Snapshot 会包含：

```text
run
journal sequence/watermark
```

等与 Journal 相关的恢复证据。

如果：

```text
Snapshot = 时间 T1
Journal = 时间 T2
```

就不能简单宣称两者是一致 recovery evidence。

------

## Q13：为什么 Migration 后不能直接回滚旧 exe？

因为当前 contract 是：

```text
forward-only migration
```

不承诺：

```text
old binary reads migrated schema
```

所以：

```text
migration commit
→ old binary compatibility NOT ASSUMED
```

需要恢复 pre-migration data backup。

------

## Q14：为什么不做 downgrade SQL？

复杂度和风险不匹配当前单机 LocalAgent。

需要维护：

```text
up migration
down migration
data loss semantics
version graph
rollback validation
```

Stage 3 选择更窄、更可靠：

```text
restore backup
```

------

## Q15：Physical signature 为什么不能只比较 column name？

因为数据库行为还受：

```text
PK
UNIQUE
NOT NULL
DEFAULT
index
partial predicate
trigger
FTS
```

影响。

同样列名完全可能有完全不同的写入约束。

------

## Q16：那为什么不直接比较 CREATE TABLE SQL？

因为：

```text
keyword case
whitespace
identifier quoting
```

这些无害格式变化不应该导致 unsupported。

最终目标是：

```text
semantic exact
```

而不是：

```text
text exact
```

------

## Q17：为什么额外 UNIQUE 要拒绝，但额外普通 INDEX 可以接受？

UNIQUE 会改变：

```text
valid data set
```

普通 non-unique index 通常只改变：

```text
query performance
```

Final Gate 最终专门验证：

```text
extra harmless non-unique index
→ CURRENT
```

避免 detector 过度收紧。

------

## Q18：SQL canonicalizer 为什么要 quote-aware？

因为：

```sql
DELETE
```

作为 SQL keyword 不区分大小写。

但：

```sql
'delete'
```

是 string literal。

其内容必须保留。

所以只能在 literal 外做 case normalization。

------

## Q19：为什么 Final Gate 测试都绿还会 FAIL？

因为 Gate 不是“测试执行器”。

它还负责验证：

```text
测试是否覆盖了真正的 Architecture Contract
```

第一次：

```text
1738 passed
```

仍发现 physical signature fail-open。

第二次：

```text
1751 passed
```

又发现 semantic UNIQUE 和 canonicalizer fail-open。

所以：

> Test suite green 是必要条件，不是充分条件。

------

## Q20：Migration 和 Runtime Recovery 有什么区别？

Migration：

```text
deployment upgrade
schema compatibility
persistent store evolution
```

Recovery：

```text
a previous Run's snapshot/journal evidence
```

当前 Runtime Recovery 只是：

```text
validation-only
```

Migration 不能把它升级成 automatic resume/replay。

------

# 18. 容易答错或夸大的问题

## 错误 1：我们实现了自动数据库迁移

不准确。

正确：

```text
automatic preflight
explicit operator migration
```

------

## 错误 2：我们实现了自动备份

错误。

```text
manual stopped-server backup
```

------

## 错误 3：`--backup-confirmed` 会验证 Backup

错误。

只是 Operator acknowledgement。

------

## 错误 4：实现了数据库自动恢复

错误。

Deployment Restore 是人工 Runbook。

Runtime Recovery 又是另一个 validation-only contract。

------

## 错误 5：Chroma 内部数据库也由我们迁移

错误。

```text
NOT_LOCAL_SCHEMA_OWNER
```

------

## 错误 6：Snapshot 支持 schema migration

错误。

只有：

```text
v1 validation
unknown fail closed
```

------

## 错误 7：Journal migration 会把 v1 row 升级成 v2

错误。

历史 v1：

```text
still v1
still v1 digest semantics
```

只补 physical nullable columns。

------

## 错误 8：系统实现 Exactly-once Migration

错误。

应说：

```text
transactional
idempotent / safely re-runnable
```

------

## 错误 9：1760 tests 说明所有 SQLite schema 都兼容

错误。

它只证明已冻结/已覆盖的 compatibility contract。

------

## 错误 10：WP1-D PASS 就表示 Disaster Recovery 完成

错误。

仍没有：

```text
automatic backup
automatic restore
cloud backup
HA
downgrade
```

------

# 19. 重点复习知识点

## P0：必须熟练

### 1. Preflight vs Migration

```text
Preflight = detect
Migration = mutate
```

为什么分开？

------

### 2. Fail Closed

```text
unknown
→ reject
```

不是：

```text
best effort open
```

------

### 3. Memory `PRAGMA user_version`

必须能讲：

```text
v1
legacy
current-unversioned adoption
future unsupported
```

------

### 4. Record Version vs DB Version

必须会解释 Journal 为什么：

```text
journal_schema_version
!=
physical DB schema version
```

------

### 5. Semantic Schema Compatibility

最核心：

```text
columns match
!=
schema compatible
```

必须能举：

```text
UNIQUE(trace_id)
```

例子。

------

### 6. Journal Historical Immutability

必须理解为什么 Migration 绝不能 UPDATE 历史 row。

------

### 7. Backup / Rollback

必须会：

```text
migration committed
→ binary-only rollback unsafe
```

------

### 8. Chroma Ownership

必须会：

```text
internal schema
vs
LocalAgent collection contract
```

------

## P1：高概率深入追问

### 9. SQLite introspection

```text
PRAGMA table_info
PRAGMA index_list
PRAGMA index_info
sqlite_schema.sql
```

分别解决什么。

------

### 10. UNIQUE constraint

为什么：

```text
origin='u'
origin='c' unique
```

都需要考虑。

------

### 11. Autoindex

为什么不能硬编码：

```text
sqlite_autoindex_*
```

------

### 12. FTS / Trigger

为什么只检查名字不够。

------

### 13. Quote-aware canonicalization

为什么：

```text
keyword case
```

可以 normalize，但 literal 不能。

------

### 14. Single-store Transaction

```text
BEGIN IMMEDIATE
revalidate
mutate
validate
version
COMMIT
```

------

### 15. Cross-store Partial Commit

为什么没有全局 rollback，下一次如何 rerun。

------

### 16. Rebuildable vs Startup-required

Checkpoint 是典型。

------

## P2：扩展知识

### 17. Forward / Backward Compatibility

【通用知识补充】

Forward migration：

```text
new version can move old data forward
```

Backward compatibility：

```text
old binary can read new data
```

两者完全不是一回事。

------

### 18. Expand / Contract Migration

【通用知识补充】

大型在线系统常采用：

```text
expand schema
→ deploy compatible code
→ backfill
→ switch reads
→ contract old schema
```

当前 LocalAgent 没有做这个，因为没有 zero-downtime / multi-version deployment requirement。

面试中可以作为对比，但不能说项目已经实现。

------

### 19. SQLite Online Backup API

【通用知识补充】

SQLite 本身存在 Backup API，但当前项目：

```text
NOT_IMPLEMENTED
```

所以不要因为 SQLite 支持就说 LocalAgent 已支持 online backup。

------

### 20. Migration Observability

【通用知识补充】

未来 production migration 通常还会有：

```text
migration duration
from/to version
success/failure
operator identity
audit record
```

但当前 WP1-D 只冻结 safe low-cardinality result，并没有实现完整 migration audit platform。

------

# 20. 最终面试速查表

| 维度                   | 核心答案                                                     |
| ---------------------- | ------------------------------------------------------------ |
| 工作包                 | Migration / Operations Closure                               |
| 核心问题               | 代码升级后持久数据能否安全读取/迁移/恢复/回滚                |
| Logical Stores         | 5：Memory / Journal / Snapshot / Checkpoint / Chroma         |
| Coordinator            | Minimal Persistence Migration Coordinator                    |
| Server startup         | automatic read-only Preflight                                |
| Migration              | explicit SCRIPT_ROLE                                         |
| Backup                 | manual stopped-server                                        |
| Restore                | manual + FULL preflight                                      |
| Rollback               | forward-only；migration 后旧 binary compatibility 不假设     |
| Downgrade              | NOT_IMPLEMENTED                                              |
| Memory                 | `PRAGMA user_version=1`                                      |
| Memory legacy          | explicit additive migration                                  |
| Journal                | physical span-column migration；历史 row 不 rewrite          |
| Journal records        | reader v1/v2、writer v2                                      |
| Snapshot               | v1 validation-only；no migration                             |
| Checkpoint             | incompatible → explicit recreate                             |
| Chroma                 | internal schema NOT_LOCAL_OWNER                              |
| Chroma compatibility   | marker + embedding digest + dimension                        |
| Chroma mismatch        | REBUILD_REQUIRED                                             |
| Startup auto rebuild   | NO                                                           |
| Preflight SQLite       | read-only + `quick_check` + semantic signature               |
| Safe codes             | `PERSISTENCE_SCHEMA_UNSUPPORTED` / `PERSISTENCE_PREFLIGHT_FAILED` / `PERSISTENCE_MIGRATION_FAILED` |
| 最关键 P1 #1           | 列名匹配但 PK/UNIQUE/index/trigger 错误仍 CURRENT            |
| 最关键 P1 #2           | required facts存在，但额外 UNIQUE 改写入语义仍 CURRENT       |
| 最关键 P1 #3           | `run_id UNIQUE` 漏检                                         |
| Canonicalizer Bad Case | `'delete'` 与 `'DELETE'` 被全局 lower 混为一谈               |
| 最终 semantic rule     | semantic exact，不是 SQL byte exact                          |
| extra non-unique index | 可以兼容，不因无害性能索引 fail closed                       |
| Final targeted         | 187 passed                                                   |
| Final full             | 1760 passed / 0 skipped / 42 subtests                        |
| Final Gate             | P0=0 / P1=0 / TEST_GAP=0 / PASS                              |
| 保留 P2                | Planning executor starvation                                 |
| 不能夸大               | 自动 backup/restore/downgrade/online migration/DR 均未实现   |

## 一句话记忆

> **WP1-D 真正做的不是“给 SQLite 加几个 ALTER TABLE”，而是把“一个旧数据库什么时候可以被新版本安全使用”变成了一个显式、可验证、fail-closed 的生产合同。**