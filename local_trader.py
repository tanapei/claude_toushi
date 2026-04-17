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

def morning_routine():
    """09:00: シグナル生成 → 売り注文 → 買い注文"""
    if not is_trading_day():
        logger.info("本日は非取引日のためスキップ")
        return
    if is_paused():
        logger.info("⏸ 一時停止中のため朝の発注をスキップ")
        return

    logger.info("╔══════════════════════════════════╗")
    logger.info("║  朝の自動発注処理 開始            ║")
    logger.info("╚══════════════════════════════════╝")

    try:
        from trading_system.signal_runner import SignalRunner
        from trading_system.order_executor import OrderExecutor

        executor = OrderExecutor()

        for market in ["JP"]:   # US株は今後対応予定
            logger.info(f"[{market}] シグナル生成中...")
            runner = SignalRunner(market=market)
            result = runner.run()

            if result.get("error"):
                logger.error(f"[{market}] シグナル生成失敗: {result['error']}")
                continue

            regime = "強気" if result.get("market_bullish") else "弱気"
            logger.info(f"[{market}] 市場レジーム: {regime}")

            # ① 売りシグナルを先に処理（ポジション解放）
            sell_signals = result.get("sell_signals", [])
            if sell_signals:
                logger.info(f"[{market}] 売りシグナル {len(sell_signals)}件")
                for r in executor.execute_sell_signals(sell_signals):
                    if r["status"] == "ordered":
                        logger.info(f"  ✓ 売り: {r['ticker']}  P&L {r.get('pnl_pct', 0):+.1f}%")
                    else:
                        logger.warning(f"  ✗ 売りエラー: {r['ticker']} — {r.get('error')}")

            # ② 買いシグナルを処理
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

    except Exception as e:
        logger.exception(f"朝の処理で予期しないエラー: {e}")

    logger.info("朝の自動発注処理 完了")


# ─── ポジション監視 ───────────────────────────────────────────

def monitor_routine():
    """5分ごと（取引時間中のみ）: 損切り・利確・トレーリングストップを確認して自動売却する。"""
    if not is_market_open():
        return
    if is_paused():
        return

    try:
        from trading_system.order_executor import OrderExecutor
        executor = OrderExecutor()

        if executor.portfolio.count() == 0:
            return  # 保有なし

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
        getattr(schedule.every(), day).at("09:00").do(morning_routine)
        getattr(schedule.every(), day).at("15:30").do(closing_routine)

    schedule.every(5).minutes.do(monitor_routine)

    logger.info("スケジュール設定完了")
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

    # kabuステーション® 接続確認
    logger.info("kabuステーション® API 接続確認中...")
    try:
        client  = KabuClient()
        wallet  = client.get_wallet_cash()
        balance = wallet.get("StockAccountWallet", 0)
        logger.info(f"接続OK ✓  現物買付余力: ¥{balance:,.0f}")
    except KabuAPIError as e:
        logger.error("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.error(f"kabuステーション® に接続できません: {e}")
        logger.error("以下を確認してください:")
        logger.error("  1. kabuステーション® が起動・ログイン済みであること")
        logger.error("  2. kabuステーション® の設定 → API → APIパスワードが設定されていること")
        logger.error("  3. .env の KABU_API_PASSWORD が正しいこと")
        logger.error("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        sys.exit(1)

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
