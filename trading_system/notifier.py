"""
LINE Messaging API 通知モジュール

トレード実行・損切り・改善分析完了などのイベントを
LINE にプッシュ通知する。

環境変数:
  LINE_CHANNEL_ACCESS_TOKEN  チャンネルアクセストークン（長期）
  LINE_USER_ID               通知先のユーザーID（U から始まる文字列）

いずれかが未設定の場合は通知をスキップする（取引ロジックには影響なし）。
"""
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

LINE_API_URL = "https://api.line.me/v2/bot/message/push"
_TIMEOUT     = 10  # 秒


def _token() -> str:
    return os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")


def _user_id() -> str:
    return os.getenv("LINE_USER_ID", "")


def _is_configured() -> bool:
    return bool(_token() and _user_id())


def send(message: str) -> bool:
    """
    LINE にテキストメッセージを送信する。

    Returns:
        True: 送信成功 / False: スキップまたは失敗
    """
    if not _is_configured():
        return False

    try:
        resp = requests.post(
            LINE_API_URL,
            headers={
                "Authorization": f"Bearer {_token()}",
                "Content-Type": "application/json",
            },
            json={
                "to": _user_id(),
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
