# Backtest Results — All Versions

## Test Conditions
- Asset: BTC/USDT Perpetual Futures (Binance)
- Timeframe: 4H
- Period: December 2023 → March 2026 (5,000 bars)
- Fees: 0.04% taker per side
- Slippage: 0.02% per side
- Data source: Binance Futures public API

---

## Version Comparison

| Version | Trades | Win% | Avg Win | Avg Loss | PF | Sharpe | Return | B&H | Drawdown |
|---|---|---|---|---|---|---|---|---|---|
| v1 | 139 | 33.8% | +5.54% | -2.56% | 1.10 | 0.25 | +5.1% | +59.8% | -39.2% |
| v2 | 44 | 47.7% | +8.91% | -5.36% | 1.52 | 0.54 | +47.3% | +59.7% | -45.9% |
| v3 | 52 | 44.2% | +9.58% | -5.35% | 1.42 | 0.62 | +53.8% | +59.6% | -41.0% |
| v4 | 50 | 40.0% | +4.25% | -2.32% | 1.22 | 0.90 | +85.9% | +59.8% | -19.8% |
| v5 | 23 | 39.1% | +10.95% | -3.98% | 1.77 | 0.91 | +108.2% | +59.5% | -30.1% |
| **v6** | **58** | **36.2%** | **+6.77%** | **-1.69%** | **2.27** | **1.24** | **+122.6%** | **+59.8%** | **-25.3%** |
| v6b | 57 | 31.6% | +7.31% | -2.00% | 1.69 | 1.06 | +93.5% | +63.4% | -19.8% |
| v7 | 65 | 32.3% | +5.65% | -2.24% | 1.20 | 0.52 | +28.2% | +63.3% | -30.1% |

---

## v6 — Optimal Version

### Key parameters
- Entry threshold: ±0.40
- Exit: score crosses zero (after min 4 bars)
- ATR trail: 4.0× ATR14, activates at 1% profit
- Initial stop: 7%
- Take profit: 12%
- SMA filter: longs above 200d, shorts below 100d
- Circuit breaker: pause 10 bars after 3 consecutive stops

### Exit breakdown
| Type | Count | % | Avg Return |
|---|---|---|---|
| Signal exits | 35 | 60% | -0.88% |
| Trail stops | 14 | 24% | -0.61% |
| Take profits | 9 | 16% | +13.19% |

### Performance by cycle phase
| Phase | Trades | Win% | Avg Return | Total |
|---|---|---|---|---|
| Accumulation | 10 | 60.0% | +3.79% | +37.9% |
| Bull | 32 | 31.2% | +0.55% | +17.7% |
| Distribution | 9 | 44.4% | +3.45% | +31.0% |
| Bear | 7 | 14.3% | -1.02% | -7.1% |

### Direction breakdown
| Direction | Trades | Win% | Total Raw |
|---|---|---|---|
| Long | 34 | 26.5% | -6.4% |
| Short | 24 | 50.0% | +85.8% |

**Key insight:** All profitable edge is on the short side.
Longs are marginally unprofitable in this test period due to
choppy bull phase conditions (mid-2024 to early 2026).

---

## Key Learnings Per Version

### v1 → v2: ADX filter + wider exit
Adding ADX > 20 entry requirement reduced trade count from 139 to 44.
Win rate jumped from 33.8% to 47.7%. Exit threshold moved to opposite
threshold instead of zero-cross. Total return improved 9×.

### v2 → v3: Risk management
Added 8% hard stop and 20% take profit. Stop rate 23%, avg stop -7.83%.
Sharpe improved but drawdown worsened due to longer hold periods.

### v3 → v4: SMA filter + ATR trail + cycle phase
The single biggest improvement. 200-day SMA blocked the Feb 2026
long entries at $70k during a bearish regime. ATR trailing stop
replaced fixed stop. Sharpe 0.90, drawdown dropped to -19.8%.

### v4 → v5: Phase filter (over-optimised)
Blocking accumulation and bear phases reduced to 23 trades — too few
for statistical confidence. Concentration risk — 2 trades drove most returns.

### v5 → v6: Rebalancing
Returned to all-phases allowed with cycle sizing multipliers.
Lowered entry threshold to 0.40, moved exit to zero-cross,
tightened TP to 12%, 4× ATR multiplier. Best overall result.

### v6 → v7: Statistical pruning (worse)
Dropped DMI and OI based on individual IC tests. This broke signal
confluence — the composite works through agreement between signals,
not individual strength. Critical lesson: individual Spearman IC
tests are the wrong test for a conjunction-based system.

### v6b: Correct statistical application
Applied stats correctly — reduced weights without dropping signals.
DMI 1.0→0.4, VWAP 1.0→1.6, CVD 1.8→2.0. Sharpe 1.06, better
drawdown (-19.8%) but lower return. Use v6b if drawdown is priority.
