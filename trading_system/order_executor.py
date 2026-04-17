"""
注文実行エンジン

シグナルを受け取り、kabu API経由で実際の注文を行う。
ポジション監視（損切り・利確・トレーリングストップ）も担当する。
"""
import logging
import time
from typing import Dict, List, Optional, Tuple

from trading_system.kabu_client import KabuClient, KabuAPIError
from trading_system.portfolio_state import PortfolioState
from trading_system.config import (
    TOTAL_CAPITAL, POSITION_SIZE, MAX_POSITIONS,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_STOP_PCT,
    KABU_TRADE_PASSWORD, KABU_EXCHANGE_CODE,
)

logger = logging.getLogger(__name__)

ORDER_INTERVAL_SEC = 1.0  # 連続注文間の待機秒数


# ─── ティッカー変換 ──────────────────────────────────────────

def ticker_to_kabu(ticker: str) -> Tuple[str, int]:
    """yfinance形式 → kabu API形式に変換する。
    "7203.T" → ("7203", 1)  東証
    """
    if ticker.endswith(".T"):
        return ticker[:-2], KABU_EXCHANGE_CODE
    raise ValueError(f"US株は現在未対応です: {ticker}")


# ─── OrderExecutor ───────────────────────────────────────────

class OrderExecutor:
    """買い・売り注文を実行し、ポートフォリオ状態を更新する。"""

    def __init__(self):
        self.client    = KabuClient()
        self.portfolio = PortfolioState()

    # ─── 公開インターフェース ──────────────────────────────

    def execute_buy_signals(self, buy_signals: List[Dict]) -> List[Dict]:
        """買いシグナルリストを受け取り、順番に注文を実行する。"""
        results = []
        for signal in buy_signals:
            ticker = signal.get("ticker", "")

            if not ticker.endswith(".T"):
                logger.info(f"[{ticker}] US株はスキップ（未対応）")
                continue
            if self.portfolio.is_holding(ticker):
                logger.info(f"[{ticker}] 既に保有中のためスキップ")
                continue
            if self.portfolio.count() >= MAX_POSITIONS:
                logger.info(f"最大保有数 ({MAX_POSITIONS}) 到達 — 買いを停止")
                break

            try:
                result = self._buy(signal)
                results.append(result)
                time.sleep(ORDER_INTERVAL_SEC)
            except KabuAPIError as e:
                logger.error(f"[{ticker}] API注文エラー: {e}")
                results.append({"ticker": ticker, "status": "error", "error": str(e)})
            except ValueError as e:
                logger.warning(f"[{ticker}] スキップ: {e}")
                results.append({"ticker": ticker, "status": "skipped", "error": str(e)})

        return results

    def execute_sell_signals(self, sell_signals: List[Dict]) -> List[Dict]:
        """売りシグナルリストを受け取り、順番に注文を実行する。"""
        results = []
        for signal in sell_signals:
            ticker = signal.get("ticker", "")
            try:
                result = self._sell(ticker, signal.get("reason", "シグナル売り"))
                results.append(result)
                time.sleep(ORDER_INTERVAL_SEC)
            except KabuAPIError as e:
                logger.error(f"[{ticker}] API売り注文エラー: {e}")
                results.append({"ticker": ticker, "status": "error", "error": str(e)})
            except ValueError as e:
                logger.warning(f"[{ticker}] 売りスキップ: {e}")

        return results

    def check_and_execute_stops(self) -> List[Dict]:
        """
        全保有銘柄の現在値を確認し、
        損切り・利確・トレーリングストップに該当すれば自動売却する。
        """
        results = []
        for pos in self.portfolio.get_positions():
            ticker = pos["ticker"]
            if not ticker.endswith(".T"):
                continue

            try:
                kabu_symbol, exchange = ticker_to_kabu(ticker)
                board         = self.client.get_board(kabu_symbol, exchange)
                current_price = _extract_price(board)
                if current_price <= 0:
                    continue

                entry_price   = pos["entry_price"]
                highest_price = pos.get("highest_price", entry_price)
                pnl_pct       = (current_price - entry_price) / entry_price * 100

                self.portfolio.update_highest_price(ticker, current_price)

                reason = _check_stop_condition(
                    current_price, entry_price, highest_price, pnl_pct
                )
                if reason:
                    logger.info(f"[{ticker}] 自動売却トリガー: {reason}")
                    result = self._sell(ticker, reason)
                    results.append(result)
                    time.sleep(ORDER_INTERVAL_SEC)

            except Exception as e:
                logger.error(f"[{ticker}] ストップ監視エラー: {e}")

        return results

    # ─── 内部実装 ───────────────────────────────────────────

    def _buy(self, signal: Dict) -> Dict:
        """1銘柄の成行買い注文を実行し、約定を確認してからポートフォリオに記録する。"""
        ticker          = signal["ticker"]
        kabu_symbol, exchange = ticker_to_kabu(ticker)

        board       = self.client.get_board(kabu_symbol, exchange)
        symbol_info = self.client.get_symbol_info(kabu_symbol, exchange)

        board_price = _extract_price(board)
        if board_price <= 0:
            raise ValueError(f"現在値が取得できません (price={board_price})")

        trading_unit = int(symbol_info.get("TradingUnit", 100))
        qty          = _calc_qty(board_price, trading_unit)
        if qty <= 0:
            raise ValueError(
                f"買付余力不足: 1単元コスト ¥{board_price * trading_unit:,.0f}"
            )

        logger.info(
            f"[{ticker}] 成行買い発注 — {qty}株 @ 約¥{board_price:,.0f}"
            f" = 約¥{board_price * qty:,.0f}"
        )

        order    = self.client.send_order(
            symbol=kabu_symbol,
            exchange=exchange,
            side="2",
            qty=qty,
            trade_password=KABU_TRADE_PASSWORD,
            front_order_type=10,  # 成行
        )
        order_id = order.get("OrderId", "")

        # 約定確認（最大20秒ポーリング）
        fill        = self.client.wait_for_fill(order_id)
        entry_price = fill["fill_price"] if fill["filled"] and fill["fill_price"] > 0 else board_price
        actual_qty  = fill["fill_qty"]   if fill["filled"] and fill["fill_qty"]   > 0 else qty

        if not fill["filled"]:
            logger.warning(
                f"[{ticker}] 約定未確認（発注は受付済）。板価格 ¥{board_price:,.0f} で記録します。"
            )

        invested = entry_price * actual_qty
        self.portfolio.add_position(
            ticker=ticker,
            entry_price=entry_price,
            shares=actual_qty,
            market="JP",
            label=signal.get("label", ""),
            invested_amount=int(invested),
        )
        logger.info(
            f"[{ticker}] ポートフォリオ記録: {actual_qty}株 @ ¥{entry_price:,.0f}"
            f" = ¥{invested:,.0f}（{'約定確認済' if fill['filled'] else '約定未確認'}）"
        )

        return {
            "ticker":        ticker,
            "status":        "ordered",
            "side":          "buy",
            "qty":           actual_qty,
            "price":         entry_price,
            "invested":      int(invested),
            "order_id":      order_id,
            "fill_confirmed": fill["filled"],
        }

    def _sell(self, ticker: str, reason: str = "") -> Dict:
        """1銘柄の成行売り注文を実行し、約定価格でトレードを記録する。"""
        pos = self.portfolio.get_position(ticker)
        if not pos:
            raise ValueError(f"{ticker} は保有していません")

        kabu_symbol, exchange = ticker_to_kabu(ticker)
        qty         = int(pos["shares"])
        board       = self.client.get_board(kabu_symbol, exchange)
        board_price = _extract_price(board)

        logger.info(
            f"[{ticker}] 成行売り発注 — {qty}株 @ 約¥{board_price:,.0f}  理由: {reason}"
        )

        order    = self.client.send_order(
            symbol=kabu_symbol,
            exchange=exchange,
            side="1",
            qty=qty,
            trade_password=KABU_TRADE_PASSWORD,
            front_order_type=10,  # 成行
        )
        order_id = order.get("OrderId", "")

        # 約定確認（最大20秒）
        fill       = self.client.wait_for_fill(order_id)
        exit_price = fill["fill_price"] if fill["filled"] and fill["fill_price"] > 0 else board_price

        if not fill["filled"]:
            logger.warning(
                f"[{ticker}] 売り約定未確認。板価格 ¥{board_price:,.0f} で損益を計算します。"
            )

        pnl     = (exit_price - pos["entry_price"]) * qty
        pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100

        logger.info(
            f"[{ticker}] 売却完了: {qty}株 @ ¥{exit_price:,.0f}"
            f"  P&L {pnl_pct:+.1f}%  ({'約定確認済' if fill['filled'] else '約定未確認'})"
        )

        from datetime import date as _date
        self.portfolio.remove_position(ticker)
        _log_real_trade({
            "ticker":      ticker,
            "label":       pos.get("label", ""),
            "entry_price": pos["entry_price"],
            "exit_price":  exit_price,
            "shares":      qty,
            "pnl":         round(pnl),
            "pnl_pct":     round(pnl_pct, 2),
            "entry_date":  pos.get("entry_date", ""),
            "exit_date":   _date.today().isoformat(),
            "exit_reason": reason,
        })

        return {
            "ticker":        ticker,
            "status":        "ordered",
            "side":          "sell",
            "qty":           qty,
            "price":         exit_price,
            "pnl":           round(pnl),
            "pnl_pct":       round(pnl_pct, 2),
            "reason":        reason,
            "order_id":      order_id,
            "fill_confirmed": fill["filled"],
        }


# ─── ユーティリティ関数 ──────────────────────────────────────

def _extract_price(board: Dict) -> float:
    """板情報から現在値を取得する（フォールバック: CalcPrice）。"""
    price = board.get("CurrentPrice") or board.get("CalcPrice") or 0
    return float(price)


def _calc_qty(price: float, trading_unit: int) -> int:
    """
    予算内で購入できる最大株数を計算する。

    - まず POSITION_SIZE (¥140,000) で何単元買えるか試みる
    - 0単元なら TOTAL_CAPITAL の残高全体を上限として 1単元だけ買う
    - それでも0なら資金不足として 0 を返す
    """
    cost_per_unit = price * trading_unit

    # 通常枠で購入できる単元数
    units = int(POSITION_SIZE // cost_per_unit)

    # 1単元コストが POSITION_SIZE を超える場合、全体資金から 1単元だけ許可
    if units == 0 and cost_per_unit <= TOTAL_CAPITAL:
        units = 1

    return units * trading_unit


def _check_stop_condition(
    current_price: float,
    entry_price:   float,
    highest_price: float,
    pnl_pct:       float,
) -> Optional[str]:
    """損切り・利確・トレーリングストップのいずれかに該当すれば理由文字列を返す。"""
    if current_price <= entry_price * (1 - STOP_LOSS_PCT):
        return f"損切りライン到達（{pnl_pct:+.1f}%）"

    if current_price >= entry_price * (1 + TAKE_PROFIT_PCT):
        return f"利確ライン到達（{pnl_pct:+.1f}%）"

    if (highest_price > entry_price * 1.03
            and current_price <= highest_price * (1 - TRAILING_STOP_PCT)):
        dd = (current_price - highest_price) / highest_price * 100
        return f"トレーリングストップ（高値から{dd:.1f}%下落）"

    return None


def _log_real_trade(trade: Dict):
    """実際のトレード結果をauto_improverに記録する。"""
    try:
        from trading_system.auto_improver import log_real_trade
        log_real_trade(trade)
    except Exception as e:
        logger.warning(f"実トレード記録エラー: {e}")
