"""
トレード日記モジュール

売却のたびに Claude がトレードを分析し、日記エントリを生成・保存する。
日記は次回の改善サイクルのコンテキストとして再利用される。

フロー:
  1. order_executor._sell() → auto_improver.log_real_trade() → write_diary_entry()
  2. Claude が勝敗の原因・教訓を分析
  3. DATA_DIR/trade_diary.json に追記
  4. _claude_improve() 呼び出し時に get_diary_context() を参照
"""
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

import anthropic

from trading_system.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, DATA_DIR

logger = logging.getLogger(__name__)

TRADE_DIARY_FILE = DATA_DIR / "trade_diary.json"


# ─── 読み書き ──────────────────────────────────────────────

def _load_diary() -> List[Dict]:
    if not TRADE_DIARY_FILE.exists():
        return []
    try:
        return json.loads(TRADE_DIARY_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"日記読み込みエラー: {e}")
        return []


def _save_diary(entries: List[Dict]):
    TRADE_DIARY_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ─── 日記エントリ生成 ─────────────────────────────────────

def write_diary_entry(trade: Dict) -> Optional[Dict]:
    """
    1件のトレード結果を受け取り、Claude が分析して日記エントリを保存する。
    API キーがない場合はルールベースの分析を行う。

    Args:
        trade: {ticker, label, entry_price, exit_price, pnl_pct, pnl,
                entry_date, exit_reason, shares}

    Returns:
        保存した日記エントリ（失敗時は None）
    """
    ticker   = trade.get("ticker", "")
    pnl_pct  = trade.get("pnl_pct", 0)
    outcome  = "勝ち" if pnl_pct > 0 else ("引き分け" if pnl_pct == 0 else "負け")

    try:
        if ANTHROPIC_API_KEY:
            analysis = _claude_analyze(trade)
        else:
            analysis = _rule_based_analyze(trade)

        entry = {
            "trade_id":    f"{ticker}_{trade.get('exit_date', datetime.now().strftime('%Y%m%d'))}",
            "ticker":      ticker,
            "label":       trade.get("label", ""),
            "entry_date":  trade.get("entry_date", ""),
            "exit_date":   trade.get("exit_date", datetime.now().strftime("%Y-%m-%d")),
            "entry_price": trade.get("entry_price", 0),
            "exit_price":  trade.get("exit_price", 0),
            "pnl_pct":     round(pnl_pct, 2),
            "pnl":         trade.get("pnl", 0),
            "exit_reason": trade.get("exit_reason", ""),
            "outcome":     outcome,
            "analysis":    analysis,
            "created_at":  datetime.now().isoformat(),
        }

        entries = _load_diary()
        # 同一 trade_id があれば上書き
        entries = [e for e in entries if e.get("trade_id") != entry["trade_id"]]
        entries.append(entry)
        _save_diary(entries)

        logger.info(
            f"日記記録: {ticker} {outcome} ({pnl_pct:+.2f}%) — "
            f"{analysis.get('summary', '')[:60]}"
        )
        return entry

    except Exception as e:
        logger.warning(f"日記記録エラー ({ticker}): {e}")
        return None


def _claude_analyze(trade: Dict) -> Dict:
    """Claude API でトレードを分析する。"""
    client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    ticker   = trade.get("ticker", "")
    label    = trade.get("label", ticker)
    pnl_pct  = trade.get("pnl_pct", 0)
    outcome  = "利益" if pnl_pct > 0 else "損失"

    prompt = f"""あなたは日本株の定量投資専門家です。
以下のトレード結果を分析し、日記エントリを作成してください。

## トレード情報
- 銘柄: {label}（{ticker}）
- 取得価格: ¥{trade.get('entry_price', 0):,.0f}
- 売却価格: ¥{trade.get('exit_price', 0):,.0f}
- 損益率: {pnl_pct:+.2f}%
- 損益額: ¥{trade.get('pnl', 0):+,.0f}
- 取得日: {trade.get('entry_date', '不明')}
- 売却日: {trade.get('exit_date', '不明')}
- 売却理由: {trade.get('exit_reason', '不明')}
- 結果: {outcome}

## タスク
以下の JSON 形式で分析を返してください。

DIARY_ANALYSIS_JSON:
{{
  "summary": "<1文で結果のまとめ>",
  "win_loss_reason": "<なぜ利益/損失になったか。市場環境・タイミング・銘柄特性など>",
  "what_went_well": "<うまくいった点（なければ空文字）>",
  "what_went_wrong": "<反省点・失敗した原因（なければ空文字）>",
  "lesson": "<次のトレードに活かすべき教訓を1文で>",
  "next_action": "<今後の取引で変えるべき具体的な行動>"
}}"""

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        return _parse_diary_json(text) or _rule_based_analyze(trade)
    except Exception as e:
        logger.warning(f"Claude 日記分析エラー: {e}")
        return _rule_based_analyze(trade)


def _parse_diary_json(text: str) -> Optional[Dict]:
    """DIARY_ANALYSIS_JSON ブロックを抽出してパースする。"""
    marker = "DIARY_ANALYSIS_JSON:"
    if marker not in text:
        return None
    start = text.find(marker) + len(marker)
    chunk = text[start:].strip()
    brace = chunk.find("{")
    if brace < 0:
        return None
    chunk = chunk[brace:]

    depth, end = 0, 0
    for i, ch in enumerate(chunk):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        return json.loads(chunk[:end])
    except json.JSONDecodeError:
        return None


def _rule_based_analyze(trade: Dict) -> Dict:
    """Claude API なしのルールベース分析（フォールバック）。"""
    pnl_pct     = trade.get("pnl_pct", 0)
    exit_reason = trade.get("exit_reason", "")
    ticker      = trade.get("ticker", "")

    if pnl_pct > 5:
        summary = f"{ticker} 大きな利益（+{pnl_pct:.1f}%）"
        reason  = "良好なモメンタムと出口タイミングにより大きな利益を実現"
        well    = "エントリーシグナルと出口条件が機能した"
        wrong   = ""
        lesson  = "同様のシグナルパターンを維持する"
    elif pnl_pct > 0:
        summary = f"{ticker} 小幅な利益（+{pnl_pct:.1f}%）"
        reason  = "モメンタムは機能したが上昇幅は限定的"
        well    = "損失を避けた"
        wrong   = "より大きな利益を取れた可能性がある"
        lesson  = "利確ラインの再検討を検討する"
    elif pnl_pct > -3:
        summary = f"{ticker} 小幅な損失（{pnl_pct:.1f}%）"
        reason  = "エントリー後に価格が下落した"
        well    = "損切りが機能して大きな損失を防いだ"
        wrong   = "エントリータイミングが早かった可能性"
        lesson  = "確認シグナルが揃ってからエントリーする"
    else:
        summary = f"{ticker} 大きな損失（{pnl_pct:.1f}%）"
        reason  = f"損切りライン到達（{exit_reason}）"
        well    = "損切りルールを守った"
        wrong   = "下降トレンド中のエントリーだった可能性"
        lesson  = "市場レジームが強気のときだけエントリーする"

    return {
        "summary":        summary,
        "win_loss_reason": reason,
        "what_went_well":  well,
        "what_went_wrong": wrong,
        "lesson":          lesson,
        "next_action":    "スコアリング閾値と市場レジーム判定を再確認する",
    }


# ─── 読み出し ──────────────────────────────────────────────

def get_diary() -> List[Dict]:
    """全日記エントリをリストで返す（新しい順）。"""
    entries = _load_diary()
    return sorted(entries, key=lambda e: e.get("created_at", ""), reverse=True)


def get_diary_context(n: int = 10) -> str:
    """
    直近 n 件の日記から学習コンテキスト文字列を生成する。
    Claude の改善プロンプトに挿入するために使う。
    """
    entries = get_diary()[:n]
    if not entries:
        return "（日記エントリなし）"

    lines = []
    for e in entries:
        outcome = "✓ 勝" if e.get("pnl_pct", 0) > 0 else "✗ 負"
        a = e.get("analysis", {})
        lines.append(
            f"[{e.get('exit_date','?')}] {e.get('ticker','')} {outcome} "
            f"({e.get('pnl_pct',0):+.1f}%) | "
            f"教訓: {a.get('lesson', '—')}"
        )
    return "\n".join(lines)
