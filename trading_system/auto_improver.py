"""
自動改善サイクル

実際のトレード結果を蓄積し、定期的にClaudeが分析して
スコアリングパラメータを自動更新する。

改善サイクル:
  1. 売却のたびに real_trades.json へ記録（order_executor が呼び出す）
  2. 引け後（15:30）に run_improvement_cycle() を実行
  3. 直近トレードが MIN_TRADES 件以上あれば Claude が分析
  4. スコアリング閾値（BUY_THRESHOLD 等）を scoring_config.json に保存
  5. 次回のシグナル生成から新しい設定が反映される
"""
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

import anthropic

from trading_system.config import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL, DATA_DIR
)

logger = logging.getLogger(__name__)

REAL_TRADES_FILE  = DATA_DIR / "real_trades.json"
SCORING_CONFIG    = DATA_DIR / "scoring_config.json"
MIN_TRADES        = 5    # 改善分析に必要な最低トレード数
ANALYSIS_WINDOW   = 30   # 分析に使う直近トレード数

# デフォルトのスコアリング設定
DEFAULT_SCORING = {
    "buy_threshold":       70,    # 買いシグナル発生スコア閾値（0-100）
    "momentum_weight":     30,    # モメンタムファクター最大点
    "trend_weight":        20,    # トレンドファクター最大点
    "ema_cross_weight":    20,    # EMAクロスファクター最大点
    "volume_weight":       15,    # 出来高ファクター最大点
    "rsi_weight":          15,    # RSIファクター最大点
    "rsi_lower":           30,    # RSI買いシグナル下限（これ以上でOK）
    "rsi_upper":           75,    # RSI買いシグナル上限（これ以下でOK）
    "updated_at":          None,
    "update_reason":       "初期設定",
}


# ─── 実トレード記録 ───────────────────────────────────────────

def log_real_trade(trade: Dict):
    """1件のトレード結果をreal_trades.jsonに追記し、日記エントリを生成する。"""
    trades = _load_real_trades()
    trades.append({
        **trade,
        "logged_at": datetime.now().isoformat(),
    })
    REAL_TRADES_FILE.write_text(
        json.dumps(trades, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        f"実トレード記録: {trade.get('ticker')} "
        f"P&L {trade.get('pnl_pct', 0):+.2f}% "
        f"({len(trades)}件目)"
    )

    # 日記エントリを非同期で生成（失敗してもトレード記録は保護する）
    try:
        from trading_system.trade_diary import write_diary_entry
        write_diary_entry(trade)
    except Exception as e:
        logger.warning(f"日記記録エラー: {e}")


def _load_real_trades() -> List[Dict]:
    if not REAL_TRADES_FILE.exists():
        return []
    try:
        return json.loads(REAL_TRADES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"実トレード読み込みエラー: {e}")
        return []


# ─── スコアリング設定の読み書き ──────────────────────────────

def load_scoring_config() -> Dict:
    """現在のスコアリング設定を返す。ファイルがなければデフォルトを返す。"""
    if SCORING_CONFIG.exists():
        try:
            cfg = json.loads(SCORING_CONFIG.read_text(encoding="utf-8"))
            # デフォルト値で欠損キーを補完
            merged = DEFAULT_SCORING.copy()
            merged.update(cfg)
            return merged
        except Exception as e:
            logger.warning(f"スコアリング設定読み込みエラー: {e}")
    return DEFAULT_SCORING.copy()


def _save_scoring_config(cfg: Dict):
    SCORING_CONFIG.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"スコアリング設定を更新: BUY_THRESHOLD={cfg.get('buy_threshold')}, RSI={cfg.get('rsi_lower')}-{cfg.get('rsi_upper')}")


# ─── 改善サイクル ────────────────────────────────────────────

def run_improvement_cycle():
    """
    引け後に呼ばれる改善サイクル。
    直近トレードを分析し、スコアリング設定を更新する。
    """
    trades = _load_real_trades()
    if len(trades) < MIN_TRADES:
        logger.info(
            f"改善分析スキップ: 実トレード {len(trades)}件 "
            f"（最低 {MIN_TRADES}件 必要）"
        )
        return

    recent   = trades[-ANALYSIS_WINDOW:]
    wins     = [t for t in recent if t.get("pnl_pct", 0) > 0]
    win_rate = len(wins) / len(recent) * 100
    avg_pnl  = sum(t.get("pnl_pct", 0) for t in recent) / len(recent)
    total_pnl = sum(t.get("pnl", 0) for t in recent)

    logger.info(
        f"改善分析開始: 直近{len(recent)}件 — "
        f"勝率 {win_rate:.1f}% / 平均損益 {avg_pnl:+.2f}% / 合計 ¥{total_pnl:+,.0f}"
    )

    current_cfg = load_scoring_config()

    if ANTHROPIC_API_KEY:
        new_cfg = _claude_improve(recent, current_cfg, win_rate, avg_pnl)
    else:
        new_cfg = _rule_based_improve(current_cfg, win_rate, avg_pnl)

    if new_cfg:
        new_cfg["updated_at"] = datetime.now().isoformat()
        _save_scoring_config(new_cfg)
    else:
        logger.info("パラメータ変更なし（現状維持）")


def _claude_improve(
    recent: List[Dict],
    current_cfg: Dict,
    win_rate: float,
    avg_pnl: float,
) -> Optional[Dict]:
    """Claude APIを使ってスコアリング設定を改善する。"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 損失が大きいトレードとその特徴を抽出
    worst = sorted(recent, key=lambda t: t.get("pnl_pct", 0))[:5]
    best  = sorted(recent, key=lambda t: t.get("pnl_pct", 0), reverse=True)[:5]

    # 日記から蓄積された教訓を取得
    try:
        from trading_system.trade_diary import get_diary_context
        diary_context = get_diary_context(n=10)
    except Exception:
        diary_context = "（日記なし）"

    prompt = f"""あなたは日本株の定量投資専門家です。
直近 {len(recent)} 件の実トレード結果と蓄積された日記を分析し、スコアリング閾値の最適化案を提示してください。

## 現在の設定
```json
{json.dumps({k: v for k, v in current_cfg.items() if k != 'updated_at'}, ensure_ascii=False, indent=2)}
```

## 直近トレード実績
- 勝率: {win_rate:.1f}%
- 平均損益: {avg_pnl:+.2f}%
- 合計損益: ¥{sum(t.get('pnl', 0) for t in recent):+,.0f}

## 最良トレード上位5件
```json
{json.dumps([{k: t[k] for k in ['ticker','pnl_pct','exit_reason'] if k in t} for t in best], ensure_ascii=False)}
```

## 最悪トレード上位5件
```json
{json.dumps([{k: t[k] for k in ['ticker','pnl_pct','exit_reason'] if k in t} for t in worst], ensure_ascii=False)}
```

## トレード日記（過去10件の教訓）
{diary_context}

## タスク
上記のデータと日記の教訓を踏まえ、以下のパラメータの最適値をJSON形式で回答してください。
変更が不要な場合は現在値をそのまま返してください。

SCORING_UPDATE_JSON:
{{
  "buy_threshold": <整数 60-85>,
  "rsi_lower": <整数 25-45>,
  "rsi_upper": <整数 65-80>,
  "update_reason": "<変更理由を1文で（日記の教訓を反映してください）>"
}}"""

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        return _parse_scoring_update(text, current_cfg)
    except Exception as e:
        logger.warning(f"Claude改善分析エラー: {e}")
        return _rule_based_improve(current_cfg, win_rate, avg_pnl)


def _parse_scoring_update(text: str, fallback: Dict) -> Optional[Dict]:
    """ClaudeのレスポンスからSCORING_UPDATE_JSONを抽出する。"""
    marker = "SCORING_UPDATE_JSON:"
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
        obj = json.loads(chunk[:end])
        cfg = fallback.copy()
        cfg["buy_threshold"]   = int(obj.get("buy_threshold",   cfg["buy_threshold"]))
        cfg["rsi_lower"]       = int(obj.get("rsi_lower",       cfg["rsi_lower"]))
        cfg["rsi_upper"]       = int(obj.get("rsi_upper",       cfg["rsi_upper"]))
        cfg["update_reason"]   = str(obj.get("update_reason",   "Claude分析による更新"))
        return cfg
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"SCORING_UPDATE_JSON パースエラー: {e}")
        return None


def _rule_based_improve(
    current_cfg: Dict,
    win_rate:    float,
    avg_pnl:     float,
) -> Optional[Dict]:
    """ルールベースのシンプルな改善（Claude API不使用時のフォールバック）。"""
    cfg = current_cfg.copy()
    changed = False

    # 勝率が低い → 閾値を上げてより厳選
    if win_rate < 45 and cfg["buy_threshold"] < 80:
        cfg["buy_threshold"] += 5
        cfg["update_reason"]  = f"勝率{win_rate:.0f}%低下により閾値を引き上げ"
        changed = True

    # 勝率が高く余裕がある → 閾値を下げてシグナル数を増やす
    elif win_rate > 65 and avg_pnl > 3 and cfg["buy_threshold"] > 65:
        cfg["buy_threshold"] -= 3
        cfg["update_reason"]  = f"勝率{win_rate:.0f}%好調により閾値を緩和"
        changed = True

    # 平均損益がマイナス → RSIフィルタを強化
    if avg_pnl < -1.0 and cfg["rsi_lower"] < 40:
        cfg["rsi_lower"]    += 5
        cfg["update_reason"]  = f"平均損益{avg_pnl:.1f}%のためRSI下限を引き上げ"
        changed = True

    return cfg if changed else None
