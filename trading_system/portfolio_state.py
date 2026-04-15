"""
ポートフォリオ状態管理モジュール
実際に保有している銘柄を JSON ファイルで永続管理する。
（証券会社 API 連携前の手動入力対応版）
"""
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from trading_system.config import DATA_DIR

logger = logging.getLogger(__name__)

PORTFOLIO_FILE = DATA_DIR / "portfolio.json"


class PortfolioState:
    """
    現在のポートフォリオ状態を管理するクラス。

    positions 構造:
    {
      "7203.T": {
        "ticker": "7203.T",
        "label": "トヨタ自動車",
        "market": "JP",
        "entry_price": 2500.0,
        "shares": 56.0,
        "entry_date": "2025-01-15",
        "highest_price": 2650.0,
        "invested_amount": 140000
      }
    }
    """

    def __init__(self):
        self.positions: Dict[str, Dict] = {}
        self._load()

    # ─── 永続化 ──────────────────────────────────────────

    def _load(self):
        if PORTFOLIO_FILE.exists():
            try:
                raw = json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))
                self.positions = raw.get("positions", {})
                logger.debug(f"ポートフォリオ読み込み: {len(self.positions)}銘柄")
            except Exception as e:
                logger.warning(f"ポートフォリオ読み込みエラー: {e}")
                self.positions = {}

    def _save(self):
        try:
            payload = {
                "positions": self.positions,
                "updated_at": datetime.now().isoformat(),
            }
            PORTFOLIO_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"ポートフォリオ保存エラー: {e}")

    # ─── ポジション操作 ────────────────────────────────────

    def add_position(
        self,
        ticker: str,
        entry_price: float,
        shares: float,
        entry_date: Optional[str] = None,
        market: str = "JP",
        label: str = "",
        invested_amount: Optional[int] = None,
    ) -> Dict:
        """ポジションを追加する。"""
        from trading_system.factor_scorer import get_ticker_label
        pos = {
            "ticker": ticker,
            "label": label or get_ticker_label(ticker),
            "market": market,
            "entry_price": round(entry_price, 2),
            "shares": round(shares, 4),
            "entry_date": entry_date or datetime.now().strftime("%Y-%m-%d"),
            "highest_price": round(entry_price, 2),
            "invested_amount": invested_amount or int(entry_price * shares),
        }
        self.positions[ticker] = pos
        self._save()
        logger.info(f"ポジション追加: {ticker} @ {entry_price} × {shares}株")
        return pos

    def remove_position(self, ticker: str) -> bool:
        """ポジションを削除する。"""
        if ticker in self.positions:
            del self.positions[ticker]
            self._save()
            logger.info(f"ポジション削除: {ticker}")
            return True
        return False

    def update_highest_price(self, ticker: str, current_price: float):
        """最高値を更新する（トレーリングストップ用）。"""
        if ticker in self.positions:
            if current_price > self.positions[ticker].get("highest_price", 0):
                self.positions[ticker]["highest_price"] = round(current_price, 2)
                self._save()

    # ─── 照会 ─────────────────────────────────────────────

    def get_positions(self) -> List[Dict]:
        return list(self.positions.values())

    def is_holding(self, ticker: str) -> bool:
        return ticker in self.positions

    def count(self) -> int:
        return len(self.positions)

    def get_position(self, ticker: str) -> Optional[Dict]:
        return self.positions.get(ticker)

    def to_dict(self) -> Dict:
        return {
            "positions": list(self.positions.values()),
            "count": self.count(),
            "updated_at": datetime.now().isoformat(),
        }

    def check_sell_signals(
        self,
        data: Dict,
        stop_loss_pct: float = 0.05,
        trailing_stop_pct: float = 0.06,
    ) -> List[Dict]:
        """
        保有ポジションのうち、損切り・トレーリングストップに
        該当するものをリストで返す。
        """
        sell_signals = []
        for pos in self.get_positions():
            ticker = pos["ticker"]
            if ticker not in data:
                continue

            df = data[ticker]
            if len(df) == 0:
                continue

            current_price  = float(df["close"].iloc[-1])
            entry_price    = pos["entry_price"]
            highest_price  = pos.get("highest_price", entry_price)

            self.update_highest_price(ticker, current_price)

            pnl_pct = (current_price - entry_price) / entry_price * 100
            reason  = None

            if current_price <= entry_price * (1 - stop_loss_pct):
                reason = f"損切りライン到達（エントリーから{pnl_pct:.1f}%）"
            elif (highest_price > entry_price * 1.03 and
                  current_price <= highest_price * (1 - trailing_stop_pct)):
                drawdown = (current_price - highest_price) / highest_price * 100
                reason = f"トレーリングストップ（高値から{drawdown:.1f}%下落）"

            if reason:
                sell_signals.append({
                    "ticker": ticker,
                    "label": pos.get("label", ticker),
                    "market": pos.get("market", "JP"),
                    "current_price": current_price,
                    "entry_price": entry_price,
                    "pnl_pct": round(pnl_pct, 2),
                    "reason": reason,
                    "signal": "sell",
                })

        return sell_signals
