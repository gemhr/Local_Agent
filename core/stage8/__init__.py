"""Stage8 Agentic Test Engineering Workflow 的最小业务层。"""

from core.stage8.domain import (
    BusinessReview,
    ReviewStatus,
    ReviewType,
    FeatureTestMission,
    MissionRunReference,
    MissionStatus,
    ExternalExecutionStatus, ExecutionPlan, ExternalExecutionJob, ExecutionResult,
    FailureEvidencePackage,
    GeneratedCaseArtifact,
)
from core.stage8.service import (
    BusinessReviewService,
    MissionService,
    Stage8ConflictError,
    Stage8NotFoundError,
    Stage8ValidationError,
    TestPlanRepository,
)
from core.stage8.specialists import (
    EvidenceSourceType, EvidenceRef, FeatureContext, FeatureUnderstandingRequest,
    FeatureUnderstandingResult, RiskAnalysisRequest, RiskAnalysisResult, RiskItem,
    TestPlanningRequest, TestPlanResult, TestScenario, SpecialistAgentApplicationService,
)
from core.stage8.execution import (
    FailureClassification, FailureTriageRequest, FailureTriageResult,
    FailureTriageService, Stage8ExecutionService, GovernedToolInvoker,
)
from core.stage8.case_generation import CaseGenerationApplicationService
from core.stage8.ci_guardian import (
    CIRunStatus, ChangeType, CIExecutionResult, CIRun, ChangeEvent,
    FailureCluster, HistoricalComparison, ChangeCorrelationCandidate,
    CIGuardianRequest, CIGuardianFinding, CIGuardianResult,
    CIGuardianApplicationService, cluster_failures, compare_history, correlate_changes,
)

__all__ = [
    "BusinessReview", "ReviewStatus", "ReviewType", "FeatureTestMission",
    "MissionRunReference", "MissionStatus", "BusinessReviewService",
    "ExternalExecutionStatus", "ExecutionPlan", "ExternalExecutionJob", "ExecutionResult", "FailureEvidencePackage",
    "GeneratedCaseArtifact", "CaseGenerationApplicationService",
    "MissionService", "Stage8ConflictError", "Stage8NotFoundError",
    "Stage8ValidationError",
    "TestPlanRepository",
    "EvidenceSourceType", "EvidenceRef", "FeatureContext", "FeatureUnderstandingRequest",
    "FeatureUnderstandingResult", "RiskAnalysisRequest", "RiskAnalysisResult", "RiskItem",
    "TestPlanningRequest", "TestPlanResult", "TestScenario", "SpecialistAgentApplicationService",
    "FailureClassification", "FailureTriageRequest", "FailureTriageResult",
    "FailureTriageService", "Stage8ExecutionService", "GovernedToolInvoker",
    "CIRunStatus", "ChangeType", "CIExecutionResult", "CIRun", "ChangeEvent",
    "FailureCluster", "HistoricalComparison", "ChangeCorrelationCandidate",
    "CIGuardianRequest", "CIGuardianFinding", "CIGuardianResult",
    "CIGuardianApplicationService", "cluster_failures", "compare_history", "correlate_changes",
]
