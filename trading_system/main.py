"""
メインエントリーポイント
株式取引シミュレーションシステムの起動・制御を行います。

使い方:
  # 単発バックテスト
  python -m trading_system.main --mode single

  # イテレーティブバックテスト（Claude AIが毎回改善）
  python -m trading_system.main --mode iterative --iterations 3

  # 過去の結果を表示
  python -m trading_system.main --mode report

  # 特定セッションの詳細表示
  python -m trading_system.main --mode report --run-id run_20240101_120000_abc123
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from trading_system.config import (
    BACKTEST_START,
    BACKTEST_END,
    STRATEGY_PARAMS,
    RESULTS_DIR,
)
from trading_system.backtest import Backtester, run_iterative_backtest
from trading_system.trade_logger import TradeLogger

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(__file__).parent.parent / "logs" / "trading.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def cmd_single(args) -> None:
    """単発バックテストを実行する。"""
    print("\n" + "=" * 60)
    print("  Claude 株式取引シミュレーション")
    print("  モード: 単発バックテスト")
    print("=" * 60)
    print(f"  期間: {args.start} 〜 {args.end}")
    print(f"  戦略: デュアルモメンタム + EMAクロス + RSIフィルタ")
    print("=" * 60 + "\n")

    # APIキー確認
    _check_api_key()

    backtester = Backtester(
        start_date=args.start,
        end_date=args.end,
    )
    result = backtester.run(save_results=True, analyze=True)

    # JSON出力
    if args.output:
        output_path = Path(args.output)
        output_path.write_text(
            json.dumps(
                {
                    "run_id": result["run_id"],
                    "summary": result["summary"],
                    "analysis": result.get("analysis"),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        print(f"\n結果をJSON出力: {output_path}")


def cmd_iterative(args) -> None:
    """イテレーティブバックテストを実行する。"""
    print("\n" + "=" * 60)
    print("  Claude 株式取引シミュレーション")
    print(f"  モード: イテレーティブバックテスト × {args.iterations}回")
    print("=" * 60)
    print(f"  期間: {args.start} 〜 {args.end}")
    print(f"  Claudeが毎回分析・パラメータ改善を行います")
    print("=" * 60 + "\n")

    _check_api_key()

    results = run_iterative_backtest(
        n_iterations=args.iterations,
        start_date=args.start,
        end_date=args.end,
    )

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(
            json.dumps(
                [
                    {
                        "run_id": r["run_id"],
                        "summary": r["summary"],
                        "analysis": r.get("analysis"),
                    }
                    for r in results
                ],
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        print(f"\n結果をJSON出力: {output_path}")


def cmd_report(args) -> None:
    """過去の結果を表示する。"""
    trade_logger = TradeLogger()

    if args.run_id:
        # 特定セッションの詳細
        session = trade_logger.get_session(args.run_id)
        if not session:
            print(f"セッション '{args.run_id}' が見つかりません")
            return

        print("\n" + "=" * 60)
        print(f"  セッション詳細: {args.run_id}")
        print("=" * 60)
        summary = session.get("summary", {})
        _print_summary_table(summary)

        trades = trade_logger.get_trades(args.run_id)
        if not trades.empty:
            print(f"\n取引履歴 ({len(trades)}件):")
            print(trades[["ticker", "entry_date", "exit_date", "pnl", "pnl_pct", "exit_reason"]].to_string())

        analysis = trade_logger.get_latest_analysis()
        if analysis:
            print("\n最新分析:")
            print("[良かった点]")
            for p in analysis.get("good_points", []):
                print(f"  - {p}")
            print("[悪かった点]")
            for p in analysis.get("bad_points", []):
                print(f"  - {p}")
    else:
        # 全セッション一覧
        sessions = trade_logger.get_all_sessions()
        if sessions.empty:
            print("バックテスト実績がありません。--mode single で実行してください。")
            return

        print("\n" + "=" * 60)
        print("  バックテスト実績一覧")
        print("=" * 60)
        display_cols = ["run_id", "start_date", "end_date", "total_return_pct",
                        "win_rate", "sharpe_ratio", "max_drawdown_pct", "total_trades"]
        available_cols = [c for c in display_cols if c in sessions.columns]
        print(sessions[available_cols].to_string(index=False))

        # 累積サマリー
        cum = trade_logger.get_cumulative_performance()
        if cum:
            print(f"\n累積パフォーマンス ({cum['total_sessions']}セッション):")
            print(f"  平均リターン: {cum['avg_return_pct']:+.2f}%")
            print(f"  最高リターン: {cum['best_return_pct']:+.2f}%")
            print(f"  最悪リターン: {cum['worst_return_pct']:+.2f}%")
            print(f"  平均勝率: {cum['avg_win_rate']:.1f}%")
            print(f"  平均シャープ比: {cum['avg_sharpe']:.2f}")


def _print_summary_table(summary: dict) -> None:
    """サマリーテーブルを表示する。"""
    print(f"  初期資金: ¥{summary.get('initial_capital', 0):,.0f}")
    print(f"  最終資産: ¥{summary.get('final_equity', 0):,.0f}")
    print(f"  総リターン: {summary.get('total_return_pct', 0):+.2f}%")
    print(f"  シャープ比: {summary.get('sharpe_ratio', 0):.2f}")
    print(f"  最大DD: {summary.get('max_drawdown_pct', 0):.2f}%")
    print(f"  勝率: {summary.get('win_rate', 0):.1f}%")


def _check_api_key() -> None:
    """APIキーの設定状況を確認・警告する。"""
    import os
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("\n[警告] ANTHROPIC_API_KEY が未設定です。")
        print("       Claude AI分析は無効（ルールベース分析を使用）。")
        print("       設定方法: export ANTHROPIC_API_KEY=sk-ant-...\n")


def main():
    parser = argparse.ArgumentParser(
        description="Claude AI 株式取引シミュレーションシステム",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python -m trading_system.main --mode single
  python -m trading_system.main --mode single --start 2023-01-01 --end 2023-12-31
  python -m trading_system.main --mode iterative --iterations 3
  python -m trading_system.main --mode report
  python -m trading_system.main --mode report --run-id run_20240101_120000_abc123
        """,
    )

    parser.add_argument(
        "--mode",
        choices=["single", "iterative", "report"],
        default="single",
        help="実行モード (default: single)",
    )
    parser.add_argument("--start", default=BACKTEST_START, help=f"開始日 (default: {BACKTEST_START})")
    parser.add_argument("--end", default=BACKTEST_END, help=f"終了日 (default: {BACKTEST_END})")
    parser.add_argument("--iterations", type=int, default=3, help="イテレーティブモードの反復回数 (default: 3)")
    parser.add_argument("--run-id", help="表示するセッションのrun_id（reportモード用）")
    parser.add_argument("--output", help="JSON出力ファイルパス（任意）")

    args = parser.parse_args()

    try:
        if args.mode == "single":
            cmd_single(args)
        elif args.mode == "iterative":
            cmd_iterative(args)
        elif args.mode == "report":
            cmd_report(args)
    except KeyboardInterrupt:
        print("\n\n中断されました。")
        sys.exit(0)
    except Exception as e:
        logger.error(f"エラーが発生しました: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
