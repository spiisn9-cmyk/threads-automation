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
from src.core.tags import TECHNIQUE_TAGS, parse_tags
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


_RATING_CHOICES = ["未設定", "good", "ok", "bad"]


def _rating_radio(current: str, key: str) -> str:
    """Horizontal good/ok/bad picker; returns '' when 未設定."""
    cur = current if current in ("good", "ok", "bad") else "未設定"
    choice = st.radio(
        "評価", _RATING_CHOICES, index=_RATING_CHOICES.index(cur),
        horizontal=True, key=key,
    )
    return "" if choice == "未設定" else choice


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


def _draft_detail(sheets, d) -> None:
    """Full edit/approve UI for a single post (shown inside an expander)."""
    qid = str(d.get("queue_id", ""))
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

    tags = st.multiselect(
        "技法タグ", TECHNIQUE_TAGS, default=parse_tags(d.get("tags", "")), key=f"tags_{qid}"
    )
    rating = _rating_radio(d.get("rating", ""), key=f"rate_{qid}")
    fb = st.text_input("一言FB", value=d.get("feedback", ""), key=f"qfb_{qid}")
    if st.button("🏷 FB保存（タグ/評価/一言）", key=f"qfbsave_{qid}", use_container_width=True):
        _run_write("FB保存", service.save_queue_feedback, sheets, qid, tags, rating, fb)


def _thread_detail(sheets, group) -> None:
    """Full edit/approve UI for a thread (shown inside an expander)."""
    rows = group["rows"]
    tid = group["thread_id"]
    items: list[dict] = []
    for n, r in enumerate(rows):
        qid = str(r.get("queue_id", ""))
        label = "親" if n == 0 else f"返信{n}"
        text = st.text_area(label, value=r.get("text", ""), height=140, key=f"text_{qid}")
        item = {"queue_id": qid, "text": text}
        if n == 0:
            sched = st.text_input(
                "親の予定時刻 (YYYY-MM-DD HH:MM)",
                value=service.normalize_scheduled_at(r.get("scheduled_at", "")),
                key=f"sched_{qid}",
            )
            item["scheduled_at"] = sched
        items.append(item)
    c1, c2 = st.columns(2)
    if c1.button("✅ ツリーを承認", key=f"apth_{tid}", use_container_width=True):
        _run_write("ツリー承認", service.save_rows, sheets, items, True)
    if c2.button("💾 まとめて下書き保存", key=f"svth_{tid}", use_container_width=True):
        _run_write("下書き保存", service.save_rows, sheets, items, False)


def _group_row_label(g: dict) -> str:
    """Single-line summary for the expander label."""
    if g["is_thread"]:
        parent = g["rows"][0]
        preview = str(parent.get("text", "")).replace("\n", " ")[:50]
        sched = service.normalize_scheduled_at(parent.get("scheduled_at", ""))
        theme = parent.get("theme", "")
        n = len(g["rows"])
        tags_str = str(parent.get("tags", "")).strip()
        tag_part = f"  [{tags_str}]" if tags_str else ""
        return f"🧵 {sched}  {theme}  {preview}… (ツリー/返信{n - 1}件){tag_part}"
    else:
        d = g["rows"][0]
        preview = str(d.get("text", "")).replace("\n", " ")[:60]
        sched = service.normalize_scheduled_at(d.get("scheduled_at", ""))
        theme = d.get("theme", "")
        tags_str = str(d.get("tags", "")).strip()
        tag_part = f"  [{tags_str}]" if tags_str else ""
        return f"📝 {sched}  {theme}  {preview}…{tag_part}"


def _tab_review(sheets) -> None:
    try:
        groups = service.read_draft_groups(sheets)
    except Exception as exc:
        logger.exception("read_draft_groups failed")
        st.error(f"候補の取得に失敗: {exc}")
        return

    total = sum(len(g["rows"]) for g in groups)

    # --- filters ---
    fc1, fc2, fc3 = st.columns([2, 3, 1])
    with fc1:
        kind_filter = st.selectbox(
            "種別", ["すべて", "単発のみ", "ツリーのみ"], key="rv_kind"
        )
    with fc2:
        kw = st.text_input("本文キーワード", key="rv_kw", placeholder="絞り込み…")
    with fc3:
        st.metric("合計", f"{total}件 / {len(groups)}グループ")

    kw_lower = kw.strip().lower()

    def _match(g: dict) -> bool:
        if kind_filter == "単発のみ" and g["is_thread"]:
            return False
        if kind_filter == "ツリーのみ" and not g["is_thread"]:
            return False
        if kw_lower:
            combined = " ".join(
                str(r.get("text", "")).lower() for r in g["rows"]
            )
            if kw_lower not in combined:
                return False
        return True

    visible = [g for g in groups if _match(g)]
    if not visible:
        st.info("該当する下書きがありません。")
        return

    st.caption(f"{len(visible)} グループを表示中")
    st.divider()

    for g in visible:
        label = _group_row_label(g)
        with st.expander(label, expanded=False):
            if g["is_thread"]:
                _thread_detail(sheets, g)
            else:
                _draft_detail(sheets, g["rows"][0])


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

    cols = st.columns(_REVIEW_COLUMNS, gap="large")
    for i, p in enumerate(posts):
        pid = str(p.get("post_id", ""))
        with cols[i % _REVIEW_COLUMNS]:
            with st.container(border=True):
                st.write(str(p.get("text", "")).replace("\n", " ")[:120] or "（本文なし）")
                st.caption(
                    f"views={p.get('views', '-')} ／ likes={p.get('likes', '-')}"
                )
                tags = st.multiselect(
                    "技法タグ", TECHNIQUE_TAGS, default=parse_tags(p.get("tags", "")),
                    key=f"ptags_{pid}",
                )
                rating = _rating_radio(p.get("rating", ""), key=f"prate_{pid}")
                fb = st.text_input("一言FB", value=p.get("feedback", ""), key=f"pfb_{pid}")
                if st.button("💾 保存（タグ/評価/一言）", key=f"psave_{pid}", use_container_width=True):
                    _run_write("FB保存", service.save_post_feedback, sheets, pid, tags, rating, fb)
            st.write("")


def _tab_notes(sheets) -> None:
    st.subheader("小言メモ（投稿素材）")
    st.caption("思いついたことを書いて追加 → 翌日の下書き生成で最優先の素材になります。")
    note = st.text_area("小言", key="note_input", height=120)
    if st.button("➕ 追加", use_container_width=True):
        if not note.strip():
            st.warning("小言が空です。")
        else:
            _run_write("小言を追加", service.add_note, sheets, note, service.today_jst())


def _tab_references(sheets) -> None:
    st.subheader("参考投稿（型・構成を学ぶ swipe file）")
    st.caption(
        "伸びてる投稿を貯めて、型・フック・ツリーの“組み立て”だけを学びます。"
        "本文は丸写しせず、中身はうに自身でオリジナル生成します。"
    )

    with st.form("add_reference", clear_on_submit=True):
        st.markdown("**参考投稿を追加**")
        source = st.text_input("source（URL / ハンドル）")
        text = st.text_area(
            "参考本文（構造分析用。ツリーなら各投稿を改行で）", height=140
        )
        structure_note = st.text_area(
            "structure_note（フック / 型 / ツリーの組み立てメモ）", height=100
        )
        c1, c2 = st.columns(2)
        is_thread = c1.checkbox("ツリー（連投）の例")
        active = c2.checkbox("active（学習対象にする）", value=True)
        if st.form_submit_button("➕ 追加", use_container_width=True):
            try:
                service.add_reference(
                    sheets, source, text, structure_note,
                    is_thread, active, service.today_jst(),
                )
            except Exception as exc:
                logger.exception("add_reference failed")
                st.error(f"追加に失敗しました: {exc}")
            else:
                st.success("追加しました")
                st.rerun()

    st.divider()
    st.markdown("**登録済みの参考投稿**")
    try:
        refs = service.list_references(sheets)
    except Exception as exc:
        logger.exception("list_references failed")
        st.error(f"一覧の取得に失敗: {exc}")
        return
    if not refs:
        st.info("まだ参考投稿がありません。")
        return

    for r in refs:
        ridx = r["row_index"]
        is_active = str(r.get("active", "")).strip().lower() in {"true", "yes", "1", "on", "active"}
        kind = "🧵ツリー" if str(r.get("is_thread", "")).strip().lower() in {"true", "yes", "1"} else "単発"
        with st.container(border=True):
            st.caption(f"{r.get('source', '') or '(no source)'} ／ {kind} ／ {'🟢active' if is_active else '⚪︎off'}")
            note = str(r.get("structure_note", "") or r.get("learn", "")).strip()
            if note:
                st.write(f"**構成**: {note}")
            body = str(r.get("text", "")).replace("\n", " ")[:120]
            if body:
                st.caption(f"参考本文: {body}")
            label = "⚪︎ off にする" if is_active else "🟢 active にする"
            if st.button(label, key=f"refactive_{ridx}", use_container_width=True):
                _run_write("更新", service.set_reference_active, sheets, ridx, not is_active)


def main() -> None:
    _require_password()
    sheets = _get_sheets()

    st.title("🧵 Threads 運用ダッシュボード")
    _sidebar(sheets)

    review, schedule, refs, notes = st.tabs(
        ["📝 候補レビュー", "📅 予定・実績", "📚 参考投稿", "🗒 小言"]
    )
    with review:
        _tab_review(sheets)
    with schedule:
        _tab_schedule(sheets)
    with refs:
        _tab_references(sheets)
    with notes:
        _tab_notes(sheets)


main()
