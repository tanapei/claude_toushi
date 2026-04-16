"""
kabuステーション® API クライアント

kabuステーション®アプリが起動・ログイン済みであること、
APIパスワードが設定済みであることが前提です。

ベースURL: http://localhost:18080/kabusapi
"""
import logging
import time
from typing import Dict, List, Optional

import requests

from trading_system.config import KABU_API_BASE_URL, KABU_API_PASSWORD

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10  # 秒


class KabuAPIError(Exception):
    pass


class KabuClient:
    """kabuステーション® REST API ラッパー。"""

    def __init__(self):
        self._cached_token: Optional[str] = None
        self._token_fetched_at: float = 0

    # ─── 認証 ────────────────────────────────────────────────

    def _refresh_token(self) -> str:
        """APIトークンを取得する（有効期限: 当日中）。"""
        resp = requests.post(
            f"{KABU_API_BASE_URL}/token",
            json={"APIPassword": KABU_API_PASSWORD},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            raise KabuAPIError(
                f"トークン取得失敗 ({resp.status_code}): {resp.text}\n"
                "kabuステーション®が起動・ログイン済みで、APIパスワードが正しいか確認してください"
            )
        token = resp.json().get("Token")
        if not token:
            raise KabuAPIError(f"トークンが空: {resp.json()}")
        logger.info("APIトークン取得OK")
        return token

    def _get_token(self) -> str:
        """トークンをキャッシュして返す（1時間ごとに更新）。"""
        if self._cached_token is None or (time.time() - self._token_fetched_at) > 3600:
            self._cached_token = self._refresh_token()
            self._token_fetched_at = time.time()
        return self._cached_token

    def _headers(self) -> Dict:
        return {"X-API-KEY": self._get_token()}

    # ─── 口座照会 ──────────────────────────────────────────

    def get_wallet_cash(self) -> Dict:
        """現物買付余力を取得する。"""
        resp = requests.get(
            f"{KABU_API_BASE_URL}/wallet/cash",
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        self._check(resp)
        return resp.json()

    # ─── 株価照会 ──────────────────────────────────────────

    def get_board(self, symbol: str, exchange: int = 1) -> Dict:
        """板情報・現在値を取得する。
        symbol: 銘柄コード（例: "7203"）
        exchange: 1=東証, 3=名証, 5=福証, 6=札証
        """
        resp = requests.get(
            f"{KABU_API_BASE_URL}/board/{symbol}@{exchange}",
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        self._check(resp)
        return resp.json()

    def get_symbol_info(self, symbol: str, exchange: int = 1) -> Dict:
        """銘柄情報（単元株数・値幅制限等）を取得する。"""
        resp = requests.get(
            f"{KABU_API_BASE_URL}/symbol/{symbol}@{exchange}",
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        self._check(resp)
        return resp.json()

    # ─── 注文 ──────────────────────────────────────────────

    def send_order(
        self,
        symbol: str,
        exchange: int,
        side: str,              # "2"=買い, "1"=売り
        qty: int,
        trade_password: str,
        price: float = 0,
        front_order_type: int = 10,  # 10=成行, 20=指値
        cash_margin: int = 1,        # 1=現物
        account_type: int = 4,       # 4=特定口座
    ) -> Dict:
        """注文を送信する。"""
        payload = {
            "Password":       trade_password,
            "Symbol":         symbol,
            "Exchange":       exchange,
            "SecurityType":   1,     # 株式
            "Side":           side,
            "CashMargin":     cash_margin,
            "DelivType":      2,     # お預り金
            "FundType":       "AA",  # 保護預り
            "AccountType":    account_type,
            "Qty":            qty,
            "Price":          price,
            "FrontOrderType": front_order_type,
            "ExpireDay":      0,     # 当日
        }
        resp = requests.post(
            f"{KABU_API_BASE_URL}/sendorder",
            json=payload,
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        self._check(resp)
        result = resp.json()
        side_str = "買い" if side == "2" else "売り"
        logger.info(f"注文送信: {symbol} {side_str} {qty}株 → OrderId={result.get('OrderId')}")
        return result

    def cancel_order(self, order_id: str, trade_password: str) -> Dict:
        """注文をキャンセルする。"""
        resp = requests.put(
            f"{KABU_API_BASE_URL}/cancelorder",
            json={"OrderId": order_id, "Password": trade_password},
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        self._check(resp)
        return resp.json()

    # ─── 注文・ポジション照会 ──────────────────────────────

    def get_orders(self, active_only: bool = True) -> List[Dict]:
        """注文一覧を取得する。"""
        params = {"activeonly": str(active_only).lower(), "product": 0}
        resp = requests.get(
            f"{KABU_API_BASE_URL}/orders",
            params=params,
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        self._check(resp)
        return resp.json() or []

    def get_positions(self) -> List[Dict]:
        """保有ポジション一覧を取得する。"""
        resp = requests.get(
            f"{KABU_API_BASE_URL}/positions",
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        self._check(resp)
        return resp.json() or []

    # ─── ユーティリティ ─────────────────────────────────────

    @staticmethod
    def _check(resp: requests.Response):
        """レスポンスのステータスコードを確認する。"""
        if resp.status_code not in (200, 201):
            raise KabuAPIError(
                f"API エラー {resp.status_code}: {resp.text}"
            )
