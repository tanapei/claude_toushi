"""
ペーパートレード（仮想売買）モジュール

シグナルに自動反応して仮想資金で売買を記録する。
損切り・利確・トレーリングストップも自動適用。
"""
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from trading_system.config import (
    DATA_DIR, MAX_POSITIONS,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_STOP_PCT,
)
from trading_system.factor_scorer import get_ticker_label

logger = logging.getLogger(__name__)

PAPER_FILE = DATA_DIR / "paper_portfolio.json"
DEFAULT_CAPITAL = 1_000_000


class PaperTrader:
    """仮想資金で自動売買を行うクラス。"""

    def __init__(self, initial_capital: float = DEFAULT_CAPITAL):
        self.initial_capital = initial_capital
        self.cash: float = initial_capital
        self.positions: Dict[str, Dict] = {}
        self.trade_history: List[Dict] = []
        self._load()

    # ─── 永続化 ──────────────────────────────────────────

    def _load(self):
        if PAPER_FILE.exists():
            try:
                raw = json.loads(PAPER_FILE.read_text(encoding="utf-8"))
                self.cash           = raw.get("cash", self.initial_capital)
                self.initial_capital= raw.get("initial_capital", self.initial_capital)
                self.positions      = raw.get("positions", {})
                self.trade_history  = raw.get("trade_history", [])
            except Exception as e:
                logger.warning(f"ペーパーポートフォリオ読み込みエラー: {e}")

    def _save(self):
        try:
            PAPER_FILE.write_text(
                json.dumps({
                    "initial_capital": self.initial_capital,
                    "cash":            self.cash,
                    "positions":       self.positions,
                    "trade_history":   self.trade_history,
                    "updated_at":      datetime.now().isoformat(),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"ペーパーポートフォリオ保存エラー: {e}")

    # ─── 売買実行 ─────────────────────────────────────────

    def buy(self, ticker: str, price: float, score: int = 0, market: str = "JP") -> Optional[Dict]:
        """買い実行。資金の1/MAX_POSITIONS を投資。株数は整数に切り捨て（実取引に近い形）。"""
        if ticker in self.positions:
            return None
        if len(self.positions) >= MAX_POSITIONS:
            logger.info(f"[ペーパー] 最大保有数到達 ({MAX_POSITIONS}件)、{ticker} 見送り")
            return None

        budget = min(self.initial_capital / MAX_POSITIONS, self.cash)
        if budget < price:
            logger.info(f"[ペーパー] 資金不足: {ticker}")
            return None

        shares = int(budget / price)  # 整数株に切り捨て
        if shares == 0:
            logger.info(f"[ペーパー] 株価が高すぎて1株購入できません: {ticker} ¥{price:,.0f}")
            return None

        invest = shares * price  # 実際の投資額（端数なし）
        self.cash -= invest
        pos = {
            "ticker":        ticker,
            "label":         get_ticker_label(ticker),
            "market":        market,
            "entry_price":   round(price, 4),
            "shares":        shares,
            "invested":      round(invest),
            "highest_price": round(price, 4),
            "entry_date":    datetime.now().strftime("%Y-%m-%d %H:%M"),
            "score":         score,
        }
        self.positions[ticker] = pos
        self._save()
        logger.info(f"[ペーパー] 買い: {ticker} @ {price:,.0f}円 × {shares}株 = ¥{invest:,.0f}")
        return pos

    def sell(self, ticker: str, price: float, reason: str) -> Optional[Dict]:
        """売り実行。"""
        pos = self.positions.pop(ticker, None)
        if pos is None:
            return None

        proceeds = price * pos["shares"]
        pnl      = proceeds - pos["invested"]
        pnl_pct  = pnl / pos["invested"] * 100
        self.cash += proceeds

        record = {
            "ticker":      ticker,
            "label":       pos.get("label", ticker),
            "entry_price": pos["entry_price"],
            "exit_price":  round(price, 4),
            "shares":      pos["shares"],
            "invested":    pos["invested"],
            "proceeds":    round(proceeds),
            "pnl":         round(pnl),
            "pnl_pct":     round(pnl_pct, 2),
            "entry_date":  pos["entry_date"],
            "exit_date":   datetime.now().strftime("%Y-%m-%d %H:%M"),
            "reason":      reason,
        }
        self.trade_history.append(record)
        self._save()
        logger.info(f"[ペーパー] 売り: {ticker} @ {price:.2f}  損益 {pnl_pct:+.2f}%  理由: {reason}")
        return record

    # ─── 自動ストップ確認 ─────────────────────────────────

    def check_stops(self, data: Dict) -> List[Dict]:
        """保有中ポジションの損切り・利確・トレーリングを確認して自動売却。"""
        executed = []
        for ticker, pos in list(self.positions.items()):
            if ticker not in data:
                continue
            df = data[ticker]
            if df.empty:
                continue

            current = float(df["close"].iloc[-1])
            entry   = pos["entry_price"]
            highest = pos.get("highest_price", entry)

            # 最高値更新
            if current > highest:
                self.positions[ticker]["highest_price"] = round(current, 4)
                highest = current

            pnl_pct = (current - entry) / entry * 100
            reason  = None

            if current <= entry * (1 - STOP_LOSS_PCT):
                reason = f"損切りライン到達（{pnl_pct:+.1f}%）"
            elif current >= entry * (1 + TAKE_PROFIT_PCT):
                reason = f"利確ライン到達（{pnl_pct:+.1f}%）"
            elif (highest > entry * 1.03 and
                  current <= highest * (1 - TRAILING_STOP_PCT)):
                dd = (current - highest) / highest * 100
                reason = f"トレーリングストップ（高値から{dd:.1f}%下落）"

            if reason:
                record = self.sell(ticker, current, reason)
                if record:
                    executed.append(record)

        if executed:
            self._save()
        return executed

    # ─── 状態取得 ─────────────────────────────────────────

    def get_status(self, data: Optional[Dict] = None) -> Dict:
        """現在のポートフォリオ状態を返す。"""
        positions_with_pnl = []
        total_position_value = 0

        for ticker, pos in self.positions.items():
            current = None
            if data and ticker in data:
                df = data[ticker]
                if not df.empty:
                    current = float(df["close"].iloc[-1])

            if current is not None:
                value   = current * pos["shares"]
                pnl     = value - pos["invested"]
                pnl_pct = pnl / pos["invested"] * 100
                total_position_value += value
            else:
                value   = pos["invested"]
                pnl     = 0.0
                pnl_pct = 0.0
                total_position_value += value

            positions_with_pnl.append({
                **pos,
                "current_price": round(current, 4) if current else None,
                "current_value": round(value),
                "pnl":           round(pnl),
                "pnl_pct":       round(pnl_pct, 2),
            })

        total_equity = self.cash + total_position_value
        total_return = (total_equity - self.initial_capital) / self.initial_capital * 100

        closed_trades = self.trade_history
        wins  = [t for t in closed_trades if t["pnl"] > 0]
        losses= [t for t in closed_trades if t["pnl"] <= 0]

        return {
            "initial_capital":  round(self.initial_capital),
            "cash":             round(self.cash),
            "position_value":   round(total_position_value),
            "total_equity":     round(total_equity),
            "total_return_pct": round(total_return, 2),
            "positions":        positions_with_pnl,
            "trade_count":      len(closed_trades),
            "win_rate":         round(len(wins) / len(closed_trades) * 100, 1) if closed_trades else 0,
            "total_pnl":        round(sum(t["pnl"] for t in closed_trades)),
            "recent_trades":    list(reversed(closed_trades[-20:])),
        }

    def reset(self, initial_capital: float = DEFAULT_CAPITAL):
        """ペーパーポートフォリオをリセットする。"""
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions = {}
        self.trade_history = []
        self._save()
        logger.info(f"[ペーパー] リセット完了 (初期資金 ¥{initial_capital:,.0f})")
