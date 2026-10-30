"""获准 CI 评测可见输入；不接收诊断 GT、报告或可执行材料。"""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from core.runtime.agent_loop.quality_contract import catalog_digest

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
ID = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$')]
Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Revision = Annotated[int, Field(ge=1, le=16)]
class Environment(Strict):
    environment_id: ID
    ip: Annotated[str, Field(pattern=r'^192\.0\.2\.[0-9]{1,3}$')]
    software_version: Literal['product-2.0','product-2.1','product-2.1.1']
    parser_version: ID
    decoder_table_version: ID
    parser_supported_field_revision: Revision
    decoder_supported_field_revision: Revision
    feature_flags: dict[Literal['compressed_transport'], bool]
    buffer_behavior: Literal['compatible','compact','legacy']
class TestCase(Strict):
    case_id: ID
    feature_id: ID
    required_field_revision: Revision
    input_revision: Revision
    golden_id: ID
    golden_version: ID
    comparison_rule_id: Literal['semantic-fields-equal/1']
    compressed_transport: bool | None = None
    input_contract: Literal['tlv'] | None = None
    buffer_requirement: Literal['legacy','modern'] | None = None
class Report(Strict):
    report_artifact_id: ID
    field_revision: Revision
    payload_matches_golden: bool
class InitialState(Strict):
    environment: Environment
    test_case: TestCase
    report: Report
    diagnostics_complete: bool
class CaseInput(Strict):
    schema_version: Literal['ci-synthetic-case-input.v2']
    template_id: Literal['operator-frozen-ci']
    producer_version: Literal['ci-simulation.v2']
    initial_state: InitialState
    state_digest: Digest
    @model_validator(mode='after')
    def bound_state(self):
        if catalog_digest(self.state()) != self.state_digest:
            raise ValueError('CI_PAIR_STATE_DIGEST_INVALID')
        return self
    def state(self):
        return self.initial_state.model_dump(exclude_none=True)
class FrozenPair(Strict):
    case_id: ID
    case_version: ID
    case_digest: Digest
    input_digest: Digest
    dataset_id: ID
    dataset_version: ID
    plan_id: ID
    plan_version: ID
    source_ref: ID
    visible_input: CaseInput
    @model_validator(mode='after')
    def bound_input(self):
        if catalog_digest(self.visible_input.model_dump(exclude_none=True)) != self.input_digest:
            raise ValueError('CI_PAIR_INPUT_DIGEST_INVALID')
        return self

def same_intent(payload, request):
    """摘要和Attempt不参与额度身份；改摘要不能刷新相同意图。"""
    if payload.get('contract_version') != 'ci-synthetic-evaluation-execute.v2':
        return False
    old, new = payload['frozen_ci'], request.frozen_ci.model_dump(exclude_none=True)
    keys = ('plan_id','plan_version','case_id','case_version')
    return payload['mode'] == request.mode and all(old[k] == new[k] for k in keys)
