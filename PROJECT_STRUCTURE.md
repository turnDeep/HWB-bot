# プロジェクト構造

```
hwb-discord-bot/
├── bot_hwb.py              # メインのBotスクリプト
├── requirements.txt        # Python依存関係
├── runtime.txt            # Pythonバージョン指定
├── Procfile               # プロセス定義
├── railway.toml           # Railway設定
├── railway.json           # Railway詳細設定（オプション）
├── nixpacks.toml          # ビルド設定（オプション）
├── .env.example           # 環境変数テンプレート
├── .env                   # ローカル環境変数（gitignore）
├── .gitignore             # Git除外ファイル
├── .dockerignore          # Docker除外ファイル
├── README.md              # プロジェクト説明
├── README_RAILWAY.md      # Railwayデプロイガイド
├── DEPLOY_BUTTON.md       # デプロイボタン設定
├── PROJECT_STRUCTURE.md   # このファイル
├── CHANGELOG.md           # 変更履歴
└── cache/                 # キャッシュディレクトリ（gitignore）
```

## ファイル説明

### 必須ファイル
- `bot_hwb.py`: HWB戦略のDiscord Bot本体
- `requirements.txt`: 必要なPythonパッケージリスト
- `.env.example`: 環境変数のテンプレート

### Railway用ファイル
- `railway.toml`: Railwayのビルド・デプロイ設定
- `Procfile`: プロセスタイプとコマンドの定義
- `runtime.txt`: Pythonバージョンの指定

### オプションファイル
- `railway.json`: より詳細なRailway設定
- `nixpacks.toml`: カスタムビルド設定
- `.dockerignore`: Dockerビルド時の除外ファイル

### ドキュメント
- `README.md`: プロジェクトの概要と使い方
- `README_RAILWAY.md`: Railway特有のデプロイ手順
- `DEPLOY_BUTTON.md`: ワンクリックデプロイの設定
- `CHANGELOG.md`: バージョンごとの変更履歴

## デプロイチェックリスト

- [ ] `.env`ファイルが`.gitignore`に含まれている
- [ ] `requirements.txt`が最新
- [ ] `runtime.txt`のPythonバージョンが正しい
- [ ] `railway.toml`の設定が適切
- [ ] Discord Bot Tokenを取得済み
- [ ] 投稿設定（POST_SUMMARY等）を確認
- [ ] GitHubリポジトリにプッシュ済み
- [ ] Railwayアカウント作成済み

## 環境変数の重要な設定

### 投稿制御
- `POST_SUMMARY`: サマリー投稿のON/OFF（デフォルト: true）
- `POST_STRATEGY1_ALERTS`: 戦略1個別アラートのON/OFF（デフォルト: false）
- `POST_STRATEGY2_ALERTS`: 戦略2個別アラートのON/OFF（デフォルト: false）

これらの設定により、必要な情報のみをDiscordに投稿し、チャンネルの混雑を防ぐことができます。
