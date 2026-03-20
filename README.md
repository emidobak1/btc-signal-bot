# BTC Composite Signal Bot

A quantitative trading signal system for BTC/USDT perpetual futures, built on multi-indicator confluence scoring with statistical validation.

## Project Status
- Signal development: **Complete (v6)**
- Backtesting: **Complete**
- Statistical validation: **Complete**
- Timeframe optimization: **Complete — 4H confirmed optimal**
- Live bot infrastructure: **In progress**

---

## Performance Summary (v6 — 4H BTC/USDT, Dec 2023 → Mar 2026)

| Metric | Value | Target |
|---|---|---|
| Total return | +122.6% | >buy & hold |
| Buy & hold | +59.8% | — |
| Sharpe ratio | 1.24 | >1.0 ✅ |
| Max drawdown | -25.3% | <25% |
| Profit factor | 2.27 | >1.5 ✅ |
| Expectancy/trade | +1.37% | >0.5% ✅ |
| Total trades | 58 | >50 ✅ |
| Win rate | 36.2% | — |

---

## Repository Structure

```
btc_signal_bot/
├── README.md
├── requirements.txt
│
├── backtest/
│   ├── backtest_v1.py          — baseline signal
│   ├── backtest_v2.py          — ADX filter + wider exit
│   ├── backtest_v3.py          — stop loss + take profit
│   ├── backtest_v4.py          — SMA filter + ATR trail + cycle phase
│   ├── backtest_v5.py          — phase filter + wider ATR
│   ├── backtest_v6.py          — OPTIMAL — all improvements combined
│   ├── backtest_v6b.py         — statistical weight adjustments
│   ├── backtest_v7.py          — aggressive statistical pruning (worse)
│   └── timeframe_optimizer.py  — tests 8 timeframes, 4H confirmed best
│
├── indicator/
│   └── composite_signal_v6.js  — MMT scripting indicator (paste into MMT)
│
├── stats/
│   └── stat_validation.py      — 7 statistical tests on all indicators
│
└── docs/
    ├── SIGNAL_ARCHITECTURE.md  — how the composite signal works
    ├── BACKTEST_RESULTS.md      — full results table across all versions
    └── STATISTICAL_FINDINGS.md — key findings from validation tests
```

---

## Signal Architecture

The composite score aggregates 9 indicators into a single -1 to +1 signal:

### Trend signals (gated by ADX multiplier)
- **RSI** (weight 1.5) — momentum oscillator, z-scored deviation from 50
- **MFI** (weight 1.0) — volume-weighted RSI, confirms capital commitment
- **MACD** (weight 1.2) — momentum acceleration/deceleration
- **DMI** (weight 1.0) — directional movement, ADX as regime filter

### Microstructure signals (always active)
- **CVD divergence** (weight 1.8) — perps vs spot per-bar flow delta
- **OI alignment** (weight 1.5) — open interest change in price direction
- **Funding rate** (weight 0.8) — contrarian gate at statistical extremes
- **RVWAP deviation** (weight 1.0) — price vs volume-weighted mean
- **Liquidation imbalance** (weight 1.2) — buy vs sell liquidation size

### Entry filters
- ADX > 20 (trending market required)
- Price above 200-day SMA for longs
- Price below 100-day SMA for shorts
- 4-year Bitcoin halving cycle phase sizing
- Circuit breaker after 3 consecutive stops

### Exit logic
- Signal exit: score crosses back through zero
- ATR trailing stop: 4× ATR14, activates after 1% profit
- Take profit: 12%
- Initial stop: 7% (before trail activates)

---

## Backtest Evolution

| Version | Key Change | Sharpe | Return | Drawdown |
|---|---|---|---|---|
| v1 | Baseline | 0.25 | +5.1% | -39.2% |
| v2 | ADX filter + wider exit | 0.54 | +47.3% | -45.9% |
| v3 | Hard stop + TP | 0.62 | +53.8% | -41.0% |
| v4 | SMA filter + ATR trail + cycle | 0.90 | +85.9% | -19.8% |
| v5 | Phase filter (over-filtered) | 0.91 | +108.2% | -30.1% |
| **v6** | **Rebalanced — optimal** | **1.24** | **+122.6%** | **-25.3%** |
| v6b | Statistical weight adjustment | 1.06 | +93.5% | -19.8% |
| v7 | Aggressive pruning (worse) | 0.52 | +28.2% | -30.1% |

---

## Statistical Validation Results

All 8 indicators pass the ADF stationarity test (p < 0.0001).

Key findings:
- **MFI**: strongest partial F-test (p=0.0002) + Granger causality ✓
- **CVD**: strongest Granger causality (p=0.0002)
- **VWAP**: best rolling information ratio (-1.33)
- **RSI ↔ DMI**: high collinearity (Spearman r=0.93) — managed via weights
- **OI**: no Granger edge due to Binance 30-day history cap

---

## Timeframe Optimization Results

| TF | Sharpe | Return | Drawdown | Composite Score |
|---|---|---|---|---|
| **4H** | **0.94** | **+69.1%** | **-19.6%** | **0.817 OPTIMAL** |
| 2H | 1.10 | +46.7% | -13.5% | 0.748 |
| 1D | 0.67 | +34.1% | -18.1% | 0.737 |
| 1H | 0.05 | -2.3% | -19.5% | 0.392 |
| 15M | -1.64 | -8.6% | -14.5% | 0.028 |

4H confirmed as optimal. Signal has no edge below 30 minutes.

---

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/btc_signal_bot
cd btc_signal_bot
pip install -r requirements.txt
```

### Run backtest
```bash
python backtest/backtest_v6.py
```

### Run statistical validation
```bash
python stats/stat_validation.py
```

### Run timeframe optimizer
```bash
python backtest/timeframe_optimizer.py
```

---

## Dependencies
See `requirements.txt`. Requires Python 3.9+.

---

## Disclaimer
This is a research and educational project. Past backtest performance does not guarantee future results. Cryptocurrency trading carries significant risk of capital loss. Never trade with money you cannot afford to lose.
