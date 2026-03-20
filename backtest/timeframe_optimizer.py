"""
Timeframe Optimization — Composite Signal v6
=============================================
Tests the v6 signal across multiple timeframes to find the optimal
timeframe for risk-adjusted returns.

Timeframes tested: 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d

Key insight: each timeframe requires different parameter scaling.
RVWAP period, z-score window, and min hold bars all need to represent
the same REAL TIME duration regardless of bar size.

Real-time anchors used:
  - RVWAP: 1 week of bars  (represents weekly volume profile)
  - Z-Window: 6 weeks of bars  (statistical normalization window)
  - Min hold: 1 day of bars  (minimum trade duration)
  - ADX min: fixed at 20 (dimensionless)
  - ATR period: 1 day of bars
  - SMA Long: 200 days of bars
  - SMA Short: 100 days of bars
"""

import requests
import pandas as pd
import numpy as np
import time

# ─── Timeframe definitions ────────────────────────────────────────────────────
TIMEFRAMES = {
    "15m":  {"interval": "15m",  "bars_per_day": 96,  "bars_per_hour": 4   },
    "30m":  {"interval": "30m",  "bars_per_day": 48,  "bars_per_hour": 2   },
    "1h":   {"interval": "1h",   "bars_per_day": 24,  "bars_per_hour": 1   },
    "2h":   {"interval": "2h",   "bars_per_day": 12,  "bars_per_hour": 0.5 },
    "4h":   {"interval": "4h",   "bars_per_day": 6,   "bars_per_hour": 0.25},
    "6h":   {"interval": "6h",   "bars_per_day": 4,   "bars_per_hour": None},
    "12h":  {"interval": "12h",  "bars_per_day": 2,   "bars_per_hour": None},
    "1d":   {"interval": "1d",   "bars_per_day": 1,   "bars_per_hour": None},
}

SYMBOL = "BTCUSDT"

# Real-time durations (in days) — same for all timeframes
RVWAP_DAYS     = 7     # 1 week RVWAP
Z_WINDOW_DAYS  = 42    # 6 weeks z-score window
MIN_HOLD_DAYS  = 1     # 1 day minimum hold
ATR_DAYS       = 1     # 1 day ATR period
SMA_LONG_DAYS  = 200   # 200 day SMA
SMA_SHORT_DAYS = 100   # 100 day SMA

# Fixed signal parameters
RSI_PERIOD   = 14
MFI_PERIOD   = 14
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIG     = 9
DMI_LEN      = 14
SMOOTH_LEN   = 5
ADX_MIN      = 20
ADX_MAX      = 45
FUND_EXTREME = 1.5

# v6 weights
W_RSI  = 1.5; W_MFI  = 1.0; W_MACD = 1.2; W_ADX  = 1.0
W_CVD  = 1.8; W_OI   = 1.5; W_FUND = 0.8; W_VWAP = 1.0; W_LIQ = 1.2

# v6 execution
BULL_THRESH      = 0.40;  BEAR_THRESH      = -0.40
ATR_MULT         = 4.0;   INITIAL_STOP_PCT = 0.07
TRAIL_ACTIVATION = 0.01;  TAKE_PROFIT      = 0.12
MAX_CONSEC_STOPS = 3;     COOLDOWN_MULT    = 10  # bars = 10 × bars_per_day

FEES     = 0.0004
SLIPPAGE = 0.0002

LAST_HALVING = pd.Timestamp("2024-04-20", tz="UTC")

CYCLE_PHASES = {
    "accumulation": (0,  6,  0.6, 0.8),
    "bull":         (6,  18, 1.4, 0.6),
    "distribution": (18, 30, 0.6, 1.4),
    "bear":         (30, 999,0.4, 1.6),
}

def get_cycle_phase(date):
    m = (date - LAST_HALVING).days / 30.44
    for phase, (s, e, lm, sm) in CYCLE_PHASES.items():
        if s <= m < e:
            return phase, lm, sm
    return "bear", 0.4, 1.6

# ─── Data fetching ────────────────────────────────────────────────────────────

def fetch_ohlcv(interval, n_bars=5000):
    """Fetch up to n_bars of OHLCV for given interval."""
    url       = "https://fapi.binance.com/fapi/v1/klines"
    all_data  = []
    end_time  = None
    limit     = 1000
    fetched   = 0

    while fetched < n_bars:
        batch = min(limit, n_bars - fetched)
        params = {"symbol": SYMBOL, "interval": interval, "limit": batch}
        if end_time:
            params["endTime"] = end_time
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        all_data  = data + all_data
        end_time  = data[0][0] - 1
        fetched  += len(data)
        time.sleep(0.12)

    df = pd.DataFrame(all_data, columns=[
        "timestamp","open","high","low","close","volume","close_time",
        "quote_volume","trades","taker_buy_base","taker_buy_quote","ignore"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df = df[["open","high","low","close","volume","taker_buy_base"]].astype(float)
    df["buy_volume"]  = df["taker_buy_base"]
    df["sell_volume"] = df["volume"] - df["taker_buy_base"]
    df = df.drop_duplicates().sort_index()
    return df

def fetch_daily_sma():
    url, all_data, end_time = "https://fapi.binance.com/fapi/v1/klines", [], None
    for _ in range(8):
        params = {"symbol": SYMBOL, "interval": "1d", "limit": 1000}
        if end_time: params["endTime"] = end_time
        r = requests.get(url, params=params, timeout=15); r.raise_for_status()
        data = r.json()
        if not data: break
        all_data = data + all_data; end_time = data[0][0] - 1
        time.sleep(0.12)
    df = pd.DataFrame(all_data, columns=[
        "timestamp","open","high","low","close","volume","close_time",
        "quote_volume","trades","taker_buy_base","taker_buy_quote","ignore"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df["close"] = df["close"].astype(float)
    df = df[["close"]].drop_duplicates().sort_index()
    df["sma_long"]        = df["close"].rolling(SMA_LONG_DAYS,  min_periods=SMA_LONG_DAYS).mean()
    df["sma_short"]       = df["close"].rolling(SMA_SHORT_DAYS, min_periods=SMA_SHORT_DAYS).mean()
    df["above_sma_long"]  = df["close"] > df["sma_long"]
    df["below_sma_short"] = df["close"] < df["sma_short"]
    return df[["sma_long","sma_short","above_sma_long","below_sma_short"]]

def fetch_funding():
    url, all_data, end_time = "https://fapi.binance.com/fapi/v1/fundingRate", [], None
    for _ in range(5):
        params = {"symbol": SYMBOL, "limit": 1000}
        if end_time: params["endTime"] = end_time
        r = requests.get(url, params=params, timeout=15); r.raise_for_status()
        data = r.json()
        if not data: break
        all_data = data + all_data; end_time = data[0]["fundingTime"] - 1
        time.sleep(0.12)
    df = pd.DataFrame(all_data)
    df["timestamp"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df["funding"] = df["fundingRate"].astype(float)
    return df[["funding"]].drop_duplicates().sort_index()

def fetch_oi():
    url    = "https://fapi.binance.com/futures/data/openInterestHist"
    # OI only available in certain periods — use 4h for merge
    params = {"symbol": SYMBOL, "period": "4h", "limit": 500}
    r      = requests.get(url, params=params, timeout=15); r.raise_for_status()
    df     = pd.DataFrame(r.json())
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df["oi"] = df["sumOpenInterestValue"].astype(float)
    return df[["oi"]].drop_duplicates().sort_index()

# ─── Signal helpers ───────────────────────────────────────────────────────────

def rolling_zscore(series, window):
    mu = series.rolling(window, min_periods=max(2, window//4)).mean()
    sd = series.rolling(window, min_periods=max(2, window//4)).std()
    return ((series - mu) / sd.replace(0, np.nan)).fillna(0).clip(-3, 3)

def compute_rsi(close, period=14):
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss  = (-delta).clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def compute_mfi(high, low, close, volume, period=14):
    tp      = (high + low + close) / 3
    mf      = tp * volume
    pos_sum = mf.where(tp > tp.shift(1), 0).rolling(period, min_periods=1).sum()
    neg_sum = mf.where(tp < tp.shift(1), 0).rolling(period, min_periods=1).sum()
    mfr     = pos_sum / neg_sum.replace(0, np.nan)
    return (100 - 100 / (1 + mfr)).fillna(50)

def compute_macd_hist(close, fast=12, slow=26, signal=9):
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    macd  = ema_f - ema_s
    sig   = macd.ewm(span=signal, adjust=False).mean()
    return macd - sig

def compute_dmi(high, low, close, period=14):
    tr = pd.concat([high-low, (high-close.shift(1)).abs(),
                    (low-close.shift(1)).abs()], axis=1).max(axis=1)
    up = high - high.shift(1); dn = low.shift(1) - low
    plus_dm  = up.where((up > dn) & (up > 0), 0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0)
    atr      = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, np.nan)
    dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx      = dx.ewm(alpha=1/period, adjust=False).mean()
    return plus_di.fillna(0), minus_di.fillna(0), adx.fillna(0)

def compute_rvwap(high, low, close, buy_vol, sell_vol, period):
    hlc3  = (high + low + close) / 3
    vol   = buy_vol + sell_vol
    pv    = hlc3 * vol
    return (pv.rolling(period, min_periods=1).sum() /
            vol.rolling(period, min_periods=1).sum().replace(0, np.nan)).ffill()

def compute_atr(high, low, close, period):
    tr = pd.concat([high-low, (high-close.shift(1)).abs(),
                    (low-close.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def softclamp(x):
    return np.tanh(x)

# ─── Compute signal for a given timeframe ─────────────────────────────────────

def compute_signal(df, bpd):
    """bpd = bars per day for this timeframe"""
    z_window     = max(20, int(Z_WINDOW_DAYS  * bpd))
    rvwap_period = max(10, int(RVWAP_DAYS     * bpd))
    atr_period   = max(3,  int(ATR_DAYS       * bpd))

    close  = df["close"]; high = df["high"]; low = df["low"]
    vol    = df["volume"]; buy_v = df["buy_volume"]; sell_v = df["sell_volume"]

    rsi_z  = rolling_zscore(compute_rsi(close) - 50, z_window)
    mfi_z  = rolling_zscore(compute_mfi(high, low, close, vol) - 50, z_window)
    macd_z = rolling_zscore(compute_macd_hist(close), z_window)

    plus_di, minus_di, adx = compute_dmi(high, low, close)
    di_z     = rolling_zscore(plus_di - minus_di, z_window)
    adx_mult = ((adx - ADX_MIN) / (ADX_MAX - ADX_MIN)).clip(0, 1)

    cvd_z    = rolling_zscore(buy_v - sell_v, z_window)

    oi_delta  = df.get("oi_delta", pd.Series(0.0, index=df.index))
    price_dir = np.sign(close - df["open"])
    oi_z      = rolling_zscore(oi_delta * price_dir, z_window)

    fund       = df.get("funding", pd.Series(0.0, index=df.index)).fillna(0)
    raw_fund_z = rolling_zscore(-fund, z_window)
    fund_z     = raw_fund_z.where(raw_fund_z.abs() >= FUND_EXTREME, 0)

    vwap_z = rolling_zscore(
        close - compute_rvwap(high, low, close, buy_v, sell_v, rvwap_period),
        z_window
    )
    liq_z = pd.Series(0.0, index=df.index)

    trend_w  = W_RSI * adx_mult + W_MFI * adx_mult + W_MACD * adx_mult + W_ADX
    micro_w  = W_CVD + W_OI + W_FUND + W_VWAP + W_LIQ
    total_w  = trend_w + micro_w

    raw_score = (
        rsi_z  * W_RSI  * adx_mult +
        mfi_z  * W_MFI  * adx_mult +
        macd_z * W_MACD * adx_mult +
        di_z   * W_ADX  +
        cvd_z  * W_CVD  +
        oi_z   * W_OI   +
        fund_z * W_FUND +
        vwap_z * W_VWAP +
        liq_z  * W_LIQ
    ) / total_w.replace(0, np.nan).fillna(1)

    score    = softclamp(raw_score * 1.5)
    smoothed = score.ewm(span=SMOOTH_LEN, adjust=False).mean()
    atr_s    = compute_atr(high, low, close, atr_period)

    df = df.copy()
    df["score"] = smoothed
    df["adx"]   = adx
    df["atr"]   = atr_s
    return df

# ─── Backtest for a given timeframe ───────────────────────────────────────────

def run_backtest(df, bpd):
    min_hold  = max(1, int(MIN_HOLD_DAYS * bpd))
    cooldown  = max(1, int(COOLDOWN_MULT * bpd / 4))  # ~10 * 4h equivalent

    scores      = df["score"].values
    closes      = df["close"].values
    highs       = df["high"].values
    lows        = df["low"].values
    adx_v       = df["adx"].values
    atr_v       = df["atr"].values
    above_long  = df["above_sma_long"].values
    below_short = df["below_sma_short"].values
    dates       = df.index

    position = 0; entry_price = 0.0; entry_bar = 0
    hwm = lwm = 0.0; trail_active = False
    consec_stops = 0; cooldown_end = 0
    trades = []; equity = [1.0]; eq = 1.0

    for i in range(1, len(df)):
        prev  = scores[i-1]; curr = scores[i]
        price = closes[i];   hi   = highs[i];  lo  = lows[i]
        atr   = atr_v[i];    adx  = adx_v[i]
        al    = above_long[i]; bs = below_short[i]
        date  = dates[i]
        phase, long_mult, short_mult = get_cycle_phase(date)

        if position == 0:
            if i < cooldown_end:
                equity.append(eq); continue
            is_trending = adx >= ADX_MIN
            bull_cross  = prev <= BULL_THRESH and curr > BULL_THRESH
            bear_cross  = prev >= BEAR_THRESH and curr < BEAR_THRESH

            if is_trending and bull_cross and al:
                position = 1; entry_price = price*(1+SLIPPAGE)
                entry_bar = i; hwm = hi; trail_active = False
            elif is_trending and bear_cross and bs:
                position = -1; entry_price = price*(1-SLIPPAGE)
                entry_bar = i; lwm = lo; trail_active = False

        elif position == 1:
            bars_held = i - entry_bar
            pnl_pct   = (price / entry_price) - 1
            if not trail_active and pnl_pct >= TRAIL_ACTIVATION:
                trail_active = True; hwm = hi
            if trail_active: hwm = max(hwm, hi)
            stop_level  = (hwm - ATR_MULT*atr) if trail_active else entry_price*(1-INITIAL_STOP_PCT)
            stop_hit    = lo <= stop_level
            tp_hit      = (hi/entry_price - 1) >= TAKE_PROFIT
            signal_exit = bars_held >= min_hold and curr < 0

            if stop_hit or tp_hit or signal_exit:
                exit_price  = max(stop_level, lo) if stop_hit else \
                              entry_price*(1+TAKE_PROFIT) if tp_hit else \
                              price*(1-SLIPPAGE)
                exit_reason = ("TRAIL" if trail_active else "STOP") if stop_hit else \
                              "TP" if tp_hit else "SIGNAL"
                if stop_hit:
                    consec_stops += 1
                    if consec_stops >= MAX_CONSEC_STOPS:
                        cooldown_end = i + cooldown; consec_stops = 0
                else:
                    consec_stops = 0
                raw_ret = (exit_price/entry_price - 1) - FEES*2
                sized   = raw_ret * long_mult; eq *= (1+sized)
                trades.append({"direction":"LONG","return_pct":raw_ret*100,
                               "sized_pct":sized*100,"bars_held":bars_held,
                               "exit_reason":exit_reason,"cycle_phase":phase})
                position = 0; trail_active = False

        elif position == -1:
            bars_held = i - entry_bar
            pnl_pct   = (entry_price / price) - 1
            if not trail_active and pnl_pct >= TRAIL_ACTIVATION:
                trail_active = True; lwm = lo
            if trail_active: lwm = min(lwm, lo)
            stop_level  = (lwm + ATR_MULT*atr) if trail_active else entry_price*(1+INITIAL_STOP_PCT)
            stop_hit    = hi >= stop_level
            tp_hit      = (entry_price/lo - 1) >= TAKE_PROFIT
            signal_exit = bars_held >= min_hold and curr > 0

            if stop_hit or tp_hit or signal_exit:
                exit_price  = min(stop_level, hi) if stop_hit else \
                              entry_price*(1-TAKE_PROFIT) if tp_hit else \
                              price*(1+SLIPPAGE)
                exit_reason = ("TRAIL" if trail_active else "STOP") if stop_hit else \
                              "TP" if tp_hit else "SIGNAL"
                if stop_hit:
                    consec_stops += 1
                    if consec_stops >= MAX_CONSEC_STOPS:
                        cooldown_end = i + cooldown; consec_stops = 0
                else:
                    consec_stops = 0
                raw_ret = (entry_price/exit_price - 1) - FEES*2
                sized   = raw_ret * short_mult; eq *= (1+sized)
                trades.append({"direction":"SHORT","return_pct":raw_ret*100,
                               "sized_pct":sized*100,"bars_held":bars_held,
                               "exit_reason":exit_reason,"cycle_phase":phase})
                position = 0; trail_active = False

        equity.append(eq)

    trades_df = pd.DataFrame(trades)
    equity_s  = pd.Series(equity, index=dates[:len(equity)])
    return trades_df, equity_s

# ─── Metrics ──────────────────────────────────────────────────────────────────

def compute_metrics(trades_df, equity_s, df, tf_name):
    if trades_df.empty or len(trades_df) < 5:
        return {"tf": tf_name, "trades": 0, "sharpe": 0, "total_ret": 0,
                "max_dd": 0, "win_rate": 0, "pf": 0, "expectancy": 0}

    raw  = trades_df["return_pct"]
    wins = trades_df[raw > 0]; loss = trades_df[raw <= 0]

    total_ret  = (equity_s.iloc[-1] - 1) * 100
    buy_hold   = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    win_rate   = len(wins) / len(trades_df) * 100
    avg_win    = wins["return_pct"].mean() if len(wins) else 0
    avg_loss   = loss["return_pct"].mean() if len(loss) else 0
    pf         = wins["return_pct"].sum() / abs(loss["return_pct"].sum()) \
                 if len(loss) and loss["return_pct"].sum() != 0 else 0
    expectancy = (win_rate/100 * avg_win) + ((1-win_rate/100) * avg_loss)

    daily_eq  = equity_s.resample("1D").last().ffill()
    daily_ret = daily_eq.pct_change().dropna()
    sharpe    = (daily_ret.mean()/daily_ret.std())*np.sqrt(365) if daily_ret.std()>0 else 0
    max_dd    = ((equity_s - equity_s.cummax())/equity_s.cummax()).min() * 100

    return {
        "tf":          tf_name,
        "trades":      len(trades_df),
        "win_rate":    round(win_rate, 1),
        "avg_win":     round(avg_win, 2),
        "avg_loss":    round(avg_loss, 2),
        "pf":          round(pf, 2),
        "expectancy":  round(expectancy, 2),
        "total_ret":   round(total_ret, 1),
        "buy_hold":    round(buy_hold, 1),
        "sharpe":      round(sharpe, 2),
        "max_dd":      round(max_dd, 1),
        "avg_bars":    round(trades_df["bars_held"].mean(), 1),
    }

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("  TIMEFRAME OPTIMIZATION — Composite Signal v6")
    print("=" * 70)
    print("  Fetching shared data (daily SMA, funding, OI)...")

    daily_sma = fetch_daily_sma()
    funding   = fetch_funding()
    oi_df     = fetch_oi()

    print(f"  Daily SMA: {len(daily_sma)} bars | Funding: {len(funding)} | OI: {len(oi_df)}")
    print()

    all_results = []

    for tf_name, tf_config in TIMEFRAMES.items():
        interval = tf_config["interval"]
        bpd      = tf_config["bars_per_day"]

        # Determine how many bars to fetch to get ~2 years of data
        n_bars = min(5000, int(730 * bpd))

        print(f"  Testing {tf_name:4s} | {n_bars:5,} bars | "
              f"RVWAP={int(RVWAP_DAYS*bpd)} bars | Z={int(Z_WINDOW_DAYS*bpd)} bars | "
              f"MinHold={max(1,int(MIN_HOLD_DAYS*bpd))} bars")

        try:
            df = fetch_ohlcv(interval, n_bars)

            # Merge daily SMA (forward fill)
            df = df.join(daily_sma, how="left")
            df["above_sma_long"]  = df["above_sma_long"].ffill().fillna(False)
            df["below_sma_short"] = df["below_sma_short"].ffill().fillna(False)

            # Merge funding
            df = df.join(funding, how="left")
            df["funding"] = df["funding"].ffill().fillna(0)

            # Merge OI
            df = df.join(oi_df, how="left")
            df["oi"]       = df["oi"].ffill().fillna(0)
            df["oi_delta"] = df["oi"].diff().fillna(0)

            # Compute signal with timeframe-scaled parameters
            df = compute_signal(df, bpd)

            # Run backtest
            trades_df, equity_s = run_backtest(df, bpd)

            # Compute metrics
            result = compute_metrics(trades_df, equity_s, df, tf_name)
            all_results.append(result)

            print(f"    → {result['trades']:3d} trades | "
                  f"Sharpe {result['sharpe']:5.2f} | "
                  f"Return {result['total_ret']:+6.1f}% | "
                  f"DD {result['max_dd']:5.1f}% | "
                  f"PF {result['pf']:4.2f}")

        except Exception as e:
            print(f"    → ERROR: {e}")
            all_results.append({"tf": tf_name, "error": str(e)})

        time.sleep(0.5)

    # ── Final comparison table
    valid = [r for r in all_results if "sharpe" in r and r["trades"] >= 5]
    valid.sort(key=lambda r: r["sharpe"], reverse=True)

    print("\n" + "=" * 80)
    print("  TIMEFRAME COMPARISON — RANKED BY SHARPE RATIO")
    print("=" * 80)
    print(f"  {'TF':>5} {'Trades':>7} {'Win%':>6} {'PF':>6} {'Expect':>8} "
          f"{'Return':>8} {'B&H':>7} {'Sharpe':>7} {'MaxDD':>7} {'AvgBars':>8}")
    print("  " + "─"*76)

    for r in valid:
        marker = " ← BEST" if r == valid[0] else ""
        print(f"  {r['tf']:>5} {r['trades']:>7} {r['win_rate']:>5.1f}% "
              f"{r['pf']:>6.2f} {r['expectancy']:>+7.2f}% "
              f"{r['total_ret']:>+7.1f}% {r['buy_hold']:>6.1f}% "
              f"{r['sharpe']:>7.2f} {r['max_dd']:>6.1f}% "
              f"{r['avg_bars']:>8.1f}{marker}")

    print("=" * 80)

    # ── Score each timeframe across multiple criteria
    print("\n  COMPOSITE SCORE (normalised rank across all metrics):")
    print(f"  Higher = better overall. Weights: Sharpe 35%, Return 20%,")
    print(f"  PF 20%, Drawdown 15%, Expectancy 10%")
    print()

    if len(valid) >= 2:
        metrics_to_score = {
            "sharpe":     (0.35, True),   # weight, higher=better
            "total_ret":  (0.20, True),
            "pf":         (0.20, True),
            "max_dd":     (0.15, False),  # lower drawdown = better
            "expectancy": (0.10, True),
        }

        for metric, (weight, higher_better) in metrics_to_score.items():
            vals = [r.get(metric, 0) for r in valid]
            mn, mx = min(vals), max(vals)
            rng = mx - mn if mx != mn else 1
            for r in valid:
                v = r.get(metric, 0)
                norm = (v - mn) / rng if higher_better else (mx - v) / rng
                r["_score"] = r.get("_score", 0) + norm * weight

        valid.sort(key=lambda r: r.get("_score", 0), reverse=True)
        print(f"  {'TF':>5} {'Composite Score':>16} {'Recommendation':>20}")
        print("  " + "─"*46)
        for i, r in enumerate(valid):
            score = r.get("_score", 0)
            if i == 0:   rec = "OPTIMAL ✓"
            elif i == 1: rec = "Runner-up"
            elif i == 2: rec = "Consider"
            else:        rec = "Suboptimal"
            print(f"  {r['tf']:>5} {score:>16.3f} {rec:>20}")

    print("\n  NOTE: Shorter timeframes have more trades but higher noise.")
    print("  Longer timeframes have fewer trades but need more capital patience.")
    print("  The optimal timeframe balances signal quality with trade frequency.")

    # Save results
    results_df = pd.DataFrame([r for r in all_results if "sharpe" in r])
    results_df.to_csv("/Users/emidobak/Desktop/timeframe_results.csv", index=False)
    print(f"\n  Full results → /Users/emidobak/Desktop/timeframe_results.csv")

if __name__ == "__main__":
    main()
