# Railway デプロイガイド

このガイドでは、HWB Discord BotをRailwayにデプロイする手順を説明します。

## 📋 前提条件

- GitHubアカウント
- Railwayアカウント（[railway.app](https://railway.app)で作成）
- Discord Bot Token

## 🚀 デプロイ手順

### 1. GitHubリポジトリの準備

1. このプロジェクトをGitHubにプッシュ:
```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/yourusername/hwb-discord-bot.git
git push -u origin main
```

**重要**: `.env`ファイルは絶対にコミットしないでください！

### 2. Railwayプロジェクトの作成

1. [Railway Dashboard](https://railway.app/dashboard)にログイン
2. 「New Project」をクリック
3. 「Deploy from GitHub repo」を選択
4. GitHubアカウントを連携（初回のみ）
5. リポジトリ「hwb-discord-bot」を選択

### 3. 環境変数の設定

Railwayダッシュボードで以下の環境変数を設定:

1. プロジェクトを選択
2. 「Variables」タブをクリック
3. 「+ New Variable」で以下を追加:

```bash
# 必須
DISCORD_BOT_TOKEN=your_discord_bot_token_here
BOT_CHANNEL_NAME=hwb-signal-alerts

# 投稿設定
POST_SUMMARY=true              # サマリー投稿
POST_STRATEGY1_ALERTS=false    # 戦略1個別アラート
POST_STRATEGY2_ALERTS=false    # 戦略2個別アラート

# HWB戦略パラメータ
PROXIMITY_PERCENTAGE=0.05
FVG_ZONE_PROXIMITY=0.10
BREAKOUT_THRESHOLD=0.001
MA_PERIOD=200
SETUP_LOOKBACK_DAYS=60
FVG_SEARCH_DAYS=30

# パフォーマンス設定
BATCH_SIZE=50
MAX_WORKERS=10
CACHE_EXPIRY_HOURS=12

# リスク管理（オプション）
STOP_LOSS_PERCENTAGE=0.02
TARGET_PROFIT_PERCENTAGE=0.05
```

### 4. デプロイ

1. 環境変数を設定すると自動的にデプロイが開始されます
2. 「Deployments」タブで進行状況を確認
3. ログで起動を確認:
   ```
   [Bot名] がログインしました！
   監視銘柄数: 7,XXX
   ```

### 5. 確認

1. Discordサーバーで`#hwb-signal-alerts`チャンネルが作成されているか確認
2. `!status`コマンドでBotの状態を確認

## 🔧 メンテナンス

### ログの確認
- Railwayダッシュボード → プロジェクト → 「Logs」タブ

### 再起動
- Railwayダッシュボード → プロジェクト → 「Settings」 → 「Restart」

### 環境変数の更新
- 「Variables」タブで変更後、自動的に再デプロイされます

## ⚡ パフォーマンス調整

Railwayの無料プランには制限があるため、以下の調整を推奨:

### 無料プラン（$5クレジット/月）の場合
```bash
# 処理を軽くする設定
BATCH_SIZE=20
MAX_WORKERS=5
CACHE_EXPIRY_HOURS=24
```

### 有料プラン（$20/月〜）の場合
```bash
# 高速処理設定
BATCH_SIZE=100
MAX_WORKERS=20
CACHE_EXPIRY_HOURS=12
```

## 🚨 トラブルシューティング

### 「メモリ不足」エラー
- `BATCH_SIZE`と`MAX_WORKERS`を減らす
- 有料プランにアップグレード

### 「Rate limit」エラー
- yfinanceのレート制限です
- `BATCH_SIZE`を小さくする（10-20）

### Botがオフラインになる
- Railwayの無料プランは500時間/月の制限があります
- 使用時間を確認: Dashboard → Usage

### デプロイが失敗する
1. ログを確認
2. `requirements.txt`の依存関係を確認
3. Pythonバージョンを確認（`runtime.txt`）

## 💰 コスト見積もり

- **無料プラン**: 月500時間まで無料（約20日分）
- **Hobby Plan ($5/月)**: 十分な実行時間
- **Pro Plan ($20/月)**: 高性能、無制限実行

24時間365日稼働の場合:
- 月間実行時間: 約720時間
- 推奨: Hobby Plan以上

## 📊 監視とアラート

### Railwayの監視機能
- CPU使用率
- メモリ使用量
- ネットワークI/O

### 外部監視（オプション）
- [UptimeRobot](https://uptimerobot.com/): 無料の死活監視
- Discord Webhookでダウン通知

## 🔄 自動デプロイ設定

GitHubにプッシュすると自動的にデプロイされます:

1. Railway Dashboard → Settings
2. 「GitHub Trigger」が有効になっていることを確認
3. ブランチ: `main`（デフォルト）

```bash
# コード更新後
git add .
git commit -m "Update bot settings"
git push origin main
# → 自動的にRailwayでデプロイ開始
```

## 📝 ベストプラクティス

1. **環境変数は必ずRailwayで設定**
   - `.env`ファイルはローカル開発のみ
   - 本番環境の秘密情報は絶対にコミットしない

2. **定期的なログ確認**
   - エラーや警告を早期発見
   - パフォーマンスの最適化

3. **バックアップ**
   - 重要な設定はドキュメント化
   - 環境変数の値は別途保管

4. **段階的なデプロイ**
   - まず少ない銘柄数でテスト
   - 徐々にスケールアップ

---

質問や問題がある場合は、[Railway Discord](https://discord.gg/railway)または[公式ドキュメント](https://docs.railway.app/)を参照してください。
