"""WP12C 第二个且最后一个正式版本；核对语义责任与物理承载责任。"""

from dataclasses import replace

from core.stage13.triage_candidate_v3 import candidate_v3_definitions

VERSION = "wp12c-flash-candidate-4-2"
PROMPT_VERSION = "stage13.triage-prompt.wp12c-v4-2"
REPAIR_VERSION = "stage13.triage-repair.wp12c-v4-2"

ROOT_RESPONSIBILITY = """
完整根因的最后核对（INITIAL 内部推理，不增加输出字段）：
mechanism_code 和 change_ref 正确后，仍必须独立回答：这个错误属于哪个被测试的
行为或数据语义对象？哪个可见组件负责该对象？哪个组件只是调用、承载、传输或
正常验证？先对照证据中的操作职责，再选择 component_id，不能凭名字熟悉程度、
排列顺序或修改文件的位置作结论。变更作用于何处，不等于失效义务归属何处。

TEST_LOGIC 的责任对象是被错误测试的行为/结果契约。对于跨组件时序，明确区分
发起操作、接受操作、完成操作和观测结果；等待义务应绑定被断言结果对应的完成
边界。提交入口或断言调用位置不能替代该责任边界。先在 summary 说明被测对象
及其组件职责，再给 descriptor；不要因为修的是测试辅助代码就选择其调用方。

TEST_DATA 的责任对象是错误构造的业务输入/字段/schema，而不是文件存放位置。
区别数据语义与存储/传输介质：若格式不符合既有输入约定，正常存储该文件、
传输该数据或拒绝坏输入的组件都未因此违反自己的契约。沿数据所属业务对象
定位其构造/供给责任，不能把“文件/字节/导出”直接等同于存储组件故障。
若证据证明存储、传输、序列化自身违反约定，按真实失效义务判断；不能由类别
单独决定组件，也不改变有效输入被产品错误拒绝的判断。

INFRASTRUCTURE_SERVICE 先用独立探针/绕行/恢复实验定位失败共享服务，再核对
服务职责；客户端入口和正常转发位置不是责任证明。PRODUCT_BEHAVIOR 保留原有
业务不变量 owner 与底层实现的区分，已有直接契约/干预证据优先。

用被测对象、角色及干预因果关系校验 Top1 的完整 component/mechanism/change。
若责任映射确实不唯一，可按原规则保留一个有证据的不同责任候选；不能枚举
inventory，不能把下游症状、正常组件或同因换措辞充当额外原因。
唯一责任只输出一个。类别/处置无法确定时才按合同求证。所有 structured、Ticket、
Critical 安全约束保持；repair 只修格式/授权引用，无 GT、评分或隐藏答案输入。
"""


def candidate_v4b_definitions(original):
    """使用 v3 安全基线发布第二个独立版本，保留第一版字节。"""
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
