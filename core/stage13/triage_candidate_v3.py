"""WP10B 根因归属优化；只消费授权证据，保留 WP08 安全约束。"""

from dataclasses import replace

from core.stage13.triage_candidate_vnext import VERSION as PREVIOUS_VERSION
from core.stage13.triage_candidate_vnext import candidate_definitions

VERSION = "wp10b-flash-candidate-3"
PROMPT_VERSION = "stage13.triage-prompt.wp10b-v3"
REPAIR_VERSION = "stage13.triage-repair.wp10b-v3"

ROOT_HARDENING = """
根因归属与排序补充（INITIAL 内部推理，继续严格遵守上述安全及七字段规则）：
先还原因果链：触发变更/条件 → 失效的约束或操作 → 跨组件传播 → 可见症状。
observed failure location != root cause owner component。component_ids 只是合法集合，
其顺序、名字熟悉程度、日志出现次数都不能证明责任。不能只复述错误症状。

为每个假设分别确定三个字段，然后组合成一个完整 cause_descriptor：
component_id 是该机制的因果责任组件；mechanism_code 是失效机制；change_ref 是
有证据关联到该完整原因的变更。不要把组件、机制、变更拆成多个候选。
责任组件既不是必然最先报错的组件，也不是必然包含底层实现细节的依赖组件。
PRODUCT_BEHAVIOR：先找被违反的业务不变量/接口契约由哪个组件负责。产品契约可能
借助依赖中的索引、缓存或配置实现；该实现发生变更不自动证明依赖组件本身有故障。
若依赖按修改后的配置正常工作，但调用方失去其产品约束，根因归属应体现产品契约
责任，并用 summary 说明底层变更如何导致该业务回归。若独立证据证明依赖服务本身
违反其契约，则按实际依赖故障归属，不一律选择上游或下游。
TEST_DATA：区分提供错误夹具/请求数据的一侧与按契约拒绝它的一侧；只更换数据就
恢复、接收方行为未变时，不应把正常拒绝数据的组件直接定为根因。
INFRASTRUCTURE_SERVICE：跨组件连接/共享设施的故障需要定位受控实验隔离的通信
或服务路径责任；报错入口或转发位置不是充分归属证据。不要将共享设施误归本地
ENVIRONMENT_CONFIG，不能因非产品原因就升级产品 Ticket。

冻结证据排序优先级：
1. 直接因果/对照干预证据：control、rollback、disable change、isolation experiment、
   alternate path/component；核对只改变了什么、哪些行为保持不变、失败是否消失。
2. 关联变更及明确时序，且与同一根因机制有关。
3. 跨组件互证：独立探针、依赖正常响应、输入生成方与接收方、传播方向。
4. 强诊断证据；5. 弱症状相关或仅提到组件名字。
干预证明了机制并不总能唯一证明组件归属；仍需核对业务/依赖契约责任。

Top1 是上述证据最支持的完整因果根，不是最显眼的报错位置。
只输出 1..3 个真实有证据支持的假设：唯一原因只输出一个，绝不强凑 Top3。
同一根因换措辞、拆出 change、下游症状都不是新假设。
如果跨组件证据确实支持不同的责任归属假设且无法唯一定位，可保留下一候选；
每项必须明确其不同的责任假设、完整 descriptor 和相关 evidence_refs，不得把
inventory 全枚举当作推理。若连业务决策也无法由证据区分，NeedMoreEvidence=true
并同时 REQUEST_MORE_EVIDENCE；不得捏造确定性或虚构归属事实。
对能由已有直接诊断/对照干预确定类别与处置的情况，保持其安全决策。

SCHEMA_REPAIR 仍仅使用 original_authorized_input、rejected_output、validation_errors，
只修结构/授权引用，不依据评分或 GT 改业务答案。不得读取隐藏 Case/GT 或新工具。
"""


def candidate_v3_definitions(original):
    """在全新 subject version 叠加通用因果规则，不修改旧定义字节。"""
    previous = candidate_definitions(original, PREVIOUS_VERSION)
    return tuple(
        (
            replace(
                d,
                agent_version=VERSION,
                instructions=d.instructions + "\n" + ROOT_HARDENING,
                business_options={
                    **dict(d.business_options),
                    "stage13_prompt_version": PROMPT_VERSION,
                },
            )
            if d.agent_id == "ci_triage_candidate"
            else d
        )
        for d in previous
    )
