"""
システム設定ファイル
株式取引シミュレーションシステムの設定値を管理します。
"""
import os
from pathlib import Path

# =============================================
# プロジェクトパス設定
# =============================================
BASE_DIR = Path(__file__).parent.parent

_storage = os.environ.get("STORAGE_DIR", "")
if _storage:
    STORAGE_ROOT = Path(_storage)
else:
    try:
        test_path = BASE_DIR / "data" / ".write_test"
        test_path.parent.mkdir(exist_ok=True)
        test_path.touch()
        test_path.unlink()
        STORAGE_ROOT = BASE_DIR
    except (PermissionError, OSError):
        STORAGE_ROOT = Path("/tmp/claude_toushi")

DATA_DIR    = STORAGE_ROOT / "data"
LOGS_DIR    = STORAGE_ROOT / "logs"
RESULTS_DIR = STORAGE_ROOT / "results"

for d in [DATA_DIR, LOGS_DIR, RESULTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

DB_PATH       = DATA_DIR / "trades.db"
ANALYSIS_PATH = RESULTS_DIR / "analysis_history.json"

# =============================================
# Claude API 設定
# =============================================
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-opus-4-6"


# =============================================
# kabuステーション® API 設定
# =============================================
KABU_API_BASE_URL   = "http://localhost:18080/kabusapi"
KABU_API_PASSWORD   = os.getenv("KABU_API_PASSWORD", "")    # kabuステーション® APIパスワード
KABU_TRADE_PASSWORD = os.getenv("KABU_TRADE_PASSWORD", "")  # 取引パスワード
KABU_EXCHANGE_CODE  = 1  # 1=東証


# =============================================
# ポートフォリオ設定（実運用）
# =============================================
TOTAL_CAPITAL    = 700_000   # 運用総額 70万円
MAX_POSITIONS    = 5         # 最大同時保有銘柄数
POSITION_SIZE    = TOTAL_CAPITAL // MAX_POSITIONS  # 1銘柄 14万円

# =============================================
# バックテスト互換設定
# =============================================
INITIAL_CAPITAL  = 1_000_000
POSITION_SIZE_PCT = 0.18      # 1銘柄あたり資金割合（バックテスト用）
COMMISSION_RATE  = 0.001

# =============================================
# リスク管理設定
# =============================================
STOP_LOSS_PCT     = 0.05
TAKE_PROFIT_PCT   = 0.15
TRAILING_STOP_PCT = 0.06

# =============================================
# 取引戦略設定（バックテスト用）
# =============================================
STRATEGY_PARAMS = {
    "ema_fast": 9,
    "ema_slow": 26,
    "ema_trend": 75,
    "rsi_period": 14,
    "rsi_lower": 30,
    "rsi_upper": 75,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "momentum_period": 120,
    "momentum_skip": 5,
    "top_n_momentum": 15,
    "volume_ma_period": 20,
    "volume_multiplier": 1.0,
    "min_score": 3,
    "adx_period": 14,
    "adx_threshold": 20,
    "confirm_days": 3,
    "max_hold_days": 30,
    "market_regime_ema": 50,
}

# =============================================
# バックテスト期間設定
# =============================================
BACKTEST_START = "2022-01-01"
BACKTEST_END   = "2024-12-31"
REBALANCE_FREQ = "monthly"

# =============================================
# 日本株ユニバース（Nikkei225 主要銘柄）
# =============================================
JP_UNIVERSE = [
    "7203.T",  # トヨタ自動車
    "7267.T",  # ホンダ
    "7269.T",  # スズキ
    "7201.T",  # 日産自動車
    "6758.T",  # ソニーグループ
    "6861.T",  # キーエンス
    "6954.T",  # ファナック
    "6902.T",  # デンソー
    "6501.T",  # 日立製作所
    "6752.T",  # パナソニック
    "6971.T",  # 京セラ
    "9984.T",  # ソフトバンクグループ
    "9432.T",  # NTT
    "9433.T",  # KDDI
    "9434.T",  # ソフトバンク（通信）
    "4755.T",  # 楽天グループ
    "8306.T",  # 三菱UFJフィナンシャル
    "8316.T",  # 三井住友フィナンシャル
    "8411.T",  # みずほフィナンシャル
    "8031.T",  # 三井物産
    "8058.T",  # 三菱商事
    "9983.T",  # ファーストリテイリング
    "2914.T",  # 日本たばこ産業
    "2502.T",  # アサヒグループ
    "2503.T",  # キリンホールディングス
    "4502.T",  # 武田薬品工業
    "4519.T",  # 中外製薬
    "4568.T",  # 第一三共
    "8802.T",  # 三菱地所
    "8830.T",  # 住友不動産
    "5020.T",  # ENEOSホールディングス
]

# =============================================
# 米国株ユニバース（S&P500 主要銘柄）
# =============================================
US_UNIVERSE = [
    # テクノロジー
    "AAPL",   # アップル
    "MSFT",   # マイクロソフト
    "NVDA",   # エヌビディア
    "GOOGL",  # アルファベット
    "META",   # メタ
    "AMZN",   # アマゾン
    "AMD",    # AMD
    "AVGO",   # ブロードコム
    "ADBE",   # アドビ
    "CRM",    # セールスフォース
    # 半導体
    "TSM",    # TSMC
    "QCOM",   # クアルコム
    # 消費・ヘルスケア
    "TSLA",   # テスラ
    "WMT",    # ウォルマート
    "COST",   # コストコ
    "MCD",    # マクドナルド
    "JNJ",    # ジョンソン＆ジョンソン
    "LLY",    # イーライリリー
    "UNH",    # ユナイテッドヘルス
    # 金融
    "JPM",    # JPモルガン
    "V",      # ビザ
    "MA",     # マスターカード
    "GS",     # ゴールドマン・サックス
    # エネルギー
    "XOM",    # エクソンモービル
    "CVX",    # シェブロン
    # ETF（少額運用でも分散効果）
    "QQQ",    # Nasdaq100
    "SPY",    # S&P500
    "VGT",    # テクノロジーセクター
    "XLF",    # 金融セクター
]

# =============================================
# 除外銘柄リスト（両バックテストで一貫して低勝率）
# =============================================
TICKER_BLACKLIST = [
    "4755.T",  # 楽天グループ: 勝率11-29%、両バックテストでワースト
    "7269.T",  # スズキ: 勝率20-22%、両バックテストでワースト
]

# 後方互換性
VALID_UNIVERSE  = [t for t in JP_UNIVERSE if t not in TICKER_BLACKLIST]
STOCK_UNIVERSE  = VALID_UNIVERSE
