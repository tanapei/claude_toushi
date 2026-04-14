# Claude 株式取引シミュレーションシステム

Claude AI を用いた日本株（東証）の自動売買シミュレーションシステムです。
バックテスト結果を Claude AI が分析し、良かった点・悪かった点を特定して次回の戦略改善に活かします。

## システム概要

```
┌─────────────────────────────────────────────────────────┐
│              株式取引シミュレーション                     │
│                                                         │
│  [データ取得]  →  [戦略計算]  →  [売買シミュレーション]  │
│  Yahoo Finance    EMA/RSI/MACD   ポートフォリオ管理      │
│      ↓                                                  │
│  [結果保存]   →  [Claude AI分析]  →  [パラメータ改善]    │
│  SQLite DB        良し悪し特定      次回戦略に反映        │
└─────────────────────────────────────────────────────────┘
```

## 取引戦略

**デュアルモメンタム + EMAクロス + RSIフィルタ**

| 条件 | 内容 |
|------|------|
| エントリー | 9日EMAが26日EMAをゴールデンクロス |
| トレンドフィルタ | 価格 > 200日EMA（上昇トレンドのみ） |
| RSIフィルタ | RSI 40〜70（過熱・売られすぎ除外） |
| モメンタム | 12ヶ月リターン上位10銘柄を対象 |
| ボリューム確認 | 出来高が20日平均の1.2倍以上 |
| 損切り | エントリー価格から-7% |
| 利確 | エントリー価格から+20% |
| トレーリングストップ | 高値から-8% |

## セットアップ

### 1. 依存パッケージのインストール

```bash
pip install -r requirements.txt
```

### 2. APIキーの設定（Claude AI分析を使う場合）

```bash
cp .env.example .env
# .env を編集して ANTHROPIC_API_KEY を設定
export ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx
```

※ APIキーなしでも動作します（ルールベース分析に切り替わります）

## 使い方

### 単発バックテスト

```bash
# デフォルト期間（2022-01-01〜2024-12-31）
python -m trading_system.main --mode single

# 期間指定
python -m trading_system.main --mode single --start 2023-01-01 --end 2023-12-31

# JSON出力
python -m trading_system.main --mode single --output results/my_backtest.json
```

### イテレーティブバックテスト（推奨）

Claude AIが毎回結果を分析し、パラメータを自動改善します。

```bash
# 3回反復（デフォルト）
python -m trading_system.main --mode iterative --iterations 3
```

### 過去の結果を確認

```bash
# 全セッション一覧
python -m trading_system.main --mode report

# 特定セッションの詳細
python -m trading_system.main --mode report --run-id run_20240101_120000_abc123
```

## ファイル構成

```
claude_toushi/
├── trading_system/
│   ├── __init__.py
│   ├── config.py          # 設定値・銘柄ユニバース
│   ├── data_fetcher.py    # Yahoo Financeデータ取得
│   ├── strategy.py        # テクニカル指標・売買シグナル
│   ├── portfolio.py       # ポートフォリオ管理・損益計算
│   ├── trade_logger.py    # SQLiteへの結果保存
│   ├── analyzer.py        # Claude AI分析
│   ├── backtest.py        # バックテストエンジン
│   └── main.py            # CLIエントリーポイント
├── data/
│   ├── trades.db          # SQLiteデータベース（自動生成）
│   └── universe_*.pkl     # 株価データキャッシュ（自動生成）
├── logs/
│   └── trading.log        # 実行ログ（自動生成）
├── results/               # JSON出力先
├── requirements.txt
├── .env.example
└── README.md
```

## 設定のカスタマイズ

`trading_system/config.py` で以下を変更できます：

- `INITIAL_CAPITAL`: 初期資金（デフォルト: 100万円）
- `MAX_POSITIONS`: 最大同時保有銘柄数（デフォルト: 5）
- `STOP_LOSS_PCT`: 損切りライン（デフォルト: 7%）
- `TAKE_PROFIT_PCT`: 利確ライン（デフォルト: 20%）
- `BACKTEST_START/END`: バックテスト期間
- `VALID_UNIVERSE`: 対象銘柄リスト

## 注意事項

- このシステムはシミュレーション専用です。実際の投資判断には使用しないでください。
- 過去のパフォーマンスは将来のリターンを保証しません。
- 株式投資にはリスクが伴います。実際の投資は自己責任でお願いします。
