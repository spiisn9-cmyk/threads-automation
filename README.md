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
| `posts` | post_id, posted_at, text, views, likes, rating, feedback |
| `logs` | datetime, job, status, count, message |
| `learnings` | created_at, learning, evidence, source |
| `post_queue` | queue_id, scheduled_at, text, theme, status, posted_post_id, posted_at |
| `notes` | created_at, note, theme, status |
| `references` | created_at, source, impressions, text, learn, active |

`post_queue.status` は `draft` → `approved` → `posted` / `failed` の4状態です。
`notes.status` は `new`（未使用）→ `used`（下書きに反映済み）の2状態です。
`references.active` は `active`（学習対象）/ `off`（無視）の2値です。
`posts.rating` は `good`/`ok`/`bad`（うに手入力）、`feedback` は一言メモ。`learnings.source` は `auto`（分析が追記）/ `uni`（うにが追記）。

## Phase 2: 投稿の下書き生成＋承認制の予約自動投稿

MVP（数値取得・毎朝レポート）はそのままに、投稿の**下書き生成**と、**承認したものだけ**を予約時刻に自動投稿する仕組みを追加しました。
人間の承認（`status` を `approved` に変更）が無い限り、何も投稿されません。

```
generate_drafts (日次)  →  post_queue に status=draft で翌日分の候補3本を書き込み
        │
        ▼
   あなたがスプレッドシートで内容を編集し、投稿したい行の status を approved に変更
        │
        ▼
publish_queue (毎時)    →  approved かつ scheduled_at<=現在 の行だけ投稿 → status=posted
```

### 前提：投稿用トークン

投稿（F5）には **`threads_content_publish` 権限を持つ Threads アクセストークン**が必要です（数値取得だけのトークンでは投稿できません）。
同じ `THREADS_ACCESS_TOKEN` に権限を付与してください。

### 使い方

1. **シート初期化（post_queue を追加）**
   ```bash
   python -m scripts.init_sheets
   ```
   `post_queue` が無ければ作成・ヘッダ追加します（既存シートは触りません）。

2. **（任意）小言メモを書いておく**
   その日の出来事・気分・つぶやきを `notes` シートに1行ずつ書きます。
   - `created_at`: 任意（いつ書いたか）
   - `note`: 小言本文（例:「今日は3時間プロンプト調整で溶けた笑」）
   - `theme`: 任意（5つの柱のどれかを書くとヒントになる。空でも可）
   - `status`: `new`（未使用）

   次の下書き生成で、`status=new` の小言が**最優先**で投稿素材になります。
   小言に書いていない数字・出来事は創作されません（温度感・言い回しを活かして「うに文体」に整える程度）。

3. **（任意）伸びてる投稿を `references` に貼る（型を学ぶ swipe file）**
   参考にしたい「伸びている投稿」を `references` シートに1行ずつ貼ります。
   - `source`: 投稿者ハンドルや URL
   - `impressions`: おおよそのインプ数（手入力・任意）
   - `text`: 投稿本文（参考にする“型”）
   - `learn`: 学ぶ点メモ（例:「問いかけで始める型」）
   - `active`: `active`（学習対象）/ `off`（無視）

   生成時、`active` の行が「型のお手本」として渡され、**型・構成・書き出し・切り口・問いかけ方**を学びます。
   **本文・トピック・具体表現は丸写ししません**（パクリ・重複を避ける）。中身はうに自身（小言／5つの柱）を、学んだ型で書きます。

4. **下書きを生成（F4）**
   ```bash
   python -m src.jobs.generate_drafts
   ```
   `prompts/post_drafts.md`（うにの文体ガイド）をシステムプロンプトに、`learnings`（効く型/避ける型）＋`posts.rating`（good/bad）＋`notes` の未使用小言＋`references` の型のお手本＋直近の投稿内容を入力として、
   Claude が5つの柱に沿って **翌日分の候補を 3本**（`DAILY_DRAFT_COUNT`）生成し、`post_queue` に `status=draft` で書き込みます（公開投稿の安全ガードにより実際に出るのは1日1本まで＝残りは候補）。
   使った小言は `notes.status=used` に更新されます。小言が足りない分は5つの柱から「事実を必要としない一般的な学び・考え・お役立ち」で補完します。`references`/`learnings` が無い場合は従来どおり（小言＋柱）で生成します。
   詳細は後述の「日々の学習ループ」を参照。

   > **数値ポリシー**: 公開投稿にはフォロワー数・views等の成長指標を**出しません**（`metrics_daily` の数値は毎朝の自分向けレポート＝F2専用）。
   > うに自身が `notes` の小言に数値や節目を書いた場合のみ、それを尊重して使います。
   `scheduled_at` は既定で「翌日の日中」（投稿可能時間帯 8〜22時JST内・時/分をばらつかせる）を割り当てます。3本とも翌日の候補で、承認した1本だけが実際に投稿されます（後で編集可）。
   本数・投稿時刻・タイムゾーンは `config/settings.py` の定数（`DRAFT_COUNT` / `DRAFT_POST_HOUR` / `DRAFT_POST_MINUTE` / `JST`）で変更できます。

5. **承認（人間の作業）**
   スプレッドシートの `post_queue` を開き、本文や `scheduled_at` を必要に応じて編集。
   投稿したい行だけ `status` を `draft` → `approved` に変更します。**`draft` のままの行は絶対に投稿されません。**

6. **予約投稿（F5）**
   ```bash
   python -m src.jobs.publish_queue
   ```
   `status==approved` かつ `scheduled_at<=現在(JST)` の行を対象に、**安全ガードを通過した場合のみ最大1本**を投稿します。
   成功で `status=posted`＋`posted_post_id`＋`posted_at`(JST) を記録、失敗で `status=failed`＋`logs` に記録。
   `posted` / `failed` は再処理しないため、毎時実行しても二重投稿しません。

### 🔒 自動投稿の安全ガード（BAN対策）

過去に連投・不自然な時刻の投稿で凍結された経験を踏まえ、**人間らしい控えめなペース**を厳守します
（本ツールは Meta 公式 Threads API `threads_content_publish` による正規投稿です）。
`publish_queue` は毎回、対象（approved かつ予約時刻到来・早い順）に対し次のガードを順に適用し、**1回の実行で最大1本**だけ投稿します。

1. **時間帯ガード** — 現在(JST)が投稿可能時間帯の外なら 0本（`logs` に「時間帯外スキップ」）
2. **日次上限** — 今日(JST)の投稿数が上限に達していたら 0本（「日次上限スキップ」）
3. **最小間隔** — 直近の投稿から最小間隔(時間)未満なら 0本（「最小間隔スキップ」）
4. **1回1本** — 通過しても投稿するのは先頭1本だけ。残りは次回以降に持ち越し
5. **ジッター** — 投稿直前に 0〜数分（既定3分）のランダム待機を入れ、`:05` など規則的な分を崩す

スキップ理由は必ず `logs` シートに記録されます。毎時(`5 * * * *`)実行しても、これらのガードにより実際の投稿は控えめなペースに保たれます。

#### 設定の変更（`config/settings.py` の定数）

| 定数 | 既定 | 意味 |
|------|------|------|
| `MAX_POSTS_PER_RUN` | 1 | 1回の実行で投稿する最大数（連投を物理的に防止） |
| `MAX_POSTS_PER_DAY` | 1 | 1日(JST)の最大投稿数 |
| `MIN_HOURS_BETWEEN_POSTS` | 4 | 直近の投稿からの最小間隔（時間） |
| `POST_WINDOW_START_HOUR` | 8 | 投稿可能時間帯(JST)の開始（含む） |
| `POST_WINDOW_END_HOUR` | 22 | 投稿可能時間帯(JST)の終了（含まない） |
| `POST_JITTER_MINUTES` | 3 | 投稿直前のランダム待機の最大（分） |

> **接続の堅牢性**: ジッターのsleep後に Google Sheets の接続（httplib2 のソケット）が切れていても落ちないよう、`SheetsClient` は一過性エラー（接続リセット・5xx・429 等）で**自動リトライ＋接続を再確立**します。`POST_JITTER_MINUTES` を長くしてもこの問題は再発しませんが、既定は短め（3分）にしてあります。

慣れてきてペースを上げたい場合は `MAX_POSTS_PER_DAY` を少しずつ増やす運用を推奨します（まずは1から）。
下書きの予約時刻も、この時間帯(8〜22時JST)内で毎日時・分をばらつかせて自動割り当てされます。

### スケジュール実行（GitHub Actions）

- `.github/workflows/generate_drafts.yml` … 日次（cron `0 22 * * *` UTC = 07:00 JST、日報の後）＋手動実行
- `.github/workflows/publish.yml` … 毎時（cron `5 * * * *`）＋手動実行
- 既存の `daily.yml` は変更ありません。

各workflowには既存と同じ Secrets を渡しています（`publish` 自体は Threads と Sheets のみ使いますが、
`load_settings()` が全項目を検証するため全 Secrets を渡しておくと安全です）。

## 日々の学習ループ（分析 → 学び → 改善した下書き）

毎日「投稿の成績を分析 → 学びを蓄積 → 学びを反映した下書きを翌日分生成」を回します。

```
毎朝(F2 run_daily, 06:30 JST)
  ├─ posts(直近7日)の成績＋rating/feedback を分析（Claude）
  ├─ 学びを learnings に追記（source=auto, evidence付き）
  └─ 日報メールに「前日投稿の簡易分析」ブロックを追加
        │
        ▼ あなたが posts に rating(good/ok/bad)・feedback を手入力
        ▼
毎朝(F4 generate_drafts, 07:00 JST)
  └─ learnings＋rating＋references＋notes を材料に、
     「bad/低反応の型を避け、good/伸びた型に寄せた」候補を翌日分 3本 生成（status=draft）
```

### 運用フロー：評価(rating)の付け方 → 翌日の生成に反映

1. **投稿の評価を入れる**（任意・いつでも）
   `posts` シートで、各投稿の `rating` に `good` / `ok` / `bad`、`feedback` に一言メモ（例:「問いかけで終えたら反応良かった」）を入力します。
   - run_daily は毎朝 views/likes を更新しますが、**あなたが入れた rating/feedback は保持**されます（上書きされません）。
2. **毎朝、自動で分析される**
   run_daily が直近7日（`ANALYSIS_LOOKBACK_DAYS`）の投稿を分析し、`learnings` に学びを追記。日報メールにも簡易分析が載ります。
   - ⚠️ 投稿数・数値が小さい初期は**断定せず「サンプルが少ない」前提**で控えめに書きます。
3. **翌日分の下書きに反映される**
   generate_drafts が `learnings`（効く型/避ける型）＋`posts.rating`（good に寄せ・bad を避ける）＋`references`（型）＋`notes`（内容）を材料に、改善した候補を **3本**（`DAILY_DRAFT_COUNT`）生成します。
   - 維持される原則：**小言の事実は創作しない／参考・型は丸写ししない／数値(フォロワー・views)は投稿に出さない。**
4. 自分でも気づきがあれば `learnings` に `source=uni` で直接追記してOK（生成材料になります）。

設定の定数（`config/settings.py`）: `DAILY_DRAFT_COUNT=3` / `ANALYSIS_LOOKBACK_DAYS=7`。
スケジュール: `generate_drafts.yml` は日次（`0 22 * * *` UTC = 07:00 JST、日報の後）。分析は `run_daily` 内包なので `daily.yml` は変更なし。

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

### Phase 2（投稿）の要確認

- 投稿は2段階です。レスポンスはいずれも `{"id": "..."}` を想定し、防御的に `id` を取り出します。
  - 作成: `POST /me/threads`（`media_type=TEXT`, `text=...`）→ `creation_id`
  - 公開: `POST /me/threads_publish`（`creation_id=...`）→ `media_id`
- パラメータはクエリ文字列で送っています。Meta 側がフォーム必須に変わった場合は `threads_client._post` の送り方を要調整。
- Meta は**コンテナ作成から公開まで少し待つこと**を推奨する場合があります（数十秒程度）。本MVPは即時公開していますが、
  もし `media not ready` 系のエラーが出る場合は、作成と公開の間に待機を入れる調整が必要です（`logs` を確認）。
- 投稿には `threads_content_publish` 権限のトークンが必須です。権限不足の場合は `failed` として記録されます。
