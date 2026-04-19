"""
LINE Messaging API 通知モジュール

トレード実行・損切り・改善分析完了などのイベントを
LINE にプッシュ通知する。

環境変数:
  LINE_CHANNEL_ACCESS_TOKEN  チャンネルアクセストークン（長期）
  LINE_USER_ID               通知先のユーザーID（U から始まる文字列）

いずれかが未設定の場合は通知をスキップする（取引ロジックには影響なし）。
"""
import json
import logging
import os
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

LINE_API_URL = "https://api.line.me/v2/bot/message/push"
_TIMEOUT     = 10  # 秒

_SETTINGS_FILE = Path(__file__).parent.parent / "settings.json"


def _load_line_credentials() -> tuple[str, str]:
    """LINE認証情報を settings.json → 環境変数の優先順で取得する。"""
    token   = ""
    user_id = ""
    try:
        if _SETTINGS_FILE.exists():
            s       = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            token   = s.get("line_channel_token", "")
            user_id = s.get("line_user_id", "")
    except Exception:
        pass
    return (
        token   or os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""),
        user_id or os.getenv("LINE_USER_ID", ""),
    )


def _token() -> str:
    return _load_line_credentials()[0]


def _user_id() -> str:
    return _load_line_credentials()[1]


def _is_configured() -> bool:
    t, u = _load_line_credentials()
    return bool(t and u)


def send(message: str) -> bool:
    """
    LINE にテキストメッセージを送信する。

    Returns:
        True: 送信成功 / False: スキップまたは失敗
    """
    token, user_id = _load_line_credentials()
    if not (token and user_id):
        return False

    try:
        resp = requests.post(
            LINE_API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "to": user_id,
                "messages": [{"type": "text", "text": message}],
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            logger.debug(f"LINE 通知送信: {message[:40]}...")
            return True
        else:
            logger.warning(f"LINE 通知失敗 ({resp.status_code}): {resp.text}")
            return False
    except Exception as e:
        logger.warning(f"LINE 通知エラー: {e}")
        return False


# ─── 通知テンプレート ───────────────────────────────────────

def notify_buy(ticker: str, label: str, qty: int, price: float, invested: int,
               fill_confirmed: bool = True) -> None:
    conf = "（約定確認済）" if fill_confirmed else "（約定未確認）"
    send(
        f"✅ 買い約定 {conf}\n"
        f"銘柄: {label}（{ticker}）\n"
        f"数量: {qty}株 @ ¥{price:,.0f}\n"
        f"投資額: ¥{invested:,}"
    )


def notify_sell(ticker: str, label: str, qty: int, price: float,
                pnl: int, pnl_pct: float, reason: str,
                fill_confirmed: bool = True) -> None:
    icon = "📈" if pnl >= 0 else "📉"
    sign = "+" if pnl >= 0 else ""
    conf = "（約定確認済）" if fill_confirmed else "（約定未確認）"
    send(
        f"{icon} 売却完了 {conf}\n"
        f"銘柄: {label}（{ticker}）\n"
        f"数量: {qty}株 @ ¥{price:,.0f}\n"
        f"損益: {sign}¥{abs(pnl):,}（{sign}{pnl_pct:.2f}%）\n"
        f"理由: {reason}"
    )


def notify_stop(ticker: str, label: str, pnl_pct: float, reason: str) -> None:
    send(
        f"⚠️ 自動売却トリガー\n"
        f"銘柄: {label}（{ticker}）\n"
        f"損益: {pnl_pct:+.2f}%\n"
        f"理由: {reason}"
    )


def notify_improvement(win_rate: float, avg_pnl: float,
                        update_reason: str, changed: bool) -> None:
    if changed:
        send(
            f"🤖 スコアリング自動更新\n"
            f"勝率: {win_rate:.1f}% / 平均損益: {avg_pnl:+.2f}%\n"
            f"変更内容: {update_reason}"
        )
    else:
        send(
            f"🤖 引け後の改善分析完了\n"
            f"勝率: {win_rate:.1f}% / 平均損益: {avg_pnl:+.2f}%\n"
            f"パラメータ変更なし（現状維持）"
        )


def notify_error(context: str, error: str) -> None:
    send(
        f"🚨 エラー発生\n"
        f"場所: {context}\n"
        f"内容: {error[:100]}"
    )


def notify_news_summary(analysis_text: str, news_count: int, mode: str = "paper") -> None:
    """朝のニュース分析サマリーをLINEに通知する。"""
    from datetime import datetime
    import pytz
    now_str = datetime.now(pytz.timezone("Asia/Tokyo")).strftime("%m/%d %H:%M")
    mode_str = "📄 ペーパー" if mode != "live" else "💴 ライブ"

    header = (
        f"📰 朝の市場ニュース分析 [{now_str}]\n"
        f"参照ニュース: {news_count}件\n"
        "━━━━━━━━━━━━━━\n"
    )
    footer = f"\n━━━━━━━━━━━━━━\nモード: {mode_str}トレード"

    message = header + analysis_text + footer
    send(message[:4900])  # LINE上限5000文字に余裕を持たせる


def notify_morning_signal(
    top_scored: list,
    market_bullish: bool,
    mode: str = "paper",
    top_n: int = 5,
) -> None:
    """朝の注目銘柄をLINEに通知する。

    Args:
        top_scored: score_universe() の結果リスト（スコア降順）
        market_bullish: 市場レジーム判定
        mode: "paper" or "live"
        top_n: 通知する上位銘柄数
    """
    from datetime import datetime
    import pytz
    now_str = datetime.now(pytz.timezone("Asia/Tokyo")).strftime("%m/%d %H:%M")

    regime = "📈 強気（新規買い有効）" if market_bullish else "📉 弱気（新規買い抑制）"
    mode_str = "📄 ペーパー" if mode != "live" else "💴 ライブ"

    lines = [
        f"📊 本日の注目銘柄 [{now_str}]",
        f"市場レジーム: {regime}",
        "━━━━━━━━━━━━━━",
    ]

    targets = [s for s in top_scored if s.get("score", 0) > 0][:top_n]
    if not targets:
        lines.append("（スコア対象銘柄なし）")
    else:
        sig_labels = {"buy": "🟢買い", "watch": "👀監視", "none": "　—　"}
        for i, s in enumerate(targets, 1):
            label = s.get("label") or s.get("ticker", "")
            score = s.get("score", 0)
            mom   = s.get("momentum_6m_pct", 0)
            sig   = sig_labels.get(s.get("signal", "none"), "　—　")
            mom_str = f"{mom:+.1f}%" if mom else "—"
            lines.append(
                f"{i}. {label}（{s['ticker']}）\n"
                f"   スコア {score}点 / 6M {mom_str} / {sig}"
            )

    lines += [
        "━━━━━━━━━━━━━━",
        f"モード: {mode_str}トレード",
    ]

    send("\n".join(lines))
