"""
バックテストエンジン
指定期間のデータを使って戦略をシミュレートし、
取引履歴・パフォーマンスを記録します。
"""
import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from trading_system.config import (
    BACKTEST_START,
    BACKTEST_END,
    STRATEGY_PARAMS,
    VALID_UNIVERSE,
)
from trading_system.data_fetcher import load_universe_data, get_benchmark_data
from trading_system.strategy import add_indicators, generate_buy_signal, generate_sell_signal, rank_by_momentum
from trading_system.portfolio import Portfolio
from trading_system.analyzer import analyze_backtest_results, print_analysis
from trading_system.trade_logger import TradeLogger

logger = logging.getLogger(__name__)


FACTOR_PARAMS_DEFAULT = {
    "buy_threshold":     70,   # スコア閾値（0-100）
    "stop_loss_pct":      4,   # 損切り（%）最適化済み
    "take_profit_pct":   15,   # 利確（%）
    "trailing_stop_pct":  5,   # トレーリングストップ（%）最適化済み
}


class Backtester:
    """
    バックテストエンジン

    strategy_mode:
      "classic" … EMA/RSI/MACD/ADX による従来戦略（strategy.py）
      "factor"  … ファクタースコア戦略（factor_scorer.py＝現在の自動取引ルール）

    動作フロー:
    1. 全銘柄のOHLCVデータ取得
    2. [classic] テクニカル指標追加 / [factor] ファクタースコア事前計算
    3. 各日付でシグナル生成・注文執行
    4. ポートフォリオ状態を更新
    5. 結果を保存・分析
    """

    def __init__(
        self,
        start_date: str = BACKTEST_START,
        end_date: str = BACKTEST_END,
        strategy_params: Optional[Dict] = None,
        tickers: Optional[List[str]] = None,
        run_id: Optional[str] = None,
        strategy_mode: str = "classic",
        factor_params: Optional[Dict] = None,
    ):
        self.start_date    = start_date
        self.end_date      = end_date
        self.strategy_mode = strategy_mode
        self.strategy_params = strategy_params or STRATEGY_PARAMS.copy()
        self.factor_params = {**FACTOR_PARAMS_DEFAULT, **(factor_params or {})}
        self.tickers = tickers or VALID_UNIVERSE
        self.run_id  = run_id or f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        # ファクターモードでは200日EMAで市場レジームを判定（自動取引と同じ）
        if self.strategy_mode == "factor":
            self.strategy_params = self.strategy_params.copy()
            self.strategy_params["market_regime_ema"] = 200

        self.portfolio   = Portfolio()
        self.logger      = TradeLogger()
        self.data: Dict[str, pd.DataFrame] = {}
        self.all_dates: List[pd.Timestamp] = []
        self.nikkei_data: pd.DataFrame = pd.DataFrame()

    def run(self, save_results: bool = True, analyze: bool = True) -> Dict:
        """
        バックテストを実行する。

        Args:
            save_results: DBに結果を保存するか
            analyze: Claude AIで分析するか

        Returns:
            パフォーマンスサマリー辞書
        """
        logger.info(f"バックテスト開始: {self.run_id}")
        logger.info(f"期間: {self.start_date} 〜 {self.end_date}")
        logger.info(f"対象銘柄数: {len(self.tickers)}")

        # 1. データ取得
        self._load_data()
        if not self.data:
            logger.error("データ取得に失敗しました")
            return {}

        # 2. 指標計算 / スコア事前計算
        if self.strategy_mode == "factor":
            self._factor_scores = self._precompute_factor_scores()
        else:
            self._add_indicators()
            self._factor_scores = {}

        # 3. バックテストループ
        self._run_simulation()

        # 4. パフォーマンス集計
        summary = self.portfolio.get_summary()
        summary["start_date"] = self.start_date
        summary["end_date"] = self.end_date
        self.portfolio.print_summary()

        # 5. 結果保存
        if save_results:
            self._save_results(summary)

        # 6. Claude AI 分析
        analysis = None
        if analyze:
            analysis = self._run_analysis(summary)

        return {
            "run_id": self.run_id,
            "summary": summary,
            "analysis": analysis,
            "trades": self.portfolio.get_trades_df(),
            "equity_curve": self.portfolio.get_equity_df(),
        }

    # ------------------------------------------------------------------
    # 内部処理
    # ------------------------------------------------------------------

    def _load_data(self, warmup_days: int = 300) -> None:
        """全銘柄のデータを取得する。
        ファクタースコア（6ヶ月モメンタム等）を初日から正確に計算するため、
        シミュレーション開始日より warmup_days 日前からデータを取得する。
        シミュレーションループ自体は self.start_date 以降のみを対象にする。
        """
        from datetime import datetime as _dt, timedelta as _td
        fetch_start = (
            _dt.strptime(self.start_date, "%Y-%m-%d") - _td(days=warmup_days)
        ).strftime("%Y-%m-%d")

        logger.info(f"株価データを取得中（ウォームアップ含む: {fetch_start} 〜 {self.end_date}）...")
        self.data = load_universe_data(fetch_start, self.end_date, self.tickers)
        if not self.data:
            return

        # 日経225データを取得（市場レジームフィルタ用）
        logger.info("日経225データを取得中...")
        nikkei_raw = get_benchmark_data(fetch_start, self.end_date)
        if not nikkei_raw.empty:
            regime_period = self.strategy_params.get("market_regime_ema", 50)
            nikkei_raw["regime_ema"] = nikkei_raw["close"].ewm(
                span=regime_period, adjust=False
            ).mean()
            self.nikkei_data = nikkei_raw
            logger.info(f"日経225: {len(self.nikkei_data)}日分取得完了")

        # 共通日付リストをシミュレーション開始日以降に限定
        sim_start_ts = pd.Timestamp(self.start_date)
        all_dates_set = set()
        for df in self.data.values():
            all_dates_set.update(df.index.tolist())
        self.all_dates = sorted(d for d in all_dates_set if d >= sim_start_ts)
        logger.info(f"取引日数: {len(self.all_dates)}日（ウォームアップ除く）")

    def _precompute_factor_scores(self) -> Dict:
        """
        全銘柄×全日付のファクタースコアを事前計算する（ファクターモード用）。
        Returns: {date: {ticker: score}}
        """
        from trading_system.factor_scorer import score_stock
        logger.info("ファクタースコアを事前計算中（しばらくお待ちください）...")
        scores: Dict = {}
        n = len(self.all_dates)

        for i, date in enumerate(self.all_dates):
            if i % 100 == 0:
                logger.info(f"  スコア計算: {date.date()} ({i}/{n}日)")

            # 6ヶ月モメンタムを全銘柄で計算してランク付け
            momentum: Dict[str, float] = {}
            for ticker, df in self.data.items():
                df_s = df[df.index <= date]
                if len(df_s) >= 126:
                    ret = (float(df_s["close"].iloc[-1]) - float(df_s["close"].iloc[-126])) \
                          / float(df_s["close"].iloc[-126])
                    momentum[ticker] = ret

            if momentum:
                ranked   = sorted(momentum, key=momentum.get, reverse=True)
                rank_map = {t: idx / len(ranked) for idx, t in enumerate(ranked)}
            else:
                rank_map = {t: 0.5 for t in self.data}

            date_scores: Dict[str, int] = {}
            for ticker, df in self.data.items():
                df_s = df[df.index <= date]
                if len(df_s) < 60:
                    continue
                rank_pct = rank_map.get(ticker, 0.5)
                score, _, _ = score_stock(df_s, rank_pct)
                date_scores[ticker] = score

            scores[date] = date_scores

        logger.info(f"ファクタースコア計算完了: {len(scores)}日分")
        return scores

    def set_raw_data(
        self,
        raw_data: Dict[str, pd.DataFrame],
        nikkei_raw: pd.DataFrame,
        all_dates: List[pd.Timestamp],
    ) -> None:
        """
        プリロード済みの生データを注入する。
        イテレーティブモードで毎回ダウンロードを避けるために使用。
        呼び出し後に _add_indicators() を別途実行すること。
        """
        self.data = {ticker: df.copy() for ticker, df in raw_data.items()}
        self.all_dates = list(all_dates)
        if not nikkei_raw.empty:
            regime_period = self.strategy_params.get("market_regime_ema", 50)
            nikkei_copy = nikkei_raw.copy()
            nikkei_copy["regime_ema"] = nikkei_copy["close"].ewm(
                span=regime_period, adjust=False
            ).mean()
            self.nikkei_data = nikkei_copy

    def _add_indicators(self) -> None:
        """全銘柄にテクニカル指標を追加する（classic モードのみ使用）。"""
        if self.strategy_mode == "factor":
            return
        logger.info("テクニカル指標を計算中...")
        for ticker in list(self.data.keys()):
            try:
                self.data[ticker] = add_indicators(self.data[ticker], self.strategy_params)
            except Exception as e:
                logger.warning(f"[{ticker}] 指標計算エラー: {e}")
                del self.data[ticker]

    def _run_simulation_factor(self, progress_callback=None, cancel_check=None) -> None:
        """ファクタースコア戦略のシミュレーションループ（自動取引と同じルール）。"""
        fp              = self.factor_params
        buy_threshold   = int(fp.get("buy_threshold",    70))
        stop_loss       = float(fp.get("stop_loss_pct",   5)) / 100
        take_profit     = float(fp.get("take_profit_pct", 15)) / 100
        trailing_stop   = float(fp.get("trailing_stop_pct", 6)) / 100
        factor_scores   = getattr(self, "_factor_scores", {})

        logger.info(f"ファクター戦略シミュレーション開始 "
                    f"(閾値={buy_threshold}pt, 損切={stop_loss*100:.0f}%, "
                    f"利確={take_profit*100:.0f}%, トレーリング={trailing_stop*100:.0f}%)")
        n_dates = len(self.all_dates)
        step    = max(1, n_dates // 20)

        for i, date in enumerate(self.all_dates):
            if cancel_check and cancel_check():
                break
            if i % step == 0:
                pct = int(i / n_dates * 100)
                msg = f"[ファクター戦略] {date.date()} ({i}/{n_dates}日)"
                if progress_callback:
                    progress_callback(pct, msg)
                else:
                    logger.info(f"進捗: {pct}% {date.date()}")

            current_prices = self._get_prices_at(date)

            # ① 損切り・利確・トレーリングストップ
            for ticker, pos in list(self.portfolio.positions.items()):
                price = current_prices.get(ticker)
                if price is None:
                    continue
                ep  = pos.entry_price
                hp  = pos.highest_price
                pnl = (price - ep) / ep
                if price <= ep * (1 - stop_loss):
                    reason = f"損切りライン到達（{pnl*100:+.1f}%）"
                elif price >= ep * (1 + take_profit):
                    reason = f"利確ライン到達（{pnl*100:+.1f}%）"
                elif hp > ep * 1.03 and price <= hp * (1 - trailing_stop):
                    dd = (price - hp) / hp * 100
                    reason = f"トレーリングストップ（高値から{dd:.1f}%下落）"
                else:
                    continue
                self.portfolio.sell(ticker=ticker, date=date, price=price, reason=reason)

            # ② 市場レジームチェック（日経225 vs 200日EMA）
            market_bullish = self._is_market_bullish(date)

            # ③ 買いシグナル（スコア閾値超え・スコア降順）
            if market_bullish and len(self.portfolio.positions) < self.portfolio.max_positions:
                day_scores = factor_scores.get(date, {})
                candidates = sorted(
                    [(t, s) for t, s in day_scores.items() if s >= buy_threshold],
                    key=lambda x: x[1], reverse=True,
                )
                for ticker, score in candidates:
                    if ticker in self.portfolio.positions:
                        continue
                    if len(self.portfolio.positions) >= self.portfolio.max_positions:
                        break
                    price = current_prices.get(ticker, 0)
                    if price <= 0:
                        continue
                    self.portfolio.buy(
                        ticker=ticker, date=date, price=price,
                        reason=f"ファクタースコア {score}点",
                    )

            # ④ トレーリングストップ高値更新 & 日次資産記録
            self.portfolio.update_trailing_stops(date, current_prices)
            self.portfolio.record_equity(date, current_prices)

        self._close_all_positions()
        logger.info("ファクター戦略シミュレーション完了")

    def _run_simulation(self, progress_callback=None, cancel_check=None) -> None:
        """モードに応じて classic / factor シミュレーションを実行する。"""
        if self.strategy_mode == "factor":
            return self._run_simulation_factor(progress_callback, cancel_check)
        return self._run_simulation_classic(progress_callback, cancel_check)

    def _run_simulation_classic(self, progress_callback=None, cancel_check=None) -> None:
        """
        従来戦略（EMA/RSI/MACD/ADX）のシミュレーションループ。

        Args:
            progress_callback: (pct: int, msg: str) -> None  進捗通知コールバック
            cancel_check: () -> bool  True を返したらループを中断
        """
        params = self.strategy_params
        top_n = params.get("top_n_momentum", 10)

        logger.info("シミュレーション開始...")
        n_dates = len(self.all_dates)
        step = max(1, n_dates // 20)

        for i, date in enumerate(self.all_dates):
            # キャンセル確認
            if cancel_check and cancel_check():
                logger.info("シミュレーションがキャンセルされました")
                break

            if i % step == 0:
                pct = int(i / n_dates * 100)
                msg = f"シミュレーション中... {date.date()} ({i}/{n_dates}日)"
                if progress_callback:
                    progress_callback(pct, msg)
                else:
                    logger.info(f"進捗: {pct}% ({date.date()}) | ポジション: {len(self.portfolio.positions)}")

            # 現在価格の辞書
            current_prices = self._get_prices_at(date)

            # --- ① 保有ポジションの売りシグナルチェック ---
            positions_to_sell = []
            for ticker, pos in list(self.portfolio.positions.items()):
                if ticker not in self.data or date not in self.data[ticker].index:
                    continue
                sell_signal = generate_sell_signal(
                    df=self.data[ticker],
                    date=date,
                    entry_price=pos.entry_price,
                    highest_price=pos.highest_price,
                    params=params,
                    entry_date=pos.entry_date,
                )
                if sell_signal:
                    positions_to_sell.append((ticker, sell_signal))

            for ticker, signal in positions_to_sell:
                self.portfolio.sell(
                    ticker=ticker,
                    date=date,
                    price=signal.price,
                    reason=signal.reason,
                    indicators=signal.indicators,
                )

            # --- ② 市場レジームチェック（日経225が弱気なら新規買い禁止）---
            market_bullish = self._is_market_bullish(date)

            # --- ③ 買いシグナルチェック（モメンタム上位から） ---
            if market_bullish and len(self.portfolio.positions) < self.portfolio.max_positions:
                candidates = rank_by_momentum(self.data, date, top_n)

                for ticker in candidates:
                    if ticker in self.portfolio.positions:
                        continue
                    if len(self.portfolio.positions) >= self.portfolio.max_positions:
                        break
                    if ticker not in self.data or date not in self.data[ticker].index:
                        continue

                    buy_signal = generate_buy_signal(
                        df=self.data[ticker],
                        date=date,
                        params=params,
                    )
                    if buy_signal:
                        buy_signal.ticker = ticker
                        self.portfolio.buy(
                            ticker=ticker,
                            date=date,
                            price=buy_signal.price,
                            reason=buy_signal.reason,
                            indicators=buy_signal.indicators,
                        )

            # --- ③ トレーリングストップ更新 ---
            self.portfolio.update_trailing_stops(date, current_prices)

            # --- ④ 日次資産記録 ---
            self.portfolio.record_equity(date, current_prices)

        # 期末に残ポジションを全決済
        self._close_all_positions()
        logger.info("シミュレーション完了")

    def _get_prices_at(self, date: pd.Timestamp) -> Dict[str, float]:
        """指定日の各銘柄終値を返す。"""
        prices = {}
        for ticker, df in self.data.items():
            if date in df.index:
                prices[ticker] = float(df.loc[date, "close"])
        return prices

    def _is_market_bullish(self, date: pd.Timestamp) -> bool:
        """
        強気相場の判定（2条件すべてを満たす必要あり）:
          1. 日経225 > 200日EMA（中長期トレンド）
          2. 日経225が直近20日高値から -5% 以内（急落検知）
        データがない場合は True を返してフィルタをスキップする。
        """
        if self.nikkei_data.empty:
            return True

        available = self.nikkei_data.index[self.nikkei_data.index <= date]
        if len(available) == 0:
            return True

        last = available[-1]
        row = self.nikkei_data.loc[last]
        close = row.get("close", float("nan"))
        regime_ema = row.get("regime_ema", float("nan"))

        import math
        if math.isnan(close) or math.isnan(regime_ema):
            return True

        # 条件1: 中長期トレンド（200日EMA上）
        if float(close) <= float(regime_ema):
            return False

        # 条件2: 急落検知（直近20日高値から-5%超の下落で新規エントリー停止）
        recent = available[-20:]
        recent_high = float(self.nikkei_data.loc[recent, "close"].max())
        if recent_high > 0 and (float(close) - recent_high) / recent_high < -0.05:
            return False

        return True

    def _close_all_positions(self) -> None:
        """バックテスト終了時に残ポジションを全決済する。"""
        if not self.portfolio.positions:
            return

        last_date = self.all_dates[-1]
        current_prices = self._get_prices_at(last_date)

        for ticker in list(self.portfolio.positions.keys()):
            price = current_prices.get(ticker, self.portfolio.positions[ticker].entry_price)
            self.portfolio.sell(
                ticker=ticker,
                date=last_date,
                price=price,
                reason="バックテスト終了・強制決済",
            )

    def _save_results(self, summary: Dict) -> None:
        """バックテスト結果をDBに保存する。"""
        self.logger.save_session(
            run_id=self.run_id,
            start_date=self.start_date,
            end_date=self.end_date,
            summary=summary,
            strategy_params=self.strategy_params,
        )
        trades = [t.to_dict() for t in self.portfolio.trades]
        self.logger.save_trades(self.run_id, trades)
        self.logger.save_equity_curve(self.run_id, self.portfolio.equity_curve)
        logger.info(f"結果をDBに保存しました: {self.run_id}")

    def _run_analysis(self, summary: Dict) -> Optional[Dict]:
        """Claude AIで結果を分析する。"""
        logger.info("Claude AI による分析を実行中...")

        # 過去の分析結果を取得（フィードバックループ）
        latest_analysis = self.logger.get_latest_analysis()
        previous_analyses = [latest_analysis] if latest_analysis else []

        trades = [t.to_dict() for t in self.portfolio.trades]
        equity_curve = self.portfolio.equity_curve

        analysis = analyze_backtest_results(
            summary=summary,
            trades=trades,
            equity_curve=equity_curve,
            current_params=self.strategy_params,
            previous_analyses=previous_analyses,
        )

        print_analysis(analysis)

        # 分析結果をDBに保存
        self.logger.save_analysis(
            run_id=self.run_id,
            analysis_type="post_backtest",
            good_points=analysis.get("good_points", []),
            bad_points=analysis.get("bad_points", []),
            improvement_suggestions=analysis.get("improvement_suggestions", []),
            next_strategy_params=analysis.get("next_strategy_params", {}),
            full_analysis=analysis.get("full_analysis", ""),
        )

        return analysis


def run_iterative_backtest(
    n_iterations: int = 3,
    start_date: str = BACKTEST_START,
    end_date: str = BACKTEST_END,
) -> List[Dict]:
    """
    複数回のバックテストを実行し、Claude AIの改善提案を次回に反映する
    イテレーティブなバックテスト。

    Args:
        n_iterations: 反復回数
        start_date: バックテスト開始日
        end_date: バックテスト終了日

    Returns:
        各イテレーションの結果リスト
    """
    results = []
    params = STRATEGY_PARAMS.copy()
    trade_logger = TradeLogger()

    print(f"\n{'='*60}")
    print(f"  イテレーティブバックテスト開始（{n_iterations}回）")
    print(f"{'='*60}\n")

    for iteration in range(1, n_iterations + 1):
        print(f"\n[イテレーション {iteration}/{n_iterations}]")
        print(f"現在のパラメータ: EMA({params['ema_fast']}/{params['ema_slow']}), "
              f"RSI({params['rsi_lower']}-{params['rsi_upper']})")

        backtester = Backtester(
            start_date=start_date,
            end_date=end_date,
            strategy_params=params,
        )

        result = backtester.run(save_results=True, analyze=True)
        results.append(result)

        # 次のイテレーションのパラメータをClaudeの提案から取得
        if result.get("analysis") and iteration < n_iterations:
            next_params = result["analysis"].get("next_strategy_params", {})
            if next_params:
                params = next_params
                print(f"\n次回パラメータを更新: {params}")

    # 全イテレーションのサマリー
    print(f"\n{'='*60}")
    print(f"  全{n_iterations}回のバックテスト完了")
    print(f"{'='*60}")
    for i, r in enumerate(results, 1):
        s = r.get("summary", {})
        print(f"  イテレーション{i}: リターン {s.get('total_return_pct', 0):+.2f}% "
              f"| 勝率 {s.get('win_rate', 0):.1f}% "
              f"| シャープ {s.get('sharpe_ratio', 0):.2f}")

    # 累積パフォーマンス
    cum_perf = trade_logger.get_cumulative_performance()
    if cum_perf:
        print(f"\n累積パフォーマンス（全セッション）:")
        print(f"  平均リターン: {cum_perf['avg_return_pct']:+.2f}%")
        print(f"  最高リターン: {cum_perf['best_return_pct']:+.2f}%")
        print(f"  平均勝率: {cum_perf['avg_win_rate']:.1f}%")
        print(f"  平均シャープ: {cum_perf['avg_sharpe']:.2f}")

    return results
