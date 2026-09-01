# Taste
- Communicates with the agent in Chinese and requires Chinese-language commit messages and push summaries ("中文摘要"). Confidence: 0.95
- Prefers analysis-first work: explain concepts, failure scenarios, and options without changing code before implementation is authorized ("不要修改代码", "先从概念入手", "大致描述一下可能发生的现象和场景"). Confidence: 0.85
- Wants trade-off and prioritization analysis with a clear recommendation (e.g., which fix is easier vs. which adds more value / 面试加分更大) before committing to an approach. Confidence: 0.8
- Favors minimal-scope, low-risk fixes over deep refactors when options are offered (explicitly chose the "推荐组合" option over "深度修复"). Confidence: 0.7
- Avoids overengineering: match implementation rigor to actual risk and scale rather than adding elaborate machinery (e.g., "Do not overengineer cancellation for an in-memory BM25 operation"). Confidence: 0.7
- Treats this project partly as interview-preparation material: keeps interview docs under docs/interview, wants interview material written to match existing template conventions, and expects those docs to be included in commits/pushes. Confidence: 0.8
