# 松村式Stocksurfing（クラウド版・Phase 2 / v3.0）

朝の仕掛け判定を自動化。GitHub Actions が毎朝7:30と寄り後9:20に指標を取得し、メール通知＋PWA表示する。

## URL（リポジトリ作成後に有効化）

`https://matsumuratantei-Stocksurfing.github.io/Stocksurfing/`

## アーキテクチャ

- `index.html` — メインアプリ（PWA対応）
- `fetch_data.py` — yfinance + Nikkei公式から12指標を取得
- `send_email.py` — Gmail SMTP で2宛先にメール送信
- `data.json` — fetch_data.py が出力。HTMLが読込
- `.github/workflows/fetch.yml` — 毎日2回スケジュール実行
- `manifest.webmanifest` + `sw.js` + `icons/` — PWA定義
- `requirements.txt` — Python依存

## スケジュール

| 時刻（JST） | 内容 |
|---|---|
| 7:30 | 場の判定スコア + 候補銘柄をメール |
| 9:20（平日） | 寄り後の最終判定をメール |

## 必要なGitHub Secrets

| Secret名 | 値 |
|---|---|
| `GMAIL_USER` | 送信元Gmailアドレス |
| `GMAIL_APP_PASSWORD` | Gmailアプリパスワード（16文字） |
| `RECIPIENTS` | 通知先（カンマ区切り） |

## 必要なGitHub Variables

| Variable名 | 値 |
|---|---|
| `PAGES_URL` | `https://matsumuratantei-Stocksurfing.github.io/Stocksurfing/` |

## 奥様向け使い方

### 初回セットアップ（iPhone）

1. Safari で `https://matsumuratantei-Stocksurfing.github.io/Stocksurfing/` を開く
2. 画面下の **共有ボタン**（□に↑） をタップ
3. **「ホーム画面に追加」** をタップ
4. ホーム画面に「松村式Stocksurfing」アイコンが追加される

### 朝のワークフロー

1. **7:30頃** — メール「📈 [日付] 場の判定 +XX 追い風」が届く（プレビューで確認）
2. **8:30頃** — 必要があれば、ホーム画面のアイコンをタップしてアプリで詳細確認
3. **9:20頃** — メール「⏰ 寄り後判定 +XX」が届く
4. **9:25頃** — アプリで「✅ GO」「⚠️ 慎重に」「🛑 見送り推奨」のいずれかを確認
5. 仕掛けると判断したら、証券会社アプリで発注

### 売買ルール

- 損切：エントリー -2.0%（必ず逆指値設定）
- 利確①：+3% で半分利確
- 利確②：+5% で残り利確 or 引き上げトレール
- 同時保有：最大3銘柄
- 1銘柄あたり：総資金の20%以下
- 場が±20以内：様子見推奨

## トラブル時の対応

- **メールが来ない**：GitHub Actions の Run履歴を確認（リポジトリの「Actions」タブ）
- **アプリが古いデータ表示**：iPhone Safari でリロード（更新ボタン）
- **🔄自動取得 をタップしても変わらない**：data.json がまだ更新されていない可能性。次の定時実行を待つ

## ライセンス・免責

このアプリはトレード判断補助ツールです。最終的な売買判断はご自身の責任で行ってください。
