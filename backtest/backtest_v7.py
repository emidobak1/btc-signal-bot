"""
Composite Signal Score — Python Backtest v7
==========================================
Changes from v6 based on statistical validation:

WEIGHT CHANGES (from partial F-test, Granger causality, VIF, rolling IC):
  - RSI:  1.5 → 0.8  (redundant with DMI, VIF=11.98)
  - MFI:  1.0 → 1.8  (strongest F-test p=0.0002 AND Granger ✓)
  - MACD: 1.2 → 0.3  (fails F-test AND Granger — adds no unique info)
  - DMI:  1.0 → 0.0  (DROPPED from score — correlation with RSI = 0.93)
                      (ADX still used as entry filter via adxMult)
  - CVD:  1.8 → 2.0  (strongest Granger p=0.0002)
  - OI:   1.5 → 0.0  (DROPPED — no edge, only 30 days of Binance history)
  - FUND: 0.8 → 0.3  (no Granger/F-test edge — keep contrarian gate only)
  - VWAP: 1.0 → 2.0  (best rolling IR = -1.33, passes F-test)
  - LIQ:  1.2 → 1.2  (unchanged — can't test without full liq data)

SIGNAL ARCHITECTURE CHANGE:
  - di_z (DMI direction) completely removed from weighted sum
  - adxMult still gates trend-following signals (RSI/MFI/MACD)
  - New: Stochastic RSI added to replace DMI direction signal
  - New: Chaikin Money Flow (CMF) added as additional volume flow signal

All execution logic (SMA filters, ATR trail, cycle phase, circuit breaker)
unchanged from v6 — only signal composition changes.
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
STOCH_K       = 14   # Stochastic RSI period
STOCH_SMOOTH  = 3    # Stochastic RSI smoothing
CMF_PERIOD    = 20   # Chaikin Money Flow period

# ── v7 STATISTICALLY OPTIMIZED WEIGHTS ───────────────────────────────────────
W_RSI   = 0.8   # reduced — redundant with DMI (VIF 11.98)
W_MFI   = 1.8   # increased — best F-test + Granger
W_MACD  = 0.3   # reduced — no F-test or Granger edge
W_CVD   = 2.0   # increased — strongest Granger (p=0.0002)
W_FUND  = 0.3   # reduced — contrarian gate only
W_VWAP  = 2.0   # increased — best rolling IR (-1.33), passes F-test
W_LIQ   = 1.2   # unchanged
W_STOCH = 1.0   # new — replaces DMI direction signal
W_CMF   = 1.0   # new — additional volume flow confirmation
# Note: W_ADX and W_OI = 0 (dropped from weighted sum)
# ADX still used as adxMult regime filter on trend signals

# Entry thresholds
BULL_THRESH   = 0.40
BEAR_THRESH   = -0.40
ADX_ENTRY_MIN = 20
ADX_MIN       = 15
ADX_MAX       = 45
FUND_EXTREME  = 1.5
MIN_HOLD_BARS = 4

LONG_EXIT_SCORE  = 0.0
SHORT_EXIT_SCORE = 0.0

LAST_HALVING  = pd.Timestamp("2024-04-20", tz="UTC")
SMA_LONG      = 200
SMA_SHORT     = 100

ATR_PERIOD       = 14
ATR_MULT         = 4.0
INITIAL_STOP_PCT = 0.07
TRAIL_ACTIVATION = 0.01
TAKE_PROFIT      = 0.12

MAX_CONSEC_STOPS = 3
COOLDOWN_BARS    = 10

FEES      = 0.0004
SLIPPAGE  = 0.0002

OUT_SIGNAL = "/Users/emidobak/Desktop/signal_data_v7.csv"
OUT_TRADES = "/Users/emidobak/Desktop/trades_v7.csv"

# ─── Cycle phase ──────────────────────────────────────────────────────────────
CYCLE_PHASES = {
    "accumulation":  (0,   6,   0.6,  0.8),
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

def fetch_ohlcv():
    print(f"Fetching {NUM_REQUESTS * LIMIT_PER_REQ} bars of {SYMBOL} {INTERVAL}...")
    url, all_data, end_time = "https://fapi.binance.com/fapi/v1/klines", [], None
    for i in range(NUM_REQUESTS):
        params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": LIMIT_PER_REQ}
        if end_time: params["endTime"] = end_time
        r = requests.get(url, params=params, timeout=10); r.raise_for_status()
        data = r.json()
        if not data: break
        all_data = data + all_data
        end_time = data[0][0] - 1
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
    print("Fetching daily SMA filters...")
    url, all_data, end_time = "https://fapi.binance.com/fapi/v1/klines", [], None
    for i in range(8):
        params = {"symbol": SYMBOL, "interval": "1d", "limit": 1000}
        if end_time: params["endTime"] = end_time
        r = requests.get(url, params=params, timeout=10); r.raise_for_status()
        data = r.json()
        if not data: break
        all_data = data + all_data
        end_time = data[0][0] - 1
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
    print(f"  Daily loaded: {len(df)}")
    return df[["sma_long","sma_short","above_sma_long","below_sma_short"]]

def fetch_funding():
    print("Fetching funding rates...")
    url, all_data, end_time = "https://fapi.binance.com/fapi/v1/fundingRate", [], None
    for _ in range(NUM_REQUESTS):
        params = {"symbol": SYMBOL, "limit": 1000}
        if end_time: params["endTime"] = end_time
        r = requests.get(url, params=params, timeout=10); r.raise_for_status()
        data = r.json()
        if not data: break
        all_data = data + all_data
        end_time = data[0]["fundingTime"] - 1
        time.sleep(0.1)
    df = pd.DataFrame(all_data)
    df["timestamp"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df["funding"] = df["fundingRate"].astype(float)
    df = df[["funding"]].drop_duplicates().sort_index()
    print(f"  Funding loaded: {len(df)}")
    return df

def fetch_oi():
    print("Fetching open interest...")
    url    = "https://fapi.binance.com/futures/data/openInterestHist"
    params = {"symbol": SYMBOL, "period": INTERVAL, "limit": 500}
    r      = requests.get(url, params=params, timeout=10); r.raise_for_status()
    df     = pd.DataFrame(r.json())
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df["oi"] = df["sumOpenInterestValue"].astype(float)
    df = df[["oi"]].drop_duplicates().sort_index()
    print(f"  OI loaded: {len(df)} bars")
    return df

# ─── Signal helpers ───────────────────────────────────────────────────────────

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

def compute_stoch_rsi(close, rsi_period=14, stoch_period=14, smooth=3):
    """Stochastic RSI — oscillator of RSI, captures momentum extremes better than raw RSI"""
    rsi    = compute_rsi(close, rsi_period)
    lo     = rsi.rolling(stoch_period, min_periods=1).min()
    hi     = rsi.rolling(stoch_period, min_periods=1).max()
    stoch  = (rsi - lo) / (hi - lo).replace(0, np.nan)
    return stoch.fillna(0.5).rolling(smooth, min_periods=1).mean() - 0.5  # center at 0

def compute_mfi(high, low, close, volume, period=14):
    tp      = (high + low + close) / 3
    mf      = tp * volume
    pos_sum = mf.where(tp > tp.shift(1), 0).rolling(period, min_periods=1).sum()
    neg_sum = mf.where(tp < tp.shift(1), 0).rolling(period, min_periods=1).sum()
    mfr     = pos_sum / neg_sum.replace(0, np.nan)
    return (100 - 100 / (1 + mfr)).fillna(50)

def compute_cmf(high, low, close, volume, period=20):
    """Chaikin Money Flow — volume-weighted price position within bar range
       Positive = buying pressure, negative = selling pressure
       Statistically independent from RSI/MFI as it uses intrabar position"""
    clv = ((close - low) - (high - close)) / (high - low).replace(0, np.nan)
    clv = clv.fillna(0)
    cmf = (clv * volume).rolling(period, min_periods=1).sum() / \
          volume.rolling(period, min_periods=1).sum().replace(0, np.nan)
    return cmf.fillna(0)

def compute_macd_hist(close, fast=12, slow=26, signal=9):
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    macd  = ema_f - ema_s
    sig   = macd.ewm(span=signal, adjust=False).mean()
    return macd - sig

def compute_adx(high, low, close, period=14):
    tr       = pd.concat([high-low, (high-close.shift(1)).abs(),
                          (low-close.shift(1)).abs()], axis=1).max(axis=1)
    up       = high - high.shift(1)
    dn       = low.shift(1) - low
    plus_dm  = up.where((up > dn) & (up > 0), 0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0)
    atr      = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, np.nan)
    dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx      = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx.fillna(0)

def compute_rvwap(high, low, close, buy_vol, sell_vol, period):
    hlc3  = (high + low + close) / 3
    vol   = buy_vol + sell_vol
    pv    = hlc3 * vol
    return (pv.rolling(period, min_periods=1).sum() /
            vol.rolling(period, min_periods=1).sum().replace(0, np.nan)).ffill()

def softclamp(x):
    return np.tanh(x)

# ─── v7 Signal pipeline ───────────────────────────────────────────────────────

def compute_signal(df):
    print("Computing v7 signals...")
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    vol    = df["volume"]
    buy_v  = df["buy_volume"]
    sell_v = df["sell_volume"]

    # ADX — used only as adxMult regime filter, NOT in weighted sum
    adx      = compute_adx(high, low, close, DMI_LEN)
    adx_mult = ((adx - ADX_MIN) / (ADX_MAX - ADX_MIN)).clip(0, 1)

    # ── Statistically validated signals (ordered by Granger/F-test strength)
    # CVD: strongest Granger (p=0.0002)
    cvd_z    = rolling_zscore(buy_v - sell_v, Z_WINDOW)

    # VWAP: best rolling IR (-1.33), passes F-test
    vwap_z   = rolling_zscore(close - compute_rvwap(high, low, close, buy_v, sell_v, RVWAP_PERIOD), Z_WINDOW)

    # MFI: strong F-test (p=0.0002) + Granger ✓ — upgraded weight
    mfi_z    = rolling_zscore(compute_mfi(high, low, close, vol, MFI_PERIOD) - 50, Z_WINDOW)

    # RSI: marginal F-test + Granger ✓ — reduced weight (high VIF with DMI)
    rsi_z    = rolling_zscore(compute_rsi(close, RSI_PERIOD) - 50, Z_WINDOW)

    # Stochastic RSI: replaces DMI direction — captures momentum extremes
    # without the redundancy problem (DMI corr=0.93 with RSI)
    stoch_z  = rolling_zscore(compute_stoch_rsi(close, RSI_PERIOD, STOCH_K, STOCH_SMOOTH), Z_WINDOW)

    # CMF: replaces OI — volume flow signal independent from RSI/MFI
    cmf_z    = rolling_zscore(compute_cmf(high, low, close, vol, CMF_PERIOD), Z_WINDOW)

    # MACD: reduced weight — fails both F-test and Granger
    macd_z   = rolling_zscore(compute_macd_hist(close, MACD_FAST, MACD_SLOW, MACD_SIG), Z_WINDOW)

    # Funding: contrarian gate only, very low weight
    fund       = df["funding"].fillna(0)
    raw_fund_z = rolling_zscore(-fund, Z_WINDOW)
    fund_z     = raw_fund_z.where(raw_fund_z.abs() >= FUND_EXTREME, 0)

    # Liquidations: zero when not available
    liq_z = pd.Series(0.0, index=df.index)

    # ── Weighted aggregation
    # Trend signals (RSI, MFI, MACD, StochRSI) gated by adxMult
    # Microstructure signals (CVD, VWAP, CMF, Fund, Liq) always active
    trend_w  = (W_RSI + W_MFI + W_MACD + W_STOCH) * adx_mult
    micro_w  = pd.Series(W_CVD + W_VWAP + W_CMF + W_FUND + W_LIQ, index=df.index)
    total_w  = trend_w + micro_w

    raw_score = (
        rsi_z   * W_RSI   * adx_mult +
        mfi_z   * W_MFI   * adx_mult +
        macd_z  * W_MACD  * adx_mult +
        stoch_z * W_STOCH * adx_mult +
        cvd_z   * W_CVD   +
        vwap_z  * W_VWAP  +
        cmf_z   * W_CMF   +
        fund_z  * W_FUND  +
        liq_z   * W_LIQ
    ) / total_w.replace(0, np.nan).fillna(1)

    score    = softclamp(raw_score * 1.5)
    smoothed = score.ewm(span=SMOOTH_LEN, adjust=False).mean()
    atr      = pd.concat([high-low, (high-close.shift(1)).abs(),
                          (low-close.shift(1)).abs()], axis=1).max(axis=1)\
                    .ewm(alpha=1/ATR_PERIOD, adjust=False).mean()

    df = df.copy()
    df["score"]   = smoothed
    df["adx"]     = adx
    df["atr"]     = atr
    df["rsi_z"]   = rsi_z
    df["mfi_z"]   = mfi_z
    df["macd_z"]  = macd_z
    df["stoch_z"] = stoch_z
    df["cmf_z"]   = cmf_z
    df["cvd_z"]   = cvd_z
    df["fund_z"]  = fund_z
    df["vwap_z"]  = vwap_z
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
    dates     = df.index

    position     = 0
    entry_price  = 0.0
    entry_bar    = 0
    hwm = lwm    = 0.0
    trail_active = False
    consec_stops = 0
    cooldown_end = 0
    blocked_sma  = 0
    blocked_cb   = 0

    trades  = []
    equity  = [1.0]
    eq      = 1.0

    for i in range(1, len(df)):
        prev  = scores[i-1]
        curr  = scores[i]
        price = closes[i]
        hi    = highs[i]
        lo    = lows[i]
        atr   = atr_v[i]
        adx   = adx_v[i]
        al    = above_long[i]
        bs    = below_short[i]
        date  = dates[i]

        phase, long_mult, short_mult = get_cycle_phase(date)

        if position == 0:
            if i < cooldown_end:
                blocked_cb += 1
                equity.append(eq)
                continue

            is_trending  = adx >= ADX_ENTRY_MIN
            bull_cross   = prev <= BULL_THRESH and curr > BULL_THRESH
            bear_cross   = prev >= BEAR_THRESH and curr < BEAR_THRESH

            if is_trending and bull_cross and al:
                position = 1; entry_price = price*(1+SLIPPAGE)
                entry_bar = i; hwm = hi; trail_active = False

            elif is_trending and bear_cross and bs:
                position = -1; entry_price = price*(1-SLIPPAGE)
                entry_bar = i; lwm = lo; trail_active = False

            elif is_trending and (bull_cross or bear_cross):
                blocked_sma += 1

        elif position == 1:
            bars_held = i - entry_bar
            pnl_pct   = (price / entry_price) - 1

            if not trail_active and pnl_pct >= TRAIL_ACTIVATION:
                trail_active = True; hwm = hi
            if trail_active:
                hwm = max(hwm, hi)

            stop_level  = (hwm - ATR_MULT * atr) if trail_active else entry_price*(1-INITIAL_STOP_PCT)
            stop_hit    = lo <= stop_level
            tp_hit      = (hi / entry_price - 1) >= TAKE_PROFIT
            signal_exit = bars_held >= MIN_HOLD_BARS and curr < LONG_EXIT_SCORE

            if stop_hit or tp_hit or signal_exit:
                if stop_hit:
                    exit_price  = max(stop_level, lo)
                    exit_reason = "TRAIL_STOP" if trail_active else "INIT_STOP"
                    consec_stops += 1
                    if consec_stops >= MAX_CONSEC_STOPS:
                        cooldown_end = i + COOLDOWN_BARS; consec_stops = 0
                elif tp_hit:
                    exit_price  = entry_price * (1 + TAKE_PROFIT)
                    exit_reason = "TP"; consec_stops = 0
                else:
                    exit_price  = price * (1 - SLIPPAGE)
                    exit_reason = "SIGNAL"; consec_stops = 0

                raw_ret = (exit_price / entry_price - 1) - FEES * 2
                sized   = raw_ret * long_mult
                eq     *= (1 + sized)

                trades.append({
                    "entry_date": dates[entry_bar], "exit_date": date,
                    "direction": "LONG",
                    "entry_price": entry_price, "exit_price": exit_price,
                    "return_pct": raw_ret*100, "sized_pct": sized*100,
                    "bars_held": bars_held, "adx_entry": adx_v[entry_bar],
                    "cycle_phase": phase, "long_mult": long_mult,
                    "trail_active": trail_active, "exit_reason": exit_reason
                })
                position = 0; trail_active = False

        elif position == -1:
            bars_held = i - entry_bar
            pnl_pct   = (entry_price / price) - 1

            if not trail_active and pnl_pct >= TRAIL_ACTIVATION:
                trail_active = True; lwm = lo
            if trail_active:
                lwm = min(lwm, lo)

            stop_level  = (lwm + ATR_MULT * atr) if trail_active else entry_price*(1+INITIAL_STOP_PCT)
            stop_hit    = hi >= stop_level
            tp_hit      = (entry_price / lo - 1) >= TAKE_PROFIT
            signal_exit = bars_held >= MIN_HOLD_BARS and curr > SHORT_EXIT_SCORE

            if stop_hit or tp_hit or signal_exit:
                if stop_hit:
                    exit_price  = min(stop_level, hi)
                    exit_reason = "TRAIL_STOP" if trail_active else "INIT_STOP"
                    consec_stops += 1
                    if consec_stops >= MAX_CONSEC_STOPS:
                        cooldown_end = i + COOLDOWN_BARS; consec_stops = 0
                elif tp_hit:
                    exit_price  = entry_price * (1 - TAKE_PROFIT)
                    exit_reason = "TP"; consec_stops = 0
                else:
                    exit_price  = price * (1 + SLIPPAGE)
                    exit_reason = "SIGNAL"; consec_stops = 0

                raw_ret = (entry_price / exit_price - 1) - FEES * 2
                sized   = raw_ret * short_mult
                eq     *= (1 + sized)

                trades.append({
                    "entry_date": dates[entry_bar], "exit_date": date,
                    "direction": "SHORT",
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
    return trades_df, equity_s, blocked_sma, blocked_cb

# ─── Metrics ──────────────────────────────────────────────────────────────────

def compute_metrics(trades_df, equity_s, df, blocked_sma, blocked_cb):
    if trades_df.empty:
        print("No trades generated."); return

    raw   = trades_df["return_pct"]
    sized = trades_df["sized_pct"]
    wins  = trades_df[raw > 0]
    loss  = trades_df[raw <= 0]

    total_ret = (equity_s.iloc[-1] - 1) * 100
    buy_hold  = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    win_rate  = len(wins) / len(trades_df) * 100
    avg_win   = wins["return_pct"].mean()   if len(wins) else 0
    avg_loss  = loss["return_pct"].mean() if len(loss) else 0
    pf        = wins["return_pct"].sum() / abs(loss["return_pct"].sum()) \
                if len(loss) and loss["return_pct"].sum() != 0 else float("inf")
    expectancy = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)
    avg_bars   = trades_df["bars_held"].mean()
    avg_adx    = trades_df["adx_entry"].mean()

    daily_eq  = equity_s.resample("1D").last().ffill()
    daily_ret = daily_eq.pct_change().dropna()
    sharpe    = (daily_ret.mean() / daily_ret.std()) * np.sqrt(365) if daily_ret.std() > 0 else 0
    max_dd    = ((equity_s - equity_s.cummax()) / equity_s.cummax()).min() * 100

    longs   = trades_df[trades_df["direction"] == "LONG"]
    shorts  = trades_df[trades_df["direction"] == "SHORT"]
    stops   = trades_df[trades_df["exit_reason"].isin(["INIT_STOP","TRAIL_STOP"])]
    tps     = trades_df[trades_df["exit_reason"] == "TP"]
    sigs    = trades_df[trades_df["exit_reason"] == "SIGNAL"]

    results  = (raw > 0).astype(int).values
    streak, curr = 0, 0
    for r in results:
        curr = curr+1 if r==0 else 0; streak = max(streak, curr)

    print("\n" + "═"*68)
    print("  COMPOSITE SIGNAL v7 — BACKTEST RESULTS")
    print("═"*68)
    print(f"  Period:               {df.index[0].date()} → {df.index[-1].date()}")
    print(f"  Key changes from v6:  DMI dropped, OI dropped, VWAP↑ CVD↑ MFI↑")
    print(f"  New signals:          Stochastic RSI + Chaikin Money Flow")
    print(f"  Entries blocked SMA:  {blocked_sma}")
    print(f"  Entries blocked CB:   {blocked_cb}")
    print("─"*68)
    print(f"  Total trades:         {len(trades_df)}  (v6: 58)")
    print(f"    Longs:              {len(longs)}")
    print(f"    Shorts:             {len(shorts)}")
    print(f"  Avg ADX at entry:     {avg_adx:.1f}")
    print(f"  Win rate:             {win_rate:.1f}%  (v6: 36.2%)")
    print(f"  Avg win:              +{avg_win:.2f}%  (v6: +6.77%)")
    print(f"  Avg loss:             {avg_loss:.2f}%  (v6: -1.69%)")
    print(f"  Profit factor:        {pf:.2f}  (v6: 2.27)")
    print(f"  Expectancy/trade:     {expectancy:+.2f}%  (v6: +1.37%)")
    print(f"  Avg bars held:        {avg_bars:.1f}  (v6: 19.8)")
    print(f"  Max consec. losses:   {streak}")
    print("─"*68)
    print(f"  Exit breakdown:")
    print(f"    Signal exits:       {len(sigs)}  ({len(sigs)/len(trades_df)*100:.0f}%)")
    print(f"    Stop exits:         {len(stops)}  ({len(stops)/len(trades_df)*100:.0f}%)")
    print(f"    Take profit exits:  {len(tps)}  ({len(tps)/len(trades_df)*100:.0f}%)")
    if len(stops): print(f"    Avg stop return:    {stops['return_pct'].mean():.2f}%")
    if len(tps):   print(f"    Avg TP return:      +{tps['return_pct'].mean():.2f}%")
    if len(sigs):  print(f"    Avg signal return:  {sigs['return_pct'].mean():+.2f}%")
    print("─"*68)
    print(f"  Total return (sized): {total_ret:+.1f}%  (v6: +122.6%)")
    print(f"  Buy & hold:           {buy_hold:+.1f}%")
    print(f"  Sharpe ratio:         {sharpe:.2f}  (v6: 1.24)")
    print(f"  Max drawdown:         {max_dd:.1f}%  (v6: -25.3%)")
    print("─"*68)
    if len(longs):
        lwr = len(longs[longs["return_pct"]>0])/len(longs)*100
        print(f"  Long win rate:        {lwr:.1f}%  |  Total: {longs['return_pct'].sum():+.1f}%  (v6: +8.8% raw)")
    if len(shorts):
        swr = len(shorts[shorts["return_pct"]>0])/len(shorts)*100
        print(f"  Short win rate:       {swr:.1f}%  |  Total: {shorts['return_pct'].sum():+.1f}%  (v6: +85.8% raw)")
    print("═"*68)

    print(f"\n  PERFORMANCE BY CYCLE PHASE:")
    print(f"  {'Phase':<16} {'Trades':>6} {'Win%':>6} {'Avg Ret':>8} {'Total':>8}")
    print("  " + "─"*48)
    for phase in ["accumulation","bull","distribution","bear"]:
        ph = trades_df[trades_df["cycle_phase"]==phase]
        if not len(ph): print(f"  {phase:<16} {'0':>6}"); continue
        pw = len(ph[ph["return_pct"]>0])/len(ph)*100
        print(f"  {phase:<16} {len(ph):>6} {pw:>5.1f}% {ph['return_pct'].mean():>+7.2f}% {ph['return_pct'].sum():>+7.1f}%")

    print(f"\n  LAST 10 TRADES:")
    print(f"  {'Entry':<20} {'Exit':<20} {'Dir':<6} {'Entry$':<9} {'Exit$':<9} {'Bars':>4} {'Phase':<14} {'Reason':<11} {'Raw%':>7}")
    print("  " + "─"*107)
    for _, t in trades_df.tail(10).iterrows():
        print(f"  {str(t['entry_date'])[:19]:<20} {str(t['exit_date'])[:19]:<20} "
              f"{t['direction']:<6} {t['entry_price']:<9,.0f} {t['exit_price']:<9,.0f} "
              f"{int(t['bars_held']):>4} {t['cycle_phase']:<14} {t['exit_reason']:<11} "
              f"{t['return_pct']:>+6.2f}%")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    df        = fetch_ohlcv()
    daily_sma = fetch_daily_sma()

    df = df.join(daily_sma, how="left")
    df["sma_long"]        = df["sma_long"].ffill().fillna(0)
    df["sma_short"]       = df["sma_short"].ffill().fillna(0)
    df["above_sma_long"]  = df["above_sma_long"].ffill().fillna(False)
    df["below_sma_short"] = df["below_sma_short"].ffill().fillna(False)

    funding = fetch_funding()
    df = df.join(funding, how="left")
    df["funding"] = df["funding"].ffill().fillna(0)

    # OI still fetched but not used in signal (weight=0)
    # Kept for potential future use and to preserve data completeness
    oi_df = fetch_oi()
    df = df.join(oi_df, how="left")
    df["oi"] = df["oi"].ffill().fillna(0)

    df = compute_signal(df)

    trades_df, equity_s, blocked_sma, blocked_cb = run_backtest(df)
    compute_metrics(trades_df, equity_s, df, blocked_sma, blocked_cb)

    out = df[["open","high","low","close","volume","score","adx","atr",
              "rsi_z","mfi_z","macd_z","stoch_z","cmf_z","cvd_z","fund_z","vwap_z"]].copy()
    out.to_csv(OUT_SIGNAL)
    if not trades_df.empty:
        trades_df.to_csv(OUT_TRADES, index=False)
    print(f"\n  Signal data → {OUT_SIGNAL}")
    print(f"  Trade log   → {OUT_TRADES}")

if __name__ == "__main__":
    main()
