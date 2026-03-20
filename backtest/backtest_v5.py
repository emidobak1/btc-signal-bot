"""
Composite Signal Score — Python Backtest v5
Changes from v4:
  1. ATR multiplier widened 3.0 → 5.0 — gives trades more room to breathe
  2. Only trade in bull and distribution cycle phases — skip accumulation/bear
  3. Trailing stop only activates after trade is 2% in profit (breakeven protection)
     Before 2% profit: fixed initial stop at INITIAL_STOP_PCT below entry
  4. Shorts now also filtered by SMA correctly (must be below 200-day SMA)
"""

import requests
import pandas as pd
import numpy as np
import time

# ─── Config ───────────────────────────────────────────────────────────────────
SYMBOL        = "BTCUSDT"
INTERVAL      = "4h"
LIMIT_PER_REQ = 1000
NUM_REQUESTS  = 5

# Signal parameters
RSI_PERIOD    = 14
MFI_PERIOD    = 14
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIG      = 9
DMI_LEN       = 14
RVWAP_PERIOD  = 168
SMOOTH_LEN    = 5
Z_WINDOW      = 200

# Weights
W_RSI   = 1.5
W_MFI   = 1.0
W_MACD  = 1.2
W_ADX   = 1.0
W_CVD   = 1.8
W_OI    = 1.5
W_FUND  = 0.8
W_VWAP  = 1.0
W_LIQ   = 1.2

# Entry thresholds
BULL_THRESH   = 0.50
BEAR_THRESH   = -0.50
ADX_ENTRY_MIN = 20
ADX_MIN       = 15
ADX_MAX       = 45
FUND_EXTREME  = 1.5
MIN_HOLD_BARS = 6

# Cycle
LAST_HALVING  = pd.Timestamp("2024-04-20", tz="UTC")
SMA_PERIOD    = 200

# ── v5: Only trade these phases ───────────────────────────────────────────────
TRADEABLE_PHASES = {"bull", "distribution"}

# ── v5: Wider ATR stop ────────────────────────────────────────────────────────
ATR_PERIOD        = 14
ATR_MULT          = 5.0          # was 3.0 — more breathing room
INITIAL_STOP_PCT  = 0.08         # 8% fixed stop before trail activates
TRAIL_ACTIVATION  = 0.02         # trail only kicks in after 2% profit

# Take profit
TAKE_PROFIT   = 0.20

# Costs
FEES          = 0.0004
SLIPPAGE      = 0.0002

# Output
OUT_SIGNAL    = "/Users/emidobak/Desktop/signal_data_v5.csv"
OUT_TRADES    = "/Users/emidobak/Desktop/trades_v5.csv"

# ─── Cycle phase logic ────────────────────────────────────────────────────────
CYCLE_PHASES = {
    "accumulation":  (0,   6,   0.5,  0.8),
    "bull":          (6,   18,  1.5,  0.5),
    "distribution":  (18,  30,  0.5,  1.5),
    "bear":          (30,  999, 0.3,  1.8),
}

def get_cycle_phase(date):
    months_since = (date - LAST_HALVING).days / 30.44
    for phase, (start, end, lm, sm) in CYCLE_PHASES.items():
        if start <= months_since < end:
            return phase, lm, sm
    return "bear", 0.3, 1.8

# ─── Data fetching ────────────────────────────────────────────────────────────

def fetch_ohlcv(symbol, interval, limit_per_req, num_requests):
    print(f"Fetching {num_requests * limit_per_req} bars of {symbol} {interval}...")
    url      = "https://fapi.binance.com/fapi/v1/klines"
    all_data = []
    end_time = None

    for i in range(num_requests):
        params = {"symbol": symbol, "interval": interval, "limit": limit_per_req}
        if end_time:
            params["endTime"] = end_time
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        all_data = data + all_data
        end_time = data[0][0] - 1
        time.sleep(0.1)
        print(f"  Fetched batch {i+1}/{num_requests} — {len(all_data)} bars total")

    df = pd.DataFrame(all_data, columns=[
        "timestamp","open","high","low","close","volume",
        "close_time","quote_volume","trades",
        "taker_buy_base","taker_buy_quote","ignore"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df = df[["open","high","low","close","volume","taker_buy_base"]].astype(float)
    df["buy_volume"]  = df["taker_buy_base"]
    df["sell_volume"] = df["volume"] - df["taker_buy_base"]
    df = df.drop_duplicates().sort_index()
    print(f"  Total bars loaded: {len(df):,} | {df.index[0]} → {df.index[-1]}")
    return df


def fetch_daily_sma(symbol, sma_period, num_requests=8):
    print(f"Fetching daily candles for {sma_period}-day SMA filter...")
    url      = "https://fapi.binance.com/fapi/v1/klines"
    all_data = []
    end_time = None

    for i in range(num_requests):
        params = {"symbol": symbol, "interval": "1d", "limit": 1000}
        if end_time:
            params["endTime"] = end_time
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        all_data = data + all_data
        end_time = data[0][0] - 1
        time.sleep(0.1)

    df = pd.DataFrame(all_data, columns=[
        "timestamp","open","high","low","close","volume",
        "close_time","quote_volume","trades",
        "taker_buy_base","taker_buy_quote","ignore"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df["close"] = df["close"].astype(float)
    df = df[["close"]].drop_duplicates().sort_index()
    df["sma200"]       = df["close"].rolling(sma_period, min_periods=sma_period).mean()
    df["above_sma200"] = df["close"] > df["sma200"]
    print(f"  Daily bars loaded: {len(df)}")
    return df[["sma200","above_sma200"]]


def fetch_funding(symbol):
    print("Fetching funding rates...")
    url      = "https://fapi.binance.com/fapi/v1/fundingRate"
    all_data = []
    end_time = None

    for _ in range(NUM_REQUESTS):
        params = {"symbol": symbol, "limit": 1000}
        if end_time:
            params["endTime"] = end_time
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        all_data = data + all_data
        end_time = data[0]["fundingTime"] - 1
        time.sleep(0.1)

    df = pd.DataFrame(all_data)
    df["timestamp"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df["funding"] = df["fundingRate"].astype(float)
    df = df[["funding"]].drop_duplicates().sort_index()
    print(f"  Funding rates loaded: {len(df)}")
    return df


def fetch_oi(symbol, interval):
    print("Fetching open interest...")
    url    = "https://fapi.binance.com/futures/data/openInterestHist"
    params = {"symbol": symbol, "period": interval, "limit": 500}
    r      = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data   = r.json()
    df = pd.DataFrame(data)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df["oi"] = df["sumOpenInterestValue"].astype(float)
    df = df[["oi"]].drop_duplicates().sort_index()
    print(f"  Open interest loaded: {len(df)} bars")
    return df

# ─── Signal helpers ───────────────────────────────────────────────────────────

def rolling_zscore(series, window):
    mu = series.rolling(window, min_periods=1).mean()
    sd = series.rolling(window, min_periods=1).std()
    z  = (series - mu) / sd.replace(0, np.nan)
    return z.fillna(0).clip(-3, 3)

def compute_rsi(close, period):
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def compute_mfi(high, low, close, volume, period):
    tp      = (high + low + close) / 3
    mf      = tp * volume
    pos_mf  = mf.where(tp > tp.shift(1), 0)
    neg_mf  = mf.where(tp < tp.shift(1), 0)
    pos_sum = pos_mf.rolling(period, min_periods=1).sum()
    neg_sum = neg_mf.rolling(period, min_periods=1).sum()
    mfr     = pos_sum / neg_sum.replace(0, np.nan)
    return (100 - 100 / (1 + mfr)).fillna(50)

def compute_macd(close, fast, slow, signal):
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    macd  = ema_f - ema_s
    sig   = macd.ewm(span=signal, adjust=False).mean()
    return macd - sig

def compute_dmi(high, low, close, period):
    tr       = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    up       = high - high.shift(1)
    down     = low.shift(1) - low
    plus_dm  = up.where((up > down) & (up > 0), 0)
    minus_dm = down.where((down > up) & (down > 0), 0)
    atr_raw  = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_raw.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_raw.replace(0, np.nan)
    dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx      = dx.ewm(alpha=1/period, adjust=False).mean()
    return plus_di.fillna(0), minus_di.fillna(0), adx.fillna(0)

def compute_atr(high, low, close, period):
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def compute_rvwap(high, low, close, buy_vol, sell_vol, period):
    hlc3  = (high + low + close) / 3
    vol   = buy_vol + sell_vol
    pv    = hlc3 * vol
    return (pv.rolling(period, min_periods=1).sum() /
            vol.rolling(period, min_periods=1).sum().replace(0, np.nan)).ffill()

def softclamp(x):
    return np.tanh(x)

# ─── Signal pipeline ──────────────────────────────────────────────────────────

def compute_signal(df):
    print("Computing signals...")
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    vol    = df["volume"]
    buy_v  = df["buy_volume"]
    sell_v = df["sell_volume"]

    rsi_z  = rolling_zscore(compute_rsi(close, RSI_PERIOD) - 50, Z_WINDOW)
    mfi_z  = rolling_zscore(compute_mfi(high, low, close, vol, MFI_PERIOD) - 50, Z_WINDOW)
    macd_z = rolling_zscore(compute_macd(close, MACD_FAST, MACD_SLOW, MACD_SIG), Z_WINDOW)

    plus_di, minus_di, adx = compute_dmi(high, low, close, DMI_LEN)
    di_z     = rolling_zscore(plus_di - minus_di, Z_WINDOW)
    adx_mult = ((adx - ADX_MIN) / (ADX_MAX - ADX_MIN)).clip(0, 1)

    cvd_z     = rolling_zscore(buy_v - sell_v, Z_WINDOW)
    oi_delta  = df["oi_delta"]
    price_dir = np.sign(close - df["open"])
    oi_z      = rolling_zscore(oi_delta * price_dir, Z_WINDOW)

    fund       = df["funding"].fillna(0)
    raw_fund_z = rolling_zscore(-fund, Z_WINDOW)
    fund_z     = raw_fund_z.where(raw_fund_z.abs() >= FUND_EXTREME, 0)

    vwap_z = rolling_zscore(
        close - compute_rvwap(high, low, close, buy_v, sell_v, RVWAP_PERIOD),
        Z_WINDOW
    )
    liq_z  = pd.Series(0.0, index=df.index)

    trend_w = W_RSI * adx_mult + W_MFI * adx_mult + W_MACD * adx_mult + W_ADX
    micro_w = W_CVD + W_OI + W_FUND + W_VWAP + W_LIQ
    total_w = trend_w + micro_w

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
    atr      = compute_atr(high, low, close, ATR_PERIOD)

    df = df.copy()
    df["score"]  = smoothed
    df["adx"]    = adx
    df["atr"]    = atr
    df["rsi_z"]  = rsi_z
    df["mfi_z"]  = mfi_z
    df["macd_z"] = macd_z
    df["di_z"]   = di_z
    df["cvd_z"]  = cvd_z
    df["oi_z"]   = oi_z
    df["fund_z"] = fund_z
    df["vwap_z"] = vwap_z
    return df

# ─── Backtest engine ──────────────────────────────────────────────────────────

def run_backtest(df):
    """
    v5 backtest engine.

    Entry gates (ALL must pass):
      1. Score crossover of ±threshold
      2. ADX > ADX_ENTRY_MIN
      3. Daily 200-SMA direction filter
      4. Cycle phase in TRADEABLE_PHASES (bull or distribution only)

    Stop logic (v5 — two stage):
      Stage 1: Fixed INITIAL_STOP_PCT from entry until trade reaches TRAIL_ACTIVATION profit
      Stage 2: ATR trailing stop kicks in once trade is TRAIL_ACTIVATION% in profit
               This prevents getting stopped out on normal early-trade noise

    Exit hierarchy:
      1. Stop (fixed or trailing depending on stage)
      2. Take profit at 20%
      3. Signal exit after MIN_HOLD_BARS
    """
    print("Running backtest...")

    scores    = df["score"].values
    closes    = df["close"].values
    highs     = df["high"].values
    lows      = df["low"].values
    adx_v     = df["adx"].values
    atr_v     = df["atr"].values
    above_sma = df["above_sma200"].values
    dates     = df.index

    position     = 0
    entry_price  = 0.0
    entry_bar    = 0
    entry_date   = None
    hwm          = 0.0
    lwm          = 0.0
    trail_active = False

    trades  = []
    equity  = [1.0]
    eq      = 1.0

    # Track blocked entries for reporting
    blocked_sma   = 0
    blocked_phase = 0

    for i in range(1, len(df)):
        prev_score = scores[i-1]
        curr_score = scores[i]
        price      = closes[i]
        high_i     = highs[i]
        low_i      = lows[i]
        adx_now    = adx_v[i]
        atr_now    = atr_v[i]
        sma_above  = above_sma[i]
        date_i     = dates[i]

        phase, long_mult, short_mult = get_cycle_phase(date_i)

        # ── Entry
        if position == 0:
            is_trending = adx_now >= ADX_ENTRY_MIN
            bull_cross  = prev_score <= BULL_THRESH and curr_score > BULL_THRESH
            bear_cross  = prev_score >= BEAR_THRESH and curr_score < BEAR_THRESH

            if is_trending and (bull_cross or bear_cross):
                # Check phase filter first
                if phase not in TRADEABLE_PHASES:
                    blocked_phase += 1
                else:
                    # Long entry: price must be above 200-day SMA
                    if bull_cross and sma_above:
                        position     = 1
                        entry_price  = price * (1 + SLIPPAGE)
                        entry_bar    = i
                        entry_date   = date_i
                        hwm          = high_i
                        trail_active = False

                    # Short entry: price must be below 200-day SMA
                    elif bear_cross and not sma_above:
                        position     = -1
                        entry_price  = price * (1 - SLIPPAGE)
                        entry_bar    = i
                        entry_date   = date_i
                        lwm          = low_i
                        trail_active = False

                    elif bull_cross and not sma_above:
                        blocked_sma += 1
                    elif bear_cross and sma_above:
                        blocked_sma += 1

        # ── Manage long
        elif position == 1:
            bars_held   = i - entry_bar
            current_pnl = (price / entry_price) - 1

            # Stage 1 → Stage 2 transition: activate trail once 2% in profit
            if not trail_active and current_pnl >= TRAIL_ACTIVATION:
                trail_active = True
                hwm          = high_i   # reset HWM from current bar

            if trail_active:
                hwm        = max(hwm, high_i)
                stop_level = hwm - ATR_MULT * atr_now
            else:
                # Fixed stop before trail activates
                stop_level = entry_price * (1 - INITIAL_STOP_PCT)

            stop_hit    = low_i <= stop_level
            tp_hit      = (high_i / entry_price - 1) >= TAKE_PROFIT
            signal_exit = bars_held >= MIN_HOLD_BARS and curr_score < BEAR_THRESH

            if stop_hit or tp_hit or signal_exit:
                if stop_hit:
                    exit_price  = max(stop_level, low_i)  # realistic fill
                    exit_reason = "TRAIL_STOP" if trail_active else "INIT_STOP"
                elif tp_hit:
                    exit_price  = entry_price * (1 + TAKE_PROFIT)
                    exit_reason = "TP"
                else:
                    exit_price  = price * (1 - SLIPPAGE)
                    exit_reason = "SIGNAL"

                raw_ret = (exit_price / entry_price - 1) - FEES * 2
                sized   = raw_ret * long_mult
                eq     *= (1 + sized)

                trades.append({
                    "entry_date":    entry_date,
                    "exit_date":     date_i,
                    "direction":     "LONG",
                    "entry_price":   entry_price,
                    "exit_price":    exit_price,
                    "return_pct":    raw_ret * 100,
                    "sized_pct":     sized * 100,
                    "bars_held":     bars_held,
                    "adx_entry":     adx_v[entry_bar],
                    "cycle_phase":   phase,
                    "long_mult":     long_mult,
                    "trail_active":  trail_active,
                    "exit_reason":   exit_reason
                })
                position     = 0
                trail_active = False

        # ── Manage short
        elif position == -1:
            bars_held   = i - entry_bar
            current_pnl = (entry_price / price) - 1

            if not trail_active and current_pnl >= TRAIL_ACTIVATION:
                trail_active = True
                lwm          = low_i

            if trail_active:
                lwm          = min(lwm, low_i)
                stop_level_s = lwm + ATR_MULT * atr_now
            else:
                stop_level_s = entry_price * (1 + INITIAL_STOP_PCT)

            stop_hit    = high_i >= stop_level_s
            tp_hit      = (entry_price / low_i - 1) >= TAKE_PROFIT
            signal_exit = bars_held >= MIN_HOLD_BARS and curr_score > BULL_THRESH

            if stop_hit or tp_hit or signal_exit:
                if stop_hit:
                    exit_price  = min(stop_level_s, high_i)
                    exit_reason = "TRAIL_STOP" if trail_active else "INIT_STOP"
                elif tp_hit:
                    exit_price  = entry_price * (1 - TAKE_PROFIT)
                    exit_reason = "TP"
                else:
                    exit_price  = price * (1 + SLIPPAGE)
                    exit_reason = "SIGNAL"

                raw_ret = (entry_price / exit_price - 1) - FEES * 2
                sized   = raw_ret * short_mult
                eq     *= (1 + sized)

                trades.append({
                    "entry_date":    entry_date,
                    "exit_date":     date_i,
                    "direction":     "SHORT",
                    "entry_price":   entry_price,
                    "exit_price":    exit_price,
                    "return_pct":    raw_ret * 100,
                    "sized_pct":     sized * 100,
                    "bars_held":     bars_held,
                    "adx_entry":     adx_v[entry_bar],
                    "cycle_phase":   phase,
                    "short_mult":    short_mult,
                    "trail_active":  trail_active,
                    "exit_reason":   exit_reason
                })
                position     = 0
                trail_active = False

        equity.append(eq)

    trades_df = pd.DataFrame(trades)
    equity_s  = pd.Series(equity, index=dates[:len(equity)])
    return trades_df, equity_s, blocked_sma, blocked_phase

# ─── Metrics ──────────────────────────────────────────────────────────────────

def compute_metrics(trades_df, equity_s, df, blocked_sma, blocked_phase):
    if trades_df.empty:
        print("No trades generated.")
        return

    raw_rets = trades_df["return_pct"]
    wins     = trades_df[raw_rets > 0]
    losses   = trades_df[raw_rets <= 0]

    total_ret     = (equity_s.iloc[-1] - 1) * 100
    buy_hold      = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    win_rate      = len(wins) / len(trades_df) * 100
    avg_win       = wins["return_pct"].mean()   if len(wins)   else 0
    avg_loss      = losses["return_pct"].mean() if len(losses) else 0
    profit_factor = (wins["return_pct"].sum() / abs(losses["return_pct"].sum())
                     if len(losses) and losses["return_pct"].sum() != 0 else float("inf"))
    avg_bars      = trades_df["bars_held"].mean()
    avg_adx       = trades_df["adx_entry"].mean()
    expectancy    = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)

    daily_eq  = equity_s.resample("1D").last().ffill()
    daily_ret = daily_eq.pct_change().dropna()
    sharpe    = ((daily_ret.mean() / daily_ret.std()) * np.sqrt(365)
                 if daily_ret.std() > 0 else 0)

    roll_max  = equity_s.cummax()
    max_dd    = ((equity_s - roll_max) / roll_max).min() * 100

    longs   = trades_df[trades_df["direction"] == "LONG"]
    shorts  = trades_df[trades_df["direction"] == "SHORT"]

    # Exit breakdown
    init_stops   = trades_df[trades_df["exit_reason"] == "INIT_STOP"]
    trail_stops  = trades_df[trades_df["exit_reason"] == "TRAIL_STOP"]
    tps          = trades_df[trades_df["exit_reason"] == "TP"]
    signals      = trades_df[trades_df["exit_reason"] == "SIGNAL"]
    all_stops    = pd.concat([init_stops, trail_stops])

    # Consecutive loss streak
    results = (raw_rets > 0).astype(int).values
    max_streak, curr = 0, 0
    for r in results:
        curr = curr + 1 if r == 0 else 0
        max_streak = max(max_streak, curr)

    print("\n" + "═" * 66)
    print("  COMPOSITE SIGNAL v5 — BACKTEST RESULTS")
    print("═" * 66)
    print(f"  Period:               {df.index[0].date()} → {df.index[-1].date()}")
    print(f"  Timeframe:            {INTERVAL}")
    print(f"  Total bars:           {len(df):,}")
    print(f"  Entry threshold:      ±{BULL_THRESH}")
    print(f"  ADX entry filter:     >{ADX_ENTRY_MIN}")
    print(f"  Min hold:             {MIN_HOLD_BARS} bars / {MIN_HOLD_BARS*4}h")
    print(f"  Stop — initial:       {INITIAL_STOP_PCT*100:.0f}% fixed until {TRAIL_ACTIVATION*100:.0f}% profit")
    print(f"  Stop — trailing:      {ATR_MULT}× ATR{ATR_PERIOD} after {TRAIL_ACTIVATION*100:.0f}% profit")
    print(f"  Take profit:          {TAKE_PROFIT*100:.0f}%")
    print(f"  Tradeable phases:     {', '.join(TRADEABLE_PHASES)}")
    print(f"  Entries blocked (SMA filter):   {blocked_sma}")
    print(f"  Entries blocked (phase filter): {blocked_phase}")
    print("─" * 66)
    print(f"  Total trades:         {len(trades_df)}  (v4: 50)")
    print(f"    Longs:              {len(longs)}")
    print(f"    Shorts:             {len(shorts)}")
    print(f"  Avg ADX at entry:     {avg_adx:.1f}")
    print(f"  Win rate:             {win_rate:.1f}%  (v4: 40.0%)")
    print(f"  Avg win:              +{avg_win:.2f}%  (v4: +4.25%)")
    print(f"  Avg loss:             {avg_loss:.2f}%  (v4: -2.32%)")
    print(f"  Profit factor:        {profit_factor:.2f}  (v4: 1.22)")
    print(f"  Expectancy/trade:     {expectancy:+.2f}%  (v4: +0.30%)")
    print(f"  Avg bars held:        {avg_bars:.1f}  (v4: 15.3)")
    print(f"  Max consec. losses:   {max_streak}")
    print("─" * 66)
    print(f"  Exit breakdown:")
    print(f"    Signal exits:       {len(signals)} ({len(signals)/len(trades_df)*100:.0f}%)")
    print(f"    Initial stop exits: {len(init_stops)} ({len(init_stops)/len(trades_df)*100:.0f}%)")
    print(f"    Trail stop exits:   {len(trail_stops)} ({len(trail_stops)/len(trades_df)*100:.0f}%)")
    print(f"    Take profit exits:  {len(tps)} ({len(tps)/len(trades_df)*100:.0f}%)")
    if len(all_stops) > 0:
        print(f"    Avg stop return:    {all_stops['return_pct'].mean():.2f}%")
    if len(tps) > 0:
        print(f"    Avg TP return:      +{tps['return_pct'].mean():.2f}%")
    if len(signals) > 0:
        print(f"    Avg signal return:  {signals['return_pct'].mean():+.2f}%")
    print("─" * 66)
    print(f"  Total return (sized): {total_ret:+.1f}%  (v4: +85.9%)")
    print(f"  Buy & hold:           {buy_hold:+.1f}%")
    print(f"  Sharpe ratio:         {sharpe:.2f}  (v4: 0.90)")
    print(f"  Max drawdown:         {max_dd:.1f}%  (v4: -19.8%)")
    print("─" * 66)

    if len(longs) > 0:
        lwr = len(longs[longs["return_pct"] > 0]) / len(longs) * 100
        print(f"  Long win rate:        {lwr:.1f}%  |  Total raw: {longs['return_pct'].sum():+.1f}%")
    if len(shorts) > 0:
        swr = len(shorts[shorts["return_pct"] > 0]) / len(shorts) * 100
        print(f"  Short win rate:       {swr:.1f}%  |  Total raw: {shorts['return_pct'].sum():+.1f}%")

    print("═" * 66)

    # Cycle phase breakdown
    print(f"\n  PERFORMANCE BY CYCLE PHASE:")
    print(f"  {'Phase':<16} {'Trades':>6} {'Win%':>6} {'Avg Ret':>8} {'Total':>8} {'Sizing':>7}")
    print("  " + "─" * 56)
    for phase in ["accumulation","bull","distribution","bear"]:
        ph = trades_df[trades_df["cycle_phase"] == phase]
        if len(ph) == 0:
            print(f"  {phase:<16} {'—':>6} {'—':>6} {'—':>8} {'—':>8} {'BLOCKED':>7}")
            continue
        ph_wr  = len(ph[ph["return_pct"] > 0]) / len(ph) * 100
        ph_avg = ph["return_pct"].mean()
        ph_tot = ph["return_pct"].sum()
        lm_val = ph["long_mult"].mean() if "long_mult" in ph.columns else ph.get("short_mult", pd.Series([1])).mean()
        print(f"  {phase:<16} {len(ph):>6} {ph_wr:>5.1f}% {ph_avg:>+7.2f}% {ph_tot:>+7.1f}% {lm_val:>6.1f}×")

    print(f"\n  LAST 10 TRADES:")
    print(f"  {'Entry':<20} {'Exit':<20} {'Dir':<6} {'Entry $':<9} "
          f"{'Exit $':<9} {'Bars':>4} {'Phase':<14} {'Reason':<11} {'Raw%':>7} {'Sized%':>7}")
    print("  " + "─" * 113)
    for _, t in trades_df.tail(10).iterrows():
        print(f"  {str(t['entry_date'])[:19]:<20} {str(t['exit_date'])[:19]:<20} "
              f"{t['direction']:<6} {t['entry_price']:<9,.0f} {t['exit_price']:<9,.0f} "
              f"{int(t['bars_held']):>4} {t['cycle_phase']:<14} {t['exit_reason']:<11} "
              f"{t['return_pct']:>+6.2f}% {t['sized_pct']:>+6.2f}%")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    df        = fetch_ohlcv(SYMBOL, INTERVAL, LIMIT_PER_REQ, NUM_REQUESTS)
    daily_sma = fetch_daily_sma(SYMBOL, SMA_PERIOD)

    df = df.join(daily_sma, how="left")
    df["sma200"]       = df["sma200"].ffill().fillna(0)
    df["above_sma200"] = df["above_sma200"].ffill().fillna(False)

    funding = fetch_funding(SYMBOL)
    df = df.join(funding, how="left")
    df["funding"] = df["funding"].ffill().fillna(0)

    oi_df = fetch_oi(SYMBOL, INTERVAL)
    df = df.join(oi_df, how="left")
    df["oi"]       = df["oi"].ffill().fillna(0)
    df["oi_delta"] = df["oi"].diff().fillna(0)

    df = compute_signal(df)

    trades_df, equity_s, blocked_sma, blocked_phase = run_backtest(df)

    compute_metrics(trades_df, equity_s, df, blocked_sma, blocked_phase)

    out = df[["open","high","low","close","volume","score","adx","atr",
              "sma200","above_sma200","rsi_z","mfi_z","macd_z",
              "di_z","cvd_z","oi_z","fund_z","vwap_z"]].copy()
    out.to_csv(OUT_SIGNAL)
    if not trades_df.empty:
        trades_df.to_csv(OUT_TRADES, index=False)
    print(f"\n  Signal data → {OUT_SIGNAL}")
    print(f"  Trade log   → {OUT_TRADES}")

if __name__ == "__main__":
    main()
