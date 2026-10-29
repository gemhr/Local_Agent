"""只解释白名单观察；缺少执行证明时不猜测根因。"""


def diagnose(decision, report, *, report_proven, required_evidence_proven, capture_available):
    observations, gaps = [], []
    if decision == 'REJECT':
        observations.append('OBSERVED_OUTCOME_DISAGREEMENT')
    if report_proven and report.get('diagnosis_status') == 'SUPPORTED' and not required_evidence_proven:
        observations.append('STRONG_CONCLUSION_WITHOUT_REQUIRED_EVIDENCE')
    if not report_proven:
        gaps.append('ACTUAL_TOOL_AND_FINAL_BINDING_UNAVAILABLE')
    if not capture_available:
        gaps.append('ACTUAL_CONTEXT_CAPTURE_UNAVAILABLE')
    return dict(observation=observations or ['INCONCLUSIVE'],
        supporting_evidence_refs=['feedback-decision'] if decision == 'REJECT' else [],
        contradicting_evidence_refs=[], candidate_causes=[], affected_component='UNKNOWN',
        uncertainty=gaps or ['HUMAN_BUSINESS_REVIEW_REQUIRED'], verification_status='INCONCLUSIVE',
        recommended_review_action='INDEPENDENT_HUMAN_REVIEW')
