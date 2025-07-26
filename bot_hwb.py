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
PROXIMITY_PERCENTAGE = float(os.getenv("PROXIMITY_PERCENTAGE", 0.05))
FVG_ZONE_PROXIMITY = float(os.getenv("FVG_ZONE_PROXIMITY", 0.10))
BREAKOUT_THRESHOLD = float(os.getenv("BREAKOUT_THRESHOLD", 0.001))
SETUP_LOOKBACK_DAYS = int(os.getenv("SETUP_LOOKBACK_DAYS", 60))

# シグナル管理の新パラメータ
SIGNAL_COOLING_PERIOD = int(os.getenv("SIGNAL_COOLING_PERIOD", 14))  # 冷却期間（デフォルト14日）

# 投稿設定
def parse_bool_env(key: str, default: bool) -> bool:
    """環境変数をboolに変換（エラーハンドリング付き）"""
    value = os.getenv(key, str(default).lower())
    return value.lower() in ['true', '1', 'yes', 'on']

POST_SUMMARY = parse_bool_env("POST_SUMMARY", True)
POST_STRATEGY1_ALERTS = parse_bool_env("POST_STRATEGY1_ALERTS", False)
POST_STRATEGY2_ALERTS = parse_bool_env("POST_STRATEGY2_ALERTS", False)

# 処理最適化パラメータ
BATCH_SIZE = int(os.getenv("BATCH_SIZE", 20))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 5))
CACHE_EXPIRY_HOURS = int(os.getenv("CACHE_EXPIRY_HOURS", 24))  # 修正2: デフォルトを24時間に変更

# グローバル変数
watched_symbols = set()
setup_alerts = {}
fvg_alerts = {}
breakout_alerts = {}
server_configs = {}
data_cache = {}
recent_signals_history = {}  # 直近シグナル履歴を保存する新しい変数

# タイムゾーン設定
ET = pytz.timezone("US/Eastern")
JST = pytz.timezone("Asia/Tokyo")


class ImprovedSignalManager:
    """改善されたシグナル履歴管理クラス"""
    
    def __init__(self, cooling_period: int = 14):
        """
        Parameters:
        -----------
        cooling_period : int
            シグナル発生後の冷却期間（日数）
        """
        self.signal_history = {}
        self.cooling_period = cooling_period
        
    def should_process_setup(self, symbol: str, setup_date: pd.Timestamp, reference_date: datetime = None) -> bool:
        """
        セットアップを処理すべきか判断
        
        Parameters:
        -----------
        reference_date : datetime
            基準日（デバッグモード用）。Noneの場合は現在日時を使用
        
        Returns:
        --------
        bool : 処理すべきならTrue
        """
        if symbol not in self.signal_history:
            return True
        
        history = self.signal_history[symbol]
        
        # 1. 同じセットアップは二度処理しない
        completed_setups = history.get('completed_setups', [])
        if any(abs((setup_date - completed).days) < 1 for completed in completed_setups):
            # 日付の誤差を考慮（1日以内は同じセットアップとみなす）
            return False
        
        # 2. 最新のシグナルから冷却期間をチェック
        last_signal_date = history.get('last_signal_date')
        if last_signal_date:
            # 基準日を使用（デバッグモード対応）
            ref_date = reference_date if reference_date else datetime.now()
            days_elapsed = (ref_date - last_signal_date).days
            if days_elapsed < self.cooling_period:
                # 冷却期間中でも、より新しいセットアップは評価
                last_setup = history.get('last_setup_date')
                if last_setup and setup_date > last_setup:
                    return True
                return False
        
        return True
    
    def record_signal(self, symbol: str, setup_date: pd.Timestamp, signal_date: datetime = None):
        """
        シグナル発生を記録
        
        Parameters:
        -----------
        signal_date : datetime
            シグナル発生日（デバッグモード用）。Noneの場合は現在日時を使用
        """
        if symbol not in self.signal_history:
            self.signal_history[symbol] = {
                'completed_setups': [],
                'last_signal_date': None,
                'last_setup_date': None
            }
        
        history = self.signal_history[symbol]
        
        # 完了済みセットアップとして記録
        if setup_date not in history['completed_setups']:
            history['completed_setups'].append(setup_date)
        
        history['last_signal_date'] = signal_date if signal_date else datetime.now()
        history['last_setup_date'] = setup_date
    
    def get_excluded_reason(self, symbol: str, setup_date: pd.Timestamp, reference_date: datetime = None) -> Optional[str]:
        """
        除外理由を取得（デバッグ用）
        
        Parameters:
        -----------
        reference_date : datetime
            基準日（デバッグモード用）
        """
        if symbol not in self.signal_history:
            return None
        
        history = self.signal_history[symbol]
        
        # 完了済みセットアップかチェック
        completed_setups = history.get('completed_setups', [])
        for completed in completed_setups:
            if abs((setup_date - completed).days) < 1:
                return f"セットアップ済み（{completed.strftime('%Y-%m-%d')}）"
        
        # 冷却期間中かチェック
        last_signal_date = history.get('last_signal_date')
        if last_signal_date:
            ref_date = reference_date if reference_date else datetime.now()
            days_elapsed = (ref_date - last_signal_date).days
            if days_elapsed < self.cooling_period:
                return f"冷却期間中（あと{self.cooling_period - days_elapsed}日）"
        
        return None
    
    def get_status_summary(self, reference_date: datetime = None) -> Dict[str, Dict]:
        """
        全銘柄のステータスサマリーを取得
        
        Parameters:
        -----------
        reference_date : datetime
            基準日（デバッグモード用）
        """
        summary = {}
        now = reference_date if reference_date else datetime.now()
        
        for symbol, history in self.signal_history.items():
            last_signal_date = history.get('last_signal_date')
            if last_signal_date:
                days_since = (now - last_signal_date).days
                in_cooling = days_since < self.cooling_period
                
                summary[symbol] = {
                    'completed_setups': len(history.get('completed_setups', [])),
                    'last_signal': last_signal_date.strftime('%Y-%m-%d'),
                    'days_since': days_since,
                    'in_cooling_period': in_cooling,
                    'cooling_remaining': max(0, self.cooling_period - days_since) if in_cooling else 0
                }
        
        return summary


# グローバルなシグナルマネージャーインスタンス
signal_manager = ImprovedSignalManager(cooling_period=SIGNAL_COOLING_PERIOD)


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


def get_business_days_ago(days: int, reference_date: pd.Timestamp = None) -> pd.Timestamp:
    """
    指定された営業日前の日付を取得
    
    Parameters:
    -----------
    reference_date : pd.Timestamp
        基準日（デバッグモード用）。Noneの場合は現在日時を使用
    """
    current_date = reference_date if reference_date else pd.Timestamp.now(tz=ET).normalize()
    business_days_count = 0
    
    while business_days_count < days:
        current_date -= pd.Timedelta(days=1)
        # 平日（月曜日=0, 金曜日=4）の場合のみカウント
        if current_date.weekday() < 5:
            business_days_count += 1
    
    return current_date.tz_localize(None)


def update_recent_signals_history(alerts: List[Dict], target_date: pd.Timestamp = None):
    """
    直近シグナル履歴を更新（修正4用）
    
    Parameters:
    -----------
    target_date : pd.Timestamp
        対象日（デバッグモード用）
    """
    global recent_signals_history
    
    today = target_date if target_date else pd.Timestamp.now().normalize()
    
    # 古いエントリを削除（3営業日より前のもの）
    three_business_days_ago = get_business_days_ago(3, today)
    recent_signals_history = {
        date: symbols for date, symbols in recent_signals_history.items()
        if pd.Timestamp(date) >= three_business_days_ago
    }
    
    # 今日のシグナルを追加
    today_str = today.strftime('%Y-%m-%d')
    today_s2_symbols = set()
    
    for alert in alerts:
        if alert['signal_type'] == 's2_breakout':
            today_s2_symbols.add(alert['symbol'])
    
    if today_s2_symbols:
        recent_signals_history[today_str] = today_s2_symbols


class HWBAnalyzer:
    """HWB戦略の分析クラス（最適化版）"""
    
    @staticmethod
    @lru_cache(maxsize=1000)
    def get_cached_stock_data(symbol: str, cache_key: str, target_date: str = None) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """
        キャッシュ付き株価データ取得
        
        Parameters:
        -----------
        target_date : str
            対象日（'YYYY-MM-DD'形式）
        """
        # キャッシュチェック
        cache_key_with_date = f"{cache_key}_{target_date}" if target_date else cache_key
        if symbol in data_cache:
            cached_data, cache_time = data_cache[symbol]
            if datetime.now() - cache_time < timedelta(hours=CACHE_EXPIRY_HOURS):
                return cached_data
        
        # データ取得
        df_daily, df_weekly = HWBAnalyzer._fetch_stock_data(symbol, target_date)
        
        # キャッシュに保存
        if df_daily is not None and df_weekly is not None:
            data_cache[symbol] = ((df_daily, df_weekly), datetime.now())
        
        return df_daily, df_weekly
    
    @staticmethod
    def _fetch_stock_data(symbol: str, target_date: str = None) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """
        実際のデータ取得処理
        
        Parameters:
        -----------
        target_date : str
            対象日（'YYYY-MM-DD'形式）。指定された場合、その日付までのデータを取得
        """
        session = requests.Session(impersonate="safari15_5")
        try:
            stock = yf.Ticker(symbol, session=session)
            
            # 終了日を設定
            if target_date:
                end_date = pd.Timestamp(target_date) + pd.Timedelta(days=1)  # 指定日を含む
            else:
                end_date = None
            
            # 日足データ（2年分または指定日まで）
            if target_date:
                start_date = pd.Timestamp(target_date) - pd.Timedelta(days=730)  # 2年前
                df_daily = stock.history(start=start_date, end=end_date, interval="1d")
            else:
                df_daily = stock.history(period="2y", interval="1d")
                
            if df_daily.empty or len(df_daily) < 200:
                return None, None
            df_daily.index = df_daily.index.tz_localize(None)
            
            # 週足データ（5年分または指定日まで）
            if target_date:
                start_date = pd.Timestamp(target_date) - pd.Timedelta(days=1825)  # 5年前
                df_weekly = stock.history(start=start_date, end=end_date, interval="1wk")
            else:
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
        
        # 週足SMAを日足データに結合（改善版）
        df_daily['Weekly_SMA200'] = np.nan
        df_daily['Weekly_Close'] = np.nan
        
        for idx, row in df_weekly.iterrows():
            if pd.notna(row['SMA200']):
                # 週の開始日（月曜日）と終了日（金曜日）を計算
                week_start = idx - pd.Timedelta(days=idx.weekday())
                week_end = week_start + pd.Timedelta(days=4)  # 金曜日まで
                
                # その週の日足データに週足情報を適用
                mask = (df_daily.index >= week_start) & (df_daily.index <= week_end)
                if mask.any():
                    df_daily.loc[mask, 'Weekly_SMA200'] = row['SMA200']
                    df_daily.loc[mask, 'Weekly_Close'] = row['Close']
        
        # 前方補完（週末や祝日のデータのため）
        df_daily['Weekly_SMA200'] = df_daily['Weekly_SMA200'].ffill()
        df_daily['Weekly_Close'] = df_daily['Weekly_Close'].ffill()
        
        return df_daily, df_weekly
    
    @staticmethod
    def check_single_symbol_rule1(symbol: str, target_date: str = None) -> Tuple[str, bool]:
        """
        単一銘柄のルール①チェック（同期版）- 修正1: 日足条件を追加
        
        Parameters:
        -----------
        target_date : str
            対象日（'YYYY-MM-DD'形式）
        """
        try:
            cache_key = target_date if target_date else datetime.now().strftime("%Y%m%d")
            df_daily, df_weekly = HWBAnalyzer.get_cached_stock_data(symbol, cache_key, target_date)
            
            if df_daily is None or df_weekly is None:
                return symbol, False
            
            df_daily, df_weekly = HWBAnalyzer.prepare_data(df_daily, df_weekly)
            
            # ルール①チェック（改善版）
            if 'Weekly_SMA200' not in df_daily.columns or 'Weekly_Close' not in df_daily.columns:
                return symbol, False
            
            # 最新の週足データを確認
            latest = df_daily.iloc[-1]
            
            # 週足終値が週足200SMAを上回っているかチェック
            weekly_condition = (pd.notna(latest['Weekly_SMA200']) and 
                               pd.notna(latest['Weekly_Close']) and 
                               latest['Weekly_Close'] > latest['Weekly_SMA200'])
            
            # 修正1: 日足で日足200SMAと日足200EMAどちらも下回っている銘柄を除外
            # つまり、日足終値が日足200SMAまたは日足200EMAのいずれかを上回っている必要がある
            daily_condition = (pd.notna(latest['SMA200']) and 
                              pd.notna(latest['EMA200']) and 
                              (latest['Close'] > latest['SMA200'] or latest['Close'] > latest['EMA200']))
            
            # 両方の条件を満たす必要がある
            passed = weekly_condition and daily_condition
            
            # デバッグ情報
            if symbol in ["AAPL", "NVDA", "TSLA"]:  # デバッグ用
                print(f"{symbol} - Weekly Close: {latest.get('Weekly_Close', 'N/A'):.2f}, "
                      f"Weekly SMA200: {latest.get('Weekly_SMA200', 'N/A'):.2f}, "
                      f"Daily Close: {latest.get('Close', 'N/A'):.2f}, "
                      f"Daily SMA200: {latest.get('SMA200', 'N/A'):.2f}, "
                      f"Daily EMA200: {latest.get('EMA200', 'N/A'):.2f}, "
                      f"Passed: {passed}")
            
            return symbol, passed
            
        except Exception as e:
            print(f"ルール①チェックエラー ({symbol}): {e}")
            return symbol, False
    
    @staticmethod
    async def batch_check_rule1_async(symbols: List[str], target_date: str = None) -> Dict[str, bool]:
        """
        ルール①を複数銘柄に対して非同期バッチチェック
        
        Parameters:
        -----------
        target_date : str
            対象日（'YYYY-MM-DD'形式）
        """
        results = {}
        
        # ThreadPoolExecutorを使って同期関数を非同期で実行
        loop = asyncio.get_event_loop()
        
        # バッチサイズを小さくして処理
        for i in range(0, len(symbols), BATCH_SIZE):
            batch = symbols[i:i + BATCH_SIZE]
            
            # 各バッチを並列処理
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [
                    loop.run_in_executor(executor, HWBAnalyzer.check_single_symbol_rule1, symbol, target_date)
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
    async def check_remaining_rules_async(symbol: str, target_date: str = None) -> List[Dict]:
        """
        ルール②③④を非同期でチェック（改善版）
        
        Parameters:
        -----------
        target_date : str
            対象日（'YYYY-MM-DD'形式）
        """
        loop = asyncio.get_event_loop()
        
        # ThreadPoolExecutorで同期関数を非同期実行
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            result = await loop.run_in_executor(
                executor,
                HWBAnalyzer._check_remaining_rules_sync,
                symbol,
                target_date
            )
        
        return result
    
    @staticmethod
    def _check_remaining_rules_sync(symbol: str, target_date: str = None) -> List[Dict]:
        """
        ルール②③④の同期版チェック（改善版）
        
        Parameters:
        -----------
        target_date : str
            対象日（'YYYY-MM-DD'形式）
        """
        cache_key = target_date if target_date else datetime.now().strftime("%Y%m%d")
        df_daily, df_weekly = HWBAnalyzer.get_cached_stock_data(symbol, cache_key, target_date)
        
        if df_daily is None or df_weekly is None:
            return []
        
        df_daily, df_weekly = HWBAnalyzer.prepare_data(df_daily, df_weekly)
        
        # ルール②セットアップを探す
        setups = HWBAnalyzer.find_rule2_setups(df_daily, lookback_days=SETUP_LOOKBACK_DAYS)
        if not setups:
            return []
        
        results = []
        
        # デバッグモード用の基準日
        reference_date = pd.Timestamp(target_date) if target_date else None
        
        # 各セットアップに対してシグナルマネージャーでチェック
        for setup in setups:
            setup_date = setup['date']
            
            # このセットアップを処理すべきかチェック
            if not signal_manager.should_process_setup(symbol, setup_date, reference_date):
                # デバッグ情報
                reason = signal_manager.get_excluded_reason(symbol, setup_date, reference_date)
                if symbol in ["NVDA", "AAPL", "MSFT"] and reason:
                    print(f"{symbol}: セットアップ {setup_date.strftime('%Y-%m-%d')} は除外 - {reason}")
                continue
            
            # ルール③FVG検出
            fvgs = HWBAnalyzer.detect_fvg_after_setup(df_daily, setup_date)
            
            for fvg in fvgs:
                # ルール④ブレイクアウトチェック（指定日のみ）
                breakout = HWBAnalyzer.check_breakout(df_daily, setup, fvg, today_only=True, target_date=target_date)
                
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
                        
                        # シグナル履歴を更新（ブレイクアウト時のみ記録）
                        signal_manager.record_signal(symbol, setup_date, reference_date)
                    
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
                
                # ルール①の再確認（週足終値 > 週足200SMA）
                if pd.notna(row.get('Weekly_Close')) and pd.notna(row.get('Weekly_SMA200')) and row['Weekly_Close'] > row['Weekly_SMA200']:
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
    def check_breakout(df_daily: pd.DataFrame, setup: Dict, fvg: Dict, today_only: bool = False, target_date: str = None) -> Optional[Dict]:
        """
        ルール④: ブレイクアウト条件をチェック
        
        Parameters:
        -----------
        today_only : bool
            Trueの場合、当日（最新日）のブレイクアウトのみを検出
        target_date : str
            対象日（'YYYY-MM-DD'形式）。デバッグモード用
        """
        setup_date = setup['date']
        fvg_formation_date = fvg['formation_date']
        fvg_lower = fvg['lower_bound']
        
        try:
            setup_idx = df_daily.index.get_loc(setup_date)
            fvg_idx = df_daily.index.get_loc(fvg_formation_date)
        except KeyError:
            return None
        
        # デバッグモード時は指定日のデータまでを使用
        if target_date:
            target_timestamp = pd.Timestamp(target_date)
            df_daily = df_daily[df_daily.index <= target_timestamp]
        
        # 最新データを確認
        latest_idx = len(df_daily) - 1
        if latest_idx <= fvg_idx:
            return None
        
        # レジスタンス計算の改善
        resistance_start_idx = setup_idx + 1
        resistance_end_idx = fvg_idx
        
        if resistance_end_idx <= resistance_start_idx:
            resistance_start_idx = max(0, setup_idx - 10)
            resistance_end_idx = setup_idx + 1
        
        resistance_high = df_daily.iloc[resistance_start_idx:resistance_end_idx]['High'].max()
        
        # FVG下限がサポートとして機能しているか（最新日まで）
        post_fvg_data = df_daily.iloc[fvg_idx + 1:]
        if len(post_fvg_data) > 0:
            min_low = post_fvg_data['Low'].min()
            if min_low < fvg_lower:
                return None  # FVGが破られた
        
        if today_only:
            # 当日のブレイクアウトのみをチェック
            current = df_daily.iloc[-1]
            if current['Close'] > resistance_high * (1 + BREAKOUT_THRESHOLD):
                return {
                    'breakout_date': df_daily.index[-1],
                    'breakout_price': current['Close'],
                    'resistance_price': resistance_high,
                    'setup_info': setup,
                    'fvg_info': fvg,
                    'breakout_percentage': (current['Close'] / resistance_high - 1) * 100
                }
        else:
            # すべての期間でブレイクアウトをチェック（!checkコマンド用）
            for i in range(len(post_fvg_data)):
                if post_fvg_data.iloc[i]['Close'] > resistance_high * (1 + BREAKOUT_THRESHOLD):
                    return {
                        'breakout_date': post_fvg_data.index[i],
                        'breakout_price': post_fvg_data.iloc[i]['Close'],
                        'resistance_price': resistance_high,
                        'setup_info': setup,
                        'fvg_info': fvg,
                        'breakout_percentage': (post_fvg_data.iloc[i]['Close'] / resistance_high - 1) * 100
                    }
        
        return None
    
    @staticmethod
    def create_hwb_chart(symbol: str, setup_date: pd.Timestamp = None, fvg_info: Dict = None, 
                        save_path: str = None, show_breakout_marker: bool = True, 
                        breakout_info: Dict = None, target_date: str = None) -> Optional[BytesIO]:
        """
        HWB戦略のチャートを作成（凡例なし、ブレイクアウトマーカーのみ表示）
        
        Parameters:
        -----------
        target_date : str
            対象日（'YYYY-MM-DD'形式）。デバッグモード用
        """
        cache_key = target_date if target_date else datetime.now().strftime("%Y%m%d")
        df_daily, df_weekly = HWBAnalyzer.get_cached_stock_data(symbol, cache_key, target_date)
        
        if df_daily is None:
            return None
        
        df_daily, _ = HWBAnalyzer.prepare_data(df_daily, df_weekly)
        
        # デバッグモード時は指定日までのデータを使用
        if target_date:
            target_timestamp = pd.Timestamp(target_date)
            df_daily = df_daily[df_daily.index <= target_timestamp]
        
        # チャート表示期間を設定（常に最新180日）
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
        
        # 週足SMA200（青色、太い線）
        if 'Weekly_SMA200' in df_plot.columns and not df_plot['Weekly_SMA200'].isna().all():
            apds.append(mpf.make_addplot(df_plot['Weekly_SMA200'], color='blue', width=3))
        
        # チャートタイトルにデバッグモード情報を追加
        title = f'{symbol} - HWB Strategy Analysis'
        if target_date:
            title += f' (Debug: {target_date})'
        
        fig, axes = mpf.plot(df_plot, type='candle', style=s, volume=True, addplot=apds,
                             title=title, returnfig=True, 
                             figsize=(12, 8), panel_ratios=(3, 1))
        
        ax = axes[0]
        
        # ブレイクアウトマーカーを表示する場合（breakout_infoが提供されている場合）
        if show_breakout_marker and breakout_info:
            # ブレイクアウト日を確認
            breakout_date = breakout_info.get('breakout_date')
            if breakout_date and breakout_date in df_plot.index:
                # ブレイクアウト日のインデックスを取得
                breakout_idx = df_plot.index.get_loc(breakout_date)
                breakout_price = df_plot.loc[breakout_date, 'Close']
                
                # 青い上向き矢印をブレイクアウト日に配置
                marker_price = breakout_price * 0.98  # 2%下に配置
                ax.scatter(
                    breakout_idx,
                    marker_price,
                    marker='^',
                    color='blue',
                    s=200,  # サイズ
                    zorder=5
                )
        
        # 凡例は表示しない（削除）
        
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


# Embed作成関数（簡略化版）
def create_simple_s1_embed(symbol: str, alerts: List[Dict]) -> discord.Embed:
    """戦略1の簡略化されたEmbed（複数FVGをまとめる）"""
    embed = discord.Embed(
        title=f"📍監視候補 - {symbol}",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="",
        value=f"日時：{datetime.now(JST).strftime('今日 %H:%M')}",
        inline=False
    )
    
    return embed


def create_simple_s2_embed(symbol: str, alerts: List[Dict]) -> discord.Embed:
    """戦略2の簡略化されたEmbed（複数ブレイクアウトをまとめる）"""
    embed = discord.Embed(
        title=f"🚀シグナル - {symbol}",
        color=discord.Color.green()
    )
    
    embed.add_field(
        name="",
        value=f"日時：{datetime.now(JST).strftime('今日 %H:%M')}",
        inline=False
    )
    
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


async def scan_all_symbols_optimized(target_date: str = None):
    """
    最適化された全銘柄スキャン（改善版）
    
    Parameters:
    -----------
    target_date : str
        対象日（'YYYY-MM-DD'形式）。デバッグモード用
    """
    alerts = []
    
    # すべての銘柄を取得
    all_symbols = list(watched_symbols)
    total = len(all_symbols)
    
    print(f"スキャン開始: {datetime.now()} - {total}銘柄")
    if target_date:
        print(f"デバッグモード: {target_date}時点のデータでスキャン")
    print("ステップ1: ルール①（週足トレンド）をチェック中...")
    
    # ステップ1: ルール①でフィルタリング（非同期バッチ処理）
    try:
        rule1_results = await HWBAnalyzer.batch_check_rule1_async(all_symbols, target_date)
        passed_rule1 = [symbol for symbol, passed in rule1_results.items() if passed]
        
        print(f"ルール①通過: {len(passed_rule1)}銘柄 ({len(passed_rule1)/total*100:.1f}%)")
        
        if not passed_rule1:
            print("ルール①を通過した銘柄がありません。")
            return alerts
        
        # ステップ2: ルール②③④をチェック（非同期）
        print("ステップ2: ルール②③④をチェック中...")
        processed = 0
        excluded_count = 0
        cooling_count = 0
        
        # バッチごとに非同期処理
        for i in range(0, len(passed_rule1), BATCH_SIZE):
            batch = passed_rule1[i:i + BATCH_SIZE]
            
            # 各銘柄を非同期でチェック
            tasks = [HWBAnalyzer.check_remaining_rules_async(symbol, target_date) for symbol in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for symbol, results in zip(batch, batch_results):
                if isinstance(results, Exception):
                    print(f"エラー ({symbol}): {results}")
                    continue
                
                # シグナル履歴による除外をカウント
                if not results:
                    status = signal_manager.signal_history.get(symbol)
                    if status:
                        excluded_count += 1
                        if status.get('last_signal_date'):
                            # デバッグモード用の基準日
                            ref_date = pd.Timestamp(target_date) if target_date else datetime.now()
                            days_since = (ref_date - status['last_signal_date']).days
                            if days_since < signal_manager.cooling_period:
                                cooling_count += 1
                
                if results:
                    for result in results:
                        alerts.append(result)
                
                processed += 1
                if processed % 10 == 0:
                    print(f"  進捗: {processed}/{len(passed_rule1)} "
                          f"(シグナル: {len(alerts)}件, 履歴除外: {excluded_count}件, "
                          f"冷却期間中: {cooling_count}件)")
            
            # バッチ間でイベントループに制御を返す
            await asyncio.sleep(0.1)
        
        print(f"スキャン完了: {len(alerts)}件のシグナルを検出")
        print(f"  履歴除外: {excluded_count}件（うち冷却期間中: {cooling_count}件）")
        
    except Exception as e:
        print(f"スキャンエラー: {e}")
        import traceback
        traceback.print_exc()
    
    return alerts


def create_summary_embed(alerts: List[Dict], target_date: pd.Timestamp = None) -> discord.Embed:
    """
    サマリーEmbed作成（修正4: 3つのカテゴリに拡張）
    
    Parameters:
    -----------
    target_date : pd.Timestamp
        対象日（デバッグモード用）
    """
    # 戦略2のティッカーを抽出（当日シグナル）
    today_s2_tickers = list(set([a['symbol'] for a in alerts if a['signal_type'] == 's2_breakout']))
    
    # 戦略1のティッカーから戦略2のティッカーを除外（監視候補）
    strategy1_tickers = list(set([a['symbol'] for a in alerts if a['signal_type'] == 's1_fvg_detected' and a['symbol'] not in today_s2_tickers]))
    
    # 直近シグナル（1-3営業日前）を取得
    recent_signal_tickers = []
    today = target_date if target_date else pd.Timestamp.now().normalize()
    for date_str, symbols in recent_signals_history.items():
        signal_date = pd.Timestamp(date_str)
        business_days_diff = 0
        current_date = today
        
        # 営業日数を計算
        while current_date > signal_date:
            current_date -= pd.Timedelta(days=1)
            if current_date.weekday() < 5:  # 平日
                business_days_diff += 1
        
        if 1 <= business_days_diff <= 3:
            recent_signal_tickers.extend(symbols)
    
    # 重複を除去して、今日のシグナルは除外
    recent_signal_tickers = list(set(recent_signal_tickers) - set(today_s2_tickers))
    
    # デバッグモード用のタイトルとタイムスタンプ
    if target_date:
        title = "AI判定システム（デバッグモード）"
        scan_time = target_date.strftime('%Y-%m-%d')
        description = f"**デバッグ: {scan_time}時点のデータでスキャン**\n"
    else:
        title = "AI判定システム"
        scan_time = datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')
        description = ""
    
    description += f"**NASDAQ/NYSE スキャン結果**\nスキャン時刻: {scan_time}"
    
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.gold()
    )
    
    # 監視候補（戦略1）
    if strategy1_tickers:
        tickers_str = ', '.join(sorted(strategy1_tickers))
        # Discordのフィールド値制限（1024文字）を考慮
        if len(tickers_str) > 1000:
            # 文字数制限を超える場合は省略
            tickers_list = []
            current_length = 0
            for ticker in sorted(strategy1_tickers):
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
    
    # 当日シグナル（戦略2）
    if today_s2_tickers:
        tickers_str = ', '.join(sorted(today_s2_tickers))
        # Discordのフィールド値制限（1024文字）を考慮
        if len(tickers_str) > 1000:
            # 文字数制限を超える場合は省略
            tickers_list = []
            current_length = 0
            for ticker in sorted(today_s2_tickers):
                if current_length + len(ticker) + 2 < 980:  # カンマとスペースを考慮
                    tickers_list.append(ticker)
                    current_length += len(ticker) + 2
                else:
                    tickers_list.append(f"... 他{len(today_s2_tickers) - len(tickers_list)}銘柄")
                    break
            tickers_str = ', '.join(tickers_list)
        
        embed.add_field(
            name="🚀 当日シグナル",
            value=tickers_str,
            inline=False
        )
    else:
        embed.add_field(
            name="🚀 当日シグナル",
            value="なし",
            inline=False
        )
    
    # 直近シグナル（３営業日以内）
    if recent_signal_tickers:
        tickers_str = ', '.join(sorted(recent_signal_tickers))
        # Discordのフィールド値制限（1024文字）を考慮
        if len(tickers_str) > 1000:
            # 文字数制限を超える場合は省略
            tickers_list = []
            current_length = 0
            for ticker in sorted(recent_signal_tickers):
                if current_length + len(ticker) + 2 < 980:  # カンマとスペースを考慮
                    tickers_list.append(ticker)
                    current_length += len(ticker) + 2
                else:
                    tickers_list.append(f"... 他{len(recent_signal_tickers) - len(tickers_list)}銘柄")
                    break
            tickers_str = ', '.join(tickers_list)
        
        embed.add_field(
            name="📈 直近シグナル（３営業日以内）",
            value=tickers_str,
            inline=False
        )
    else:
        embed.add_field(
            name="📈 直近シグナル（３営業日以内）",
            value="なし",
            inline=False
        )
    
    embed.set_footer(text="AI Trading Analysis System")
    
    return embed


async def post_alerts(channel, alerts: List[Dict], target_date: pd.Timestamp = None):
    """
    アラートを投稿（修正5: 当日シグナルは必ずアラートも出す）
    
    Parameters:
    -----------
    target_date : pd.Timestamp
        対象日（デバッグモード用）
    """
    # 履歴を更新
    update_recent_signals_history(alerts, target_date)
    
    # サマリーの投稿（POST_SUMMARYがTrueの場合）
    if POST_SUMMARY:
        if not alerts:
            # シグナルがない場合のサマリー
            no_signal_embed = discord.Embed(
                title="AI判定システム" + ("（デバッグモード）" if target_date else ""),
                description=f"**NASDAQ/NYSE スキャン結果**\nスキャン時刻: {target_date.strftime('%Y-%m-%d') if target_date else datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}",
                color=discord.Color.grey(),
                timestamp=datetime.now()
            )
            no_signal_embed.add_field(name="📍 監視候補", value="なし", inline=False)
            no_signal_embed.add_field(name="🚀 当日シグナル", value="なし", inline=False)
            no_signal_embed.add_field(name="📈 直近シグナル（３営業日以内）", value="なし", inline=False)
            no_signal_embed.set_footer(text="AI Trading Analysis System")
            await channel.send(embed=no_signal_embed)
        else:
            # シグナルがある場合のサマリー
            summary_embed = create_summary_embed(alerts, target_date)
            await channel.send(embed=summary_embed)
    
    # デバッグモードでは個別アラートは投稿しない
    if target_date:
        return
    
    # 個別アラートの投稿
    # 修正5: 当日シグナル（戦略2）は設定に関わらず常に出す
    # 銘柄ごとにアラートをグループ化
    alerts_by_symbol = {}
    for alert in alerts:
        symbol = alert['symbol']
        if symbol not in alerts_by_symbol:
            alerts_by_symbol[symbol] = []
        alerts_by_symbol[symbol].append(alert)
    
    # 戦略2のアラートを持つ銘柄を特定
    s2_symbols = set()
    for symbol, symbol_alerts in alerts_by_symbol.items():
        if any(a['signal_type'] == 's2_breakout' for a in symbol_alerts):
            s2_symbols.add(symbol)
    
    posted_count = 0
    max_individual_alerts = 30
    
    for symbol, symbol_alerts in alerts_by_symbol.items():
        if posted_count >= max_individual_alerts:
            break
        
        # 戦略1と戦略2のアラートを分離
        s1_alerts = [a for a in symbol_alerts if a['signal_type'] == 's1_fvg_detected']
        s2_alerts = [a for a in symbol_alerts if a['signal_type'] == 's2_breakout']
        
        # 戦略2アラート（ブレイクアウト）- 修正5: 常に投稿
        if s2_alerts:
            try:
                embed = create_simple_s2_embed(symbol, s2_alerts)
                
                # 最新のブレイクアウト情報を取得
                latest_breakout = None
                for alert in s2_alerts:
                    if 'breakout' in alert:
                        latest_breakout = alert['breakout']
                
                # チャート作成（ブレイクアウト情報付き）
                chart = HWBAnalyzer.create_hwb_chart(
                    symbol,
                    show_breakout_marker=True,
                    breakout_info=latest_breakout,  # ブレイクアウト情報を渡す
                    target_date=target_date.strftime('%Y-%m-%d') if target_date else None
                )
                
                if chart:
                    file = discord.File(chart, filename=f"{symbol}_hwb_chart.png")
                    embed.set_image(url=f"attachment://{symbol}_hwb_chart.png")
                    await channel.send(embed=embed, file=file)
                else:
                    await channel.send(embed=embed)
                
                posted_count += 1
                
            except Exception as e:
                print(f"戦略2アラート送信エラー ({symbol}): {e}")
        
        # 戦略1アラート（FVG検出）- 設定がONの場合のみ
        elif s1_alerts and POST_STRATEGY1_ALERTS and symbol not in s2_symbols:
            try:
                embed = create_simple_s1_embed(symbol, s1_alerts)
                
                # チャート作成（マーカーなし）
                chart = HWBAnalyzer.create_hwb_chart(
                    symbol,
                    show_breakout_marker=False,  # マーカーなし
                    target_date=target_date.strftime('%Y-%m-%d') if target_date else None
                )
                
                if chart:
                    file = discord.File(chart, filename=f"{symbol}_hwb_chart.png")
                    embed.set_image(url=f"attachment://{symbol}_hwb_chart.png")
                    await channel.send(embed=embed, file=file)
                else:
                    await channel.send(embed=embed)
                
                posted_count += 1
                
            except Exception as e:
                print(f"戦略1アラート送信エラー ({symbol}): {e}")
    
    # 投稿上限に達した場合の通知
    if posted_count >= max_individual_alerts and len(alerts_by_symbol) > max_individual_alerts:
        remaining = len(alerts_by_symbol) - max_individual_alerts
        await channel.send(f"📋 他に{remaining}銘柄のアラートがありますが、投稿上限に達しました。")


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
    print(f"  戦略2アラート: {'ON' if POST_STRATEGY2_ALERTS else 'OFF'}（当日シグナルは常に投稿）")
    print(f"  シグナル冷却期間: {signal_manager.cooling_period}日")
    
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
        
        # 修正3: スキャン前にキャッシュをクリア
        global data_cache
        cache_size = len(data_cache)
        data_cache.clear()
        HWBAnalyzer.get_cached_stock_data.cache_clear()  # LRUキャッシュもクリア
        print(f"キャッシュをクリアしました（{cache_size}件）")
        
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
    """Botのステータスを表示（改善版）"""
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
    post_settings.append(f"※当日シグナルは常に投稿")
    
    embed.add_field(
        name="投稿設定",
        value="\n".join(post_settings),
        inline=True
    )
    
    # キャッシュ統計
    cache_size = len(data_cache)
    embed.add_field(
        name="キャッシュ",
        value=f"{cache_size} 銘柄\n有効期限: {CACHE_EXPIRY_HOURS}時間",
        inline=True
    )
    
    # シグナル履歴統計（改善版）
    status_summary = signal_manager.get_status_summary()
    cooling_count = sum(1 for s in status_summary.values() if s['in_cooling_period'])
    total_signals = len(status_summary)
    
    embed.add_field(
        name="シグナル履歴",
        value=f"記録済み: {total_signals} 銘柄\n冷却期間中: {cooling_count} 銘柄\n冷却期間: {signal_manager.cooling_period}日",
        inline=False
    )
    
    await ctx.send(embed=embed)


@bot.command(name="scan")
@commands.has_permissions(administrator=True)
async def manual_scan(ctx, target_date: str = None):
    """
    手動でスキャンを実行（管理者のみ）
    
    Parameters:
    -----------
    target_date : str
        YYYYMMDD形式の日付（デバッグモード用）
    """
    # 日付パラメータの解析
    formatted_date = None
    if target_date:
        try:
            # YYYYMMDD形式を解析
            if len(target_date) == 8:
                year = int(target_date[:4])
                month = int(target_date[4:6])
                day = int(target_date[6:8])
                parsed_date = datetime(year, month, day)
                
                # 未来の日付は不可
                if parsed_date > datetime.now():
                    await ctx.send("❌ エラー: 未来の日付は指定できません。")
                    return
                
                # 2年以上前の日付は不可
                if parsed_date < datetime.now() - timedelta(days=730):
                    await ctx.send("❌ エラー: 2年以上前の日付は指定できません。")
                    return
                
                formatted_date = parsed_date.strftime('%Y-%m-%d')
                await ctx.send(f"📡 デバッグモード: {formatted_date}時点のデータで手動スキャンを開始します...")
            else:
                await ctx.send("❌ エラー: 日付はYYYYMMDD形式で指定してください（例: 20250529）")
                return
        except ValueError:
            await ctx.send("❌ エラー: 無効な日付形式です。YYYYMMDD形式で指定してください（例: 20250529）")
            return
    else:
        await ctx.send("📡 手動スキャンを開始します... (時間がかかる場合があります)")
    
    # 修正3: スキャン前にキャッシュをクリア
    global data_cache
    cache_size = len(data_cache)
    data_cache.clear()
    HWBAnalyzer.get_cached_stock_data.cache_clear()  # LRUキャッシュもクリア
    await ctx.send(f"✅ キャッシュをクリアしました（{cache_size}件）")
    
    # デバッグモード有効化メッセージ
    if not target_date:
        await ctx.send("📊 デバッグモードで実行中（NVDA、AAPL、MSFTの詳細情報を表示）")
    
    start_time = datetime.now()
    alerts = await scan_all_symbols_optimized(formatted_date)
    processing_time = (datetime.now() - start_time).total_seconds()
    
    # 除外された銘柄の情報（改善版）
    excluded_info = []
    reference_date = pd.Timestamp(formatted_date) if formatted_date else datetime.now()
    
    for symbol in ["NVDA", "AAPL", "MSFT"]:
        if symbol in signal_manager.signal_history:
            status = signal_manager.signal_history[symbol]
            last_signal = status.get('last_signal_date')
            if last_signal:
                days_since = (reference_date - last_signal).days
                if days_since < signal_manager.cooling_period:
                    excluded_info.append(f"{symbol}: 冷却期間中（あと{signal_manager.cooling_period - days_since}日）")
                else:
                    completed = len(status.get('completed_setups', []))
                    excluded_info.append(f"{symbol}: {completed}個のセットアップ完了済み")
    
    scan_summary = f"スキャン完了: {processing_time:.1f}秒"
    if excluded_info and not target_date:
        scan_summary += f"\n履歴情報: {', '.join(excluded_info)}"
    
    await ctx.send(scan_summary)
    
    if alerts:
        target_timestamp = pd.Timestamp(formatted_date) if formatted_date else None
        await post_alerts(ctx.channel, alerts, target_timestamp)
    else:
        await ctx.send("シグナルは検出されませんでした。")


@bot.command(name="check")
async def check_symbol(ctx, symbol: str):
    """特定の銘柄をチェック（改善版）"""
    symbol = symbol.upper()
    await ctx.send(f"🔍 {symbol} をチェック中...")
    
    try:
        # まずルール①をチェック
        rule1_results = await HWBAnalyzer.batch_check_rule1_async([symbol])
        if not rule1_results.get(symbol, False):
            await ctx.send(f"{symbol} はルール①（週足トレンド条件または日足MA条件）を満たしていません。")
            return
        
        # シグナル履歴の確認（情報表示用）
        history_info = ""
        current_signal_active = False
        if symbol in signal_manager.signal_history:
            status = signal_manager.signal_history[symbol]
            last_signal = status.get('last_signal_date')
            if last_signal:
                days_since = (datetime.now() - last_signal).days
                completed_count = len(status.get('completed_setups', []))
                
                history_info = f"\n\n📊 履歴情報:\n"
                history_info += f"- 完了済みセットアップ: {completed_count}個\n"
                history_info += f"- 最後のシグナル: {days_since}日前\n"
                
                if days_since < signal_manager.cooling_period:
                    history_info += f"- 状態: 冷却期間中（あと{signal_manager.cooling_period - days_since}日）"
                    # 0日前の場合は現在シグナル発生中
                    if days_since == 0:
                        current_signal_active = True
                else:
                    history_info += f"- 状態: ✅ 新規シグナル可能"
        
        # ルール②③④をチェック（個別チェックでは履歴を無視）
        cache_key = datetime.now().strftime("%Y%m%d")
        df_daily, df_weekly = HWBAnalyzer.get_cached_stock_data(symbol, cache_key)
        
        if df_daily is None or df_weekly is None:
            await ctx.send(f"エラー: {symbol} のデータ取得に失敗しました。")
            return
        
        df_daily, df_weekly = HWBAnalyzer.prepare_data(df_daily, df_weekly)
        
        # セットアップを検出（履歴チェックなし）
        setups = HWBAnalyzer.find_rule2_setups(df_daily, lookback_days=SETUP_LOOKBACK_DAYS)
        if not setups:
            await ctx.send(f"該当なし - {symbol} は現在セットアップ条件（ルール②）を満たしていません。{history_info}")
            return
        
        # 各セットアップに対してFVGとブレイクアウトをチェック
        all_results = []
        excluded_setups = []
        
        for setup in setups:
            setup_date = setup['date']
            
            # 履歴情報を収集（表示用）
            reason = signal_manager.get_excluded_reason(symbol, setup_date)
            if reason:
                excluded_setups.append(f"- {setup_date.strftime('%Y-%m-%d')}: {reason}")
            
            # FVG検出
            fvgs = HWBAnalyzer.detect_fvg_after_setup(df_daily, setup_date)
            
            for fvg in fvgs:
                # ブレイクアウトチェック（!checkでは全期間をチェック）
                breakout = HWBAnalyzer.check_breakout(df_daily, setup, fvg, today_only=False)
                
                if fvg:  # FVGが検出された
                    result = {
                        'symbol': symbol,
                        'signal_type': 's1_fvg_detected',
                        'setup': setup,
                        'fvg': fvg,
                        'current_price': df_daily['Close'].iloc[-1],
                        'daily_ma200': df_daily['SMA200'].iloc[-1],
                        'weekly_sma200': df_daily['Weekly_SMA200'].iloc[-1]
                    }
                    
                    if breakout:  # ブレイクアウトも発生
                        result['signal_type'] = 's2_breakout'
                        result['breakout'] = breakout
                    
                    all_results.append(result)
        
        # 現在の条件を満たすシグナルがあるかチェック
        current_s2_signals = [r for r in all_results if r['signal_type'] == 's2_breakout']
        current_s1_signals = [r for r in all_results if r['signal_type'] == 's1_fvg_detected']
        
        # 結果の表示
        if current_s2_signals:
            # 最新のブレイクアウト情報を確認
            latest_breakout = None
            for signal in current_s2_signals:
                if 'breakout' in signal and signal['breakout']:
                    latest_breakout = signal['breakout']
                    break
            
            # ブレイクアウトが本日発生しているか確認
            is_today_breakout = False
            if latest_breakout:
                breakout_date = latest_breakout.get('breakout_date')
                if breakout_date:
                    today = datetime.now().date()
                    is_today_breakout = breakout_date.date() == today
            
            # 状態メッセージを調整
            if is_today_breakout:
                status_msg = "✅ 本日ブレイクアウト条件を満たしました"
            else:
                status_msg = "✅ 過去にブレイクアウト条件を満たしています（現在は条件外の可能性）"
            
            if current_signal_active:
                status_msg += "（シグナル発生済み）"
            
            await ctx.send(f"{status_msg}{history_info}")
            
            # 戦略2のEmbed表示（過去のブレイクアウトでもマーカーを表示）
            embed = create_simple_s2_embed(symbol, current_s2_signals)
            chart = HWBAnalyzer.create_hwb_chart(
                symbol, 
                show_breakout_marker=True,  # 常にマーカーを表示
                breakout_info=latest_breakout  # ブレイクアウト情報を渡す
            )
            if chart:
                file = discord.File(chart, filename=f"{symbol}_hwb_chart.png")
                embed.set_image(url=f"attachment://{symbol}_hwb_chart.png")
                await ctx.send(embed=embed, file=file)
            else:
                await ctx.send(embed=embed)
                
        elif current_s1_signals:
            # FVG条件のみ満たしている
            status_msg = "✅ 現在FVG条件を満たしています（ブレイクアウト待ち）"
            await ctx.send(f"{status_msg}{history_info}")
            
            # 戦略1のEmbed表示
            embed = create_simple_s1_embed(symbol, current_s1_signals)
            chart = HWBAnalyzer.create_hwb_chart(symbol, show_breakout_marker=False)
            if chart:
                file = discord.File(chart, filename=f"{symbol}_hwb_chart.png")
                embed.set_image(url=f"attachment://{symbol}_hwb_chart.png")
                await ctx.send(embed=embed, file=file)
            else:
                await ctx.send(embed=embed)
                
        else:
            # 現在条件を満たしていない
            msg = f"該当なし - {symbol} は現在の条件を満たしていません。"
            if excluded_setups:
                msg += f"\n\n除外されたセットアップ:\n" + "\n".join(excluded_setups[:10])
                if len(excluded_setups) > 10:
                    msg += f"\n...他{len(excluded_setups)-10}個"
            msg += history_info
            await ctx.send(msg)
            
    except Exception as e:
        await ctx.send(f"エラーが発生しました: {e}")
        import traceback
        traceback.print_exc()


@bot.command(name="clear_cache")
@commands.has_permissions(administrator=True)
async def clear_cache(ctx):
    """キャッシュをクリア（管理者のみ）"""
    global data_cache
    cache_size = len(data_cache)
    data_cache.clear()
    HWBAnalyzer.get_cached_stock_data.cache_clear()  # LRUキャッシュもクリア
    await ctx.send(f"✅ キャッシュをクリアしました（{cache_size}件）")


@bot.command(name="clear_history")
@commands.has_permissions(administrator=True)
async def clear_history(ctx):
    """シグナル履歴をクリア（管理者のみ）"""
    history_size = len(signal_manager.signal_history)
    signal_manager.signal_history.clear()
    
    # 直近シグナル履歴もクリア
    global recent_signals_history
    recent_history_size = len(recent_signals_history)
    recent_signals_history.clear()
    
    await ctx.send(f"✅ シグナル履歴をクリアしました（{history_size}件）\n✅ 直近シグナル履歴もクリアしました（{recent_history_size}件）")


@bot.command(name="history")
async def show_history(ctx, symbol: str = None):
    """シグナル履歴を表示"""
    if symbol:
        # 特定銘柄の履歴
        symbol = symbol.upper()
        if symbol not in signal_manager.signal_history:
            await ctx.send(f"{symbol} のシグナル履歴はありません。")
            return
        
        history = signal_manager.signal_history[symbol]
        embed = discord.Embed(
            title=f"📊 {symbol} のシグナル履歴",
            color=discord.Color.blue()
        )
        
        last_signal = history.get('last_signal_date')
        if last_signal:
            days_since = (datetime.now() - last_signal).days
            embed.add_field(
                name="最後のシグナル",
                value=f"{last_signal.strftime('%Y-%m-%d %H:%M')}\n({days_since}日前)",
                inline=True
            )
        
        completed = history.get('completed_setups', [])
        embed.add_field(
            name="完了済みセットアップ",
            value=f"{len(completed)}個",
            inline=True
        )
        
        if days_since < signal_manager.cooling_period:
            embed.add_field(
                name="状態",
                value=f"冷却期間中（あと{signal_manager.cooling_period - days_since}日）",
                inline=True
            )
        else:
            embed.add_field(
                name="状態",
                value="✅ 新規シグナル可能",
                inline=True
            )
        
        # 最近のセットアップ日を表示
        if completed:
            recent_setups = sorted(completed, reverse=True)[:5]
            setup_list = [s.strftime('%Y-%m-%d') for s in recent_setups]
            embed.add_field(
                name="最近のセットアップ",
                value="\n".join(setup_list),
                inline=False
            )
        
        await ctx.send(embed=embed)
    else:
        # 全体のサマリー
        status_summary = signal_manager.get_status_summary()
        if not status_summary:
            await ctx.send("シグナル履歴はまだありません。")
            return
        
        embed = discord.Embed(
            title="📊 シグナル履歴サマリー",
            description=f"記録済み銘柄数: {len(status_summary)}",
            color=discord.Color.blue()
        )
        
        # 冷却期間中の銘柄
        cooling_symbols = [(s, info) for s, info in status_summary.items() if info['in_cooling_period']]
        if cooling_symbols:
            cooling_list = []
            for symbol, info in sorted(cooling_symbols, key=lambda x: x[1]['cooling_remaining'])[:10]:
                cooling_list.append(f"{symbol}: あと{info['cooling_remaining']}日")
            
            embed.add_field(
                name=f"冷却期間中の銘柄 ({len(cooling_symbols)})",
                value="\n".join(cooling_list) + (f"\n...他{len(cooling_symbols)-10}銘柄" if len(cooling_symbols) > 10 else ""),
                inline=False
            )
        
        # 最近のシグナル
        recent_signals = sorted(status_summary.items(), key=lambda x: x[1]['last_signal'], reverse=True)[:10]
        if recent_signals:
            recent_list = []
            for symbol, info in recent_signals:
                recent_list.append(f"{symbol}: {info['days_since']}日前")
            
            embed.add_field(
                name="最近のシグナル",
                value="\n".join(recent_list),
                inline=False
            )
        
        await ctx.send(embed=embed)


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
        embed.add_field(
            name="注意",
            value="※当日シグナル（戦略2）は設定に関わらず常に個別投稿されます",
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
        await ctx.send(f"✅ 戦略2アラートを{'ON' if POST_STRATEGY2_ALERTS else 'OFF'}にしました\n※ただし、当日シグナルは常に投稿されます")
    else:
        await ctx.send("❌ 無効なタイプです。`summary`, `s1`, `s2` のいずれかを指定してください。")


# メイン実行
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)