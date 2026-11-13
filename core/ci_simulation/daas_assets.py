"""公开合成 DAAS 资产的有限版本图、值域与本地执行配置映射。"""
from copy import deepcopy
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from core.feature_impact.knowledge import digest, read

EXECUTOR = "local-integer-executor@1"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Ref(Strict):
    id: str
    version: Literal["1", "2"]


class Configuration(Strict):
    gain: Literal[1,2]
    sample_rate: Literal[30]
    stimulus: Literal[7]
    software_version: Literal["baseband-2.0"]
    environment_condition: Literal["nominal"]
    reference_version: Literal["2"]


class Test(Strict):
    test_kind: Literal["DAAS_CASE"]
    test_id: str = Field(min_length=1)
    preconditions: str = Field(min_length=1)
    observation: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    historical_cases: list[str]
    coverage_gap: str
    feature_ref: Ref
    interface_ref: Ref
    tag_ref: Ref
    message_ref: Ref
    testcase_ref: Ref
    case_configuration: Configuration


def catalog(task):
    docs = [read(task,d["source_ref"]) for d in task["documents"]]
    return deepcopy(next(d["facts"]["catalog"] for d in docs if d["kind"]=="INTERFACE_CATALOG"))


def definition(catalog, kind, ref):
    value=next((v for v in catalog[kind] if v["ref"]==ref),None)
    if value is None: raise ValueError("DAAS_VERSION_UNKNOWN")
    return value


def validate_test(task, test):
    value=Test.model_validate({k:v for k,v in test.items() if k not in {"finding_id","source"}}).model_dump()
    c=catalog(task)
    feature=definition(c,"features",value["feature_ref"])
    interface=definition(c,"interfaces",value["interface_ref"])
    tag=definition(c,"tags",value["tag_ref"])
    message=definition(c,"messages",value["message_ref"])
    case=definition(c,"testcases",value["testcase_ref"])
    if (feature["interface_ref"]!=interface["ref"] or tag["interface_ref"]!=interface["ref"]
            or message["interface_ref"]!=interface["ref"] or message["tags"]!=[tag["ref"]]
            or case["message_ref"]!=message["ref"] or case["feature_ref"]!=feature["ref"]
            or value["case_configuration"]["gain"] not in tag["allowed_values"]):
        raise ValueError("DAAS_VERSION_OR_VALUE_INVALID")
    return value


def build(task,test,executor):
    if executor!=EXECUTOR: raise ValueError("DAAS_EXECUTOR_UNSUPPORTED")
    value=validate_test(task,test)
    public=dict(schema_version="daas-case-asset.v1",**{k:value[k] for k in
        ("feature_ref","interface_ref","tag_ref","message_ref","testcase_ref","case_configuration")},
        executor_ref=executor,catalog_digest=digest(catalog(task)))
    public["asset_digest"]=digest(public)
    # 固定本地合成目标的配方；不接收候选方 initial/events/offset 或预期 verdict。
    from core.ci_simulation.oracle_chain import initial_state
    state=initial_state("oracle_application_delay")
    config=value["case_configuration"]
    for field in ("ddt","kdt"): state["baseband"][field]["gain"]=config["gain"]
    state["baseband"]["daas"]["requested"]["gain"]=config["gain"]
    state["case_asset"]=public
    state["recipe_digest"]=digest(state["baseband"])
    return state
