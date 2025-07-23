import discord
from discord.ext import commands, tasks
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time
import asyncio
import os
from dotenv import load_dotenv
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import mplfinance as mpf
from io import BytesIO
import warnings
import pytz
import sys
import json
from curl_cffi import requests
warnings.filterwarnings("ignore")

# .envファイルから環境変数を読み込み
load_dotenv()

# 環境変数から設定を読み込み
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not DISCORD_BOT_TOKEN:
    raise ValueError("DISCORD_BOT_TOKENが設定されていません。.envファイルを確認してください。")

# Bot設定
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# 設定項目
BOT_CHANNEL_NAME = os.getenv("BOT_CHANNEL_NAME", "hwb-signal-alerts")

# HWB戦略のパラメータ（strategy_params.jsonから）
PROXIMITY_PERCENTAGE = 0.05  # ルール③条件Aの許容範囲（5%）
FVG_ZONE_PROXIMITY = 0.10   # ルール③条件Bの許容範囲（10%）
BREAKOUT_THRESHOLD = 0.001  # ブレイクアウトの閾値（0.1%）

# グローバル変数
watched_symbols = set()
setup_alerts = {}  # ルール②のセットアップを記録
fvg_alerts = {}    # ルール③のFVG検出を記録
breakout_alerts = {} # ルール④のブレイクアウトを記録
server_configs = {}

# タイムゾーン設定
ET = pytz.timezone("US/Eastern")
JST = pytz.timezone("Asia/Tokyo")

def get_sp500_symbols():
    """S&P500の銘柄リストを取得"""
    try:
        sp500 = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
        symbols = sp500["Symbol"].str.replace(".", "-", regex=False).tolist()
        print(f"S&P500銘柄数: {len(symbols)}")
        return symbols
    except Exception as e:
        print(f"S&P500リスト取得エラー: {e}")
        return ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "JPM", "JNJ"]

def get_market_close_time_et():
    """米国市場の終了時刻（ET）を取得"""
    now_et = datetime.now(ET)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_close

def get_scan_time():
    """スキャン実行時刻を計算（市場終了15分後）"""
    market_close = get_market_close_time_et()
    scan_time = market_close + timedelta(minutes=15)
    return scan_time

class HWBAnalyzer:
    """HWB戦略の分析クラス"""
    
    @staticmethod
    def get_stock_data(symbol, period="2y"):
        """株価データを取得（日足・週足）"""
        session = requests.Session(impersonate="safari15_5")
        try:
            stock = yf.Ticker(symbol, session=session)
            
            # 日足データ
            df_daily = stock.history(period=period, interval="1d")
            if df_daily.empty:
                return None, None
            df_daily.index = df_daily.index.tz_localize(None)
            
            # 週足データ
            df_weekly = stock.history(period="5y", interval="1wk")
            if df_weekly.empty:
                return None, None
            df_weekly.index = df_weekly.index.tz_localize(None)
            
            return df_daily, df_weekly
        except Exception as e:
            print(f"データ取得エラー ({symbol}): {e}")
            return None, None
    
    @staticmethod
    def prepare_data(df_daily, df_weekly):
        """データに移動平均を追加"""
        # 日足200SMAとEMA
        df_daily['SMA200'] = df_daily['Close'].rolling(window=200, min_periods=200).mean()
        df_daily['EMA200'] = df_daily['Close'].ewm(span=200, adjust=False, min_periods=200).mean()
        
        # 週足200SMA
        df_weekly['SMA200'] = df_weekly['Close'].rolling(window=200, min_periods=200).mean()
        
        # 週足SMAを日足データに結合
        df_daily['Weekly_SMA200'] = np.nan
        for idx, row in df_weekly.iterrows():
            if pd.notna(row['SMA200']):
                week_start = idx - pd.Timedelta(days=idx.weekday())
                week_end = week_start + pd.Timedelta(days=6)
                mask = (df_daily.index >= week_start) & (df_daily.index <= week_end)
                if mask.any():
                    df_daily.loc[mask, 'Weekly_SMA200'] = row['SMA200']
        
        df_daily['Weekly_SMA200'] = df_daily['Weekly_SMA200'].ffill()
        return df_daily, df_weekly
    
    @staticmethod
    def check_rule1(df_daily):
        """ルール①: 週足の終値が週足200SMAを上回っているか"""
        if 'Weekly_SMA200' not in df_daily.columns:
            return False
        
        latest = df_daily.iloc[-1]
        return (pd.notna(latest['Weekly_SMA200']) and 
                pd.notna(latest['Close']) and 
                latest['Close'] > latest['Weekly_SMA200'])
    
    @staticmethod
    def find_rule2_setups(df_daily, lookback_days=30):
        """ルール②: SMA/EMAゾーン内のローソク足を検出"""
        setups = []
        valid_data = df_daily[(df_daily['SMA200'].notna()) & (df_daily['EMA200'].notna())].tail(lookback_days)
        
        for i in range(len(valid_data)):
            row = valid_data.iloc[i]
            zone_upper = max(row['SMA200'], row['EMA200'])
            zone_lower = min(row['SMA200'], row['EMA200'])
            
            # 始値と終値の両方がゾーン内
            if (zone_lower <= row['Open'] <= zone_upper and 
                zone_lower <= row['Close'] <= zone_upper):
                
                # ルール①の確認
                if pd.notna(row.get('Weekly_SMA200')) and row['Close'] > row['Weekly_SMA200']:
                    setups.append({
                        'date': valid_data.index[i],
                        'open': row['Open'],
                        'close': row['Close'],
                        'high': row['High'],
                        'low': row['Low'],
                        'sma200': row['SMA200'],
                        'ema200': row['EMA200'],
                        'zone_upper': zone_upper,
                        'zone_lower': zone_lower
                    })
        
        return setups
    
    @staticmethod
    def detect_fvg_after_setup(df_daily, setup_date, max_days_after=20):
        """ルール③: セットアップ後のFVGを検出"""
        fvg_list = []
        
        try:
            setup_idx = df_daily.index.get_loc(setup_date)
        except KeyError:
            return fvg_list
        
        search_end = min(setup_idx + max_days_after, len(df_daily) - 1)
        
        for i in range(setup_idx + 3, search_end + 1):
            candle_1 = df_daily.iloc[i-2]
            candle_2 = df_daily.iloc[i-1]
            candle_3 = df_daily.iloc[i]
            
            # Bullish FVG: 1本目の高値 < 3本目の安値
            gap = candle_3['Low'] - candle_1['High']
            
            if gap > 0 and gap / candle_1['High'] > 0.001:  # 0.1%以上のギャップ
                # MA近接条件をチェック
                if HWBAnalyzer._check_fvg_ma_proximity(candle_3, candle_1):
                    fvg = {
                        'start_date': df_daily.index[i-2],
                        'end_date': df_daily.index[i],
                        'formation_date': df_daily.index[i],
                        'upper_bound': candle_3['Low'],
                        'lower_bound': candle_1['High'],
                        'gap_size': gap,
                        'gap_percentage': gap / candle_1['High'] * 100,
                        'third_candle_open': candle_3['Open'],
                        'third_candle_close': candle_3['Close']
                    }
                    fvg_list.append(fvg)
        
        return fvg_list
    
    @staticmethod
    def _check_fvg_ma_proximity(candle_3, candle_1):
        """FVGがMA近接条件を満たすかチェック"""
        if pd.isna(candle_3.get('SMA200')) or pd.isna(candle_3.get('EMA200')):
            return False
        
        # 条件A: 3本目の始値or終値がMA±5%以内
        for price in [candle_3['Open'], candle_3['Close']]:
            sma_deviation = abs(price - candle_3['SMA200']) / candle_3['SMA200']
            ema_deviation = abs(price - candle_3['EMA200']) / candle_3['EMA200']
            if sma_deviation <= PROXIMITY_PERCENTAGE or ema_deviation <= PROXIMITY_PERCENTAGE:
                return True
        
        # 条件B: FVGゾーンの中心がMA±10%以内
        fvg_center = (candle_1['High'] + candle_3['Low']) / 2
        sma_deviation = abs(fvg_center - candle_3['SMA200']) / candle_3['SMA200']
        ema_deviation = abs(fvg_center - candle_3['EMA200']) / candle_3['EMA200']
        
        return sma_deviation <= FVG_ZONE_PROXIMITY or ema_deviation <= FVG_ZONE_PROXIMITY
    
    @staticmethod
    def check_breakout(df_daily, setup, fvg):
        """ルール④: ブレイクアウト条件をチェック"""
        setup_date = setup['date']
        fvg_formation_date = fvg['formation_date']
        fvg_lower = fvg['lower_bound']
        
        try:
            setup_idx = df_daily.index.get_loc(setup_date)
            fvg_idx = df_daily.index.get_loc(fvg_formation_date)
        except KeyError:
            return None
        
        # 最新データを確認
        latest_idx = len(df_daily) - 1
        if latest_idx <= fvg_idx:
            return None
        
        # レジスタンス計算（セットアップ翌日から昨日まで）
        resistance_start_idx = setup_idx + 1
        resistance_end_idx = latest_idx - 1
        
        if resistance_end_idx <= resistance_start_idx:
            return None
        
        resistance_high = df_daily.iloc[resistance_start_idx:resistance_end_idx + 1]['High'].max()
        
        # 現在の価格
        current = df_daily.iloc[-1]
        
        # FVG下限がサポートとして機能しているか
        post_fvg_lows = df_daily.iloc[fvg_idx + 1:]['Low']
        if (post_fvg_lows < fvg_lower).any():
            return None  # FVGが破られた
        
        # ブレイクアウト確認
        if current['Close'] > resistance_high * (1 + BREAKOUT_THRESHOLD):
            return {
                'breakout_date': df_daily.index[-1],
                'breakout_price': current['Close'],
                'resistance_price': resistance_high,
                'setup_info': setup,
                'fvg_info': fvg
            }
        
        return None

    @staticmethod
    def scan_symbol(symbol):
        """銘柄をスキャンしてHWBシグナルを検出"""
        df_daily, df_weekly = HWBAnalyzer.get_stock_data(symbol)
        if df_daily is None or df_weekly is None:
            return None
        
        df_daily, df_weekly = HWBAnalyzer.prepare_data(df_daily, df_weekly)
        
        # ルール①チェック
        if not HWBAnalyzer.check_rule1(df_daily):
            return None
        
        # ルール②セットアップを探す
        setups = HWBAnalyzer.find_rule2_setups(df_daily, lookback_days=60)
        if not setups:
            return None
        
        results = []
        
        for setup in setups:
            # ルール③FVG検出
            fvgs = HWBAnalyzer.detect_fvg_after_setup(df_daily, setup['date'])
            
            for fvg in fvgs:
                # ルール④ブレイクアウトチェック
                breakout = HWBAnalyzer.check_breakout(df_daily, setup, fvg)
                
                # 結果を収集
                if fvg:  # FVGが検出された（戦略1）
                    result = {
                        'symbol': symbol,
                        'signal_type': 's1_fvg_detected',
                        'setup': setup,
                        'fvg': fvg,
                        'current_price': df_daily['Close'].iloc[-1],
                        'daily_ma200': df_daily['SMA200'].iloc[-1],
                        'weekly_sma200': df_daily['Weekly_SMA200'].iloc[-1]
                    }
                    
                    if breakout:  # ブレイクアウトも発生（戦略2）
                        result['signal_type'] = 's2_breakout'
                        result['breakout'] = breakout
                    
                    results.append(result)
        
        return results

    @staticmethod
    def create_hwb_chart(symbol, setup_date=None, fvg_info=None, save_path=None):
        """HWB戦略のチャートを作成"""
        df_daily, df_weekly = HWBAnalyzer.get_stock_data(symbol, period="1y")
        if df_daily is None:
            return None
        
        df_daily, _ = HWBAnalyzer.prepare_data(df_daily, df_weekly)
        
        # チャート表示期間を設定
        if setup_date:
            center_date = pd.to_datetime(setup_date)
            start_date = center_date - pd.Timedelta(days=90)
            end_date = center_date + pd.Timedelta(days=90)
            df_plot = df_daily[(df_daily.index >= start_date) & (df_daily.index <= end_date)].copy()
        else:
            df_plot = df_daily.tail(180).copy()
        
        # mplfinanceスタイル設定
        mc = mpf.make_marketcolors(up='green', down='red', edge='inherit', 
                                   wick={'up':'green', 'down':'red'}, volume='in')
        s = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)
        
        # 追加プロット
        apds = []
        
        # 日足SMA200（明るい紫）
        if 'SMA200' in df_plot.columns and not df_plot['SMA200'].isna().all():
            apds.append(mpf.make_addplot(df_plot['SMA200'], color='#9370DB', width=2))
        
        # 日足EMA200（紫）
        if 'EMA200' in df_plot.columns and not df_plot['EMA200'].isna().all():
            apds.append(mpf.make_addplot(df_plot['EMA200'], color='purple', width=2))
        
        fig, axes = mpf.plot(df_plot, type='candle', style=s, volume=True, addplot=apds,
                             title=f'{symbol} - HWB Strategy Analysis', returnfig=True, 
                             figsize=(12, 8), panel_ratios=(3, 1))
        
        ax = axes[0]
        
        # セットアップゾーンをハイライト
        if setup_date and setup_date in df_plot.index:
            setup_idx = df_plot.index.get_loc(setup_date)
            ax.axvspan(setup_idx - 0.5, setup_idx + 0.5, alpha=0.3, color='yellow', zorder=0)
        
        # FVGを描画
        if fvg_info and fvg_info['start_date'] in df_plot.index:
            start_idx = df_plot.index.get_loc(fvg_info['start_date'])
            rect = patches.Rectangle((start_idx - 0.5, fvg_info['lower_bound']), 
                                     len(df_plot) - start_idx, 
                                     fvg_info['upper_bound'] - fvg_info['lower_bound'],
                                     linewidth=1, edgecolor='green', facecolor='green', alpha=0.2)
            ax.add_patch(rect)
        
        # 凡例
        ax.legend(['Daily SMA200', 'Daily EMA200'], loc='upper left')
        
        if save_path:
            plt.savefig(save_path)
            plt.close()
            return save_path
        else:
            buf = BytesIO()
            plt.savefig(buf, format='png')
            buf.seek(0)
            plt.close()
            return buf

# Embed作成関数
def create_hwb_fvg_alert_embed(result):
    """戦略1（ルール③: FVG検出）のアラート用Embed"""
    symbol = result["symbol"]
    setup = result["setup"]
    fvg = result["fvg"]
    
    try:
        company_name = yf.Ticker(symbol).info.get("longName", symbol)
    except:
        company_name = symbol
    
    embed = discord.Embed(
        title=f"📈 HWB戦略1: FVG形成検出 - {symbol}",
        description=f"**{company_name}** でセットアップ後のFVGを検出しました。",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    # セットアップ情報
    embed.add_field(
        name="📊 セットアップ情報（ルール②）",
        value=f"• 日付: `{setup['date'].strftime('%Y-%m-%d')}`\n"
              f"• SMA200: `${setup['sma200']:.2f}`\n"
              f"• EMA200: `${setup['ema200']:.2f}`\n"
              f"• ゾーン幅: `{abs(setup['zone_upper'] - setup['zone_lower']):.2f}`",
        inline=False
    )
    
    # FVG情報
    embed.add_field(
        name="🎯 FVG情報（ルール③）",
        value=f"• 形成日: `{fvg['formation_date'].strftime('%Y-%m-%d')}`\n"
              f"• 上限: `${fvg['upper_bound']:.2f}`\n"
              f"• 下限: `${fvg['lower_bound']:.2f}`\n"
              f"• ギャップサイズ: `{fvg['gap_percentage']:.2f}%`",
        inline=False
    )
    
    # 現在の状況
    embed.add_field(
        name="📍 現在の状況",
        value=f"• 現在価格: `${result['current_price']:.2f}`\n"
              f"• 週足SMA200: `${result['weekly_sma200']:.2f}` ✅\n"
              f"• MA近接条件: 満たしています ✅",
        inline=False
    )
    
    # 次のステップ
    embed.add_field(
        name="➡️ 次のステップ",
        value="レジスタンス突破（ルール④）を監視します。\n"
              "セットアップ翌日からの高値を超えるブレイクアウトを待ちます。",
        inline=False
    )
    
    embed.set_footer(text="HWB Strategy Alert System")
    
    return embed

def create_hwb_breakout_alert_embed(result):
    """戦略2（ルール④: ブレイクアウト）のアラート用Embed"""
    symbol = result["symbol"]
    breakout = result["breakout"]
    setup = result["setup"]
    fvg = result["fvg"]
    
    try:
        company_name = yf.Ticker(symbol).info.get("longName", symbol)
    except:
        company_name = symbol
    
    price_change = ((breakout['breakout_price'] - breakout['resistance_price']) / 
                    breakout['resistance_price']) * 100
    
    embed = discord.Embed(
        title=f"🚀 HWB戦略2: ブレイクアウト達成 - {symbol}",
        description=f"**{company_name}** がルール④の条件を満たし、エントリーシグナルが発生しました！",
        color=discord.Color.green(),
        timestamp=datetime.now()
    )
    
    # ブレイクアウト情報
    embed.add_field(
        name="💥 ブレイクアウト情報",
        value=f"• 現在価格: `${breakout['breakout_price']:.2f}`\n"
              f"• レジスタンス: `${breakout['resistance_price']:.2f}`\n"
              f"• 上昇率: `+{price_change:.1f}%`",
        inline=False
    )
    
    # セットアップからの経過
    days_since_setup = (breakout['breakout_date'] - setup['date']).days
    days_since_fvg = (breakout['breakout_date'] - fvg['formation_date']).days
    
    embed.add_field(
        name="⏱️ タイミング",
        value=f"• セットアップから: `{days_since_setup}日`\n"
              f"• FVG形成から: `{days_since_fvg}日`\n"
              f"• FVGサポート: `${fvg['lower_bound']:.2f}` (維持中✅)",
        inline=False
    )
    
    # トレード戦略
    stop_loss = fvg['lower_bound'] * 0.98
    target_1 = breakout['breakout_price'] * 1.05
    target_2 = breakout['breakout_price'] * 1.10
    
    embed.add_field(
        name="📋 推奨トレード戦略",
        value=f"• エントリー: `${breakout['breakout_price']:.2f}`\n"
              f"• ストップロス: `${stop_loss:.2f}` (FVG下限の2%下)\n"
              f"• 目標1: `${target_1:.2f}` (+5%)\n"
              f"• 目標2: `${target_2:.2f}` (+10%)",
        inline=False
    )
    
    # リスクリワード
    risk = breakout['breakout_price'] - stop_loss
    reward_1 = target_1 - breakout['breakout_price']
    rr_ratio = reward_1 / risk
    
    embed.add_field(
        name="⚖️ リスク管理",
        value=f"• リスク: `${risk:.2f}`\n"
              f"• リワード: `${reward_1:.2f}`\n"
              f"• R/R比: `1:{rr_ratio:.1f}`",
        inline=False
    )
    
    embed.set_footer(text="HWB Strategy Alert System - Trade at your own risk")
    
    return embed

# Bot機能
async def setup_guild(guild):
    """サーバーの初期設定"""
    alert_channel = None
    for channel in guild.text_channels:
        if channel.name == BOT_CHANNEL_NAME:
            alert_channel = channel
            break
    
    if not alert_channel:
        try:
            alert_channel = await guild.create_text_channel(
                name=BOT_CHANNEL_NAME,
                topic="📈 HWB Strategy Alerts - S&P500 Technical Analysis Signals"
            )
        except discord.Forbidden:
            print(f"チャンネル作成権限がありません: {guild.name}")
    
    server_configs[guild.id] = {
        "alert_channel": alert_channel,
        "enabled": True
    }
    
    if alert_channel:
        print(f"サーバー '{guild.name}' の設定完了。アラートチャンネル: #{alert_channel.name}")

async def scan_all_symbols():
    """全S&P500銘柄をスキャン"""
    alerts = []
    total = len(watched_symbols)
    processed = 0
    
    print(f"スキャン開始: {datetime.now()} - {total}銘柄")
    
    for symbol in watched_symbols:
        try:
            results = HWBAnalyzer.scan_symbol(symbol)
            if results:
                for result in results:
                    # 重複チェック（24時間以内に同じアラートを送信しない）
                    alert_key = f"{symbol}_{result['signal_type']}"
                    
                    if result['signal_type'] == 's1_fvg_detected':
                        if alert_key not in fvg_alerts or \
                           (datetime.now() - fvg_alerts[alert_key]) > timedelta(hours=24):
                            alerts.append(result)
                            fvg_alerts[alert_key] = datetime.now()
                    
                    elif result['signal_type'] == 's2_breakout':
                        if alert_key not in breakout_alerts or \
                           (datetime.now() - breakout_alerts[alert_key]) > timedelta(hours=24):
                            alerts.append(result)
                            breakout_alerts[alert_key] = datetime.now()
            
            processed += 1
            if processed % 50 == 0:
                print(f"進捗: {processed}/{total}")
                
        except Exception as e:
            print(f"エラー ({symbol}): {e}")
    
    print(f"スキャン完了: {len(alerts)}件のシグナルを検出")
    return alerts

def create_summary_embed(alerts):
    """サマリーEmbed作成"""
    fvg_count = len([a for a in alerts if a['signal_type'] == 's1_fvg_detected'])
    breakout_count = len([a for a in alerts if a['signal_type'] == 's2_breakout'])
    
    embed = discord.Embed(
        title="📊 HWB戦略 スキャン結果サマリー",
        description=f"スキャン時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}",
        color=discord.Color.gold()
    )
    
    embed.add_field(name="🔵 戦略1: FVG検出", value=f"{fvg_count} 銘柄", inline=True)
    embed.add_field(name="🟢 戦略2: ブレイクアウト", value=f"{breakout_count} 銘柄", inline=True)
    embed.add_field(name="📈 合計シグナル", value=f"{len(alerts)} 件", inline=True)
    
    # 検出銘柄リスト（最大10件）
    if alerts:
        symbols_list = [a['symbol'] for a in alerts[:10]]
        symbols_str = ", ".join(symbols_list)
        if len(alerts) > 10:
            symbols_str += f" ... 他{len(alerts) - 10}件"
        embed.add_field(name="🎯 検出銘柄", value=symbols_str, inline=False)
    
    embed.set_footer(text="HWB Strategy Scanner - Based on Technical Analysis Rules")
    
    return embed

async def post_alerts(channel, alerts):
    """アラートを投稿"""
    if not alerts:
        return
    
    # サマリーを投稿
    summary_embed = create_summary_embed(alerts)
    await channel.send(embed=summary_embed)
    
    # 個別アラート（最大20件）
    for alert in alerts[:20]:
        try:
            if alert['signal_type'] == 's1_fvg_detected':
                embed = create_hwb_fvg_alert_embed(alert)
            else:  # s2_breakout
                embed = create_hwb_breakout_alert_embed(alert)
            
            # チャート作成
            chart = HWBAnalyzer.create_hwb_chart(
                alert['symbol'], 
                setup_date=alert['setup']['date'],
                fvg_info=alert['fvg']
            )
            
            if chart:
                file = discord.File(chart, filename=f"{alert['symbol']}_hwb_chart.png")
                embed.set_image(url=f"attachment://{alert['symbol']}_hwb_chart.png")
                await channel.send(embed=embed, file=file)
            else:
                await channel.send(embed=embed)
                
        except Exception as e:
            print(f"アラート送信エラー ({alert['symbol']}): {e}")

# Bot イベント
@bot.event
async def on_ready():
    global watched_symbols
    watched_symbols = set(get_sp500_symbols())
    print(f"{bot.user} がログインしました！")
    print(f"監視銘柄数: {len(watched_symbols)}")
    
    for guild in bot.guilds:
        await setup_guild(guild)
    
    # 日次スキャンタスクを開始
    daily_scan.start()

@bot.event
async def on_guild_join(guild):
    await setup_guild(guild)

# 日次スキャンタスク
@tasks.loop(minutes=1)
async def daily_scan():
    """毎日のスキャンをスケジュール"""
    now_et = datetime.now(ET)
    scan_time = get_scan_time()
    
    # スキャン時刻かチェック（1分の幅を持たせる）
    if scan_time <= now_et < scan_time + timedelta(minutes=1):
        # 今日既にスキャン済みかチェック
        today_key = now_et.strftime("%Y-%m-%d")
        if hasattr(daily_scan, 'last_scan_date') and daily_scan.last_scan_date == today_key:
            return
        
        daily_scan.last_scan_date = today_key
        
        print(f"日次スキャン開始: {now_et}")
        
        # 全銘柄スキャン
        alerts = await scan_all_symbols()
        
        # 各サーバーに投稿
        for guild_id, config in server_configs.items():
            if config.get("enabled") and config.get("alert_channel"):
                try:
                    await post_alerts(config["alert_channel"], alerts)
                except Exception as e:
                    print(f"投稿エラー (Guild {guild_id}): {e}")

@daily_scan.before_loop
async def before_daily_scan():
    await bot.wait_until_ready()

# コマンド
@bot.command(name="status")
async def bot_status(ctx):
    """Botのステータスを表示"""
    now_et = datetime.now(ET)
    now_jst = datetime.now(JST)
    next_scan = get_scan_time()
    
    if now_et > next_scan:
        next_scan = next_scan + timedelta(days=1)
    
    time_until_scan = next_scan - now_et
    hours, remainder = divmod(time_until_scan.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    
    embed = discord.Embed(
        title="🤖 HWB Strategy Bot Status",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="現在時刻",
        value=f"ET: {now_et.strftime('%H:%M')}\nJST: {now_jst.strftime('%H:%M')}",
        inline=True
    )
    
    embed.add_field(
        name="次回スキャン",
        value=f"{next_scan.strftime('%H:%M ET')}\n(約{hours}時間{minutes}分後)",
        inline=True
    )
    
    embed.add_field(
        name="監視銘柄数",
        value=f"{len(watched_symbols)} 銘柄",
        inline=True
    )
    
    # 最近のアラート数
    recent_fvg = len([t for t in fvg_alerts.values() if datetime.now() - t < timedelta(hours=24)])
    recent_breakout = len([t for t in breakout_alerts.values() if datetime.now() - t < timedelta(hours=24)])
    
    embed.add_field(
        name="24時間以内のアラート",
        value=f"戦略1: {recent_fvg}件\n戦略2: {recent_breakout}件",
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command(name="scan")
@commands.has_permissions(administrator=True)
async def manual_scan(ctx):
    """手動でスキャンを実行（管理者のみ）"""
    await ctx.send("📡 手動スキャンを開始します...")
    
    alerts = await scan_all_symbols()
    
    if alerts:
        await post_alerts(ctx.channel, alerts)
    else:
        await ctx.send("シグナルは検出されませんでした。")

@bot.command(name="check")
async def check_symbol(ctx, symbol: str):
    """特定の銘柄をチェック"""
    symbol = symbol.upper()
    await ctx.send(f"🔍 {symbol} をチェック中...")
    
    try:
        results = HWBAnalyzer.scan_symbol(symbol)
        
        if not results:
            await ctx.send(f"{symbol} はHWB戦略の条件を満たしていません。")
            return
        
        for result in results:
            if result['signal_type'] == 's1_fvg_detected':
                embed = create_hwb_fvg_alert_embed(result)
            else:
                embed = create_hwb_breakout_alert_embed(result)
            
            # チャート作成
            chart = HWBAnalyzer.create_hwb_chart(
                symbol,
                setup_date=result['setup']['date'],
                fvg_info=result['fvg']
            )
            
            if chart:
                file = discord.File(chart, filename=f"{symbol}_hwb_chart.png")
                embed.set_image(url=f"attachment://{symbol}_hwb_chart.png")
                await ctx.send(embed=embed, file=file)
            else:
                await ctx.send(embed=embed)
            
    except Exception as e:
        await ctx.send(f"エラーが発生しました: {e}")

# メイン実行
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)