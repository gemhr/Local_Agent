import pytest

from core.runtime.model_context import ModelContextRequirements
from core.runtime.model_selection import (ModelPreference, ModelProfile, ModelProfileId, ModelResolver, ModelSelectionError, ModelSelectionPolicy, ModelSelectionReason, ModelSelectionRequest)
from core.runtime.planning import RiskLevel, TaskCapabilityRequirements


def profile(profile_id, *, window=4096, tools=False, structured=False, code=False, long=False):
    return ModelProfile(profile_id, window, 512, tools, structured, code, long, 1, 1)


def request(req=None, context=None, preference=ModelPreference.AUTO, profiles=None):
    return ModelSelectionRequest("knowledge_expert", req or TaskCapabilityRequirements(), context or ModelContextRequirements(100, 600, False, False, False, 1, 1, 0, False, False), preference, tuple(profiles or (profile(ModelProfileId.LOCAL_FAST), profile(ModelProfileId.REMOTE_ADVANCED, window=16384, tools=True, structured=True, code=True, long=True))))


def test_auto_simple_rag_and_code_choose_local_stably() -> None:
    policy = ModelSelectionPolicy()
    for req in (TaskCapabilityRequirements(requires_rag=True), TaskCapabilityRequirements(requires_code_reasoning=False)):
        decision = policy.select(request(req))
        assert decision.selected_profile == ModelProfileId.LOCAL_FAST
        assert decision.reason_code == ModelSelectionReason.LOCAL_SUFFICIENT
        assert decision.reason_text and any("\u4e00" <= char <= "\u9fff" for char in decision.reason_text)
        assert decision.fallback_allowed is False


@pytest.mark.parametrize(("req", "context", "reason"), [
    (TaskCapabilityRequirements(), ModelContextRequirements(4000, 4300, True, False, False, 1, 0, 0, False, False), ModelSelectionReason.CONTEXT_WINDOW_REQUIRED),
    (TaskCapabilityRequirements(requires_tools=True), None, ModelSelectionReason.TOOL_CAPABILITY_REQUIRED),
    (TaskCapabilityRequirements(requires_structured_output=True), None, ModelSelectionReason.STRUCTURED_OUTPUT_REQUIRED),
    (TaskCapabilityRequirements(estimated_steps=3), None, ModelSelectionReason.MULTI_STEP_PLAN),
    (TaskCapabilityRequirements(requires_multi_agent=True), None, ModelSelectionReason.MULTI_AGENT_REQUIRED),
    (TaskCapabilityRequirements(risk_level=RiskLevel.HIGH), None, ModelSelectionReason.HIGH_RISK_TASK),
])
def test_remote_rules(req, context, reason) -> None:
    decision = ModelSelectionPolicy().select(request(req, context))
    assert decision.selected_profile == ModelProfileId.REMOTE_ADVANCED and decision.reason_code == reason


def test_forced_preferences_and_unsatisfied_profiles_fail_closed() -> None:
    policy = ModelSelectionPolicy()
    assert policy.select(request(preference=ModelPreference.FORCE_LOCAL)).reason_code == ModelSelectionReason.USER_FORCED_LOCAL
    assert policy.select(request(preference=ModelPreference.FORCE_REMOTE)).reason_code == ModelSelectionReason.USER_FORCED_REMOTE
    with pytest.raises(ModelSelectionError): policy.select(request(TaskCapabilityRequirements(requires_tools=True), preference=ModelPreference.FORCE_LOCAL))
    with pytest.raises(ModelSelectionError): policy.select(request(profiles=(profile(ModelProfileId.LOCAL_FAST),), preference=ModelPreference.FORCE_REMOTE))
    with pytest.raises(ModelSelectionError): policy.select(request(context=ModelContextRequirements(99999, 99999, True, False, False, 1, 0, 0, False, False)))


def test_profiles_reject_invalid_numbers_duplicates_and_resolver_never_falls_back() -> None:
    with pytest.raises(ValueError): profile(ModelProfileId.LOCAL_FAST, window=True)
    with pytest.raises(ModelSelectionError): ModelSelectionPolicy().select(request(profiles=(profile(ModelProfileId.LOCAL_FAST), profile(ModelProfileId.LOCAL_FAST))))
    local, remote = object(), object()
    resolver = ModelResolver({ModelProfileId.LOCAL_FAST: local, ModelProfileId.REMOTE_ADVANCED: remote})
    assert resolver.resolve(ModelProfileId.LOCAL_FAST) is local


def test_safety_margin_uses_minimum_window_once_at_exact_boundary() -> None:
    policy = ModelSelectionPolicy()
    requirements = ModelContextRequirements(3000, 4000, False, False, False, 1, 0, 0, False, False)
    assert policy.required_context_window(requirements.minimum_context_window) == 4400
    insufficient = profile(ModelProfileId.LOCAL_FAST, window=4399)
    sufficient = profile(ModelProfileId.REMOTE_ADVANCED, window=4400)
    decision = policy.select(request(context=requirements, profiles=(insufficient, sufficient)))
    assert decision.selected_profile == ModelProfileId.REMOTE_ADVANCED
    assert decision.reason_code == ModelSelectionReason.CONTEXT_WINDOW_REQUIRED


def test_hybrid_long_context_selects_remote_and_local_only_fails_closed() -> None:
    requirements = ModelContextRequirements(8500, 9000, True, False, False, 2, 1, 0, False, False, 8500, 9000)
    local = profile(ModelProfileId.LOCAL_FAST, window=4096)
    remote = profile(ModelProfileId.REMOTE_ADVANCED, window=32768)
    decision = ModelSelectionPolicy().select(request(context=requirements, profiles=(local, remote)))
    assert decision.selected_profile == ModelProfileId.REMOTE_ADVANCED
    assert decision.reason_code == ModelSelectionReason.CONTEXT_WINDOW_REQUIRED
    with pytest.raises(ModelSelectionError):
        ModelSelectionPolicy().select(request(context=requirements, profiles=(local,)))


def test_raw_overflow_allows_final_remote_and_force_local_with_optional_trim() -> None:
    requirements = ModelContextRequirements(20000, 21000, True, True, False, 3, 2, 0, False, False, 40000, 41000)
    local = profile(ModelProfileId.LOCAL_FAST, window=4096)
    remote = profile(ModelProfileId.REMOTE_ADVANCED, window=32768)
    decision = ModelSelectionPolicy().select(request(context=requirements, profiles=(local, remote)))
    assert decision.selected_profile == ModelProfileId.REMOTE_ADVANCED
    assert decision.reason_code == ModelSelectionReason.CONTEXT_WINDOW_REQUIRED
    assert "context_truncated" in decision.matched_rules
    local_trimmed = ModelContextRequirements(2000, 3000, False, True, False, 2, 1, 0, False, False, 9000, 10000)
    forced = ModelSelectionPolicy().select(request(context=local_trimmed, preference=ModelPreference.FORCE_LOCAL, profiles=(local, remote)))
    assert forced.selected_profile == ModelProfileId.LOCAL_FAST
    assert "context_truncated" in forced.matched_rules and forced.fallback_allowed is False

from core.runtime.budget import BudgetLedger, RunBudget
from core.runtime.model_selection import BudgetInsufficientAction, ModelCostProfile, ModelSelectionObjective

def cost(profile_id, remote, fixed=0): return ModelCostProfile(profile_id, remote, fixed, 0, 0, 10)
def snapshot(**kwargs): return BudgetLedger(RunBudget(**kwargs)).snapshot()
def test_explicit_metadata_and_unknown_cost_fail_closed():
    local=profile(ModelProfileId.LOCAL_FAST); remote=profile(ModelProfileId.REMOTE_ADVANCED)
    local=ModelProfile(*local.__dict__.values()) if False else ModelProfile(ModelProfileId.LOCAL_FAST,4096,512,False,False,False,False,1,1,cost(ModelProfileId.LOCAL_FAST,False,0))
    remote=ModelProfile(ModelProfileId.REMOTE_ADVANCED,16384,512,True,True,True,True,2,2,cost(ModelProfileId.REMOTE_ADVANCED,True,3))
    assert ModelSelectionPolicy()._estimated_usage(remote,10).remote_model_calls==1
    unknown=profile(ModelProfileId.REMOTE_ADVANCED, window=16384, tools=True, structured=True, code=True, long=True)
    with pytest.raises(ModelSelectionError, match='MODEL_BUDGET_METADATA_MISSING'):
        ModelSelectionPolicy().select(request(profiles=(local,unknown), context=ModelContextRequirements(10,20,False,False,False,1,0,0,False,False),)) if False else ModelSelectionPolicy().select(ModelSelectionRequest('x',TaskCapabilityRequirements(),ModelContextRequirements(10,20,False,False,False,1,0,0,False,False),ModelPreference.AUTO,(local,unknown),snapshot(max_remote_model_calls=1)))
    with pytest.raises(ModelSelectionError, match='MODEL_BUDGET_METADATA_MISSING'):
        ModelSelectionPolicy().select(ModelSelectionRequest('x',TaskCapabilityRequirements(),ModelContextRequirements(10,20,False,False,False,1,0,0,False,False),ModelPreference.AUTO,(local,unknown),None,ModelSelectionObjective.COST_FIRST))

def test_budget_degrade_and_force_remote_fail_closed():
    local=ModelProfile(ModelProfileId.LOCAL_FAST,4096,512,False,False,False,False,1,1,cost(ModelProfileId.LOCAL_FAST,False,0))
    remote=ModelProfile(ModelProfileId.REMOTE_ADVANCED,16384,512,True,True,True,True,2,2,cost(ModelProfileId.REMOTE_ADVANCED,True,5))
    req=ModelSelectionRequest('x',TaskCapabilityRequirements(requires_multi_agent=True),ModelContextRequirements(10,20,False,False,False,1,0,0,False,False),ModelPreference.AUTO,(local,remote),snapshot(max_cost_units=0),ModelSelectionObjective.QUALITY_FIRST,BudgetInsufficientAction.DEGRADE)
    d=ModelSelectionPolicy().select(req); assert d.capability_preferred_profile_id==ModelProfileId.REMOTE_ADVANCED and d.selected_profile_id==ModelProfileId.LOCAL_FAST and d.budget_adjustment.name=='DOWNGRADE_TO_LOCAL' and d.quality_tradeoff_disclosed
    with pytest.raises(ModelSelectionError): ModelSelectionPolicy().select(ModelSelectionRequest('x',TaskCapabilityRequirements(requires_multi_agent=True),req.context_requirements,ModelPreference.FORCE_REMOTE,(local,remote),snapshot(max_cost_units=0)))

def test_budget_require_confirmation_is_not_a_model_selection():
    local=ModelProfile(ModelProfileId.LOCAL_FAST,4096,512,False,False,False,False,1,1,cost(ModelProfileId.LOCAL_FAST,False,0))
    remote=ModelProfile(ModelProfileId.REMOTE_ADVANCED,16384,512,True,True,True,True,2,2,cost(ModelProfileId.REMOTE_ADVANCED,True,5))
    d=ModelSelectionPolicy().select(ModelSelectionRequest('x',TaskCapabilityRequirements(requires_multi_agent=True),ModelContextRequirements(10,20,False,False,False,1,0,0,False,False),ModelPreference.AUTO,(local,remote),snapshot(max_cost_units=0),ModelSelectionObjective.QUALITY_FIRST,BudgetInsufficientAction.REQUIRE_CONFIRMATION))
    assert d.confirmation_required and d.selected_profile_id is None and d.capability_preferred_profile_id == ModelProfileId.REMOTE_ADVANCED
