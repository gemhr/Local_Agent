"""有限业务动作的预览与白名单状态变换，不执行数据库事务。"""
from copy import deepcopy
from core.ci_simulation.execution import case_catalog, PRODUCT_RELEASES
from core.ci_simulation.service import CIError

ACTION_OPTIONS = [
    dict(action_kind="UPGRADE_PARSER_DECODER", title="升级 parser/decoder", parameters={"target_version": "2.0"}),
    dict(action_kind="SET_FEATURE_FLAG", title="设置 compressed_transport", parameters={"compressed_transport": False}),
    dict(action_kind="REFRESH_CASE_INPUT", title="刷新主用例 TLV 输入", parameters={"target_revision": 2}),
    dict(action_kind="DELIVER_PRODUCT_FIX", title="模拟研发交付 product-2.1.1（不修改真实代码）", parameters={"target_version": "product-2.1.1"}),
]


def preview(state, action_kind, target_case_id, parameters):
    cases = case_catalog(state)
    primary = cases[0]
    if target_case_id is not None and target_case_id != primary["case_id"]:
        raise CIError("CI_ACTION_TARGET_INVALID", 422)
    option = next((o for o in ACTION_OPTIONS if o["action_kind"] == action_kind), None)
    if option is None or parameters != option["parameters"]:
        raise CIError("CI_ACTION_PARAMETERS_INVALID", 422)
    changes = []
    def change(path, after):
        node = state
        for key in path[:-1]:
            node = node.get(key, {})
        before = node.get(path[-1])
        if before != after:
            changes.append(dict(path=path, before=before, after=after))
    if action_kind == "UPGRADE_PARSER_DECODER":
        for key, value in dict(parser_version="parser-2.0", decoder_table_version="decoder-2.0",
                parser_supported_field_revision=2, decoder_supported_field_revision=2).items():
            change(["environment", key], value)
    elif action_kind == "SET_FEATURE_FLAG":
        change(["environment", "feature_flags", "compressed_transport"], False)
    elif action_kind == "REFRESH_CASE_INPUT":
        if primary.get("input_contract") != "tlv" or PRODUCT_RELEASES[state["environment"]["software_version"]]["required_tlv_revision"] != parameters["target_revision"]:
            raise CIError("CI_ACTION_TARGET_INVALID", 422)
        change(["test_case", "input_revision"], parameters["target_revision"])
        change(["report", "field_revision"], parameters["target_revision"])
    else:
        if primary.get("buffer_requirement") != "legacy":
            raise CIError("CI_ACTION_TARGET_INVALID", 422)
        change(["environment", "software_version"], "product-2.1.1")
        change(["environment", "buffer_behavior"], PRODUCT_RELEASES["product-2.1.1"]["buffer_behavior"])
    if not changes:
        raise CIError("CI_ACTION_NO_CHANGE", 409)
    return dict(action_kind=action_kind, target_case_id=target_case_id, changes=changes,
        environment_id=state["environment"]["environment_id"], case_ids=[c["case_id"] for c in cases],
        description=option["title"], source_kind="HUMAN_MANUAL")


def apply_preview(state, action_kind, target_case_id, parameters, frozen_preview):
    if preview(state, action_kind, target_case_id, parameters) != frozen_preview:
        raise CIError("CI_ACTION_STALE", 409)
    updated = deepcopy(state)
    for change in frozen_preview["changes"]:
        node = updated
        for key in change["path"][:-1]:
            node = node.setdefault(key, {})
        node[change["path"][-1]] = change["after"]
    return updated
