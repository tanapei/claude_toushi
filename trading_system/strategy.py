"""
取引戦略モジュール
複数のテクニカル指標を組み合わせた高期待値戦略を実装します。

戦略: デュアルモメンタム + EMAクロス + RSIフィルタ
- モメンタムスクリーニング: 過去12ヶ月リターン上位銘柄を選択
- トレンドフィルタ: 200日EMAより上にある銘柄のみ対象
- エントリー: 9日EMAが26日EMAをゴールデンクロス + RSI(40-70) + ボリューム確認
- エグジット: デッドクロス OR トレーリングストップ OR 利確ライン
"""
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from trading_system.config import STRATEGY_PARAMS

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """取引シグナル"""
    ticker: str
    date: pd.Timestamp
    action: str          # "BUY" | "SELL" | "HOLD"
    price: float
    reason: str          # シグナル発生理由
    strength: float      # シグナル強度 0.0〜1.0
    indicators: Dict     # 計算に使用した指標値


class TechnicalIndicators:
    """テクニカル指標計算クラス"""

    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
        avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def macd(
        series: pd.Series,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """MACD, シグナル線, ヒストグラムを返す"""
        ema_fast = series.ewm(span=fast, adjust=False).mean()
        ema_slow = series.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Average True Range（ボラティリティ指標）"""
        high = df["high"]
        low = df["low"]
        close_prev = df["close"].shift(1)
        tr = pd.concat([
            high - low,
            (high - close_prev).abs(),
            (low - close_prev).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(com=period - 1, adjust=False).mean()

    @staticmethod
    def momentum(series: pd.Series, period: int, skip: int = 21) -> pd.Series:
        """モメンタム: N期間前〜skip期間前のリターン"""
        return series.pct_change(period - skip).shift(skip)

    @staticmethod
    def volume_ratio(df: pd.DataFrame, period: int = 20) -> pd.Series:
        """出来高比率: 当日出来高 / 移動平均出来高"""
        vol_ma = df["volume"].rolling(period).mean()
        return df["volume"] / vol_ma.replace(0, np.nan)


def add_indicators(df: pd.DataFrame, params: Optional[Dict] = None) -> pd.DataFrame:
    """
    DataFrameにテクニカル指標を追加する。

    Args:
        df: OHLCV DataFrame
        params: 戦略パラメータ（Noneの場合はデフォルト設定を使用）

    Returns:
        指標追加済みDataFrame
    """
    if params is None:
        params = STRATEGY_PARAMS

    df = df.copy()
    ti = TechnicalIndicators()

    # EMA
    df["ema_fast"] = ti.ema(df["close"], params["ema_fast"])
    df["ema_slow"] = ti.ema(df["close"], params["ema_slow"])
    df["ema_trend"] = ti.ema(df["close"], params["ema_trend"])

    # RSI
    df["rsi"] = ti.rsi(df["close"], params["rsi_period"])

    # MACD
    df["macd"], df["macd_signal"], df["macd_hist"] = ti.macd(
        df["close"],
        params["macd_fast"],
        params["macd_slow"],
        params["macd_signal"],
    )

    # モメンタム
    df["momentum"] = ti.momentum(
        df["close"],
        params["momentum_period"],
        params["momentum_skip"],
    )

    # ボリューム比率
    df["volume_ratio"] = ti.volume_ratio(df, params["volume_ma_period"])

    # ATR（ボラティリティ）
    df["atr"] = ti.atr(df)

    # EMAクロスシグナル（ゴールデンクロス=1, デッドクロス=-1, それ以外=0）
    df["ema_cross"] = 0
    bullish = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
    bearish = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
    df.loc[bullish, "ema_cross"] = 1
    df.loc[bearish, "ema_cross"] = -1

    return df


def rank_by_momentum(
    data: Dict[str, pd.DataFrame],
    date: pd.Timestamp,
    top_n: int,
) -> List[str]:
    """
    指定日時点でモメンタム上位N銘柄を返す。

    モメンタムデータが不足している初期期間は、短期リターン（20日）を
    フォールバックスコアとして使用し、候補銘柄ゼロを防ぐ。

    Args:
        data: {ticker: DataFrame} の辞書（要 add_indicators 済み）
        date: 評価日
        top_n: 上位N銘柄を返す

    Returns:
        モメンタム上位N銘柄のティッカーリスト（降順）
    """
    momentum_scores: Dict[str, float] = {}
    fallback_scores: Dict[str, float] = {}

    for ticker, df in data.items():
        if date not in df.index:
            continue
        row = df.loc[date]
        mom = row.get("momentum", np.nan)
        if not np.isnan(mom):
            momentum_scores[ticker] = float(mom)
        else:
            # フォールバック: 20日リターンで代替
            idx = df.index.get_loc(date)
            if idx >= 20:
                ret20 = (df["close"].iloc[idx] / df["close"].iloc[idx - 20] - 1)
                if not np.isnan(ret20):
                    fallback_scores[ticker] = float(ret20)

    if momentum_scores:
        sorted_tickers = sorted(momentum_scores, key=momentum_scores.get, reverse=True)
    else:
        # 全銘柄でモメンタムが計算できない場合は短期リターンで代替
        sorted_tickers = sorted(fallback_scores, key=fallback_scores.get, reverse=True)

    return sorted_tickers[:top_n]


def generate_buy_signal(
    df: pd.DataFrame,
    date: pd.Timestamp,
    params: Optional[Dict] = None,
) -> Optional[Signal]:
    """
    買いシグナルをスコアリング方式で生成する。

    必須条件（1つでも欠けると不可）:
      - EMAゴールデンクロス（9日EMAが26日EMAを上抜け）
      - RSI が rsi_lower〜rsi_upper の範囲内

    加点条件（各1点、min_score点以上で買いシグナル）:
      1. 価格 > ema_trend 日EMA（中期上昇トレンド）
      2. MACDヒストグラム > 0（モメンタム上昇中）
      3. EMAfast が EMAslowを明確に上回っている（乖離率 > 0.3%）
      4. ボリューム比率 >= volume_multiplier（出来高増加）
      5. RSI が 45〜65 の理想ゾーン（過熱でも弱くもない）

    この方式により、弱気相場・中立相場でも取引機会を確保しつつ
    ノイズシグナルを除外する。
    """
    if params is None:
        params = STRATEGY_PARAMS

    if date not in df.index:
        return None

    idx = df.index.get_loc(date)
    if idx < 1:
        return None

    row = df.loc[date]
    prev_row = df.iloc[idx - 1]

    rsi_val = float(row["rsi"]) if not np.isnan(row["rsi"]) else np.nan

    # ── 必須条件（どちらか欠けたら即座にNone）──
    # 必須1: EMAゴールデンクロス
    golden_cross = (
        row["ema_fast"] > row["ema_slow"] and
        prev_row["ema_fast"] <= prev_row["ema_slow"]
    )
    if not golden_cross:
        return None

    # 必須2: RSI が範囲内
    if np.isnan(rsi_val) or not (params["rsi_lower"] <= rsi_val <= params["rsi_upper"]):
        return None

    # ── 加点条件（スコアリング）──
    score = 0
    score_details = []

    # 加点1: 中期トレンド上向き
    if row["close"] > row["ema_trend"]:
        score += 1
        score_details.append(f"トレンド↑(>{params['ema_trend']}EMA)")

    # 加点2: MACDヒストグラム正
    if not np.isnan(row["macd_hist"]) and row["macd_hist"] > 0:
        score += 1
        score_details.append("MACD↑")

    # 加点3: EMAの乖離率が明確（0.3%以上）
    if row["ema_slow"] > 0:
        ema_spread = (row["ema_fast"] - row["ema_slow"]) / row["ema_slow"]
        if ema_spread > 0.003:
            score += 1
            score_details.append(f"EMA乖離{ema_spread*100:.1f}%")

    # 加点4: ボリューム増加
    vol_ratio = float(row.get("volume_ratio", np.nan)) if not np.isnan(row.get("volume_ratio", np.nan)) else np.nan
    if not np.isnan(vol_ratio) and vol_ratio >= params["volume_multiplier"]:
        score += 1
        score_details.append(f"出来高{vol_ratio:.1f}x")

    # 加点5: RSI理想ゾーン（45-65）
    if 45 <= rsi_val <= 65:
        score += 1
        score_details.append(f"RSI理想({rsi_val:.0f})")

    min_score = params.get("min_score", 3)
    if score < min_score:
        return None

    strength = _calc_signal_strength(row, params)
    reason_str = f"GC+RSI{rsi_val:.0f} [{'/'.join(score_details)}] score={score}/5"

    return Signal(
        ticker="",
        date=date,
        action="BUY",
        price=row["close"],
        reason=reason_str,
        strength=strength,
        indicators={
            "rsi": rsi_val,
            "macd_hist": float(row["macd_hist"]) if not np.isnan(row["macd_hist"]) else 0,
            "volume_ratio": vol_ratio,
            "ema_fast": float(row["ema_fast"]),
            "ema_slow": float(row["ema_slow"]),
            "ema_trend": float(row["ema_trend"]),
            "signal_score": score,
        },
    )


def generate_sell_signal(
    df: pd.DataFrame,
    date: pd.Timestamp,
    entry_price: float,
    highest_price: float,
    params: Optional[Dict] = None,
) -> Optional[Signal]:
    """
    売りシグナルを生成する。

    条件（OR）:
    1. EMAデッドクロス
    2. 損切りライン到達（entry_price × (1 - STOP_LOSS_PCT)）
    3. 利確ライン到達（entry_price × (1 + TAKE_PROFIT_PCT)）
    4. トレーリングストップ（highest_price × (1 - TRAILING_STOP_PCT)）
    5. RSI > 75（過熱）
    """
    if params is None:
        params = STRATEGY_PARAMS

    from trading_system.config import STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_STOP_PCT

    if date not in df.index:
        return None

    idx = df.index.get_loc(date)
    if idx < 1:
        return None

    row = df.loc[date]
    prev_row = df.iloc[idx - 1]
    current_price = row["close"]

    # 1. デッドクロス
    dead_cross = (
        row["ema_fast"] < row["ema_slow"] and
        prev_row["ema_fast"] >= prev_row["ema_slow"]
    )

    # 2. 損切り
    stop_loss_price = entry_price * (1 - STOP_LOSS_PCT)
    stop_loss_hit = current_price <= stop_loss_price

    # 3. 利確
    take_profit_price = entry_price * (1 + TAKE_PROFIT_PCT)
    take_profit_hit = current_price >= take_profit_price

    # 4. トレーリングストップ（含み益が entry_price × 3% 以上の場合のみ発動）
    #    → 含み損中のトレーリングストップは損切りラインに任せる
    trailing_stop_price = highest_price * (1 - TRAILING_STOP_PCT)
    has_meaningful_gain = highest_price >= entry_price * 1.03
    trailing_stop_hit = (
        current_price <= trailing_stop_price and
        has_meaningful_gain
    )

    # 5. 過熱RSI（売りシグナルのRSI閾値はパラメータから動的に取得）
    rsi_val = row["rsi"]
    rsi_threshold = params.get("rsi_upper", 75) + 5   # 買いRSI上限+5で売り
    rsi_overbought = not np.isnan(rsi_val) and rsi_val > rsi_threshold

    if dead_cross:
        reason = f"デッドクロス: EMA{params['ema_fast']}<EMA{params['ema_slow']}"
    elif stop_loss_hit:
        pct = (current_price - entry_price) / entry_price * 100
        reason = f"損切り: {pct:.1f}% 下落"
    elif take_profit_hit:
        pct = (current_price - entry_price) / entry_price * 100
        reason = f"利確: +{pct:.1f}% 上昇"
    elif trailing_stop_hit:
        pct = (current_price - highest_price) / highest_price * 100
        reason = f"トレーリングストップ: 高値から{pct:.1f}%下落"
    elif rsi_overbought:
        reason = f"RSI過熱: {rsi_val:.1f}"
    else:
        return None

    return Signal(
        ticker="",
        date=date,
        action="SELL",
        price=current_price,
        reason=reason,
        strength=1.0,
        indicators={
            "rsi": rsi_val,
            "macd_hist": row["macd_hist"],
            "entry_price": entry_price,
            "highest_price": highest_price,
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
        },
    )


def _calc_signal_strength(row: pd.Series, params: Dict) -> float:
    """
    買いシグナルの強度を 0.0〜1.0 で計算する。
    RSI・MACD・ボリュームを正規化して加重平均。
    """
    scores = []

    # RSI: 55に近いほど強い（中央より少し上が最良）
    rsi = row.get("rsi", 55)
    rsi_score = 1.0 - abs(rsi - 55) / 30
    scores.append(max(0, min(1, rsi_score)))

    # MACD: ヒストグラムが正で大きいほど強い
    macd_hist = row.get("macd_hist", 0)
    macd_score = min(1.0, max(0, macd_hist / (abs(macd_hist) + 1)))
    scores.append(macd_score)

    # ボリューム: 高いほど良い（上限2.5倍）
    vol_ratio = row.get("volume_ratio", 1.0)
    if np.isnan(vol_ratio):
        vol_ratio = 1.0
    vol_score = min(1.0, (vol_ratio - 1.0) / 1.5)
    scores.append(max(0, vol_score))

    return float(np.mean(scores))
