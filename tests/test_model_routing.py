import unittest

from core.runtime import (
    BudgetLedger,
    ModelCircuitBreakerRegistry,
    ModelContextRequirements,
    ModelCostProfile,
    ModelFailureCategory,
    ModelPreference,
    ModelProfile,
    ModelProfileId,
    ModelRoutingPolicy,
    ModelSelectionDecision,
    ModelSelectionObjective,
    ModelSelectionReason,
    RiskLevel,
    RoutingAdjustment,
    RunBudget,
    TaskCapabilityRequirements,
)


def profile(
    profile_id: ModelProfileId,
    *,
    remote: bool,
    window: int = 8192,
    tools: bool = True,
    quality: int = 1,
) -> ModelProfile:
    return ModelProfile(
        profile_id,
        window,
        256,
        tools,
        True,
        True,
        True,
        quality,
        quality,
        ModelCostProfile(profile_id, remote, 1, 1, 1, 100),
        remote,
        f"breaker:{profile_id.value}",
    )


def requirements(window: int = 1024) -> ModelContextRequirements:
    return ModelContextRequirements(
        256, window, False, False, False, 2, 0, 0, False, False
    )


def decision(selected: ModelProfileId) -> ModelSelectionDecision:
    return ModelSelectionDecision(
        selected,
        ModelSelectionReason.LOCAL_SUFFICIENT,
        "安全说明",
        ("rule",),
        False,
        ModelProfileId.REMOTE_ADVANCED,
        selected,
        ModelSelectionObjective.QUALITY_FIRST,
    )


class ModelRoutingPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.local = profile(ModelProfileId.LOCAL_FAST, remote=False, quality=1)
        self.remote = profile(
            ModelProfileId.REMOTE_ADVANCED, remote=True, quality=2
        )
        self.capabilities = TaskCapabilityRequirements(risk_level=RiskLevel.LOW)

    def route(self, **kwargs):
        policy = kwargs.pop("policy", ModelRoutingPolicy())
        return policy.route(
            selection_decision=kwargs.pop(
                "selection_decision", decision(ModelProfileId.LOCAL_FAST)
            ),
            capability_requirements=kwargs.pop(
                "capability_requirements", self.capabilities
            ),
            context_requirements=kwargs.pop(
                "context_requirements", requirements()
            ),
            profiles=kwargs.pop("profiles", (self.local, self.remote)),
            preference=kwargs.pop("preference", ModelPreference.AUTO),
            budget_snapshot=kwargs.pop(
                "budget_snapshot", BudgetLedger(RunBudget()).snapshot()
            ),
            breaker_snapshots=kwargs.pop(
                "breaker_snapshots",
                ModelCircuitBreakerRegistry().snapshots(
                    (
                        self.local.effective_breaker_key,
                        self.remote.effective_breaker_key,
                    )
                ),
            ),
            **kwargs,
        )

    def test_three_profile_identities_and_stable_deduplicated_chain(self) -> None:
        result = self.route()
        self.assertEqual(
            result.capability_preferred_profile_id,
            ModelProfileId.REMOTE_ADVANCED,
        )
        self.assertEqual(
            result.initial_selected_profile_id, ModelProfileId.LOCAL_FAST
        )
        self.assertEqual(
            [item.profile_id for item in result.candidates],
            [ModelProfileId.LOCAL_FAST, ModelProfileId.REMOTE_ADVANCED],
        )
        self.assertEqual(len(set(item.profile_id for item in result.candidates)), 2)

    def test_hard_capability_and_context_filter(self) -> None:
        weak_local = profile(
            ModelProfileId.LOCAL_FAST,
            remote=False,
            window=512,
            tools=False,
        )
        result = self.route(
            profiles=(weak_local, self.remote),
            selection_decision=decision(ModelProfileId.REMOTE_ADVANCED),
            capability_requirements=TaskCapabilityRequirements(requires_tools=True),
            context_requirements=requirements(2048),
        )
        self.assertEqual(
            [item.profile_id for item in result.candidates],
            [ModelProfileId.REMOTE_ADVANCED],
        )

    def test_force_local_and_force_remote_cannot_be_bypassed(self) -> None:
        local_only = self.route(preference=ModelPreference.FORCE_LOCAL)
        self.assertEqual(
            [item.profile_id for item in local_only.candidates],
            [ModelProfileId.LOCAL_FAST],
        )
        remote_only = self.route(
            preference=ModelPreference.FORCE_REMOTE,
            selection_decision=decision(ModelProfileId.REMOTE_ADVANCED),
        )
        self.assertEqual(
            [item.profile_id for item in remote_only.candidates],
            [ModelProfileId.REMOTE_ADVANCED],
        )

    def test_auto_escalation_and_downgrade_have_explicit_adjustments(self) -> None:
        escalation = self.route()
        self.assertEqual(
            escalation.candidates[1].adjustment,
            RoutingAdjustment.ESCALATE_TO_REMOTE,
        )
        downgrade = self.route(
            selection_decision=decision(ModelProfileId.REMOTE_ADVANCED)
        )
        self.assertEqual(
            downgrade.candidates[1].adjustment,
            RoutingAdjustment.DOWNGRADE_TO_LOCAL,
        )
        self.assertTrue(downgrade.quality_tradeoff_disclosed)

    def test_downgrade_can_require_confirmation_without_candidates(self) -> None:
        result = self.route(
            policy=ModelRoutingPolicy(require_confirmation_for_downgrade=True),
            selection_decision=decision(ModelProfileId.REMOTE_ADVANCED),
        )
        self.assertTrue(result.confirmation_required)
        self.assertEqual(result.candidates, ())

    def test_fallback_taxonomy_is_conservative(self) -> None:
        self.assertTrue(
            ModelRoutingPolicy.can_fallback(
                ModelFailureCategory.PROVIDER_TIMEOUT,
                failed_profile=self.local,
                next_profile=self.remote,
                output_started=False,
            )
        )
        for category in (
            ModelFailureCategory.SAFETY_REFUSAL,
            ModelFailureCategory.INVALID_REQUEST,
            ModelFailureCategory.CANCELLED,
            ModelFailureCategory.DEADLINE_EXCEEDED,
            ModelFailureCategory.BUDGET_EXHAUSTED,
            ModelFailureCategory.UNKNOWN_FAILURE,
        ):
            self.assertFalse(
                ModelRoutingPolicy.can_fallback(
                    category,
                    failed_profile=self.local,
                    next_profile=self.remote,
                    output_started=False,
                )
            )
        self.assertFalse(
            ModelRoutingPolicy.can_fallback(
                ModelFailureCategory.PROVIDER_TIMEOUT,
                failed_profile=self.local,
                next_profile=self.remote,
                output_started=True,
            )
        )

    def test_context_overflow_only_switches_to_larger_window(self) -> None:
        smaller = profile(
            ModelProfileId.REMOTE_ADVANCED, remote=True, window=4096
        )
        self.assertFalse(
            ModelRoutingPolicy.can_fallback(
                ModelFailureCategory.CONTEXT_LIMIT_EXCEEDED,
                failed_profile=self.local,
                next_profile=smaller,
                output_started=False,
            )
        )
        larger = profile(
            ModelProfileId.REMOTE_ADVANCED, remote=True, window=16384
        )
        self.assertTrue(
            ModelRoutingPolicy.can_fallback(
                ModelFailureCategory.CONTEXT_LIMIT_EXCEEDED,
                failed_profile=self.local,
                next_profile=larger,
                output_started=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
