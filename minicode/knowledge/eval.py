"""检索质量评测：MRR / Hit@K / Recall@K。

数据集格式（JSONL），每行一个样本：

    {"query": "how does auth work", "relevant_chunks": ["<chunk_id>", ...]}

命令行：

    python -m minicode.knowledge.eval --index default --dataset eval.jsonl

也可在测试里直接调用 ``evaluate(dataset, index_name=...)``。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minicode.knowledge import pipeline


@dataclass
class EvalSample:
    query: str
    relevant_chunks: set[str]


@dataclass
class EvalReport:
    """评测汇总结果。"""

    num_samples: int = 0
    mrr: float = 0.0
    hit_at: dict[int, float] = field(default_factory=dict)
    recall_at: dict[int, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "num_samples": self.num_samples,
            "mrr": round(self.mrr, 4),
            "hit_at": {k: round(v, 4) for k, v in self.hit_at.items()},
            "recall_at": {k: round(v, 4) for k, v in self.recall_at.items()},
        }

    def format(self) -> str:
        lines = [
            f"Samples : {self.num_samples}",
            f"MRR     : {self.mrr:.4f}",
        ]
        for k in sorted(self.hit_at):
            lines.append(f"Hit@{k:<4}: {self.hit_at[k]:.4f}")
        for k in sorted(self.recall_at):
            lines.append(f"Recall@{k:<2}: {self.recall_at[k]:.4f}")
        return "\n".join(lines)


def load_dataset(path: str | Path) -> list[EvalSample]:
    """从 JSONL 文件加载评测数据集。"""
    samples: list[EvalSample] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        samples.append(
            EvalSample(
                query=obj["query"],
                relevant_chunks=set(obj.get("relevant_chunks", [])),
            )
        )
    return samples


def evaluate(
    dataset: list[EvalSample],
    index_name: str = "default",
    *,
    scope: str | None = None,
    cwd: str | Path | None = None,
    ks: tuple[int, ...] = (1, 3, 5, 10),
) -> EvalReport:
    """在给定数据集上评测检索质量。

    参数：
        dataset: 评测样本列表
        index_name: 索引名
        ks: 计算 Hit@K / Recall@K 的 K 值

    返回：
        ``EvalReport``。
    """
    report = EvalReport(num_samples=len(dataset))
    if not dataset:
        return report

    max_k = max(ks) if ks else 10
    reciprocal_ranks: list[float] = []
    hit_counts = {k: 0 for k in ks}
    recall_sums = {k: 0.0 for k in ks}

    for sample in dataset:
        result = pipeline.query(
            sample.query, index_name=index_name, top_k=max_k, scope=scope, cwd=cwd
        )
        retrieved_ids = [c.id for c in result.chunks]
        relevant = sample.relevant_chunks

        # MRR：第一个命中的相关文档排名
        rr = 0.0
        for rank, cid in enumerate(retrieved_ids, start=1):
            if cid in relevant:
                rr = 1.0 / rank
                break
        reciprocal_ranks.append(rr)

        for k in ks:
            top_k_ids = set(retrieved_ids[:k])
            if top_k_ids & relevant:
                hit_counts[k] += 1
            if relevant:
                recall_sums[k] += len(top_k_ids & relevant) / len(relevant)

    n = len(dataset)
    report.mrr = sum(reciprocal_ranks) / n
    report.hit_at = {k: hit_counts[k] / n for k in ks}
    report.recall_at = {k: recall_sums[k] / n for k in ks}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate knowledge base retrieval quality.")
    parser.add_argument("--index", default="default", help="Index name")
    parser.add_argument("--dataset", required=True, help="Path to JSONL dataset")
    parser.add_argument("--scope", default=None, choices=["project", "user"], help="Index scope")
    parser.add_argument("--cwd", default=None, help="Workspace root")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args(argv)

    dataset = load_dataset(args.dataset)
    report = evaluate(dataset, index_name=args.index, scope=args.scope, cwd=args.cwd)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(report.format())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
