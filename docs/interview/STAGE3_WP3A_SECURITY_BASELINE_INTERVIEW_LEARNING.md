# LocalAgent Stage 3 WP3 — Security Baseline 工程面试学习材料

# 1. 一句话项目 / 工作包定义

WP3 Security Baseline（安全基线）的目标，是在 WP2 Tool Platformization（工具平台化）已经解决“哪个 Agent 可以调用哪个 Tool”之后，继续补上：

```text
即使 Agent 有权调用 Tool
也不代表这次 Tool Invocation 可以访问任意 Resource
```

最终形成：

```text
Tool Permission
        ↓
Tool Risk / Approval
        ↓
Resource Authorization
        ↓
ToolExecutionService
        ↓
真实资源访问
```

当前 WP3 已正式：

```text
WP3 Final Gate = PASS

P0 = 0
P1 = 0
P2 = 1 retained finding cluster
TEST_GAP = 0
DOC_ONLY = 0
ENVIRONMENT_BLOCKED = 0

WP3 completed = YES
Allowed to continue WP4 = YES
```

但：

```text
Stage 3 completed = NO
```

。

------

# 2. 为什么要做

WP2 已经解决了：

```text
Tool 是否存在？
Agent 是否可以调用这个 Tool？
Invocation 风险是什么？
是否需要 Approval？
谁真正执行 Tool？
```

但是进入 WP3 前仍存在一个明确缺口：

```text
core_router
→ list_files
→ Permission ALLOW
```

不代表：

```text
C:\任意目录
D:\任意目录
用户目录
系统目录
其它 OS 可读资源
```

就都应该允许访问。

Scout 已真实确认：

```text
Resource Authorization = NOT_IMPLEMENTED
Resource Authorization Owner = NONE

list_files arbitrary local path read = YES
analyze_excel arbitrary local path read = YES
```

这里的 `arbitrary` 指：

> 应用层没有资源范围限制，只要进程自身的 Windows ACL（访问控制列表）允许读取，该 File Tool 就可能读取。

这不是说系统绕过了操作系统权限。

------

# 3. 真实性与完成边界

## 3.1 已真实实现

WP3 最终真实实现：

```text
ResourceAuthorizationService

FilesystemResourcePolicy

ToolResourceExtractorCatalog

multiple explicit filesystem read roots

Windows real-target canonicalization

list_files Resource Authorization

analyze_excel Resource Authorization

Wiki output-root containment

credential-safe Settings repr/str

PRODUCTION numeric loopback-only network boundary
```

------

## 3.2 已真实测试

Final Gate 实际验证：

```text
WP3 targeted
42 passed

WP3 E2E
2 passed × 3

WP2 regression
119 passed

Runtime Security
218 passed

Wiki / KB
115 passed

Settings / Startup
229 passed

Five-file migration
189 passed

Full regression
1916 passed
0 failed
0 skipped
42 subtests passed
```

以及：

```text
compileall = PASS
uv lock --check = PASS
git diff --check = PASS
packaging diff = EMPTY
scope = PASS
```

------

## 3.3 真实发现，但不是用户生产事故

Scout 真实发现：

### Finding 1

```text
File Tool arbitrary path read
```

真实性：

```text
SOURCE_AUDIT_FINDING
+
DIRECT_PROBE_FINDING
```

### Finding 2

```text
Wiki remote sn
→ configured output root escape
```

真实性同样是：

```text
SOURCE_AUDIT_FINDING
+
DIRECT_PROBE_FINDING
```

### Finding 3

```text
Settings repr/str
→ credential exposure
```

使用 synthetic secret（合成敏感值）复现。

### Finding 4

```text
PRODUCTION 可配置 non-loopback
+
无 user authentication
+
无 inbound TLS
```

属于部署安全债务。

这些都不是：

```text
USER_REAL_PRODUCTION_INCIDENT
```

。

------

## 3.4 尚未实现

WP3 PASS 后依然没有：

```text
authenticated human IAM
login
OAuth / OIDC
RBAC / ABAC

inbound Local API TLS

request-size limit

full Sandbox

process isolation
container isolation
VM isolation

OS-level filesystem isolation

TOCTOU elimination

generic DLP

network egress sandbox

approval evidence
Human Review
durable approval resume
```

------

# 4. 修改前架构与根因

WP3 前 File Tool 链：

```text
User / Model
    ↓
AgentRouter
    ↓
ToolRegistry
    ↓
ToolGovernanceService
    ↓
ToolExecutionService
    ↓
ToolAdapter
    ↓
list_files / analyze_excel
    ↓
Filesystem
```

看起来已经有 Governance（治理），但真正的问题是：

```text
Governance
只回答：
Agent → Tool 是否允许

没有回答：
Tool Invocation → Resource 是否允许
```

因此真实缺口是：

```text
ToolGovernanceService ALLOW
        ↓
[NO RESOURCE AUTHORITY]
        ↓
ToolExecutionService
        ↓
Filesystem
```

------

# 5. 方案讨论与取舍

# 5.1 为什么不能直接把 Resource Authorization 塞进 ToolGovernanceService？

Codex 明确否决：

```text
ToolGovernanceService extension
```

作为最终方案。

原因是它已有明确职责：

```text
Agent → Tool Permission
Risk
Approval requirement
```

如果再加入：

```text
filesystem roots
path canonicalization
resource containment
```

就会把：

```text
Who may use the Tool?
```

和：

```text
May this invocation access this resource?
```

混在一起。

最终采用：

```text
ToolGovernanceService
= Agent → Tool Authority

ResourceAuthorizationService
= Invocation → Resource Authority
```

。

------

# 5.2 为什么不把授权塞进 ToolExecutionService？

这其实是一个有吸引力的方案。

因为 ToolExecutionService 是：

```text
sole execution owner
```

把资源授权放在里面，看起来最难绕过。

但问题是：

当前 ToolExecutionService contract没有：

```text
actual executing principal
resource authorization policy
resource context
```

如果为了 WP3 改它：

就会扩大 Stage 2 已冻结的 Tool Runtime contract。

因此 Codex选择：

```text
AgentRouter pre-service Gate
```

即：

```text
Resource Authorization
→ before ToolExecutionService
```

而 Final Gate 又额外扫描：

```text
ToolExecutionService.execute_sync()
production business caller = AgentRouter only
```

确认当前没有第二条 production bypass。

------

# 5.3 为什么单独设计 ResourceAuthorizationService？

最终 Owner Map：

```text
ToolRegistry
→ Tool 是否存在

ToolGovernanceService
→ Agent 是否可以使用 Tool

ResourceAuthorizationService
→ 本次 Invocation 是否可以访问该 Resource

ToolExecutionService
→ 如何执行
```

每个 Owner 只回答一个问题。

这是整个 WP3 最重要的架构思想。

------

# 5.4 为什么是 application-wide roots，而不是 per-Agent / per-Tool roots？

Scout 没有找到真实业务证据证明当前需要：

```text
不同 Agent 不同路径
不同 Tool 不同 root
```

所以 Codex拒绝过度设计。

最终冻结：

```text
multiple explicit application-wide filesystem READ roots
```

即所有受保护 File Tool共享同一组 read roots。

这是 intentional MVP boundary（刻意的最小可行产品边界）。

------

# 5.5 为什么支持多个 root？

单一 root 无法表达：

```text
C:\LocalAgentData
D:\Knowledge
```

等少量明确数据源。

所以选择：

```text
multiple explicit roots
```

但不是：

```text
arbitrary roots
dynamic roots
request-provided roots
```

。

------

# 5.6 为什么 PRODUCTION 没配 root 直接启动失败？

安全系统最危险的默认：

```text
配置缺失
→ unrestricted
```

会形成：

```text
fail open
```

所以当前：

```text
PRODUCTION absent roots
→ startup FAIL

PRODUCTION explicit empty roots
→ startup FAIL
```

不存在：

```text
allow all fallback
```

。

------

# 6. 最终架构

完整 File Tool 调用链：

```text
ToolRegistry.require
        ↓
ToolGovernanceService.authorize_tool
        ↓
adapter.build_invocation
        ↓
adapter.spec_for
        ↓
ToolGovernanceService.evaluate_invocation
        ↓
ToolResourceExtractorCatalog.extract
        ↓
ResourceAuthorizationService.require_authorized
        ↓
ToolExecutionService.execute_sync
        ↓
ToolAdapter
        ↓
Business Tool
        ↓
Filesystem
```

关键：

```text
Permission
→ Risk / Approval
→ Resource Authorization
→ Execution
```

顺序明确。

------

# 7. 核心状态机和时序

## 7.1 Authorized path

```text
Tool Permission ALLOW
        ↓
Risk / Approval ALLOW
        ↓
Extract Resource
        ↓
Resource ALLOW
        ↓
TOOL_STARTED
        ↓
ToolExecutionService
        ↓
real Tool
        ↓
TOOL_COMPLETED
        ↓
final answer
```

------

## 7.2 Resource DENY

```text
Tool Permission ALLOW
        ↓
Risk / Approval ALLOW
        ↓
Extract Resource
        ↓
Resource DENY
        ↓
STOP
```

最终必须：

```text
TOOL_STARTED = 0
TOOL_COMPLETED = 0

ToolExecutionService calls = 0

business Tool access = 0

FINAL_ANSWER model = 0
```

然后固定输出：

```text
Tool 调用未执行：请求的资源不在允许访问范围内（TOOL_RESOURCE_DENIED）
```

。

------

## 7.3 为什么 Run 还是 SUCCEEDED？

当前 Resource DENY被定义为：

```text
safe delivered business denial
```

所以：

```text
STEP_COMPLETED
SUCCEEDED / DELIVERED

RUN_COMPLETED
SUCCEEDED / DELIVERED
```

而不是：

```text
FAILED
PENDING
```

。

这和 WP2 的 `APPROVAL_REQUIRED` 安全拒绝语义保持一致。

------

# 8. 数据、权限和 Owner 边界

| Concern                | Owner                        |
| ---------------------- | ---------------------------- |
| Tool identity          | ToolRegistry                 |
| Tool Permission        | ToolGovernanceService        |
| Tool Risk              | ToolGovernanceService        |
| Approval requirement   | ToolGovernanceService        |
| Resource extraction    | ToolResourceExtractorCatalog |
| FilesGovernanceService |                              |
| Approval requirement   | ToolGovernanceService        |
| Resource extraction    | ToolResourceExtractorCatalog |
| Filesystem read policy | FilesystemResourcePolicy     |
| Resource decision      | ResourceAuthorizationService |
| Execution              | ToolExecutionService         |
| Tool business behavior | Tool / Adapter               |
| Final publication      | OutputGate                   |
| Runtime facts          | Event / Journal              |
| Settings parse         | Settings                     |
| Production composition | server.py::lifespan          |

------

## 必须熟练的几个“不等于”

```text
Tool Permission
!= Resource Authorization
Resource Authorization
!= Windows ACL
Resource Authorization
!= Sandbox
Allowed Root
!= OS-level filesystem isolation
Risk
!= Resource Permission
Approval
!= Resource Authorization
```

------

# 9. Windows 路径安全合同

这是 WP3 面试价值最高的一块。

最终算法不是：

```text
startswith()
```

而是：

```text
1. lexical validation
2. Path.resolve(strict=TRY type check
4. ntpath.normpath
5. ntpath.normcase
6. ntpath.commonpath
7. component-aware containment
```

fileciteturn74file0

------

# 10. 为什么 `startswith(root)` 不安全

假设：

```text
allowed root:
C:\workspace
```

攻击/错误 candidate：

```text
C:\workspace-secret\data.csv
```

字符串：

```text
"C:\workspace-secret".startswith("C:\workspace")
== True
```

但它根本不在 `workspace` 目录里。

所以必须：

```text
commonpath(root, candidate)
==
root
```

做 component-aware containment（组件级包含判断）。

------

# 11. 为什么先 `resolve(strict=True)`

例如：

```text
C:\workspace\sub\..\..\outside
```

lexical path表面包含：

```text
C:\workspace
```

但真实 target：

```text
C:\outside
```

`resolve(strict=True)` 后：

再判断真实目标是否仍在 root 内。

当前 File Tool都是读取已经存在的：

```text
FILE
DIRECTORY
```

所以 strict resolution是可行的。

------

# 12. 为什么还要处理 Windows case

Windows通常：

~~~text
C:\DAT语义。

因此：

```text
ntpath.normcase
~~~

进入 security comparison。

Final Gate也真实做了 mixed-case probe，并得到正确 ALLOW。fileciteturn74file0

------

# 13. 为什么拒绝 relative path

WP3前：

```text
foo
..\foo
```

最终由：

```text
process cwd
```

解释。

这意味着实际访问范围受启动目录隐式影响。

所以 WP3冻结：

```text
relative path = DENY
```

不再猜：

```text
cwd
first allowed root
project root
```

。

------

# 14. 为什么拒绝 `C:foo`

Windows：

```text
C:foo
```

不是：

```text
C:\foo
```

它是 drive-relative path（驱动器相对路径）。

会依赖 C 盘当前工作目录。

所以：

```text
C:foo
→ DENY
```

。

------

# 15. 为什么拒绝 UNC

当前 Repo没有证据要求 LocalAgent File Tool支持：

```text
\\server\share
```

所以 WP3采用最小安全策略：

```text
UNC candidate = DENY
UNC configured root = DENY
```

这不代表未来不能支持。

而是未来如果需要：

```text
new Architecture Decision
```

。

------

# 16. Junction / Symlink 的真实验证

这部分尤其适合面试。

Scout阶段：

```text
real symlink/junction probe
= ENVIRONMENT_BLOCKED
```

但 Phase 3 后：

```text
symlink创建失败
→ 尝试真实 Windows junction
→ junction创建成功
```

Final Gate再次独立验证：

```text
<allowetside-root>
```

最终：

```text
resolved target outside
→ DENY
```

所以：

```text
ENVIRONMENT_BLOCKED = 0
```

。fileciteturn74file0

------

# 17. TOCTOU 为什么仍是 Known Limitation

即使：

```text
resolve path
→ authorize
```

之后，到：

```text
open/list
```

之间仍存在一个时间窗口。

理论上资源可能被替换。

这叫：

```text
TOCTOU
Time Of Check To Time Of Use
检查时间与使用时间竞态
```

当前 WP3没有：

```text
handle-based secure open
reparse lock
OS sandbox
ACL virtualization
```

所以必须保留：

```text
KNOWN_LIMITATION
DEFER_TO_FULL_SANDBOX
```

。

不能说：

> “我们的路径授权已经彻底没有竞态。”

------

# 18. Wiki Root Escape

Scout真实发现 Wiki同步还有另一条非 Tool filesystem write问题。

旧链：

```text
configured save_dir
+
remote write
```

remote `sn` 未做完整安全约束。

Direct probe真实复现：

```text
remote sn
→ outside configured root write
```

fileciteturn69file0

------

# 19. 为什么 Wiki 不复用 ResourceAuthorizationService

这是一个很好的架构追问。

ResourceAuthorizationService当前合同是：

```text
Tool Invocation
→ filesystem READ resource
```

Wiki属于：

```text
remote metadata
→ component-owned output directory
→ filesystem WRITE
```

如果强行复用：

就会把 ResourceAuthorizationService从：

```text
Tool read policy
```

扩大成通用 filesystem security platform。

所以 Wiki自己维护：

```text
configured-output containment invariant
```

。

这是“职责分离”而不是“重复实现”。

------

# 20. Wiki 最终安全策略

remote `sn`必须是：

```text
single Windows leaf component
```

拒绝：

```text
/
\
NUL
control chars
Windows invalid chars
.
..
trailing dot
trailing space
DOS device basename
```

而且：

```text
invalid sn
→ reject / skip
```

不能：

```text
../foo
→ __foo
→ 继续写
```

。

因为那属于把攻击/错误输入“静默转换成另一个合法身份”。

------

# 21. Wiki Final Target Containment

最终：

```text
output candidate
→ Path.resolve(strict=False)
→ normpath
→ normcase
→ ainment
```

如果 existing link target指向 outside：

同样：

```text
DENY
```

Final Gate实际做了 junction target probe。fileciteturn74file0

------

# 22. Settings Secret 为什么也属于 WP3

Scout synthetic probe：

```text
remote_api_key = WP3_TEST_SECRET_9F31
```

默认 dataclass：

```text
repr(settings)
```

会包含这个值。

虽然 current logging并没有直接打印 Settings，但它属于：

```text
latent disclosure defect
```

未来任何：

~~~text
logger.debug(settings)
assert settings
exce可能泄漏 credential。

所以采用非常窄的修复：

```text
field(repr=False)
~~~

只保护：

```text
remote_api_key
wiki_cookie
```

。fileciteturn74file0

------

# 23. 为什么不做通用 Redaction Framework

因为真实 finding只证明：

```text
credential-bearing Settings repr
```

有问题。

没有证据要求重构：

```text
all logs
all errors
all Settings fields
all strings
```

所以只做 narrow fix。

这也是 Minimal Productionization（最小必要生产化）的体现。

------

# 24. 网络安全边界为什么选择 loopback-only

Scout发现：

```text
default api_host = loopback
```

但：

```text
non-loopback configurable
```

同时：

~~~text
API user authentication =``

如果继续允许：

```text
0.0.0.0 / LAN
~~~

作为 PRODUCTION，就意味着当前系统实际暴露在：

```text
ambient network trust
```

下。

fileciteturn69file0

------

# 25. 为什么不临时实现一个 X-API-Key？

因为一个：

```text
hardcoded X-API-Key
```

没有：

```text
credential lifecycle
rotation
identity
RBAC
session
audit
```

很可能只是：

```text
fake security
```

所以 Codex选择收窄信任边界，而不是实现半套 IAM。

------

# 26. 最终 PRODUCTION 网络合同

PRODUCTION：

```text
numeric loopback only
```

实际：

```text
127.0.0.1 → ALLOW
127.0.0.2 → ALLOW
::1       → ALLOW

0.0.0.0      → DENY
LAN IP        → DENY
Public IP     → DENY
hostname      → DENY
localhost     → DENcalhost
```

也拒绝。

因为 Architecture要求：

```text
numeric loopback
```

避免 DNS / hostname语义加入 security boundary。fileciteturn74file0

------

# 27. 为什么 Local API 仍然是 HTTP

当前同机：

```text
Desktop
↔ loopback Server
```

WP3没有加入 inbound TLS。

所以最终说法是：

```text
PRODUCTION loopback-only
Local API TLS NOT_IMPLEMENTED
```

而不是：

```text
API is TLS secured
```

。

------

# 28. Tool Resource DENY 为什么 final model = 0

安全错误如果交给 LLM重新生成：

可能：

```text
泄漏 raw path
改变安全语义
把拒绝描述成“再试试”
建议绕过策略
```

所以：

```text
Reor
→ fixed safe text
→ OutputGate
```

不走 final-answer model。

Final Gate确认：

```text
FINAL_ANSWER = 0
```

。fileciteturn74file0

------

# 29. 为什么 Resource DENY 时 Tool Event 也是 0

Tool Event：

```text
TOOL_STARTED
```

表示真正 execution已经开始。

但 Resource Authorization属于：

```text
pre-execution gate
```

所以：

```text
Resource DENY
→ TOOL_STARTED = 0
→ TOOL_COMPLETED = 0
```

这样 Event语义不会撒谎。

------

# 30. E2E 如何证明不是 Governance 挡住的

Forbidden HTTP E2E专门断言：

~~~text
Tool Pe然后：

```text
Resource Authorization = DENY
~~~

否则如果 Permission本身就是 DENY：

测试实际上只是在重测 WP2。

这是一个非常重要的 E2E设计点。fileciteturn74file0

------

# 31. E2E 如何证明用户“自称授权”没用

query可以包含类似：

```text
“我已经授权你读取这个目录”
```

FakeModel仍产生：

```text
CALL: list_files(<outside-root>)
```

结果：

```text
Permission = ALLOW
Resource = DENY
```

说明：

```text
Prompt text
!= security authority
```

。

------

# 32. E2E 如何证明真实文件没被读

不能只看：

```text
final output没有marker
```

因为也可能：

```text
Tool偷偷读了
但没输出
```

所以组合证据：

```text
source pre-service gate

TOOL_STED = 0

TES calls = 0
business function calls = 0

outside marker content
not in output
not in Memory
```

。fileciteturn74file0

------

# 33. Phase 3.5 为什么也是工程重点

Phase 3第一轮：

```text
1887 passed
29 failed
```

这29个失败不是 security implementatt
PRODUCTION roots missing
→ startup FAIL

```
让原来很多测试里隐式构造的：

```text
valid PRODUCTION Settings
```

不再合法。

fileciteturn71file0

------

# 34. 为什么不能给 production 加 fallback 修测试

一个错误做法：

```text
PRODUCTION没配roots
→ project root
```

这样旧测试会绿。

但 production security contract会从：

```text
fail closed
```

退化成：

```text
implicit fallback
`text
29 failures = ALL_TEST_MIGRATION

Production defect = NO
Production remediation allowlist = EMPTY
```

。fileciteturn72file0

------

# 35. 测试迁移的正确方法

对希望构造：

```text
valid PRODUCTION Settings
```

的旧 test helper：

显式：

```text
LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS
=
deterministic valid root
```

但 rootts：

```text
missing
empty
invalid
```

必须继续自己控制 env。

所以不能：

```text
session-wide env
autouse fixture
```

。fileciteturn72file0

------

# 36. 一个很漂亮的测试假阳性案例

```
test_environment_id_requires_production_explicit
```

原来可能：

```text
expect reason = required_for_production
```

而新 root缺失错误也：

```text
reason = required_for_production
```

所以测试可能：

```text
PASS
```

但实际失败的是错误字段。

Phase 3.5迁移后直接 probe确认：

```text
code_ERROR
field = LOCAL_AGENT_ENVIRONMENT_ID
reason = required_for_production
```

证明它现在测试的才是真正的 environment_id。fileciteturn74file0

------

# 37. Bad Cases

# Bad Case 1：Tool Permission ALLOW = 任意路径 ALLOW

真实性：

```text
原系统真实安全债务
SOURCE_AUDIT_FINDING
DIRECT_PROBE_FINDING
```

修复：

```text
独立 Resource Authorization
```

------

# Bad Case 2：Path `startswith(root)`

真实性：

```text
HYPOTHETICAL_BAD_CASE
regression-covered
```

当前实现没有使用它。

------

# Bad Case 3：Traversal

进入 WP3前：

```text
<root>/sub/../../outside
```

可按普通filesystem语义访问 outside。

由于当时根本没有 root policy：

不能称：

```text
root bypass
```

正确说：

```text
resource boundary absent
```

现在：

```text
resolve real target
→ outside
→ DENY
```

。

------

# Bad Case 4：Prefix collision

```text
workspace
workspace-secret
```

当前通过 `commonpath` 防止。

真实性：

```text
HYPOTHETICAL_BAD_CASE
regression-covered
```

------

# Bad Case 5：Junction escape

```text
allowed/link
→ outside
```

Phase 3 / Final Gate已经真实创建 junction验证：

```text
DENY
```

但原先不存在真实生产攻击。

所以分类：

```text
HYPOTHETICAL_BAD_CASE
real regression-covered
```

。

------

# Bad Case 6：用户自称“已授权”

```text
Prompt
→ cannot mutate startup security policy
```

E2E已覆盖。

------

# Bad Case 7：Resource DENY后仍发 TOOL_STARTED

这会破坏 Event合同。

当前：

```text
TOOL_STARTED = 0
```

。

------

# Bad Case 8：Resource DENY后给 LLM改写

当前：

```text
FINAL_ANSWER = 0
```

避免安全结果被重写。

------

# Bad Case 9：Wiki非法sn sanitize后继续使用

错误：

```text
../foo
→ __foo
```

正确：

```text
invalid identity
→ skip
```

。

------

# Bad Case 10：Settings Secret只因为现在没人logging就不处理

这是：

```text
latent disclosure
```

不是“没有 bug”。

当前使用：

```text
repr=False
```

关闭。

------

# Bad Case 11：为了29个测试失败回退production合同

这是典型：

```text
test-driven fail-open
```

最终拒绝。

------

# Bad Case 12：全局设置root让全量测试通过

错误：

```text
session-wide env
```

会让：issing root

```
测试失真。

最终 full regression前真实确认：

```text
LOCAL_AGENT_TOOL_ALLOWED_READ_ROOTS = ABSENT
```

。fileciteturn74file0

------

# 38. 测试架构

WP3采用三层。

## Layer 1 — Policy / canonicalization

覆盖：

```text
authorized
outside
traversal
prefix
relative
drive-relative
case
UNC
device
nonexistent
wrong type
empty roots
```

------

## Layer 2 — Tool / Wiki integration

真实：

```text
list_files
analyze_excel
Wiki
```

- temp filesystem。

------

## Layer 3 — Full HTTP E2E

```text
/api/chat
→ COORDINATED
→ Planning
→ Governance
→ Resource Authorization
→ Tool / DENY
→ OutputGate
→ Journal
→ Memory
```

唯一 business mock：

```text
external model output
```

。

------

# 39. P0 / P1 / P2 复习

# P0：必须熟练

## 1. Permission 与 Resource Authorization区别

```text
Who may use Tool?
vs
May this invocation access this resource?
```

## 2. ResourceAuthorizationService Owner

## 3. 完整 Gate ordering

## 4. Windows canonicalization算法

## 5. DENY核心不变量

```text
TOOL_STARTED=0
TES=0
business access=0
final model=0
```

## 6. PRODUCTION loopback-only原因

## 7. Wiki root containment

## 8. Settings repr secret修复

------

# P1：建议熟练

## 9. 为什么不改 ToolExecutionService

## 10. 为什么 application-wide roots

## 11. 为什么relative/UNC/device deny

## 12. junction与TOCTOU区别

## 13. 为什么 Run仍SUCCEEDED

## 14. Test migration为什么不能改production

## 15. 假阳性test案例

## 16. Mock boundary

------

# P2：了解即可

## 17. Future IAM

## 18. Future Sandbox

## 19. TOCTOU hardening

## 20. Request-size limit

## 21. DLP

## 22. External TLS/reverse proxy trust

------

# 40. 30 秒面试表达

我在 LocalAgent Stage 3 做过一轮最小安全基线建设。WP2已经解决Agent能不能调用Tool，但当时`list_files`和`analyze_excel`只要Permission ALLOW，就可以访问进程OS权限范围内的任意路径，所以我把Tool Permission和Resource Authorization拆成两个Authority。

我新增了application-scoped `ResourceAuthorizationService`，配置多组显式read roots。Windows路径不是字符串startswith，而是先拒绝relative、drive-relative、UNC、device path，再做`Path.resolve(strict=True)`，之后用`normcase/normpath/commonpath`做真实目标的component containment。

Resource DENY发生在ToolExecutionService之前，所以Tool event、真实业务访问和final model调用全部是0，只返回固定安全文本。

另外还修了Wiki remote filename越界写和Settings secret repr泄漏，并把当前没有Auth/TLS的PRODUCTION信任边界收窄成numeric loopback-only。

最终1916个测试全部通过，P0/P1和mandatory TEST_GAP都归零。

------

# 41. 2 分钟面试表达

WP3的背景是WP2已经把Tool Registry、Permission、Risk和Approval做好了，但我发现这只能回答“这个Agent能不能用这个Tool”，不能回答“这次Tool调用能不能访问这个具体文件”。

Scout阶段我用temp目录真实复现了`list_files`和`analyze_excel`可以读取任意OS可读absolute path。当时也没有allowed-root policy，所以我没有把`..`描述成path traversal bypass，因为根本没有policy可以绕；正确的问题是application resource boundary不存在。

架构上我没有扩展ToolGovernanceService，也没有修改ToolExecutionService冻结合同，而是新增独立application-scoped ResourceAuthorizationService。调用顺序保持Permission→build/spec→Risk/Approval→Resource Authorization→ToolExecutionService。

Path Policy是multiple explicit read roots。候选路径先在Windows lexical层拒绝relative、drive-relative、UNC和device namespace，再用`Path.resolve(strict=True)`得到real target，验证FILE/DIRECTORY类型，然后`ntpath.normpath`、`normcase`、`commonpath`做component containment。我们还在Windows上真实创建junction指向root外，最终成功DENY。

完整HTTP E2E里我专门保证Tool Permission是ALLOW，再让FakeModel生成outside path，验证Resource是DENY，Tool events=0、TES/business access=0、final model=0，最终只由OutputGate发布固定安全拒绝。

此外Scout还发现Wiki remote `sn`可以把文件写出configured root，以及Settings默认repr会暴露API key/cookie，这两个也一起修了。

因为当前API没有用户认证和入站TLS，我没有临时做一个假的API key，而是把当前PRODUCTION支持边界收窄成同机numeric loopback-only。

实现后又遇到29个旧测试失败，原因是它们构造PRODUCTION Settings时没有迁移新必填root。我们没有给production加fallback，而是只迁移五个测试helper，最终全量1916 passed。

------

# 42. 高频追问

## Q1：Tool Permission和Resource Authorization区别？

Tool Permission：

```text
Agent → Tool
```

例如：

```text
core_router可以使用list_files
```

Resource Authorization：

```text
Invocation → concrete resource
```

例如：

```text
这次list_files是否可以读取D:\data
```

------

## Q2：为什么不统一叫Authorization？

可以抽象层面都叫授权，但工程Owner必须分开，否则后面policy会越来越混乱。

------

## Q3：为什么不放ToolExecutionService里？

因为它是已冻结执行Owner，当前contract里也没有Resource Security context。当前production caller只有AgentRouter，所以Router pre-service Gate可以以更小改动完成MVP。

------

## Q4：这不会被未来新caller绕过吗？

有这个风险，所以Final Gate全仓扫描TES caller，并把：

```text
新增production caller绕过Authority
```

定义为P0。

未来如果执行路径变多，可能就需要把Authority进一步下沉。

------

## Q5：为什么用`Path.resolve(strict=True)`？

因为当前保护的是已有read resource，可以用real-target semantics，能处理`..`和link/junction指向。

------

## Q6：为什么还需要commonpath？

resolve只告诉你真实路径，不告诉你它是否属于allowed root。

------

## Q7：为什么不用startswith？

prefix collision。

------

## Q8：为什么relative path直接禁止？

避免process cwd成为隐式security policy。

------

## Q9：UNC为什么禁止？

当前没有业务证据要求支持，最小安全基线优先fail closed。未来有需求再重新设计。

------

## Q10：你们支持symlink安全吗？

准确说法：

> 当前用real-target resolve做containment，并在Windows上真实创建junction escape进行了DENY验证。

但TOCTOU仍没有解决。

------

## Q11：那是不是Sandbox？

不是。

Resource Authorization只是应用层allowed-root policy。

------

## Q12：为什么Resource DENY还是SUCCEEDED？

因为用户得到的是一个确定且安全的业务拒绝结果，Runtime本身没有异常。

------

## Q13：为什么Resource DENY不让LLM解释？

避免模型改变安全语义或泄漏内部path/detail。

------

## Q14：为什么PRODUCTION限制loopback？

当前没有human auth和inbound TLS。如果允许LAN暴露，就无法证明当前生产安全边界。

------

## Q15：为什么不加一个API key？

没有identity/lifecycle/rotation的临时token很容易成为“假安全”。

------

## Q16：loopback就绝对安全吗？

不是。

它只是当前冻结的trust boundary。

同机其它进程、OS用户权限等风险并没有被消除。

------

## Q17：Settings repr为什么算安全问题？

credential-bearing object一旦被异常日志、debug或assert输出就可能泄漏。

------

## Q18：Wiki为什么不是ResourceAuthorizationService处理？

Wiki不是Tool read invocation，而是自己的remote-data→local-write invariant。为了不扩大Resource service成通用filesystem平台，它保持组件内containment。

------

## Q19：29个测试失败说明架构有问题吗？

不是。它说明新production config contract引入后，旧test fixtures需要迁移。

------

## Q20：为什么不用全局env解决29个测试？

因为会污染专门验证missing-root的negative tests。

------

## Q21：测试假阳性怎么发现的？

`environment_id`和root missing都用了`required_for_production` reason，旧test只看reason可能误通过。迁移后用direct probe验证具体`field`才确认真实语义。

------

# 43. 容易夸大 / 答错

## 错误 1

“我们做了Sandbox。”

错误。

------

## 错误 2

“我们做了文件系统隔离。”

错误。

是application root authorization。

------

## 错误 3

“Tool Permission已经能控制文件路径。”

错误。

WP3之前正是这个缺口。

------

## 错误 4

“现在可以安全跨机器部署。”

错误。

PRODUCTION明确loopback-only。

------

## 错误 5

“现在有API认证。”

错误。

------

## 错误 6

“Local API有TLS。”

错误。

------

## 错误 7

“所有Settings字段都脱敏。”

错误。

只对credential字段做repr hardening。

------

## 错误 8

“所有日志都已经脱敏。”

错误。

UI/script raw logs仍是P2 limitation。

------

## 错误 9

“解决了TOCTOU。”

错误。

明确没有。

------

## 错误 10

“支持UNC File Tool。”

错误。

当前explicit DENY。

------

## 错误 11

“Wiki和File Tool共用同一ResourceAuthorizationService。”

错误。

它们是不同安全Owner。

------

## 错误 12

“29个测试失败最后靠放宽production配置解决。”

完全相反。

production contract保持fail closed，只迁移test fixture。

------

# 44. Known Limitations

最终必须记住：

```text
No authenticated human IAM

No inbound Local API TLS
PRODUCTION loopback-only

No request-size limit

No full Sandbox

No OS-level isolation

No TOCTOU elimination

No generic DLP

No egress sandbox

No approval evidence / HITL / resume

Authorized business path/content may enter Wire / Memory

FastAPI 422 may echo caller-provided invalid input

UI/script raw logs remain

Hardcoded Wiki endpoint config debt

UNC File Tool unsupported

Resource / Tool contraery validation-only

single-process Windows Native

historical planning executor starvation accepted P2
```

fileciteturn74file0

------

# 45. 最终速查表

| 项目                      | 当前真实状态                 |
| ------------------------- | ---------------------------- |
| WP3                       | PASS / completed             |
| P0                        | 0                            |
| P1                        | 0                            |
| P2                        | 1 retained cluster           |
| TEST_GAP                  | 0                            |
| Resource Authority        | ResourceAuthorizationService |
| Policy scope              | APPLICATION_SCOPE            |
| Roots                     | multiple explicit READ roots |
| File Tools protected      | list_files / analyze_excel   |
| Permission Owner          | ToolGovernanceService        |
| Execution Owner           | ToolExecutionService         |
| Registry Owner            | ToolRegistry                 |
| Relative path             | DENY                         |
| Drive-relative            | DENY                         |
| UNC                       | DENY                         |
| Device/extended           | DENY                         |
| Traversal                 | real-target containment      |
| Prefix collision          | commonpath                   |
| Case                      | Windows normcase             |
| Junction                  | real Windows probe PASS      |
| TOCTOU                    | NOT_SOLVED                   |
| Resource DENY Tool events | 0 / 0                        |
| Resource DENY final model | 0                            |
| Resource DENY execution   | 0                            |
| Resource DENY terminal    | SUCCEEDED / DELIVERED        |
| Wiki remote escape        | FIXED                        |
| Settings credential repr  | FIXED                        |
| PRODUCTION network        | numeric loopback only        |
| Human authentication      | NOT_IMPLEMENTED              |
| Inbound TLS               | NOT_IMPLEMENTED              |
| Request size              | NOT_IMPLEMENTED              |
| Full regression           | 1916 passed                  |
| Subtests                  | 42 passed                    |
| Contracts                 | INTERNAL_RC                  |
| Stage3 completed          | NO                           |

------

# 46. 最值得记住的四句话

第一句：

> **Tool Permission回答“谁能调用Tool”，Resource Authorization回答“这次Invocation能不能访问这个具体Resource”，两者不能混成一个Authority。**

第二句：

> **Windows路径安全不能靠字符串前缀；我们的安全判断是namespace reject → real-target resolve → resource type → normcase/normpath → commonpath containment。**

第三句：

> **没有Auth/TLS时，我没有为了“看起来安全”临时加一个假的API Key，而是收窄当前PRODUCTION trust boundary到numeric loopback-only。**

第四句：

> **production contract收紧后旧测试失败，正确做法是迁移test fixture，而不是给production增加fail-open fallback；否则测试绿了，安全合同反而退化了。**