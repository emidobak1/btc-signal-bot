"""
Composite Signal Score — Python Backtest v6b
============================================
Base: v6 (Sharpe 1.24, profit factor 2.27, return +122.6%)

v7 taught us: dropping signals based on individual IC tests breaks confluence.
The composite works because of AGREEMENT between signals, not individual strength.

v6b applies statistical findings CORRECTLY:
  - Keep all v6 signals (no signals dropped)
  - DMI weight 1.0 → 0.4  (reduce redundancy with RSI, correlation=0.93)
  - VWAP weight 1.0 → 1.6  (best rolling IR -1.33, passes F-test — elevate)
  - MFI weight 1.0 → 1.4   (strong F-test p=0.0002 + Granger — small bump)
  - CVD weight 1.8 → 2.0   (strongest Granger p=0.0002 — small bump)
  - OI weight 1.5 → 0.3    (no edge with limited data — reduce, don't drop)
  - All other weights unchanged from v6
  - All execution logic identical to v6
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

RSI_PERIOD    = 14
MFI_PERIOD    = 14
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIG      = 9
DMI_LEN       = 14
RVWAP_PERIOD  = 168
SMOOTH_LEN    = 5
Z_WINDOW      = 200

# ── v6b weights — statistical adjustments without dropping signals ────────────
W_RSI   = 1.5   # unchanged
W_MFI   = 1.4   # 1.0→1.4  (strong F-test + Granger)
W_MACD  = 1.2   # unchanged
W_ADX   = 0.4   # 1.0→0.4  (reduce DMI redundancy with RSI, corr=0.93)
W_CVD   = 2.0   # 1.8→2.0  (strongest Granger p=0.0002)
W_OI    = 0.3   # 1.5→0.3  (reduce, no Granger edge with 30d data)
W_FUND  = 0.8   # unchanged
W_VWAP  = 1.6   # 1.0→1.6  (best rolling IR, passes F-test)
W_LIQ   = 1.2   # unchanged

# Entry / exit — all identical to v6
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

OUT_SIGNAL = "/Users/emidobak/Desktop/signal_data_v6b.csv"
OUT_TRADES = "/Users/emidobak/Desktop/trades_v6b.csv"

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
    print("Fetching daily SMA filters...")
    url, all_data, end_time = "https://fapi.binance.com/fapi/v1/klines", [], None
    for i in range(8):
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
        all_data = data + all_data; end_time = data[0]["fundingTime"] - 1
        time.sleep(0.1)
    df = pd.DataFrame(all_data)
    df["timestamp"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df["funding"] = df["fundingRate"].astype(float)
    return df[["funding"]].drop_duplicates().sort_index()

def fetch_oi():
    print("Fetching open interest...")
    url    = "https://fapi.binance.com/futures/data/openInterestHist"
    params = {"symbol": SYMBOL, "period": INTERVAL, "limit": 500}
    r      = requests.get(url, params=params, timeout=10); r.raise_for_status()
    df     = pd.DataFrame(r.json())
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df["oi"] = df["sumOpenInterestValue"].astype(float)
    return df[["oi"]].drop_duplicates().sort_index()

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
    return plus_di.fillna(0), minus_di.fillna(0), adx.fillna(0)

def compute_rvwap(high, low, close, buy_vol, sell_vol, period):
    hlc3  = (high + low + close) / 3
    vol   = buy_vol + sell_vol
    pv    = hlc3 * vol
    return (pv.rolling(period, min_periods=1).sum() /
            vol.rolling(period, min_periods=1).sum().replace(0, np.nan)).ffill()

def softclamp(x):
    return np.tanh(x)

# ─── Signal pipeline — identical to v6 structure, v6b weights ─────────────────

def compute_signal(df):
    print("Computing signals (v6b weights)...")
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    vol    = df["volume"]
    buy_v  = df["buy_volume"]
    sell_v = df["sell_volume"]

    rsi_z  = rolling_zscore(compute_rsi(close, RSI_PERIOD) - 50, Z_WINDOW)
    mfi_z  = rolling_zscore(compute_mfi(high, low, close, vol, MFI_PERIOD) - 50, Z_WINDOW)
    macd_z = rolling_zscore(compute_macd_hist(close, MACD_FAST, MACD_SLOW, MACD_SIG), Z_WINDOW)

    plus_di, minus_di, adx = compute_dmi(high, low, close, DMI_LEN)
    di_z     = rolling_zscore(plus_di - minus_di, Z_WINDOW)
    adx_mult = ((adx - ADX_MIN) / (ADX_MAX - ADX_MIN)).clip(0, 1)

    cvd_z    = rolling_zscore(buy_v - sell_v, Z_WINDOW)

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

    liq_z = pd.Series(0.0, index=df.index)

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
    atr      = pd.concat([high-low, (high-close.shift(1)).abs(),
                          (low-close.shift(1)).abs()], axis=1).max(axis=1)\
                    .ewm(alpha=1/ATR_PERIOD, adjust=False).mean()

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

# ─── Backtest engine — identical to v6 ───────────────────────────────────────

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

    position = 0; entry_price = 0.0; entry_bar = 0
    hwm = lwm = 0.0; trail_active = False
    consec_stops = 0; cooldown_end = 0
    blocked_sma = 0; blocked_cb = 0
    trades = []; equity = [1.0]; eq = 1.0

    for i in range(1, len(df)):
        prev  = scores[i-1]; curr = scores[i]
        price = closes[i]; hi = highs[i]; lo = lows[i]
        atr   = atr_v[i]; adx = adx_v[i]
        al    = above_long[i]; bs = below_short[i]
        date  = dates[i]
        phase, long_mult, short_mult = get_cycle_phase(date)

        if position == 0:
            if i < cooldown_end:
                blocked_cb += 1; equity.append(eq); continue

            is_trending = adx >= ADX_ENTRY_MIN
            bull_cross  = prev <= BULL_THRESH and curr > BULL_THRESH
            bear_cross  = prev >= BEAR_THRESH and curr < BEAR_THRESH

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
            if trail_active: hwm = max(hwm, hi)
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
                    exit_price = entry_price*(1+TAKE_PROFIT); exit_reason = "TP"; consec_stops = 0
                else:
                    exit_price = price*(1-SLIPPAGE); exit_reason = "SIGNAL"; consec_stops = 0

                raw_ret = (exit_price/entry_price - 1) - FEES*2
                sized   = raw_ret * long_mult; eq *= (1+sized)
                trades.append({
                    "entry_date": dates[entry_bar], "exit_date": date,
                    "direction": "LONG", "entry_price": entry_price, "exit_price": exit_price,
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
            if trail_active: lwm = min(lwm, lo)
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
                    exit_price = entry_price*(1-TAKE_PROFIT); exit_reason = "TP"; consec_stops = 0
                else:
                    exit_price = price*(1+SLIPPAGE); exit_reason = "SIGNAL"; consec_stops = 0

                raw_ret = (entry_price/exit_price - 1) - FEES*2
                sized   = raw_ret * short_mult; eq *= (1+sized)
                trades.append({
                    "entry_date": dates[entry_bar], "exit_date": date,
                    "direction": "SHORT", "entry_price": entry_price, "exit_price": exit_price,
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

    raw  = trades_df["return_pct"]
    wins = trades_df[raw > 0]
    loss = trades_df[raw <= 0]

    total_ret  = (equity_s.iloc[-1] - 1) * 100
    buy_hold   = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    win_rate   = len(wins) / len(trades_df) * 100
    avg_win    = wins["return_pct"].mean() if len(wins) else 0
    avg_loss   = loss["return_pct"].mean() if len(loss) else 0
    pf         = wins["return_pct"].sum() / abs(loss["return_pct"].sum()) \
                 if len(loss) and loss["return_pct"].sum() != 0 else float("inf")
    expectancy = (win_rate/100 * avg_win) + ((1-win_rate/100) * avg_loss)
    avg_bars   = trades_df["bars_held"].mean()
    avg_adx    = trades_df["adx_entry"].mean()

    daily_eq  = equity_s.resample("1D").last().ffill()
    daily_ret = daily_eq.pct_change().dropna()
    sharpe    = (daily_ret.mean()/daily_ret.std())*np.sqrt(365) if daily_ret.std()>0 else 0
    max_dd    = ((equity_s - equity_s.cummax())/equity_s.cummax()).min() * 100

    longs  = trades_df[trades_df["direction"]=="LONG"]
    shorts = trades_df[trades_df["direction"]=="SHORT"]
    stops  = trades_df[trades_df["exit_reason"].isin(["INIT_STOP","TRAIL_STOP"])]
    tps    = trades_df[trades_df["exit_reason"]=="TP"]
    sigs   = trades_df[trades_df["exit_reason"]=="SIGNAL"]

    results = (raw>0).astype(int).values
    streak, curr = 0, 0
    for r in results:
        curr = curr+1 if r==0 else 0; streak = max(streak, curr)

    print("\n" + "═"*68)
    print("  COMPOSITE SIGNAL v6b — BACKTEST RESULTS")
    print("═"*68)
    print(f"  Period:           {df.index[0].date()} → {df.index[-1].date()}")
    print(f"  Weight changes:   DMI↓0.4  VWAP↑1.6  MFI↑1.4  CVD↑2.0  OI↓0.3")
    print(f"  Blocked SMA:      {blocked_sma}  |  Blocked circuit break: {blocked_cb}")
    print("─"*68)
    print(f"  Total trades:     {len(trades_df)}  (v6: 58, v7: 65)")
    print(f"    Longs:          {len(longs)}  |  Shorts: {len(shorts)}")
    print(f"  Avg ADX at entry: {avg_adx:.1f}")
    print(f"  Win rate:         {win_rate:.1f}%  (v6: 36.2%)")
    print(f"  Avg win:          +{avg_win:.2f}%  (v6: +6.77%)")
    print(f"  Avg loss:         {avg_loss:.2f}%  (v6: -1.69%)")
    print(f"  Profit factor:    {pf:.2f}  (v6: 2.27)")
    print(f"  Expectancy/trade: {expectancy:+.2f}%  (v6: +1.37%)")
    print(f"  Avg bars held:    {avg_bars:.1f}  (v6: 19.8)")
    print(f"  Max consec loss:  {streak}")
    print("─"*68)
    print(f"  Signal exits:     {len(sigs)} ({len(sigs)/len(trades_df)*100:.0f}%)  "
          f"Stop: {len(stops)} ({len(stops)/len(trades_df)*100:.0f}%)  "
          f"TP: {len(tps)} ({len(tps)/len(trades_df)*100:.0f}%)")
    if len(stops): print(f"  Avg stop return:  {stops['return_pct'].mean():.2f}%")
    if len(tps):   print(f"  Avg TP return:    +{tps['return_pct'].mean():.2f}%")
    if len(sigs):  print(f"  Avg signal exit:  {sigs['return_pct'].mean():+.2f}%")
    print("─"*68)
    print(f"  Total return:     {total_ret:+.1f}%  (v6: +122.6%, v7: +28.2%)")
    print(f"  Buy & hold:       {buy_hold:+.1f}%")
    print(f"  Sharpe ratio:     {sharpe:.2f}  (v6: 1.24, v7: 0.52)")
    print(f"  Max drawdown:     {max_dd:.1f}%  (v6: -25.3%, v7: -30.1%)")
    print("─"*68)
    if len(longs):
        lwr = len(longs[longs["return_pct"]>0])/len(longs)*100
        print(f"  Long win rate:    {lwr:.1f}%  |  Total: {longs['return_pct'].sum():+.1f}%")
    if len(shorts):
        swr = len(shorts[shorts["return_pct"]>0])/len(shorts)*100
        print(f"  Short win rate:   {swr:.1f}%  |  Total: {shorts['return_pct'].sum():+.1f}%")
    print("═"*68)

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
    print(f"  {'Entry':<20} {'Exit':<20} {'Dir':<6} {'Entry$':<9} {'Exit$':<9} "
          f"{'Bars':>4} {'Phase':<14} {'Reason':<11} {'Raw%':>7}")
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

    oi_df = fetch_oi()
    df = df.join(oi_df, how="left")
    df["oi"]       = df["oi"].ffill().fillna(0)
    df["oi_delta"] = df["oi"].diff().fillna(0)

    df = compute_signal(df)

    trades_df, equity_s, blocked_sma, blocked_cb = run_backtest(df)
    compute_metrics(trades_df, equity_s, df, blocked_sma, blocked_cb)

    out = df[["open","high","low","close","volume","score","adx","atr",
              "rsi_z","mfi_z","macd_z","di_z","cvd_z","oi_z","fund_z","vwap_z"]].copy()
    out.to_csv(OUT_SIGNAL)
    if not trades_df.empty:
        trades_df.to_csv(OUT_TRADES, index=False)
    print(f"\n  Signal data → {OUT_SIGNAL}")
    print(f"  Trade log   → {OUT_TRADES}")

if __name__ == "__main__":
    main()
