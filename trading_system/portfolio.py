"""
ポートフォリオ管理モジュール
ポジション管理・注文執行シミュレーション・パフォーマンス計算を担当します。
"""
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from trading_system.config import (
    INITIAL_CAPITAL,
    MAX_POSITIONS,
    POSITION_SIZE_PCT,
    COMMISSION_RATE,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    TRAILING_STOP_PCT,
)

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """保有ポジション"""
    ticker: str
    entry_date: pd.Timestamp
    entry_price: float
    shares: int
    cost: float              # 取得コスト（手数料込み）
    highest_price: float     # 保有中の最高値（トレーリングストップ用）
    entry_reason: str = ""
    entry_indicators: Dict = field(default_factory=dict)


@dataclass
class Trade:
    """完結した取引記録"""
    ticker: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float           # 損益（円）
    pnl_pct: float       # 損益率（%）
    hold_days: int
    exit_reason: str
    entry_indicators: Dict = field(default_factory=dict)
    exit_indicators: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)


class Portfolio:
    """
    ポートフォリオ管理クラス
    現金残高・ポジション・取引履歴を管理し、
    買い/売り注文をシミュレートします。
    """

    def __init__(
        self,
        initial_capital: float = INITIAL_CAPITAL,
        max_positions: int = MAX_POSITIONS,
        position_size_pct: float = POSITION_SIZE_PCT,
        commission_rate: float = COMMISSION_RATE,
    ):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.max_positions = max_positions
        self.position_size_pct = position_size_pct
        self.commission_rate = commission_rate

        self.positions: Dict[str, Position] = {}  # ticker -> Position
        self.trades: List[Trade] = []
        self.equity_curve: List[Dict] = []        # 日別資産推移

    # ------------------------------------------------------------------
    # 注文処理
    # ------------------------------------------------------------------

    def buy(
        self,
        ticker: str,
        date: pd.Timestamp,
        price: float,
        reason: str = "",
        indicators: Optional[Dict] = None,
    ) -> bool:
        """
        買い注文を執行する。

        Returns:
            True: 注文成功, False: 失敗（資金不足・ポジション上限等）
        """
        if ticker in self.positions:
            logger.debug(f"[{ticker}] 既にポジションあり。スキップ。")
            return False

        if len(self.positions) >= self.max_positions:
            logger.debug(f"[{ticker}] ポジション上限({self.max_positions})到達。スキップ。")
            return False

        # ポジションサイズ計算
        budget = self.initial_capital * self.position_size_pct
        budget = min(budget, self.cash * 0.95)  # 現金の95%まで使用可

        if budget < price:
            logger.debug(f"[{ticker}] 資金不足。スキップ。")
            return False

        shares = int(budget / price / 100) * 100  # 100株単位
        if shares == 0:
            shares = int(budget / price)
            if shares == 0:
                logger.debug(f"[{ticker}] 購入可能株数ゼロ。スキップ。")
                return False

        cost = shares * price
        commission = cost * self.commission_rate
        total_cost = cost + commission

        if total_cost > self.cash:
            # 株数を調整
            shares = int(self.cash / (price * (1 + self.commission_rate)))
            if shares == 0:
                return False
            cost = shares * price
            commission = cost * self.commission_rate
            total_cost = cost + commission

        self.cash -= total_cost
        self.positions[ticker] = Position(
            ticker=ticker,
            entry_date=date,
            entry_price=price,
            shares=shares,
            cost=total_cost,
            highest_price=price,
            entry_reason=reason,
            entry_indicators=indicators or {},
        )

        logger.info(
            f"[{ticker}] 買い: {date.date()} @ ¥{price:,.0f} × {shares}株 "
            f"= ¥{total_cost:,.0f} | 残金: ¥{self.cash:,.0f}"
        )
        return True

    def sell(
        self,
        ticker: str,
        date: pd.Timestamp,
        price: float,
        reason: str = "",
        indicators: Optional[Dict] = None,
    ) -> Optional[Trade]:
        """
        売り注文を執行する。

        Returns:
            Trade: 完結した取引記録, None: ポジションなし
        """
        if ticker not in self.positions:
            logger.debug(f"[{ticker}] ポジションなし。スキップ。")
            return None

        pos = self.positions[ticker]
        proceeds = pos.shares * price
        commission = proceeds * self.commission_rate
        net_proceeds = proceeds - commission

        pnl = net_proceeds - pos.cost
        pnl_pct = pnl / pos.cost * 100

        hold_days = (date - pos.entry_date).days

        trade = Trade(
            ticker=ticker,
            entry_date=str(pos.entry_date.date()),
            exit_date=str(date.date()),
            entry_price=pos.entry_price,
            exit_price=price,
            shares=pos.shares,
            pnl=round(pnl, 0),
            pnl_pct=round(pnl_pct, 2),
            hold_days=hold_days,
            exit_reason=reason,
            entry_indicators=pos.entry_indicators,
            exit_indicators=indicators or {},
        )

        self.cash += net_proceeds
        del self.positions[ticker]
        self.trades.append(trade)

        emoji = "✓" if pnl > 0 else "✗"
        logger.info(
            f"[{ticker}] {emoji}売り: {date.date()} @ ¥{price:,.0f} "
            f"| PnL: ¥{pnl:+,.0f} ({pnl_pct:+.1f}%) "
            f"| 保有{hold_days}日 | {reason}"
        )
        return trade

    # ------------------------------------------------------------------
    # 日次更新
    # ------------------------------------------------------------------

    def update_trailing_stops(self, date: pd.Timestamp, prices: Dict[str, float]) -> None:
        """保有ポジションの最高値を更新する（トレーリングストップ用）。"""
        for ticker, pos in self.positions.items():
            if ticker in prices:
                pos.highest_price = max(pos.highest_price, prices[ticker])

    def record_equity(self, date: pd.Timestamp, prices: Dict[str, float]) -> float:
        """日次の資産評価額を記録し、合計資産を返す。"""
        position_value = sum(
            pos.shares * prices.get(pos.ticker, pos.entry_price)
            for pos in self.positions.values()
        )
        total_equity = self.cash + position_value
        self.equity_curve.append({
            "date": str(date.date()),
            "cash": round(self.cash, 0),
            "position_value": round(position_value, 0),
            "total_equity": round(total_equity, 0),
            "n_positions": len(self.positions),
        })
        return total_equity

    # ------------------------------------------------------------------
    # パフォーマンス計算
    # ------------------------------------------------------------------

    def get_summary(self) -> Dict:
        """バックテスト終了時のパフォーマンスサマリーを返す。"""
        if not self.trades:
            return {"error": "取引記録なし"}

        trades_df = pd.DataFrame([t.to_dict() for t in self.trades])
        equity_df = pd.DataFrame(self.equity_curve)

        # 基本統計
        total_trades = len(trades_df)
        winning = trades_df[trades_df["pnl"] > 0]
        losing = trades_df[trades_df["pnl"] <= 0]
        win_rate = len(winning) / total_trades * 100 if total_trades > 0 else 0

        total_pnl = trades_df["pnl"].sum()
        avg_win = winning["pnl"].mean() if len(winning) > 0 else 0
        avg_loss = losing["pnl"].mean() if len(losing) > 0 else 0
        profit_factor = abs(winning["pnl"].sum() / losing["pnl"].sum()) if len(losing) > 0 and losing["pnl"].sum() != 0 else float("inf")

        # リターン
        final_equity = equity_df["total_equity"].iloc[-1] if len(equity_df) > 0 else self.cash
        total_return_pct = (final_equity - self.initial_capital) / self.initial_capital * 100

        # ドローダウン
        equity_series = equity_df["total_equity"]
        rolling_max = equity_series.cummax()
        drawdown = (equity_series - rolling_max) / rolling_max * 100
        max_drawdown = drawdown.min()

        # シャープレシオ（年次化）
        if len(equity_df) > 1:
            daily_returns = equity_series.pct_change().dropna()
            sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(252)
                      if daily_returns.std() > 0 else 0)
        else:
            sharpe = 0

        return {
            "initial_capital": self.initial_capital,
            "final_equity": round(final_equity, 0),
            "total_return_pct": round(total_return_pct, 2),
            "total_pnl": round(total_pnl, 0),
            "total_trades": total_trades,
            "win_rate": round(win_rate, 1),
            "avg_win": round(avg_win, 0),
            "avg_loss": round(avg_loss, 0),
            "profit_factor": round(profit_factor, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "sharpe_ratio": round(sharpe, 2),
            "avg_hold_days": round(trades_df["hold_days"].mean(), 1),
            "best_trade": {
                "ticker": trades_df.loc[trades_df["pnl"].idxmax(), "ticker"],
                "pnl": round(trades_df["pnl"].max(), 0),
                "pnl_pct": round(trades_df.loc[trades_df["pnl"].idxmax(), "pnl_pct"], 2),
            },
            "worst_trade": {
                "ticker": trades_df.loc[trades_df["pnl"].idxmin(), "ticker"],
                "pnl": round(trades_df["pnl"].min(), 0),
                "pnl_pct": round(trades_df.loc[trades_df["pnl"].idxmin(), "pnl_pct"], 2),
            },
            "trades_by_ticker": (
                trades_df.groupby("ticker")["pnl"].sum()
                .sort_values(ascending=False)
                .round(0)
                .to_dict()
            ),
            "exit_reasons": trades_df["exit_reason"].value_counts().to_dict(),
        }

    def get_trades_df(self) -> pd.DataFrame:
        """取引履歴DataFrameを返す。"""
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([t.to_dict() for t in self.trades])

    def get_equity_df(self) -> pd.DataFrame:
        """資産推移DataFrameを返す。"""
        if not self.equity_curve:
            return pd.DataFrame()
        return pd.DataFrame(self.equity_curve)

    def print_summary(self) -> None:
        """パフォーマンスサマリーをコンソールに表示する。"""
        s = self.get_summary()
        if "error" in s:
            print(s["error"])
            return

        sign = "+" if s["total_return_pct"] >= 0 else ""
        print("\n" + "=" * 60)
        print("  バックテスト結果サマリー")
        print("=" * 60)
        print(f"  初期資金         : ¥{s['initial_capital']:>12,.0f}")
        print(f"  最終資産         : ¥{s['final_equity']:>12,.0f}")
        print(f"  総損益           : ¥{s['total_pnl']:>+12,.0f}")
        print(f"  総リターン       :  {sign}{s['total_return_pct']:>10.2f}%")
        print(f"  シャープレシオ   :  {s['sharpe_ratio']:>10.2f}")
        print(f"  最大ドローダウン :  {s['max_drawdown_pct']:>10.2f}%")
        print("-" * 60)
        print(f"  総取引数         : {s['total_trades']:>12}")
        print(f"  勝率             :  {s['win_rate']:>10.1f}%")
        print(f"  平均勝ち          : ¥{s['avg_win']:>12,.0f}")
        print(f"  平均負け          : ¥{s['avg_loss']:>12,.0f}")
        print(f"  プロフィットFactor:  {s['profit_factor']:>10.2f}")
        print(f"  平均保有日数     : {s['avg_hold_days']:>12.1f}日")
        print("-" * 60)
        print(f"  最良取引: [{s['best_trade']['ticker']}] "
              f"¥{s['best_trade']['pnl']:+,.0f} ({s['best_trade']['pnl_pct']:+.1f}%)")
        print(f"  最悪取引: [{s['worst_trade']['ticker']}] "
              f"¥{s['worst_trade']['pnl']:+,.0f} ({s['worst_trade']['pnl_pct']:+.1f}%)")
        print("=" * 60)
