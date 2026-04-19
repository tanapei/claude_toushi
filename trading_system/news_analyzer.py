"""
Claude API を使ってニュースを分析し、株価への影響サマリーを生成するモジュール。
"""
import json
import logging
from pathlib import Path
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

_SETTINGS_FILE = Path(__file__).parent.parent / "settings.json"


def _get_api_key() -> str:
    """settings.json → 環境変数の順でAnthropic APIキーを取得する。"""
    import os
    try:
        if _SETTINGS_FILE.exists():
            s = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            key = s.get("anthropic_api_key", "")
            if key:
                return key
    except Exception:
        pass
    return os.getenv("ANTHROPIC_API_KEY", "")


def _build_ticker_table(universe: List[str]) -> str:
    """ティッカー → 銘柄名の対応表を文字列化する（プロンプト埋め込み用）。"""
    from trading_system.factor_scorer import get_ticker_label
    rows = [f"{t}:{get_ticker_label(t)}" for t in universe if t.endswith(".T")]
    return ", ".join(rows)


def analyze_news_impact(
    news_items: List[Dict],
    universe: Optional[List[str]] = None,
) -> str:
    """
    ニュースリストを Claude Haiku で分析し、LINE通知用のサマリーテキストを返す。

    Args:
        news_items: news_fetcher.fetch_recent_news() の結果
        universe:   対象銘柄リスト（省略時は VALID_UNIVERSE を使用）

    Returns:
        分析サマリーテキスト（最大1500文字）。APIキー未設定時は空文字。
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("[news_analyzer] Anthropic APIキーが未設定のためスキップ")
        return ""

    if not news_items:
        return ""

    if universe is None:
        from trading_system.config import VALID_UNIVERSE
        universe = VALID_UNIVERSE

    # ニュースをプロンプト用テキストに整形（最大20件）
    news_text = "\n".join(
        f"[{n['source']} {n['published']}] {n['title']}"
        + (f"\n  {n['summary'][:120]}" if n.get("summary") else "")
        for n in news_items[:20]
    )

    ticker_table = _build_ticker_table(universe)

    prompt = f"""あなたは日本株の専門アナリストです。
以下の本日のニュースを読んで、株価への影響をまとめてください。

【本日のニュース】
{news_text}

【対象銘柄】
{ticker_table}

以下の形式で、LINE通知用に簡潔（合計600文字以内）にまとめてください：

■ 本日の注目ニュース（最大3件・各1行）
・<ニュース概要>

■ 影響を受けそうな銘柄（最大5件）
・<銘柄名>（<ティッカー>）：<上昇/下落/不透明> — <理由を1行で>

■ 総評（1〜2行）
<全体の市場への影響コメント>

注意：
- 影響が明確な銘柄のみ挙げてください（無理に5件にする必要はありません）
- 根拠のある分析のみ記載してください
"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        logger.info(f"[news_analyzer] 分析完了 ({len(text)}文字)")
        return text[:1500]  # LINEの文字制限に余裕を持って切り詰め
    except Exception as e:
        logger.error(f"[news_analyzer] Claude API エラー: {e}")
        return ""
