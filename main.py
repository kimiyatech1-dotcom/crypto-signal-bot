import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import time
import os
import html
from datetime import datetime, timezone

# ============================================================
# VERSION 2 CONFIG
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

SCAN_INTERVAL = 900          # Scan every 15 minutes
TOP_N = 30                   # Coins to analyze
MAX_SIGNALS_PER_SCAN = 2     # Only strongest setups per scan
MAX_SIGNALS_PER_DAY = 6      # Hard daily Telegram limit

MIN_SCORE = 90               # Strict confluence score
MIN_ADX = 20
MIN_VOLUME_RATIO = 1.10

TIMEFRAMES = ["1d", "4h", "1h"]

# Do not trade stablecoins against stablecoins.
STABLE_BASES = {
    "USDT", "USDC", "FDUSD", "DAI", "TUSD",
    "USDP", "PYUSD", "BUSD", "USDE"
}

# Duplicate / cooldown control
sent_signals = {}
active_signals = {}
daily_sent = 0
daily_date = None

# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram credentials missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        response = requests.post(url, data=payload, timeout=10)

        if not response.ok:
            print("Telegram error:", response.text)
            return False

        return True

    except Exception as e:
        print("Telegram error:", e)
        return False


# ============================================================
# FEAR & GREED
# ============================================================

def get_fear_greed():
    try:
        r = requests.get(
            "https://api.alternative.me/fng/",
            timeout=10
        )
        data = r.json()["data"][0]
        return int(data["value"]), data["value_classification"]

    except Exception as e:
        print("Fear & Greed error:", e)
        return None, "Unknown"


# ============================================================
# DATA
# ============================================================

def candles_to_df(ohlcv):
    df = pd.DataFrame(
        ohlcv,
        columns=[
            "timestamp", "open", "high",
            "low", "close", "volume"
        ]
    )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        unit="ms",
        utc=True
    )

    return df


def get_closed_candles(exchange, symbol, timeframe, limit=250):
    ohlcv = exchange.fetch_ohlcv(
        symbol,
        timeframe=timeframe,
        limit=limit
    )

    df = candles_to_df(ohlcv)

    if len(df) < 210:
        return None

    # Never analyze the currently forming candle.
    return df.iloc[:-1].copy()


# ============================================================
# INDICATORS
# ============================================================

def add_indicators(df):
    df = df.copy()

    df["ema20"] = ta.ema(df["close"], length=20)
    df["ema50"] = ta.ema(df["close"], length=50)
    df["ema200"] = ta.ema(df["close"], length=200)

    df["rsi"] = ta.rsi(df["close"], length=14)

    macd = ta.macd(df["close"])
    if macd is not None:
        df = pd.concat([df, macd], axis=1)

    adx = ta.adx(
        df["high"],
        df["low"],
        df["close"],
        length=14
    )
    if adx is not None:
        df = pd.concat([df, adx], axis=1)

    df["atr"] = ta.atr(
        df["high"],
        df["low"],
        df["close"],
        length=14
    )

    bb = ta.bbands(
        df["close"],
        length=20,
        std=2
    )
    if bb is not None:
        df = pd.concat([df, bb], axis=1)

    df["volume_ma"] = df["volume"].rolling(20).mean()

    # Previous 20-candle structure, excluding current candle.
    df["recent_high"] = (
        df["high"].shift(1).rolling(20).max()
    )
    df["recent_low"] = (
        df["low"].shift(1).rolling(20).min()
    )

    return df.dropna().copy()


def get_macd_histogram(row):
    return float(row.get("MACDh_12_26_9", 0) or 0)


def get_macd_line(row):
    return float(row.get("MACD_12_26_9", 0) or 0)


def get_macd_signal(row):
    return float(row.get("MACDs_12_26_9", 0) or 0)


def get_adx_values(row):
    return (
        float(row.get("ADX_14", 0) or 0),
        float(row.get("DMP_14", 0) or 0),
        float(row.get("DMN_14", 0) or 0)
    )


# ============================================================
# TIMEFRAME ANALYSIS
# ============================================================

def analyze_timeframe(df, timeframe):
    df = add_indicators(df)

    if len(df) < 50:
        return None

    last = df.iloc[-1]
    previous = df.iloc[-2]

    bullish = 0
    bearish = 0

    bull_reasons = []
    bear_reasons = []

    # Trend structure
    if last["close"] > last["ema50"] > last["ema200"]:
        bullish += 20
        bull_reasons.append("Price > EMA50 > EMA200")

    elif last["close"] < last["ema50"] < last["ema200"]:
        bearish += 20
        bear_reasons.append("Price < EMA50 < EMA200")

    # Short-term momentum
    if last["close"] > last["ema20"]:
        bullish += 5
        bull_reasons.append("Price above EMA20")

    elif last["close"] < last["ema20"]:
        bearish += 5
        bear_reasons.append("Price below EMA20")

    # RSI
    rsi = float(last["rsi"])

    if 52 <= rsi <= 68:
        bullish += 10
        bull_reasons.append(f"RSI healthy bullish ({rsi:.1f})")

    elif 32 <= rsi <= 48:
        bearish += 10
        bear_reasons.append(f"RSI healthy bearish ({rsi:.1f})")

    elif rsi > 72:
        bearish += 5
        bear_reasons.append(f"RSI overextended ({rsi:.1f})")

    elif rsi < 28:
        bullish += 5
        bull_reasons.append(f"RSI oversold ({rsi:.1f})")

    # MACD
    hist = get_macd_histogram(last)
    prev_hist = get_macd_histogram(previous)
    macd_line = get_macd_line(last)
    macd_signal = get_macd_signal(last)

    if hist > 0 and macd_line > macd_signal:
        bullish += 10
        bull_reasons.append("MACD bullish")

        if prev_hist <= 0:
            bullish += 5
            bull_reasons.append("Fresh MACD bullish crossover")

    elif hist < 0 and macd_line < macd_signal:
        bearish += 10
        bear_reasons.append("MACD bearish")

        if prev_hist >= 0:
            bearish += 5
            bear_reasons.append("Fresh MACD bearish crossover")

    # ADX / DI
    adx, plus_di, minus_di = get_adx_values(last)

    if adx >= MIN_ADX:
        if plus_di > minus_di:
            bullish += 10
            bull_reasons.append(f"ADX bullish ({adx:.1f})")
        elif minus_di > plus_di:
            bearish += 10
            bear_reasons.append(f"ADX bearish ({adx:.1f})")

    # Volume
    volume_ratio = 0.0
    if last["volume_ma"] > 0:
        volume_ratio = float(last["volume"] / last["volume_ma"])

    if volume_ratio >= MIN_VOLUME_RATIO:
        if last["close"] > last["open"]:
            bullish += 5
            bull_reasons.append(
                f"Volume confirmation ({volume_ratio:.1f}x)"
            )
        elif last["close"] < last["open"]:
            bearish += 5
            bear_reasons.append(
                f"Volume confirmation ({volume_ratio:.1f}x)"
            )

    # Breakout / breakdown
    previous_high = float(df["high"].iloc[-21:-1].max())
    previous_low = float(df["low"].iloc[-21:-1].min())

    breakout = last["close"] > previous_high
    breakdown = last["close"] < previous_low

    if breakout:
        bullish += 10
        bull_reasons.append("Breakout above recent structure")

    elif breakdown:
        bearish += 10
        bear_reasons.append("Breakdown below recent structure")

    if bullish > bearish:
        direction = "BULLISH"
    elif bearish > bullish:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    return {
        "timeframe": timeframe,
        "direction": direction,
        "bullish": bullish,
        "bearish": bearish,
        "bull_reasons": bull_reasons,
        "bear_reasons": bear_reasons,
        "price": float(last["close"]),
        "atr": float(last["atr"]),
        "rsi": rsi,
        "adx": adx,
        "volume_ratio": volume_ratio,
        "timestamp": last["timestamp"],
        "breakout": breakout,
        "breakdown": breakdown
    }


# ============================================================
# BTC MARKET
# ============================================================

def get_btc_market_analysis(exchange):
    results = {}

    for tf in TIMEFRAMES:
        try:
            df = get_closed_candles(
                exchange, "BTC/USDT", tf, 250
            )

            if df is None:
                continue

            result = analyze_timeframe(df, tf.upper())

            if result:
                results[tf] = result

        except Exception as e:
            print(f"BTC {tf} analysis error:", e)

    if not results:
        return "NEUTRAL", 0, results

    bullish_count = sum(
        x["direction"] == "BULLISH"
        for x in results.values()
    )

    bearish_count = sum(
        x["direction"] == "BEARISH"
        for x in results.values()
    )

    if bullish_count == 3:
        return "BULLISH", 3, results

    if bearish_count == 3:
        return "BEARISH", -3, results

    if bullish_count > bearish_count:
        return "BULLISH", 1, results

    if bearish_count > bullish_count:
        return "BEARISH", -1, results

    return "NEUTRAL", 0, results


# ============================================================
# SIGNAL QUALITY
# ============================================================

def build_signal(symbol, analyses, btc_trend, btc_score, fg_value, fg_text):
    if not all(tf in analyses for tf in TIMEFRAMES):
        return None

    daily = analyses["1d"]
    four_h = analyses["4h"]
    one_h = analyses["1h"]

    all_bullish = all(
        analyses[tf]["direction"] == "BULLISH"
        for tf in TIMEFRAMES
    )

    all_bearish = all(
        analyses[tf]["direction"] == "BEARISH"
        for tf in TIMEFRAMES
    )

    # Do not allow weak/sideways 1H entries.
    if one_h["adx"] < MIN_ADX:
        return None

    # ========================================================
    # LONG
    # ========================================================

    if all_bullish:
        score = 0
        reasons = []

        score += 30
        reasons.append("1D bullish trend confirmed")

        score += 30
        reasons.append("4H bullish structure confirmed")

        score += 25
        reasons.append("1H bullish entry confirmation")

        # BTC alignment: full points only when BTC is bullish.
        if btc_trend == "BULLISH":
            score += 10
            reasons.append("BTC market aligned bullish")
        elif btc_trend == "NEUTRAL":
            score += 4
            reasons.append("BTC market neutral")
        else:
            # Strongly bearish BTC is a hard filter.
            if btc_score <= -3:
                return None
            score += 0
            reasons.append("BTC market bearish — no alignment points")

        # F&G is a small context filter, not a trade trigger.
        if fg_value is not None and 35 <= fg_value <= 75:
            score += 5
            reasons.append(f"Market sentiment acceptable ({fg_value})")
        elif fg_value is not None and fg_value > 85:
            return None
        else:
            score += 0

        # Strict 1H confirmation.
        if one_h["rsi"] < 52 or one_h["rsi"] > 68:
            return None

        if one_h["volume_ratio"] < MIN_VOLUME_RATIO:
            return None

        if not (
            one_h["bullish"] > one_h["bearish"]
            and get_macd_histogram_from_analysis(one_h) > 0
        ):
            return None

        # Reward structure confirmation.
        reasons.extend(one_h["bull_reasons"][:5])

        if score < MIN_SCORE:
            return None

        return make_long_signal(
            symbol, score, reasons,
            daily, four_h, one_h,
            btc_trend, fg_text
        )

    # ========================================================
    # SHORT
    # ========================================================

    if all_bearish:
        score = 0
        reasons = []

        score += 30
        reasons.append("1D bearish trend confirmed")

        score += 30
        reasons.append("4H bearish structure confirmed")

        score += 25
        reasons.append("1H bearish entry confirmation")

        if btc_trend == "BEARISH":
            score += 10
            reasons.append("BTC market aligned bearish")
        elif btc_trend == "NEUTRAL":
            score += 4
            reasons.append("BTC market neutral")
        else:
            if btc_score >= 3:
                return None
            score += 0
            reasons.append("BTC market bullish — no alignment points")

        if fg_value is not None and 25 <= fg_value <= 65:
            score += 5
            reasons.append(f"Market sentiment acceptable ({fg_value})")
        elif fg_value is not None and fg_value < 15:
            return None

        if one_h["rsi"] < 32 or one_h["rsi"] > 48:
            return None

        if one_h["volume_ratio"] < MIN_VOLUME_RATIO:
            return None

        if not (
            one_h["bearish"] > one_h["bullish"]
            and get_macd_histogram_from_analysis(one_h) < 0
        ):
            return None

        reasons.extend(one_h["bear_reasons"][:5])

        if score < MIN_SCORE:
            return None

        return make_short_signal(
            symbol, score, reasons,
            daily, four_h, one_h,
            btc_trend, fg_text
        )

    return None


# ============================================================
# MACD STATE STORED IN ANALYSIS
# ============================================================

# These helpers use the direction/reason set because the original
# dataframe is intentionally not kept in memory.
def get_macd_histogram_from_analysis(analysis):
    for reason in analysis.get("bull_reasons", []):
        if "MACD bullish" in reason:
            return 1.0
    for reason in analysis.get("bear_reasons", []):
        if "MACD bearish" in reason:
            return -1.0
    return 0.0


# ============================================================
# SIGNAL CONSTRUCTION
# ============================================================

def make_long_signal(
    symbol, score, reasons,
    daily, four_h, one_h,
    btc_trend, fg_text
):
    price = one_h["price"]
    atr = one_h["atr"]

    stop_loss = price - (atr * 1.5)
    risk = price - stop_loss

    if risk <= 0:
        return None

    tp1 = price + risk * 1.5
    tp2 = price + risk * 2.5
    tp3 = price + risk * 3.5

    return {
        "signal": "🟢 HIGH-CONFLUENCE LONG",
        "side": "LONG",
        "symbol": symbol,
        "score": score,
        "price": price,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "risk_reward": 3.5,
        "reasons": list(dict.fromkeys(reasons)),
        "daily": daily["direction"],
        "four_h": four_h["direction"],
        "one_h": one_h["direction"],
        "btc": btc_trend,
        "fg": fg_text,
        "candle_time": one_h["timestamp"]
    }


def make_short_signal(
    symbol, score, reasons,
    daily, four_h, one_h,
    btc_trend, fg_text
):
    price = one_h["price"]
    atr = one_h["atr"]

    stop_loss = price + (atr * 1.5)
    risk = stop_loss - price

    if risk <= 0:
        return None

    tp1 = price - risk * 1.5
    tp2 = price - risk * 2.5
    tp3 = price - risk * 3.5

    return {
        "signal": "🔴 HIGH-CONFLUENCE SHORT",
        "side": "SHORT",
        "symbol": symbol,
        "score": score,
        "price": price,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "risk_reward": 3.5,
        "reasons": list(dict.fromkeys(reasons)),
        "daily": daily["direction"],
        "four_h": four_h["direction"],
        "one_h": one_h["direction"],
        "btc": btc_trend,
        "fg": fg_text,
        "candle_time": one_h["timestamp"]
    }


# ============================================================
# PRICE FORMAT
# ============================================================

def format_price(price):
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:,.4f}"
    if price >= 0.01:
        return f"{price:.6f}"
    return f"{price:.8f}"


# ============================================================
# TELEGRAM HTML SAFE SIGNAL
# ============================================================

def format_signal(signal):
    reasons = "\n".join(
        f"• {html.escape(str(r))}"
        for r in signal["reasons"][:10]
    )

    symbol = html.escape(signal["symbol"])
    side_signal = html.escape(signal["signal"])
    daily = html.escape(signal["daily"])
    four_h = html.escape(signal["four_h"])
    one_h = html.escape(signal["one_h"])
    btc = html.escape(signal["btc"])
    fg = html.escape(str(signal["fg"]))

    return f"""
<b>{side_signal}</b>

━━━━━━━━━━━━━━━━━━

🪙 <b>Coin:</b> {symbol}

💰 <b>Entry:</b>
{format_price(signal["price"])}

🛑 <b>Stop Loss:</b>
{format_price(signal["stop_loss"])}

🎯 <b>TP1:</b>
{format_price(signal["tp1"])}

🎯 <b>TP2:</b>
{format_price(signal["tp2"])}

🎯 <b>TP3:</b>
{format_price(signal["tp3"])}

📊 <b>Confluence Score:</b>
{signal["score"]}/100

📈 <b>1D:</b> {daily}
📊 <b>4H:</b> {four_h}
⏰ <b>1H:</b> {one_h}

₿ <b>BTC Market:</b>
{btc}

😱 <b>Fear &amp; Greed:</b>
{fg}

⚖️ <b>Target R:R:</b>
1:{signal["risk_reward"]}

━━━━━━━━━━━━━━━━━━

<b>Confirmations:</b>

{reasons}

━━━━━━━━━━━━━━━━━━

⚠️ <i>High-confluence setup, not guaranteed profit.
Use proper risk management.</i>
"""


# ============================================================
# COIN UNIVERSE
# ============================================================

def get_top_coins(exchange):
    try:
        tickers = exchange.fetch_tickers()
        usdt_pairs = []

        for symbol, data in tickers.items():
            if not symbol.endswith("/USDT"):
                continue

            base = symbol.split("/")[0].upper()

            if base in STABLE_BASES:
                continue

            quote_volume = data.get("quoteVolume")

            if not quote_volume:
                continue

            if any(
                x in symbol
                for x in [
                    "UP/", "DOWN/", "3L/",
                    "3S/", "5L/", "5S/"
                ]
            ):
                continue

            usdt_pairs.append({
                "symbol": symbol,
                "volume": float(quote_volume)
            })

        usdt_pairs.sort(
            key=lambda x: x["volume"],
            reverse=True
        )

        coins = [x["symbol"] for x in usdt_pairs[:TOP_N]]

        # Exclude BTC from altcoin candidates because BTC
        # is already used as the market filter.
        coins = [
            x for x in coins
            if x not in {"BTC/USDT", "USDC/USDT"}
        ]

        return coins

    except Exception as e:
        print("Top coins error:", e)
        return [
            "ETH/USDT",
            "SOL/USDT",
            "XRP/USDT",
            "DOGE/USDT"
        ]


# ============================================================
# DAILY LIMIT / DUPLICATE CONTROL
# ============================================================

def reset_daily_counter():
    global daily_sent, daily_date

    today = datetime.now(timezone.utc).date()

    if daily_date != today:
        daily_date = today
        daily_sent = 0


def signal_key(signal):
    # Same direction + same completed 1H candle = same setup.
    candle = signal["candle_time"].isoformat()
    return f"{signal['symbol']}|{signal['side']}|{candle}"


def has_active_same_coin(symbol):
    return symbol in active_signals


def remember_signal(signal):
    global daily_sent

    key = signal_key(signal)

    sent_signals[key] = time.time()

    active_signals[signal["symbol"]] = {
        "side": signal["side"],
        "entry": signal["price"],
        "sl": signal["stop_loss"],
        "tp1": signal["tp1"],
        "tp2": signal["tp2"],
        "tp3": signal["tp3"],
        "created": time.time()
    }

    daily_sent += 1



# ============================================================
# ACTIVE SIGNAL HOUSEKEEPING
# ============================================================

def cleanup_active_signals(exchange):
    """Remove setups that have reached SL/TP3 or expired."""
    now = time.time()
    expired = []

    for symbol, data in list(active_signals.items()):
        try:
            # 24-hour maximum lifetime for an unresolved setup.
            if now - data["created"] > 24 * 60 * 60:
                expired.append(symbol)
                continue

            ticker = exchange.fetch_ticker(symbol)
            current = ticker.get("last")

            if current is None:
                continue

            current = float(current)

            if data["side"] == "LONG":
                if current <= data["sl"] or current >= data["tp3"]:
                    expired.append(symbol)

            elif data["side"] == "SHORT":
                if current >= data["sl"] or current <= data["tp3"]:
                    expired.append(symbol)

        except Exception as e:
            print(f"Active signal check error {symbol}:", e)

    for symbol in expired:
        active_signals.pop(symbol, None)

# ============================================================
# SCAN
# ============================================================

def scan_market(exchange):
    global sent_signals, active_signals

    reset_daily_counter()
    cleanup_active_signals(exchange)

    btc_trend, btc_score, btc_analysis = (
        get_btc_market_analysis(exchange)
    )

    fg_value, fg_text = get_fear_greed()

    coins = get_top_coins(exchange)

    print(
        f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"
    )
    print(f"BTC Market: {btc_trend}")
    print(f"Fear & Greed: {fg_text}")
    print(f"Scanning {len(coins)} coins...")
    print(f"Daily signals: {daily_sent}/{MAX_SIGNALS_PER_DAY}")

    if daily_sent >= MAX_SIGNALS_PER_DAY:
        print("Daily signal limit reached. No new signals.")
        return

    candidates = []

    for symbol in coins:
        try:
            # Do not repeatedly alert on a coin that already has
            # an unresolved setup.
            if has_active_same_coin(symbol):
                continue

            analyses = {}

            for tf in TIMEFRAMES:
                df = get_closed_candles(
                    exchange,
                    symbol,
                    tf,
                    250
                )

                if df is None:
                    continue

                result = analyze_timeframe(
                    df,
                    tf.upper()
                )

                if result:
                    analyses[tf] = result

            signal = build_signal(
                symbol,
                analyses,
                btc_trend,
                btc_score,
                fg_value,
                fg_text
            )

            if signal:
                candidates.append(signal)

        except Exception as e:
            print(f"Error analyzing {symbol}:", e)

    # Strongest candidates first.
    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    remaining = MAX_SIGNALS_PER_SCAN

    for signal in candidates:
        if remaining <= 0:
            break

        if daily_sent >= MAX_SIGNALS_PER_DAY:
            break

        key = signal_key(signal)

        if key in sent_signals:
            continue

        message = format_signal(signal)

        if send_telegram(message):
            remember_signal(signal)
            remaining -= 1

            print(
                f"🚨 SIGNAL: "
                f"{signal['symbol']} "
                f"{signal['side']} "
                f"{signal['score']}/100"
            )

            time.sleep(1.5)

    print(
        f"Candidates found: {len(candidates)} | "
        f"Signals sent: {MAX_SIGNALS_PER_SCAN - remaining}"
    )

    # Keep memory bounded.
    if len(sent_signals) > 1000:
        sent_signals = dict(
            list(sent_signals.items())[-500:]
        )


# ============================================================
# RUN BOT
# ============================================================

def run_bot():
    exchange = ccxt.okx({
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot"
        }
    })

    print("================================================")
    print("Crypto High-Confluence Signal Bot V2 Started")
    print("Exchange: OKX")
    print("Timeframes: 1D + 4H + 1H")
    print("Minimum Score:", MIN_SCORE)
    print("Max Signals / Scan:", MAX_SIGNALS_PER_SCAN)
    print("Max Signals / Day:", MAX_SIGNALS_PER_DAY)
    print("================================================")

    send_telegram(
        """
<b>🚀 Crypto Signal Bot V2 Started</b>

Exchange: OKX

<b>Strict Analysis:</b>
• 1D Trend
• 4H Structure
• 1H Entry
• BTC Market Alignment
• EMA 20/50/200
• RSI
• MACD
• ADX
• Volume
• ATR
• Fear &amp; Greed
• Duplicate Protection
• Signal Ranking

🎯 Minimum Confluence: 90/100
📨 Max Signals/Scan: 2
📅 Max Signals/Day: 6

Bot will stay silent when no strong setup exists.

⚠️ Paper/demo testing only.
"""
    )

    while True:
        try:
            scan_market(exchange)

            print(
                f"Scan complete. Next scan in "
                f"{SCAN_INTERVAL // 60} minutes."
            )

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            print("Bot stopped.")
            break

        except Exception as e:
            print("MAIN ERROR:", e)
            time.sleep(60)


if __name__ == "__main__":
    run_bot()
