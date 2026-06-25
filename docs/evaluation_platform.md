# Local Agent 项目 1：自动评估与回归测试平台（MVP）

本文档定义离线评估平台的首版实现，覆盖样本定义、批量运行、指标计算、报告输出。

## 1. 数据集 Schema（JSONL）

每一行一条样本，字段如下：

- `sample_id`: 样本 ID（唯一）
- `category`: 任务类别（如 `rag/tool/router/general`）
- `user_query`: 用户问题
- `expected_citations`: 期望引用来源列表
- `expected_tools`: 期望工具列表
- `expected_agent`: 期望路由的 agent
- `reference_answer`: 参考答案（用于回答可用性粗判）
- `tags`: 标签

示例见：`data/eval/samples.jsonl`。

## 2. 批跑器

入口脚本：`scripts/run_eval.py`

支持两种运行模式：

1. **API 实时评测**：通过 `/api/chat` 调用被测版本
2. **Replay 回放评测**：读取历史运行输出 JSONL，比对指标（适合离线复现）

## 3. 核心指标

MVP 已实现以下指标：

- `Recall@K`
- `引用命中率`
- `Router 正确率`
- `Tool Call 正确率`
- `最终回答通过率`（规则版）
- `平均响应时间`

> 注：回答通过率当前是规则策略；后续可引入 LLM-as-a-judge + 人工抽检。

## 4. 报告输出

输出目录默认：`data/eval/runs/`

- `{variant}_records.jsonl`: 每条样本的运行结果
- `report_{baseline}_vs_{candidate}.md`: 版本对比报告（含 bad cases）

## 5. 快速开始

### 5.1 使用回放文件跑评估（推荐先验证流程）

```bash
python scripts/run_eval.py \
  --dataset data/eval/samples.jsonl \
  --baseline-replay data/eval/mock_baseline.jsonl \
  --candidate-replay data/eval/mock_candidate.jsonl \
  --baseline-name baseline \
  --candidate-name candidate
```

### 5.2 对接本地服务进行实时评测

```bash
python scripts/run_eval.py \
  --dataset data/eval/samples.jsonl \
  --baseline-api http://127.0.0.1:8000 \
  --candidate-api http://127.0.0.1:8001
```

> 如要获得更准确的工具/引用/路由评估，请在 Agent 输出中附带 `[[EVAL_META]]{...}` 结构化元信息。

## 6. 两周目标对应关系

- 第 1 周：样本 schema + runner + 基础指标 + 初版测试集 ✅
- 第 2 周：接入真实版本对比 + 自动报告 + bad case 归因分析（进行中）
