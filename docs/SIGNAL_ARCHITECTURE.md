# Signal Architecture

## Overview

The Composite Signal Score aggregates 9 market indicators into a single
normalised score ranging from -1 (strong bearish) to +1 (strong bullish).

Each indicator is independently z-scored against its own rolling history,
making the score robust to changes in absolute market scale.

## Mathematical Foundation

### Z-score normalisation
Each raw indicator value is normalised as:

    z = (value - rolling_mean) / rolling_std

Clamped to ±3σ to prevent outlier dominance.
Window: 200 bars (33 days on 4H).

### Weighted aggregation

    raw_score = Σ(z_i × w_i × adx_mult_i) / Σ(w_i × adx_mult_i)

Where `adx_mult` scales from 0→1 between ADX 15 and 45,
gating trend-following signals in ranging markets.

### Softclamp (tanh squashing)

    score = tanh(raw_score × 1.5)

Maps the unbounded raw score to (-1, 1) smoothly.

### EMA smoothing

    smoothed = EMA(score, span=5)

Reduces single-bar noise while preserving responsiveness.

## Signal Components

### Trend signals (multiplied by ADX regime factor)

| Signal | Formula | Weight | Rationale |
|---|---|---|---|
| RSI z | z(RSI(14) - 50) | 1.5 | Price momentum |
| MFI z | z(MFI(14) - 50) | 1.0 | Volume-weighted momentum |
| MACD z | z(MACD histogram) | 1.2 | Momentum acceleration |
| DMI z | z(+DI - -DI) | 1.0 | Trend direction |

### Microstructure signals (always active)

| Signal | Formula | Weight | Rationale |
|---|---|---|---|
| CVD z | z(perp_delta - spot_delta) | 1.8 | Leveraged vs spot flow |
| OI z | z(OI_change × price_sign) | 1.5 | New position conviction |
| Funding z | z(-funding) if |z| > 1.5σ else 0 | 0.8 | Contrarian extreme |
| VWAP z | z(close - RVWAP(168)) | 1.0 | Price vs vol-weighted mean |
| Liq z | z(buy_liq - sell_liq) | 1.2 | Liquidation cascade direction |

## ADX Regime Filter

The ADX multiplier prevents false signals in ranging markets:

    adx_mult = clamp((ADX - 15) / (45 - 15), 0, 1)

- ADX < 15: adx_mult = 0 → all trend signals muted
- ADX = 30: adx_mult = 0.5 → trend signals at half weight
- ADX > 45: adx_mult = 1.0 → trend signals at full weight

## Entry Conditions (ALL must be true)

1. Score crosses above +0.40 (long) or below -0.40 (short)
2. ADX > 20 (market is trending)
3. Price above 200-day SMA (longs only)
4. Price below 100-day SMA (shorts only)
5. Not in circuit breaker cooldown (10 bars after 3 consecutive stops)

## Exit Hierarchy

1. **ATR trailing stop** — activates after 1% profit, trails at 4×ATR14
2. **Take profit** — fixed at 12% from entry
3. **Signal exit** — score crosses back through zero after min 4 bars

## 4-Year Halving Cycle Sizing

Position size is scaled by cycle phase (April 2024 halving):

| Phase | Months post-halving | Long mult | Short mult |
|---|---|---|---|
| Accumulation | 0-6 | 0.6× | 0.8× |
| Bull | 6-18 | 1.4× | 0.6× |
| Distribution | 18-30 | 0.6× | 1.4× |
| Bear | 30+ | 0.4× | 1.6× |

Current (March 2026): Month 23 → Distribution phase.
