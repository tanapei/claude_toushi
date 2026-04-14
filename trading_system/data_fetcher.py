"""
データ取得モジュール
Yahoo Finance から日本株の株価データを取得します。
"""
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import numpy as np
import yfinance as yf

from trading_system.config import VALID_UNIVERSE, DATA_DIR

logger = logging.getLogger(__name__)


def fetch_stock_data(
    tickers: List[str],
    start_date: str,
    end_date: str,
    interval: str = "1d",
) -> Dict[str, pd.DataFrame]:
    """
    複数銘柄の株価データを取得する。

    Args:
        tickers: ティッカーシンボルのリスト（例: ["7203.T", "6758.T"]）
        start_date: 開始日 (YYYY-MM-DD)
        end_date: 終了日 (YYYY-MM-DD)
        interval: データ間隔 ('1d', '1wk', '1mo')

    Returns:
        {ticker: DataFrame} の辞書。DataFrameは OHLCV + 追加カラム。
    """
    stock_data: Dict[str, pd.DataFrame] = {}
    failed_tickers: List[str] = []

    for ticker in tickers:
        try:
            df = _fetch_single(ticker, start_date, end_date, interval)
            if df is not None and len(df) >= 60:  # 最低60日分のデータが必要
                stock_data[ticker] = df
                logger.info(f"[{ticker}] {len(df)}件取得完了")
            else:
                logger.warning(f"[{ticker}] データ不足（{len(df) if df is not None else 0}件）")
                failed_tickers.append(ticker)
        except Exception as e:
            logger.error(f"[{ticker}] 取得失敗: {e}")
            failed_tickers.append(ticker)
        time.sleep(0.3)  # レート制限回避

    if failed_tickers:
        logger.warning(f"取得失敗銘柄: {failed_tickers}")

    logger.info(f"データ取得完了: {len(stock_data)}/{len(tickers)} 銘柄")
    return stock_data


def _fetch_single(
    ticker: str,
    start_date: str,
    end_date: str,
    interval: str,
) -> Optional[pd.DataFrame]:
    """単一銘柄のデータを取得し、クリーニングして返す。"""
    raw = yf.download(
        ticker,
        start=start_date,
        end=end_date,
        interval=interval,
        auto_adjust=True,
        progress=False,
    )
    if raw.empty:
        return None

    # カラム名を正規化
    raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]

    # 欠損値処理
    df = df.dropna(subset=["close"])
    df["volume"] = df["volume"].fillna(0)

    # 株式分割などの異常値除去（前日比±50%超はスキップ）
    pct = df["close"].pct_change().abs()
    df = df[pct < 0.5].copy()

    df.index = pd.to_datetime(df.index)
    df.sort_index(inplace=True)
    return df


def get_benchmark_data(
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """
    ベンチマーク（日経225）データを取得する。
    比較用パフォーマンス指標の算出に使用。
    """
    df = _fetch_single("^N225", start_date, end_date, "1d")
    if df is None:
        logger.warning("日経225データの取得に失敗しました")
        return pd.DataFrame()
    return df


def load_universe_data(
    start_date: str,
    end_date: str,
    tickers: Optional[List[str]] = None,
) -> Dict[str, pd.DataFrame]:
    """
    設定ファイルのユニバース全銘柄のデータを取得する。
    キャッシュが存在する場合はキャッシュから読み込む。
    """
    if tickers is None:
        tickers = VALID_UNIVERSE

    cache_path = DATA_DIR / f"universe_{start_date}_{end_date}.pkl"

    if cache_path.exists():
        logger.info(f"キャッシュからデータを読み込み: {cache_path}")
        return pd.read_pickle(cache_path)

    logger.info(f"{len(tickers)}銘柄のデータを取得中...")
    data = fetch_stock_data(tickers, start_date, end_date)

    # キャッシュ保存
    pd.to_pickle(data, cache_path)
    logger.info(f"キャッシュ保存: {cache_path}")
    return data


def compute_returns(df: pd.DataFrame) -> pd.DataFrame:
    """日次リターン・対数リターンを計算して追加する。"""
    df = df.copy()
    df["return"] = df["close"].pct_change()
    df["log_return"] = np.log(df["close"] / df["close"].shift(1))
    return df


def align_dates(
    data: Dict[str, pd.DataFrame],
) -> Dict[str, pd.DataFrame]:
    """
    全銘柄の日付を共通取引日に揃える。
    欠損日はforward fillで補完する。
    """
    if not data:
        return data

    # 共通日付インデックスを取得
    all_dates = sorted(
        set.union(*[set(df.index) for df in data.values()])
    )
    common_index = pd.DatetimeIndex(all_dates)

    aligned: Dict[str, pd.DataFrame] = {}
    for ticker, df in data.items():
        reindexed = df.reindex(common_index)
        reindexed = reindexed.ffill()  # 前日値で補完
        aligned[ticker] = reindexed.dropna()

    return aligned
