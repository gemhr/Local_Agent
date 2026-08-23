# Evaluation Dataset Versioning

## Dataset identity
DatasetVersion 由 dataset_id 与 version 标识，并保存有序 CaseVersionRef 集合。Baseline 与 Candidate 必须复用相同版本。

## Case identity
TestCaseVersion identity 是 case_id 加 version。修改 Ground Truth 必须 version bump，不能原地覆盖已冻结标签。

## Suite
EvaluationSuiteVersion 冻结 case selection、EvaluatorSpec、EvaluationPolicy 与 target capability requirements。
