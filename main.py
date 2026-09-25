import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import time
import os
from datetime import datetime, timezone

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

SCAN_INTERVAL = 900       # 15 minutes
TOP_N = 30
MIN_SCORE = 80

TIMEFRAMES = ["1d", "4h", "1h"]

# Prevent repeated signals
sent_signals = {}

# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram credentials missing.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        response = requests.post(
            url,
            data=payload,
            timeout=10
        )

        if not response.ok:
            print("Telegram error:", response.text)

    except Exception as e:
        print("Telegram error:", e)


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

        return (
            int(data["value"]),
            data["value_classification"]
        )

    except Exception as e:
        print("Fear & Greed error:", e)
        return None, "Unknown"


# ============================================================
# DATAFRAME
# ============================================================

def candles_to_df(ohlcv):

    df = pd.DataFrame(
        ohlcv,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        unit="ms"
    )

    return df


# ============================================================
# REMOVE CURRENT INCOMPLETE CANDLE
# ============================================================

def get_closed_candles(exchange, symbol, timeframe, limit=250):

    ohlcv = exchange.fetch_ohlcv(
        symbol,
        timeframe=timeframe,
        limit=limit
    )

    df = candles_to_df(ohlcv)

    if len(df) < 50:
        return None

    # Last candle can still be forming.
    # Use only completed candles.
    df = df.iloc[:-1].copy()

    return df


# ============================================================
# INDICATORS
# ============================================================

def add_indicators(df):

    df = df.copy()

    # EMAs
    df["ema20"] = ta.ema(df["close"], length=20)
    df["ema50"] = ta.ema(df["close"], length=50)
    df["ema200"] = ta.ema(df["close"], length=200)

    # RSI
    df["rsi"] = ta.rsi(
        df["close"],
        length=14
    )

    # MACD
    macd = ta.macd(df["close"])

    if macd is not None:
        df = pd.concat(
            [df, macd],
            axis=1
        )

    # ADX
    adx = ta.adx(
        df["high"],
        df["low"],
        df["close"],
        length=14
    )

    if adx is not None:
        df = pd.concat(
            [df, adx],
            axis=1
        )

    # ATR
    df["atr"] = ta.atr(
        df["high"],
        df["low"],
        df["close"],
        length=14
    )

    # Bollinger Bands
    bb = ta.bbands(
        df["close"],
        length=20,
        std=2
    )

    if bb is not None:
        df = pd.concat(
            [df, bb],
            axis=1
        )

    # Volume average
    df["volume_ma"] = (
        df["volume"]
        .rolling(20)
        .mean()
    )

    # Recent highs/lows
    df["recent_high"] = (
        df["high"]
        .rolling(20)
        .max()
    )

    df["recent_low"] = (
        df["low"]
        .rolling(20)
        .min()
    )

    return df.dropna().copy()


# ============================================================
# GET MACD HISTOGRAM
# ============================================================

def get_macd_histogram(row):

    possible = [
        "MACDh_12_26_9"
    ]

    for col in possible:
        if col in row.index:
            return row[col]

    return 0


# ============================================================
# GET ADX
# ============================================================

def get_adx_values(row):

    adx = row.get("ADX_14", 0)
    plus = row.get("DMP_14", 0)
    minus = row.get("DMN_14", 0)

    return adx, plus, minus


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

    reasons_bull = []
    reasons_bear = []

    # --------------------------------------------------------
    # EMA TREND
    # --------------------------------------------------------

    if (
        last["close"] > last["ema50"]
        and last["ema50"] > last["ema200"]
    ):
        bullish += 20
        reasons_bull.append(
            "EMA50 > EMA200 + price above EMA50"
        )

    elif (
        last["close"] < last["ema50"]
        and last["ema50"] < last["ema200"]
    ):
        bearish += 20
        reasons_bear.append(
            "EMA50 < EMA200 + price below EMA50"
        )

    # --------------------------------------------------------
    # EMA 20 MOMENTUM
    # --------------------------------------------------------

    if last["close"] > last["ema20"]:
        bullish += 5
        reasons_bull.append("Price above EMA20")

    elif last["close"] < last["ema20"]:
        bearish += 5
        reasons_bear.append("Price below EMA20")

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    rsi = last["rsi"]

    if 50 <= rsi <= 68:
        bullish += 10
        reasons_bull.append(
            f"RSI bullish ({rsi:.1f})"
        )

    elif 32 <= rsi < 50:
        bearish += 5
        reasons_bear.append(
            f"RSI weak ({rsi:.1f})"
        )

    elif rsi > 72:
        bearish += 8
        reasons_bear.append(
            f"RSI overbought ({rsi:.1f})"
        )

    elif rsi < 28:
        bullish += 8
        reasons_bull.append(
            f"RSI oversold ({rsi:.1f})"
        )

    # --------------------------------------------------------
    # MACD
    # --------------------------------------------------------

    macd_hist = get_macd_histogram(last)
    prev_macd_hist = get_macd_histogram(previous)

    if macd_hist > 0:

        bullish += 10

        reasons_bull.append(
            "MACD histogram positive"
        )

        if prev_macd_hist <= 0:
            bullish += 5
            reasons_bull.append(
                "MACD bullish crossover"
            )

    elif macd_hist < 0:

        bearish += 10

        reasons_bear.append(
            "MACD histogram negative"
        )

        if prev_macd_hist >= 0:
            bearish += 5
            reasons_bear.append(
                "MACD bearish crossover"
            )

    # --------------------------------------------------------
    # ADX / TREND STRENGTH
    # --------------------------------------------------------

    adx, plus_di, minus_di = get_adx_values(last)

    if adx >= 20:

        if plus_di > minus_di:

            bullish += 10

            reasons_bull.append(
                f"ADX trend bullish ({adx:.1f})"
            )

        elif minus_di > plus_di:

            bearish += 10

            reasons_bear.append(
                f"ADX trend bearish ({adx:.1f})"
            )

    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    if last["volume"] > last["volume_ma"] * 1.5:

        if last["close"] > last["open"]:

            bullish += 5

            reasons_bull.append(
                "High bullish volume"
            )

        elif last["close"] < last["open"]:

            bearish += 5

            reasons_bear.append(
                "High bearish volume"
            )

    # --------------------------------------------------------
    # PRICE STRUCTURE
    # --------------------------------------------------------

    previous_high = df["high"].iloc[-21:-1].max()
    previous_low = df["low"].iloc[-21:-1].min()

    if last["close"] > previous_high:

        bullish += 10

        reasons_bull.append(
            "Breakout above recent high"
        )

    elif last["close"] < previous_low:

        bearish += 10

        reasons_bear.append(
            "Breakdown below recent low"
        )

    # --------------------------------------------------------
    # DIRECTION
    # --------------------------------------------------------

    if bullish > bearish:
        direction = "BULLISH"

    elif bearish > bullish:
        direction = "BEARISH"

    else:
        direction = "NEUTRAL"

    return {
        "timeframe": timeframe,
        "bullish": bullish,
        "bearish": bearish,
        "direction": direction,
        "bull_reasons": reasons_bull,
        "bear_reasons": reasons_bear,
        "price": float(last["close"]),
        "atr": float(last["atr"]),
        "rsi": float(last["rsi"]),
        "adx": float(adx),
        "volume": float(last["volume"]),
        "volume_ma": float(last["volume_ma"])
    }


# ============================================================
# BTC MARKET TREND
# ============================================================

def get_btc_market_analysis(exchange):

    results = {}

    for tf in TIMEFRAMES:

        try:

            df = get_closed_candles(
                exchange,
                "BTC/USDT",
                tf,
                250
            )

            if df is None:
                continue

            analysis = analyze_timeframe(
                df,
                tf.upper()
            )

            if analysis:
                results[tf] = analysis

        except Exception as e:

            print(
                f"BTC {tf} analysis error:",
                e
            )

    if not results:
        return "NEUTRAL", 0, results

    bullish_count = sum(
        1
        for x in results.values()
        if x["direction"] == "BULLISH"
    )

    bearish_count = sum(
        1
        for x in results.values()
        if x["direction"] == "BEARISH"
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
# BUILD SIGNAL
# ============================================================

def build_signal(
    symbol,
    analyses,
    btc_trend,
    fg_value,
    fg_text
):

    if not all(
        tf in analyses
        for tf in ["1d", "4h", "1h"]
    ):
        return None

    daily = analyses["1d"]
    four_h = analyses["4h"]
    one_h = analyses["1h"]

    # ========================================================
    # LONG
    # ========================================================

    long_score = 0
    long_reasons = []

    # 1D = 30 points
    if daily["direction"] == "BULLISH":

        long_score += 30

        long_reasons.append(
            "1D bullish trend confirmed"
        )

    # 4H = 30 points
    if four_h["direction"] == "BULLISH":

        long_score += 30

        long_reasons.append(
            "4H bullish structure confirmed"
        )

    # 1H = 25 points
    if one_h["direction"] == "BULLISH":

        long_score += 25

        long_reasons.append(
            "1H entry confirmation bullish"
        )

    # BTC market = 10
    if btc_trend == "BULLISH":

        long_score += 10

        long_reasons.append(
            "BTC market trend bullish"
        )

    # Fear & Greed = 5
    if fg_value is not None:

        if fg_value < 75:

            long_score += 5

            long_reasons.append(
                f"Fear & Greed acceptable ({fg_value})"
            )

    # Add technical evidence from 1H
    long_reasons.extend(
        one_h["bull_reasons"][:4]
    )

    # ========================================================
    # SHORT
    # ========================================================

    short_score = 0
    short_reasons = []

    if daily["direction"] == "BEARISH":

        short_score += 30

        short_reasons.append(
            "1D bearish trend confirmed"
        )

    if four_h["direction"] == "BEARISH":

        short_score += 30

        short_reasons.append(
            "4H bearish structure confirmed"
        )

    if one_h["direction"] == "BEARISH":

        short_score += 25

        short_reasons.append(
            "1H entry confirmation bearish"
        )

    if btc_trend == "BEARISH":

        short_score += 10

        short_reasons.append(
            "BTC market trend bearish"
        )

    if fg_value is not None:

        if fg_value > 25:

            short_score += 5

            short_reasons.append(
                f"Fear & Greed acceptable ({fg_value})"
            )

    short_reasons.extend(
        one_h["bear_reasons"][:4]
    )

    # ========================================================
    # REQUIRE ALL TIMEFRAMES TO AGREE
    # ========================================================

    all_bullish = (
        daily["direction"] == "BULLISH"
        and four_h["direction"] == "BULLISH"
        and one_h["direction"] == "BULLISH"
    )

    all_bearish = (
        daily["direction"] == "BEARISH"
        and four_h["direction"] == "BEARISH"
        and one_h["direction"] == "BEARISH"
    )

    # ========================================================
    # LONG SIGNAL
    # ========================================================

    if all_bullish and long_score >= MIN_SCORE:

        price = one_h["price"]
        atr = one_h["atr"]

        # ATR based stop
        stop_loss = price - (atr * 1.5)

        risk = price - stop_loss

        tp1 = price + risk * 1.5
        tp2 = price + risk * 2.5
        tp3 = price + risk * 3.5

        return {
            "signal": "🟢 HIGH-CONFLUENCE LONG",
            "side": "LONG",
            "symbol": symbol,
            "score": long_score,
            "price": price,
            "stop_loss": stop_loss,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "risk_reward": 3.5,
            "reasons": list(dict.fromkeys(long_reasons)),
            "daily": daily["direction"],
            "four_h": four_h["direction"],
            "one_h": one_h["direction"],
            "btc": btc_trend,
            "fg": fg_text
        }

    # ========================================================
    # SHORT SIGNAL
    # ========================================================

    if all_bearish and short_score >= MIN_SCORE:

        price = one_h["price"]
        atr = one_h["atr"]

        stop_loss = price + (atr * 1.5)

        risk = stop_loss - price

        tp1 = price - risk * 1.5
        tp2 = price - risk * 2.5
        tp3 = price - risk * 3.5

        return {
            "signal": "🔴 HIGH-CONFLUENCE SHORT",
            "side": "SHORT",
            "symbol": symbol,
            "score": short_score,
            "price": price,
            "stop_loss": stop_loss,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "risk_reward": 3.5,
            "reasons": list(dict.fromkeys(short_reasons)),
            "daily": daily["direction"],
            "four_h": four_h["direction"],
            "one_h": one_h["direction"],
            "btc": btc_trend,
            "fg": fg_text
        }

    return None


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
# TELEGRAM SIGNAL
# ============================================================

def format_signal(signal):

    reasons = "\n".join(
        f"• {r}"
        for r in signal["reasons"][:10]
    )

    return f"""
<b>{signal["signal"]}</b>

━━━━━━━━━━━━━━━━━━

🪙 <b>Coin:</b> {signal["symbol"]}

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

📈 <b>1D:</b> {signal["daily"]}
📊 <b>4H:</b> {signal["four_h"]}
⏰ <b>1H:</b> {signal["one_h"]}

₿ <b>BTC Market:</b>
{signal["btc"]}

😱 <b>Fear & Greed:</b>
{signal["fg"]}

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
# TOP COINS
# ============================================================

def get_top_coins(exchange):

    try:

        tickers = exchange.fetch_tickers()

        usdt_pairs = []

        for symbol, data in tickers.items():

            if not symbol.endswith("/USDT"):
                continue

            quote_volume = data.get("quoteVolume")

            if not quote_volume:
                continue

            # Ignore leveraged / weird tokens
            if any(
                x in symbol
                for x in [
                    "UP/",
                    "DOWN/",
                    "3L/",
                    "3S/",
                    "5L/",
                    "5S/"
                ]
            ):
                continue

            usdt_pairs.append(
                {
                    "symbol": symbol,
                    "volume": float(quote_volume)
                }
            )

        usdt_pairs.sort(
            key=lambda x: x["volume"],
            reverse=True
        )

        coins = [
            x["symbol"]
            for x in usdt_pairs[:TOP_N]
        ]

        # Make sure BTC and ETH are included
        for major in ["BTC/USDT", "ETH/USDT"]:

            if major not in coins:
                coins.append(major)

        return coins[:TOP_N]

    except Exception as e:

        print(
            "Top coins error:",
            e
        )

        return [
            "BTC/USDT",
            "ETH/USDT",
            "SOL/USDT",
            "XRP/USDT",
            "DOGE/USDT"
        ]


# ============================================================
# MAIN SCANNER
# ============================================================

def scan_market(exchange):

    global sent_signals

    # --------------------------------------------------------
    # BTC MARKET
    # --------------------------------------------------------

    btc_trend, btc_score, btc_analysis = (
        get_btc_market_analysis(exchange)
    )

    # --------------------------------------------------------
    # FEAR & GREED
    # --------------------------------------------------------

    fg_value, fg_text = get_fear_greed()

    # --------------------------------------------------------
    # COINS
    # --------------------------------------------------------

    coins = get_top_coins(exchange)

    print(
        f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"
    )

    print(
        f"BTC Market: {btc_trend}"
    )

    print(
        f"Fear & Greed: {fg_text}"
    )

    print(
        f"Scanning {len(coins)} coins..."
    )

    # --------------------------------------------------------
    # EACH COIN
    # --------------------------------------------------------

    for symbol in coins:

        try:

            analyses = {}

            # ----------------------------------------------
            # GET 1D / 4H / 1H
            # ----------------------------------------------

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

            # ----------------------------------------------
            # BUILD SIGNAL
            # ----------------------------------------------

            signal = build_signal(
                symbol,
                analyses,
                btc_trend,
                fg_value,
                fg_text
            )

            if not signal:
                continue

            # ----------------------------------------------
            # SIGNAL ID
            # ----------------------------------------------

            candle_time = analyses["1h"]["price"]

            signal_id = (
                f"{symbol}_"
                f"{signal['side']}_"
                f"{candle_time}"
            )

            # Don't send same signal repeatedly
            if signal_id in sent_signals:
                continue

            # ----------------------------------------------
            # SEND
            # ----------------------------------------------

            message = format_signal(signal)

            send_telegram(message)

            sent_signals[signal_id] = time.time()

            print(
                f"🚨 SIGNAL: "
                f"{symbol} "
                f"{signal['side']} "
                f"{signal['score']}/100"
            )

            time.sleep(1.5)

        except Exception as e:

            print(
                f"Error analyzing {symbol}:",
                e
            )

            continue

    # Keep memory small
    if len(sent_signals) > 500:

        sent_signals = dict(
            list(sent_signals.items())[-250:]
        )


# ============================================================
# RUN BOT
# ============================================================

def run_bot():

    exchange = ccxt.okx(
        {
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot"
            }
        }
    )

    print(
        "================================================"
    )

    print(
        "Crypto High-Confluence Signal Bot Started"
    )

    print(
        "Exchange: OKX"
    )

    print(
        "Timeframes: 1D + 4H + 1H"
    )

    print(
        "Minimum Score:",
        MIN_SCORE
    )

    print(
        "================================================"
    )

    send_telegram(
        """
✅ <b>Crypto Signal Bot Started</b>

Exchange: OKX

Analysis:
• 1D Trend
• 4H Structure
• 1H Confirmation
• BTC Market
• RSI
• MACD
• EMA 20/50/200
• ADX
• Volume
• ATR
• Fear & Greed

🎯 Minimum Confluence: 80/100

Bot will only send high-confluence setups.
"""
    )

    while True:

        try:

            scan_market(exchange)

            print(
                f"\nScan complete."
            )

            print(
                f"Next scan in "
                f"{SCAN_INTERVAL // 60} minutes."
            )

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:

            print(
                "Bot stopped."
            )

            break

        except Exception as e:

            print(
                "MAIN ERROR:",
                e
            )

            time.sleep(60)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    run_bot()
