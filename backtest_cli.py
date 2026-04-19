"""
バックテスト CLI — 取引ルール最適化用

使い方:
  # デフォルトパラメータで実行
  python backtest_cli.py

  # パラメータを指定して実行
  python backtest_cli.py --start 2021-01-01 --end 2024-12-31 \
      --threshold 70 --stop 5 --profit 15 --trail 6

  # 複数パラメータセットを一括比較
  python backtest_cli.py --sweep

出力は results/ フォルダに JSON で保存されます。
"""
import sys, os, json, argparse
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from trading_system.backtest import Backtester
from trading_system.config import BACKTEST_START, BACKTEST_END


# ─────────────────────────────────────────────────────
# 結果表示ユーティリティ
# ─────────────────────────────────────────────────────

def _bar(val, max_val=100, width=20, char="█"):
    n = int(abs(val) / max_val * width) if max_val else 0
    return char * min(n, width)

def print_report(params, summary, trades_list):
    """コンソールに見やすいレポートを出力する。"""
    sep = "═" * 60

    print(f"\n{sep}")
    print("  バックテスト結果レポート")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(sep)

    # パラメータ
    print(f"\n【パラメータ】")
    print(f"  期間       : {params['start_date']} 〜 {params['end_date']}")
    print(f"  買いスコア閾値 : {params['buy_threshold']} 点")
    print(f"  損切りライン  : -{params['stop_loss_pct']}%")
    print(f"  利確ライン   : +{params['take_profit_pct']}%")
    print(f"  トレーリング  : -{params['trailing_stop_pct']}%")

    if "error" in summary:
        print(f"\n⚠ 取引なし: {summary['error']}")
        return

    # 主要KPI
    ret = summary.get("total_return_pct", 0)
    wr  = summary.get("win_rate", 0)
    pf  = summary.get("profit_factor", 0)
    sh  = summary.get("sharpe_ratio", 0)
    dd  = summary.get("max_drawdown_pct", 0)
    n   = summary.get("total_trades", 0)
    hold = summary.get("avg_hold_days", 0)

    print(f"\n【パフォーマンス概要】")
    sign = "+" if ret >= 0 else ""
    print(f"  最終リターン  : {sign}{ret:.2f}%  {_bar(abs(ret), 50)}")
    print(f"  勝率        : {wr:.1f}%  {_bar(wr)}")
    print(f"  プロフィットF : {pf:.2f}")
    print(f"  シャープ比   : {sh:.2f}")
    print(f"  最大DD      : {dd:.2f}%")
    print(f"  取引数      : {n} 件  平均保有 {hold:.1f} 日")
    print(f"  初期資金    : ¥{summary.get('initial_capital',1_000_000):,.0f}")
    print(f"  最終資産    : ¥{summary.get('final_equity',0):,.0f}")
    total_pnl = summary.get("total_pnl", 0)
    sign2 = "+" if total_pnl >= 0 else ""
    print(f"  累計損益    : {sign2}¥{total_pnl:,.0f}")

    # 売却理由の内訳
    reasons = summary.get("exit_reasons", {})
    if reasons:
        print(f"\n【売却理由の内訳】")
        total_r = sum(reasons.values())
        for reason, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
            pct = cnt / total_r * 100 if total_r else 0
            bar = _bar(pct, width=15)
            print(f"  {reason[:25]:<25} : {cnt:3d}件 ({pct:5.1f}%) {bar}")

    # ベスト/ワーストトレード
    best  = summary.get("best_trade",  {})
    worst = summary.get("worst_trade", {})
    print(f"\n【最大益トレード】 {best.get('ticker','—')}  {best.get('pnl_pct',0):+.2f}%  ¥{best.get('pnl',0):,.0f}")
    print(f"【最大損トレード】 {worst.get('ticker','—')}  {worst.get('pnl_pct',0):+.2f}%  ¥{worst.get('pnl',0):,.0f}")

    # 銘柄別損益上位5 / 下位5
    by_ticker = summary.get("trades_by_ticker", {})
    if by_ticker:
        sorted_t = sorted(by_ticker.items(), key=lambda x: x[1], reverse=True)
        print(f"\n【銘柄別損益 TOP5】")
        for t, pnl in sorted_t[:5]:
            sign3 = "+" if pnl >= 0 else ""
            print(f"  {t}  {sign3}¥{pnl:,.0f}")
        print(f"【銘柄別損益 BOTTOM5】")
        for t, pnl in sorted_t[-5:]:
            sign3 = "+" if pnl >= 0 else ""
            print(f"  {t}  {sign3}¥{pnl:,.0f}")

    # 時系列: 月別損益
    if trades_list:
        import pandas as pd
        df = pd.DataFrame(trades_list)
        df["exit_month"] = pd.to_datetime(df["exit_date"]).dt.to_period("M")
        monthly = df.groupby("exit_month")["pnl"].sum()
        print(f"\n【月別損益】")
        for month, pnl in monthly.items():
            sign3 = "+" if pnl >= 0 else ""
            bar = _bar(abs(pnl)/5000, max_val=10, width=15,
                       char="▓" if pnl >= 0 else "░")
            print(f"  {month}  {sign3}¥{pnl:>10,.0f}  {bar}")

    print(f"\n{sep}\n")


def run_single(params: dict, save: bool = True) -> dict:
    """1回バックテストを実行してサマリーを返す。"""
    factor_params = {
        "buy_threshold":     params["buy_threshold"],
        "stop_loss_pct":     params["stop_loss_pct"],
        "take_profit_pct":   params["take_profit_pct"],
        "trailing_stop_pct": params["trailing_stop_pct"],
    }
    bt = Backtester(
        start_date=params["start_date"],
        end_date=params["end_date"],
        strategy_mode="factor",
        factor_params=factor_params,
    )
    result = bt.run(save_results=save, analyze=False)
    summary = result.get("summary", {})
    trades_df = result.get("trades")
    trades_list = trades_df.to_dict(orient="records") if trades_df is not None and not trades_df.empty else []
    return summary, trades_list


def run_sweep():
    """主要パラメータのグリッドサーチを実行して比較表を出力する。"""
    import itertools

    grid = {
        "buy_threshold":     [65, 70, 75],
        "stop_loss_pct":     [4, 5, 7],
        "take_profit_pct":   [12, 15, 20],
        "trailing_stop_pct": [5, 6, 8],
    }

    base = {
        "start_date": "2021-01-01",
        "end_date":   "2024-12-31",
    }

    # デフォルト値を基準に1パラメータずつ変えて比較（全組み合わせは多すぎるため）
    default = {"buy_threshold": 70, "stop_loss_pct": 5, "take_profit_pct": 15, "trailing_stop_pct": 6}
    combos = [default.copy()]
    for key, vals in grid.items():
        for v in vals:
            if v != default[key]:
                combo = default.copy()
                combo[key] = v
                combos.append(combo)

    print(f"\n{'='*80}")
    print(f"  パラメータスイープ ({len(combos)} パターン)")
    print(f"{'='*80}")
    print(f"{'閾値':>4} {'損切':>5} {'利確':>5} {'TR':>5} | {'リターン':>8} {'勝率':>6} {'PF':>5} {'シャープ':>7} {'最大DD':>7} {'取引数':>5}")
    print("-"*80)

    results = []
    for i, combo in enumerate(combos):
        params = {**base, **combo}
        print(f"  [{i+1}/{len(combos)}] 実行中: {combo}", end="\r", flush=True)
        try:
            summary, _ = run_single(params, save=False)
            if "error" in summary:
                continue
            ret = summary.get("total_return_pct", 0)
            wr  = summary.get("win_rate", 0)
            pf  = summary.get("profit_factor", 0)
            sh  = summary.get("sharpe_ratio", 0)
            dd  = summary.get("max_drawdown_pct", 0)
            n   = summary.get("total_trades", 0)
            mark = "◀ default" if combo == default else ""
            print(f"{combo['buy_threshold']:>4} {combo['stop_loss_pct']:>5} {combo['take_profit_pct']:>5} "
                  f"{combo['trailing_stop_pct']:>5} | "
                  f"{ret:>+8.2f}% {wr:>5.1f}% {pf:>5.2f} {sh:>7.2f} {dd:>6.2f}% {n:>5} {mark}")
            results.append({"params": combo, "return": ret, "win_rate": wr, "pf": pf, "sharpe": sh, "dd": dd})
        except Exception as e:
            print(f"  エラー: {e}")

    if results:
        best = max(results, key=lambda x: x["sharpe"])
        print(f"\n★ シャープ比最高: {best['params']}  シャープ={best['sharpe']:.2f}  リターン={best['return']:+.2f}%")

    print(f"{'='*80}\n")


# ─────────────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="バックテスト CLI")
    parser.add_argument("--start",     default="2021-01-01",  help="開始日 (YYYY-MM-DD)")
    parser.add_argument("--end",       default="2024-12-31",  help="終了日 (YYYY-MM-DD)")
    parser.add_argument("--threshold", type=float, default=70, help="買いスコア閾値")
    parser.add_argument("--stop",      type=float, default=5,  help="損切り%")
    parser.add_argument("--profit",    type=float, default=15, help="利確%")
    parser.add_argument("--trail",     type=float, default=6,  help="トレーリングストップ%")
    parser.add_argument("--sweep",     action="store_true",    help="パラメータスイープを実行")
    args = parser.parse_args()

    if args.sweep:
        run_sweep()
        return

    params = {
        "start_date":        args.start,
        "end_date":          args.end,
        "buy_threshold":     args.threshold,
        "stop_loss_pct":     args.stop,
        "take_profit_pct":   args.profit,
        "trailing_stop_pct": args.trail,
    }

    print(f"バックテスト実行中... (データ取得に数分かかります)")
    summary, trades_list = run_single(params, save=True)
    print_report(params, summary, trades_list)

    # ① タイムスタンプ付きで保存（履歴用）
    out_dir = ROOT / "results"
    out_dir.mkdir(exist_ok=True)
    ts_fname = out_dir / f"bt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    payload = {"params": params, "summary": summary, "trades": trades_list}
    with open(ts_fname, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    # ② latest.json に上書き（Claude が常にここを読む）
    latest = out_dir / "latest.json"
    with open(latest, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    print(f"結果を保存しました: {ts_fname}")
    print(f"latest.json を更新しました: {latest}")

    # ③ git push で Claude が読めるようにする
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    print("\nClaudeが読めるよう git push します...")
    ret = os.system(
        f'cd "{ROOT}" && '
        f'git add results/latest.json && '
        f'git commit -m "backtest result {ts}" --allow-empty && '
        f'git push origin HEAD'
    )
    if ret == 0:
        print("✓ push 完了 — Claudeが結果を確認できます")
    else:
        print("⚠ push に失敗しました。手動で git push してください")


if __name__ == "__main__":
    main()
