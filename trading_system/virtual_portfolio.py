"""
仮保有ポートフォリオ管理モジュール

買いシグナルが出た銘柄を「仮保有」として登録し、
実際に買っていたら何円の損益になっていたかを追跡する。
"""
import json
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

import yfinance as yf

from trading_system.config import DATA_DIR, POSITION_SIZE

logger = logging.getLogger(__name__)

VIRTUAL_FILE = DATA_DIR / "virtual_portfolio.json"


class VirtualPortfolio:
    """
    仮保有ポートフォリオを管理するクラス。

    positions 構造:
    {
      "7203.T": {
        "ticker": "7203.T",
        "label": "トヨタ自動車",
        "market": "JP",
        "entry_price": 2500.0,
        "shares": 56.0,
        "entry_date": "2025-01-15",
        "invested_amount": 140000,
        "signal_score": 85
      }
    }
    """

    def __init__(self):
        self.positions: Dict[str, Dict] = {}
        self._load()

    # ─── 永続化 ──────────────────────────────────────────

    def _load(self):
        if VIRTUAL_FILE.exists():
            try:
                raw = json.loads(VIRTUAL_FILE.read_text(encoding="utf-8"))
                self.positions = raw.get("positions", {})
            except Exception as e:
                logger.warning(f"仮保有読み込みエラー: {e}")
                self.positions = {}

    def _save(self):
        try:
            VIRTUAL_FILE.write_text(
                json.dumps(
                    {"positions": self.positions, "updated_at": datetime.now().isoformat()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"仮保有保存エラー: {e}")

    # ─── ポジション操作 ────────────────────────────────────

    def add_position(
        self,
        ticker: str,
        entry_price: float,
        market: str = "JP",
        label: str = "",
        signal_score: int = 0,
        entry_date: Optional[str] = None,
    ) -> Dict:
        """仮保有ポジションを追加する。"""
        from trading_system.factor_scorer import get_ticker_label

        shares = POSITION_SIZE / entry_price if entry_price > 0 else 0
        pos = {
            "ticker": ticker,
            "label": label or get_ticker_label(ticker),
            "market": market,
            "entry_price": round(entry_price, 4),
            "shares": round(shares, 4),
            "entry_date": entry_date or datetime.now().strftime("%Y-%m-%d"),
            "invested_amount": POSITION_SIZE,
            "signal_score": signal_score,
        }
        self.positions[ticker] = pos
        self._save()
        logger.info(f"仮保有追加: {ticker} @ {entry_price} × {shares:.2f}株")
        return pos

    def remove_position(self, ticker: str) -> bool:
        """仮保有ポジションを削除する。"""
        if ticker in self.positions:
            del self.positions[ticker]
            self._save()
            logger.info(f"仮保有削除: {ticker}")
            return True
        return False

    def is_holding(self, ticker: str) -> bool:
        return ticker in self.positions

    # ─── 損益計算 ──────────────────────────────────────────

    def get_positions_with_pnl(self) -> Dict:
        """現在値を取得して損益付きのポジション一覧を返す。"""
        if not self.positions:
            return {
                "positions": [],
                "total_invested": 0,
                "total_current": 0,
                "total_pnl": 0,
                "total_pnl_pct": 0.0,
                "updated_at": datetime.now().isoformat(),
            }

        tickers = list(self.positions.keys())
        current_prices = self._fetch_prices(tickers)

        result_positions = []
        total_invested = 0
        total_current = 0

        for ticker, pos in self.positions.items():
            entry_price    = pos["entry_price"]
            shares         = pos["shares"]
            invested       = pos.get("invested_amount", POSITION_SIZE)
            total_invested += invested

            current_price = current_prices.get(ticker)
            if current_price is not None:
                current_value = current_price * shares
                pnl_amount    = current_value - invested
                pnl_pct       = (current_price - entry_price) / entry_price * 100
                total_current += current_value
                fetch_error   = None
            else:
                current_value = invested
                pnl_amount    = 0.0
                pnl_pct       = 0.0
                total_current += invested
                fetch_error   = "価格取得失敗"

            try:
                entry_dt  = datetime.strptime(pos["entry_date"], "%Y-%m-%d")
                hold_days = (datetime.now() - entry_dt).days
            except Exception:
                hold_days = 0

            result_positions.append({
                **pos,
                "current_price": round(current_price, 4) if current_price else None,
                "current_value": round(current_value),
                "pnl_amount":    round(pnl_amount),
                "pnl_pct":       round(pnl_pct, 2),
                "hold_days":     hold_days,
                "fetch_error":   fetch_error,
            })

        total_pnl     = total_current - total_invested
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0.0

        return {
            "positions":      result_positions,
            "total_invested": round(total_invested),
            "total_current":  round(total_current),
            "total_pnl":      round(total_pnl),
            "total_pnl_pct":  round(total_pnl_pct, 2),
            "updated_at":     datetime.now().isoformat(),
        }

    def _fetch_prices(self, tickers: List[str]) -> Dict[str, float]:
        """yfinance で現在値を取得する。"""
        prices: Dict[str, float] = {}
        for ticker in tickers:
            try:
                raw = yf.download(ticker, period="5d", progress=False, auto_adjust=True)
                if not raw.empty:
                    close = raw["Close"].squeeze()
                    prices[ticker] = float(close.iloc[-1])
                time.sleep(0.2)
            except Exception as e:
                logger.warning(f"[{ticker}] 現在値取得エラー: {e}")
        return prices
