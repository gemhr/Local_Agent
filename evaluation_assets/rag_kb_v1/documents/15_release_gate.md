# Evaluation Release Gate

## Regression
Candidate 与 Baseline 比较必须使用相同 DatasetVersion、SuiteVersion 和 frozen evaluator provenance。

## Inconclusive
缺少 required result、evaluator error 或 evidence 不完整时，默认 policy 产生 INCONCLUSIVE，而不是把未知当 PASS。

## Scope
低 Recall 或高 latency 本身不会让 Baseline 失败。可信测量应如实保存差结果，不能为了漂亮数字调整阈值。
