# ワンクリックデプロイ

## Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template/hwb-bot?referralCode=YOUR_REFERRAL_CODE)

上記のボタンをREADME.mdに追加することで、ワンクリックでRailwayにデプロイできます。

### テンプレート作成手順

1. GitHubリポジトリを公開
2. Railwayにログイン
3. [Railway Templates](https://railway.app/templates)にアクセス
4. 「Create Template」をクリック
5. リポジトリURLを入力
6. 必要な環境変数を定義:
   - `DISCORD_BOT_TOKEN` (required)
   - `BOT_CHANNEL_NAME` (default: "hwb-signal-alerts")
   - その他のオプション変数

### ボタンのカスタマイズ

```markdown
[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template/YOUR_TEMPLATE_ID)
```

または、リポジトリURLを直接使用:

```markdown
[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template?template=https%3A%2F%2Fgithub.com%2Fyourusername%2Fhwb-discord-bot)
```

## その他のプラットフォーム

### Heroku（オプション）
```markdown
[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/yourusername/hwb-discord-bot)
```

### Render（オプション）
```markdown
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/yourusername/hwb-discord-bot)
```
