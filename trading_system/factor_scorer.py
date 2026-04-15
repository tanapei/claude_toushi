"""
ファクタースコアリングモジュール
各銘柄に 0〜100 点のスコアをつけ、売買シグナルを判定する。

スコア構成（合計100点）:
  モメンタム  30点: 過去6ヶ月リターンが全銘柄中の上位30%
  トレンド    20点: 価格が50日EMAより上
  EMAクロス   20点: 短期EMAが長期EMAを上抜け（直近5日以内）
  出来高      15点: 直近出来高が20日平均を上回る
  RSI         15点: RSIが40〜70の適正範囲

買いシグナル条件: 70点以上 かつ 市場レジームが強気
"""
import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

BUY_THRESHOLD = 70  # 買いシグナル閾値（70点以上）


# ─── 指標計算ユーティリティ ────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ─── 1銘柄スコアリング ──────────────────────────────────

def score_stock(
    df: pd.DataFrame,
    momentum_rank_pct: float,
) -> Tuple[int, List[str], Dict]:
    """
    1銘柄のファクタースコアを計算する。

    Args:
        df: OHLCV DataFrame（closeカラム必須）
        momentum_rank_pct: モメンタム順位 0.0（最上位）〜 1.0（最下位）

    Returns:
        (score, reasons, details)
    """
    if len(df) < 60:
        return 0, ["データ不足（60日分未満）"], {}

    score = 0
    reasons: List[str] = []
    details: Dict = {}

    close = df["close"]
    latest_close = float(close.iloc[-1])

    # ① モメンタム（30点）: 過去6ヶ月リターン上位30%
    details["momentum_rank_pct"] = round(momentum_rank_pct * 100, 1)
    if momentum_rank_pct <= 0.30:
        score += 30
        reasons.append(f"モメンタム上位{int(momentum_rank_pct * 100)}%（6ヶ月リターン優秀）")
    elif momentum_rank_pct <= 0.50:
        score += 15
        reasons.append(f"モメンタム上位{int(momentum_rank_pct * 100)}%（平均以上）")

    # ② トレンド（20点）: 価格 > 50日EMA
    ema50 = float(_ema(close, 50).iloc[-1])
    details["ema50"] = round(ema50, 2)
    details["price"] = round(latest_close, 2)
    if latest_close > ema50:
        score += 20
        diff_pct = (latest_close - ema50) / ema50 * 100
        reasons.append(f"50日EMA上（+{diff_pct:.1f}%）")
    else:
        diff_pct = (latest_close - ema50) / ema50 * 100
        reasons.append(f"50日EMA下（{diff_pct:.1f}%）")

    # ③ EMAクロス（20点）: 短期9日EMAが長期26日EMAを上抜け（直近5日以内）
    ema9  = _ema(close, 9)
    ema26 = _ema(close, 26)
    ema9_now  = float(ema9.iloc[-1])
    ema26_now = float(ema26.iloc[-1])
    details["ema9"]  = round(ema9_now, 2)
    details["ema26"] = round(ema26_now, 2)

    golden_cross = False
    for i in range(1, min(6, len(df))):
        if (float(ema9.iloc[-i]) > float(ema26.iloc[-i]) and
                float(ema9.iloc[-i - 1]) <= float(ema26.iloc[-i - 1])):
            golden_cross = True
            break

    if golden_cross:
        score += 20
        reasons.append("EMAゴールデンクロス発生（直近5日以内）")
    elif ema9_now > ema26_now:
        score += 10
        reasons.append("短期EMA > 長期EMA（上昇トレンド継続）")
    else:
        reasons.append("短期EMA < 長期EMA（下降トレンド）")

    # ④ 出来高（15点）: 直近出来高 > 20日平均
    if "volume" in df.columns:
        latest_vol = float(df["volume"].iloc[-1])
        vol_ma20   = float(df["volume"].rolling(20).mean().iloc[-1])
        details["volume_ratio"] = round(latest_vol / vol_ma20, 2) if vol_ma20 > 0 else 0
        if vol_ma20 > 0 and latest_vol > vol_ma20:
            score += 15
            reasons.append(f"出来高増加（平均の{latest_vol / vol_ma20:.1f}倍）")
        else:
            reasons.append("出来高は平均以下")

    # ⑤ RSI（15点）: 40〜70 の適正範囲
    rsi_val = float(_rsi(close, 14).iloc[-1])
    details["rsi"] = round(rsi_val, 1)
    if 40 <= rsi_val <= 70:
        score += 15
        reasons.append(f"RSI適正（{rsi_val:.0f}）")
    elif rsi_val > 70:
        reasons.append(f"RSI過熱（{rsi_val:.0f}）")
    else:
        reasons.append(f"RSI低水準（{rsi_val:.0f}）")

    return score, reasons, details


# ─── 全銘柄スコアリング ────────────────────────────────

def score_universe(
    data: Dict[str, pd.DataFrame],
    market_bullish: bool = True,
) -> List[Dict]:
    """
    全銘柄をスコアリングしてリストを返す（スコア降順）。

    Returns:
        List of {ticker, score, reasons, details, signal, momentum_6m_pct}
    """
    if not data:
        return []

    # クロスセクショナル・モメンタム計算（全銘柄の6ヶ月リターン）
    momentum: Dict[str, float] = {}
    for ticker, df in data.items():
        if len(df) >= 126:
            ret = (float(df["close"].iloc[-1]) - float(df["close"].iloc[-126])) / float(df["close"].iloc[-126])
            momentum[ticker] = ret

    # 順位付け（0.0 = 最上位）
    if momentum:
        ranked = sorted(momentum, key=momentum.get, reverse=True)
        rank_map = {t: i / len(ranked) for i, t in enumerate(ranked)}
    else:
        rank_map = {t: 0.5 for t in data}

    results = []
    for ticker, df in data.items():
        rank_pct = rank_map.get(ticker, 0.5)
        score, reasons, details = score_stock(df, rank_pct)

        # 市場レジームが弱気なら買いシグナルを抑制
        if score >= BUY_THRESHOLD and market_bullish:
            signal = "buy"
        elif score >= BUY_THRESHOLD:
            signal = "watch"  # 条件は満たすが市場が弱気
        else:
            signal = "none"

        results.append({
            "ticker": ticker,
            "score": score,
            "reasons": reasons,
            "details": details,
            "signal": signal,
            "momentum_6m_pct": round(momentum.get(ticker, 0) * 100, 2),
        })

    return sorted(results, key=lambda x: x["score"], reverse=True)


def get_ticker_label(ticker: str) -> str:
    """ティッカーから銘柄名を返す（主要銘柄のみ）"""
    labels = {
        # 日本株
        "7203.T": "トヨタ自動車",    "7267.T": "ホンダ",
        "7269.T": "スズキ",          "7201.T": "日産自動車",
        "6758.T": "ソニーグループ",   "6861.T": "キーエンス",
        "6954.T": "ファナック",       "6902.T": "デンソー",
        "6501.T": "日立製作所",       "6752.T": "パナソニック",
        "6971.T": "京セラ",           "9984.T": "ソフトバンクG",
        "9432.T": "NTT",              "9433.T": "KDDI",
        "9434.T": "ソフトバンク通信", "4755.T": "楽天グループ",
        "8306.T": "三菱UFJ",          "8316.T": "三井住友FG",
        "8411.T": "みずほFG",         "8031.T": "三井物産",
        "8058.T": "三菱商事",         "9983.T": "ファーストリテイリング",
        "2914.T": "日本たばこ産業",   "2502.T": "アサヒグループ",
        "2503.T": "キリンHD",         "4502.T": "武田薬品工業",
        "4519.T": "中外製薬",         "4568.T": "第一三共",
        "8802.T": "三菱地所",         "8830.T": "住友不動産",
        "5020.T": "ENEOS",
        # 米国株
        "AAPL": "アップル",     "MSFT": "マイクロソフト",
        "NVDA": "エヌビディア", "GOOGL": "アルファベット",
        "META": "メタ",         "AMZN": "アマゾン",
        "AMD":  "AMD",          "AVGO": "ブロードコム",
        "ADBE": "アドビ",       "CRM":  "セールスフォース",
        "TSM":  "TSMC",         "QCOM": "クアルコム",
        "TSLA": "テスラ",       "WMT":  "ウォルマート",
        "COST": "コストコ",     "MCD":  "マクドナルド",
        "JNJ":  "J&J",          "LLY":  "イーライリリー",
        "UNH":  "ユナイテッドヘルス",
        "JPM":  "JPモルガン",   "V":    "ビザ",
        "MA":   "マスターカード","GS":  "ゴールドマン",
        "XOM":  "エクソン",     "CVX":  "シェブロン",
        "QQQ":  "Nasdaq100 ETF","SPY":  "S&P500 ETF",
        "VGT":  "テクノロジーETF","XLF": "金融ETF",
    }
    return labels.get(ticker, ticker)
