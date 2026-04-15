"""
Claude AI 分析モジュール
バックテスト結果をClaude APIで分析し、良かった点・悪かった点を特定し、
次回投資戦略のパラメータ改善案を生成します。
"""
import json
import logging
from typing import Dict, List, Optional, Tuple

import anthropic
import pandas as pd

from trading_system.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, STRATEGY_PARAMS

logger = logging.getLogger(__name__)


def analyze_backtest_results(
    summary: Dict,
    trades: List[Dict],
    equity_curve: List[Dict],
    current_params: Dict,
    previous_analyses: Optional[List[Dict]] = None,
) -> Dict:
    """
    Claude APIを使ってバックテスト結果を分析する。

    Args:
        summary: ポートフォリオサマリー（リターン、シャープ比等）
        trades: 全取引履歴
        equity_curve: 日次資産推移
        current_params: 現在の戦略パラメータ
        previous_analyses: 過去の分析結果（フィードバックループ用）

    Returns:
        {
            "good_points": [...],
            "bad_points": [...],
            "improvement_suggestions": [...],
            "next_strategy_params": {...},
            "full_analysis": "..."
        }
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY が未設定のため、分析をスキップします")
        return _fallback_analysis(summary, trades, current_params)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # トレードデータを圧縮してプロンプトに含める
    trade_stats = _compute_trade_stats(trades)
    top_trades = _get_notable_trades(trades)

    prompt = _build_analysis_prompt(
        summary=summary,
        trade_stats=trade_stats,
        top_trades=top_trades,
        equity_curve=equity_curve,
        current_params=current_params,
        previous_analyses=previous_analyses or [],
    )

    logger.info("Claude APIで分析中...")

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system="""あなたは日本株の定量投資（クオンツ）の専門家アナリストです。
バックテスト結果を詳細に分析し、以下を提供してください：
1. 良かった点（戦略の強み）
2. 悪かった点（弱みと問題点）
3. 具体的な改善提案
4. 次回バックテスト用の最適化パラメータ（JSON形式）

分析は定量的・具体的に行い、感覚的な意見は避けてください。
必ず最後に「NEXT_PARAMS_JSON:」というラベルに続けてJSONを出力してください。""",
            messages=[{"role": "user", "content": prompt}],
        )

        full_text = response.content[0].text
        parsed = _parse_claude_response(full_text, current_params)
        parsed["full_analysis"] = full_text
        return parsed

    except anthropic.APIError as e:
        logger.error(f"Claude API エラー: {e}")
        return _fallback_analysis(summary, trades, current_params)


def _build_analysis_prompt(
    summary: Dict,
    trade_stats: Dict,
    top_trades: Dict,
    equity_curve: List[Dict],
    current_params: Dict,
    previous_analyses: List[Dict],
) -> str:
    """分析プロンプトを構築する。"""

    # 資産推移の要約
    if equity_curve:
        eq_df = pd.DataFrame(equity_curve)
        eq_summary = {
            "最初の資産": f"¥{eq_df['total_equity'].iloc[0]:,.0f}",
            "最後の資産": f"¥{eq_df['total_equity'].iloc[-1]:,.0f}",
            "最高資産": f"¥{eq_df['total_equity'].max():,.0f}",
            "最低資産": f"¥{eq_df['total_equity'].min():,.0f}",
        }
    else:
        eq_summary = {}

    # 過去の分析がある場合、フィードバックとして含める
    prev_feedback = ""
    if previous_analyses:
        last = previous_analyses[-1]
        prev_feedback = f"""
## 前回の分析結果（改善前）
### 前回の問題点:
{chr(10).join(f"- {p}" for p in last.get("bad_points", []))}

### 前回の改善提案:
{chr(10).join(f"- {s}" for s in last.get("improvement_suggestions", []))}

→ 上記改善を踏まえた今回結果を評価してください。
"""

    prompt = f"""
# バックテスト分析レポート

## 実行条件
- バックテスト期間: {summary.get('start_date', 'N/A')} 〜 {summary.get('end_date', 'N/A')}
- 初期資金: ¥{summary.get('initial_capital', 0):,.0f}
- 対象市場: 日本株（東証）

## 戦略パラメータ
```json
{json.dumps(current_params, indent=2, ensure_ascii=False)}
```

## パフォーマンス指標
| 指標 | 値 |
|------|-----|
| 最終資産 | ¥{summary.get('final_equity', 0):,.0f} |
| 総リターン | {summary.get('total_return_pct', 0):+.2f}% |
| シャープレシオ | {summary.get('sharpe_ratio', 0):.2f} |
| 最大ドローダウン | {summary.get('max_drawdown_pct', 0):.2f}% |
| 総取引数 | {summary.get('total_trades', 0)} |
| 勝率 | {summary.get('win_rate', 0):.1f}% |
| 平均勝ち | ¥{summary.get('avg_win', 0):,.0f} |
| 平均負け | ¥{summary.get('avg_loss', 0):,.0f} |
| プロフィットファクター | {summary.get('profit_factor', 0):.2f} |
| 平均保有日数 | {summary.get('avg_hold_days', 0):.1f}日 |

## 取引詳細統計
```json
{json.dumps(trade_stats, indent=2, ensure_ascii=False, default=str)}
```

## 特筆すべき取引
### 最良取引 Top3
```json
{json.dumps(top_trades.get('best', []), indent=2, ensure_ascii=False, default=str)}
```
### 最悪取引 Top3
```json
{json.dumps(top_trades.get('worst', []), indent=2, ensure_ascii=False, default=str)}
```

## 撤退理由の内訳
```json
{json.dumps(summary.get('exit_reasons', {}), indent=2, ensure_ascii=False)}
```

## 銘柄別損益
```json
{json.dumps(summary.get('trades_by_ticker', {}), indent=2, ensure_ascii=False)}
```

## 資産推移概要
```json
{json.dumps(eq_summary, indent=2, ensure_ascii=False)}
```

{prev_feedback}

---

上記データを分析し、以下の形式で回答してください：

## 良かった点（3〜5項目）
各項目を具体的な数字で説明してください。

## 悪かった点（3〜5項目）
各項目を具体的な数字で説明してください。

## 改善提案（3〜5項目）
次回バックテストで試すべき具体的な変更を提案してください。

## 総評
200字以内で総合評価を述べてください。

## 次回戦略パラメータ
NEXT_PARAMS_JSON:
{{
  "ema_fast": <整数>,
  "ema_slow": <整数>,
  "ema_trend": <整数>,
  "rsi_period": <整数>,
  "rsi_lower": <整数>,
  "rsi_upper": <整数>,
  "macd_fast": <整数>,
  "macd_slow": <整数>,
  "macd_signal": <整数>,
  "momentum_period": <整数>,
  "momentum_skip": <整数>,
  "top_n_momentum": <整数>,
  "volume_ma_period": <整数>,
  "volume_multiplier": <実数>
}}
"""
    return prompt


def _compute_trade_stats(trades: List[Dict]) -> Dict:
    """取引履歴から統計量を計算する。"""
    if not trades:
        return {}

    df = pd.DataFrame(trades)

    winning = df[df["pnl"] > 0]
    losing = df[df["pnl"] <= 0]

    stats = {
        "合計取引数": len(df),
        "勝ち取引数": len(winning),
        "負け取引数": len(losing),
        "最大勝ちPnL": f"¥{df['pnl'].max():,.0f}",
        "最大負けPnL": f"¥{df['pnl'].min():,.0f}",
        "中央値PnL": f"¥{df['pnl'].median():,.0f}",
        "平均保有日数（勝ち）": f"{winning['hold_days'].mean():.1f}日" if len(winning) > 0 else "N/A",
        "平均保有日数（負け）": f"{losing['hold_days'].mean():.1f}日" if len(losing) > 0 else "N/A",
        "損切り発動回数": len(df[df["exit_reason"].str.contains("損切り", na=False)]),
        "利確発動回数": len(df[df["exit_reason"].str.contains("利確", na=False)]),
        "トレーリングストップ発動回数": len(df[df["exit_reason"].str.contains("トレーリング", na=False)]),
        "デッドクロス発動回数": len(df[df["exit_reason"].str.contains("デッドクロス", na=False)]),
    }
    return stats


def _get_notable_trades(trades: List[Dict], top_n: int = 3) -> Dict:
    """特筆すべき取引（上位・下位N件）を返す。"""
    if not trades:
        return {"best": [], "worst": []}

    df = pd.DataFrame(trades)
    fields = ["ticker", "entry_date", "exit_date", "entry_price", "exit_price",
              "pnl", "pnl_pct", "hold_days", "exit_reason"]

    best = df.nlargest(top_n, "pnl")[fields].to_dict(orient="records")
    worst = df.nsmallest(top_n, "pnl")[fields].to_dict(orient="records")
    return {"best": best, "worst": worst}


def _parse_claude_response(text: str, fallback_params: Dict) -> Dict:
    """Claudeのレスポンスから構造化データを抽出する。"""
    result = {
        "good_points": [],
        "bad_points": [],
        "improvement_suggestions": [],
        "next_strategy_params": fallback_params.copy(),
    }

    # 良かった点を抽出
    result["good_points"] = _extract_section(text, "良かった点")
    result["bad_points"] = _extract_section(text, "悪かった点")
    result["improvement_suggestions"] = _extract_section(text, "改善提案")

    # NEXT_PARAMS_JSONを抽出
    if "NEXT_PARAMS_JSON:" in text:
        json_start = text.find("NEXT_PARAMS_JSON:") + len("NEXT_PARAMS_JSON:")
        json_text = text[json_start:].strip()
        # JSON部分を特定
        brace_start = json_text.find("{")
        if brace_start >= 0:
            json_text = json_text[brace_start:]
            # 対応する閉じ括弧を探す
            depth = 0
            end_idx = 0
            for i, ch in enumerate(json_text):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end_idx = i + 1
                        break
            json_text = json_text[:end_idx]
            try:
                parsed_params = json.loads(json_text)
                # 数値型を保証
                for k, v in parsed_params.items():
                    if k in ["volume_multiplier"]:
                        parsed_params[k] = float(v)
                    else:
                        parsed_params[k] = int(v)
                result["next_strategy_params"] = parsed_params
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"パラメータJSONのパース失敗: {e}. フォールバックを使用。")

    return result


def _extract_section(text: str, section_name: str) -> List[str]:
    """マークダウンセクションから箇条書き項目を抽出する。"""
    items = []
    in_section = False

    for line in text.split("\n"):
        if section_name in line and line.startswith("#"):
            in_section = True
            continue
        if in_section:
            if line.startswith("#") and section_name not in line:
                break
            line = line.strip()
            if line.startswith(("-", "*", "•", "・")) or (len(line) > 2 and line[0].isdigit() and line[1] == "."):
                cleaned = line.lstrip("-*•・0123456789. ").strip()
                if cleaned:
                    items.append(cleaned)

    return items


def _fallback_analysis(summary: Dict, trades: List[Dict], current_params: Dict) -> Dict:
    """Claude APIが使えない場合のルールベース分析。"""
    good_points = []
    bad_points = []
    suggestions = []

    total_return = summary.get("total_return_pct", 0)
    win_rate = summary.get("win_rate", 0)
    sharpe = summary.get("sharpe_ratio", 0)
    max_dd = summary.get("max_drawdown_pct", 0)
    pf = summary.get("profit_factor", 0)

    # 良かった点の自動判定
    if total_return > 10:
        good_points.append(f"総リターン {total_return:.1f}% は市場平均を上回る水準")
    if win_rate > 55:
        good_points.append(f"勝率 {win_rate:.1f}% は高水準（50%超が目標）")
    if sharpe > 1.0:
        good_points.append(f"シャープレシオ {sharpe:.2f} はリスク調整後リターンが良好")
    if pf > 1.5:
        good_points.append(f"プロフィットファクター {pf:.2f} は安定した収益性を示す")
    if not good_points:
        good_points.append("リスク管理（損切り・トレーリングストップ）が機能した")

    # 悪かった点の自動判定
    if total_return < 0:
        bad_points.append(f"総リターン {total_return:.1f}% がマイナス。戦略の根本的見直しが必要")
    if win_rate < 45:
        bad_points.append(f"勝率 {win_rate:.1f}% が低い。エントリー条件の絞り込みが必要")
    if max_dd < -20:
        bad_points.append(f"最大ドローダウン {max_dd:.1f}% が大きい。リスク管理強化が必要")
    if sharpe < 0.5:
        bad_points.append(f"シャープレシオ {sharpe:.2f} が低い。ボラティリティに対するリターンが不十分")
    if not bad_points:
        bad_points.append("取引回数が少なく、統計的有意性の確認が必要")

    # 改善提案の自動生成
    next_params = current_params.copy()

    if win_rate < 50:
        suggestions.append("RSIフィルタを強化（下限を45に引き上げ、上限を65に引き下げ）")
        next_params["rsi_lower"] = min(50, current_params.get("rsi_lower", 40) + 5)
        next_params["rsi_upper"] = max(65, current_params.get("rsi_upper", 70) - 5)

    if max_dd < -15:
        suggestions.append("損切りラインを5%に狭める（現在7%）")

    if total_return < 5:
        suggestions.append("モメンタム期間を短縮（252日→180日）して短期トレンドへの感度を高める")
        next_params["momentum_period"] = 180

    if not suggestions:
        suggestions.append("現行パラメータは安定しているため、市場環境別のパラメータセットを検討する")

    full_text = f"""
## ルールベース分析（Claude API 未接続）

### 良かった点
{chr(10).join(f"- {p}" for p in good_points)}

### 悪かった点
{chr(10).join(f"- {p}" for p in bad_points)}

### 改善提案
{chr(10).join(f"- {s}" for s in suggestions)}
"""

    return {
        "good_points": good_points,
        "bad_points": bad_points,
        "improvement_suggestions": suggestions,
        "next_strategy_params": next_params,
        "full_analysis": full_text,
    }


def explain_trade_signal(signal: Dict, signal_type: str, market: str = "JP") -> str:
    """
    売買シグナル1件について Claude が日本語の解説文を生成する。

    Args:
        signal: factor_scorer / portfolio_state が生成したシグナル辞書
        signal_type: "buy" または "sell"
        market: "JP" または "US"

    Returns:
        1〜3文の解説文（例: "EMAゴールデンクロスが発生し..."）
    """
    if not ANTHROPIC_API_KEY:
        # APIキーなし → ルールベースで簡易説明
        if signal_type == "sell":
            return signal.get("reason", "売却条件に達しました")
        reasons = signal.get("reasons", [])
        score   = signal.get("score", 0)
        m6      = signal.get("momentum_6m_pct", 0)
        return f"スコア{score}点。6ヶ月リターン{m6:+.1f}%。{'; '.join(reasons[:3])}"

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    ticker = signal.get("ticker", "")
    from trading_system.factor_scorer import get_ticker_label
    label  = get_ticker_label(ticker)

    if signal_type == "buy":
        details  = signal.get("details", {})
        reasons  = signal.get("reasons", [])
        score    = signal.get("score", 0)
        m6       = signal.get("momentum_6m_pct", 0)
        price    = details.get("price", 0)
        rsi      = details.get("rsi", 0)
        ema9     = details.get("ema9", 0)
        ema26    = details.get("ema26", 0)
        currency = "¥" if market == "JP" else "$"

        prompt = f"""以下の買いシグナルについて、投資家向けに日本語で2文（60〜100字程度）で説明してください。
なぜ今この銘柄を買うべきか、定量的な根拠を含めてください。

銘柄: {ticker}（{label}）
現在値: {currency}{price:,.0f}
スコア: {score}/100点
6ヶ月モメンタム: {m6:+.1f}%
RSI: {rsi}
EMA9/EMA26: {ema9:,.0f}/{ema26:,.0f}
判定理由: {', '.join(reasons)}

出力は説明文のみ（箇条書き不要、マークダウン不要）。"""

    else:  # sell
        pnl     = signal.get("pnl_pct", 0)
        reason  = signal.get("reason", "")
        prompt = f"""以下の売りシグナルについて、投資家向けに日本語で1〜2文（40〜80字程度）で説明してください。

銘柄: {ticker}（{label}）
売却理由: {reason}
損益: {pnl:+.1f}%

出力は説明文のみ（箇条書き不要）。"""

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.warning(f"explain_trade_signal エラー: {e}")
        return signal.get("reason", "") or "; ".join(signal.get("reasons", [])[:2])


def print_analysis(analysis: Dict) -> None:
    """分析結果をコンソールに表示する。"""
    print("\n" + "=" * 60)
    print("  Claude AI 分析レポート")
    print("=" * 60)

    print("\n[良かった点]")
    for i, p in enumerate(analysis.get("good_points", []), 1):
        print(f"  {i}. {p}")

    print("\n[悪かった点]")
    for i, p in enumerate(analysis.get("bad_points", []), 1):
        print(f"  {i}. {p}")

    print("\n[改善提案]")
    for i, s in enumerate(analysis.get("improvement_suggestions", []), 1):
        print(f"  {i}. {s}")

    print("\n[次回推奨パラメータ]")
    params = analysis.get("next_strategy_params", {})
    for k, v in params.items():
        print(f"  {k}: {v}")

    print("=" * 60)
