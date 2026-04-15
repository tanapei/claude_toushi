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
    TOTAL_CAPITAL,
    POSITION_SIZE,
    MAX_POSITIONS,
    JP_UNIVERSE,
    US_UNIVERSE,
)
from trading_system.trade_logger import TradeLogger

app = Flask(__name__)
CORS(app)

# クラウド環境ではストリームのみ、ローカルではファイルログも出力
_log_handlers = [logging.StreamHandler()]
try:
    from trading_system.config import LOGS_DIR
    _log_handlers.append(
        logging.FileHandler(LOGS_DIR / "trading.log", encoding="utf-8")
    )
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# グローバル状態管理
# ─────────────────────────────────────────────

class RunState:
    """バックテスト実行状態を管理するスレッドセーフな状態クラス"""

    def __init__(self):
        self.lock = threading.Lock()
        self.is_running = False
        self.cancel_requested = False
        self.run_id: Optional[str] = None
        self.progress = 0        # 0-100
        self.message = ""
        self.error: Optional[str] = None
        self.result: Optional[dict] = None
        self.log_queue: queue.Queue = queue.Queue(maxsize=500)

    def reset(self):
        with self.lock:
            self.is_running = True
            self.cancel_requested = False
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

    def is_cancelled(self) -> bool:
        with self.lock:
            return self.cancel_requested

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "is_running": self.is_running,
                "cancel_requested": self.cancel_requested,
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
        try:
            if self.mode == "iterative":
                results = self._run_iterative()
                if run_state.is_cancelled():
                    run_state.fail("キャンセルされました")
                    return
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
                if run_state.is_cancelled():
                    run_state.fail("キャンセルされました")
                    return
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

        run_state.update(15, "データ取得中...")
        backtester._load_data()

        run_state.update(30, "テクニカル指標を計算中...")
        backtester._add_indicators()

        run_state.update(40, "シミュレーション実行中...")

        def sim_cb(pct, msg):
            run_state.update(40 + int(pct * 0.44), msg)

        backtester._run_simulation(
            progress_callback=sim_cb,
            cancel_check=run_state.is_cancelled,
        )

        if run_state.is_cancelled():
            return {"run_id": backtester.run_id, "summary": {}, "analysis": None}

        run_state.update(86, "結果を集計・保存中...")
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
        from trading_system.data_fetcher import load_universe_data, get_benchmark_data

        results = []
        params = self.strategy_params.copy()

        # ─── データは1回だけダウンロード ───
        run_state.update(5, "株価データを取得中（全イテレーション共通）...")
        raw_data = load_universe_data(self.start_date, self.end_date)
        if not raw_data:
            raise RuntimeError("株価データの取得に失敗しました")

        run_state.update(12, "日経225データを取得中...")
        nikkei_raw = get_benchmark_data(self.start_date, self.end_date)

        all_dates_set: set = set()
        for df in raw_data.values():
            all_dates_set.update(df.index.tolist())
        all_dates = sorted(all_dates_set)

        run_state.update(15, f"データ取得完了 ({len(raw_data)}銘柄 / {len(all_dates)}営業日)")

        # ─── 各イテレーション ───
        # 進捗帯域: 15〜90% を iterations 等分
        band = (90 - 15) // self.iterations

        for i in range(1, self.iterations + 1):
            if run_state.is_cancelled():
                break

            base = 15 + (i - 1) * band
            end  = 15 + i * band

            run_state.update(base, f"イテレーション {i}/{self.iterations} 開始...")

            backtester = Backtester(
                start_date=self.start_date,
                end_date=self.end_date,
                strategy_params=params,
            )
            if i == 1:
                run_state.run_id = backtester.run_id

            # プリロード済みデータを注入（再ダウンロード不要）
            backtester.set_raw_data(raw_data, nikkei_raw, all_dates)

            run_state.update(base + 2, f"[{i}/{self.iterations}] 指標計算中...")
            backtester._add_indicators()

            # シミュレーション（進捗コールバック付き）
            sim_span = end - base - 10

            def make_cb(b, span, idx, total):
                def cb(pct, msg):
                    run_state.update(b + 4 + int(pct / 100 * span), f"[{idx}/{total}] {msg}")
                return cb

            run_state.update(base + 4, f"[{i}/{self.iterations}] シミュレーション実行中...")
            backtester._run_simulation(
                progress_callback=make_cb(base, sim_span, i, self.iterations),
                cancel_check=run_state.is_cancelled,
            )

            if run_state.is_cancelled():
                break

            run_state.update(end - 6, f"[{i}/{self.iterations}] 結果を保存中...")
            summary = backtester.portfolio.get_summary()
            summary["start_date"] = backtester.start_date
            summary["end_date"] = backtester.end_date
            backtester._save_results(summary)

            run_state.update(end - 3, f"[{i}/{self.iterations}] Claude AI が分析中...")
            analysis = backtester._run_analysis(summary)

            results.append({
                "run_id": backtester.run_id,
                "summary": summary,
                "analysis": analysis,
            })

            # 次イテレーションのパラメータをClaudeの提案から更新
            if analysis and i < self.iterations:
                next_params = analysis.get("next_strategy_params", {})
                if next_params:
                    params = next_params
                    run_state.update(
                        end,
                        f"[{i}/{self.iterations}] パラメータ更新: "
                        f"EMA({params.get('ema_fast')}/{params.get('ema_slow')}), "
                        f"RSI({params.get('rsi_lower')}-{params.get('rsi_upper')})",
                    )

        return results


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


@app.route("/api/cancel", methods=["POST"])
def cancel_run():
    """実行中のバックテストをキャンセルする"""
    with run_state.lock:
        if run_state.is_running:
            run_state.cancel_requested = True
            return jsonify({"status": "cancelling"})
    return jsonify({"status": "not_running"})


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
# シグナル配信 API
# ─────────────────────────────────────────────

# 最新シグナル結果をメモリにキャッシュ
_latest_signals: dict = {}


@app.route("/api/run-signal", methods=["POST", "GET"])
def run_daily_signal():
    """デイリーシグナルを生成して結果を返す。"""
    market = request.args.get("market", "JP").upper()
    if market not in ("JP", "US"):
        return jsonify({"error": "market は JP または US を指定してください"}), 400

    try:
        from trading_system.signal_runner import SignalRunner
        runner = SignalRunner(market=market)
        result = runner.run()
        _latest_signals[market] = result
        return jsonify({
            "status": "ok",
            "market": market,
            "buy_count": len(result.get("buy_signals", [])),
            "sell_count": len(result.get("sell_signals", [])),
            "market_bullish": result.get("market_bullish", True),
            "timestamp": result.get("timestamp"),
        })
    except Exception as e:
        logger.exception(f"シグナル生成エラー ({market})")
        return jsonify({"error": str(e)}), 500


@app.route("/api/signals/latest")
def get_latest_signals():
    """最新シグナル結果を返す（Web UI 用）。"""
    market = request.args.get("market", "JP").upper()
    result = _latest_signals.get(market)
    if not result:
        return jsonify({"error": "シグナル未生成", "message": "まだシグナルが生成されていません"}), 404

    # top_scored の reasons を短縮して返す
    top = []
    for s in result.get("top_scored", [])[:15]:
        from trading_system.factor_scorer import get_ticker_label
        top.append({
            "ticker": s["ticker"],
            "label": get_ticker_label(s["ticker"]),
            "score": s["score"],
            "signal": s["signal"],
            "momentum_6m_pct": s.get("momentum_6m_pct", 0),
            "rsi": s.get("details", {}).get("rsi", 0),
            "price": s.get("details", {}).get("price", 0),
        })

    return jsonify({
        "market": market,
        "buy_signals": result.get("buy_signals", []),
        "sell_signals": result.get("sell_signals", []),
        "market_bullish": result.get("market_bullish", True),
        "top_scored": top,
        "timestamp": result.get("timestamp"),
    })


# ─────────────────────────────────────────────
# ポートフォリオ管理 API
# ─────────────────────────────────────────────

@app.route("/api/portfolio", methods=["GET"])
def get_portfolio():
    """現在の保有ポジションを返す。"""
    from trading_system.portfolio_state import PortfolioState
    portfolio = PortfolioState()
    return jsonify({
        **portfolio.to_dict(),
        "total_capital": TOTAL_CAPITAL,
        "position_size": POSITION_SIZE,
        "max_positions": MAX_POSITIONS,
    })


@app.route("/api/portfolio", methods=["POST"])
def add_portfolio_position():
    """ポジションを追加する（手動入力）。"""
    from trading_system.portfolio_state import PortfolioState
    data = request.get_json() or {}

    ticker       = data.get("ticker", "").upper().strip()
    entry_price  = float(data.get("entry_price", 0))
    shares       = float(data.get("shares", 0))
    entry_date   = data.get("entry_date", "")
    market       = data.get("market", "JP").upper()

    if not ticker or entry_price <= 0 or shares <= 0:
        return jsonify({"error": "ticker, entry_price, shares は必須です"}), 400

    portfolio = PortfolioState()
    pos = portfolio.add_position(
        ticker=ticker,
        entry_price=entry_price,
        shares=shares,
        entry_date=entry_date,
        market=market,
        invested_amount=int(entry_price * shares),
    )
    return jsonify({"status": "added", "position": pos})


@app.route("/api/portfolio/<ticker>", methods=["DELETE"])
def delete_portfolio_position(ticker: str):
    """ポジションを削除する（売却後の手動削除）。"""
    from trading_system.portfolio_state import PortfolioState
    portfolio = PortfolioState()
    removed = portfolio.remove_position(ticker.upper())
    if removed:
        return jsonify({"status": "removed", "ticker": ticker.upper()})
    return jsonify({"error": f"{ticker} は保有していません"}), 404


# ─────────────────────────────────────────────
# 仮保有ポートフォリオ API
# ─────────────────────────────────────────────

@app.route("/api/virtual-portfolio", methods=["GET"])
def get_virtual_portfolio():
    """仮保有ポジション一覧（現在の損益付き）を返す。"""
    from trading_system.virtual_portfolio import VirtualPortfolio
    vp = VirtualPortfolio()
    return jsonify(vp.get_positions_with_pnl())


@app.route("/api/virtual-portfolio/list", methods=["GET"])
def get_virtual_portfolio_list():
    """現在値取得なしで仮保有リストだけを返す（高速）。"""
    from trading_system.virtual_portfolio import VirtualPortfolio
    vp = VirtualPortfolio()
    return jsonify({
        "positions": list(vp.positions.values()),
        "count": len(vp.positions),
    })


@app.route("/api/virtual-portfolio", methods=["POST"])
def add_virtual_portfolio_position():
    """買いシグナルから仮保有ポジションを登録する。"""
    from trading_system.virtual_portfolio import VirtualPortfolio
    data = request.get_json() or {}

    ticker       = data.get("ticker", "").upper().strip()
    entry_price  = float(data.get("entry_price", 0))
    market       = data.get("market", "JP").upper()
    label        = data.get("label", "")
    signal_score = int(data.get("signal_score", 0))
    entry_date   = data.get("entry_date", "")

    if not ticker or entry_price <= 0:
        return jsonify({"error": "ticker と entry_price は必須です"}), 400

    vp = VirtualPortfolio()
    pos = vp.add_position(
        ticker=ticker,
        entry_price=entry_price,
        market=market,
        label=label,
        signal_score=signal_score,
        entry_date=entry_date,
    )
    return jsonify({"status": "added", "position": pos})


@app.route("/api/virtual-portfolio/<ticker>", methods=["DELETE"])
def delete_virtual_portfolio_position(ticker: str):
    """仮保有ポジションを削除する。"""
    from trading_system.virtual_portfolio import VirtualPortfolio
    vp = VirtualPortfolio()
    removed = vp.remove_position(ticker.upper())
    if removed:
        return jsonify({"status": "removed", "ticker": ticker.upper()})
    return jsonify({"error": f"{ticker} は仮保有していません"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n株式取引シミュレーション UI 起動中...")
    print(f"ブラウザで http://localhost:{port} を開いてください\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
