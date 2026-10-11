"""WP12C 责任义务推理；仅使用授权证据，继承 v3 安全合同。"""

from dataclasses import replace

from core.stage13.triage_candidate_v3 import candidate_v3_definitions

VERSION = "wp12c-flash-candidate-4"
PROMPT_VERSION = "stage13.triage-prompt.wp12c-v4"
REPAIR_VERSION = "stage13.triage-repair.wp12c-v4"

ROOT_RESPONSIBILITY = """
责任义务核对（INITIAL 内部推理补充，不增加输出字段）：
机制与 change_ref 判断正确不代表 component_id 已正确。组合 descriptor 前，逐项核对：
哪个操作/输入/共享服务承担失效义务？哪个组件只观察、调用或正常拒绝？
改变何种行为使故障消失，哪些行为保持不变？为责任组件提供角色及干预证据，
不能用“错误发生于某处”代替因果归属，也不能从 component_ids 的排列推断答案。

TEST_LOGIC：先定位被错误测试的操作契约。调用被接受、操作完成、结果可见是不同
阶段；若测试在完成承诺满足前断言，根组件应反映被等待操作/完成事件的责任边界，
而不是仅凭提交入口或测试辅助代码位置归属。若问题是同步断言自身、期望值或测试
控制流程，仍按相应测试义务判断；不要把所有 TEST_LOGIC 自动归到异步事件生产方。
TEST_DATA：分别列出数据产生/供给、传输、验证角色。只替换坏输入而验证行为不变
时，正常拒绝方不是坏数据的因果 owner；有效输入被错误拒绝则重新核对服务契约。
INFRASTRUCTURE_SERVICE：用绕行、独立探针及配置恢复定位故障共享路径，再确认
该服务职责映射到哪个可见组件。受影响客户端、转发入口不自动拥有共享服务故障。
PRODUCT_BEHAVIOR：保留业务不变量 owner 与底层实现位置的区分；不要因新增上述
规则，把已确认的产品契约责任改成 TEST/数据/基础设施责任。

先确定角色，后选择 component_id，再组合 mechanism_code 和关联的 change_ref。
同一变更涉及多个组件时，变更出现的位置也不能独自决定 root component。
Top1 优先选择直接失效义务及对照证据支持的完整根因。只有机制/处置已确定、
而证据实际支持两种不同组件责任解释且未能排除时，才允许保留一个替代候选；
替代项必须说明独立责任假设及相同干预为何仍与之相容，不把所有 inventory 枚举
成答案，不把正常工作的组件、下游症状或同一原因改写作为替代项。
明确唯一责任时只输出一个；无法支持业务决策时按原合同求证。不得依据 GT、
评分或隐藏 root 标签构造假设。repair 继续只修结构和授权引用，不修业务答案。
"""


def candidate_v4_definitions(original):
    """只发布新的候选版本，旧 v3 定义与 Baseline 完全保留。"""
    return tuple(
        (
            replace(
                d,
                agent_version=VERSION,
                instructions=d.instructions + "\n" + ROOT_RESPONSIBILITY,
                business_options={
                    **dict(d.business_options),
                    "stage13_prompt_version": PROMPT_VERSION,
                },
            )
            if d.agent_id == "ci_triage_candidate"
            else d
        )
        for d in candidate_v3_definitions(original)
    )
