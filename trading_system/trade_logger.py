"""
取引ログモジュール
SQLiteデータベースに取引履歴・バックテスト結果・Claude分析結果を保存します。
"""
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from trading_system.config import DB_PATH

logger = logging.getLogger(__name__)


def init_db(db_path: Path = DB_PATH) -> None:
    """データベースとテーブルを初期化する。"""
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # バックテストセッションテーブル
    cur.execute("""
        CREATE TABLE IF NOT EXISTS backtest_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT UNIQUE NOT NULL,
            start_date TEXT,
            end_date TEXT,
            initial_capital REAL,
            final_equity REAL,
            total_return_pct REAL,
            total_trades INTEGER,
            win_rate REAL,
            sharpe_ratio REAL,
            max_drawdown_pct REAL,
            profit_factor REAL,
            strategy_params TEXT,  -- JSON
            summary_json TEXT,     -- JSON（完全なサマリー）
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # 個別取引テーブル
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            ticker TEXT,
            entry_date TEXT,
            exit_date TEXT,
            entry_price REAL,
            exit_price REAL,
            shares INTEGER,
            pnl REAL,
            pnl_pct REAL,
            hold_days INTEGER,
            exit_reason TEXT,
            entry_indicators TEXT,  -- JSON
            exit_indicators TEXT,   -- JSON
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (run_id) REFERENCES backtest_sessions(run_id)
        )
    """)

    # Claude分析結果テーブル
    cur.execute("""
        CREATE TABLE IF NOT EXISTS analysis_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            analysis_type TEXT,     -- 'post_backtest' | 'strategy_improvement'
            good_points TEXT,       -- JSON: 良かった点
            bad_points TEXT,        -- JSON: 悪かった点
            improvement_suggestions TEXT,  -- JSON: 改善提案
            next_strategy_params TEXT,     -- JSON: 次回推奨パラメータ
            full_analysis TEXT,     -- Claudeの生レスポンス
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (run_id) REFERENCES backtest_sessions(run_id)
        )
    """)

    # 資産推移テーブル
    cur.execute("""
        CREATE TABLE IF NOT EXISTS equity_curves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            date TEXT,
            cash REAL,
            position_value REAL,
            total_equity REAL,
            n_positions INTEGER,
            FOREIGN KEY (run_id) REFERENCES backtest_sessions(run_id)
        )
    """)

    conn.commit()
    conn.close()
    logger.info(f"データベース初期化完了: {db_path}")


class TradeLogger:
    """取引ログの読み書きを管理するクラス"""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        init_db(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # 書き込み
    # ------------------------------------------------------------------

    def save_session(
        self,
        run_id: str,
        start_date: str,
        end_date: str,
        summary: Dict,
        strategy_params: Dict,
    ) -> None:
        """バックテストセッションを保存する。"""
        conn = self._connect()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO backtest_sessions
                (run_id, start_date, end_date, initial_capital, final_equity,
                 total_return_pct, total_trades, win_rate, sharpe_ratio,
                 max_drawdown_pct, profit_factor, strategy_params, summary_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id, start_date, end_date,
                summary.get("initial_capital"),
                summary.get("final_equity"),
                summary.get("total_return_pct"),
                summary.get("total_trades"),
                summary.get("win_rate"),
                summary.get("sharpe_ratio"),
                summary.get("max_drawdown_pct"),
                summary.get("profit_factor"),
                json.dumps(strategy_params, ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False, default=str),
            ))
            conn.commit()
            logger.info(f"セッション保存: {run_id}")
        finally:
            conn.close()

    def save_trades(self, run_id: str, trades: List[Dict]) -> None:
        """取引履歴を一括保存する。"""
        if not trades:
            return
        conn = self._connect()
        try:
            conn.executemany("""
                INSERT INTO trades
                (run_id, ticker, entry_date, exit_date, entry_price, exit_price,
                 shares, pnl, pnl_pct, hold_days, exit_reason,
                 entry_indicators, exit_indicators)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (
                    run_id,
                    t["ticker"],
                    t["entry_date"],
                    t["exit_date"],
                    t["entry_price"],
                    t["exit_price"],
                    t["shares"],
                    t["pnl"],
                    t["pnl_pct"],
                    t["hold_days"],
                    t["exit_reason"],
                    json.dumps(t.get("entry_indicators", {}), ensure_ascii=False),
                    json.dumps(t.get("exit_indicators", {}), ensure_ascii=False),
                )
                for t in trades
            ])
            conn.commit()
            logger.info(f"{len(trades)}件の取引を保存: {run_id}")
        finally:
            conn.close()

    def save_equity_curve(self, run_id: str, equity_curve: List[Dict]) -> None:
        """資産推移を保存する。"""
        if not equity_curve:
            return
        conn = self._connect()
        try:
            conn.executemany("""
                INSERT INTO equity_curves (run_id, date, cash, position_value, total_equity, n_positions)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [
                (run_id, e["date"], e["cash"], e["position_value"], e["total_equity"], e["n_positions"])
                for e in equity_curve
            ])
            conn.commit()
        finally:
            conn.close()

    def save_analysis(
        self,
        run_id: str,
        analysis_type: str,
        good_points: List[str],
        bad_points: List[str],
        improvement_suggestions: List[str],
        next_strategy_params: Dict,
        full_analysis: str,
    ) -> None:
        """Claude分析結果を保存する。"""
        conn = self._connect()
        try:
            conn.execute("""
                INSERT INTO analysis_results
                (run_id, analysis_type, good_points, bad_points,
                 improvement_suggestions, next_strategy_params, full_analysis)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id, analysis_type,
                json.dumps(good_points, ensure_ascii=False),
                json.dumps(bad_points, ensure_ascii=False),
                json.dumps(improvement_suggestions, ensure_ascii=False),
                json.dumps(next_strategy_params, ensure_ascii=False, default=str),
                full_analysis,
            ))
            conn.commit()
            logger.info(f"分析結果保存: {run_id}")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 読み込み
    # ------------------------------------------------------------------

    def get_all_sessions(self) -> pd.DataFrame:
        """全バックテストセッションを取得する。"""
        conn = self._connect()
        try:
            df = pd.read_sql("SELECT * FROM backtest_sessions ORDER BY created_at DESC", conn)
            return df
        finally:
            conn.close()

    def get_session(self, run_id: str) -> Optional[Dict]:
        """特定セッションの詳細を取得する。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM backtest_sessions WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row:
                d = dict(row)
                d["summary"] = json.loads(d.get("summary_json") or "{}")
                d["strategy_params"] = json.loads(d.get("strategy_params") or "{}")
                return d
            return None
        finally:
            conn.close()

    def get_trades(self, run_id: Optional[str] = None) -> pd.DataFrame:
        """取引履歴を取得する。run_idを指定すると特定セッションのみ。"""
        conn = self._connect()
        try:
            if run_id:
                df = pd.read_sql(
                    "SELECT * FROM trades WHERE run_id = ? ORDER BY entry_date",
                    conn, params=(run_id,)
                )
            else:
                df = pd.read_sql("SELECT * FROM trades ORDER BY entry_date", conn)
            return df
        finally:
            conn.close()

    def get_latest_analysis(self) -> Optional[Dict]:
        """最新のClaude分析結果を取得する。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM analysis_results ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if row:
                d = dict(row)
                d["good_points"] = json.loads(d.get("good_points") or "[]")
                d["bad_points"] = json.loads(d.get("bad_points") or "[]")
                d["improvement_suggestions"] = json.loads(d.get("improvement_suggestions") or "[]")
                d["next_strategy_params"] = json.loads(d.get("next_strategy_params") or "{}")
                return d
            return None
        finally:
            conn.close()

    def get_cumulative_performance(self) -> Dict:
        """全セッションの累積パフォーマンスを集計する。"""
        conn = self._connect()
        try:
            sessions = pd.read_sql(
                "SELECT * FROM backtest_sessions ORDER BY created_at", conn
            )
            if sessions.empty:
                return {}

            return {
                "total_sessions": len(sessions),
                "avg_return_pct": round(sessions["total_return_pct"].mean(), 2),
                "best_return_pct": round(sessions["total_return_pct"].max(), 2),
                "worst_return_pct": round(sessions["total_return_pct"].min(), 2),
                "avg_win_rate": round(sessions["win_rate"].mean(), 1),
                "avg_sharpe": round(sessions["sharpe_ratio"].mean(), 2),
                "avg_max_drawdown": round(sessions["max_drawdown_pct"].mean(), 2),
            }
        finally:
            conn.close()

    def export_to_json(self, run_id: str, output_path: Path) -> None:
        """指定セッションの全データをJSONにエクスポートする。"""
        session = self.get_session(run_id)
        trades = self.get_trades(run_id).to_dict(orient="records")
        analysis = self.get_latest_analysis()

        export = {
            "session": session,
            "trades": trades,
            "analysis": analysis,
            "exported_at": datetime.now().isoformat(),
        }

        output_path.write_text(
            json.dumps(export, ensure_ascii=False, indent=2, default=str)
        )
        logger.info(f"JSONエクスポート完了: {output_path}")
