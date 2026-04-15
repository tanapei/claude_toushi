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


class Backtester:
    """
    バックテストエンジン

    動作フロー:
    1. 全銘柄のOHLCVデータ取得
    2. テクニカル指標を全銘柄に追加
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
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.strategy_params = strategy_params or STRATEGY_PARAMS.copy()
        self.tickers = tickers or VALID_UNIVERSE
        self.run_id = run_id or f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        self.portfolio = Portfolio()
        self.logger = TradeLogger()
        self.data: Dict[str, pd.DataFrame] = {}
        self.all_dates: List[pd.Timestamp] = []
        self.nikkei_data: pd.DataFrame = pd.DataFrame()  # 市場レジームフィルタ用

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

        # 2. テクニカル指標追加
        self._add_indicators()

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

    def _load_data(self) -> None:
        """全銘柄のデータを取得する。"""
        logger.info("株価データを取得中...")
        self.data = load_universe_data(self.start_date, self.end_date, self.tickers)
        if not self.data:
            return

        # 日経225データを取得（市場レジームフィルタ用）
        logger.info("日経225データを取得中...")
        nikkei_raw = get_benchmark_data(self.start_date, self.end_date)
        if not nikkei_raw.empty:
            regime_period = self.strategy_params.get("market_regime_ema", 50)
            nikkei_raw["regime_ema"] = nikkei_raw["close"].ewm(
                span=regime_period, adjust=False
            ).mean()
            self.nikkei_data = nikkei_raw
            logger.info(f"日経225: {len(self.nikkei_data)}日分取得完了")

        # 共通日付リストを作成
        all_dates_set = set()
        for df in self.data.values():
            all_dates_set.update(df.index.tolist())
        self.all_dates = sorted(all_dates_set)
        logger.info(f"取引日数: {len(self.all_dates)}日")

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
        """全銘柄にテクニカル指標を追加する。"""
        logger.info("テクニカル指標を計算中...")
        for ticker in list(self.data.keys()):
            try:
                self.data[ticker] = add_indicators(self.data[ticker], self.strategy_params)
            except Exception as e:
                logger.warning(f"[{ticker}] 指標計算エラー: {e}")
                del self.data[ticker]

    def _run_simulation(self, progress_callback=None, cancel_check=None) -> None:
        """
        メインのシミュレーションループ。

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
        日経225が市場レジームEMAを上回っていれば True（強気相場）。
        データがない場合は True を返してフィルタをスキップする。
        """
        if self.nikkei_data.empty:
            return True

        # 当日以前で最も近い日付を取得
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

        return float(close) > float(regime_ema)

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
