"""Daily job: fetch Threads numbers, record them, generate a report, email it.

Resilient by design: any step may fail without aborting the whole run. We
collect failures, still send the best report we can (prefixed with a warning),
and always write a row to the logs sheet.

Run from the repo root:
    python -m src.jobs.run_daily
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import load_settings
from src.clients.claude_client import ClaudeClient
from src.clients.notify_client import NotifyClient, SmtpConfig
from src.clients.sheets_client import SheetsClient
from src.clients.threads_client import ThreadsClient
from src.core.models import DailyMetric, PostMetric
from src.core.upsert import METRICS_DAILY_SHEET, POSTS_SHEET, upsert_daily
from src.utils.logging_setup import setup_logging

logger = logging.getLogger("run_daily")

JST = timezone(timedelta(hours=9))
LOGS_SHEET = "logs"
JOB_NAME = "run_daily"
TOP_POSTS = 3
PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "system_report.md"


def _today_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S%z")


def _collect_posts(threads: ThreadsClient, limit: int = 10) -> list[PostMetric]:
    posts: list[PostMetric] = []
    for item in threads.list_recent_media(limit=limit):
        stats = threads.get_media_insights(item["id"])
        posts.append(
            PostMetric(
                post_id=item["id"],
                posted_at=item.get("timestamp", ""),
                text=item.get("text", ""),
                views=stats.get("views") or 0,
                likes=stats.get("likes") or 0,
            )
        )
    return posts


def _recent_metrics_csv(sheets: SheetsClient, lookback_days: int) -> str:
    rows = sheets.read_rows(METRICS_DAILY_SHEET, "A1:ZZ")
    if not rows:
        return "（履歴なし）"
    header = rows[0]
    date_idx = header.index("date") if "date" in header else 0
    data = [r for r in rows[1:] if r]
    data.sort(key=lambda r: r[date_idx] if len(r) > date_idx else "")
    recent = data[-lookback_days:]
    lines = [",".join(str(c) for c in header)]
    lines.extend(",".join(str(c) for c in row) for row in recent)
    return "\n".join(lines)


def _build_user_content(
    today: str,
    metric: DailyMetric,
    recent_csv: str,
    top_posts: list[PostMetric],
) -> str:
    delta = "" if metric.follower_delta is None else metric.follower_delta
    lines = [
        f"本日の日付: {today}",
        "",
        "## 当日のアカウント数値",
        "followers,views,follower_delta",
        f"{metric.followers},{metric.views},{delta}",
        "",
        "## 直近の推移 (metrics_daily, 古い→新しい)",
        recent_csv,
        "",
        "## 伸びた投稿 TOP3 (views降順)",
        "post_id,views,likes,posted_at,text",
    ]
    if top_posts:
        for p in top_posts:
            text = p.text.replace("\n", " ").replace(",", "、")[:60]
            lines.append(f"{p.post_id},{p.views},{p.likes},{p.posted_at},{text}")
    else:
        lines.append("（投稿データなし）")
    return "\n".join(lines)


def _record_post(sheets: SheetsClient, post: PostMetric) -> None:
    sheets.upsert_row(
        POSTS_SHEET,
        key_col="post_id",
        key_val=post.post_id,
        row_dict={
            "post_id": post.post_id,
            "posted_at": post.posted_at,
            "text": post.text.replace("\n", " "),
            "views": post.views,
            "likes": post.likes,
        },
    )


def _record_log(sheets: SheetsClient, status: str, count: int, message: str) -> None:
    sheets.append_row(LOGS_SHEET, [_now_iso(), JOB_NAME, status, count, message])


def run() -> int:
    setup_logging()
    failures: list[str] = []

    try:
        settings = load_settings()
    except Exception as exc:
        logger.error("Failed to load settings: %s", exc)
        return 1

    today = _today_jst()

    # --- Sheets (optional — report can still be emailed without it) ---
    sheets: SheetsClient | None = None
    try:
        sheets = SheetsClient(settings.google_sa_json, settings.spreadsheet_id)
    except Exception as exc:
        logger.error("Sheets init failed: %s", exc)
        failures.append(f"Sheets初期化: {exc}")

    # --- Threads: account numbers + recent post insights ---
    followers: int | None = None
    views: int | None = None
    posts: list[PostMetric] = []
    try:
        with ThreadsClient(settings.threads_access_token, user_id="me") as threads:
            insights = threads.get_account_insights()
            followers, views = insights.followers, insights.views
            if followers is None or views is None:
                failures.append("アカウント数値の一部が取得できませんでした")
            try:
                posts = _collect_posts(threads)
            except Exception as exc:
                logger.error("Post collection failed: %s", exc)
                failures.append(f"投稿インサイト取得: {exc}")
    except Exception as exc:
        logger.error("Threads account insights failed: %s", exc)
        failures.append(f"Threads取得: {exc}")

    top_posts = sorted(posts, key=lambda p: p.views, reverse=True)[:TOP_POSTS]

    # --- Record into Sheets ---
    written_metric: DailyMetric | None = None
    if sheets is not None and followers is not None and views is not None:
        try:
            written_metric = upsert_daily(
                sheets, DailyMetric(date=today, followers=followers, views=views)
            )
        except Exception as exc:
            logger.error("upsert_daily failed: %s", exc)
            failures.append(f"metrics_daily書き込み: {exc}")
        for p in posts:
            try:
                _record_post(sheets, p)
            except Exception as exc:
                logger.error("posts write failed for %s: %s", p.post_id, exc)
                failures.append(f"posts書き込み({p.post_id}): {exc}")
    elif sheets is not None:
        failures.append("数値未取得のためmetrics_daily書き込みをスキップ")

    # --- Build report context ---
    report_metric = written_metric or DailyMetric(
        date=today, followers=followers or 0, views=views or 0
    )
    recent_csv = "（Sheets未取得）"
    if sheets is not None:
        try:
            recent_csv = _recent_metrics_csv(sheets, settings.report_lookback_days)
        except Exception as exc:
            logger.error("recent metrics read failed: %s", exc)
            failures.append(f"推移読み込み: {exc}")

    user_content = _build_user_content(today, report_metric, recent_csv, top_posts)

    # --- Claude: 所感 + 今日の一手 ---
    try:
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        claude = ClaudeClient(settings.anthropic_api_key, settings.claude_model)
        report_body = claude.generate(system_prompt, user_content)
    except Exception as exc:
        logger.error("Claude generation failed: %s", exc)
        failures.append(f"Claude生成: {exc}")
        report_body = "（所感の生成に失敗しました。取得した数値のみお届けします。）\n\n" + user_content

    # --- Email ---
    subject = f"[Threads日報] {today}"
    prefix = ""
    if failures:
        subject = "⚠️ " + subject
        prefix = "⚠️取得失敗あり\n" + "\n".join(f"- {f}" for f in failures) + "\n\n"
    body = prefix + report_body

    mail_ok = False
    try:
        notify = NotifyClient(
            SmtpConfig(
                host=settings.smtp_host,
                port=settings.smtp_port,
                user=settings.smtp_user,
                password=settings.smtp_pass,
                mail_to=settings.mail_to,
            )
        )
        notify.notify(subject, body)
        mail_ok = True
    except Exception as exc:
        logger.error("notify failed: %s", exc)
        failures.append(f"メール送信: {exc}")

    # --- Always log the outcome ---
    status = "ok" if not failures else "partial"
    message = "; ".join(failures) if failures else "completed"
    if sheets is not None:
        try:
            _record_log(sheets, status=status, count=len(posts), message=message)
        except Exception as exc:
            logger.error("logs write failed: %s", exc)

    logger.info(
        "Run finished status=%s failures=%d mail_ok=%s", status, len(failures), mail_ok
    )
    # Report delivered -> success exit even with partial fetch failures.
    return 0 if mail_ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
