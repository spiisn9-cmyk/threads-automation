"""F4: daily, learning-aware generation of post-draft candidates.

Each daily run asks Claude (claude-sonnet-4-6) for DAILY_DRAFT_COUNT candidate
drafts for the NEXT day, steered by the learning loop:
  - recent learnings (effective vs. avoid),
  - post ratings (lean to good, avoid bad),
  - references (learn structure only — no copying),
  - notes (小言 = the actual content material).
Growth metrics (followers/views) are deliberately NOT fed in — those stay in
the private morning report (F2). Drafts are written to post_queue with
status=draft, scheduled for the next day within the publish window (times
spread out). A human edits/approves later.

Run from the repo root:
    python -m src.jobs.generate_drafts
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from config.settings import (
    DAILY_DRAFT_COUNT,
    DRAFT_TOP_POSTS,
    JST,
    POST_WINDOW_END_HOUR,
    POST_WINDOW_START_HOUR,
    load_settings,
)
from collections import Counter

from src.core.learnings import Learning, read_recent_learnings
from src.core.notes import NewNote, mark_note_used, read_new_notes
from src.core.references import Reference, read_active_references
from src.core.tags import normalize_tags, split_tags
from src.core.queue import (
    POST_QUEUE_HEADER,
    POST_QUEUE_SHEET,
    STATUS_DRAFT,
    rows_to_dicts,
)
from src.core.upsert import POSTS_SHEET
from src.utils.logging_setup import setup_logging

logger = logging.getLogger("generate_drafts")

JOB_NAME = "generate_drafts"
LOGS_SHEET = "logs"
MAX_DRAFT_CHARS = 500
PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "post_drafts.md"


class SheetsLike(Protocol):
    def read_rows(self, sheet: str, a1: str) -> list[list[Any]]: ...

    def append_row(self, sheet: str, row: list[Any]) -> None: ...

    def update_row(self, sheet: str, a1: str, row: list[Any]) -> None: ...


class ClaudeLike(Protocol):
    def generate(self, system_prompt: str, user_content: str) -> str: ...


def _top_posts(sheets: SheetsLike, limit: int) -> list[dict[str, Any]]:
    rows = sheets.read_rows(POSTS_SHEET, "A1:ZZ")
    posts = rows_to_dicts(rows)

    def views_of(p: dict[str, Any]) -> int:
        try:
            return int(p.get("views") or 0)
        except (TypeError, ValueError):
            return 0

    posts.sort(key=views_of, reverse=True)
    return posts[:limit]


def _rated_posts(sheets: SheetsLike, limit: int = 5) -> tuple[list[str], list[str]]:
    """Return (good_texts, bad_texts) from human ratings on the posts sheet."""
    good: list[str] = []
    bad: list[str] = []
    for p in rows_to_dicts(sheets.read_rows(POSTS_SHEET, "A1:ZZ")):
        rating = str(p.get("rating", "")).strip().lower()
        text = str(p.get("text", "")).replace("\n", " ")[:80]
        if not text:
            continue
        if rating == "good":
            good.append(text)
        elif rating == "bad":
            bad.append(text)
    return good[:limit], bad[:limit]


def _feedback_signals(sheets: SheetsLike, limit: int = 5) -> dict[str, Any]:
    """Aggregate technique tags + one-line feedback from posts AND post_queue.

    good rows -> techniques to lean toward; bad rows -> points to reinforce/avoid.
    """
    good_tags: Counter = Counter()
    bad_tags: Counter = Counter()
    good_ex: list[tuple[str, list[str], str]] = []
    bad_ex: list[tuple[str, list[str], str]] = []

    rows = rows_to_dicts(sheets.read_rows(POSTS_SHEET, "A1:ZZ")) + rows_to_dicts(
        sheets.read_rows(POST_QUEUE_SHEET, "A1:ZZ")
    )
    for r in rows:
        rating = str(r.get("rating", "")).strip().lower()
        tags = normalize_tags(split_tags(r.get("tags", "")))
        text = str(r.get("text", "")).replace("\n", " ")[:60]
        fb = str(r.get("feedback", "")).strip()
        if rating == "good":
            good_tags.update(tags)
            if text or fb:
                good_ex.append((text, tags, fb))
        elif rating == "bad":
            bad_tags.update(tags)
            if text or fb:
                bad_ex.append((text, tags, fb))

    return {
        "good_tags": [t for t, _ in good_tags.most_common()],
        "bad_tags": [t for t, _ in bad_tags.most_common()],
        "good_examples": good_ex[:limit],
        "bad_examples": bad_ex[:limit],
    }


def _build_user_content(
    top_posts: list[dict[str, Any]],
    count: int,
    notes_to_use: list[NewNote],
    references: list[Reference],
    learnings: list[Learning],
    good_posts: list[str],
    bad_posts: list[str],
    feedback: dict[str, Any],
) -> str:
    lines = [f"以下を参考に、投稿の下書きを{count}本作ってください。", ""]

    # Growth metrics (followers/views) are deliberately NOT provided here — they
    # belong to the private morning report (F2), not to public post drafts.
    lines.append("## 方針（必ず守る）")
    lines.append(
        "- 公開投稿にはフォロワー数・views等の数値（成長指標）を出さない。"
        "うに自身が下の小言に数値や節目を書いている場合のみ、それを尊重して使う。"
    )
    lines.append(
        "- 数字や成長アピールを前面に出さず、挑戦の過程・気づき・本音を共有する姿勢で書く。"
    )
    lines.append("")

    n = len(notes_to_use)
    if n:
        lines.append("## 最優先：今日の小言（これを土台に整える）")
        lines.append(
            "各小言の温度感・言い回しを活かして「うに文体」に整える程度にとどめる。"
            "小言に書かれていない数字・出来事・エピソードは創作しない（盛らない）。"
        )
        for i, note in enumerate(notes_to_use, start=1):
            theme = note.theme or "（テーマ指定なし）"
            body = note.note.replace("\n", " ")
            lines.append(f"{i}. [theme={theme}] {body}")
        lines.append("")
        lines.append(
            f"→ 最初の{n}本は上の小言1〜{n}をそれぞれ土台に整える。"
            f"残り{count - n}本は、5つの柱から事実を必要としない一般的な学び・考え・"
            "お役立ちで補完（数値以外の具体的な事実は創作しない）。"
        )
    else:
        lines.append("## 小言なし")
        lines.append(
            f"未使用の小言はありません。{count}本すべてを5つの柱から、"
            "事実を必要としない一般的な学び・考え・お役立ちで作成する"
            "（上の数値以外の具体的な事実は創作しない）。"
        )
    lines.append("")

    if learnings:
        lines.append("## これまでの学び（効く型に寄せ、避ける型を避ける）")
        for ln in learnings:
            ev = f"（根拠: {ln.evidence}）" if ln.evidence else ""
            lines.append(f"- {ln.learning}{ev}")
        lines.append("")

    if good_posts or bad_posts:
        lines.append("## 評価フィードバック（rating）")
        if good_posts:
            lines.append("good評価・伸びた型 → こういう型・切り口に寄せる：")
            for t in good_posts:
                lines.append(f"  ◎ {t}")
        if bad_posts:
            lines.append("bad評価・低反応の型 → こういう型は避ける（繰り返さない）：")
            for t in bad_posts:
                lines.append(f"  ✕ {t}")
        lines.append("")

    good_tags = feedback.get("good_tags") or []
    bad_tags = feedback.get("bad_tags") or []
    good_ex = feedback.get("good_examples") or []
    bad_ex = feedback.get("bad_examples") or []
    if good_tags or bad_tags or good_ex or bad_ex:
        lines.append("## 技法フィードバック（タグ＋一言。学習して反映）")
        if good_tags:
            lines.append(
                "good/伸びた投稿でよく使われた技法 → これらの型に寄せる: "
                + ", ".join(good_tags)
            )
        if bad_tags:
            lines.append(
                "bad/弱いとされた投稿の技法・指摘 → 補強または回避する: "
                + ", ".join(bad_tags)
            )
        for text, tags, fb in good_ex:
            tagstr = " / ".join(tags) if tags else "-"
            note = f" / 一言: {fb}" if fb else ""
            lines.append(f"  ◎ [{tagstr}] {text}{note}")
        for text, tags, fb in bad_ex:
            tagstr = " / ".join(tags) if tags else "-"
            note = f" / 一言: {fb}" if fb else ""
            lines.append(f"  ✕ [{tagstr}] {text}{note}")
        lines.append("")

    lines.append("## 反応が良かった投稿（内容の傾向の参考。数値は出さない）")
    if top_posts:
        for p in top_posts:
            text = str(p.get("text", "")).replace("\n", " ")[:80]
            lines.append(f"- {text}")
    else:
        lines.append("（まだ投稿データがありません）")
    lines.append("")

    if references:
        lines.append("## 参考資料（型のお手本。伸びている投稿の“構成”だけ学ぶ）")
        lines.append(
            "下は伸びている投稿の例。型・構成・書き出し・切り口・問いかけ方を学ぶために使う。"
            "本文・トピック・具体的な表現は丸写ししない（パクリ・重複を避ける）。"
            "あくまでうに自身の内容（上の小言／5つの柱）を、学んだ型で書くこと。"
        )
        for i, ref in enumerate(references, start=1):
            sample = ref.text.replace("\n", " ")[:120]
            meta = f"source={ref.source or '不明'}"
            if ref.impressions:
                meta += f", impressions≒{ref.impressions}"
            learn = f" / 学ぶ点: {ref.learn}" if ref.learn else ""
            lines.append(f"{i}. ({meta}) 例: 「{sample}」{learn}")
        lines.append("")

    lines.append(
        f"ちょうど{count}本。各本にtheme（5つの柱のいずれか）を付け、"
        f"本文は日本語{MAX_DRAFT_CHARS}文字以内。"
        "全部を同じ長さに揃えず、普段は短め中心、1本程度は熱量のある長文を混ぜる。"
    )
    lines.append(
        "改善方針: good・伸びた投稿でよく使われた技法に寄せ、badや一言で指摘された点は補強・回避する。"
        "低反応だった型も避ける。"
        "ただし小言の事実は創作しない／参考・型は丸写ししない／数値(フォロワー・views)は投稿に出さない。"
    )
    return "\n".join(lines)


def parse_drafts(raw: str, expected: int) -> list[tuple[str, str]]:
    """Parse the model's JSON array into (theme, text) tuples, defensively."""
    text = raw.strip()
    if text.startswith("```"):
        # strip an optional ```json ... ``` fence
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse drafts JSON: %s | raw=%r", exc, raw[:500])
        raise RuntimeError("Claude did not return valid JSON drafts") from exc

    if not isinstance(data, list):
        raise RuntimeError("Drafts JSON is not a list")

    drafts: list[tuple[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        body = str(item.get("text", "")).strip()
        if not body:
            continue
        theme = str(item.get("theme", "")).strip() or "未分類"
        drafts.append((theme, body[:MAX_DRAFT_CHARS]))

    if not drafts:
        raise RuntimeError("No usable drafts found in Claude response")
    if len(drafts) != expected:
        logger.warning("Expected %d drafts, parsed %d", expected, len(drafts))
    return drafts


def build_queue_rows(
    drafts: list[tuple[str, str]],
    now: datetime,
    count: int,
) -> list[list[Any]]:
    """Build post_queue rows (header order) for tomorrow as same-day candidates.

    Daily run: all `count` drafts are candidates for the NEXT day, each at a
    randomized time within the window [POST_WINDOW_START_HOUR,
    POST_WINDOW_END_HOUR) (JST) with a random minute, so times are spread out
    and never land on the same hour:minute. The human approves the ones to post
    (the publish guard caps how many actually go out per day); the rest can be
    edited or left. queue_id is unique per run via a timestamp prefix + sequence.
    """
    stamp = now.astimezone(JST).strftime("%Y%m%d%H%M")
    next_day = now.astimezone(JST).date() + timedelta(days=1)
    # Last hour that still leaves room before the window closes.
    last_hour = max(POST_WINDOW_START_HOUR, POST_WINDOW_END_HOUR - 1)
    rows: list[list[Any]] = []
    for i, (theme, body) in enumerate(drafts[:count]):
        hour = random.randint(POST_WINDOW_START_HOUR, last_hour)
        minute = random.randint(0, 59)
        scheduled = datetime.combine(
            next_day,
            time(hour=hour, minute=minute),
            tzinfo=JST,
        )
        scheduled_at = scheduled.strftime("%Y-%m-%dT%H:%M:%S%z")
        queue_id = f"q{stamp}-{i + 1:02d}"
        row_dict = {
            "queue_id": queue_id,
            "scheduled_at": scheduled_at,
            "text": body,
            "theme": theme,
            "status": STATUS_DRAFT,
            "posted_post_id": "",
            "posted_at": "",
            "tags": "",
            "rating": "",
            "feedback": "",
        }
        # Default any columns not set here (forward-compatible with new columns).
        rows.append([row_dict.get(col, "") for col in POST_QUEUE_HEADER])
    return rows


def generate(
    sheets: SheetsLike,
    claude: ClaudeLike,
    system_prompt: str,
    now: datetime,
    count: int = DAILY_DRAFT_COUNT,
    top_posts_limit: int = DRAFT_TOP_POSTS,
) -> list[list[Any]]:
    """Core (testable) flow: gather learning materials -> generate -> write."""
    new_notes = read_new_notes(sheets)
    notes_to_use = new_notes[:count]  # unused notes get top priority
    top_posts = _top_posts(sheets, top_posts_limit)
    references = read_active_references(sheets)  # swipe-file: learn structure only
    learnings = read_recent_learnings(sheets)  # steer toward what works
    good_posts, bad_posts = _rated_posts(sheets)  # lean to good, avoid bad
    feedback = _feedback_signals(sheets)  # technique tags + one-line notes
    # Growth metrics are intentionally not read here — see _build_user_content.
    user_content = _build_user_content(
        top_posts, count, notes_to_use, references, learnings,
        good_posts, bad_posts, feedback,
    )

    raw = claude.generate(system_prompt, user_content)
    drafts = parse_drafts(raw, count)
    rows = build_queue_rows(drafts, now, count)

    for row in rows:
        sheets.append_row(POST_QUEUE_SHEET, row)
    logger.info("Wrote %d draft(s) to %s", len(rows), POST_QUEUE_SHEET)

    # Mark the notes we used (the prioritized slice) as used.
    for note in notes_to_use:
        try:
            mark_note_used(sheets, note)
        except Exception as exc:
            logger.error("Failed to mark note row %d used: %s", note.row_index, exc)
    if notes_to_use:
        logger.info("Marked %d note(s) as used", len(notes_to_use))
    return rows


def _record_log(sheets: SheetsLike, status: str, count: int, message: str) -> None:
    now_iso = datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z")
    sheets.append_row(LOGS_SHEET, [now_iso, JOB_NAME, status, count, message])  # type: ignore[attr-defined]


def run() -> int:
    setup_logging()
    # Imported lazily so the testable core (generate/parse) doesn't require the
    # anthropic / google SDKs to be installed.
    from src.clients.claude_client import ClaudeClient
    from src.clients.sheets_client import SheetsClient

    try:
        settings = load_settings()
    except Exception as exc:
        logger.error("Failed to load settings: %s", exc)
        return 1

    try:
        sheets = SheetsClient(settings.google_sa_json, settings.spreadsheet_id)
    except Exception as exc:
        logger.error("Sheets init failed: %s", exc)
        return 1

    try:
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        claude = ClaudeClient(settings.anthropic_api_key, settings.claude_model)
        rows = generate(sheets, claude, system_prompt, datetime.now(JST))
    except Exception as exc:
        logger.error("Draft generation failed: %s", exc)
        try:
            _record_log(sheets, status="failed", count=0, message=str(exc))
        except Exception as log_exc:
            logger.error("logs write failed: %s", log_exc)
        return 1

    try:
        _record_log(sheets, status="ok", count=len(rows), message="drafts generated")
    except Exception as exc:
        logger.error("logs write failed: %s", exc)

    logger.info("generate_drafts finished: %d drafts", len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
