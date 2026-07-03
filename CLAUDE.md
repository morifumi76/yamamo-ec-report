# yamamo-ec-report

## 概要
森田醤油醸造元のBASE ECショップから売上データを毎日蓄積し、月次レポート（HTML）を毎月1日に自動生成してGitHub Pagesで公開する自動化ツール。

## 技術スタック
- 言語: Python 3.11+（データ取得・集計スクリプト）
- フロントエンド: HTML + Tailwind CSS（CDN版）+ Vanilla JavaScript
- CI/CD: GitHub Actions（毎日取得ジョブ + 月次生成ジョブの2系統）
- デプロイ先: GitHub Pages
- AI: GitHub Models（月次AI分析コメント生成・予定）

## フォルダ構成
- `docs/specs/` : 設計書・仕様書（現行は v1.5）
- `scripts/` : Python スクリプト群（今後追加）
  - `oauth_init.py` : 初回認証（ローカル1回のみ）
  - `fetch_daily.py` : 前日分データ取得
  - `generate_monthly.py` : 月次集計＋latest.json生成
  - `ai_comment.py` : AI分析コメント生成
- `data/` : データ保管（今後追加）
  - `daily/YYYY-MM-DD.json` : 日次生データ
  - `latest.json` : index.htmlが読む最新月次データ
  - `archive/YYYY-MM.json` : 過去月次バックアップ
- `.github/workflows/` : GitHub Actions 定義（今後追加）
- `index.html` : レポートUI（ルート直下）
- `sample-data.json` : 開発用ダミーデータ
- `yamamo-rogo.png` : ロゴ画像
- `starter/` : 旧テンプレ残骸（.gitignoreで除外済み・将来要否判断）

## このプロジェクト固有のルール
- 設計書（`docs/specs/yamamo-ec-report｜最終設計書 v1.5.md`）に従って実装する。差分が生じたら**設計書を先に更新**してから実装を変更する。
- 既存ファイル（`index.html` / `sample-data.json` / `yamamo-rogo.png`）は不用意に上書きしない。
- `main` ブランチへの直接pushは禁止。必ず feature ブランチ + PR 方式で進める。
- 削除系コマンド（`rm -rf` 等）は一切使わない。
- APIキー等の秘密情報は `.env` で管理し、絶対にコミットしない。
- マイルストーン（M0〜M7）単位で「計画提示 → 確認 → 実装 → 動作確認」のサイクルを回す。次のマイルストーンに進む前に必ず確認を取る。

## 主要な外部サービス
- BASE（EC基盤）: OAuth 2.0 で API 利用。Secrets に `BASE_CLIENT_ID` / `BASE_CLIENT_SECRET` / `BASE_REFRESH_TOKEN` を登録。
- GitHub Actions: 毎日 09:00 JST でデータ取得、毎月1日 20:00 JST でレポート生成。
- GitHub Models: AI分析コメント生成に使用（月次確定時＋年度締め時）。
- GitHub Pages: レポート公開先。

## 現在の状態
- M0〜M8 完了・本番運用中（2026-04〜 日次取得と月次レポート生成が自動実行されている）
- M9 実施中（2026-07-04〜）: 運用堅牢化（設計書 v1.5 の改訂履歴を参照）

## 🔒 機密情報の取り扱いルール（厳守）

このプロジェクトでは、認証情報・APIキー・トークン等の機密情報をチャット・ターミナル出力・ログ・コミットメッセージ・PR説明・スクショに**一切表示してはならない**。

### ❌ 絶対に禁止する行為

1. **シークレットの実値を出力に表示しない**
    - `BASE_CLIENT_SECRET` / `BASE_REFRESH_TOKEN` / `BASE_ACCESS_TOKEN` / `REPO_PAT` / `BASE_CLIENT_ID` 等の値を平文でチャット・標準出力・ログ・PR説明に表示しない
    - `.env` / `.env.local` / `secrets.json` 等のファイル内容をそのまま出力しない
    - `cat .env` / `echo $BASE_CLIENT_SECRET` / `printenv` 等のコマンドを実行して値を表示しない
    - ファイル読み取りツールで `.env` 系を読んだ場合、その内容を要約・参照のみに留め、実値はチャットに出さない

2. **ユーザーにシークレットの貼付を要求しない**
    - 「Client Secretをここに貼ってください」のような依頼をしない
    - 必要な値はすべて `.env` または環境変数として設定してもらう前提で実装・案内する
    - どうしても確認が必要な場合は「`.env` の `BASE_CLIENT_SECRET` の**先頭4文字と末尾4文字**だけ教えてください」のように部分マスクで聞く

3. **値を含めた状態でコミットしない**
    - `.env` は必ず `.gitignore` に含まれている前提で動く（無ければ追加を提案）
    - `git add .env` / `git add secrets.*` のような操作を提案しない
    - シークレットがコードにハードコードされた状態で commit/push を行わない
    - コミットメッセージ・PR本文・コードコメントにシークレットの値を入れない

4. **デバッグ出力・ログでも漏らさない**
    - `print(client_secret)` 的なデバッグ出力を書かない
    - `logger.info(f"token={token}")` のようなロギングを書かない
    - `requests` の `headers` / `params` / `json` を**まるごと**ログに出力しない（個別フィールドのみ、かつトークン系は除外）
    - HTTPレスポンスの本文にシークレットが含まれる場合は、ログ前にマスクする

### ✅ 推奨される表現

機密情報に言及する必要があるときは、以下のように扱う：

- **名前のみ参照**：「`BASE_CLIENT_SECRET` を `.env` に設定してください」（値は書かない）
- **マスク表示**：`token=***` / `secret=<REDACTED>` / `client_id=993d…ee82`（先頭4文字＋末尾4文字のみ表示）
- **長さ・形式チェックのみ**：「32文字の16進数文字列であることを確認しました」

### 🚨 漏洩リスクを検知したら

1. 出力前にユーザーへ警告を出す：「この出力には実シークレットが含まれます。マスク表示でよいですか？」
2. ユーザーが既にチャット等にシークレットを貼ってしまった場合：「**このシークレットは即時ローテーションを推奨します**」と通知し、再発行手順を提示する
3. 自動でマスク済みの版に置き換えて出力する

### 📝 具体例

**❌ NG例**

```
BASE_REFRESH_TOKEN の値:
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```
（↑ 本来は実値が書かれているケース。これを禁止する。例示のため伏字に置換済み）

**✅ OK例**

```
BASE_REFRESH_TOKEN の現在値:
- 先頭4文字: xxxx
- 末尾4文字: xxxx
- 文字数: 32（想定通り）
- 完全な値は .env ファイルを直接確認してください
```

### 対象となる識別子（本プロジェクトの機密情報リスト）

以下のキー名・パターンに該当するものはすべて機密情報として扱う：

- `BASE_CLIENT_ID`
- `BASE_CLIENT_SECRET`
- `BASE_REFRESH_TOKEN`
- `BASE_ACCESS_TOKEN`
- `REPO_PAT`
- `GITHUB_TOKEN`
- `OAUTH_CODE`（一時的な authorization code も対象）
- `.env` / `.env.local` / `.env.*` ファイル内の全変数
- 名前に `SECRET` / `TOKEN` / `KEY` / `PASSWORD` / `PAT` / `CREDENTIAL` を含む環境変数

### 原則

ユーザーが**明示的に**「実値を見せて」「`.env` の中身を表示して」と指示した場合のみ、実値を出力してよい。
それ以外は**「疑わしきは伏せる（mask-by-default）」**。
