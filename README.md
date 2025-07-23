# HWB Strategy Discord Bot

S&P500銘柄に対してHWB（Hana Wall Break）戦略のシグナルを検出し、Discordに通知するBotです。

## 🎯 戦略概要

### ルール①：長期トレンドの確認
- 週足の終値が週足200SMAを上回っている

### ルール②：セットアップの検出
- 日足200SMAと日足200EMAで形成されるゾーン（帯）の間に、始値と終値の両方が収まるローソク足が出現

### ルール③：買い圧力の確認（戦略1アラート）
- セットアップ後にBullish FVG（Fair Value Gap）が形成される
- 以下のいずれかの条件を満たす：
  - **条件A**: FVGの3本目のローソク足の始値or終値が、日足200SMA/EMAの±5%範囲内
  - **条件B**: FVGゾーンの中心が、日足200SMA/EMAの±10%範囲内

### ルール④：エントリートリガー（戦略2アラート）
- **Part A**: セットアップ翌日から昨日までの最高値をレジスタンスとする
- **Part B**: FVG下限がサポートとして機能し、現在の終値がレジスタンスを0.1%以上ブレイクアウト

## 🚀 セットアップ

### 1. 前提条件
- Python 3.8以上
- Discord Bot Token

### 2. インストール
```bash
# リポジトリのクローン
git clone https://github.com/yourusername/hwb-discord-bot.git
cd hwb-discord-bot

# 仮想環境の作成
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 依存関係のインストール
pip install -r requirements.txt
```

### 3. 環境変数の設定
`.env.example`を`.env`にコピーして編集：
```bash
cp .env.example .env
```

`.env`ファイルを開き、Discord Bot Tokenを設定：
```
DISCORD_BOT_TOKEN="あなたのトークン"
```

### 4. Discordサーバーの準備
- Botを招待するサーバーを用意
- Bot権限：チャンネル作成、メッセージ送信、ファイル添付

## 📅 実行タイミング

Botは以下のスケジュールで自動実行されます：

- **夏時間（3月第2日曜〜11月第1日曜）**: 毎日 5:15 JST（16:15 ET）
- **冬時間（11月第1日曜〜3月第2日曜）**: 毎日 6:15 JST（16:15 ET）

米国市場終了（16:00 ET）の15分後に自動でスキャンを開始します。

## 🤖 Bot実行

```bash
# Botを起動
python bot_hwb.py
```

Botが起動すると：
1. 自動的に`#hwb-signal-alerts`チャンネルを作成（存在しない場合）
2. 毎日の指定時刻にS&P500全銘柄をスキャン
3. 条件を満たす銘柄をDiscordに通知

## 📱 Discordコマンド

| コマンド | 説明 | 使用例 |
|---------|------|--------|
| `!status` | Botのステータスと次回スキャン時刻を表示 | `!status` |
| `!check <SYMBOL>` | 特定の銘柄をチェック | `!check AAPL` |
| `!scan` | 手動でスキャンを実行（管理者のみ） | `!scan` |

## 📊 アラート内容

### 戦略1アラート（FVG検出）
- セットアップ情報（日付、MA値）
- FVG情報（形成日、ギャップサイズ）
- MA近接条件の充足状況
- チャート画像

### 戦略2アラート（ブレイクアウト）
- ブレイクアウト価格とレジスタンス
- セットアップからの経過日数
- 推奨トレード戦略（エントリー、ストップロス、目標価格）
- リスクリワード比
- チャート画像

## 🔧 カスタマイズ

`.env`ファイルでパラメータを調整可能：

```bash
# MA近接条件（ルール③）
PROXIMITY_PERCENTAGE=0.05      # 条件A: ±5%
FVG_ZONE_PROXIMITY=0.10       # 条件B: ±10%

# ブレイクアウト閾値（ルール④）
BREAKOUT_THRESHOLD=0.001      # 0.1%以上の上昇

# その他のパラメータ
MA_PERIOD=200                 # 移動平均期間
SETUP_LOOKBACK_DAYS=60        # セットアップ検索期間
FVG_SEARCH_DAYS=30           # FVG検索期間
```

## 📈 チャート表示

各アラートには以下を含むチャートが添付されます：
- 日足ローソク足（6ヶ月分）
- 日足200SMA（明るい紫）
- 日足200EMA（紫）
- セットアップゾーン（黄色）
- FVGゾーン（緑色）
- ボリューム

## ⚠️ 注意事項

- このBotは教育・情報提供目的です
- 投資判断は自己責任で行ってください
- yfinanceのレート制限に注意（大量アクセスは避ける）
- 市場の休場日は考慮されていません

## 🐛 トラブルシューティング

### yfinanceエラー
```bash
pip install --upgrade yfinance curl_cffi
```

### Discordに接続できない
- Bot Tokenが正しいか確認
- Botに必要な権限があるか確認

### アラートが来ない
- `!status`でBotの状態を確認
- コンソールログでエラーを確認
- 市場が開いている日か確認

## 📝 ログ

Botのログはコンソールに出力されます。詳細なログが必要な場合は、ログファイルへのリダイレクトを推奨：

```bash
python bot_hwb.py > hwb_bot.log 2>&1
```

## 🤝 貢献

バグ報告や機能提案は、GitHubのIssueまたはPull Requestでお願いします。

## 📄 ライセンス

MIT License