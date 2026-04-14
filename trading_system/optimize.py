"""
パラメータ最適化エンジン
グリッドサーチ + ウォークフォワード検証で最良のパラメータセットを探索します。

【なぜ最適化が必要か】
  EMAの期間・RSIの閾値・ADX閾値などは市場環境によって最適値が変わります。
  手動で試すと数十回のバックテストが必要ですが、グリッドサーチで自動化できます。

【オーバーフィットを防ぐ方法（ウォークフォワード）】
  データを「学習期間」と「検証期間」に分割し、
  学習期間で最良だったパラメータが検証期間でも有効か確認します。
  例: 2022〜2023年で最適化 → 2024年で検証
"""
import itertools
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from trading_system.config import (
    BACKTEST_START,
    BACKTEST_END,
    STRATEGY_PARAMS,
    VALID_UNIVERSE,
    RESULTS_DIR,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 最適化対象パラメータグリッド
# ─────────────────────────────────────────────
DEFAULT_PARAM_GRID = {
    "ema_fast":       [5, 9, 12],
    "ema_slow":       [21, 26, 34],
    "ema_trend":      [50, 75, 100],
    "rsi_lower":      [25, 30, 35],
    "rsi_upper":      [70, 75, 80],
    "adx_threshold":  [15, 20, 25],
    "min_score":      [2, 3],
    "confirm_days":   [2, 3],
}

# 固定パラメータ（最適化しない）
FIXED_PARAMS = {
    "rsi_period":      14,
    "macd_fast":       12,
    "macd_slow":       26,
    "macd_signal":     9,
    "momentum_period": 120,
    "momentum_skip":   5,
    "top_n_momentum":  15,
    "volume_ma_period": 20,
    "volume_multiplier": 1.0,
    "adx_period":      14,
    "max_hold_days":   30,
    "market_regime_ema": 50,
}


def run_optimization(
    start_date: str = BACKTEST_START,
    end_date:   str = BACKTEST_END,
    param_grid: Optional[Dict[str, List]] = None,
    max_combinations: int = 50,
    use_walk_forward: bool = True,
    progress_callback=None,
) -> Dict:
    """
    グリッドサーチでパラメータを最適化する。

    Args:
        start_date: バックテスト開始日
        end_date:   バックテスト終了日
        param_grid: 探索するパラメータ範囲（Noneでデフォルト）
        max_combinations: 最大試行数（多いほど精度高いが時間かかる）
        use_walk_forward: ウォークフォワード検証を使うか
        progress_callback: 進捗コールバック fn(current, total, message)

    Returns:
        {
          "best_params": {...},
          "best_score": float,
          "all_results": [...],
          "walk_forward": {...} (use_walk_forward=True の場合)
        }
    """
    if param_grid is None:
        param_grid = DEFAULT_PARAM_GRID

    # ─── データを先に1回だけ取得してキャッシュ ───
    logger.info("データ取得中（最適化前の一括取得）...")
    from trading_system.data_fetcher import load_universe_data, get_benchmark_data
    from trading_system.strategy import add_indicators
    from trading_system.config import STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_STOP_PCT

    raw_data = load_universe_data(start_date, end_date)
    nikkei_raw = get_benchmark_data(start_date, end_date)

    if not raw_data:
        return {"error": "データ取得失敗"}

    # ─── パラメータの組み合わせを生成 ───
    keys   = list(param_grid.keys())
    values = list(param_grid.values())
    all_combos = list(itertools.product(*values))

    # 無効な組み合わせを除外（ema_fast < ema_slow でないもの等）
    valid_combos = []
    for combo in all_combos:
        p = dict(zip(keys, combo))
        if p.get("ema_fast", 9) >= p.get("ema_slow", 26):
            continue
        valid_combos.append(p)

    # ランダムサンプリング（全探索が多すぎる場合）
    import random
    if len(valid_combos) > max_combinations:
        random.seed(42)
        valid_combos = random.sample(valid_combos, max_combinations)

    logger.info(f"最適化: {len(valid_combos)}パターンを評価します")
    total = len(valid_combos)

    # ─── ウォークフォワード設定 ───
    if use_walk_forward:
        # 前半70%を学習、後半30%を検証
        start_dt = pd.Timestamp(start_date)
        end_dt   = pd.Timestamp(end_date)
        days     = (end_dt - start_dt).days
        train_end = (start_dt + pd.Timedelta(days=int(days * 0.7))).strftime("%Y-%m-%d")
        test_start = (start_dt + pd.Timedelta(days=int(days * 0.7) + 1)).strftime("%Y-%m-%d")
        logger.info(f"ウォークフォワード: 学習 {start_date}〜{train_end} / 検証 {test_start}〜{end_date}")
    else:
        train_end  = end_date
        test_start = start_date

    # ─── 各パラメータセットを評価 ───
    all_results = []

    for i, combo_params in enumerate(valid_combos):
        params = {**FIXED_PARAMS, **combo_params}

        if progress_callback:
            pct = int(i / total * 85)
            progress_callback(i, total, f"[{i+1}/{total}] EMA({params['ema_fast']}/{params['ema_slow']}) ADX>{params['adx_threshold']} RSI({params['rsi_lower']}-{params['rsi_upper']})")

        try:
            # 学習期間でバックテスト
            train_score = _quick_backtest(
                raw_data=raw_data,
                nikkei_raw=nikkei_raw,
                params=params,
                start_date=start_date,
                end_date=train_end,
            )

            result = {
                "params": params,
                "train": train_score,
                "test": None,
                "combined_score": _compute_score(train_score),
            }

            # 検証期間でバックテスト（ウォークフォワード）
            if use_walk_forward and test_start < end_date:
                test_score = _quick_backtest(
                    raw_data=raw_data,
                    nikkei_raw=nikkei_raw,
                    params=params,
                    start_date=test_start,
                    end_date=end_date,
                )
                result["test"] = test_score
                # 学習+検証の調和平均でスコア算出（過学習ペナルティ）
                train_s = _compute_score(train_score)
                test_s  = _compute_score(test_score)
                overfit_penalty = max(0, (train_s - test_s) / (abs(train_s) + 1e-6))
                result["combined_score"] = test_s * (1 - 0.3 * overfit_penalty)

            all_results.append(result)

        except Exception as e:
            logger.debug(f"パラメータ評価エラー: {e}")
            continue

    if not all_results:
        return {"error": "有効な結果なし"}

    # ─── 最良パラメータの選択 ───
    all_results.sort(key=lambda x: x["combined_score"], reverse=True)
    best = all_results[0]

    if progress_callback:
        progress_callback(total, total, "最適化完了")

    output = {
        "best_params":   best["params"],
        "best_score":    round(best["combined_score"], 4),
        "best_train":    best["train"],
        "best_test":     best.get("test"),
        "top10":         _summarize_results(all_results[:10]),
        "total_tested":  len(all_results),
        "walk_forward":  use_walk_forward,
        "train_period":  f"{start_date} 〜 {train_end}",
        "test_period":   f"{test_start} 〜 {end_date}" if use_walk_forward else None,
        "completed_at":  datetime.now().isoformat(),
    }

    # JSON保存
    out_path = RESULTS_DIR / f"optimize_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    logger.info(f"最適化結果保存: {out_path}")

    return output


def _quick_backtest(
    raw_data: Dict[str, pd.DataFrame],
    nikkei_raw: pd.DataFrame,
    params: Dict,
    start_date: str,
    end_date: str,
) -> Dict:
    """
    データを再利用して高速バックテストを実行する。
    （データ取得コストをゼロにして多数の試行を可能にする）
    """
    from trading_system.strategy import add_indicators, generate_buy_signal, generate_sell_signal, rank_by_momentum
    from trading_system.portfolio import Portfolio
    from trading_system.config import STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_STOP_PCT

    start_ts = pd.Timestamp(start_date)
    end_ts   = pd.Timestamp(end_date)

    # 指標を再計算（パラメータが変わるため）
    data = {}
    for ticker, raw_df in raw_data.items():
        subset = raw_df[(raw_df.index >= start_ts) & (raw_df.index <= end_ts)]
        if len(subset) < 60:
            continue
        try:
            data[ticker] = add_indicators(subset, params)
        except Exception:
            continue

    if not data:
        return {"total_return_pct": 0, "win_rate": 0, "sharpe_ratio": 0, "max_drawdown_pct": 0, "total_trades": 0}

    # 日経225レジームデータ
    nikkei_in_range = pd.DataFrame()
    if not nikkei_raw.empty:
        regime_period = params.get("market_regime_ema", 50)
        nk = nikkei_raw[(nikkei_raw.index >= start_ts) & (nikkei_raw.index <= end_ts)].copy()
        nk["regime_ema"] = nk["close"].ewm(span=regime_period, adjust=False).mean()
        nikkei_in_range = nk

    all_dates = sorted(set().union(*[set(df.index) for df in data.values()]))
    portfolio = Portfolio()
    top_n = params.get("top_n_momentum", 15)

    for date in all_dates:
        # 売りチェック
        for ticker, pos in list(portfolio.positions.items()):
            if ticker not in data or date not in data[ticker].index:
                continue
            sig = generate_sell_signal(
                df=data[ticker], date=date,
                entry_price=pos.entry_price,
                highest_price=pos.highest_price,
                params=params,
                entry_date=pos.entry_date,
            )
            if sig:
                portfolio.sell(ticker, date, sig.price, sig.reason)

        # 市場レジームチェック
        market_ok = True
        if not nikkei_in_range.empty:
            avail = nikkei_in_range.index[nikkei_in_range.index <= date]
            if len(avail) > 0:
                row = nikkei_in_range.loc[avail[-1]]
                c, r = float(row.get("close", float("nan"))), float(row.get("regime_ema", float("nan")))
                if not (np.isnan(c) or np.isnan(r)):
                    market_ok = c > r

        # 買いチェック
        if market_ok and len(portfolio.positions) < portfolio.max_positions:
            for ticker in rank_by_momentum(data, date, top_n):
                if ticker in portfolio.positions:
                    continue
                if len(portfolio.positions) >= portfolio.max_positions:
                    break
                if ticker not in data or date not in data[ticker].index:
                    continue
                sig = generate_buy_signal(data[ticker], date, params)
                if sig:
                    sig.ticker = ticker
                    portfolio.buy(ticker, date, sig.price, sig.reason, sig.indicators)

        prices = {t: float(df.loc[date, "close"]) for t, df in data.items() if date in df.index}
        portfolio.update_trailing_stops(date, prices)
        portfolio.record_equity(date, prices)

    # 残ポジション決済
    if all_dates and portfolio.positions:
        last = all_dates[-1]
        prices = {t: float(df.loc[last, "close"]) for t, df in data.items() if last in df.index}
        for ticker in list(portfolio.positions.keys()):
            p = prices.get(ticker, portfolio.positions[ticker].entry_price)
            portfolio.sell(ticker, last, p, "期末強制決済")

    return portfolio.get_summary()


def _compute_score(summary: Dict) -> float:
    """
    パラメータセットの総合スコアを計算する。
    シャープ比を軸に、勝率・ドローダウン・取引数で調整。
    """
    if not summary or "error" in summary:
        return -999.0

    sharpe  = summary.get("sharpe_ratio", 0) or 0
    win_rate = summary.get("win_rate", 0) or 0
    max_dd  = summary.get("max_drawdown_pct", 0) or 0
    ret     = summary.get("total_return_pct", 0) or 0
    n_trades = summary.get("total_trades", 0) or 0

    # 取引が少なすぎると統計的意味がない
    if n_trades < 5:
        return -999.0

    # スコア計算（重み付き）
    # シャープ比が最重要、次にリターン、勝率でボーナス、DDでペナルティ
    score = (
        sharpe * 2.0           # シャープ比（最重要）
        + ret * 0.05           # 総リターン
        + (win_rate - 40) * 0.03  # 勝率（40%基準でボーナス/ペナルティ）
        + max_dd * 0.05        # ドローダウン（負の値なのでペナルティ）
    )
    return round(score, 4)


def _summarize_results(results: List[Dict]) -> List[Dict]:
    """上位結果を表示用に整形する。"""
    summary = []
    for r in results:
        p = r.get("params", {})
        tr = r.get("train", {}) or {}
        te = r.get("test", {}) or {}
        summary.append({
            "score":       round(r.get("combined_score", 0), 3),
            "ema":         f"{p.get('ema_fast')}/{p.get('ema_slow')}",
            "ema_trend":   p.get("ema_trend"),
            "rsi_range":   f"{p.get('rsi_lower')}-{p.get('rsi_upper')}",
            "adx_thresh":  p.get("adx_threshold"),
            "min_score":   p.get("min_score"),
            "confirm_days": p.get("confirm_days"),
            "train_return": round(tr.get("total_return_pct", 0), 2),
            "train_winrate": round(tr.get("win_rate", 0), 1),
            "train_sharpe": round(tr.get("sharpe_ratio", 0), 2),
            "train_dd":    round(tr.get("max_drawdown_pct", 0), 2),
            "test_return":  round(te.get("total_return_pct", 0), 2) if te else None,
            "test_winrate": round(te.get("win_rate", 0), 1) if te else None,
            "test_sharpe":  round(te.get("sharpe_ratio", 0), 2) if te else None,
        })
    return summary
