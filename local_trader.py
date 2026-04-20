"""
Claude 自動トレーダー — スケジューラー / デーモン

平日の東証取引時間に合わせて自動で以下を実行する:

  09:00 JST  朝の処理: シグナル生成 → 売り注文 → 買い注文
  5分ごと     ポジション監視: 損切り・利確・トレーリングストップ
  15:30 JST  引け後の処理: 結果記録 → Claude 改善分析

実行方法:
  python local_trader.py

  または、バックグラウンド起動 (nohup):
  nohup python local_trader.py >> logs/trader.log 2>&1 &

前提条件:
  - kabuステーション® アプリが起動・ログイン済みであること
  - .env に KABU_API_PASSWORD, KABU_TRADE_PASSWORD を設定済みであること
  - pip install -r requirements.txt 完了済みであること
"""
import logging
import os
import sys
import time
from datetime import datetime, date
from pathlib import Path

import schedule
import pytz
from dotenv import load_dotenv

# ─── パス設定 & 環境変数ロード ───────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from trading_system.config import LOGS_DIR, DATA_DIR
from trading_system.kabu_client import KabuClient, KabuAPIError


def _trading_mode() -> str:
    """settings.json から取引モードを読み込む（"paper" or "live"）。"""
    try:
        settings_file = ROOT / "settings.json"
        if settings_file.exists():
            import json as _json
            return _json.loads(settings_file.read_text(encoding="utf-8")).get("trading_mode", "paper")
    except Exception:
        pass
    return "paper"  # デフォルトはペーパー（安全側）

JST = pytz.timezone("Asia/Tokyo")

# ─── ロギング設定 ──────────────────────────────────────────────
_log_file = LOGS_DIR / f"auto_trader_{date.today():%Y%m%d}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_log_file, encoding="utf-8"),
    ],
)
logger = logging.getLogger("local_trader")

# 一時停止フラグファイル
PAUSE_FLAG = DATA_DIR / "trader_paused.flag"


# ─── 一時停止チェック ─────────────────────────────────────────

def is_paused() -> bool:
    return PAUSE_FLAG.exists()


# ─── 日時ユーティリティ ───────────────────────────────────────

def _now_jst() -> datetime:
    return datetime.now(JST)


def is_trading_day() -> bool:
    """今日が取引日（平日かつ祝日でない）かどうかを確認する。"""
    today = _now_jst().date()
    if today.weekday() >= 5:  # 土日
        return False
    try:
        import jpholiday
        if jpholiday.is_holiday(today):
            logger.debug(f"{today} は祝日のためスキップ")
            return False
    except ImportError:
        logger.warning("jpholiday 未インストール。祝日チェックをスキップします。")
    return True


def is_market_open() -> bool:
    """東証が取引中かどうかを確認する（9:00〜15:25）。"""
    if not is_trading_day():
        return False
    h, m = _now_jst().hour, _now_jst().minute
    return (9, 0) <= (h, m) <= (15, 25)


# ─── 朝の発注処理 ─────────────────────────────────────────────

def pre_market_news():
    """08:30: 直近ニュースを取得し Claude で分析して LINE に通知する。"""
    if not is_trading_day():
        return

    logger.info("08:30 朝のニュース分析 開始")
    try:
        from trading_system.news_fetcher import fetch_recent_news
        from trading_system.news_analyzer import analyze_news_impact
        from trading_system.notifier import notify_news_summary

        news = fetch_recent_news(hours=18)
        if not news:
            logger.info("取得ニュースなし。通知をスキップ")
            return

        logger.info(f"ニュース {len(news)}件 取得。Claude で分析中...")
        analysis = analyze_news_impact(news)

        if not analysis:
            logger.warning("分析結果が空のため通知スキップ（APIキー未設定の可能性）")
            return

        notify_news_summary(analysis, news_count=len(news), mode=_trading_mode())
        logger.info("朝のニュース分析通知 完了")
    except Exception as e:
        logger.exception(f"朝のニュース分析エラー: {e}")


def pre_market_notify():
    """08:50: 本日の注目銘柄をLINEに事前通知する。"""
    if not is_trading_day():
        return
    if is_paused():
        return

    logger.info("08:50 注目銘柄 LINE 通知 開始")
    try:
        from trading_system.signal_runner import SignalRunner
        from trading_system.notifier import notify_morning_signal

        runner = SignalRunner(market="JP")
        # スコアリングのみ実行（Claude説明は不要なので軽量に）
        data = runner._fetch_data(runner.universe)
        if not data:
            logger.warning("株価データ取得失敗のため通知スキップ")
            return

        market_bullish = runner._check_market_regime()
        from trading_system.factor_scorer import score_universe
        scored = score_universe(data, market_bullish)

        mode = _trading_mode()
        notify_morning_signal(
            top_scored=scored,
            market_bullish=market_bullish,
            mode=mode,
            top_n=5,
        )
        logger.info(f"注目銘柄通知 完了（上位 {min(5, len(scored))} 銘柄）")
    except Exception as e:
        logger.exception(f"注目銘柄通知エラー: {e}")


def morning_routine():
    """09:00: シグナル生成 → 売り注文 → 買い注文"""
    if not is_trading_day():
        logger.info("本日は非取引日のためスキップ")
        return
    if is_paused():
        logger.info("⏸ 一時停止中のため朝の発注をスキップ")
        return

    mode = _trading_mode()
    logger.info("╔══════════════════════════════════╗")
    logger.info(f"║  朝の自動発注処理 開始 [{mode.upper()}]  ║")
    logger.info("╚══════════════════════════════════╝")

    try:
        from trading_system.signal_runner import SignalRunner

        for market in ["JP"]:
            logger.info(f"[{market}] シグナル生成中...")
            runner = SignalRunner(market=market)
            result = runner.run()

            if result.get("error"):
                logger.error(f"[{market}] シグナル生成失敗: {result['error']}")
                continue

            regime = "強気" if result.get("market_bullish") else "弱気"
            logger.info(f"[{market}] 市場レジーム: {regime}")

            if mode == "paper":
                _morning_paper(market, result)
            else:
                _morning_live(market, result)

    except Exception as e:
        logger.exception(f"朝の処理で予期しないエラー: {e}")
        try:
            from trading_system.notifier import notify_error
            notify_error("朝の自動発注処理", str(e))
        except Exception:
            pass

    logger.info("朝の自動発注処理 完了")


def _morning_paper(market: str, result: dict):
    """ペーパーモード: シグナルに基づいて仮想売買を実行する。"""
    import json as _json
    from trading_system.paper_trader import PaperTrader
    from trading_system.factor_scorer import score_universe

    settings_file = ROOT / "settings.json"
    capital = 1_000_000
    if settings_file.exists():
        try:
            capital = float(_json.loads(settings_file.read_text("utf-8")).get("paper_initial_capital", 1_000_000))
        except Exception:
            pass

    pt = PaperTrader(initial_capital=capital)
    price_data = result.get("_price_data", {})  # signal_runner が設定した価格データ

    # ── 売り: ペーパーポジションを price_data で値洗いして損切り・利確判定 ──
    if pt.positions and price_data:
        stopped = pt.check_stops(price_data)
        for rec in stopped:
            sign = "✅" if rec["pnl"] >= 0 else "🔴"
            logger.info(f"[ペーパー] {sign} 自動売却: {rec['ticker']}  {rec['pnl_pct']:+.1f}%  {rec['reason']}")

    # ── 買い: buy_signals のティッカーを price_data から価格取得して購入 ──
    buy_signals = result.get("buy_signals", [])
    bought_count = 0
    for sig in buy_signals:
        ticker = sig["ticker"]
        # price_data（DataFrame形式）から終値を取得
        price = None
        if price_data.get(ticker) is not None:
            df = price_data[ticker]
            if not df.empty:
                price = float(df["close"].iloc[-1])
        if not price:
            logger.warning(f"[ペーパー] {ticker} の価格が取得できず購入スキップ")
            continue
        pos = pt.buy(ticker, price, score=sig.get("score", 0), market=market)
        if pos:
            bought_count += 1
            logger.info(f"[ペーパー] 📈 買い: {ticker} @ ¥{price:,.0f} (スコア{sig.get('score',0)})")

    if not buy_signals:
        logger.info(f"[ペーパー][{market}] 買いシグナルなし（本日は購入見送り）")
    else:
        logger.info(f"[ペーパー][{market}] 買いシグナル{len(buy_signals)}件 → 購入{bought_count}件")


def _morning_live(market: str, result: dict):
    """ライブモード: kabu APIで実際に発注する。"""
    from trading_system.order_executor import OrderExecutor
    executor = OrderExecutor()

    sell_signals = result.get("sell_signals", [])
    if sell_signals:
        logger.info(f"[{market}] 売りシグナル {len(sell_signals)}件")
        for r in executor.execute_sell_signals(sell_signals):
            if r["status"] == "ordered":
                logger.info(f"  ✓ 売り: {r['ticker']}  P&L {r.get('pnl_pct', 0):+.1f}%")
            else:
                logger.warning(f"  ✗ 売りエラー: {r['ticker']} — {r.get('error')}")

    buy_signals = result.get("buy_signals", [])
    if buy_signals:
        logger.info(f"[{market}] 買いシグナル {len(buy_signals)}件")
        for r in executor.execute_buy_signals(buy_signals):
            if r["status"] == "ordered":
                logger.info(f"  ✓ 買い: {r['ticker']}  {r['qty']}株  ¥{r['invested']:,}")
            elif r["status"] == "skipped":
                logger.info(f"  - スキップ: {r['ticker']} — {r.get('error')}")
            else:
                logger.warning(f"  ✗ 買いエラー: {r['ticker']} — {r.get('error')}")

    if not sell_signals and not buy_signals:
        logger.info(f"[{market}] シグナルなし（本日は取引なし）")


# ─── ポジション監視 ───────────────────────────────────────────

def monitor_routine():
    """5分ごと: ステータスをログに記録し、取引時間中は損切り・利確・トレーリングストップを確認する。"""
    now_str = _now_jst().strftime("%H:%M")
    mode = _trading_mode()
    mode_label = "ペーパー" if mode == "paper" else "ライブ"

    if not is_market_open():
        if is_trading_day():
            logger.info(f"[監視 {now_str}] 取引時間外 — 市場: 09:00〜15:25 / モード: {mode_label}")
        else:
            logger.info(f"[監視 {now_str}] 非取引日（土日・祝日）/ モード: {mode_label}")
        return

    if is_paused():
        logger.info(f"[監視 {now_str}] 一時停止中")
        return

    if mode == "paper":
        try:
            from trading_system.paper_trader import PaperTrader
            from trading_system.kabu_client import fetch_current_prices
            import pandas as pd

            pt = PaperTrader()
            pos_count = len(pt.positions)
            if not pt.positions:
                logger.info(f"[監視 {now_str}] ペーパー: 保有ポジションなし（損切り監視スキップ）")
                return

            tickers = list(pt.positions.keys())

            # kabu API（リアルタイム）優先、失敗時はyfinanceにフォールバック
            spot = fetch_current_prices(tickers)
            if not spot:
                logger.warning(f"[監視 {now_str}] 現在値取得失敗（kabu API・yfinanceともに応答なし）")
                return

            data = {t: pd.DataFrame({"close": [p]}) for t, p in spot.items()}
            logger.info(f"[監視 {now_str}] ペーパー: {pos_count}銘柄を監視中...")
            results = pt.check_stops(data)
            if results:
                for r in results:
                    sign = "✅" if r["pnl"] >= 0 else "🔴"
                    logger.info(
                        f"[ペーパー自動売却] {sign} {r['ticker']}  "
                        f"P&L {r.get('pnl_pct', 0):+.1f}%  "
                        f"理由: {r.get('reason', '')}"
                    )
            else:
                tickers_str = ", ".join(tickers)
                logger.info(f"[監視 {now_str}] ペーパー: 損切り・利確なし ({tickers_str})")
        except Exception as e:
            logger.exception(f"ペーパーポジション監視エラー: {e}")
    else:
        try:
            from trading_system.order_executor import OrderExecutor
            executor = OrderExecutor()

            if executor.portfolio.count() == 0:
                logger.info(f"[監視 {now_str}] ライブ: 保有ポジションなし（損切り監視スキップ）")
                return

            logger.info(f"[監視 {now_str}] ライブ: {executor.portfolio.count()}銘柄を監視中...")
            results = executor.check_and_execute_stops()
            for r in results:
                if r["status"] == "ordered":
                    logger.info(
                        f"[自動売却] {r['ticker']}  "
                        f"P&L {r.get('pnl_pct', 0):+.1f}%  "
                        f"理由: {r.get('reason', '')}"
                    )

        except KabuAPIError as e:
            logger.error(f"ポジション監視 API エラー: {e}")
        except Exception as e:
            logger.exception(f"ポジション監視エラー: {e}")


# ─── 引け後の改善分析 ─────────────────────────────────────────

def closing_routine():
    """15:30: 本日の取引結果を記録し、Claude が改善分析を実行する。"""
    if not is_trading_day():
        return
    # 改善分析は一時停止中でも実行する（データを蓄積するため）

    logger.info("╔══════════════════════════════════╗")
    logger.info("║  引け後の改善分析 開始            ║")
    logger.info("╚══════════════════════════════════╝")

    try:
        from trading_system.auto_improver import run_improvement_cycle
        run_improvement_cycle()
    except Exception as e:
        logger.exception(f"改善分析エラー: {e}")

    logger.info("引け後の改善分析 完了")


# ─── スケジュール設定 ─────────────────────────────────────────

def _setup_schedule():
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday"]

    for day in weekdays:
        getattr(schedule.every(), day).at("08:30").do(pre_market_news)
        getattr(schedule.every(), day).at("08:50").do(pre_market_notify)
        getattr(schedule.every(), day).at("09:00").do(morning_routine)
        getattr(schedule.every(), day).at("15:30").do(closing_routine)

    schedule.every(5).minutes.do(monitor_routine)

    logger.info("スケジュール設定完了")
    logger.info("  平日 08:30 JST  → 朝のニュース分析 LINE 通知")
    logger.info("  平日 08:50 JST  → 注目銘柄スコア LINE 通知")
    logger.info("  平日 09:00 JST  → 朝の発注処理（祝日チェック済み）")
    logger.info("  平日 5分ごと     → ポジション監視（取引時間中のみ）")
    logger.info("  平日 15:30 JST  → 引け後の改善分析")


# ─── エントリーポイント ──────────────────────────────────────

def main():
    logger.info("╔══════════════════════════════════════════╗")
    logger.info("║   Claude 自動トレーダー 起動              ║")
    logger.info(f"║   {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S %Z')}              ║")
    logger.info("╚══════════════════════════════════════════╝")

    if is_paused():
        logger.warning("⚠ 一時停止フラグが立っています。取引はスキップされます（改善分析は実行されます）。")

    # kabuステーション® 接続確認（ライブモード時のみ）
    mode = _trading_mode()
    if mode == "paper":
        logger.info(f"取引モード: ペーパー（仮想売買）— kabuステーション® 接続不要")
    else:
        logger.info("kabuステーション® API 接続確認中...")
        try:
            client  = KabuClient()
            wallet  = client.get_wallet_cash()
            balance = wallet.get("StockAccountWallet", 0)
            logger.info(f"接続OK ✓  現物買付余力: ¥{balance:,.0f}")
        except KabuAPIError as e:
            logger.warning("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            logger.warning(f"kabuステーション® に接続できません: {e}")
            logger.warning("取引機能は無効で起動します（動作確認モード）。")
            logger.warning("kabuステーション® 起動後に再起動してください。")
            logger.warning("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        except Exception as e:
            logger.warning(f"接続確認エラー（起動は続行）: {e}")

    _setup_schedule()
    logger.info("スケジューラー稼働中... (Ctrl+C で停止)")

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            logger.info("停止シグナルを受信しました。シャットダウンします。")
            break
        except Exception as e:
            logger.exception(f"スケジューラーエラー（継続）: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
