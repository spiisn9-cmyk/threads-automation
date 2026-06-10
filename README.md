# Threads 運用 自動化ツール (MVP)

毎朝、自分の Threads の数値（フォロワー / views）を自動取得して Google スプレッドシートに記録し、
Claude が 3〜5 行の所感＋今日の一手を生成して **メールで 1 通** 届けます。
実行基盤は **GitHub Actions**（サーバー不要）。

## アーキテクチャ

```
GitHub Actions (cron 06:30 JST)
        │
        ▼
src.jobs.run_daily
  ├─ ThreadsClient   … graph.threads.net から followers / views / 投稿インサイト取得
  ├─ SheetsClient    … metrics_daily / posts / logs に冪等記録 (upsert)
  ├─ ClaudeClient    … claude-sonnet-4-6 で所感＋今日の一手を生成
  └─ NotifyClient    … SMTP(STARTTLS) でメール1通
```

設計方針: イミュータブル（frozen dataclass）、小さなファイル分割、例外は握りつぶさずログ＋再送出、
秘密情報は環境変数のみ（コードに鍵を書かない）。

## 必要なもの

- Python 3.11
- Threads（Meta）の長期アクセストークン
- Google サービスアカウント（スプレッドシート編集権限を付与）
- Anthropic API キー
- SMTP 送信できるメールアカウント（例: Gmail のアプリパスワード）

## セットアップ手順

### 1. 依存インストール

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 環境変数

`.env.example` を `.env` にコピーして埋めます（`.env` はコミットしないこと）。

```bash
cp .env.example .env
```

| 変数 | 説明 |
|------|------|
| `THREADS_ACCESS_TOKEN` | Threads の長期アクセストークン |
| `THREADS_USER_ID` | Threads ユーザー ID（取得は `/me` を使うため必須ではないが Secret として保持） |
| `GOOGLE_SA_JSON` | サービスアカウント JSON の中身を**1行の文字列**で |
| `SPREADSHEET_ID` | 記録先スプレッドシートの ID |
| `ANTHROPIC_API_KEY` | Anthropic API キー |
| `CLAUDE_MODEL` | 既定 `claude-sonnet-4-6` |
| `SMTP_HOST` / `SMTP_PORT` | SMTP ホスト / ポート（既定 587） |
| `SMTP_USER` / `SMTP_PASS` | SMTP 認証情報 |
| `MAIL_TO` | 送信先メールアドレス |
| `REPORT_LOOKBACK_DAYS` | 推移を読む日数（既定 7） |

`GOOGLE_SA_JSON` の1行化:

```bash
cat service_account.json | python -c "import json,sys;print(json.dumps(json.load(sys.stdin)))"
```

> **スプレッドシート共有を忘れずに**: サービスアカウントの `client_email` を、対象スプレッドシートに「編集者」で共有してください。

### 3. Threads 取得だけ先に動作確認

Sheets / Claude / メールを設定する前に、まず数値が取れるか確認します（`THREADS_ACCESS_TOKEN` だけで動きます）。
RAW レスポンスもログ出力されるので、実際の JSON 構造を確認できます。

```bash
python -m scripts.check_threads
```

### 4. シート初期化

`metrics_daily` / `posts` / `logs` の3シートとヘッダ行を作成します（既存ならスキップ）。

```bash
python -m scripts.init_sheets
```

### 5. 手動実行

全体を一度手で走らせて、メールが届くか確認します。

```bash
python -m src.jobs.run_daily
```

### 6. スケジュール実行（GitHub Actions）

`.github/workflows/daily.yml` が cron `30 21 * * *`（UTC = **JST 06:30**）と `workflow_dispatch` で起動します。
リポジトリの **Settings → Secrets and variables → Actions** に、上表の各環境変数を **Secret** として登録してください。

手動トリガーは Actions タブの **Run workflow** から実行できます。

## テスト

```bash
pytest
```

- `tests/test_upsert.py` — 同一日付で2回 upsert しても重複しない／follower_delta の計算
- `tests/test_threads_client.py` — httpx をモックしてレスポンスのパースを検証（実 API は叩かない）

## シート構成

| シート | カラム |
|--------|--------|
| `metrics_daily` | date, followers, views, follower_delta, note |
| `posts` | post_id, posted_at, text, views, likes |
| `logs` | datetime, job, status, count, message |

## ⚠️ 要確認（Meta API の不確実性）

実機で確認済みの仕様に合わせていますが、Threads API のレスポンス構造は変わり得ます。
パースは防御的に実装し（キー欠落でも落ちず `None`）、初回実行時は RAW レスポンスをログに出します。
本番投入前に `python -m scripts.check_threads` のログで以下を確認してください。

- アカウント数値: `GET /me/threads_insights?metric=views,followers_count`
  - 値は `total_value.value` の想定。ただし `views` は時系列 `values[].value` で返る可能性もあるため両対応済み。
- 投稿一覧: `GET /me/threads?fields=id,text,timestamp&limit=10`
- 投稿インサイト: `GET /{media-id}/insights?metric=views,likes`
- `followers_count` は since/until 非対応。

数値が `None` で返る場合はメール冒頭に「⚠️取得失敗」が付き、`logs` にも記録されます。
