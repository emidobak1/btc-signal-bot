"""
Composite Signal — Statistical Validation Suite
================================================
Tests each indicator component for:
  1. Predictive validity  — does it actually predict future returns?
  2. Multicollinearity    — are indicators too correlated with each other?
  3. Information content  — does each add unique information (partial F-test)?
  4. Stationarity         — are the series stationary (ADF test)?
  5. Granger causality    — does indicator X Granger-cause future price moves?
  6. Correlation matrix   — full pairwise Spearman rank correlation
  7. Walk-forward IC      — rolling Information Coefficient (IC) per indicator
  8. Composite VIF        — Variance Inflation Factor for multicollinearity
"""

import requests
import pandas as pd
import numpy as np
import time
from scipy import stats
from statsmodels.tsa.stattools import adfuller, grangercausalitytests
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant
from statsmodels.stats.outliers_influence import variance_inflation_factor
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler

# ─── Config ───────────────────────────────────────────────────────────────────
SYMBOL        = "BTCUSDT"
INTERVAL      = "4h"
LIMIT_PER_REQ = 1000
NUM_REQUESTS  = 5
FORWARD_BARS  = 6    # bars ahead to predict (24h on 4H)
ROLL_WINDOW   = 100  # rolling window for IC calculation
Z_WINDOW      = 200

# ─── Fetch data ───────────────────────────────────────────────────────────────
def fetch_ohlcv():
    print("Fetching OHLCV data...")
    url      = "https://fapi.binance.com/fapi/v1/klines"
    all_data = []
    end_time = None

    for i in range(NUM_REQUESTS):
        params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": LIMIT_PER_REQ}
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
    df = df[["open","high","low","close","volume","taker_buy_base"]].astype(float)
    df["buy_volume"]  = df["taker_buy_base"]
    df["sell_volume"] = df["volume"] - df["taker_buy_base"]
    df = df.drop_duplicates().sort_index()
    print(f"  Loaded {len(df)} bars | {df.index[0].date()} → {df.index[-1].date()}")
    return df

# ─── Compute all indicator signals ───────────────────────────────────────────
def rolling_zscore(series, window):
    mu = series.rolling(window, min_periods=max(2, window//4)).mean()
    sd = series.rolling(window, min_periods=max(2, window//4)).std()
    z  = (series - mu) / sd.replace(0, np.nan)
    return z.fillna(0).clip(-3, 3)

def compute_rsi(close, period=14):
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss  = (-delta).clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def compute_mfi(high, low, close, volume, period=14):
    tp      = (high + low + close) / 3
    mf      = tp * volume
    pos     = mf.where(tp > tp.shift(1), 0).rolling(period, min_periods=1).sum()
    neg     = mf.where(tp < tp.shift(1), 0).rolling(period, min_periods=1).sum()
    mfr     = pos / neg.replace(0, np.nan)
    return (100 - 100 / (1 + mfr)).fillna(50)

def compute_macd_hist(close, fast=12, slow=26, signal=9):
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    macd  = ema_f - ema_s
    sig   = macd.ewm(span=signal, adjust=False).mean()
    return macd - sig

def compute_adx(high, low, close, period=14):
    tr       = pd.concat([high-low, (high-close.shift(1)).abs(), (low-close.shift(1)).abs()], axis=1).max(axis=1)
    up       = high - high.shift(1)
    down     = low.shift(1) - low
    plus_dm  = up.where((up > down) & (up > 0), 0)
    minus_dm = down.where((down > up) & (down > 0), 0)
    atr      = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, np.nan)
    dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx      = dx.ewm(alpha=1/period, adjust=False).mean()
    return plus_di - minus_di, adx

def compute_rvwap_dev(high, low, close, buy_vol, sell_vol, period=168):
    hlc3  = (high + low + close) / 3
    vol   = buy_vol + sell_vol
    pv    = hlc3 * vol
    vwap  = pv.rolling(period, min_periods=1).sum() / vol.rolling(period, min_periods=1).sum().replace(0, np.nan)
    return close - vwap.ffill()

def compute_all_signals(df):
    print("Computing all signal components...")
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    vol    = df["volume"]
    buy_v  = df["buy_volume"]
    sell_v = df["sell_volume"]

    signals = pd.DataFrame(index=df.index)

    # Trend-following signals
    signals["rsi"]      = rolling_zscore(compute_rsi(close) - 50, Z_WINDOW)
    signals["mfi"]      = rolling_zscore(compute_mfi(high, low, close, vol) - 50, Z_WINDOW)
    signals["macd"]     = rolling_zscore(compute_macd_hist(close), Z_WINDOW)
    di_diff, adx        = compute_adx(high, low, close)
    signals["dmi"]      = rolling_zscore(di_diff, Z_WINDOW)
    signals["adx"]      = adx   # raw, used as multiplier — not z-scored

    # Microstructure signals
    signals["cvd"]      = rolling_zscore(buy_v - sell_v, Z_WINDOW)
    signals["vwap_dev"] = rolling_zscore(compute_rvwap_dev(high, low, close, buy_v, sell_v), Z_WINDOW)

    # Volume / momentum signals
    signals["vol_ratio"] = rolling_zscore(buy_v / vol.replace(0, np.nan) - 0.5, Z_WINDOW)
    signals["roc"]       = rolling_zscore(close.pct_change(6) * 100, Z_WINDOW)
    signals["atr_norm"]  = rolling_zscore(
        pd.concat([high-low, (high-close.shift(1)).abs(), (low-close.shift(1)).abs()], axis=1).max(axis=1)
        / close, Z_WINDOW
    )

    # Forward return — what we're trying to predict
    signals["fwd_ret_1"]  = close.pct_change(1).shift(-1)   * 100   # 1 bar ahead
    signals["fwd_ret_6"]  = close.pct_change(FORWARD_BARS).shift(-FORWARD_BARS) * 100  # 6 bars ahead
    signals["fwd_ret_12"] = close.pct_change(12).shift(-12) * 100   # 12 bars ahead

    signals["close"] = close

    return signals.dropna(subset=["fwd_ret_6"])

# ─── Statistical tests ────────────────────────────────────────────────────────

INDICATOR_COLS = ["rsi","mfi","macd","dmi","cvd","vwap_dev","vol_ratio","roc","atr_norm"]

def test_stationarity(signals):
    """ADF test — is each series stationary (mean-reverting)?"""
    print("\n" + "═"*66)
    print("  1. STATIONARITY TEST (Augmented Dickey-Fuller)")
    print("  H0: series has a unit root (non-stationary)")
    print("  Want: p < 0.05 to reject H0 → series IS stationary")
    print("═"*66)
    print(f"  {'Indicator':<14} {'ADF stat':>10} {'p-value':>10} {'Stationary?':>14}")
    print("  " + "─"*52)

    results = {}
    for col in INDICATOR_COLS:
        series = signals[col].dropna()
        if len(series) < 50:
            continue
        adf_stat, p_val, _, _, _, _ = adfuller(series, autolag='AIC')
        is_stat = p_val < 0.05
        flag = "YES ✓" if is_stat else "NO  ✗"
        print(f"  {col:<14} {adf_stat:>10.3f} {p_val:>10.4f} {flag:>14}")
        results[col] = {"adf": adf_stat, "p": p_val, "stationary": is_stat}

    return results

def test_predictive_power(signals):
    """Spearman rank IC — does each indicator predict forward returns?"""
    print("\n" + "═"*66)
    print("  2. PREDICTIVE POWER (Spearman Rank Correlation)")
    print("  IC = correlation between indicator and forward return")
    print("  |IC| > 0.05 = weak edge, > 0.10 = meaningful edge")
    print("═"*66)
    print(f"  {'Indicator':<14} {'IC 1bar':>8} {'IC 6bar':>8} {'IC 12bar':>9} {'p-value':>9} {'Edge?':>8}")
    print("  " + "─"*60)

    results = {}
    for col in INDICATOR_COLS:
        clean = signals[[col, "fwd_ret_1", "fwd_ret_6", "fwd_ret_12"]].dropna()
        if len(clean) < 100:
            continue

        ic1,  p1  = stats.spearmanr(clean[col], clean["fwd_ret_1"])
        ic6,  p6  = stats.spearmanr(clean[col], clean["fwd_ret_6"])
        ic12, p12 = stats.spearmanr(clean[col], clean["fwd_ret_12"])

        has_edge = abs(ic6) > 0.05 and p6 < 0.05
        flag = "YES ✓" if has_edge else "WEAK" if abs(ic6) > 0.03 else "NO  ✗"
        print(f"  {col:<14} {ic1:>8.4f} {ic6:>8.4f} {ic12:>9.4f} {p6:>9.4f} {flag:>8}")
        results[col] = {"ic1": ic1, "ic6": ic6, "ic12": ic12, "p": p6, "edge": has_edge}

    return results

def test_multicollinearity(signals):
    """VIF test — are indicators redundant with each other?"""
    print("\n" + "═"*66)
    print("  3. MULTICOLLINEARITY (Variance Inflation Factor)")
    print("  VIF < 5: acceptable, VIF 5-10: moderate, VIF > 10: severe")
    print("  High VIF = indicator adds no new information")
    print("═"*66)
    print(f"  {'Indicator':<14} {'VIF':>10} {'Assessment':>20}")
    print("  " + "─"*48)

    clean = signals[INDICATOR_COLS].dropna()
    X     = clean.values

    results = {}
    for i, col in enumerate(INDICATOR_COLS):
        vif = variance_inflation_factor(X, i)
        if vif < 5:
            assessment = "Good ✓"
        elif vif < 10:
            assessment = "Moderate ~"
        else:
            assessment = "Redundant ✗"
        print(f"  {col:<14} {vif:>10.2f} {assessment:>20}")
        results[col] = vif

    return results

def test_correlation_matrix(signals):
    """Spearman rank correlation matrix between all indicators"""
    print("\n" + "═"*66)
    print("  4. INDICATOR CORRELATION MATRIX (Spearman)")
    print("  Values close to ±1 = highly correlated (redundant)")
    print("  Want: most pairs below |0.5| for good diversification")
    print("═"*66)

    clean = signals[INDICATOR_COLS].dropna()
    corr  = clean.apply(lambda x: clean.apply(lambda y: stats.spearmanr(x, y)[0]))

    # Short names for table
    short = {"rsi":"RSI","mfi":"MFI","macd":"MACD","dmi":"DMI",
             "cvd":"CVD","vwap_dev":"VWAP","vol_ratio":"VOL","roc":"ROC","atr_norm":"ATR"}
    corr.index   = [short.get(c, c) for c in corr.index]
    corr.columns = [short.get(c, c) for c in corr.columns]

    print(f"\n  {'':8}", end="")
    for col in corr.columns:
        print(f"{col:>7}", end="")
    print()
    print("  " + "─"*72)

    for row in corr.index:
        print(f"  {row:8}", end="")
        for col in corr.columns:
            v = corr.loc[row, col]
            if row == col:
                print(f"{'  1.00':>7}", end="")
            elif abs(v) > 0.7:
                print(f"{'⚠'+f'{v:.2f}':>7}", end="")
            else:
                print(f"{v:>7.2f}", end="")
        print()

    # Flag high correlations
    high_corr = []
    for i, r in enumerate(corr.index):
        for j, c in enumerate(corr.columns):
            if i < j and abs(corr.loc[r, c]) > 0.6:
                high_corr.append((r, c, corr.loc[r, c]))

    if high_corr:
        print(f"\n  High correlations (|r| > 0.6):")
        for r, c, v in high_corr:
            print(f"    {r} ↔ {c}: {v:.3f} — consider reducing weight of one")
    else:
        print(f"\n  No high correlations found — good diversification ✓")

    return corr

def test_partial_f(signals):
    """Partial F-test — does each indicator add significant information
       above and beyond all others combined?"""
    print("\n" + "═"*66)
    print("  5. PARTIAL F-TEST (Incremental Information Content)")
    print("  Tests if removing one indicator significantly hurts prediction")
    print("  p < 0.05: indicator adds unique information worth keeping")
    print("═"*66)
    print(f"  {'Indicator':<14} {'F-stat':>10} {'p-value':>10} {'Keep?':>10}")
    print("  " + "─"*48)

    clean = signals[INDICATOR_COLS + ["fwd_ret_6"]].dropna()
    y     = clean["fwd_ret_6"].values
    X_all = add_constant(clean[INDICATOR_COLS].values)

    # Full model
    full_model = OLS(y, X_all).fit()
    rss_full   = full_model.ssr

    results = {}
    for i, col in enumerate(INDICATOR_COLS):
        # Restricted model — drop this indicator (col i+1 because of constant)
        cols_restricted = [j for j in range(X_all.shape[1]) if j != i + 1]
        X_restr         = X_all[:, cols_restricted]
        restr_model     = OLS(y, X_restr).fit()
        rss_restr       = restr_model.ssr

        # F statistic: (RSS_restr - RSS_full) / q / (RSS_full / df_full)
        q        = 1   # one constraint (dropped one variable)
        df_full  = len(y) - X_all.shape[1]
        f_stat   = ((rss_restr - rss_full) / q) / (rss_full / df_full)
        p_val    = 1 - stats.f.cdf(f_stat, q, df_full)

        keep = p_val < 0.05
        flag = "KEEP ✓" if keep else "MARGINAL" if p_val < 0.15 else "DROP  ✗"
        print(f"  {col:<14} {f_stat:>10.2f} {p_val:>10.4f} {flag:>10}")
        results[col] = {"f": f_stat, "p": p_val, "keep": keep}

    return results

def test_granger_causality(signals):
    """Granger causality — does indicator X predict future price changes?"""
    print("\n" + "═"*66)
    print("  6. GRANGER CAUSALITY (does indicator predict price moves?)")
    print("  p < 0.05: indicator Granger-causes forward price change")
    print("  Note: Granger causality ≠ true causality, but indicates edge")
    print("═"*66)
    print(f"  {'Indicator':<14} {'F-stat':>10} {'p-value':>10} {'Granger?':>12}")
    print("  " + "─"*50)

    price_change = signals["close"].pct_change().fillna(0) * 100
    results = {}

    for col in INDICATOR_COLS:
        try:
            combined = pd.DataFrame({
                "price_change": price_change,
                "indicator":    signals[col]
            }).dropna()

            if len(combined) < 100:
                continue

            # Test with lag = FORWARD_BARS
            gc_result = grangercausalitytests(
                combined[["price_change", "indicator"]],
                maxlag=FORWARD_BARS,
                verbose=False
            )

            # Take the minimum p-value across lags
            min_p = min(gc_result[lag][0]["ssr_ftest"][1] for lag in range(1, FORWARD_BARS+1))
            best_lag = min(gc_result.keys(), key=lambda lag: gc_result[lag][0]["ssr_ftest"][1])
            f_stat = gc_result[best_lag][0]["ssr_ftest"][0]

            granger = min_p < 0.05
            flag = "YES ✓" if granger else "NO  ✗"
            print(f"  {col:<14} {f_stat:>10.2f} {min_p:>10.4f} {flag:>12}")
            results[col] = {"f": f_stat, "p": min_p, "granger": granger}

        except Exception as e:
            print(f"  {col:<14} {'ERROR':>10} {str(e)[:20]}")

    return results

def test_rolling_ic(signals):
    """Rolling Information Coefficient — how consistent is each indicator's edge?"""
    print("\n" + "═"*66)
    print(f"  7. ROLLING IC CONSISTENCY (window={ROLL_WINDOW} bars)")
    print("  Mean IC, StdDev, and IR (IC/StdDev — like a Sharpe for signals)")
    print("  IR > 0.3 = reliable, > 0.5 = strong, > 1.0 = exceptional")
    print("═"*66)
    print(f"  {'Indicator':<14} {'Mean IC':>9} {'Std IC':>9} {'IR':>9} {'% pos':>9} {'Grade':>10}")
    print("  " + "─"*64)

    results = {}
    for col in INDICATOR_COLS:
        clean   = signals[[col, "fwd_ret_6"]].dropna()
        roll_ic = []

        for start in range(0, len(clean) - ROLL_WINDOW, ROLL_WINDOW // 2):
            window = clean.iloc[start:start + ROLL_WINDOW]
            if len(window) < 20:
                continue
            ic, _ = stats.spearmanr(window[col], window["fwd_ret_6"])
            if not np.isnan(ic):
                roll_ic.append(ic)

        if not roll_ic:
            continue

        roll_ic  = np.array(roll_ic)
        mean_ic  = roll_ic.mean()
        std_ic   = roll_ic.std()
        ir       = mean_ic / std_ic if std_ic > 0 else 0
        pct_pos  = (roll_ic > 0).mean() * 100

        if abs(ir) > 0.5:
            grade = "Strong ✓"
        elif abs(ir) > 0.3:
            grade = "Reliable"
        elif abs(ir) > 0.1:
            grade = "Weak"
        else:
            grade = "Noise ✗"

        print(f"  {col:<14} {mean_ic:>9.4f} {std_ic:>9.4f} {ir:>9.3f} {pct_pos:>8.1f}% {grade:>10}")
        results[col] = {"mean_ic": mean_ic, "std_ic": std_ic, "ir": ir, "pct_pos": pct_pos}

    return results

def summary_scorecard(stationarity, predictive, vif, partial_f, granger, rolling_ic):
    """Final scorecard combining all tests"""
    print("\n" + "═"*66)
    print("  FINAL SCORECARD — KEEP / ADJUST / REMOVE RECOMMENDATIONS")
    print("═"*66)
    print(f"  {'Indicator':<14} {'Stat':>5} {'IC':>5} {'VIF':>5} {'F':>5} {'GC':>5} {'IC_IR':>6} {'Action':>14}")
    print("  " + "─"*64)

    actions = {}
    for col in INDICATOR_COLS:
        stat_ok  = stationarity.get(col, {}).get("stationary", False)
        ic_ok    = abs(predictive.get(col, {}).get("ic6", 0)) > 0.04
        vif_ok   = vif.get(col, 9999) < 8
        f_ok     = partial_f.get(col, {}).get("keep", False)
        gc_ok    = granger.get(col, {}).get("granger", False)
        ir_val   = rolling_ic.get(col, {}).get("ir", 0)
        ir_ok    = abs(ir_val) > 0.2

        score    = sum([stat_ok, ic_ok, vif_ok, f_ok, gc_ok, ir_ok])

        if score >= 5:
            action = "KEEP ✓"
        elif score >= 3:
            action = "KEEP (adjust wt)"
        elif score >= 2:
            action = "REDUCE WEIGHT"
        else:
            action = "CONSIDER DROP"

        s = lambda b: "✓" if b else "✗"
        print(f"  {col:<14} {s(stat_ok):>5} {s(ic_ok):>5} {s(vif_ok):>5} "
              f"{s(f_ok):>5} {s(gc_ok):>5} {ir_val:>6.2f} {action:>14}")
        actions[col] = {"score": score, "action": action}

    print("═"*66)
    print("\n  COLUMN KEY: Stat=Stationarity, IC=Predictive power,")
    print("  VIF=Non-redundant, F=Partial F-test, GC=Granger causality,")
    print("  IC_IR=Rolling information ratio")

    return actions

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    df      = fetch_ohlcv()
    signals = compute_all_signals(df)

    print(f"\nSignal matrix shape: {signals.shape}")
    print(f"Date range: {signals.index[0].date()} → {signals.index[-1].date()}")

    stationarity = test_stationarity(signals)
    predictive   = test_predictive_power(signals)
    vif          = test_multicollinearity(signals)
    corr         = test_correlation_matrix(signals)
    partial_f    = test_partial_f(signals)
    granger      = test_granger_causality(signals)
    rolling_ic   = test_rolling_ic(signals)

    actions = summary_scorecard(
        stationarity, predictive, vif, partial_f, granger, rolling_ic
    )

    print("\n\n  RECOMMENDED WEIGHT ADJUSTMENTS FOR MMT INDICATOR:")
    print("  ─"*33)
    weight_map = {
        "rsi":      ("wRSI",   1.5),
        "mfi":      ("wMFI",   1.0),
        "macd":     ("wMACD",  1.2),
        "dmi":      ("wADX",   1.0),
        "cvd":      ("wCVD",   1.8),
        "vwap_dev": ("wVWAP",  1.0),
        "vol_ratio": ("wVOL",  0.0),
        "roc":      ("wROC",   0.0),
        "atr_norm": ("wATR",   0.0),
    }

    for col, data in actions.items():
        wname, current = weight_map.get(col, (col, 1.0))
        score = data["score"]
        if score >= 5:
            suggested = round(current * 1.2, 1)
            note = "→ increase weight"
        elif score >= 3:
            suggested = current
            note = "→ keep as is"
        elif score >= 2:
            suggested = round(current * 0.5, 1)
            note = "→ halve weight"
        else:
            suggested = 0.0
            note = "→ set to 0 or remove"
        print(f"  {wname:<8} currently {current:.1f}  suggested {suggested:.1f}  ({note})")

if __name__ == "__main__":
    main()
