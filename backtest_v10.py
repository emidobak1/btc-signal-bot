"""
Composite Signal Score — Python Backtest v10
============================================
HYBRID: v9 long logic + v8c short logic

THESIS:
- v8c proved: short EMA5 is optimal (+85.8% short total, Sharpe 1.55)
- v9_final proved: long side can reach +45.2% with EMA3 + velocity + breakout
- v10 combines both: each side uses its proven-best smoothing and entry logic

LONG SIDE (from v9_final):
  - EMA3 smoothing (fast entry)
  - Standard long: score > 0.40
  - Early velocity: approach zone 0.25-0.40, velocity > 0.12, CVD z > 0.5
  - Breakout long: new 10-bar high + volume + CVD z > 0.3 + score > 0.15

SHORT SIDE (from v8c):
  - EMA5 smoothing (stable, proven optimal)
  - Standard short only: score < -0.40
  - No early short entries (bear CVD has no edge, validated in v8)
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
Z_WINDOW      = 200

# v8: Asymmetric smoothing — longs need speed, shorts need stability
SMOOTH_LEN_LONG  = 3   # v9 long logic: EMA3, fast entry
SMOOTH_LEN_SHORT = 5   # v8c short logic: EMA5, proven optimal for shorts

# v6 weights — unchanged
W_RSI  = 1.5; W_MFI  = 1.0; W_MACD = 1.2; W_ADX  = 1.0
W_CVD  = 1.8; W_OI   = 1.5; W_FUND = 0.8; W_VWAP = 1.0; W_LIQ  = 1.2

# ── Standard entry thresholds (unchanged from v6)
BULL_THRESH   = 0.40
BEAR_THRESH   = -0.40
ADX_ENTRY_MIN = 20
ADX_MIN       = 15
ADX_MAX       = 45
FUND_EXTREME  = 1.5

# ── v8: Early entry thresholds (data-validated)
# LONGS ONLY — bear early entry has no statistical edge
APPROACH_ZONE_LONG    = 0.25   # score must be above this to trigger early long
VELOCITY_THRESH_LONG  = 0.12   # score change per bar — validated: 56% hit rate at 0.12
CVD_BURST_LONG        = 2.0    # CVD z-score threshold for burst entry
CVD_MIN_SCORE_LONG    = 0.20   # score must be this positive to allow CVD burst long
EARLY_STOP_PCT        = 0.05   # tighter initial stop for early entries (vs 7% standard)

# ── v9 port: Breakout long entry (added to v8c, not replacing anything)
# Fires when price breaks above 20-bar high with volume + CVD confirmation
# v9.1 data: 69.2% win rate, +0.95% avg — best long quality found
BREAKOUT_LOOKBACK   = 10     # 10 bars = 40H — more frequent breakouts
BREAKOUT_VOL_MULT   = 1.0    # volume must exceed 10-bar median
BREAKOUT_CVD_MIN    = 0.3    # CVD z > 0.3 — buyers in control
BREAKOUT_SCORE_MIN  = 0.15   # composite score must be positive (lowered from 0.20)
BREAKOUT_STOP_PCT   = 0.06   # tighter stop — invalidated if falls back below breakout

# ── Exit (unchanged from v6)
LONG_EXIT_SCORE  = 0.0
SHORT_EXIT_SCORE = 0.0
MIN_HOLD_BARS    = 4

# ── Risk (unchanged from v6)
ATR_PERIOD       = 14
ATR_MULT         = 4.0
INITIAL_STOP_PCT = 0.07
TRAIL_ACTIVATION = 0.01
TAKE_PROFIT      = 0.12

# ── Circuit breaker (unchanged)
MAX_CONSEC_STOPS = 3
COOLDOWN_BARS    = 10

# ── Filters
LAST_HALVING = pd.Timestamp("2024-04-20", tz="UTC")
SMA_LONG     = 200
SMA_SHORT    = 100

CYCLE_PHASES = {
    "accumulation": (0,   6,   0.6, 0.8),
    "bull":         (6,   18,  1.4, 0.6),
    "distribution": (18,  30,  0.6, 1.4),
    "bear":         (30,  999, 0.4, 1.6),
}

def get_cycle_phase(date):
    months_since = (date - LAST_HALVING).days / 30.44
    for phase, (start, end, lm, sm) in CYCLE_PHASES.items():
        if start <= months_since < end:
            return phase, lm, sm
    return "bear", 0.4, 1.6

FEES     = 0.0004
SLIPPAGE = 0.0002

OUT_SIGNAL = "/Users/emidobak/Desktop/signal_data_v10.csv"
OUT_TRADES = "/Users/emidobak/Desktop/trades_v10.csv"

# ─── Data fetching (identical to v6) ─────────────────────────────────────────

def fetch_ohlcv():
    print(f"Fetching {NUM_REQUESTS * LIMIT_PER_REQ} bars of {SYMBOL} {INTERVAL}...")
    url, all_data, end_time = "https://fapi.binance.com/fapi/v1/klines", [], None
    for i in range(NUM_REQUESTS):
        params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": LIMIT_PER_REQ}
        if end_time: params["endTime"] = end_time
        r = requests.get(url, params=params, timeout=10); r.raise_for_status()
        data = r.json()
        if not data: break
        all_data = data + all_data; end_time = data[0][0] - 1
        time.sleep(0.1)
        print(f"  Batch {i+1}/{NUM_REQUESTS} — {len(all_data)} bars")
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
    print(f"  Loaded: {len(df):,} | {df.index[0].date()} → {df.index[-1].date()}")
    return df

def fetch_daily_sma():
    url, all_data, end_time = "https://fapi.binance.com/fapi/v1/klines", [], None
    for _ in range(8):
        params = {"symbol": SYMBOL, "interval": "1d", "limit": 1000}
        if end_time: params["endTime"] = end_time
        r = requests.get(url, params=params, timeout=10); r.raise_for_status()
        data = r.json()
        if not data: break
        all_data = data + all_data; end_time = data[0][0] - 1
        time.sleep(0.1)
    df = pd.DataFrame(all_data, columns=[
        "timestamp","open","high","low","close","volume","close_time",
        "quote_volume","trades","taker_buy_base","taker_buy_quote","ignore"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df["close"] = df["close"].astype(float)
    df = df[["close"]].drop_duplicates().sort_index()
    df["sma_long"]        = df["close"].rolling(SMA_LONG,  min_periods=SMA_LONG).mean()
    df["sma_short"]       = df["close"].rolling(SMA_SHORT, min_periods=SMA_SHORT).mean()
    df["above_sma_long"]  = df["close"] > df["sma_long"]
    df["below_sma_short"] = df["close"] < df["sma_short"]
    return df[["sma_long","sma_short","above_sma_long","below_sma_short"]]

def fetch_funding():
    url, all_data, end_time = "https://fapi.binance.com/fapi/v1/fundingRate", [], None
    for _ in range(NUM_REQUESTS):
        params = {"symbol": SYMBOL, "limit": 1000}
        if end_time: params["endTime"] = end_time
        r = requests.get(url, params=params, timeout=10); r.raise_for_status()
        data = r.json()
        if not data: break
        all_data = data + all_data; end_time = data[0]["fundingTime"] - 1
        time.sleep(0.1)
    df = pd.DataFrame(all_data)
    df["timestamp"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df["funding"] = df["fundingRate"].astype(float)
    return df[["funding"]].drop_duplicates().sort_index()

def fetch_oi():
    url    = "https://fapi.binance.com/futures/data/openInterestHist"
    params = {"symbol": SYMBOL, "period": INTERVAL, "limit": 500}
    r      = requests.get(url, params=params, timeout=10); r.raise_for_status()
    df     = pd.DataFrame(r.json())
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df["oi"] = df["sumOpenInterestValue"].astype(float)
    return df[["oi"]].drop_duplicates().sort_index()

# ─── Signal helpers (identical to v6) ────────────────────────────────────────

def rolling_zscore(series, window):
    mu = series.rolling(window, min_periods=1).mean()
    sd = series.rolling(window, min_periods=1).std()
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
    return macd - macd.ewm(span=signal, adjust=False).mean()

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

def softclamp(x): return np.tanh(x)

# ─── Signal computation ───────────────────────────────────────────────────────

def compute_signal(df):
    print("Computing signals...")
    close  = df["close"]; high = df["high"]; low = df["low"]
    vol    = df["volume"]; buy_v = df["buy_volume"]; sell_v = df["sell_volume"]

    rsi_z  = rolling_zscore(compute_rsi(close) - 50, Z_WINDOW)
    mfi_z  = rolling_zscore(compute_mfi(high, low, close, vol) - 50, Z_WINDOW)
    macd_z = rolling_zscore(compute_macd_hist(close), Z_WINDOW)

    plus_di, minus_di, adx = compute_dmi(high, low, close)
    di_z     = rolling_zscore(plus_di - minus_di, Z_WINDOW)
    adx_mult = ((adx - ADX_MIN) / (ADX_MAX - ADX_MIN)).clip(0, 1)

    cvd_z = rolling_zscore(buy_v - sell_v, Z_WINDOW)

    oi_delta  = df["oi_delta"]
    price_dir = np.sign(close - df["open"])
    oi_z      = rolling_zscore(oi_delta * price_dir, Z_WINDOW)

    fund       = df["funding"].fillna(0)
    raw_fund_z = rolling_zscore(-fund, Z_WINDOW)
    fund_z     = raw_fund_z.where(raw_fund_z.abs() >= FUND_EXTREME, 0)

    vwap_z = rolling_zscore(
        close - compute_rvwap(high, low, close, buy_v, sell_v, RVWAP_PERIOD), Z_WINDOW
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

    # Asymmetric smoothing — inserted here after raw_score is computed
    score         = np.tanh(raw_score * 1.5)
    smoothed_long  = score.ewm(span=SMOOTH_LEN_LONG,  adjust=False).mean()  # span=3 for longs
    smoothed_short = score.ewm(span=SMOOTH_LEN_SHORT, adjust=False).mean()  # span=4 for shorts
    smoothed       = smoothed_long   # primary score used for display and long logic

    atr_s    = compute_atr(high, low, close, ATR_PERIOD)
    velocity = smoothed_long.diff()

    # Early long: approach zone + high velocity + CVD confirmation
    early_long_velocity = (
        (smoothed_long > APPROACH_ZONE_LONG) &
        (smoothed_long < BULL_THRESH) &
        (velocity > VELOCITY_THRESH_LONG) &
        (cvd_z > 0.5)
    )

    # Early long: CVD burst
    early_long_cvd = (
        (cvd_z > CVD_BURST_LONG) &
        (smoothed_long > CVD_MIN_SCORE_LONG) &
        (smoothed_long < BULL_THRESH) &
        (velocity > 0.02)
    )

    # ── v9 port: Breakout long detection
    # New 20-bar high + volume + CVD confirmation + not overextended
    # From v9.1 research: 69.2% win rate, best long quality found
    rolling_high = high.rolling(BREAKOUT_LOOKBACK, min_periods=BREAKOUT_LOOKBACK).max().shift(1)
    vol_median   = vol.rolling(BREAKOUT_LOOKBACK, min_periods=BREAKOUT_LOOKBACK).median()
    # vwap_dist still computed for df storage but not used in filter
    vwap_val_s   = compute_rvwap(high, low, close, buy_v, sell_v, RVWAP_PERIOD)
    vwap_dist    = (close / vwap_val_s - 1).replace([np.inf, -np.inf], np.nan).fillna(0)

    breakout_cond = (
        (close > rolling_high) &                              # new 10-bar high
        (vol > vol_median * BREAKOUT_VOL_MULT) &              # volume confirmed
        (cvd_z > BREAKOUT_CVD_MIN) &                          # buyers in control
        (smoothed_long > BREAKOUT_SCORE_MIN)                  # composite confirms
    )  # VWAP/funding filters removed — too restrictive, already in composite
    # Only trigger on first bar of breakout (not sustained)
    breakout_trigger = breakout_cond & ~breakout_cond.shift(1).fillna(False)

    df = df.copy()
    df["score"]              = smoothed_long
    df["score_short"]        = smoothed_short
    df["score_raw"]          = raw_score
    df["adx"]                = adx
    df["atr"]                = atr_s
    df["velocity"]           = velocity
    df["cvd_z"]              = cvd_z
    df["rsi_z"]              = rsi_z
    df["mfi_z"]              = mfi_z
    df["macd_z"]             = macd_z
    df["di_z"]               = di_z
    df["oi_z"]               = oi_z
    df["fund_z"]             = fund_z
    df["vwap_z"]             = vwap_z
    df["early_long_velocity"]= early_long_velocity
    df["early_long_cvd"]     = early_long_cvd
    df["breakout_trigger"]   = breakout_trigger
    df["vwap_dist"]          = vwap_dist
    return df

# ─── Backtest engine ──────────────────────────────────────────────────────────

def run_backtest(df):
    print("Running backtest...")
    scores    = df["score"].values
    closes    = df["close"].values
    highs     = df["high"].values
    lows      = df["low"].values
    adx_v     = df["adx"].values
    atr_v     = df["atr"].values
    above_long  = df["above_sma_long"].values
    below_short = df["below_sma_short"].values
    el_vel      = df["early_long_velocity"].values
    el_cvd      = df["early_long_cvd"].values
    bo_trig     = df["breakout_trigger"].values
    scores_short = df["score_short"].values   # separate smoothing for shorts
    dates       = df.index

    position     = 0; entry_price = 0.0; entry_bar = 0
    hwm = lwm    = 0.0; trail_active = False
    consec_stops = 0; cooldown_end  = 0
    entry_type   = "standard"   # track whether early or standard entry

    # Counters for diagnostics
    std_long_entries    = 0; std_short_entries = 0
    early_vel_entries   = 0; early_cvd_entries = 0
    breakout_entries    = 0
    blocked_sma         = 0

    trades  = []; equity = [1.0]; eq = 1.0

    for i in range(1, len(df)):
        prev  = scores[i-1]; curr = scores[i]
        price = closes[i]; hi = highs[i]; lo = lows[i]
        atr   = atr_v[i]; adx = adx_v[i]
        al    = above_long[i]; bs = below_short[i]
        bo    = bool(bo_trig[i])
        date  = dates[i]
        phase, long_mult, short_mult = get_cycle_phase(date)

        # ── Entry
        if position == 0:
            if i < cooldown_end:
                equity.append(eq); continue

            is_trending = adx >= ADX_ENTRY_MIN

            # Asymmetric crossover detection
            # Longs: use long-smoothed score (span=3, faster)
            bull_cross  = prev <= BULL_THRESH and curr > BULL_THRESH

            # Shorts: use short-smoothed score (span=4, slightly smoother)
            prev_s = scores_short[i-1]
            curr_s = scores_short[i]
            bear_cross  = prev_s >= BEAR_THRESH and curr_s < BEAR_THRESH

            # v8: Early long entries (longs only — shorts keep standard)
            vel_entry = bool(el_vel[i]) and not bool(el_vel[i-1])  # new trigger only
            cvd_entry = bool(el_cvd[i]) and not bool(el_cvd[i-1])  # new trigger only

            if is_trending and bull_cross and al:
                position = 1; entry_price = price*(1+SLIPPAGE)
                entry_bar = i; hwm = hi; trail_active = False
                entry_type = "standard_long"
                std_long_entries += 1

            elif is_trending and bear_cross and bs:
                position = -1; entry_price = price*(1-SLIPPAGE)
                entry_bar = i; lwm = lo; trail_active = False
                entry_type = "standard_short"
                std_short_entries += 1

            # Early long: velocity approach (only fires on NEW trigger, not sustained)
            elif is_trending and vel_entry and al and position == 0:
                position = 1; entry_price = price*(1+SLIPPAGE)
                entry_bar = i; hwm = hi; trail_active = False
                entry_type = "early_velocity"
                early_vel_entries += 1

            # Early long: CVD burst (only fires on NEW trigger)
            elif is_trending and cvd_entry and al and position == 0:
                position = 1; entry_price = price*(1+SLIPPAGE)
                entry_bar = i; hwm = hi; trail_active = False
                entry_type = "early_cvd"
                early_cvd_entries += 1

            # v9 port: Breakout long — new 20-bar high with confirmation
            # 69.2% win rate in v9.1, added as 3rd long entry type
            elif is_trending and bo and al and position == 0:
                position = 1; entry_price = price*(1+SLIPPAGE)
                entry_bar = i; hwm = hi; trail_active = False
                entry_type = "breakout_long"
                breakout_entries += 1

            elif is_trending and (bull_cross or bear_cross):
                blocked_sma += 1

        # ── Manage long
        elif position == 1:
            bars_held   = i - entry_bar
            current_pnl = (price / entry_price) - 1

            if not trail_active and current_pnl >= TRAIL_ACTIVATION:
                trail_active = True; hwm = hi
            if trail_active: hwm = max(hwm, hi)

            # Breakout entries: 6% stop. Early entries: 5%. Standard: 7%.
            if trail_active:
                stop_level = hwm - ATR_MULT * atr
            elif entry_type == "breakout_long":
                stop_level = entry_price * (1 - BREAKOUT_STOP_PCT)
            elif "early" in entry_type:
                stop_level = entry_price * (1 - EARLY_STOP_PCT)
            else:
                stop_level = entry_price * (1 - INITIAL_STOP_PCT)
            tp_level   = entry_price * (1 + TAKE_PROFIT)
            stop_hit   = lo <= stop_level
            tp_hit     = (hi / entry_price - 1) >= TAKE_PROFIT
            sig_exit   = bars_held >= MIN_HOLD_BARS and curr < LONG_EXIT_SCORE

            if stop_hit or tp_hit or sig_exit:
                if stop_hit:
                    exit_price  = max(stop_level, lo)
                    exit_reason = "TRAIL_STOP" if trail_active else "INIT_STOP"
                    consec_stops += 1
                    if consec_stops >= MAX_CONSEC_STOPS:
                        cooldown_end = i + COOLDOWN_BARS; consec_stops = 0
                elif tp_hit:
                    exit_price = entry_price*(1+TAKE_PROFIT); exit_reason = "TP"; consec_stops = 0
                else:
                    exit_price = price*(1-SLIPPAGE); exit_reason = "SIGNAL"; consec_stops = 0

                raw_ret = (exit_price/entry_price - 1) - FEES*2
                sized   = raw_ret * long_mult; eq *= (1+sized)
                trades.append({
                    "entry_date": dates[entry_bar], "exit_date": date,
                    "direction": "LONG", "entry_type": entry_type,
                    "entry_price": entry_price, "exit_price": exit_price,
                    "return_pct": raw_ret*100, "sized_pct": sized*100,
                    "bars_held": bars_held, "adx_entry": adx_v[entry_bar],
                    "cycle_phase": phase, "long_mult": long_mult,
                    "trail_active": trail_active, "exit_reason": exit_reason
                })
                position = 0; trail_active = False

        # ── Manage short (unchanged from v6)
        elif position == -1:
            bars_held   = i - entry_bar
            current_pnl = (entry_price / price) - 1

            if not trail_active and current_pnl >= TRAIL_ACTIVATION:
                trail_active = True; lwm = lo
            if trail_active: lwm = min(lwm, lo)

            stop_level_s = (lwm + ATR_MULT * atr) if trail_active else \
                           entry_price * (1 + INITIAL_STOP_PCT)
            stop_hit = hi >= stop_level_s
            tp_hit   = (entry_price / lo - 1) >= TAKE_PROFIT
            # Short exit uses short-smoothed score for consistency
            curr_s_exit = scores_short[i]
            sig_exit = bars_held >= MIN_HOLD_BARS and curr_s_exit > SHORT_EXIT_SCORE

            if stop_hit or tp_hit or sig_exit:
                if stop_hit:
                    exit_price   = min(stop_level_s, hi)
                    exit_reason  = "TRAIL_STOP" if trail_active else "INIT_STOP"
                    consec_stops += 1
                    if consec_stops >= MAX_CONSEC_STOPS:
                        cooldown_end = i + COOLDOWN_BARS; consec_stops = 0
                elif tp_hit:
                    exit_price = entry_price*(1-TAKE_PROFIT); exit_reason = "TP"; consec_stops = 0
                else:
                    exit_price = price*(1+SLIPPAGE); exit_reason = "SIGNAL"; consec_stops = 0

                raw_ret = (entry_price/exit_price - 1) - FEES*2
                sized   = raw_ret * short_mult; eq *= (1+sized)
                trades.append({
                    "entry_date": dates[entry_bar], "exit_date": date,
                    "direction": "SHORT", "entry_type": entry_type,
                    "entry_price": entry_price, "exit_price": exit_price,
                    "return_pct": raw_ret*100, "sized_pct": sized*100,
                    "bars_held": bars_held, "adx_entry": adx_v[entry_bar],
                    "cycle_phase": phase, "short_mult": short_mult,
                    "trail_active": trail_active, "exit_reason": exit_reason
                })
                position = 0; trail_active = False

        equity.append(eq)

    trades_df = pd.DataFrame(trades)
    equity_s  = pd.Series(equity, index=dates[:len(equity)])
    return trades_df, equity_s, {
        "std_long": std_long_entries, "std_short": std_short_entries,
        "early_vel": early_vel_entries, "early_cvd": early_cvd_entries,
        "breakout": breakout_entries,
        "blocked_sma": blocked_sma
    }

# ─── Metrics ──────────────────────────────────────────────────────────────────

def compute_metrics(trades_df, equity_s, df, entry_counts):
    if trades_df.empty:
        print("No trades."); return

    raw  = trades_df["return_pct"]
    wins = trades_df[raw > 0]; loss = trades_df[raw <= 0]

    total_ret  = (equity_s.iloc[-1] - 1) * 100
    buy_hold   = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    win_rate   = len(wins) / len(trades_df) * 100
    avg_win    = wins["return_pct"].mean() if len(wins) else 0
    avg_loss   = loss["return_pct"].mean() if len(loss) else 0
    pf         = wins["return_pct"].sum() / abs(loss["return_pct"].sum()) \
                 if len(loss) and loss["return_pct"].sum() != 0 else float("inf")
    expectancy = (win_rate/100 * avg_win) + ((1-win_rate/100) * avg_loss)
    avg_bars   = trades_df["bars_held"].mean()

    daily_eq  = equity_s.resample("1D").last().ffill()
    daily_ret = daily_eq.pct_change().dropna()
    sharpe    = (daily_ret.mean()/daily_ret.std())*np.sqrt(365) if daily_ret.std()>0 else 0
    max_dd    = ((equity_s - equity_s.cummax())/equity_s.cummax()).min() * 100

    longs  = trades_df[trades_df["direction"]=="LONG"]
    shorts = trades_df[trades_df["direction"]=="SHORT"]
    stops  = trades_df[trades_df["exit_reason"].isin(["INIT_STOP","TRAIL_STOP"])]
    tps    = trades_df[trades_df["exit_reason"]=="TP"]
    sigs   = trades_df[trades_df["exit_reason"]=="SIGNAL"]

    # Entry type breakdown
    by_type = trades_df.groupby("entry_type").agg(
        count=("return_pct","count"),
        win_rate=("return_pct", lambda x: (x>0).mean()*100),
        avg_ret=("return_pct","mean"),
        total=("sized_pct","sum")
    ).round(2)

    print("\n" + "═"*68)
    print("  COMPOSITE SIGNAL v10 — BACKTEST RESULTS")
    print("  HYBRID: v9 long logic (EMA3+vel+breakout) + v8c short logic (EMA5)")
    print("═"*68)
    print(f"  Period:               {df.index[0].date()} → {df.index[-1].date()}")
    print(f"  Entry counts:  std_long={entry_counts['std_long']}  std_short={entry_counts['std_short']}  "
          f"early_vel={entry_counts['early_vel']}  early_cvd={entry_counts['early_cvd']}  "
          f"breakout={entry_counts.get('breakout',0)}")
    print("─"*68)
    print(f"  Total trades:         {len(trades_df)}  (v8c: 51, v9: 78)")
    print(f"    Longs:              {len(longs)}  (v8c: 28, v9: 55)")
    print(f"    Shorts:             {len(shorts)}  (v8c: 23, v9: 23)")
    print(f"  Win rate:             {win_rate:.1f}%  (v8c: 52.9%, v9: 52.6%)")
    print(f"  Avg win:              +{avg_win:.2f}%  (v8c: +5.72%, v9: +4.84%)")
    print(f"  Avg loss:             {avg_loss:.2f}%  (v8c: -2.03%, v9: -2.38%)")
    print(f"  Profit factor:        {pf:.2f}  (v8c: 2.39, v9: 2.26)")
    print(f"  Expectancy/trade:     {expectancy:+.2f}%  (v8c: +1.55%, v9: +1.42%)")
    print(f"  Avg bars held:        {avg_bars:.1f}  (v8c: 19.6, v9: 19.0)")
    print("─"*68)
    print(f"  Signal:  {len(sigs)} ({len(sigs)/len(trades_df)*100:.0f}%)  "
          f"Stop: {len(stops)} ({len(stops)/len(trades_df)*100:.0f}%)  "
          f"TP: {len(tps)} ({len(tps)/len(trades_df)*100:.0f}%)")
    if len(stops): print(f"  Avg stop:    {stops['return_pct'].mean():.2f}%")
    if len(tps):   print(f"  Avg TP:      +{tps['return_pct'].mean():.2f}%")
    if len(sigs):  print(f"  Avg signal:  {sigs['return_pct'].mean():+.2f}%")
    print("─"*68)
    print(f"  Total return (sized): {total_ret:+.1f}%  (v8c: +198.8%, v9: +178.7%)")
    print(f"  Buy & hold:           {buy_hold:+.1f}%")
    print(f"  Sharpe ratio:         {sharpe:.2f}  (v8c: 1.55, v9: 1.49)")
    print(f"  Max drawdown:         {max_dd:.1f}%  (v8c: -15.8%, v9: -13.7%)")
    print("─"*68)
    if len(longs):
        lwr = len(longs[longs["return_pct"]>0])/len(longs)*100
        print(f"  Long  win rate: {lwr:.1f}%  Total: {longs['return_pct'].sum():+.1f}%  (v8c: 47.9%, +13.0% | v9: 52.7%, +45.2%)")
    if len(shorts):
        swr = len(shorts[shorts["return_pct"]>0])/len(shorts)*100
        print(f"  Short win rate: {swr:.1f}%  Total: {shorts['return_pct'].sum():+.1f}%  (v8c: 50.0%, +85.8% | v9: 52.2%, +65.4%)")
    print("═"*68)


    print(f"\n  ENTRY TYPE BREAKDOWN:")
    print(f"  {'Type':<20} {'Count':>6} {'Win%':>6} {'Avg Ret':>8} {'Total Sized':>12}")
    print("  " + "─"*56)
    for etype, row in by_type.iterrows():
        print(f"  {etype:<20} {int(row['count']):>6} {row['win_rate']:>5.1f}% "
              f"{row['avg_ret']:>+7.2f}% {row['total']:>+11.1f}%")

    print(f"\n  CYCLE PHASE BREAKDOWN:")
    print(f"  {'Phase':<16} {'Trades':>6} {'Win%':>6} {'Avg Ret':>8} {'Total':>8}")
    print("  " + "─"*48)
    for phase in ["accumulation","bull","distribution","bear"]:
        ph = trades_df[trades_df["cycle_phase"]==phase]
        if not len(ph): continue
        pw = len(ph[ph["return_pct"]>0])/len(ph)*100
        print(f"  {phase:<16} {len(ph):>6} {pw:>5.1f}% "
              f"{ph['return_pct'].mean():>+7.2f}% {ph['return_pct'].sum():>+7.1f}%")

    print(f"\n  LAST 10 TRADES:")
    print(f"  {'Entry':<20} {'Dir':<6} {'Type':<18} {'Bars':>4} {'Phase':<14} {'Reason':<11} {'Raw%':>7}")
    print("  " + "─"*92)
    for _, t in trades_df.tail(10).iterrows():
        print(f"  {str(t['entry_date'])[:19]:<20} {t['direction']:<6} "
              f"{t['entry_type']:<18} {int(t['bars_held']):>4} {t['cycle_phase']:<14} "
              f"{t['exit_reason']:<11} {t['return_pct']:>+6.2f}%")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    df        = fetch_ohlcv()
    daily_sma = fetch_daily_sma()
    df = df.join(daily_sma, how="left")
    df["above_sma_long"]  = df["above_sma_long"].ffill().fillna(False).infer_objects(copy=False)
    df["below_sma_short"] = df["below_sma_short"].ffill().fillna(False).infer_objects(copy=False)

    funding = fetch_funding()
    df = df.join(funding, how="left")
    df["funding"] = df["funding"].ffill().fillna(0)

    oi_df = fetch_oi()
    df = df.join(oi_df, how="left")
    df["oi"]       = df["oi"].ffill().fillna(0)
    df["oi_delta"] = df["oi"].diff().fillna(0)

    df = compute_signal(df)

    trades_df, equity_s, counts = run_backtest(df)
    compute_metrics(trades_df, equity_s, df, counts)

    out_cols = ["open","high","low","close","volume","score","score_raw","velocity",
                "adx","atr","cvd_z","rsi_z","mfi_z","macd_z","di_z",
                "fund_z","vwap_z","early_long_velocity","early_long_cvd"]
    df[out_cols].to_csv(OUT_SIGNAL)
    if not trades_df.empty:
        trades_df.to_csv(OUT_TRADES, index=False)
    print(f"\n  Signal → {OUT_SIGNAL}")
    print(f"  Trades → {OUT_TRADES}")

if __name__ == "__main__":
    main()