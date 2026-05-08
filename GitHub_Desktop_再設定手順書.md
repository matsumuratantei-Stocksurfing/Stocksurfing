# GitHub Desktop 再設定手順書

> OneDrive 撤去後、GitHub Desktop のリポジトリを C:\matsumura\Stocksurfing\ に再構築する手順

⚠️ 「OneDrive_完全撤去手順書.md」を完了してから実施してください。

---

## 目的

OneDrive 撤去前は、GitHub Desktop のリポジトリが `C:\Users\matsu\OneDrive\ドキュメント\GitHub\Stocksurfing\` にクローンされていました。OneDrive を撤去した後は、OneDrive 外の `C:\matsumura\Stocksurfing\` にクローンを再構築します。

これで以後の編集 → push が、OneDrive 干渉なしで安定動作します。

---

## ステップ1：旧クローンを GitHub Desktop から削除

1. GitHub Desktop を起動
2. 上部メニュー **「File」** → **「Remove repository」**（Ctrl+R）
3. 確認ダイアログ：
   - 「Also move this repository to the Recycle Bin」にチェック ✅
   - **「Remove」** クリック

これで旧クローン（OneDrive配下、すでに撤去済みなら空のフォルダ）が削除されます。

---

## ステップ2：新規クローン（C:\matsumura\Stocksurfing\）

1. GitHub Desktop の画面で **「Clone a repository from the Internet...」**
2. **「GitHub.com」** タブで `matsumuratantei-Stocksurfing/Stocksurfing` を選択
3. **Local path** の右の **「Choose...」** をクリック
4. フォルダ選択ダイアログで **`C:\matsumura`** を選択
5. Local path が **`C:\matsumura\Stocksurfing`** になる
6. **「Clone」** クリック
7. クローン完了（1-2分）

---

## ステップ3：動作確認

1. GitHub Desktop の「Current repository」が **「Stocksurfing」** になっている
2. エクスプローラーで `C:\matsumura\Stocksurfing\` を開く
3. index.html / fetch_data.py / data.json などがあること確認
4. フッターに「v3.2」と書かれていることを確認（Edge等で右クリック → プログラムから開く → メモ帳 で先頭少し見るか、Cowork セッション再開後に Claude に確認してもらう）

---

## ステップ4（任意）：ローカル運用ファイルとの統合

C:\matsumura\ には以下が既にあります：

- `松村式Stocksurfing_v3.2.html` ← ローカル動作確認用（v3.2 反映済み）
- `Stocksurfing_v3.2起動.bat` ← ローカル起動 bat
- `data.json` ← ローカル fetch_data.py の出力
- `fetch_data.py` ← ローカル取得スクリプト
- `Stocksurfing_v2.3.3.html` ← 旧版バックアップ

これらと、新しい `C:\matsumura\Stocksurfing\` （Git管理リポジトリ）は **別フォルダ** として共存します。

役割分担：
- `C:\matsumura\` 直下のファイル → ローカル運用専用（クラウドと無関係）
- `C:\matsumura\Stocksurfing\` 内 → GitHub と同期するファイル群

混乱を避けるため、運用上は以下のルールを推奨：

- **クラウド版 v3.2 を編集したい時** → `C:\matsumura\Stocksurfing\index.html` を編集 → GitHub Desktop で commit & push
- **ローカル動作確認したい時** → `C:\matsumura\松村式Stocksurfing_v3.2.html` を起動bat 経由で実行
- 両方を同期させたい場合は、編集後に手動コピーで反映

---

## 以後の標準ワークフロー（v3.3 以降の更新時）

1. C:\matsumura\Stocksurfing\index.html を編集（私と一緒に）
2. GitHub Desktop の Changes タブに変更が表示される
3. Summary に「v3.3: 機能追加内容」と入力
4. **「Commit to main」** クリック
5. 上部 **「Push origin」** クリック
6. 1-2分後に GitHub Pages 反映
7. 奥様の iPhone PWA はアイコン作り直しで v3.3 が見られる

---

## トラブル時

### Q. クローン中に「Authentication failed」
→ GitHub Desktop で再ログインが必要。File → Options → Accounts → Sign Out → Sign In

### Q. クローンが終わらない（フリーズ）
→ ネットワーク接続確認。GitHub の Status 確認：https://www.githubstatus.com/

### Q. リポジトリが見つからない
→ matsumuratantei-Stocksurfing アカウントでログインしているか確認

---

## 完了確認チェックリスト

- [ ] `C:\matsumura\Stocksurfing\` フォルダが存在する
- [ ] そこに index.html, fetch_data.py, data.json などがある
- [ ] GitHub Desktop の Current repository が「Stocksurfing」
- [ ] index.html を試しに編集（footer など軽微な変更）→ Changes タブに表示される
- [ ] commit & push して GitHub の commits に反映される

すべてOKなら GitHub Desktop の再設定完了！

以上
