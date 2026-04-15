"""
LINE Notify 通知モジュール
LINE Notify API を使ってスマートフォンにトレードシグナルを送信する。

セットアップ:
  1. https://notify-bot.line.me/my/ でトークンを発行
  2. Render の環境変数に LINE_NOTIFY_TOKEN=<token> を設定
"""
import logging
import requests
from typing import Optional

from trading_system.config import LINE_NOTIFY_TOKEN

logger = logging.getLogger(__name__)

LINE_NOTIFY_URL = "https://notify-api.line.me/api/notify"


def send_line_message(message: str, token: Optional[str] = None) -> bool:
    """
    LINE Notify にメッセージを送信する。

    Args:
        message: 送信テキスト（最大 1000 文字）
        token: 上書きトークン（省略時は環境変数を使用）

    Returns:
        成功した場合 True
    """
    t = token or LINE_NOTIFY_TOKEN
    if not t:
        logger.warning("LINE_NOTIFY_TOKEN が未設定のため通知をスキップします")
        return False

    # LINE Notify は 1000 文字制限
    if len(message) > 1000:
        message = message[:997] + "..."

    headers = {"Authorization": f"Bearer {t}"}
    data    = {"message": message}

    try:
        res = requests.post(LINE_NOTIFY_URL, headers=headers, data=data, timeout=15)
        if res.status_code == 200:
            logger.info("LINE Notify 送信成功")
            return True
        else:
            logger.error(f"LINE Notify 送信失敗: {res.status_code} {res.text}")
            return False
    except requests.RequestException as e:
        logger.error(f"LINE Notify 通信エラー: {e}")
        return False


def send_signal_notification(
    market: str,
    buy_signals: list,
    sell_signals: list,
    market_bullish: bool,
    n_positions: int,
    total_capital: int,
    position_size: int,
) -> bool:
    """
    トレードシグナルを LINE に整形して送信する。

    Args:
        market: "JP" または "US"
        buy_signals: 買いシグナルリスト
        sell_signals: 売りシグナルリスト
        market_bullish: 市場レジームが強気か
        n_positions: 現在の保有銘柄数
        total_capital: 運用総額（円）
        position_size: 1銘柄あたり投資額（円）
    """
    from datetime import datetime
    from trading_system.factor_scorer import get_ticker_label

    now   = datetime.now().strftime("%m/%d %H:%M")
    flag  = "🇯🇵" if market == "JP" else "🇺🇸"
    mname = "日本株" if market == "JP" else "米国株"

    lines = [
        f"\n{flag} {mname}シグナル {now}",
        f"市場: {'📈 強気' if market_bullish else '📉 弱気（新規買い抑制中）'}",
    ]

    # 売りシグナル
    if sell_signals:
        lines.append(f"\n📉 売りシグナル（{len(sell_signals)}件）")
        for s in sell_signals:
            label  = get_ticker_label(s["ticker"])
            pnl    = s.get("pnl_pct", 0)
            sign   = "+" if pnl >= 0 else ""
            lines.append(
                f"  {s['ticker']} {label}\n"
                f"  損益: {sign}{pnl:.1f}% / {s.get('reason', '')}"
            )

    # 買いシグナル
    if buy_signals:
        lines.append(f"\n📈 買いシグナル（{len(buy_signals)}件）")
        for s in buy_signals:
            label = get_ticker_label(s["ticker"])
            price = s.get("details", {}).get("price", 0)
            score = s.get("score", 0)

            # 株数計算
            shares_str = ""
            if price > 0:
                shares = position_size / price
                currency = "株" if market == "JP" else "株（端数あり）"
                shares_str = f"  投資: ¥{position_size:,}（{shares:.1f}{currency}）\n"

            explanation = s.get("explanation", "")
            lines.append(
                f"  {s['ticker']} {label}（スコア: {score}点）\n"
                f"  現在値: {'¥' if market == 'JP' else '$'}{price:,.0f}\n"
                f"{shares_str}"
                f"  {explanation}"
            )
    elif market_bullish:
        lines.append("\n📊 買いシグナルなし（本日は見送り）")

    # ポートフォリオ状態
    cash = total_capital - n_positions * position_size
    lines.append(
        f"\n💼 保有: {n_positions}銘柄 / 待機資金: ¥{cash:,}"
    )

    message = "\n".join(lines)
    return send_line_message(message)
