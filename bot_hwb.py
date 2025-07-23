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
import concurrent.futures
from functools import lru_cache
import pickle
from typing import List, Dict, Set, Tuple, Optional
import aiohttp
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

# HWB戦略のパラメータ
PROXIMITY_PERCENTAGE = 0.05  # ルール③条件Aの許容範囲（5%）
FVG_ZONE_PROXIMITY = 0.10   # ルール③条件Bの許容範囲（10%）
BREAKOUT_THRESHOLD = 0.001  # ブレイクアウトの閾値（0.1%）

# 投稿設定
def parse_bool_env(key: str, default: bool) -> bool:
    """環境変数をboolに変換（エラーハンドリング付き）"""
    value = os.getenv(key, str(default).lower())
    return value.lower() in ['true', '1', 'yes', 'on']

POST_SUMMARY = parse_bool_env("POST_SUMMARY", True)  # デフォルトON
POST_STRATEGY1_ALERTS = parse_bool_env("POST_STRATEGY1_ALERTS", False)  # デフォルトOFF
POST_STRATEGY2_ALERTS = parse_bool_env("POST_STRATEGY2_ALERTS", False)  # デフォルトOFF

# 処理最適化パラメータ
BATCH_SIZE = int(os.getenv("BATCH_SIZE", 20))  # 並列処理のバッチサイズを減らす
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 5))  # 並列ワーカー数を減らす
CACHE_EXPIRY_HOURS = 12  # キャッシュ有効期限

# グローバル変数
watched_symbols = set()
setup_alerts = {}
fvg_alerts = {}
breakout_alerts = {}
server_configs = {}
data_cache = {}  # 銘柄データのキャッシュ

# タイムゾーン設定
ET = pytz.timezone("US/Eastern")
JST = pytz.timezone("Asia/Tokyo")

def get_nasdaq_nyse_symbols() -> Set[str]:
    """NASDAQ/NYSEの全銘柄リストを取得（重複除去）"""
    symbols = set()
    
    try:
        # NASDAQ銘柄を取得
        nasdaq_url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&exchange=NASDAQ&download=true"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        print("NASDAQ銘柄リストを取得中...")
        try:
            response = requests.get(nasdaq_url, headers=headers, timeout=30)
            if response.status_code == 200:
                data = response.json()
                nasdaq_stocks = data.get('data', {}).get('rows', [])
                nasdaq_symbols = {stock['symbol'] for stock in nasdaq_stocks if stock.get('symbol')}
                symbols.update(nasdaq_symbols)
                print(f"NASDAQ: {len(nasdaq_symbols)}銘柄")
        except Exception as e:
            print(f"NASDAQ取得エラー: {e}")
            # フォールバック: yfinanceから主要銘柄を取得
            nasdaq_tickers = yf.Tickers("AAPL MSFT GOOGL AMZN NVDA META TSLA")
            symbols.update(nasdaq_tickers.symbols)
        
        # NYSE銘柄を取得
        print("NYSE銘柄リストを取得中...")
        try:
            # NYSE銘柄はfinvizから取得を試みる
            nyse_url = "https://finviz.com/screener.ashx?v=111&f=exch_nyse"
            response = requests.get(nyse_url, headers=headers, timeout=30)
            if response.status_code == 200:
                # HTMLパースして銘柄を抽出（簡易的な方法）
                # より確実な方法はSeleniumやAPIを使用
                pass
        except Exception as e:
            print(f"NYSE取得エラー: {e}")
        
        # 代替方法: S&P500 + 追加の主要銘柄
        if len(symbols) < 100:  # 取得失敗時のフォールバック
            print("フォールバック: S&P500 + 主要銘柄を使用")
            # S&P500
            sp500 = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
            sp500_symbols = sp500["Symbol"].str.replace(".", "-", regex=False).tolist()
            symbols.update(sp500_symbols)
            
            # 追加の主要銘柄
            major_stocks = [
                "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B",
                "JPM", "JNJ", "V", "PG", "UNH", "HD", "MA", "DIS", "BAC", "XOM",
                "NFLX", "ADBE", "CRM", "PFE", "TMO", "ABBV", "KO", "PEP", "AVGO",
                "CSCO", "ACN", "COST", "WMT", "MRK", "CVX", "LLY", "ORCL", "DHR"
            ]
            symbols.update(major_stocks)
        
        # 重複を除去してソート
        symbols = {s.upper() for s in symbols if s and len(s) <= 5}  # 通常のティッカーは5文字以下
        print(f"合計: {len(symbols)}銘柄（重複除去後）")
        
        return symbols
        
    except Exception as e:
        print(f"銘柄リスト取得エラー: {e}")
        # 最小限のリストを返す
        return set(["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"])

class HWBAnalyzer:
    """HWB戦略の分析クラス（最適化版）"""
    
    @staticmethod
    @lru_cache(maxsize=1000)
    def get_cached_stock_data(symbol: str, cache_key: str) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """キャッシュ付き株価データ取得"""
        # キャッシュチェック
        if symbol in data_cache:
            cached_data, cache_time = data_cache[symbol]
            if datetime.now() - cache_time < timedelta(hours=CACHE_EXPIRY_HOURS):
                return cached_data
        
        # データ取得
        df_daily, df_weekly = HWBAnalyzer._fetch_stock_data(symbol)
        
        # キャッシュに保存
        if df_daily is not None and df_weekly is not None:
            data_cache[symbol] = ((df_daily, df_weekly), datetime.now())
        
        return df_daily, df_weekly
    
    @staticmethod
    def _fetch_stock_data(symbol: str) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """実際のデータ取得処理"""
        session = requests.Session(impersonate="safari15_5")
        try:
            stock = yf.Ticker(symbol, session=session)
            
            # 日足データ（2年分）
            df_daily = stock.history(period="2y", interval="1d")
            if df_daily.empty or len(df_daily) < 200:
                return None, None
            df_daily.index = df_daily.index.tz_localize(None)
            
            # 週足データ（5年分）
            df_weekly = stock.history(period="5y", interval="1wk")
            if df_weekly.empty or len(df_weekly) < 200:
                return None, None
            df_weekly.index = df_weekly.index.tz_localize(None)
            
            return df_daily, df_weekly
        except Exception as e:
            return None, None
    
    @staticmethod
    def prepare_data(df_daily: pd.DataFrame, df_weekly: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
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
    def check_single_symbol_rule1(symbol: str) -> Tuple[str, bool]:
        """単一銘柄のルール①チェック（同期版）"""
        try:
            cache_key = datetime.now().strftime("%Y%m%d")
            df_daily, df_weekly = HWBAnalyzer.get_cached_stock_data(symbol, cache_key)
            
            if df_daily is None or df_weekly is None:
                return symbol, False
            
            df_daily, _ = HWBAnalyzer.prepare_data(df_daily, df_weekly)
            
            # ルール①チェック
            if 'Weekly_SMA200' not in df_daily.columns:
                return symbol, False
            
            latest = df_daily.iloc[-1]
            passed = (pd.notna(latest['Weekly_SMA200']) and 
                     pd.notna(latest['Close']) and 
                     latest['Close'] > latest['Weekly_SMA200'])
            
            return symbol, passed
            
        except Exception:
            return symbol, False
    
    @staticmethod
    async def batch_check_rule1_async(symbols: List[str]) -> Dict[str, bool]:
        """ルール①を複数銘柄に対して非同期バッチチェック"""
        results = {}
        
        # ThreadPoolExecutorを使って同期関数を非同期で実行
        loop = asyncio.get_event_loop()
        
        # バッチサイズを小さくして処理
        for i in range(0, len(symbols), BATCH_SIZE):
            batch = symbols[i:i + BATCH_SIZE]
            
            # 各バッチを並列処理
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [
                    loop.run_in_executor(executor, HWBAnalyzer.check_single_symbol_rule1, symbol)
                    for symbol in batch
                ]
                
                # 結果を収集
                batch_results = await asyncio.gather(*futures)
                for symbol, passed in batch_results:
                    results[symbol] = passed
            
            # イベントループに制御を返す
            await asyncio.sleep(0.1)
            
            # 進捗表示
            processed = min(i + BATCH_SIZE, len(symbols))
            passed_count = sum(1 for p in results.values() if p)
            print(f"  進捗: {processed}/{len(symbols)} ({passed_count}銘柄が通過)")
        
        return results
    
    @staticmethod
    async def check_remaining_rules_async(symbol: str) -> List[Dict]:
        """ルール②③④を非同期でチェック"""
        loop = asyncio.get_event_loop()
        
        # ThreadPoolExecutorで同期関数を非同期実行
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            result = await loop.run_in_executor(
                executor,
                HWBAnalyzer._check_remaining_rules_sync,
                symbol
            )
        
        return result
    
    @staticmethod
    def _check_remaining_rules_sync(symbol: str) -> List[Dict]:
        """ルール②③④の同期版チェック（内部用）"""
        cache_key = datetime.now().strftime("%Y%m%d")
        df_daily, df_weekly = HWBAnalyzer.get_cached_stock_data(symbol, cache_key)
        
        if df_daily is None or df_weekly is None:
            return []
        
        df_daily, df_weekly = HWBAnalyzer.prepare_data(df_daily, df_weekly)
        
        # ルール②セットアップを探す
        setups = HWBAnalyzer.find_rule2_setups(df_daily, lookback_days=60)
        if not setups:
            return []
        
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
    def find_rule2_setups(df_daily: pd.DataFrame, lookback_days: int = 30) -> List[Dict]:
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
                
                # ルール①の確認（既にチェック済みだが念のため）
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
    def detect_fvg_after_setup(df_daily: pd.DataFrame, setup_date: pd.Timestamp, max_days_after: int = 20) -> List[Dict]:
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
    def _check_fvg_ma_proximity(candle_3: pd.Series, candle_1: pd.Series) -> bool:
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
    def check_breakout(df_daily: pd.DataFrame, setup: Dict, fvg: Dict) -> Optional[Dict]:
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
    def create_hwb_chart(symbol: str, setup_date: pd.Timestamp = None, fvg_info: Dict = None, save_path: str = None) -> Optional[BytesIO]:
        """HWB戦略のチャートを作成"""
        cache_key = datetime.now().strftime("%Y%m%d")
        df_daily, df_weekly = HWBAnalyzer.get_cached_stock_data(symbol, cache_key)
        
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
        
        if len(df_plot) < 20:
            return None
        
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

# Embed作成関数（変更なし）
def create_hwb_fvg_alert_embed(result: Dict) -> discord.Embed:
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

def create_hwb_breakout_alert_embed(result: Dict) -> discord.Embed:
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
                topic="📈 HWB Strategy Alerts - NASDAQ/NYSE Technical Analysis Signals"
            )
        except discord.Forbidden:
            print(f"チャンネル作成権限がありません: {guild.name}")
    
    server_configs[guild.id] = {
        "alert_channel": alert_channel,
        "enabled": True
    }
    
    if alert_channel:
        print(f"サーバー '{guild.name}' の設定完了。アラートチャンネル: #{alert_channel.name}")

async def scan_all_symbols_optimized():
    """最適化された全銘柄スキャン（非同期版）"""
    alerts = []
    
    # すべての銘柄を取得
    all_symbols = list(watched_symbols)
    total = len(all_symbols)
    
    print(f"スキャン開始: {datetime.now()} - {total}銘柄")
    print("ステップ1: ルール①（週足トレンド）をチェック中...")
    
    # ステップ1: ルール①でフィルタリング（非同期バッチ処理）
    try:
        rule1_results = await HWBAnalyzer.batch_check_rule1_async(all_symbols)
        passed_rule1 = [symbol for symbol, passed in rule1_results.items() if passed]
        
        print(f"ルール①通過: {len(passed_rule1)}銘柄 ({len(passed_rule1)/total*100:.1f}%)")
        
        if not passed_rule1:
            print("ルール①を通過した銘柄がありません。")
            return alerts
        
        # ステップ2: ルール②③④をチェック（非同期）
        print("ステップ2: ルール②③④をチェック中...")
        processed = 0
        
        # バッチごとに非同期処理
        for i in range(0, len(passed_rule1), BATCH_SIZE):
            batch = passed_rule1[i:i + BATCH_SIZE]
            
            # 各銘柄を非同期でチェック
            tasks = [HWBAnalyzer.check_remaining_rules_async(symbol) for symbol in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for symbol, results in zip(batch, batch_results):
                if isinstance(results, Exception):
                    print(f"エラー ({symbol}): {results}")
                    continue
                
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
                if processed % 10 == 0:
                    print(f"  進捗: {processed}/{len(passed_rule1)} (シグナル: {len(alerts)}件)")
            
            # バッチ間でイベントループに制御を返す
            await asyncio.sleep(0.1)
        
        print(f"スキャン完了: {len(alerts)}件のシグナルを検出")
        
        # 統計情報を保存
        scan_all_symbols_optimized.last_stats = {
            'rule1_pass_rate': len(passed_rule1) / len(all_symbols) * 100 if all_symbols else 0,
            'total_signals': len(alerts)
        }
        
    except Exception as e:
        print(f"スキャンエラー: {e}")
        import traceback
        traceback.print_exc()
    
    return alerts

def create_summary_embed(alerts: List[Dict]) -> discord.Embed:
    """サマリーEmbed作成"""
    # 戦略1と戦略2のティッカーを分離
    strategy1_tickers = [a['symbol'] for a in alerts if a['signal_type'] == 's1_fvg_detected']
    strategy2_tickers = [a['symbol'] for a in alerts if a['signal_type'] == 's2_breakout']
    
    embed = discord.Embed(
        title="AI判定システム",
        description=f"**NASDAQ/NYSE スキャン結果**\nスキャン時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}",
        color=discord.Color.gold()
    )
    
    # 監視候補（戦略1）
    if strategy1_tickers:
        tickers_str = ', '.join(strategy1_tickers)
        # Discordのフィールド値制限（1024文字）を考慮
        if len(tickers_str) > 1000:
            # 文字数制限を超える場合は省略
            tickers_list = []
            current_length = 0
            for ticker in strategy1_tickers:
                if current_length + len(ticker) + 2 < 980:  # カンマとスペースを考慮
                    tickers_list.append(ticker)
                    current_length += len(ticker) + 2
                else:
                    tickers_list.append(f"... 他{len(strategy1_tickers) - len(tickers_list)}銘柄")
                    break
            tickers_str = ', '.join(tickers_list)
        
        embed.add_field(
            name="📍 監視候補",
            value=tickers_str,
            inline=False
        )
    else:
        embed.add_field(
            name="📍 監視候補",
            value="なし",
            inline=False
        )
    
    # シグナル（戦略2）
    if strategy2_tickers:
        tickers_str = ', '.join(strategy2_tickers)
        # Discordのフィールド値制限（1024文字）を考慮
        if len(tickers_str) > 1000:
            # 文字数制限を超える場合は省略
            tickers_list = []
            current_length = 0
            for ticker in strategy2_tickers:
                if current_length + len(ticker) + 2 < 980:  # カンマとスペースを考慮
                    tickers_list.append(ticker)
                    current_length += len(ticker) + 2
                else:
                    tickers_list.append(f"... 他{len(strategy2_tickers) - len(tickers_list)}銘柄")
                    break
            tickers_str = ', '.join(tickers_list)
        
        embed.add_field(
            name="🚀 シグナル",
            value=tickers_str,
            inline=False
        )
    else:
        embed.add_field(
            name="🚀 シグナル",
            value="なし",
            inline=False
        )
    
    embed.set_footer(text="AI Trading Analysis System")
    
    return embed

async def post_alerts(channel, alerts: List[Dict]):
    """アラートを投稿"""
    # サマリーの投稿（POST_SUMMARYがTrueの場合）
    if POST_SUMMARY:
        if not alerts:
            # シグナルがない場合のサマリー
            no_signal_embed = discord.Embed(
                title="AI判定システム",
                description=f"**NASDAQ/NYSE スキャン結果**\nスキャン時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}",
                color=discord.Color.grey(),
                timestamp=datetime.now()
            )
            no_signal_embed.add_field(name="📍 監視候補", value="なし", inline=False)
            no_signal_embed.add_field(name="🚀 シグナル", value="なし", inline=False)
            no_signal_embed.set_footer(text="AI Trading Analysis System")
            await channel.send(embed=no_signal_embed)
        else:
            # シグナルがある場合のサマリー
            summary_embed = create_summary_embed(alerts)
            await channel.send(embed=summary_embed)
    
    # 個別アラートの投稿（該当する設定がONの場合のみ）
    if POST_STRATEGY1_ALERTS or POST_STRATEGY2_ALERTS:
        posted_count = 0
        max_individual_alerts = 30
        
        for alert in alerts:
            # 投稿上限チェック
            if posted_count >= max_individual_alerts:
                break
            
            # 戦略1アラート（FVG検出）
            if alert['signal_type'] == 's1_fvg_detected' and POST_STRATEGY1_ALERTS:
                try:
                    embed = create_hwb_fvg_alert_embed(alert)
                    
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
                    
                    posted_count += 1
                    
                except Exception as e:
                    print(f"戦略1アラート送信エラー ({alert['symbol']}): {e}")
            
            # 戦略2アラート（ブレイクアウト）
            elif alert['signal_type'] == 's2_breakout' and POST_STRATEGY2_ALERTS:
                try:
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
                    
                    posted_count += 1
                    
                except Exception as e:
                    print(f"戦略2アラート送信エラー ({alert['symbol']}): {e}")
        
        # 投稿上限に達した場合の通知
        if posted_count >= max_individual_alerts and len(alerts) > max_individual_alerts:
            remaining = len(alerts) - max_individual_alerts
            await channel.send(f"📋 他に{remaining}件のアラートがありますが、投稿上限に達しました。")

# Bot イベント
@bot.event
async def on_ready():
    global watched_symbols
    watched_symbols = get_nasdaq_nyse_symbols()
    print(f"{bot.user} がログインしました！")
    print(f"監視銘柄数: {len(watched_symbols):,}")
    
    # 投稿設定を表示
    print("\n投稿設定:")
    print(f"  サマリー: {'ON' if POST_SUMMARY else 'OFF'}")
    print(f"  戦略1アラート: {'ON' if POST_STRATEGY1_ALERTS else 'OFF'}")
    print(f"  戦略2アラート: {'ON' if POST_STRATEGY2_ALERTS else 'OFF'}")
    
    # キャッシュディレクトリを作成
    os.makedirs("cache", exist_ok=True)
    
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
    
    # 市場終了15分後の時刻を計算
    market_close = now_et.replace(hour=16, minute=15, second=0, microsecond=0)
    
    # スキャン時刻かチェック（1分の幅を持たせる）
    if market_close <= now_et < market_close + timedelta(minutes=1):
        # 週末はスキップ
        if now_et.weekday() >= 5:  # 土曜日(5)または日曜日(6)
            return
        
        # 今日既にスキャン済みかチェック
        today_key = now_et.strftime("%Y-%m-%d")
        if hasattr(daily_scan, 'last_scan_date') and daily_scan.last_scan_date == today_key:
            return
        
        daily_scan.last_scan_date = today_key
        
        print(f"日次スキャン開始: {now_et}")
        
        # 処理時間を計測
        start_time = datetime.now()
        
        # 全銘柄スキャン（最適化版）
        alerts = await scan_all_symbols_optimized()
        
        # 処理統計を保存
        processing_time = (datetime.now() - start_time).total_seconds()
        print(f"処理完了: {processing_time:.1f}秒")
        
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
    market_close = now_et.replace(hour=16, minute=15, second=0, microsecond=0)
    
    if now_et > market_close:
        market_close = market_close + timedelta(days=1)
    
    # 週末の場合は月曜日まで
    while market_close.weekday() >= 5:
        market_close = market_close + timedelta(days=1)
    
    time_until_scan = market_close - now_et
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
        value=f"{market_close.strftime('%m/%d %H:%M ET')}\n(約{hours}時間{minutes}分後)",
        inline=True
    )
    
    embed.add_field(
        name="監視対象",
        value=f"NASDAQ/NYSE\n{len(watched_symbols):,} 銘柄",
        inline=True
    )
    
    # 投稿設定
    post_settings = []
    post_settings.append(f"サマリー: {'✅' if POST_SUMMARY else '❌'}")
    post_settings.append(f"戦略1: {'✅' if POST_STRATEGY1_ALERTS else '❌'}")
    post_settings.append(f"戦略2: {'✅' if POST_STRATEGY2_ALERTS else '❌'}")
    
    embed.add_field(
        name="投稿設定",
        value="\n".join(post_settings),
        inline=True
    )
    
    # キャッシュ統計
    cache_size = len(data_cache)
    embed.add_field(
        name="キャッシュ",
        value=f"{cache_size} 銘柄",
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
    await ctx.send("📡 手動スキャンを開始します... (時間がかかる場合があります)")
    
    start_time = datetime.now()
    alerts = await scan_all_symbols_optimized()
    processing_time = (datetime.now() - start_time).total_seconds()
    
    await ctx.send(f"スキャン完了: {processing_time:.1f}秒")
    
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
        # まずルール①をチェック
        rule1_results = await HWBAnalyzer.batch_check_rule1_async([symbol])
        if not rule1_results.get(symbol, False):
            await ctx.send(f"{symbol} はルール①（週足トレンド）を満たしていません。")
            return
        
        # ルール②③④をチェック
        results = await HWBAnalyzer.check_remaining_rules_async(symbol)
        
        if not results:
            await ctx.send(f"{symbol} はルール②以降の条件を満たしていません。")
            return
        
        # 個別チェックの場合は常に結果を表示（投稿設定に関係なく）
        await ctx.send(f"✅ {symbol} は以下の条件を満たしています：")
        
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

@bot.command(name="clear_cache")
@commands.has_permissions(administrator=True)
async def clear_cache(ctx):
    """キャッシュをクリア（管理者のみ）"""
    global data_cache
    cache_size = len(data_cache)
    data_cache.clear()
    await ctx.send(f"✅ キャッシュをクリアしました（{cache_size}件）")

@bot.command(name="toggle")
@commands.has_permissions(administrator=True)
async def toggle_alerts(ctx, alert_type: str = None):
    """投稿設定を切り替え（管理者のみ）"""
    global POST_SUMMARY, POST_STRATEGY1_ALERTS, POST_STRATEGY2_ALERTS
    
    if alert_type is None:
        # 現在の設定を表示
        embed = discord.Embed(
            title="📮 投稿設定",
            color=discord.Color.blue()
        )
        embed.add_field(name="サマリー", value="✅ ON" if POST_SUMMARY else "❌ OFF", inline=True)
        embed.add_field(name="戦略1アラート", value="✅ ON" if POST_STRATEGY1_ALERTS else "❌ OFF", inline=True)
        embed.add_field(name="戦略2アラート", value="✅ ON" if POST_STRATEGY2_ALERTS else "❌ OFF", inline=True)
        embed.add_field(
            name="使用方法",
            value="`!toggle summary` - サマリー投稿の切り替え\n"
                  "`!toggle s1` - 戦略1アラートの切り替え\n"
                  "`!toggle s2` - 戦略2アラートの切り替え",
            inline=False
        )
        await ctx.send(embed=embed)
        return
    
    alert_type = alert_type.lower()
    
    if alert_type in ["summary", "sum"]:
        POST_SUMMARY = not POST_SUMMARY
        await ctx.send(f"✅ サマリー投稿を{'ON' if POST_SUMMARY else 'OFF'}にしました")
    elif alert_type in ["s1", "strategy1", "1"]:
        POST_STRATEGY1_ALERTS = not POST_STRATEGY1_ALERTS
        await ctx.send(f"✅ 戦略1アラートを{'ON' if POST_STRATEGY1_ALERTS else 'OFF'}にしました")
    elif alert_type in ["s2", "strategy2", "2"]:
        POST_STRATEGY2_ALERTS = not POST_STRATEGY2_ALERTS
        await ctx.send(f"✅ 戦略2アラートを{'ON' if POST_STRATEGY2_ALERTS else 'OFF'}にしました")
    else:
        await ctx.send("❌ 無効なタイプです。`summary`, `s1`, `s2` のいずれかを指定してください。")

# メイン実行
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)