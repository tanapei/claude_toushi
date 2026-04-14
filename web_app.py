"""
Flask Web アプリケーション
株式取引シミュレーションシステムのUIバックエンドです。
バックテストをバックグラウンドで実行し、SSEでリアルタイム進捗を配信します。
"""
import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from flask_cors import CORS

from trading_system.config import (
    BACKTEST_START,
    BACKTEST_END,
    STRATEGY_PARAMS,
    VALID_UNIVERSE,
    INITIAL_CAPITAL,
)
from trading_system.trade_logger import TradeLogger

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# グローバル状態管理
# ─────────────────────────────────────────────

class RunState:
    """バックテスト実行状態を管理するスレッドセーフな状態クラス"""

    def __init__(self):
        self.lock = threading.Lock()
        self.is_running = False
        self.run_id: Optional[str] = None
        self.progress = 0        # 0-100
        self.message = ""
        self.error: Optional[str] = None
        self.result: Optional[dict] = None
        self.log_queue: queue.Queue = queue.Queue(maxsize=500)

    def reset(self):
        with self.lock:
            self.is_running = True
            self.progress = 0
            self.message = "初期化中..."
            self.error = None
            self.result = None
            # キューをクリア
            while not self.log_queue.empty():
                self.log_queue.get_nowait()

    def update(self, progress: int, message: str):
        with self.lock:
            self.progress = progress
            self.message = message
        self.log_queue.put_nowait({"progress": progress, "message": message})

    def finish(self, result: dict):
        with self.lock:
            self.is_running = False
            self.progress = 100
            self.result = result
            self.message = "完了"
        self.log_queue.put_nowait({"progress": 100, "message": "完了", "done": True})

    def fail(self, error: str):
        with self.lock:
            self.is_running = False
            self.error = error
            self.message = f"エラー: {error}"
        self.log_queue.put_nowait({"progress": -1, "message": f"エラー: {error}", "error": True, "done": True})

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "is_running": self.is_running,
                "run_id": self.run_id,
                "progress": self.progress,
                "message": self.message,
                "error": self.error,
                "has_result": self.result is not None,
            }


run_state = RunState()
trade_logger = TradeLogger()


# ─────────────────────────────────────────────
# バックテストワーカー（別スレッド）
# ─────────────────────────────────────────────

class ProgressBacktester:
    """進捗をrun_stateに書き込みながらバックテストを実行するラッパー"""

    def __init__(self, start_date, end_date, strategy_params, mode, iterations):
        self.start_date = start_date
        self.end_date = end_date
        self.strategy_params = strategy_params
        self.mode = mode
        self.iterations = iterations

    def run(self):
        from trading_system.backtest import Backtester, run_iterative_backtest
        from trading_system.config import VALID_UNIVERSE

        run_state.update(5, "株価データを取得中...")

        try:
            if self.mode == "iterative":
                results = self._run_iterative()
                # 最終結果を集約
                final_result = {
                    "mode": "iterative",
                    "iterations": [
                        {
                            "run_id": r["run_id"],
                            "summary": r["summary"],
                            "analysis": r.get("analysis"),
                        }
                        for r in results
                    ],
                }
                run_state.finish(final_result)
            else:
                result = self._run_single()
                final_result = {
                    "mode": "single",
                    "run_id": result["run_id"],
                    "summary": result["summary"],
                    "analysis": result.get("analysis"),
                }
                run_state.finish(final_result)
        except Exception as e:
            logger.exception("バックテスト実行エラー")
            run_state.fail(str(e))

    def _run_single(self):
        from trading_system.backtest import Backtester

        run_state.update(10, "バックテストエンジン初期化中...")

        backtester = Backtester(
            start_date=self.start_date,
            end_date=self.end_date,
            strategy_params=self.strategy_params,
        )
        run_state.run_id = backtester.run_id

        run_state.update(15, "データ取得・指標計算中...")
        backtester._load_data()

        run_state.update(30, "テクニカル指標を計算中...")
        backtester._add_indicators()

        run_state.update(40, "シミュレーション実行中...")
        # シミュレーションを段階的に進捗更新しながら実行
        self._patch_simulation(backtester)
        backtester._run_simulation()

        run_state.update(85, "結果を集計・保存中...")
        summary = backtester.portfolio.get_summary()
        summary["start_date"] = backtester.start_date
        summary["end_date"] = backtester.end_date
        backtester.portfolio.print_summary()
        backtester._save_results(summary)

        run_state.update(92, "Claude AI が分析中...")
        analysis = backtester._run_analysis(summary)

        return {
            "run_id": backtester.run_id,
            "summary": summary,
            "analysis": analysis,
        }

    def _run_iterative(self):
        from trading_system.backtest import Backtester
        results = []
        params = self.strategy_params.copy()

        for i in range(1, self.iterations + 1):
            base_pct = int((i - 1) / self.iterations * 85)
            iter_pct = int(i / self.iterations * 85)

            run_state.update(base_pct + 5, f"イテレーション {i}/{self.iterations} 開始...")

            backtester = Backtester(
                start_date=self.start_date,
                end_date=self.end_date,
                strategy_params=params,
            )
            if i == 1:
                run_state.run_id = backtester.run_id

            run_state.update(base_pct + 10, f"[{i}/{self.iterations}] データ取得中...")
            backtester._load_data()

            run_state.update(base_pct + 15, f"[{i}/{self.iterations}] 指標計算中...")
            backtester._add_indicators()

            run_state.update(base_pct + 20, f"[{i}/{self.iterations}] シミュレーション実行中...")
            backtester._run_simulation()

            run_state.update(iter_pct - 5, f"[{i}/{self.iterations}] 結果保存・AI分析中...")
            summary = backtester.portfolio.get_summary()
            summary["start_date"] = backtester.start_date
            summary["end_date"] = backtester.end_date
            backtester._save_results(summary)
            analysis = backtester._run_analysis(summary)

            result = {
                "run_id": backtester.run_id,
                "summary": summary,
                "analysis": analysis,
            }
            results.append(result)

            # 次のイテレーションのパラメータを更新
            if analysis and i < self.iterations:
                next_params = analysis.get("next_strategy_params", {})
                if next_params:
                    params = next_params
                    run_state.update(iter_pct, f"[{i}/{self.iterations}] パラメータ更新: EMA({params.get('ema_fast')}/{params.get('ema_slow')})")

        return results

    def _patch_simulation(self, backtester):
        """シミュレーションに進捗コールバックを注入する"""
        original = backtester._run_simulation

        def patched():
            all_dates = backtester.all_dates
            n = len(all_dates)
            orig_run = backtester._run_simulation

            # モンキーパッチで進捗を更新
            import trading_system.backtest as bt_module
            original_fn = bt_module.Backtester._run_simulation

            def _run_sim_with_progress(self_inner):
                from trading_system.strategy import generate_buy_signal, generate_sell_signal, rank_by_momentum
                params = self_inner.strategy_params
                top_n = params.get("top_n_momentum", 10)

                for i, date in enumerate(self_inner.all_dates):
                    if i % max(1, n // 20) == 0:
                        pct = 40 + int(i / n * 40)
                        run_state.update(pct, f"シミュレーション中... {date.date()} ({i}/{n}日)")

                    current_prices = self_inner._get_prices_at(date)

                    positions_to_sell = []
                    for ticker, pos in list(self_inner.portfolio.positions.items()):
                        if ticker not in self_inner.data or date not in self_inner.data[ticker].index:
                            continue
                        sell_signal = generate_sell_signal(
                            df=self_inner.data[ticker],
                            date=date,
                            entry_price=pos.entry_price,
                            highest_price=pos.highest_price,
                            params=params,
                        )
                        if sell_signal:
                            positions_to_sell.append((ticker, sell_signal))

                    for ticker, signal in positions_to_sell:
                        self_inner.portfolio.sell(
                            ticker=ticker, date=date,
                            price=signal.price, reason=signal.reason,
                            indicators=signal.indicators,
                        )

                    if len(self_inner.portfolio.positions) < self_inner.portfolio.max_positions:
                        candidates = rank_by_momentum(self_inner.data, date, top_n)
                        for ticker in candidates:
                            if ticker in self_inner.portfolio.positions:
                                continue
                            if len(self_inner.portfolio.positions) >= self_inner.portfolio.max_positions:
                                break
                            if ticker not in self_inner.data or date not in self_inner.data[ticker].index:
                                continue
                            buy_signal = generate_buy_signal(
                                df=self_inner.data[ticker], date=date, params=params
                            )
                            if buy_signal:
                                buy_signal.ticker = ticker
                                self_inner.portfolio.buy(
                                    ticker=ticker, date=date,
                                    price=buy_signal.price, reason=buy_signal.reason,
                                    indicators=buy_signal.indicators,
                                )

                    self_inner.portfolio.update_trailing_stops(date, current_prices)
                    self_inner.portfolio.record_equity(date, current_prices)

                self_inner._close_all_positions()

            backtester._run_simulation = lambda: _run_sim_with_progress(backtester)

        patched()


def _worker(start_date, end_date, strategy_params, mode, iterations):
    pb = ProgressBacktester(start_date, end_date, strategy_params, mode, iterations)
    pb.run()


# ─────────────────────────────────────────────
# API ルート
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config/defaults")
def get_defaults():
    """デフォルト設定を返す"""
    return jsonify({
        "start_date": BACKTEST_START,
        "end_date": BACKTEST_END,
        "strategy_params": STRATEGY_PARAMS,
        "initial_capital": INITIAL_CAPITAL,
        "universe": VALID_UNIVERSE,
    })


@app.route("/api/run", methods=["POST"])
def start_run():
    """バックテストを開始する"""
    if run_state.is_running:
        return jsonify({"error": "既に実行中です"}), 409

    data = request.get_json() or {}
    start_date = data.get("start_date", BACKTEST_START)
    end_date = data.get("end_date", BACKTEST_END)
    mode = data.get("mode", "single")
    iterations = int(data.get("iterations", 3))
    strategy_params = data.get("strategy_params", STRATEGY_PARAMS.copy())

    # 型変換（フロントから文字列で来る可能性）
    for k, v in strategy_params.items():
        if k == "volume_multiplier":
            strategy_params[k] = float(v)
        else:
            try:
                strategy_params[k] = int(v)
            except (ValueError, TypeError):
                pass

    run_state.reset()

    t = threading.Thread(
        target=_worker,
        args=(start_date, end_date, strategy_params, mode, iterations),
        daemon=True,
    )
    t.start()

    return jsonify({"status": "started", "mode": mode})


@app.route("/api/status")
def get_status():
    """現在の実行状態を返す"""
    return jsonify(run_state.snapshot())


@app.route("/api/events")
def sse_events():
    """Server-Sent Events でリアルタイム進捗を配信する"""
    def generate():
        yield "data: {}\n\n"  # 接続確認
        while True:
            try:
                event = run_state.log_queue.get(timeout=30)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("done"):
                    break
            except queue.Empty:
                yield ": keepalive\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/sessions")
def get_sessions():
    """全バックテストセッション一覧を返す"""
    df = trade_logger.get_all_sessions()
    if df.empty:
        return jsonify([])
    records = df.to_dict(orient="records")
    # NaN を None に変換
    clean = []
    for r in records:
        clean.append({k: (None if (isinstance(v, float) and v != v) else v) for k, v in r.items()})
    return jsonify(clean)


@app.route("/api/sessions/<run_id>")
def get_session(run_id):
    """特定セッションの詳細を返す"""
    session = trade_logger.get_session(run_id)
    if not session:
        return jsonify({"error": "Not found"}), 404
    return jsonify(session)


@app.route("/api/trades/<run_id>")
def get_trades(run_id):
    """特定セッションの取引履歴を返す"""
    df = trade_logger.get_trades(run_id)
    if df.empty:
        return jsonify([])
    return jsonify(df.to_dict(orient="records"))


@app.route("/api/equity/<run_id>")
def get_equity(run_id):
    """特定セッションの資産推移を返す"""
    from trading_system.config import DB_PATH
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT date, cash, position_value, total_equity, n_positions FROM equity_curves WHERE run_id=? ORDER BY date",
        (run_id,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/analysis/latest")
def get_latest_analysis():
    """最新のClaude AI分析結果を返す"""
    analysis = trade_logger.get_latest_analysis()
    if not analysis:
        return jsonify({"error": "分析結果なし"}), 404
    return jsonify(analysis)


@app.route("/api/analysis/<run_id>")
def get_analysis(run_id):
    """特定セッションの分析結果を返す"""
    from trading_system.config import DB_PATH
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM analysis_results WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
        (run_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    d = dict(row)
    for key in ["good_points", "bad_points", "improvement_suggestions", "next_strategy_params"]:
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except Exception:
                pass
    return jsonify(d)


@app.route("/api/performance")
def get_cumulative_performance():
    """全セッションの累積パフォーマンスを返す"""
    return jsonify(trade_logger.get_cumulative_performance())


# ─────────────────────────────────────────────
# 最適化 API
# ─────────────────────────────────────────────

class OptimizeState:
    def __init__(self):
        self.is_running = False
        self.progress = 0
        self.message = ""
        self.result = None
        self.error = None
        self.log_queue: queue.Queue = queue.Queue(maxsize=500)

    def reset(self):
        self.is_running = True
        self.progress = 0
        self.message = "初期化中..."
        self.result = None
        self.error = None
        while not self.log_queue.empty():
            self.log_queue.get_nowait()

    def update(self, current, total, message):
        pct = int(current / max(total, 1) * 90)
        self.progress = pct
        self.message = message
        self.log_queue.put_nowait({"progress": pct, "message": message})

    def finish(self, result):
        self.is_running = False
        self.progress = 100
        self.result = result
        self.log_queue.put_nowait({"progress": 100, "message": "最適化完了", "done": True})

    def fail(self, error):
        self.is_running = False
        self.error = error
        self.log_queue.put_nowait({"progress": -1, "message": f"エラー: {error}", "error": True, "done": True})


opt_state = OptimizeState()


def _opt_worker(start_date, end_date, max_combinations, use_walk_forward, param_grid):
    from trading_system.optimize import run_optimization
    try:
        result = run_optimization(
            start_date=start_date,
            end_date=end_date,
            max_combinations=max_combinations,
            use_walk_forward=use_walk_forward,
            param_grid=param_grid if param_grid else None,
            progress_callback=opt_state.update,
        )
        opt_state.finish(result)
    except Exception as e:
        logger.exception("最適化エラー")
        opt_state.fail(str(e))


@app.route("/api/optimize", methods=["POST"])
def start_optimize():
    if opt_state.is_running:
        return jsonify({"error": "最適化実行中"}), 409

    data = request.get_json() or {}
    start_date       = data.get("start_date", BACKTEST_START)
    end_date         = data.get("end_date", BACKTEST_END)
    max_combinations = int(data.get("max_combinations", 30))
    use_wf           = bool(data.get("walk_forward", True))
    param_grid       = data.get("param_grid", None)

    opt_state.reset()
    t = threading.Thread(
        target=_opt_worker,
        args=(start_date, end_date, max_combinations, use_wf, param_grid),
        daemon=True,
    )
    t.start()
    return jsonify({"status": "started"})


@app.route("/api/optimize/status")
def opt_status():
    return jsonify({
        "is_running": opt_state.is_running,
        "progress":   opt_state.progress,
        "message":    opt_state.message,
        "has_result": opt_state.result is not None,
        "error":      opt_state.error,
    })


@app.route("/api/optimize/events")
def opt_sse_events():
    def generate():
        yield "data: {}\n\n"
        while True:
            try:
                ev = opt_state.log_queue.get(timeout=30)
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev.get("done"):
                    break
            except queue.Empty:
                yield ": keepalive\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/optimize/result")
def opt_result():
    if opt_state.result is None:
        return jsonify({"error": "結果なし"}), 404
    return jsonify(opt_state.result)


@app.route("/api/optimize/default_grid")
def opt_default_grid():
    from trading_system.optimize import DEFAULT_PARAM_GRID
    return jsonify(DEFAULT_PARAM_GRID)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n株式取引シミュレーション UI 起動中...")
    print(f"ブラウザで http://localhost:{port} を開いてください\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
