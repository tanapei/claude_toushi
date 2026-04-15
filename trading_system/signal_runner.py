"""
デイリーシグナルランナー
毎日のシグナルを生成する。URLを開いた際に自動実行される。

実行タイミング:
  日本株: 16:30 JST以降にURLを開いた際
  米国株: 07:00 JST以降にURLを開いた際
"""
import logging
import time
from datetime import datetime
from typing import Dict, List

import yfinance as yf
import pandas as pd

from trading_system.config import (
    JP_UNIVERSE, US_UNIVERSE,
    TOTAL_CAPITAL, POSITION_SIZE, MAX_POSITIONS,
    STOP_LOSS_PCT, TRAILING_STOP_PCT,
)
from trading_system.factor_scorer import score_universe
from trading_system.portfolio_state import PortfolioState

logger = logging.getLogger(__name__)


class SignalRunner:
    """
    デイリーシグナル生成クラス。

    使い方:
        runner = SignalRunner(market="JP")
        result = runner.run()
    """

    def __init__(self, market: str = "JP"):
        self.market    = market.upper()
        self.universe  = JP_UNIVERSE if self.market == "JP" else US_UNIVERSE
        self.portfolio = PortfolioState()

    def run(self) -> Dict:
        """
        シグナルを生成して結果を返す。

        Returns:
            {
              "market", "buy_signals", "sell_signals",
              "market_bullish", "top_scored", "timestamp"
            }
        """
        logger.info(f"[{self.market}] デイリーシグナル生成開始")

        # 1. 価格データ取得
        data = self._fetch_data(self.universe)
        if not data:
            msg = f"[{self.market}] 株価データの取得に失敗しました"
            logger.error(msg)
            return {"error": msg}

        # 2. 市場レジーム確認
        market_bullish = self._check_market_regime()

        # 3. 全銘柄スコアリング
        scored = score_universe(data, market_bullish)

        # 4. 保有ポジションの売りシグナル確認
        sell_signals = self.portfolio.check_sell_signals(
            data,
            stop_loss_pct=STOP_LOSS_PCT,
            trailing_stop_pct=TRAILING_STOP_PCT,
        )

        # 5. 買いシグナル（空きスロット分）
        available_slots = MAX_POSITIONS - self.portfolio.count() + len(sell_signals)
        buy_signals = [
            s for s in scored
            if s["signal"] == "buy"
            and not self.portfolio.is_holding(s["ticker"])
        ][:max(0, available_slots)]

        # 6. Claude で各シグナルを説明
        buy_signals  = self._explain_signals(buy_signals,  "buy")
        sell_signals = self._explain_signals(sell_signals, "sell")

        result = {
            "market": self.market,
            "buy_signals": buy_signals,
            "sell_signals": sell_signals,
            "market_bullish": market_bullish,
            "top_scored": scored[:15],
            "timestamp": datetime.now().isoformat(),
        }

        logger.info(
            f"[{self.market}] 完了: 買い{len(buy_signals)}件 / "
            f"売り{len(sell_signals)}件 / 市場{'強気' if market_bullish else '弱気'}"
        )
        return result

    def _fetch_data(self, universe: List[str], period: str = "200d") -> Dict[str, pd.DataFrame]:
        """全銘柄の直近データを取得する（200日分）。"""
        data: Dict[str, pd.DataFrame] = {}
        failed: List[str] = []

        for ticker in universe:
            try:
                raw = yf.download(ticker, period=period, progress=False, auto_adjust=True)
                if raw.empty or len(raw) < 60:
                    failed.append(ticker)
                    continue

                raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
                df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
                df.columns = ["open", "high", "low", "close", "volume"]
                df.index = pd.to_datetime(df.index)
                df.dropna(subset=["close"], inplace=True)
                df["volume"] = df["volume"].fillna(0)

                if len(df) >= 60:
                    data[ticker] = df
                else:
                    failed.append(ticker)

                time.sleep(0.2)

            except Exception as e:
                logger.warning(f"[{ticker}] データ取得エラー: {e}")
                failed.append(ticker)

        if failed:
            logger.warning(f"データ取得失敗: {failed}")
        logger.info(f"データ取得完了: {len(data)}/{len(universe)} 銘柄")
        return data

    def _check_market_regime(self) -> bool:
        """インデックスが200日EMAより上なら強気（True）。"""
        index = "^N225" if self.market == "JP" else "^IXIC"
        try:
            raw = yf.download(index, period="250d", progress=False, auto_adjust=True)
            if raw.empty:
                return True
            close   = raw["Close"].squeeze()
            ema200  = close.ewm(span=200, adjust=False).mean()
            bullish = float(close.iloc[-1]) > float(ema200.iloc[-1])
            logger.info(f"市場レジーム ({index}): {'強気' if bullish else '弱気'}")
            return bullish
        except Exception as e:
            logger.warning(f"市場レジーム取得エラー: {e}")
            return True

    def _explain_signals(self, signals: List[Dict], signal_type: str) -> List[Dict]:
        """各シグナルに Claude の解説を付与する。"""
        from trading_system.analyzer import explain_trade_signal
        explained = []
        for s in signals:
            try:
                s["explanation"] = explain_trade_signal(s, signal_type, self.market)
            except Exception as e:
                logger.warning(f"[{s.get('ticker')}] 解説生成エラー: {e}")
                s["explanation"] = self._fallback_explanation(s, signal_type)
            explained.append(s)
        return explained

    def _fallback_explanation(self, signal: Dict, signal_type: str) -> str:
        if signal_type == "sell":
            return signal.get("reason", "売却条件に達しました")
        reasons = signal.get("reasons", [])
        score   = signal.get("score", 0)
        m6      = signal.get("momentum_6m_pct", 0)
        return f"スコア{score}点。6ヶ月リターン{m6:+.1f}%。{'; '.join(reasons[:3])}"
