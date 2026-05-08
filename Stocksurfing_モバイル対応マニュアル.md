# 松村式Stocksurfing モバイル対応マニュアル

> 出張先・移動中など、PCが使えない時に iPhone から運用・トラブル対応するための手順書

最終更新: 2026/05/08（v3.2 リリース時）

---

## 1. 想定する状況

| 状況 | 対応場所 |
|---|---|
| 朝7:30 のメールが届かない | iPhone Safari（cron-job.org） |
| 9:20 寄り後判定が届かない | iPhone Safari（cron-job.org） |
| 23:00 答え合わせが届かない | iPhone Safari（cron-job.org） |
| index.html の軽微な修正 | iPhone Safari（GitHub.dev） |
| ジョブの一時停止 / スケジュール変更 | iPhone Safari（cron-job.org） |
| 監視 20銘柄の追加・削除 | 奥様の iPhone PWA で「銘柄編集」タブ |
| 重大な故障 | このマニュアルに沿って自分で対応、または帰宅後に対応 |

---

## 2. 必要なアプリ・ブックマーク

### 2-1. iPhone App Store からインストール

- **GitHub Mobile**（App Store で「GitHub」検索、公式アプリ）
  - 用途：Actions のログ確認、Issue 管理、リポジトリ閲覧
  - 初回ログイン：matsumuratantei@gmail.com（GitHub と同じアカウント）

### 2-2. Safari ブックマークに必須登録

以下4つを Safari でブックマーク（or ホーム画面に追加）してください。

| 名前 | URL | 用途 |
|---|---|---|
| Stocksurfing アプリ | https://matsumuratantei-stocksurfing.github.io/Stocksurfing/ | 奥様と同じPWA |
| GitHub Actions | https://github.com/matsumuratantei-Stocksurfing/Stocksurfing/actions | ワークフロー実行ログ確認 |
| cron-job.org | https://console.cron-job.org/jobs | スケジュール3ジョブの管理 |
| GitHub Edit | https://github.dev/matsumuratantei-Stocksurfing/Stocksurfing | ブラウザ版 VS Code（コード編集） |

### 2-3. パスワード保管

iPhone のパスワードマネージャー（iCloud キーチェーンまたは別アプリ）に以下を保存：

- **GitHub**：matsumuratantei-Stocksurfing アカウント
- **cron-job.org**：matsumuratantei@gmail.com アカウント

⚠️ Gmail App Password・GitHub PAT などのトークン類は、絶対に SMS / Slack / メール本文に貼らないこと。漏洩したら即 Revoke。

---

## 3. 初回セットアップ

### 3-1. GitHub Mobile のインストール

1. App Store で「GitHub」検索
2. 緑の Octocat アイコンの公式アプリをインストール
3. 起動して Sign in
4. ブラウザに飛ぶ → matsumuratantei-Stocksurfing アカウントでログイン
5. 「Authorize」で許可
6. アプリに戻る → ホーム画面表示

### 3-2. ホーム画面に追加すべきアイコン

iPhone ホーム画面に以下4つ並べると、緊急時1タップで対応できます：

1. **Stocksurfing**（PWA、青いアイコン）
2. **GitHub Mobile**（緑のOctocat）
3. **Safari ブックマーク → cron-job.org**
4. **Safari ブックマーク → GitHub Actions**

PWA の追加方法：Safari で URL を開く → 共有ボタン（□↑）→「ホーム画面に追加」

---

## 4. トラブル対応フロー

### 4-1. 朝7:30 / 9:20 / 23:00 のいずれかメールが届かない

**ステップ1：Gmail で確認**

iPhone の Gmail アプリで、該当時刻の前後30分以内に「Stocksurfing」または「matsumuratantei@gmail.com（自分宛）」を検索。届いていたら問題なし（迷惑メール振り分けの可能性も確認）。

**ステップ2：cron-job.org で確認**

1. Safari で `console.cron-job.org/jobs` を開く
2. 該当のジョブ（朝7:30 / 9:20 / 23:00）の Last execution を確認：
   - **Successful (204)** → cron-job.org から GitHub への呼び出しは成功。Github Actions側の問題（ステップ3へ）
   - **Failed (HTTP error)** → cron-job.org → GitHub の認証失敗。PAT の期限切れ、または権限変更の可能性（ステップ4へ）
   - **未実行（Last execution が空）** → スケジュール時刻を過ぎたのに動いていない。cron-job.org のサーバー障害の可能性（ステップ5へ）

**ステップ3：GitHub Actions で確認**

1. GitHub Mobile を開く
2. リポジトリ → Stocksurfing → Actions タブ
3. 最新の「Fetch market data and notify」または「Verify predictions (nightly)」をタップ
4. ログを確認：
   - ✅ 緑：成功しているのにメールが来ない → Gmail App Password の期限切れの可能性
   - ❌ 赤：エラー詳細を確認、必要なら再実行（Re-run failed jobs）

**ステップ4：PAT の期限切れ確認**

1. Safari で `github.com/settings/personal-access-tokens` を開く
2. `cron-job-stocksurfing` の Expires 日付を確認
3. 期限切れなら Regenerate token → 新しい PAT を cron-job.org の各ジョブの Authorization Header に貼り直し（3ジョブとも）

PAT の期限：**2027年5月7日まで**（次回更新タイミング：2027年4月頃に通知メール来るはず）

**ステップ5：cron-job.org サーバー障害**

cron-job.org の Status ページで状態確認：https://status.cron-job.org/

---

### 4-2. 特定銘柄の追加・削除（奥様の iPhone PWA）

奥様の iPhone PWA には「銘柄編集」タブがあるので、奥様自身が編集可能です。設定はその端末の localStorage に保存されるので、奥様の iPhone でしか反映されません（クラウド同期なし）。

奥様の好み変更がある場合は、本人に PWA で直接編集してもらうのが速い。

---

### 4-3. index.html の軽微な修正（GitHub.dev）

iPhone でコードを直接編集できる方法：

1. Safari で `github.dev/matsumuratantei-Stocksurfing/Stocksurfing` を開く
2. ブラウザ版 VS Code が開く
3. 左サイドバーの index.html をタップ
4. 編集 → 左サイドバーの Source Control（分岐アイコン）→ Commit message 入力 → ✓ Commit & Push

⚠️ iPhone の小さい画面ではコード編集はミスしやすい。緊急対応のみに使用。本格的な変更は帰宅後にPCで。

---

### 4-4. cron-job.org ジョブの一時停止 / 再開

例：「奥様が出張で取引しないので、明日3日間メール止めたい」

1. Safari で `console.cron-job.org/jobs` を開く
2. 該当ジョブの **チェックボックス選択** → ACTIONS → **Disable**（無効化）
3. 復帰時は同じ手順で **Enable**

または、ジョブの Schedule を一時的に「2030年1月1日」のような未来日にする方法もあります（Schedule expires チェック）。

---

## 5. 緊急連絡・参照リンク

- リポジトリ：https://github.com/matsumuratantei-Stocksurfing/Stocksurfing
- アプリURL：https://matsumuratantei-stocksurfing.github.io/Stocksurfing/
- cron-job.org：https://console.cron-job.org/
- GitHub PAT 管理：https://github.com/settings/personal-access-tokens
- GitHub.dev（リポジトリ直接編集）：https://github.dev/matsumuratantei-Stocksurfing/Stocksurfing

---

## 6. 月次・四半期メンテナンス

| 時期 | 作業 |
|---|---|
| 毎月1日 | cron-job.org の History を見て、3ジョブとも 95%以上の成功率か確認 |
| 毎月1日 | GitHub Actions の月間使用時間を確認（無料枠2000分/月以内か） |
| 毎四半期 | Gmail App Password の有効期限確認・更新 |
| 2027年4月頃 | GitHub PAT 更新（期限：2027/5/7） |

---

## 7. PWA 更新手順（奥様向け説明用）

新バージョン（v3.3 など）を push した後、奥様の iPhone PWA に反映させる方法：

1. ホーム画面のアイコンを長押し → 削除
2. Safari で `https://matsumuratantei-stocksurfing.github.io/Stocksurfing/` を開く
3. 共有ボタン（□↑）→「ホーム画面に追加」
4. 新しいアイコンができるのでタップ
5. フッターのバージョン番号で確認（v3.3 など）

メジャーアップデート時は奥様にこの手順を伝える。

---

以上
