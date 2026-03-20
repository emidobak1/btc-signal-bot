"""
Composite Signal Score — Python Backtest v6
Base: v4 (best overall balance)
Targets: 60-80 trades, avg hold 20-35 bars, signal-driven exits, cleaner metrics

Changes from v4:
  1. Entry threshold lowered 0.50 → 0.40 — more signals, more trades
  2. ATR multiplier 3.0 → 4.0 — trades breathe without over-holding
  3. Take profit tightened 20% → 12% — lock in gains on shorter moves
  4. Short SMA filter uses 100-day instead of 200-day — more responsive
  5. Trail activation lowered 2% → 1% — trail kicks in sooner
  6. Signal exit threshold loosened — exit when score crosses zero not -0.50
     This lets the signal drive more exits for cleaner trade management
  7. All 4 cycle phases allowed but sized appropriately — more trades
  8. Consecutive stop circuit breaker — pause 10 bars after 3 stops in a row
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

# ── v6: Entry — lower threshold = more trades ─────────────────────────────────
BULL_THRESH   = 0.40          # was 0.50 in v4/v5
BEAR_THRESH   = -0.40
ADX_ENTRY_MIN = 20
ADX_MIN       = 15
ADX_MAX       = 45
FUND_EXTREME  = 1.5
MIN_HOLD_BARS = 4             # reduced from 6 — shorter trades need less hold

# ── v6: Exit — signal exits at zero cross, not opposite threshold ─────────────
# This is the key change — the signal drives exits more frequently
LONG_EXIT_SCORE  = 0.0        # exit long when score drops below zero
SHORT_EXIT_SCORE = 0.0        # exit short when score rises above zero

# Cycle — all phases allowed, sized differently
LAST_HALVING  = pd.Timestamp("2024-04-20", tz="UTC")
SMA_LONG      = 200           # long entries: must be above 200-day SMA
SMA_SHORT     = 100           # short entries: must be below 100-day SMA (more responsive)

# ── v6: Stop — tighter trail, activates sooner ────────────────────────────────
ATR_PERIOD       = 14
ATR_MULT         = 4.0        # was 3.0 in v4, 5.0 in v5 — middle ground
INITIAL_STOP_PCT = 0.07       # 7% initial fixed stop
TRAIL_ACTIVATION = 0.01       # trail activates after just 1% profit

# ── v6: Tighter TP — capture shorter moves ────────────────────────────────────
TAKE_PROFIT   = 0.12          # was 0.20 — lock gains faster

# ── v6: Circuit breaker — pause after 3 consecutive stops ────────────────────
MAX_CONSEC_STOPS = 3
COOLDOWN_BARS    = 10

# Costs
FEES      = 0.0004
SLIPPAGE  = 0.0002

# Output
OUT_SIGNAL = "/Users/emidobak/Desktop/signal_data_v6.csv"
OUT_TRADES = "/Users/emidobak/Desktop/trades_v6.csv"

# ─── Cycle phase config ───────────────────────────────────────────────────────
CYCLE_PHASES = {
    "accumulation":  (0,   6,   0.6,  0.8),   # cautious but not blocked
    "bull":          (6,   18,  1.4,  0.6),
    "distribution":  (18,  30,  0.6,  1.4),
    "bear":          (30,  999, 0.4,  1.6),
}

def get_cycle_phase(date):
    months_since = (date - LAST_HALVING).days / 30.44
    for phase, (start, end, lm, sm) in CYCLE_PHASES.items():
        if start <= months_since < end:
            return phase, lm, sm
    return "bear", 0.4, 1.6

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


def fetch_daily_sma(symbol, long_period=200, short_period=100, num_requests=8):
    print(f"Fetching daily candles for SMA filters ({short_period}/{long_period}-day)...")
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

    df["sma_long"]       = df["close"].rolling(long_period,  min_periods=long_period).mean()
    df["sma_short"]      = df["close"].rolling(short_period, min_periods=short_period).mean()
    df["above_sma_long"] = df["close"] > df["sma_long"]    # for longs
    df["below_sma_short"]= df["close"] < df["sma_short"]   # for shorts

    print(f"  Daily bars loaded: {len(df)}")
    return df[["sma_long","sma_short","above_sma_long","below_sma_short"]]


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
    v6 backtest engine.

    Key changes vs v4:
    - Lower entry threshold (0.40) → more trades
    - Signal exit at zero cross not opposite threshold → signal drives exits
    - 4× ATR trail (not 3×) → more breathing room
    - 12% TP (not 20%) → captures shorter moves
    - 100-day SMA for shorts → more short entries allowed
    - Circuit breaker → pause after 3 consecutive stops
    """
    print("Running backtest...")

    scores       = df["score"].values
    closes       = df["close"].values
    highs        = df["high"].values
    lows         = df["low"].values
    adx_v        = df["adx"].values
    atr_v        = df["atr"].values
    above_long   = df["above_sma_long"].values   # 200-day for longs
    below_short  = df["below_sma_short"].values  # 100-day for shorts
    dates        = df.index

    position      = 0
    entry_price   = 0.0
    entry_bar     = 0
    entry_date    = None
    hwm           = 0.0
    lwm           = 0.0
    trail_active  = False

    # Circuit breaker state
    consec_stops  = 0
    cooldown_end  = 0

    trades        = []
    equity        = [1.0]
    eq            = 1.0

    blocked_sma   = 0
    blocked_cb    = 0   # circuit breaker blocks

    for i in range(1, len(df)):
        prev_score  = scores[i-1]
        curr_score  = scores[i]
        price       = closes[i]
        high_i      = highs[i]
        low_i       = lows[i]
        adx_now     = adx_v[i]
        atr_now     = atr_v[i]
        al          = above_long[i]
        bs          = below_short[i]
        date_i      = dates[i]

        phase, long_mult, short_mult = get_cycle_phase(date_i)

        # ── Entry
        if position == 0:

            # Circuit breaker — skip entry if in cooldown
            if i < cooldown_end:
                blocked_cb += 1
                equity.append(eq)
                continue

            is_trending = adx_now >= ADX_ENTRY_MIN
            bull_cross  = prev_score <= BULL_THRESH and curr_score > BULL_THRESH
            bear_cross  = prev_score >= BEAR_THRESH and curr_score < BEAR_THRESH

            if is_trending and bull_cross and al:
                position     = 1
                entry_price  = price * (1 + SLIPPAGE)
                entry_bar    = i
                entry_date   = date_i
                hwm          = high_i
                trail_active = False

            elif is_trending and bear_cross and bs:
                position     = -1
                entry_price  = price * (1 - SLIPPAGE)
                entry_bar    = i
                entry_date   = date_i
                lwm          = low_i
                trail_active = False

            elif is_trending and (bull_cross or bear_cross):
                blocked_sma += 1

        # ── Manage long
        elif position == 1:
            bars_held   = i - entry_bar
            current_pnl = (price / entry_price) - 1

            if not trail_active and current_pnl >= TRAIL_ACTIVATION:
                trail_active = True
                hwm          = high_i

            if trail_active:
                hwm        = max(hwm, high_i)
                stop_level = hwm - ATR_MULT * atr_now
            else:
                stop_level = entry_price * (1 - INITIAL_STOP_PCT)

            stop_hit    = low_i  <= stop_level
            tp_hit      = (high_i / entry_price - 1) >= TAKE_PROFIT
            # v6: exit on zero cross, not opposite threshold
            signal_exit = bars_held >= MIN_HOLD_BARS and curr_score < LONG_EXIT_SCORE

            if stop_hit or tp_hit or signal_exit:
                if stop_hit:
                    exit_price  = max(stop_level, low_i)
                    exit_reason = "TRAIL_STOP" if trail_active else "INIT_STOP"
                    consec_stops += 1
                    if consec_stops >= MAX_CONSEC_STOPS:
                        cooldown_end = i + COOLDOWN_BARS
                        print(f"  Circuit breaker triggered at bar {i} ({date_i.date()}) — "
                              f"pausing {COOLDOWN_BARS} bars after {consec_stops} consecutive stops")
                        consec_stops = 0
                elif tp_hit:
                    exit_price   = entry_price * (1 + TAKE_PROFIT)
                    exit_reason  = "TP"
                    consec_stops = 0
                else:
                    exit_price   = price * (1 - SLIPPAGE)
                    exit_reason  = "SIGNAL"
                    consec_stops = 0

                raw_ret = (exit_price / entry_price - 1) - FEES * 2
                sized   = raw_ret * long_mult
                eq     *= (1 + sized)

                trades.append({
                    "entry_date":   entry_date,
                    "exit_date":    date_i,
                    "direction":    "LONG",
                    "entry_price":  entry_price,
                    "exit_price":   exit_price,
                    "return_pct":   raw_ret * 100,
                    "sized_pct":    sized * 100,
                    "bars_held":    bars_held,
                    "adx_entry":    adx_v[entry_bar],
                    "cycle_phase":  phase,
                    "long_mult":    long_mult,
                    "trail_active": trail_active,
                    "exit_reason":  exit_reason
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
            signal_exit = bars_held >= MIN_HOLD_BARS and curr_score > SHORT_EXIT_SCORE

            if stop_hit or tp_hit or signal_exit:
                if stop_hit:
                    exit_price   = min(stop_level_s, high_i)
                    exit_reason  = "TRAIL_STOP" if trail_active else "INIT_STOP"
                    consec_stops += 1
                    if consec_stops >= MAX_CONSEC_STOPS:
                        cooldown_end = i + COOLDOWN_BARS
                        print(f"  Circuit breaker triggered at bar {i} ({date_i.date()}) — "
                              f"pausing {COOLDOWN_BARS} bars after {consec_stops} consecutive stops")
                        consec_stops = 0
                elif tp_hit:
                    exit_price   = entry_price * (1 - TAKE_PROFIT)
                    exit_reason  = "TP"
                    consec_stops = 0
                else:
                    exit_price   = price * (1 + SLIPPAGE)
                    exit_reason  = "SIGNAL"
                    consec_stops = 0

                raw_ret = (entry_price / exit_price - 1) - FEES * 2
                sized   = raw_ret * short_mult
                eq     *= (1 + sized)

                trades.append({
                    "entry_date":   entry_date,
                    "exit_date":    date_i,
                    "direction":    "SHORT",
                    "entry_price":  entry_price,
                    "exit_price":   exit_price,
                    "return_pct":   raw_ret * 100,
                    "sized_pct":    sized * 100,
                    "bars_held":    bars_held,
                    "adx_entry":    adx_v[entry_bar],
                    "cycle_phase":  phase,
                    "short_mult":   short_mult,
                    "trail_active": trail_active,
                    "exit_reason":  exit_reason
                })
                position     = 0
                trail_active = False

        equity.append(eq)

    trades_df = pd.DataFrame(trades)
    equity_s  = pd.Series(equity, index=dates[:len(equity)])
    return trades_df, equity_s, blocked_sma, blocked_cb

# ─── Metrics ──────────────────────────────────────────────────────────────────

def compute_metrics(trades_df, equity_s, df, blocked_sma, blocked_cb):
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

    longs  = trades_df[trades_df["direction"] == "LONG"]
    shorts = trades_df[trades_df["direction"] == "SHORT"]

    init_stops  = trades_df[trades_df["exit_reason"] == "INIT_STOP"]
    trail_stops = trades_df[trades_df["exit_reason"] == "TRAIL_STOP"]
    tps         = trades_df[trades_df["exit_reason"] == "TP"]
    signals     = trades_df[trades_df["exit_reason"] == "SIGNAL"]
    all_stops   = pd.concat([init_stops, trail_stops])

    results = (raw_rets > 0).astype(int).values
    max_streak, curr = 0, 0
    for r in results:
        curr = curr + 1 if r == 0 else 0
        max_streak = max(max_streak, curr)

    print("\n" + "═" * 66)
    print("  COMPOSITE SIGNAL v6 — BACKTEST RESULTS")
    print("═" * 66)
    print(f"  Period:               {df.index[0].date()} → {df.index[-1].date()}")
    print(f"  Timeframe:            {INTERVAL}")
    print(f"  Total bars:           {len(df):,}")
    print(f"  Entry threshold:      ±{BULL_THRESH}  (v4: ±0.50)")
    print(f"  ADX filter:           >{ADX_ENTRY_MIN}")
    print(f"  Min hold:             {MIN_HOLD_BARS} bars")
    print(f"  Stop — initial:       {INITIAL_STOP_PCT*100:.0f}% until {TRAIL_ACTIVATION*100:.0f}% profit")
    print(f"  Stop — trailing:      {ATR_MULT}× ATR{ATR_PERIOD}")
    print(f"  Take profit:          {TAKE_PROFIT*100:.0f}%  (v4: 20%)")
    print(f"  Signal exit:          score crosses zero  (v4: opposite threshold)")
    print(f"  SMA filters:          longs >{SMA_LONG}d, shorts <{SMA_SHORT}d")
    print(f"  Circuit breaker:      pause {COOLDOWN_BARS} bars after {MAX_CONSEC_STOPS} consec stops")
    print(f"  Entries blocked (SMA):          {blocked_sma}")
    print(f"  Entries blocked (circuit break):{blocked_cb}")
    print("─" * 66)
    print(f"  Total trades:         {len(trades_df)}  (v4: 50, target: 60-80)")
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

    print(f"\n  PERFORMANCE BY CYCLE PHASE:")
    print(f"  {'Phase':<16} {'Trades':>6} {'Win%':>6} {'Avg Ret':>8} {'Total':>8} {'Sizing':>7}")
    print("  " + "─" * 56)
    for phase in ["accumulation","bull","distribution","bear"]:
        ph = trades_df[trades_df["cycle_phase"] == phase]
        if len(ph) == 0:
            print(f"  {phase:<16} {'0':>6} {'—':>6} {'—':>8} {'—':>8}")
            continue
        ph_wr  = len(ph[ph["return_pct"] > 0]) / len(ph) * 100
        ph_avg = ph["return_pct"].mean()
        ph_tot = ph["return_pct"].sum()
        sizing = ph["long_mult"].mean() if "long_mult" in ph.columns else ph.get("short_mult", pd.Series([1])).mean()
        print(f"  {phase:<16} {len(ph):>6} {ph_wr:>5.1f}% {ph_avg:>+7.2f}% {ph_tot:>+7.1f}% {sizing:>6.1f}×")

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
    daily_sma = fetch_daily_sma(SYMBOL, SMA_LONG, SMA_SHORT)

    df = df.join(daily_sma, how="left")
    df["sma_long"]        = df["sma_long"].ffill().fillna(0)
    df["sma_short"]       = df["sma_short"].ffill().fillna(0)
    df["above_sma_long"]  = df["above_sma_long"].ffill().fillna(False)
    df["below_sma_short"] = df["below_sma_short"].ffill().fillna(False)

    funding = fetch_funding(SYMBOL)
    df = df.join(funding, how="left")
    df["funding"] = df["funding"].ffill().fillna(0)

    oi_df = fetch_oi(SYMBOL, INTERVAL)
    df = df.join(oi_df, how="left")
    df["oi"]       = df["oi"].ffill().fillna(0)
    df["oi_delta"] = df["oi"].diff().fillna(0)

    df = compute_signal(df)

    trades_df, equity_s, blocked_sma, blocked_cb = run_backtest(df)

    compute_metrics(trades_df, equity_s, df, blocked_sma, blocked_cb)

    out = df[["open","high","low","close","volume","score","adx","atr",
              "sma_long","sma_short","above_sma_long","below_sma_short",
              "rsi_z","mfi_z","macd_z","di_z","cvd_z","oi_z","fund_z","vwap_z"]].copy()
    out.to_csv(OUT_SIGNAL)
    if not trades_df.empty:
        trades_df.to_csv(OUT_TRADES, index=False)
    print(f"\n  Signal data → {OUT_SIGNAL}")
    print(f"  Trade log   → {OUT_TRADES}")

if __name__ == "__main__":
    main()
