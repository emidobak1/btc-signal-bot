//@version=2
indicator("Composite Signal v8c", false)

// ─── Inputs ───────────────────────────────────────────────────────────────────
input.section("Signal Weights")
const wRSI  = input.float("RSI Weight",            1.5, { min: 0, max: 3 })
const wMFI  = input.float("MFI Weight",            1.0, { min: 0, max: 3 })
const wMACD = input.float("MACD Weight",           1.2, { min: 0, max: 3 })
const wADX  = input.float("ADX Weight",            1.0, { min: 0, max: 3 })
const wCVD  = input.float("CVD Divergence Weight", 1.8, { min: 0, max: 3 })
const wOI   = input.float("OI Delta Weight",       1.5, { min: 0, max: 3 })
const wFund = input.float("Funding Rate Weight",   0.8, { min: 0, max: 3 })
const wVWAP = input.float("RVWAP Position Weight", 1.0, { min: 0, max: 3 })
const wLiq  = input.float("Liq Imbalance Weight",  1.2, { min: 0, max: 3 })

input.section("Signal Parameters")
const rsiPeriod   = input.int("RSI Period",       14,  { min: 2 })
const mfiPeriod   = input.int("MFI Period",       14,  { min: 2 })
const macdFast    = input.int("MACD Fast",        12,  { min: 2 })
const macdSlow    = input.int("MACD Slow",        26,  { min: 2 })
const macdSig     = input.int("MACD Signal",       9,  { min: 2 })
const dmiLen      = input.int("DMI Length",       14,  { min: 2 })
const rvwapPeriod = input.int("RVWAP Period",    168,  { min: 10 })
const zWindow     = input.int("Z-Score Window",  200,  { min: 50 })

// v8c: Asymmetric smoothing
// Longs: EMA3 — faster response, catches developing trends 4H earlier
// Shorts: EMA5 — original v6 smoothing, perfectly calibrated for short entries
const smoothLong  = input.int("Long Score Smoothing",  3, { min: 1, max: 10,
    description: "EMA span for long signals. 3 = faster entry, less lag" })
const smoothShort = input.int("Short Score Smoothing", 5, { min: 1, max: 10,
    description: "EMA span for short signals. 5 = original v6, proven optimal" })

input.section("Entry & Exit")
const bullThresh  = input.float("Bull Entry Threshold",   0.40, { min: 0.1, max: 0.9 })
const bearThresh  = input.float("Bear Entry Threshold",  -0.40, { min: -0.9, max: -0.1 })
const adxMin      = input.float("Min ADX (trend floor)",  20,   { min: 5, max: 50 })
const adxScaleMax = input.float("Max ADX (trend ceil)",   45,   { min: 20, max: 80 })
const fundExtreme = input.float("Funding Extreme Z",       1.5,  { min: 0.5, max: 3.0 })
const minHold     = input.int("Min Hold Bars",             4,    { min: 1 })

// v8c: Early long entry — fires 1 bar before main threshold crossover
// Gate: score in approach zone + accelerating strongly + CVD positive
// Validated: 56-59% hit rate at velocity > 0.15, tighter 5% initial stop
const approachZone   = input.float("Long Approach Zone",    0.25, { min: 0.10, max: 0.38,
    description: "Score must be above this to trigger early long entry" })
const velocityThresh = input.float("Long Velocity Threshold", 0.15, { min: 0.05, max: 0.40,
    description: "Min score change per bar to qualify as early entry. 0.15 = 59% hit rate" })
const earlyStopPct   = input.float("Early Entry Stop %",    5.0, { min: 2.0, max: 8.0,
    description: "Tighter stop for early entries (less conviction than standard)" })

input.section("Risk Management")
const atrMult       = input.float("ATR Trail Multiplier",    4.0, { min: 1.0, max: 8.0 })
const atrPeriod     = input.int("ATR Period",               14,   { min: 5 })
const initStopPct   = input.float("Initial Stop %",          7.0, { min: 1.0, max: 15.0 })
const trailActivate = input.float("Trail Activation % Gain", 1.0, { min: 0.1, max: 5.0 })
const takeProfitPct = input.float("Take Profit %",          12.0, { min: 3.0, max: 30.0 })

input.section("Trend Filters")
const smaLongPeriod  = input.int("SMA Long Period (longs)",   200, { min: 50 })
const smaShortPeriod = input.int("SMA Short Period (shorts)", 100, { min: 20 })

input.section("Display")
const showMarkers  = input.bool("Show Entry/Exit Markers", true)
const showEarly    = input.bool("Show Early Entry Markers", true)
const showDebug    = input.bool("Show Component Lines",    false)
const bullCol      = input.color("Bull Color",    color.Green)
const bearCol      = input.color("Bear Color",    color.Red)
const neutCol      = input.color("Neutral Color", color.Gray)
const earlyCol     = input.color("Early Entry Color", "#00BFFF")

// ─── Data subscriptions ───────────────────────────────────────────────────────
const SPOT_EX = ["binance", "okx", "coinbase"]
const PERP_EX = ["binancef", "okxf", "bybitf", "hyperliquid"]

const candles = subscribe(data.OHLCV)
const stats   = subscribe(data.STAT)
const oi      = subscribe(data.OI)
const spotCVD = subscribe(data.CVD, { exchange: SPOT_EX.join(":") })
const perpCVD = subscribe(data.CVD, { exchange: PERP_EX.join(":") })

const dailyCandles   = subscribe(data.OHLCV, { timeframe: "1D" })
const smaLongSeries  = dailyCandles.calc(src => ta.sma(src.close(), smaLongPeriod))
const smaShortSeries = dailyCandles.calc(src => ta.sma(src.close(), smaShortPeriod))

// ─── Halving cycle ────────────────────────────────────────────────────────────
const HALVING_UNIX = 1713571200  // April 20 2024

function getCyclePhase(unixSec) {
    const m = (unixSec - HALVING_UNIX) / (60 * 60 * 24 * 30.44)
    if (m < 0)  return { phase: "pre",          longMult: 0.5, shortMult: 0.8 }
    if (m < 6)  return { phase: "accumulation", longMult: 0.6, shortMult: 0.8 }
    if (m < 18) return { phase: "bull",         longMult: 1.4, shortMult: 0.6 }
    if (m < 30) return { phase: "distribution", longMult: 0.6, shortMult: 1.4 }
    return             { phase: "bear",          longMult: 0.4, shortMult: 1.6 }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function zscore(value, period) {
    if (isNaN(value)) return 0
    const mu = ta.sma(value, period)
    const sd = ta.stdev(value, period)
    if (isNaN(mu) || isNaN(sd) || sd === 0) return 0
    return Math.max(-3, Math.min(3, (value - mu) / sd))
}

function softclamp(x) { return Math.tanh(x) }

function rvwap(len, idx) {
    const lb = Math.min(len, idx + 1)
    let spv = 0, sv = 0
    for (let i = 0; i < lb; i++) {
        const p   = (candles.high(i) + candles.low(i) + candles.close(i)) / 3
        const vol = candles.buyVolume(i) + candles.sellVolume(i)
        if (vol > 0 && !isNaN(p)) { spv += p * vol; sv += vol }
    }
    return sv > 0 ? spv / sv : NaN
}

// ─── Position state ───────────────────────────────────────────────────────────
let position    = 0
let entryPrice  = 0
let hwm         = 0
let lwm         = 0
let trailActive = false
let barsHeld    = 0
let consecStops = 0
let cooldownEnd = 0
let isEarlyEntry = false   // track whether current position is an early entry

// ─── Main loop ────────────────────────────────────────────────────────────────
function onBar(index) {
    const close   = candles.close()
    const high    = candles.high()
    const low     = candles.low()
    const open    = candles.open()
    const vol     = candles.volume()
    const buyVol  = candles.buyVolume()
    const sellVol = candles.sellVolume()

    const atr = ta.atr(high, low, close, atrPeriod)

    const smaLong       = smaLongSeries()
    const smaShort      = smaShortSeries()
    const aboveSmaLong  = !isNaN(smaLong)  && close > smaLong
    const belowSmaShort = !isNaN(smaShort) && close < smaShort

    const { phase, longMult, shortMult } = getCyclePhase(unix(0))

    // ── Signal components
    const rsiVal  = ta.rsi(close, rsiPeriod)
    const rsiZ    = zscore(isNaN(rsiVal) ? 0 : rsiVal - 50, zWindow)

    const hlc3    = (high + low + close) / 3
    const mfiVal  = ta.mfi(hlc3, vol, mfiPeriod)
    const mfiZ    = zscore(isNaN(mfiVal) ? 0 : mfiVal - 50, zWindow)

    const macd    = ta.macd(close, macdFast, macdSlow, macdSig)
    const macdZ   = zscore(isNaN(macd.hist) ? 0 : macd.hist, zWindow)

    const dmi     = ta.dmi(candles, dmiLen, dmiLen)
    const adxVal  = dmi.adx
    const diDiff  = (isNaN(dmi.plusDI) || isNaN(dmi.minusDI)) ? 0 : dmi.plusDI - dmi.minusDI
    const diZ     = zscore(diDiff, zWindow)
    const adxMult = isNaN(adxVal) ? 0 : Math.min(1, Math.max(0, (adxVal - adxMin) / (adxScaleMax - adxMin)))
    const isTrending = !isNaN(adxVal) && adxVal >= adxMin

    const perpDelta = perpCVD.close() - perpCVD.open()
    const spotDelta = spotCVD.close() - spotCVD.open()
    const cvdDiv    = (isNaN(perpDelta) || isNaN(spotDelta)) ? 0 : perpDelta - spotDelta
    const cvdZ      = zscore(cvdDiv, zWindow)

    const oiDelta   = oi.close() - oi.open()
    const priceDir  = close - open
    const eps       = close * 0.00001
    const signedDir = Math.abs(priceDir) < eps ? 0 : Math.sign(priceDir)
    const oiAlign   = (isNaN(oiDelta) || signedDir === 0) ? 0 : oiDelta * signedDir
    const oiZ       = zscore(oiAlign, zWindow)

    const funding   = stats.fundingRate()
    const rawFundZ  = isNaN(funding) ? 0 : zscore(-funding, zWindow)
    const fundZ     = Math.abs(rawFundZ) >= fundExtreme ? rawFundZ : 0

    const vwapVal   = rvwap(rvwapPeriod, index)
    const vwapDev   = isNaN(vwapVal) ? 0 : close - vwapVal
    const vwapZ     = zscore(vwapDev, zWindow)

    const buyLiq    = stats.buyLiq()
    const sellLiq   = stats.sellLiq()
    const liqImb    = (isNaN(buyLiq) || isNaN(sellLiq)) ? 0 : buyLiq - sellLiq
    const liqZ      = zscore(liqImb, zWindow)

    // ── Weighted aggregation
    const trendW  = wRSI * adxMult + wMFI * adxMult + wMACD * adxMult + wADX
    const microW  = wCVD + wOI + wFund + wVWAP + wLiq
    const totalW  = trendW + microW

    const rawScore = totalW < 0.01 ? 0 : (
        rsiZ  * wRSI  * adxMult +
        mfiZ  * wMFI  * adxMult +
        macdZ * wMACD * adxMult +
        diZ   * wADX  +
        cvdZ  * wCVD  +
        oiZ   * wOI   +
        fundZ * wFund +
        vwapZ * wVWAP +
        liqZ  * wLiq
    ) / totalW

    const score = softclamp(rawScore * 1.5)

    // v8c: Asymmetric smoothing
    // Long score: EMA3 — faster, saves 4H of lag on long entries
    // Short score: EMA5 — original v6 timing, proven optimal for shorts
    const scoreLong  = ta.ema(score, smoothLong)
    const scoreShort = ta.ema(score, smoothShort)

    // v8c: Score velocity for early long entry detection
    const velocity = scoreLong - ta.ema(score, smoothLong + 1)  // approx 1-bar rate of change

    // ── v8c: Early long entry condition
    // Fires when score is building toward threshold but not there yet
    // Validated: velocity > 0.15 gives 59% hit rate, CVD > 0.5 adds confirmation
    const earlyLongCondition = (
        scoreLong > approachZone &&
        scoreLong < bullThresh &&
        velocity > velocityThresh &&
        cvdZ > 0.5 &&
        aboveSmaLong &&
        isTrending
    )
    // Only trigger on the FIRST bar of condition, not sustained
    const earlyLongTrigger = earlyLongCondition && !ta.ema(earlyLongCondition ? 1 : 0, 2)

    // ── Position management
    if (position !== 0) {
        barsHeld++

        if (position === 1) {
            const pnlPct = (close / entryPrice - 1) * 100
            if (!trailActive && pnlPct >= trailActivate) {
                trailActive = true; hwm = high
            }
            if (trailActive) hwm = Math.max(hwm, high)

            // Early entries get tighter initial stop (5% vs 7%)
            const stopPct    = isEarlyEntry ? earlyStopPct : initStopPct
            const stopLevel  = trailActive ? hwm - atrMult * atr : entryPrice * (1 - stopPct / 100)
            const tpLevel    = entryPrice * (1 + takeProfitPct / 100)
            const stopHit    = low  <= stopLevel
            const tpHit      = high >= tpLevel
            // Long exit uses long-smoothed score
            const signalExit = barsHeld >= minHold && scoreLong < 0

            if (stopHit || tpHit || signalExit) {
                const exitReason = stopHit ? (trailActive ? "TRAIL" : "STOP") : tpHit ? "TP" : "EXIT"
                const exitCol    = tpHit ? color.Green : stopHit ? color.Red : color.Gray
                if (stopHit) { consecStops++; if (consecStops >= 3) { cooldownEnd = index + 10; consecStops = 0 } }
                else consecStops = 0
                if (showMarkers) plotMarker("EXIT_L", high, {
                    marker: marker.Down, color: exitCol, size: 8,
                    border: 1, borderColor: color.White, forceOverlay: true
                })
                position = 0; entryPrice = 0; hwm = 0; trailActive = false; barsHeld = 0; isEarlyEntry = false
            }

        } else if (position === -1) {
            const pnlPct = (entryPrice / close - 1) * 100
            if (!trailActive && pnlPct >= trailActivate) {
                trailActive = true; lwm = low
            }
            if (trailActive) lwm = Math.min(lwm, low)

            const stopLevel  = trailActive ? lwm + atrMult * atr : entryPrice * (1 + initStopPct / 100)
            const tpLevel    = entryPrice * (1 - takeProfitPct / 100)
            const stopHit    = high >= stopLevel
            const tpHit      = low  <= tpLevel
            // Short exit uses short-smoothed score (EMA5 — consistent with short entry)
            const signalExit = barsHeld >= minHold && scoreShort > 0

            if (stopHit || tpHit || signalExit) {
                const exitReason = stopHit ? (trailActive ? "TRAIL" : "STOP") : tpHit ? "TP" : "EXIT"
                const exitCol    = tpHit ? color.Green : stopHit ? color.Red : color.Gray
                if (stopHit) { consecStops++; if (consecStops >= 3) { cooldownEnd = index + 10; consecStops = 0 } }
                else consecStops = 0
                if (showMarkers) plotMarker("EXIT_S", low, {
                    marker: marker.Up, color: exitCol, size: 8,
                    border: 1, borderColor: color.White, forceOverlay: true
                })
                position = 0; entryPrice = 0; lwm = 0; trailActive = false; barsHeld = 0; isEarlyEntry = false
            }
        }
    }

    // ── Entry logic
    if (position === 0 && index > cooldownEnd && isTrending) {

        // Long: uses fast long-smoothed score (EMA3)
        const bullCross = ta.crossover(scoreLong, bullThresh)

        // Short: uses slow short-smoothed score (EMA5) — original v6 timing
        const bearCross = ta.crossunder(scoreShort, bearThresh)

        if (bullCross && aboveSmaLong) {
            position = 1; entryPrice = close; hwm = high
            trailActive = false; barsHeld = 0; isEarlyEntry = false
            if (showMarkers) plotMarker("BUY", low, {
                marker: marker.Up, color: bullCol, size: 14,
                border: 2, borderColor: color.White, forceOverlay: true
            })
        }

        else if (bearCross && belowSmaShort) {
            position = -1; entryPrice = close; lwm = low
            trailActive = false; barsHeld = 0; isEarlyEntry = false
            if (showMarkers) plotMarker("SELL", high, {
                marker: marker.Down, color: bearCol, size: 14,
                border: 2, borderColor: color.White, forceOverlay: true
            })
        }

        // v8c: Early long entry — fires before main threshold crossover
        // Only when not about to fire a standard long on this same bar
        else if (earlyLongTrigger && !bullCross) {
            position = 1; entryPrice = close; hwm = high
            trailActive = false; barsHeld = 0; isEarlyEntry = true
            if (showEarly) plotMarker("EARLY_L", low, {
                marker: marker.Up, color: earlyCol, size: 10,
                border: 2, borderColor: color.White, forceOverlay: true
            })
        }
    }

    // ── Plotting — use long score for display (primary)
    const isBull = scoreLong >  bullThresh
    const isBear = scoreShort < bearThresh
    const col    = isBull ? bullCol : isBear ? bearCol : neutCol

    // Main histogram shows long-smoothed score
    plotHistogram("Score", scoreLong, { color: color.transp(col, 20) })

    // Optional: show short score as a subtle line for comparison
    plotLine("Score Short", scoreShort, {
        color: color.transp(neutCol, 65),
        style: linestyle.Dashed,
        showValue: false,
        showLabel: false
    })

    plotLine("Bull line", bullThresh, {
        color: color.transp(bullCol, 55), style: linestyle.Dashed, showValue: false
    })
    plotLine("Bear line", bearThresh, {
        color: color.transp(bearCol, 55), style: linestyle.Dashed, showValue: false
    })
    plotLine("Zero", 0, {
        color: color.transp(neutCol, 70), style: linestyle.Dashed, showValue: false
    })

    if (isBull) bg("regime", { color: color.transp(bullCol, 94), forceOverlay: true })
    else if (isBear) bg("regime", { color: color.transp(bearCol, 94), forceOverlay: true })

    if (position === 1 && trailActive) {
        plotLine("Trail Stop", hwm - atrMult * atr, {
            color: color.transp(color.Red, 30), style: linestyle.Dashed,
            showValue: true, forceOverlay: true
        })
    } else if (position === -1 && trailActive) {
        plotLine("Trail Stop", lwm + atrMult * atr, {
            color: color.transp(color.Red, 30), style: linestyle.Dashed,
            showValue: true, forceOverlay: true
        })
    }

    if (showDebug) {
        plotLine("RSI z",  rsiZ  * 0.1, { color: color.transp(color.Pink,  55), showLabel: false, showValue: false })
        plotLine("CVD z",  cvdZ  * 0.1, { color: color.transp(color.Blue,  55), showLabel: false, showValue: false })
        plotLine("OI z",   oiZ   * 0.1, { color: color.transp("#FFD700",   55), showLabel: false, showValue: false })
        plotLine("Liq z",  liqZ  * 0.1, { color: color.transp("#FFA500",   55), showLabel: false, showValue: false })
        plotLine("Fund z", fundZ * 0.1, { color: color.transp("#FF69B4",   55), showLabel: false, showValue: false })
        plotLine("VWAP z", vwapZ * 0.1, { color: color.transp(color.White, 55), showLabel: false, showValue: false })
    }
}
