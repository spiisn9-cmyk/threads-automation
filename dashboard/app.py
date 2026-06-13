"""Streamlit management dashboard for the Threads automation tool.

Sheets-only: it never touches the Threads or Anthropic APIs, so it adds no new
secrets beyond GOOGLE_SA_JSON / SPREADSHEET_ID / DASHBOARD_PASSWORD. Run with:

    streamlit run dashboard/app.py
"""
from __future__ import annotations

import logging
import os
import sys

# Make the repo root importable when launched via `streamlit run dashboard/app.py`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import streamlit as st

from dashboard import service
from src.core.queue import STATUS_APPROVED, STATUS_FAILED, STATUS_POSTED
from src.utils.logging_setup import setup_logging

setup_logging()
logger = logging.getLogger("dashboard")

st.set_page_config(page_title="Threads 運用ダッシュボード", page_icon="🧵", layout="wide")

# Number of side-by-side columns for the candidate cards on wide screens.
# Streamlit stacks columns vertically on narrow screens, so this stays readable
# on mobile.
_REVIEW_COLUMNS = 2


def _secret(key: str) -> str | None:
    """Read from Streamlit secrets first, then environment."""
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:  # secrets file may be absent locally
        pass
    return os.environ.get(key)


def _require_password() -> None:
    expected = _secret("DASHBOARD_PASSWORD")
    if not expected:
        st.error("DASHBOARD_PASSWORD が未設定です（secrets/環境変数を確認）。")
        st.stop()
    if st.session_state.get("auth_ok"):
        return
    st.title("🔒 ログイン")
    pw = st.text_input("パスワード", type="password")
    if not pw:
        st.stop()
    if pw != expected:
        st.error("パスワードが違います。")
        st.stop()
    st.session_state["auth_ok"] = True
    st.rerun()


@st.cache_resource
def _get_sheets():
    sa_json = _secret("GOOGLE_SA_JSON")
    spreadsheet_id = _secret("SPREADSHEET_ID")
    if not sa_json or not spreadsheet_id:
        st.error("GOOGLE_SA_JSON / SPREADSHEET_ID が未設定です。")
        st.stop()
    # Imported here so tests/imports don't require google libs at module load.
    from src.clients.sheets_client import SheetsClient

    return SheetsClient(sa_json, spreadsheet_id)


def _run_write(label: str, fn, *args) -> None:
    """Run a write op, surface errors in the UI + logs, and refresh on success."""
    try:
        fn(*args)
    except Exception as exc:
        logger.exception("%s failed", label)
        st.error(f"{label}に失敗しました: {exc}")
        return
    st.success(f"{label}しました")
    st.rerun()


def _sidebar(sheets) -> None:
    st.sidebar.header("📊 数値サマリ")
    try:
        latest = service.latest_metric(sheets)
        if latest:
            st.sidebar.metric("followers", latest.get("followers", "-"))
            st.sidebar.metric("views", latest.get("views", "-"))
            st.sidebar.caption(f"最新: {latest.get('date', '')}")
        else:
            st.sidebar.info("metrics_daily にデータがありません")

        series = service.metrics_series(sheets)
        if series["followers"]:
            st.sidebar.caption("followers 推移")
            st.sidebar.line_chart(series["followers"])
            st.sidebar.caption("views 推移")
            st.sidebar.line_chart(series["views"])
    except Exception as exc:
        logger.exception("sidebar metrics failed")
        st.sidebar.error(f"数値の取得に失敗: {exc}")

    if st.sidebar.button("🔄 更新"):
        st.rerun()


def _draft_card(sheets, d) -> None:
    qid = str(d.get("queue_id", ""))
    with st.container(border=True):
        st.caption(f"{qid} ／ theme: {d.get('theme', '')}")
        text = st.text_area("本文", value=d.get("text", ""), height=180, key=f"text_{qid}")
        sched = st.text_input(
            "予定時刻 (YYYY-MM-DD HH:MM)",
            value=service.normalize_scheduled_at(d.get("scheduled_at", "")),
            key=f"sched_{qid}",
        )
        c1, c2 = st.columns(2)
        if c1.button("✅ 承認", key=f"ap_{qid}", use_container_width=True):
            _run_write("承認", service.approve_draft, sheets, qid, text, sched)
        if c2.button("💾 下書き保存", key=f"sv_{qid}", use_container_width=True):
            _run_write("下書き保存", service.save_draft, sheets, qid, text, sched)


def _tab_review(sheets) -> None:
    try:
        drafts = service.read_drafts(sheets)
    except Exception as exc:
        logger.exception("read_drafts failed")
        st.error(f"候補の取得に失敗: {exc}")
        return

    st.markdown(f"### 候補レビュー（draft: {len(drafts)}）")
    if not drafts:
        st.info("レビュー待ちの下書きはありません。")
        return

    # Lay cards out across N columns on wide screens; Streamlit stacks them
    # vertically on narrow/mobile screens automatically.
    cols = st.columns(_REVIEW_COLUMNS, gap="large")
    for i, d in enumerate(drafts):
        with cols[i % _REVIEW_COLUMNS]:
            _draft_card(sheets, d)
            st.write("")  # vertical breathing room between stacked cards


def _tab_schedule(sheets) -> None:
    st.subheader("予定・実績")
    try:
        rows = service.read_queue_by_statuses(
            sheets, [STATUS_APPROVED, STATUS_POSTED, STATUS_FAILED]
        )
    except Exception as exc:
        logger.exception("read_queue failed")
        st.error(f"キューの取得に失敗: {exc}")
        rows = []

    if rows:
        st.dataframe(
            [
                {
                    "status": r.get("status", ""),
                    "scheduled_at": r.get("scheduled_at", ""),
                    "本文": str(r.get("text", "")).replace("\n", " ")[:40],
                    "posted_at": r.get("posted_at", ""),
                }
                for r in rows
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("approved / posted / failed の行はまだありません。")

    st.divider()
    st.subheader("投稿済みの評価")
    try:
        posts = service.read_posts(sheets)
    except Exception as exc:
        logger.exception("read_posts failed")
        st.error(f"投稿の取得に失敗: {exc}")
        return

    if not posts:
        st.info("posts にデータがありません。")
        return

    for p in posts:
        pid = str(p.get("post_id", ""))
        with st.container(border=True):
            st.write(str(p.get("text", "")).replace("\n", " ")[:120] or "（本文なし）")
            st.caption(
                f"views={p.get('views', '-')} ／ likes={p.get('likes', '-')} "
                f"／ 現在のrating: {p.get('rating', '') or '未評価'}"
            )
            fb = st.text_input("feedback（一言メモ）", value=p.get("feedback", ""), key=f"fb_{pid}")
            c1, c2 = st.columns(2)
            if c1.button("👍 good", key=f"g_{pid}", use_container_width=True):
                _run_write("評価(good)", service.set_post_rating, sheets, pid, "good", fb)
            if c2.button("👎 bad", key=f"b_{pid}", use_container_width=True):
                _run_write("評価(bad)", service.set_post_rating, sheets, pid, "bad", fb)


def _tab_notes(sheets) -> None:
    st.subheader("小言メモ（投稿素材）")
    st.caption("思いついたことを書いて追加 → 翌日の下書き生成で最優先の素材になります。")
    note = st.text_area("小言", key="note_input", height=120)
    if st.button("➕ 追加", use_container_width=True):
        if not note.strip():
            st.warning("小言が空です。")
        else:
            _run_write("小言を追加", service.add_note, sheets, note, service.today_jst())


def main() -> None:
    _require_password()
    sheets = _get_sheets()

    st.title("🧵 Threads 運用ダッシュボード")
    _sidebar(sheets)

    review, schedule, notes = st.tabs(["📝 候補レビュー", "📅 予定・実績", "🗒 小言"])
    with review:
        _tab_review(sheets)
    with schedule:
        _tab_schedule(sheets)
    with notes:
        _tab_notes(sheets)


main()
