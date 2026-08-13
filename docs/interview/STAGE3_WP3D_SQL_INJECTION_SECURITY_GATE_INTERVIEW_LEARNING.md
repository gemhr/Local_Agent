# 1. 一句话项目 / 工作包定义

WP3-D 的目标不是给 LocalAgent 增加一个 SQL Firewall（SQL 防火墙），而是为当前生产 SQLite inventory（SQLite 使用清单）建立一条**可验证的 SQL Injection（SQL 注入）安全边界**：

> 所有来自 User、HTTP、Model、RAG、Tool、Memory 的不可信内容，只能作为数据库参数值进入 SQL，不能获得 SQL 结构、语句、标识符或执行所有权。

最终正式状态为：

```text
WP3-D Final Re-Gate = PASS
WP3-D Complete = YES

P0 = 0
P1 = 0
P2 = 0

CAPABILITY_GAP = 0
TEST_GAP = 0
DOC_DRIFT = 0
ENVIRONMENT_BLOCKED = 0
```

正式能力可以表述为：

```text
SQL Injection protection = SUPPORTED
scope = current LocalAgent production SQLite inventory
```

但这个 `SUPPORTED` 只覆盖**当前 LocalAgent 的 SQLite 使用面**，不是通用数据库安全能力。

------

# 2. 为什么做

这项工作的起点并不是已经发生了生产 SQL 注入事故。

Scout（侦察审计）和后续 Codex Gate（门禁）一直确认：

```text
Current production SQL injection defect = NO
Unsafe interpolation in current production = 0
Raw SQL Tool = NONE
NL2SQL = NO
Model-generated SQL executable = NO
```

真正的问题是：**目前代码看起来安全，不等于未来代码修改后仍然安全。**

如果只人工检查当前 `.execute()` 代码，即使现在全部参数化，也无法阻止后续开发者增加：

```python
connection.execute(f"SELECT ... {user_input}")
```

或者新建一个 SQLite owner（执行所有者），绕过现有审计。

所以 WP3-D 最终解决的是两个层面：

第一层是 **current-source safety（当前源码安全）**：确认现在没有 SQL 注入路径。

第二层是 **durable regression safety（耐久回归安全）**：让未来出现新的 SQL owner、动态 SQL、未知 receiver（接收器）等情况时，测试 Gate 能 fail-closed（失败关闭），而不是静默漏过去。

------

# 3. 真实性与完成边界

这一部分面试时必须严格区分。

**源码审查真实发现**：项目当前直接 SQL 技术只有 Python 标准库 `sqlite3`，当前五个直接 SQLite owner，当前生产 SQL 没发现用户输入参与 SQL structure（结构），当前没有 Raw SQL Tool（原始 SQL 工具）和 NL2SQL（自然语言转 SQL）能力。

**实施 / 测试真实发现**：Codex 连续几轮独立 Gate 真实构造出了 AST（抽象语法树）Guard 自身的 false negative（假阴性），包括 wrapper provenance、PRAGMA exception、`_select_one` alias、unknown receiver、`.format()`、ambiguous receiver 和 token boundary 等。这些是真正通过直接 synthetic probe（合成探针）发现的 Guard 缺陷。

**不是生产事故**：这些 bypass（绕过）没有在当前 production path（生产路径）中发现真实利用链，最终报告持续确认 `Current production SQL injection defect = NO`。

**假设构造 Bad Case（坏案例）**：例如新 module 通过未知 factory 获得 connection 后执行动态 SQL，是为了验证未来代码变化能否被 Guard 拦住，并不是用户真实遇到过这种生产调用。

**未实现范围**：通用 SQL parser、SQL firewall、NL2SQL validator、SQL Tool、所有数据库技术保护、通用 ORM 安全、whole-program Python analysis（全程序 Python 分析）均没有实现。

------

# 4. 修改前架构与根因

当前生产 SQL 本身已经相当规范，所以 WP3-D 最大的问题反而不是 Runtime，而是**如何证明这个规范不会悄悄退化**。

初始架构可以抽象为：

```text
HTTP / User / Model / Memory
            ↓
      application logic
            ↓
        sqlite3.execute
```

当前源码里，外部输入基本都通过 DB-API parameter binding（参数绑定）：

```text
SQL structure → code-owned
SQL values    → bound parameters
```

问题在于没有一个 durable static oracle（耐久静态判定器）保证这个边界。

于是最初的 AST Guard 思路是：

```text
找到 SQLite owner
        ↓
找到 .execute()
        ↓
检查 SQL statement
```

这个方向本身没错，但后续 Gate 证明：如果 **owner discovery、receiver provenance、statement classifier、exception inventory** 任意一层有漏检，整个静态安全结论都会出现 false negative。

所以根因最终不是“一句 SQL 拼接写错”，而是：

> **安全 Guard 自身必须被当作安全关键代码审计，而不能因为 Guard tests 全绿就假定 Guard 正确。**

------

# 5. 方案讨论与取舍

一个比较容易想到的方案是直接引入 ORM 或 SQL parser，但 WP3-D 没有这么做。

原因是当前 LocalAgent 实际只使用有限 SQLite inventory，而且当前 SQL 都已经是手写、结构固定、参数绑定。如果为了 SQL Injection Gate 全面改 ORM，会造成明显 scope expansion（范围扩张），同时把 Stage 3 最小必要生产化变成数据库架构重构。

另一个方案是用正则或简单 grep 搜：

```text
f"..."
.format(...)
+
%
```

这个方案也被放弃，因为它既无法判断 receiver 到底是不是 SQLite，也无法区分：

```python
agent.execute(task)
```

和：

```python
connection.execute(sql)
```

最终采用的是 **test-resident AST static guard（位于测试侧的 AST 静态守卫）**：

```text
Candidate enumeration
        ↓
Receiver / Owner classification
        ↓
Statement classification
        ↓
Audited exception validation
        ↓
Fail-closed decision
```

它没有进入 production runtime，所以不会增加运行时开销；同时又可以作为 CI / Release Gate（发布门禁）的一部分长期运行。

------

# 6. 最终架构

最终 Guard 的核心可以理解成四层。

第一层：

```text
execute
executemany
executescript
```

在整个 production Python scope 内先枚举候选，而不是只扫描已知 SQLite 文件。

第二层进行 receiver classification（接收器分类）：

```text
KNOWN_SQLITE
KNOWN_BUSINESS
UNKNOWN_DB_SHAPED
AMBIGUOUS_SQL_RECEIVER
NON_SQL
```

只有**正向证明为 audited business receiver（已审计业务接收器）**的 `.execute()` 才能直接跳过 SQL 审计。

第三层统一进入 statement classifier（语句分类器）：

```text
literal
parameterized
module constant
approved structural pattern

f-string
string +
%
.format()
unresolved statement
raw join
executescript
...
```

第四层再结合 owner 决策：

```text
安全 SQL
+
未知新 SQL owner
=
仍然 FAIL
```

所以：

```python
unknown_connection.execute(
    "SELECT * FROM x WHERE id = ?",
    (value,),
)
```

SQL statement 本身没有注入，但**新的 SQL execution ownership 没审计**，仍必须 Fail Gate。

这是 WP3-D 中很重要的设计思想：

> **SQL statement safety 和 SQL execution authority 是两件不同的事。**

------

# 7. 核心状态机和时序

WP3-D 不是 Runtime 状态机，而更像一个静态安全判定流水线：

```text
Python source
   ↓
enumerate execute-like call
   ↓
positive audited business?
   ├─ YES → NON_SQL / PASS
   └─ NO
        ↓
known SQLite / DB factory provenance?
        ├─ YES
        │
        └─ NO
             ↓
        clear SQL intent?
             ↓
     SQL-capable receiver
             ↓
 common statement classifier
             ↓
 owner decision + statement decision
             ↓
 PASS / finding / inventory mismatch
```

最终原则：

```text
无法证明安全
≠
自动当作业务调用

无法证明安全
→
在明确 SQL 证据存在时 fail closed
```

但同时也不能变成：

```text
所有 .execute()
→ SQL
```

所以正向 business identity 是这个模型避免 false positive（假阳性）的关键。

------

# 8. 数据 / 权限 / Owner

最终确认的 SQLite owner 精确为 5 个：

```text
core/memory_manager.py
core/persistence_migration.py
core/runtime/event_consumer.py
core/runtime/event_journal_store.py
core/runtime/snapshot_store.py
```

Current production scan 最终确认：

```text
SQLite owners                  5
Business execute calls        13

Syntactic SQL sites          152
Shadowed/unreachable           4
Reachable/effective          148

Runtime reachable             57
Startup/admin-only            91
```

这里特别注意：

```text
152 syntax sites
!=
152 attack surfaces
```

因为其中 4 个属于 shadowed definition（被后续同名定义覆盖），实际 reachable/effective sink（可达有效 SQL 汇点）是 148。

------

# 9. 兼容策略

WP3-D 最重要的兼容策略就是 **production zero mutation（生产代码零修改）**。

最终整个 WP3-D：

```text
Production code mutation = NONE
Database schema change   = NONE
Configuration change     = NONE
Dependency change        = NONE
pyproject / uv.lock      = NONE
```

也就是说，没有为了让安全测试通过去改业务代码。

Static Guard 位于：

```
tests/test_stage3_wp3d_sql_injection_guard.py
```

因此对当前 Runtime、Memory、Journal、Snapshot、HTTP API 都没有运行时兼容风险。

已有 Parameter Binding、FTS、LIKE、PRAGMA 等行为也没有为了 Gate 被重新定义。

------

# 10. Bad Cases

WP3-D 最大的面试价值就在这里。

最开始 Guard tests 全绿，但 Codex Final Gate 构造：

```text
imported SQLite wrapper
但 module 没直接 import sqlite3
```

scanner 漏掉。

修完之后又发现 PRAGMA audited exception 只看“函数里有没有 `except sqlite3.Error`”，结果：

```python
except sqlite3.Error:
    return True
```

这种 fail-open（失败开放）逻辑也可能被错误接受。

再修之后发现：

```python
selector = self._select_one
selector(statement, ())
```

alias 可以绕过 caller inventory。

后续又依次出现：

```text
unknown wrapper
local alias
qualified alias
unknown factory
.format()
ambiguous receiver
SELECT\t...
```

最有价值的不是单独记住这些 bug，而是看到 Guard 架构如何逐步演进：

```text
扫描已知 owner
↓
全 production candidate enumeration

简单 owner
↓
receiver provenance

简单 SQL-shaped heuristic
↓
shared statement classifier

factory receiver
↓
ambiguous SQL-intent receiver

startswith("SELECT ")
↓
真正的 SQL token-boundary rule
```

最终 Final Re-Gate V 才关闭 `P1-01`。

------

# 11. Tests / Gate

最终真实测试证据：

```text
AST Guard                    91 passed
WP3-D combined              171 passed

Persistence                  65 passed
WP3-A                        42 passed
WP3-B                        90 passed
WP3-C                        15 passed
Runtime relevant            218 passed
Formal docs                  99 passed

Full collection            2192
Full regression            2192 passed
Subtests                      42 passed

failed                        0
skipped                       0
xfail                         0
```

其他 Gate：

```text
compileall          PASS
uv lock --check     PASS
git diff --check    PASS
packaging           EMPTY
scope               PASS
```

最终 Codex 独立验证还覆盖 SQLite whitespace：

```text
space       valid
tab         valid
CR          valid
LF          valid
form-feed   valid

vertical tab   invalid
NBSP           invalid
```

并验证 `SELECTOR`、`SELECTED`、`selection`、`RESELECT` 等不会误判成 `SELECT`。

------

# 12. Known Limitations

WP3-D 完成后仍然明确保留以下边界：

No generic SQL firewall/parser。

没有 NL2SQL feature / validator。

没有 Raw SQL Tool。

未来新增 SQLite owner、新数据库技术、SQL Tool 或 NL2SQL 都必须重新 Gate。

FTS5 `MATCH ?` 的 `OR` / `NOT` / `NEAR` 等属于：

```text
SEARCH_QUERY_SEMANTIC_LIMITATION
```

不是 SQL Injection。

LIKE `%` / `_` wildcard broadening 同样属于搜索语义限制，不是 SQL Injection。

PRAGMA schema metadata 只允许当前 narrow startup/internal/read-only（窄范围启动期 / 内部 / 只读）例外。

Chroma 内部 SQLite 不属于 LocalAgent direct SQL ownership。

HTTP DB error non-leak 只证明 user-visible response，没有证明 generic internal-log DLP（通用内部日志防泄漏）。

Comment-prefixed generic SQL：

```text
/* comment */ SELECT ...
```

没有被做成通用 SQL parser 能力。

------

# 13. 这项工作体现的工程能力

WP3-D 真正体现的不是“会写参数化 SQL”。

参数化 SQL 是基础。

更值得讲的是三个工程能力。

第一，**安全边界建模**：

```text
untrusted value
!=
SQL structure authority
```

同时进一步区分：

```text
SQL safe
!=
owner approved
```

第二，**安全测试的 adversarial review（对抗性审查）**。

全仓测试长期保持全绿，但 Codex 仍通过独立 probe 发现 Guard false negative。你没有把“测试绿”直接等价为“安全能力完成”。

第三，**控制 scope**。

面对连续安全 Gate Failure，没有把问题扩大成 ORM 重构、SQL parser、生产 Runtime 改造，而是始终保持：

```text
production mutation = NONE
```

把问题限制在静态安全 Guard 和测试合同里。

------

# 14. 30 秒面试回答

> 我在 LocalAgent Stage 3 里做过一轮 SQL Injection 安全门禁。当前项目生产代码本身没有发现 SQL 注入，我们主要解决的是如何防止未来回归。我的核心原则是外部输入只能作为 DB-API bound value，不能获得 SQL structure authority。我们基于 Python AST 做了一个 test-side 静态 Guard，扫描当前所有 SQLite execution owner、execute sink、动态 SQL 构造和受控例外。这个过程比较有意思的是，全仓测试一直是绿的，但独立 Final Gate 连续发现 Guard 自身的 false negative，比如跨模块 wrapper、alias、unknown receiver、`.format()` 和 SQL token boundary，最后把 Guard 收敛成 owner、receiver、statement 和 token-boundary 分层模型。最终 2192 个测试和 42 个 subtests 全部通过，P0/P1/P2 都是 0。

------

# 15. 2 分钟面试回答

> WP3-D 的目标不是给项目加一个通用 SQL 防火墙，因为 LocalAgent 目前只有有限的 SQLite 使用面，也没有 NL2SQL 和 Raw SQL Tool。我们首先做了生产 SQL inventory，确认当前有五个直接 SQLite owner，所有外部数据都通过 DB-API binding，没有发现生产 SQL Injection defect。
>
> 真正的问题是怎样防止未来改代码时把这个边界破坏。所以我们在测试侧基于 AST 建了一个 fail-closed Guard。最开始是找 SQLite owner 和 `.execute()`，后来独立 Codex Gate 发现这种方案会漏掉跨模块 wrapper，于是改成先枚举整个 production scope 的 execute-like candidate，再做 receiver provenance。
>
> 后面又发现 unknown receiver 的 `.format()`、ambiguous receiver 和 SQL keyword 的 tab token boundary 可以绕过 SQL-intent detection。所以我们进一步拆开了 receiver classification、SQL intent detection 和 statement safety classification，并让 known SQLite、unknown DB receiver 使用同一套 statement classifier。
>
> 最后 Guard 能区分真实业务的 `agent.execute()` 和 SQL execute，也能识别 f-string、字符串拼接、百分号、`.format()`、unresolved statement、executescript 等，同时对新 SQL owner 即使使用安全参数化 SQL也会要求重新审计。最终 Final Re-Gate PASS，P0/P1/P2 都是 0，全仓 2192 tests 和 42 subtests 通过。能力状态是 SQL Injection protection SUPPORTED，但严格限定当前 LocalAgent production SQLite inventory。

------

# 16. 深入版本

如果面试官继续问“为什么这么麻烦，参数化查询不就够了吗”，核心回答是：

参数化只能证明**已经找到的 SQL statement 中 value 与 structure 分离**。

它不能解决：

```text
新 SQLite owner 没被扫描
wrapper 跨模块后 provenance 丢失
unknown receiver 被当成业务 execute
异常例外错误放行
private SQL wrapper 被 alias
```

例如：

```python
with future_read_only(path) as conn:
    conn.execute(
        "SELECT * FROM x WHERE id = ?",
        (value,),
    )
```

它没有 SQL Injection。

但这个 module 是一个**新增 SQL execution owner**。

所以静态安全 Gate 应该报告：

```text
statement safety = PASS
owner governance = FAIL
```

而不是直接全绿。

这是 WP3-D 最值得讲的设计点之一。

------

# 17. 高频追问

1. **为什么不用 ORM？**
   当前 SQLite inventory 有限，现有 SQL 已经使用参数绑定；ORM 重构会扩大 Stage 3 scope，而且不能自动解决所有动态 SQL authority 问题。
2. **为什么 AST 而不是 regex？**
   AST 能区分表达式结构、调用 receiver、alias、`.format()`、f-string、`Try/Except` 等；regex 无法可靠表达这些语义。
3. **参数化是不是绝对安全？**
   对 value injection 是核心防御，但不能证明 identifier、ORDER BY token、PRAGMA identifier、SQL owner 等结构边界安全。
4. **为什么 unknown parameterized SQL 也 Gate FAIL？**
   因为失败原因是新 SQL owner 未审计，不是 statement injection。
5. **FTS 的 OR/NOT 算注入吗？**
   当前项目把它定义为搜索查询语言语义问题，因为 query 仍通过 `MATCH ?` 绑定，没有获得外层 SQL structure authority。
6. **为什么 Guard tests 全绿还连续失败？**
   因为独立 Final Gate 使用的是测试套件外的 adversarial probes，验证测试是否真的覆盖 frozen contract，而不是只验证实现者自己想到的场景。

------

# 18. 易夸大 / 易答错

不能说：

> “我们项目实现了通用 SQL 注入防御。”

应该说：

> “对当前 LocalAgent production SQLite inventory 建立了 SUPPORTED 级别的 SQL Injection Gate。”

不能说：

> “我们发现并修复了多个生产 SQL 注入漏洞。”

应该说：

> “当前生产代码始终没有发现 SQL Injection defect；连续发现的是 test-side AST security Guard 的 false negatives。”

不能说：

> “所有 SQL 都有 152 个攻击点。”

实际是：

```text
152 syntactic sites
148 reachable/effective sinks
4 shadowed/unreachable
```

不能说：

> “FTS OR/NOT 是 SQL Injection。”

它们当前被归类为 `SEARCH_QUERY_SEMANTIC_LIMITATION`。

不能说：

> “静态分析能识别未来所有 SQL。”

最终 Guard 明确是 **current-inventory-oriented AST oracle**，不是 generic whole-program analysis。

------

# 19. P0 / P1 / P2 复习

**P0**：当前可利用的严重安全问题，例如用户能够控制 SQL structure、执行任意 DDL/DML。最终：

```text
P0 = 0
```

**P1**：足以阻塞当前 WP 完成的重要能力问题。本阶段主要就是：

```text
P1-01
AST_STATIC_GUARD_MATERIAL_BYPASS
```

它经历多轮 Gate 后最终：

```text
P1-01 = CLOSED
P1 = 0
```

**P2**：可以接受的次要问题。WP3-D 最终：

```text
P2 = 0
```

FTS / LIKE 没有算 P2 security finding，而是明确保留为 semantic limitation。

------

# 20. 速查表

```text
目标
----
当前 SQLite SQL Injection 安全边界 + 耐久回归 Gate

当前生产漏洞
------------
NO

直接数据库技术
--------------
stdlib sqlite3

SQLite owners
-------------
5

SQL syntax sites
----------------
152

reachable/effective
-------------------
148

runtime
-------
57

startup/admin
-------------
91

business execute
----------------
13

核心原则
--------
Untrusted data = bound value
SQL structure owner = code

关键静态模型
------------
Candidate
→ Receiver
→ SQL Intent
→ Statement Classifier
→ Owner + Statement Decision

最终 Guard
----------
91 passed

WP3-D combined
--------------
171 passed

Full regression
---------------
2192 passed
42 subtests passed
0 failed
0 skipped
0 xfail

Final Gate
----------
PASS

P0 / P1 / P2
------------
0 / 0 / 0

P1-01
-----
CLOSED

Formal capability
-----------------
SQL Injection protection = SUPPORTED

Formal scope
------------
current LocalAgent production SQLite inventory

明确没有
--------
Generic SQL Firewall
Generic SQL Parser
NL2SQL
Raw SQL Tool
All-DB protection
Whole-program Python analysis
```