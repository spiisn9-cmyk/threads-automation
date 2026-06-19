"""Daily post-performance analysis (pure, testable core).

Given recent posts (with views/likes/rating/feedback), asks Claude to produce:
  - a short report block for the morning email, and
  - 1..3 evidence-backed learnings to store.

The model is told to stay humble when the sample is small.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

MAX_LEARNINGS = 3


@dataclass(frozen=True)
class AnalysisResult:
    report_block: str
    learnings: list[tuple[str, str]]  # (learning, evidence)


class ClaudeLike(Protocol):
    def generate(self, system_prompt: str, user_content: str) -> str: ...


def build_analysis_user(recent_posts: list[dict[str, Any]]) -> str:
    lines = ["以下は直近の自分の投稿の成績です。分析してください。", ""]
    if not recent_posts:
        lines.append("（まだ分析できる投稿がありません。サンプル不足です）")
        return "\n".join(lines)

    lines.append(f"## 直近の投稿（{len(recent_posts)}件）")
    lines.append("posted_at | views | likes | rating | tags | feedback | 本文(先頭)")
    for p in recent_posts:
        text = str(p.get("text", "")).replace("\n", " ")[:60]
        lines.append(
            f"- {p.get('posted_at', '')} | views={p.get('views', '')} | "
            f"likes={p.get('likes', '')} | rating={p.get('rating', '')} | "
            f"tags={p.get('tags', '')} | fb={p.get('feedback', '')} | {text}"
        )
    lines.append("")
    lines.append(
        "伸びた投稿の共通点／伸びなかった共通点／bad評価の投稿の特徴（避けるべき型）を簡潔に。"
        "good傾向の投稿によく付いている技法タグ（tags）の傾向にも触れる。"
        f"学びは最大{MAX_LEARNINGS}個、それぞれevidence（根拠）付きで。"
    )
    return "\n".join(lines)


def parse_analysis(raw: str) -> AnalysisResult:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse analysis JSON: %s | raw=%r", exc, raw[:300])
        raise RuntimeError("分析結果のJSON解析に失敗しました") from exc

    if not isinstance(data, dict):
        raise RuntimeError("分析結果のJSONがオブジェクトではありません")

    report_block = str(data.get("report_block", "")).strip()
    learnings: list[tuple[str, str]] = []
    for item in data.get("learnings", []) or []:
        if not isinstance(item, dict):
            continue
        learning = str(item.get("learning", "")).strip()
        if not learning:
            continue
        evidence = str(item.get("evidence", "")).strip()
        learnings.append((learning, evidence))
        if len(learnings) >= MAX_LEARNINGS:
            break
    return AnalysisResult(report_block=report_block, learnings=learnings)


def analyze(
    claude: ClaudeLike, system_prompt: str, recent_posts: list[dict[str, Any]]
) -> AnalysisResult:
    user_content = build_analysis_user(recent_posts)
    raw = claude.generate(system_prompt, user_content)
    return parse_analysis(raw)
