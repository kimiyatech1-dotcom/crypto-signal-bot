import ccxt
import pandas as pd
import requests
import time
import os
import json
import math
import html
from datetime import datetime, timezone, timedelta


# ============================================================
# V3 CRYPTO MARKET INTELLIGENCE SIGNAL BOT
# ============================================================
#
# IMPORTANT:
# - This bot generates signals only.
# - It does NOT place trades.
# - LONG and SHORT are evaluated independently.
# - 1H reversal has strong authority.
# - BTC regime is separate from individual coin direction.
# - Early Momentum is separate from Confirmed setups.
# - News/fundamental API is NOT faked. It is marked unavailable
#   unless a real source is connected.
#
# ============================================================


# -----------------------------
# TELEGRAM
# -----------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")


# -----------------------------
# SCANNER SETTINGS
# -----------------------------

SCAN_INTERVAL = 900                 # 15 minutes

# Wider universe than old V2.
# We first inspect the exchange universe and then technically
# analyze the most liquid/active coins.
MAX_UNIVERSE = 100

MAX_CONFIRMED_PER_SCAN = 2
MAX_EARLY_PER_SCAN = 1

MAX_CONFIRMED_PER_DAY = 5
MAX_EARLY_PER_DAY = 3

MIN_CONFIRMED_SCORE = 72
MIN_EARLY_SCORE = 64

MIN_ADX = 18
MIN_VOLUME_RATIO = 1.05

TIMEFRAMES = ["1d", "4h", "1h"]

STABLE_BASES = {
    "USDT",
    "USDC",
    "FDUSD",
    "DAI",
    "TUSD",
    "USDP",
    "PYUSD",
    "BUSD",
    "USDE",
    "USD1",
}

# Avoid obvious leveraged tokens.
LEVERAGED_WORDS = (
    "3L",
    "3S",
    "5L",
    "5S",
    "2L",
    "2S",
    "BULL",
    "BEAR",
)

JOURNAL_FILE = "signal_journal.json"

COOLDOWN_HOURS_AFTER_SL = 12
REENTRY_CONFIRMATION_HOURS = 2

# Do not chase an already vertical move.
MAX_1H_EXTENSION_FOR_NORMAL_ENTRY = 8.0
MAX_4H_EXTENSION_FOR_NORMAL_ENTRY = 18.0

# Early radar thresholds.
EARLY_VOLUME_RATIO = 1.60
EARLY_1H_MOVE = 2.5
EARLY_4H_MOVE = 5.0

# Derivatives are fetched only for stronger candidates so that
# the scanner does not hammer public endpoints.
DERIVATIVE_CANDIDATES = 20

OKX_BASE_URL = "https://www.okx.com"


# -----------------------------
# GLOBAL STATE
# -----------------------------

active_signals = {}
cooldowns = {}

daily_confirmed_sent = 0
daily_early_sent = 0
daily_date = None

journal = []


# ============================================================
# BASIC HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def clamp(value, low, high):
    return max(low, min(high, value))


def pct_change(a, b):
    a = safe_float(a)
    b = safe_float(b)

    if a == 0:
        return 0.0

    return ((b - a) / a) * 100.0


def format_price(price):
    price = safe_float(price)

    if price >= 1000:
        return f"{price:,.2f}"

    if price >= 100:
        return f"{price:,.3f}"

    if price >= 1:
        return f"{price:,.4f}"

    if price >= 0.01:
        return f"{price:.6f}"

    return f"{price:.8f}"


def format_pct(value):
    return f"{safe_float(value):+.2f}%"


def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram credentials missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(
            url,
            data=payload,
            timeout=15,
        )

        if not response.ok:
            print("Telegram error:", response.text)
            return False

        return True

    except Exception as e:
        print("Telegram exception:", e)
        return False


# ============================================================
# FEAR & GREED
# ============================================================

def get_fear_greed():
    try:
        response = requests.get(
            "https://api.alternative.me/fng/",
            timeout=10,
        )

        data = response.json()["data"][0]

        return (
            int(data["value"]),
            data["value_classification"],
        )

    except Exception as e:
        print("Fear & Greed error:", e)
        return None, "Unknown"


# ============================================================
# JOURNAL
# ============================================================

def load_journal():
    global journal

    try:
        if not os.path.exists(JOURNAL_FILE):
            journal = []
            return

        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            journal = data
        else:
            journal = []

    except Exception as e:
        print("Journal load error:", e)
        journal = []


def save_journal():
    try:
        # Keep file from growing forever.
        recent = journal[-1000:]

        with open(JOURNAL_FILE, "w", encoding="utf-8") as f:
            json.dump(
                recent,
                f,
                indent=2,
                ensure_ascii=False,
            )

    except Exception as e:
        print("Journal save error:", e)


def journal_event(event):
    event = dict(event)
    event["logged_at"] = now_utc().isoformat()

    journal.append(event)

    save_journal()


# ============================================================
# DAILY LIMIT
# ============================================================

def reset_daily_counter():
    global daily_date
    global daily_confirmed_sent
    global daily_early_sent

    today = now_utc().date()

    if daily_date != today:
        daily_date = today
        daily_confirmed_sent = 0
        daily_early_sent = 0


# ============================================================
# CANDLE DATA
# ============================================================

def candles_to_df(ohlcv):
    if not ohlcv:
        return None

    df = pd.DataFrame(
        ohlcv,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ],
    )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        unit="ms",
        utc=True,
    )

    for column in [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = df.dropna().reset_index(drop=True)

    return df


def get_closed_candles(
    exchange,
    symbol,
    timeframe,
    limit=250,
):
    try:
        ohlcv = exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit,
        )

        df = candles_to_df(ohlcv)

        if df is None:
            return None

        if len(df) < 210:
            return None

        # Last candle can still be forming.
        return df.iloc[:-1].copy()

    except Exception as e:
        print(
            f"Candle error {symbol} {timeframe}:",
            e,
        )
        return None


# ============================================================
# INDICATORS
# ============================================================

def ema(series, length):
    return series.ewm(
        span=length,
        adjust=False,
        min_periods=length,
    ).mean()


def rsi(series, length=14):
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()

    rs = avg_gain / avg_loss.replace(0, math.nan)

    result = 100 - (100 / (1 + rs))

    return result.fillna(50)


def atr(df, length=14):
    previous_close = df["close"].shift(1)

    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - previous_close).abs()
    tr3 = (df["low"] - previous_close).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1,
    ).max(axis=1)

    return true_range.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()


def macd(series):
    fast = ema(series, 12)
    slow = ema(series, 26)

    line = fast - slow
    signal = ema(line, 9)
    histogram = line - signal

    return line, signal, histogram


def adx(df, length=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        0.0,
        index=df.index,
    )

    minus_dm = pd.Series(
        0.0,
        index=df.index,
    )

    plus_condition = (
        (up_move > down_move)
        & (up_move > 0)
    )

    minus_condition = (
        (down_move > up_move)
        & (down_move > 0)
    )

    plus_dm.loc[plus_condition] = up_move.loc[
        plus_condition
    ]

    minus_dm.loc[minus_condition] = down_move.loc[
        minus_condition
    ]

    previous_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - previous_close).abs()
    tr3 = (low - previous_close).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1,
    ).max(axis=1)

    atr_value = true_range.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()

    plus_smoothed = plus_dm.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()

    minus_smoothed = minus_dm.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()

    plus_di = (
        100
        * plus_smoothed
        / atr_value.replace(0, math.nan)
    )

    minus_di = (
        100
        * minus_smoothed
        / atr_value.replace(0, math.nan)
    )

    denominator = (
        plus_di + minus_di
    ).replace(0, math.nan)

    dx = (
        100
        * (plus_di - minus_di).abs()
        / denominator
    )

    adx_value = dx.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()

    return (
        adx_value.fillna(0),
        plus_di.fillna(0),
        minus_di.fillna(0),
    )


# ============================================================
# TECHNICAL ENGINE
# ============================================================

def add_indicators(df):
    df = df.copy()

    df["ema20"] = ema(
        df["close"],
        20,
    )

    df["ema50"] = ema(
        df["close"],
        50,
    )

    df["ema200"] = ema(
        df["close"],
        200,
    )

    df["rsi"] = rsi(
        df["close"],
        14,
    )

    (
        df["macd_line"],
        df["macd_signal"],
        df["macd_hist"],
    ) = macd(df["close"])

    (
        df["adx"],
        df["plus_di"],
        df["minus_di"],
    ) = adx(df, 14)

    df["atr"] = atr(
        df,
        14,
    )

    df["volume_ma"] = (
        df["volume"]
        .rolling(20)
        .mean()
    )

    df["recent_high"] = (
        df["high"]
        .shift(1)
        .rolling(20)
        .max()
    )

    df["recent_low"] = (
        df["low"]
        .shift(1)
        .rolling(20)
        .min()
    )

    df["range"] = (
        df["high"] - df["low"]
    )

    df["body"] = (
        df["close"] - df["open"]
    )

    df["body_ratio"] = (
        df["body"].abs()
        / df["range"].replace(0, math.nan)
    ).fillna(0)

    df = df.dropna().copy()

    return df


def analyze_timeframe(
    raw_df,
    timeframe,
):
    if raw_df is None:
        return None

    df = add_indicators(raw_df)

    if len(df) < 50:
        return None

    last = df.iloc[-1]
    previous = df.iloc[-2]

    bullish = 0.0
    bearish = 0.0

    bull_reasons = []
    bear_reasons = []

    close = safe_float(last["close"])

    # -----------------------------
    # TREND
    # -----------------------------

    if close > last["ema50"] > last["ema200"]:
        bullish += 18
        bull_reasons.append(
            "Price > EMA50 > EMA200"
        )

    elif close < last["ema50"] < last["ema200"]:
        bearish += 18
        bear_reasons.append(
            "Price < EMA50 < EMA200"
        )

    if last["ema20"] > last["ema50"]:
        bullish += 8
        bull_reasons.append(
            "EMA20 above EMA50"
        )

    elif last["ema20"] < last["ema50"]:
        bearish += 8
        bear_reasons.append(
            "EMA20 below EMA50"
        )

    # -----------------------------
    # RSI
    # -----------------------------

    current_rsi = safe_float(
        last["rsi"]
    )

    if 52 <= current_rsi <= 68:
        bullish += 10
        bull_reasons.append(
            f"Healthy bullish RSI ({current_rsi:.1f})"
        )

    elif 32 <= current_rsi <= 48:
        bearish += 10
        bear_reasons.append(
            f"Healthy bearish RSI ({current_rsi:.1f})"
        )

    elif 68 < current_rsi <= 75:
        bullish += 4
        bull_reasons.append(
            f"Strong RSI ({current_rsi:.1f})"
        )

    elif 25 <= current_rsi < 32:
        bearish += 4
        bear_reasons.append(
            f"Weak RSI ({current_rsi:.1f})"
        )

    elif current_rsi > 78:
        bearish += 5
        bear_reasons.append(
            f"Overextended RSI ({current_rsi:.1f})"
        )

    elif current_rsi < 22:
        bullish += 5
        bull_reasons.append(
            f"Oversold RSI ({current_rsi:.1f})"
        )

    # -----------------------------
    # MACD
    # -----------------------------

    hist = safe_float(
        last["macd_hist"]
    )

    previous_hist = safe_float(
        previous["macd_hist"]
    )

    macd_line = safe_float(
        last["macd_line"]
    )

    macd_signal = safe_float(
        last["macd_signal"]
    )

    if hist > 0 and macd_line > macd_signal:
        bullish += 10
        bull_reasons.append(
            "MACD bullish"
        )

        if previous_hist <= 0:
            bullish += 5
            bull_reasons.append(
                "Fresh MACD bullish crossover"
            )

    elif hist < 0 and macd_line < macd_signal:
        bearish += 10
        bear_reasons.append(
            "MACD bearish"
        )

        if previous_hist >= 0:
            bearish += 5
            bear_reasons.append(
                "Fresh MACD bearish crossover"
            )

    # -----------------------------
    # ADX / DI
    # -----------------------------

    adx_value = safe_float(
        last["adx"]
    )

    plus_di = safe_float(
        last["plus_di"]
    )

    minus_di = safe_float(
        last["minus_di"]
    )

    if adx_value >= MIN_ADX:

        if plus_di > minus_di:
            bullish += 9
            bull_reasons.append(
                f"Directional strength bullish (ADX {adx_value:.1f})"
            )

        elif minus_di > plus_di:
            bearish += 9
            bear_reasons.append(
                f"Directional strength bearish (ADX {adx_value:.1f})"
            )

    # -----------------------------
    # VOLUME
    # -----------------------------

    volume_ratio = 0.0

    if safe_float(last["volume_ma"]) > 0:
        volume_ratio = (
            safe_float(last["volume"])
            / safe_float(last["volume_ma"])
        )

    if volume_ratio >= MIN_VOLUME_RATIO:

        if last["close"] > last["open"]:
            bullish += 7
            bull_reasons.append(
                f"Volume confirmation ({volume_ratio:.2f}x)"
            )

        elif last["close"] < last["open"]:
            bearish += 7
            bear_reasons.append(
                f"Sell-volume confirmation ({volume_ratio:.2f}x)"
            )

    # -----------------------------
    # BREAKOUT / BREAKDOWN
    # -----------------------------

    previous_high = safe_float(
        df["high"].iloc[-21:-1].max()
    )

    previous_low = safe_float(
        df["low"].iloc[-21:-1].min()
    )

    breakout = close > previous_high
    breakdown = close < previous_low

    if breakout:
        bullish += 12
        bull_reasons.append(
            "Breakout above recent structure"
        )

    if breakdown:
        bearish += 12
        bear_reasons.append(
            "Breakdown below recent structure"
        )

    # -----------------------------
    # SHORT-TERM STRUCTURE
    # -----------------------------

    higher_high = (
        last["high"]
        > df["high"].iloc[-3]
    )

    higher_low = (
        last["low"]
        > df["low"].iloc[-3]
    )

    lower_high = (
        last["high"]
        < df["high"].iloc[-3]
    )

    lower_low = (
        last["low"]
        < df["low"].iloc[-3]
    )

    if higher_high and higher_low:
        bullish += 6
        bull_reasons.append(
            "Higher-high / higher-low structure"
        )

    if lower_high and lower_low:
        bearish += 6
        bear_reasons.append(
            "Lower-high / lower-low structure"
        )

    # -----------------------------
    # CANDLE QUALITY
    # -----------------------------

    body_ratio = safe_float(
        last["body_ratio"]
    )

    if body_ratio >= 0.55:

        if last["close"] > last["open"]:
            bullish += 4
            bull_reasons.append(
                "Strong bullish candle body"
            )

        elif last["close"] < last["open"]:
            bearish += 4
            bear_reasons.append(
                "Strong bearish candle body"
            )

    # -----------------------------
    # DIRECTION
    # -----------------------------

    if bullish >= bearish + 8:
        direction = "BULLISH"

    elif bearish >= bullish + 8:
        direction = "BEARISH"

    else:
        direction = "MIXED"

    # -----------------------------
    # REVERSAL FLAGS
    # -----------------------------

    bullish_reversal_risk = False
    bearish_reversal_risk = False

    if (
        close < last["ema20"]
        and last["ema20"] < last["ema50"]
        and hist < 0
        and current_rsi < 48
    ):
        bullish_reversal_risk = True

    if (
        close > last["ema20"]
        and last["ema20"] > last["ema50"]
        and hist > 0
        and current_rsi > 52
    ):
        bearish_reversal_risk = True

    return {
        "timeframe": timeframe,

        "bullish": round(
            clamp(bullish, 0, 100),
            2,
        ),

        "bearish": round(
            clamp(bearish, 0, 100),
            2,
        ),

        "direction": direction,

        "bull_reasons": bull_reasons,
        "bear_reasons": bear_reasons,

        "price": close,

        "atr": safe_float(
            last["atr"]
        ),

        "rsi": current_rsi,

        "adx": adx_value,

        "plus_di": plus_di,

        "minus_di": minus_di,

        "volume_ratio": volume_ratio,

        "macd_line": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": hist,

        "ema20": safe_float(
            last["ema20"]
        ),

        "ema50": safe_float(
            last["ema50"]
        ),

        "ema200": safe_float(
            last["ema200"]
        ),

        "recent_high": previous_high,
        "recent_low": previous_low,

        "breakout": breakout,
        "breakdown": breakdown,

        "higher_high": higher_high,
        "higher_low": higher_low,

        "lower_high": lower_high,
        "lower_low": lower_low,

        "body_ratio": body_ratio,

        "bullish_reversal_risk":
            bullish_reversal_risk,

        "bearish_reversal_risk":
            bearish_reversal_risk,

        "timestamp": last["timestamp"],
    }


# ============================================================
# BTC MARKET REGIME
# ============================================================

def get_btc_market_analysis(exchange):
    analyses = {}

    for tf in TIMEFRAMES:

        try:
            df = get_closed_candles(
                exchange,
                "BTC/USDT",
                tf,
                250,
            )

            if df is None:
                continue

            result = analyze_timeframe(
                df,
                tf,
            )

            if result:
                analyses[tf] = result

        except Exception as e:
            print(
                f"BTC {tf} analysis error:",
                e,
            )

    if not analyses:
        return {
            "regime": "UNKNOWN",
            "score": 0,
            "analyses": {},
        }

    daily = analyses.get("1d")
    four_h = analyses.get("4h")
    one_h = analyses.get("1h")

    long_score = 0.0
    short_score = 0.0

    if daily:
        long_score += daily["bullish"] * 0.25
        short_score += daily["bearish"] * 0.25

    if four_h:
        long_score += four_h["bullish"] * 0.35
        short_score += four_h["bearish"] * 0.35

    if one_h:
        long_score += one_h["bullish"] * 0.40
        short_score += one_h["bearish"] * 0.40

    # Strong 1H reversal can override a bullish higher timeframe.
    if one_h:

        if (
            one_h["bearish"] >= 65
            and one_h["bullish"] + 15
            < one_h["bearish"]
        ):
            short_score += 10

        if (
            one_h["bullish"] >= 65
            and one_h["bearish"] + 15
            < one_h["bullish"]
        ):
            long_score += 10

    difference = long_score - short_score

    if long_score >= 65 and difference >= 15:
        regime = "BULLISH"

    elif short_score >= 65 and difference <= -15:
        regime = "BEARISH"

    elif (
        abs(difference) <= 12
        or (
            one_h
            and (
                one_h["direction"] == "MIXED"
            )
        )
    ):
        regime = "TRANSITION"

    else:
        regime = "NEUTRAL"

    return {
        "regime": regime,
        "long_score": round(
            long_score,
            2,
        ),
        "short_score": round(
            short_score,
            2,
        ),
        "score": round(
            difference,
            2,
        ),
        "analyses": analyses,
    }


# ============================================================
# OKX DERIVATIVES INTELLIGENCE
# ============================================================

def okx_get(path, params):
    try:
        response = requests.get(
            f"{OKX_BASE_URL}{path}",
            params=params,
            timeout=10,
        )

        if not response.ok:
            return None

        data = response.json()

        if data.get("code") != "0":
            return None

        return data.get("data")

    except Exception as e:
        print(
            "OKX public API error:",
            path,
            e,
        )
        return None


def get_swap_derivatives(base):
    """
    Returns:
        funding_rate
        open_interest
        swap_price
    """

    result = {
        "available": False,
        "funding_rate": None,
        "open_interest": None,
        "swap_price": None,
    }

    inst_id = f"{base}-USDT-SWAP"

    # Funding
    funding_data = okx_get(
        "/api/v5/public/funding-rate",
        {
            "instId": inst_id,
        },
    )

    if funding_data:

        funding_rate = safe_float(
            funding_data[0].get(
                "fundingRate"
            ),
            0,
        )

        result["funding_rate"] = funding_rate

    # Open interest
    oi_data = okx_get(
        "/api/v5/public/open-interest",
        {
            "instType": "SWAP",
            "instId": inst_id,
        },
    )

    if oi_data:

        oi = safe_float(
            oi_data[0].get("oi"),
            0,
        )

        result["open_interest"] = oi

    # Swap ticker
    ticker_data = okx_get(
        "/api/v5/market/ticker",
        {
            "instId": inst_id,
        },
    )

    if ticker_data:

        result["swap_price"] = safe_float(
            ticker_data[0].get("last"),
            0,
        )

    if any(
        value is not None
        for value in [
            result["funding_rate"],
            result["open_interest"],
            result["swap_price"],
        ]
    ):
        result["available"] = True

    return result


def evaluate_derivatives(
    derivatives,
    side,
):
    """
    Funding is used as a warning/confirmation signal,
    not as a standalone trade trigger.
    """

    if not derivatives:
        return 0, []

    if not derivatives.get("available"):
        return 0, []

    funding = derivatives.get(
        "funding_rate"
    )

    score = 0
    reasons = []

    if funding is None:
        return 0, []

    funding_pct = funding * 100

    if side == "LONG":

        # Positive funding means longs are paying.
        # Extremely positive funding can mean crowded longs.
        if 0 <= funding_pct <= 0.03:
            score += 3
            reasons.append(
                f"Funding neutral/healthy ({funding_pct:.3f}%)"
            )

        elif funding_pct > 0.08:
            score -= 6
            reasons.append(
                f"Crowded long funding ({funding_pct:.3f}%)"
            )

        elif funding_pct < -0.03:
            score += 4
            reasons.append(
                f"Negative funding supports squeeze risk ({funding_pct:.3f}%)"
            )

    elif side == "SHORT":

        if -0.03 <= funding_pct <= 0:
            score += 3
            reasons.append(
                f"Funding neutral/healthy ({funding_pct:.3f}%)"
            )

        elif funding_pct < -0.08:
            score -= 6
            reasons.append(
                f"Crowded short funding ({funding_pct:.3f}%)"
            )

        elif funding_pct > 0.03:
            score += 4
            reasons.append(
                f"Positive funding supports short squeeze/reversal risk ({funding_pct:.3f}%)"
            )

    return score, reasons


# ============================================================
# RELATIVE STRENGTH
# ============================================================

def get_relative_strength(
    coin_df,
    btc_df,
):
    try:
        if len(coin_df) < 25:
            return 0, "Unavailable"

        if len(btc_df) < 25:
            return 0, "Unavailable"

        coin_return = pct_change(
            coin_df["close"].iloc[-6],
            coin_df["close"].iloc[-1],
        )

        btc_return = pct_change(
            btc_df["close"].iloc[-6],
            btc_df["close"].iloc[-1],
        )

        relative = coin_return - btc_return

        if relative >= 4:
            return 6, f"Strong relative strength vs BTC ({relative:+.2f}%)"

        if relative >= 2:
            return 4, f"Relative strength vs BTC ({relative:+.2f}%)"

        if relative <= -4:
            return -6, f"Weak relative strength vs BTC ({relative:+.2f}%)"

        if relative <= -2:
            return -4, f"Weak relative strength vs BTC ({relative:+.2f}%)"

        return 0, f"Neutral relative strength ({relative:+.2f}%)"

    except Exception:
        return 0, "Relative strength unavailable"


# ============================================================
# MARKET BREADTH
# ============================================================

def calculate_market_breadth(
    exchange,
    symbols,
    max_check=30,
):
    bullish = 0
    bearish = 0
    checked = 0

    for symbol in symbols[:max_check]:

        try:
            df = get_closed_candles(
                exchange,
                symbol,
                "1h",
                80,
            )

            if df is None:
                continue

            if len(df) < 30:
                continue

            close = safe_float(
                df["close"].iloc[-1]
            )

            ema20_value = safe_float(
                ema(
                    df["close"],
                    20,
                ).iloc[-1]
            )

            if close > ema20_value:
                bullish += 1
            else:
                bearish += 1

            checked += 1

        except Exception:
            continue

    if checked == 0:
        return {
            "bullish": 0,
            "bearish": 0,
            "breadth": 0,
            "status": "UNKNOWN",
        }

    breadth = (
        (bullish - bearish)
        / checked
        * 100
    )

    if breadth >= 25:
        status = "POSITIVE"

    elif breadth <= -25:
        status = "NEGATIVE"

    else:
        status = "MIXED"

    return {
        "bullish": bullish,
        "bearish": bearish,
        "breadth": round(
            breadth,
            2,
        ),
        "status": status,
    }


# ============================================================
# COIN UNIVERSE
# ============================================================

def get_top_coins(exchange):
    """
    Gets a broad USDT spot universe.

    We do NOT silently discard everything outside the old TOP 30.
    We rank the exchange universe by 24h quote volume and scan
    a larger set.
    """

    try:
        markets = exchange.load_markets()

        tickers = exchange.fetch_tickers()

        candidates = []

        available_count = 0

        for symbol, market in markets.items():

            if not market.get("spot"):
                continue

            if not market.get("active", True):
                continue

            if not symbol.endswith("/USDT"):
                continue

            base = market.get(
                "base",
                "",
            )

            if not base:
                continue

            if base.upper() in STABLE_BASES:
                continue

            upper_base = base.upper()

            if any(
                word in upper_base
                for word in LEVERAGED_WORDS
            ):
                continue

            available_count += 1

            ticker = tickers.get(symbol)

            if not ticker:
                continue

            quote_volume = safe_float(
                ticker.get("quoteVolume")
            )

            last_price = safe_float(
                ticker.get("last")
            )

            if quote_volume <= 0:
                continue

            if last_price <= 0:
                continue

            candidates.append(
                {
                    "symbol": symbol,
                    "base": base,
                    "quote_volume": quote_volume,
                    "last": last_price,
                }
            )

        candidates.sort(
            key=lambda x: x["quote_volume"],
            reverse=True,
        )

        selected = candidates[
            :MAX_UNIVERSE
        ]

        return (
            selected,
            available_count,
        )

    except Exception as e:
        print(
            "Universe error:",
            e,
        )

        return [], 0


# ============================================================
# COOLDOWN
# ============================================================

def signal_key(signal):
    return (
        f"{signal['symbol']}|"
        f"{signal['side']}|"
        f"{signal['candle_time']}"
    )


def has_active_same_coin(symbol):
    return symbol in active_signals


def is_on_cooldown(
    symbol,
    side,
):
    key = f"{symbol}|{side}"

    data = cooldowns.get(key)

    if not data:
        return False

    try:
        until = datetime.fromisoformat(
            data["until"]
        )

        if now_utc() < until:
            return True

        del cooldowns[key]

        return False

    except Exception:
        return False


def set_cooldown(
    symbol,
    side,
    reason,
    hours=COOLDOWN_HOURS_AFTER_SL,
):
    key = f"{symbol}|{side}"

    until = (
        now_utc()
        + timedelta(hours=hours)
    )

    cooldowns[key] = {
        "until": until.isoformat(),
        "reason": reason,
    }


# ============================================================
# ACTIVE SIGNAL MANAGEMENT
# ============================================================

def remember_signal(signal):
    active_signals[
        signal["symbol"]
    ] = {
        "symbol": signal["symbol"],
        "side": signal["side"],
        "entry": signal["entry"],
        "sl": signal["sl"],
        "tp1": signal["tp1"],
        "tp2": signal["tp2"],
        "tp3": signal["tp3"],
        "created_at": signal["created_at"],
        "candle_time": signal["candle_time"],
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "sl_hit": False,
        "result": "ACTIVE",
        "max_favorable": 0.0,
        "max_adverse": 0.0,
    }

    journal_event(
        {
            "event": "SIGNAL_CREATED",
            "symbol": signal["symbol"],
            "side": signal["side"],
            "entry": signal["entry"],
            "sl": signal["sl"],
            "tp1": signal["tp1"],
            "tp2": signal["tp2"],
            "tp3": signal["tp3"],
            "score": signal["score"],
            "created_at": signal["created_at"],
        }
    )


def check_signal_with_candles(
    exchange,
    data,
):
    symbol = data["symbol"]
    side = data["side"]

    try:
        df = get_closed_candles(
            exchange,
            symbol,
            "1h",
            100,
        )

        if df is None:
            return

        entry = safe_float(
            data["entry"]
        )

        sl = safe_float(
            data["sl"]
        )

        tp1 = safe_float(
            data["tp1"]
        )

        tp2 = safe_float(
            data["tp2"]
        )

        tp3 = safe_float(
            data["tp3"]
        )

        created_at = datetime.fromisoformat(
            data["created_at"]
        )

        # Only inspect candles after signal creation.
        recent = df[
            df["timestamp"]
            > created_at
        ].copy()

        if recent.empty:
            return

        for _, candle in recent.iterrows():

            high = safe_float(
                candle["high"]
            )

            low = safe_float(
                candle["low"]
            )

            if side == "LONG":

                favorable = (
                    (high - entry)
                    / entry
                    * 100
                )

                adverse = (
                    (entry - low)
                    / entry
                    * 100
                )

                data["max_favorable"] = max(
                    data["max_favorable"],
                    favorable,
                )

                data["max_adverse"] = max(
                    data["max_adverse"],
                    adverse,
                )

                # Conservative rule:
                # if SL and TP occur in same candle,
                # treat SL as first because intrabar ordering
                # is unknown.
                if low <= sl:
                    data["sl_hit"] = True
                    data["result"] = "SL"

                    journal_event(
                        {
                            "event": "SL_HIT",
                            "symbol": symbol,
                            "side": side,
                            "price": sl,
                        }
                    )

                    set_cooldown(
                        symbol,
                        side,
                        "SL hit",
                    )

                    del active_signals[
                        symbol
                    ]

                    return

                if high >= tp1:
                    data["tp1_hit"] = True

                if high >= tp2:
                    data["tp2_hit"] = True

                if high >= tp3:
                    data["tp3_hit"] = True
                    data["result"] = "TP3"

                    journal_event(
                        {
                            "event": "TP3_HIT",
                            "symbol": symbol,
                            "side": side,
                            "price": tp3,
                        }
                    )

                    del active_signals[
                        symbol
                    ]

                    return

            else:

                favorable = (
                    (entry - low)
                    / entry
                    * 100
                )

                adverse = (
                    (high - entry)
                    / entry
                    * 100
                )

                data["max_favorable"] = max(
                    data["max_favorable"],
                    favorable,
                )

                data["max_adverse"] = max(
                    data["max_adverse"],
                    adverse,
                )

                if high >= sl:
                    data["sl_hit"] = True
                    data["result"] = "SL"

                    journal_event(
                        {
                            "event": "SL_HIT",
                            "symbol": symbol,
                            "side": side,
                            "price": sl,
                        }
                    )

                    set_cooldown(
                        symbol,
                        side,
                        "SL hit",
                    )

                    del active_signals[
                        symbol
                    ]

                    return

                if low <= tp1:
                    data["tp1_hit"] = True

                if low <= tp2:
                    data["tp2_hit"] = True

                if low <= tp3:
                    data["tp3_hit"] = True
                    data["result"] = "TP3"

                    journal_event(
                        {
                            "event": "TP3_HIT",
                            "symbol": symbol,
                            "side": side,
                            "price": tp3,
                        }
                    )

                    del active_signals[
                        symbol
                    ]

                    return

        # Timeout after 24h.
        age = now_utc() - created_at

        if age.total_seconds() >= 24 * 3600:

            data["result"] = "TIMEOUT"

            journal_event(
                {
                    "event": "TIMEOUT",
                    "symbol": symbol,
                    "side": side,
                    "tp1_hit": data["tp1_hit"],
                    "tp2_hit": data["tp2_hit"],
                    "tp3_hit": data["tp3_hit"],
                    "max_favorable": data[
                        "max_favorable"
                    ],
                    "max_adverse": data[
                        "max_adverse"
                    ],
                }
            )

            del active_signals[
                symbol
            ]

    except Exception as e:
        print(
            f"Signal tracking error {symbol}:",
            e,
        )


def cleanup_active_signals(exchange):
    for symbol in list(
        active_signals.keys()
    ):

        data = active_signals.get(
            symbol
        )

        if not data:
            continue

        check_signal_with_candles(
            exchange,
            data,
        )


# ============================================================
# 1H REVERSAL / INVALIDATION
# ============================================================

def long_hard_invalidated(
    one_h,
):
    if not one_h:
        return True

    # Strong bearish 1H reversal.
    if (
        one_h["bearish"] >= 68
        and one_h["bearish"]
        >= one_h["bullish"] + 15
    ):
        return True

    # Price loses EMA20 + EMA50 with bearish momentum.
    if (
        one_h["price"] < one_h["ema20"]
        and one_h["price"] < one_h["ema50"]
        and one_h["macd_hist"] < 0
        and one_h["rsi"] < 47
    ):
        return True

    # Structure breakdown.
    if (
        one_h["breakdown"]
        and one_h["macd_hist"] < 0
    ):
        return True

    return False


def short_hard_invalidated(
    one_h,
):
    if not one_h:
        return True

    if (
        one_h["bullish"] >= 68
        and one_h["bullish"]
        >= one_h["bearish"] + 15
    ):
        return True

    if (
        one_h["price"] > one_h["ema20"]
        and one_h["price"] > one_h["ema50"]
        and one_h["macd_hist"] > 0
        and one_h["rsi"] > 53
    ):
        return True

    if (
        one_h["breakout"]
        and one_h["macd_hist"] > 0
    ):
        return True

    return False


# ============================================================
# SCORE ENGINE
# ============================================================

def calculate_side_score(
    analyses,
    side,
    btc_market,
    breadth,
    derivatives=None,
    relative_score=0,
    relative_reason="",
):
    daily = analyses.get("1d")
    four_h = analyses.get("4h")
    one_h = analyses.get("1h")

    if not daily or not four_h or not one_h:
        return {
            "score": 0,
            "reasons": [
                "Incomplete timeframe data"
            ],
            "veto": True,
        }

    if side == "LONG":

        base = (
            daily["bullish"] * 0.25
            + four_h["bullish"] * 0.35
            + one_h["bullish"] * 0.40
        )

        reasons = []

        if daily["bullish"] >= 55:
            reasons.append(
                "1D bullish context"
            )

        if four_h["bullish"] >= 55:
            reasons.append(
                "4H bullish structure"
            )

        if one_h["bullish"] >= 55:
            reasons.append(
                "1H bullish momentum"
            )

        modifier = 0

        # BTC regime.
        regime = btc_market["regime"]

        if regime == "BULLISH":
            modifier += 7
            reasons.append(
                "BTC regime aligned bullish"
            )

        elif regime == "BEARISH":
            modifier -= 12
            reasons.append(
                "BTC regime bearish"
            )

        elif regime == "TRANSITION":
            modifier -= 2
            reasons.append(
                "BTC regime transition"
            )

        # Breadth.
        if breadth["status"] == "POSITIVE":
            modifier += 4
            reasons.append(
                "Market breadth positive"
            )

        elif breadth["status"] == "NEGATIVE":
            modifier -= 5
            reasons.append(
                "Market breadth negative"
            )

        # Relative strength.
        modifier += relative_score

        if relative_reason:
            reasons.append(
                relative_reason
            )

        # Derivatives.
        deriv_score, deriv_reasons = (
            evaluate_derivatives(
                derivatives,
                "LONG",
            )
        )

        modifier += deriv_score
        reasons.extend(
            deriv_reasons
        )

        score = clamp(
            base + modifier,
            0,
            100,
        )

        veto = long_hard_invalidated(
            one_h
        )

        if veto:
            reasons.append(
                "1H bearish reversal veto"
            )

        return {
            "score": round(score, 2),
            "base": round(base, 2),
            "modifier": round(
                modifier,
                2,
            ),
            "reasons": reasons,
            "veto": veto,
        }

    # SHORT
    base = (
        daily["bearish"] * 0.25
        + four_h["bearish"] * 0.35
        + one_h["bearish"] * 0.40
    )

    reasons = []

    if daily["bearish"] >= 55:
        reasons.append(
            "1D bearish context"
        )

    if four_h["bearish"] >= 55:
        reasons.append(
            "4H bearish structure"
        )

    if one_h["bearish"] >= 55:
        reasons.append(
            "1H bearish momentum"
        )

    modifier = 0

    regime = btc_market["regime"]

    if regime == "BEARISH":
        modifier += 7
        reasons.append(
            "BTC regime aligned bearish"
        )

    elif regime == "BULLISH":
        modifier -= 12
        reasons.append(
            "BTC regime bullish"
        )

    elif regime == "TRANSITION":
        modifier -= 2
        reasons.append(
            "BTC regime transition"
        )

    if breadth["status"] == "NEGATIVE":
        modifier += 4
        reasons.append(
            "Market breadth negative"
        )

    elif breadth["status"] == "POSITIVE":
        modifier -= 5
        reasons.append(
            "Market breadth positive"
        )

    # For SHORT, weak relative strength is positive.
    modifier -= relative_score

    if relative_reason:
        reasons.append(
            relative_reason
        )

    deriv_score, deriv_reasons = (
        evaluate_derivatives(
            derivatives,
            "SHORT",
        )
    )

    modifier += deriv_score
    reasons.extend(
        deriv_reasons
    )

    score = clamp(
        base + modifier,
        0,
        100,
    )

    veto = short_hard_invalidated(
        one_h
    )

    if veto:
        reasons.append(
            "1H bullish reversal veto"
        )

    return {
        "score": round(score, 2),
        "base": round(base, 2),
        "modifier": round(
            modifier,
            2,
        ),
        "reasons": reasons,
        "veto": veto,
    }


# ============================================================
# EARLY MOMENTUM RADAR
# ============================================================

def detect_early_momentum(
    symbol,
    analyses,
    btc_market,
):
    one_h = analyses.get("1h")
    four_h = analyses.get("4h")

    if not one_h or not four_h:
        return None

    score = 0
    reasons = []

    # 1H volume expansion.
    if one_h["volume_ratio"] >= EARLY_VOLUME_RATIO:
        score += 18
        reasons.append(
            f"1H unusual volume ({one_h['volume_ratio']:.2f}x)"
        )

    # 1H price acceleration.
    if one_h["price"] > 0:

        if one_h["bullish"] > one_h["bearish"]:
            move = 0

            # We approximate using EMA20 distance.
            move = pct_change(
                one_h["ema20"],
                one_h["price"],
            )

            if move >= EARLY_1H_MOVE:
                score += 16
                reasons.append(
                    f"Strong 1H price acceleration ({move:+.2f}%)"
                )

        elif one_h["bearish"] > one_h["bullish"]:

            move = pct_change(
                one_h["ema20"],
                one_h["price"],
            )

            if move <= -EARLY_1H_MOVE:
                score += 16
                reasons.append(
                    f"Strong 1H downside acceleration ({move:+.2f}%)"
                )

    # Structure break.
    if one_h["breakout"]:
        score += 20
        reasons.append(
            "Fresh 1H breakout"
        )

    if one_h["breakdown"]:
        score += 20
        reasons.append(
            "Fresh 1H breakdown"
        )

    # 4H confirmation.
    if four_h["bullish"] >= 55:
        score += 10
        reasons.append(
            "4H bullish structure building"
        )

    if four_h["bearish"] >= 55:
        score += 10
        reasons.append(
            "4H bearish structure building"
        )

    # MACD acceleration.
    if one_h["macd_hist"] > 0:
        score += 8
        reasons.append(
            "Positive MACD momentum"
        )

    elif one_h["macd_hist"] < 0:
        score += 8
        reasons.append(
            "Negative MACD momentum"
        )

    # BTC transition can actually be interesting for
    # individual coin breakouts, but don't chase against
    # strong BTC reversal.
    if btc_market["regime"] == "TRANSITION":
        score += 3
        reasons.append(
            "BTC transition regime"
        )

    # Avoid extremely weak/no-volume setups.
    if (
        one_h["volume_ratio"] < 1.25
        and not one_h["breakout"]
        and not one_h["breakdown"]
    ):
        return None

    if score < MIN_EARLY_SCORE:
        return None

    if (
        one_h["bullish"]
        > one_h["bearish"]
    ):
        side = "LONG"
    elif (
        one_h["bearish"]
        > one_h["bullish"]
    ):
        side = "SHORT"
    else:
        return None

    return {
        "symbol": symbol,
        "side": side,
        "score": round(
            clamp(score, 0, 100),
            2,
        ),
        "reasons": reasons[:7],
        "price": one_h["price"],
        "timestamp": one_h["timestamp"],
    }


# ============================================================
# EXTENSION / CHASING FILTER
# ============================================================

def is_overextended(
    analyses,
    side,
):
    one_h = analyses.get("1h")
    four_h = analyses.get("4h")

    if not one_h or not four_h:
        return True

    one_h_move = pct_change(
        one_h["ema20"],
        one_h["price"],
    )

    four_h_move = pct_change(
        four_h["ema20"],
        four_h["price"],
    )

    if side == "LONG":

        if (
            one_h_move
            > MAX_1H_EXTENSION_FOR_NORMAL_ENTRY
        ):
            return True

        if (
            four_h_move
            > MAX_4H_EXTENSION_FOR_NORMAL_ENTRY
        ):
            return True

    else:

        if (
            one_h_move
            < -MAX_1H_EXTENSION_FOR_NORMAL_ENTRY
        ):
            return True

        if (
            four_h_move
            < -MAX_4H_EXTENSION_FOR_NORMAL_ENTRY
        ):
            return True

    return False


# ============================================================
# SIGNAL BUILDERS
# ============================================================

def make_signal(
    symbol,
    side,
    score_data,
    analyses,
    btc_market,
    fg_value,
    fg_text,
    derivatives,
    relative_reason,
):
    one_h = analyses["1h"]

    price = safe_float(
        one_h["price"]
    )

    atr_value = safe_float(
        one_h["atr"]
    )

    if atr_value <= 0:
        return None

    # ATR-based risk.
    risk_distance = atr_value * 1.45

    if side == "LONG":

        entry = price

        sl = entry - risk_distance

        risk = entry - sl

        tp1 = entry + risk * 1.35
        tp2 = entry + risk * 2.25
        tp3 = entry + risk * 3.15

    else:

        entry = price

        sl = entry + risk_distance

        risk = sl - entry

        tp1 = entry - risk * 1.35
        tp2 = entry - risk * 2.25
        tp3 = entry - risk * 3.15

    if risk <= 0:
        return None

    reasons = list(
        score_data["reasons"]
    )

    # Add actual technical evidence.
    if side == "LONG":
        reasons.extend(
            analyses["1h"]["bull_reasons"][:4]
        )
    else:
        reasons.extend(
            analyses["1h"]["bear_reasons"][:4]
        )

    if relative_reason:
        reasons.append(
            relative_reason
        )

    if derivatives.get("available"):
        funding = derivatives.get(
            "funding_rate"
        )

        if funding is not None:
            reasons.append(
                f"Perp funding {funding * 100:+.3f}%"
            )

    if fg_value is not None:
        reasons.append(
            f"Fear & Greed {fg_value} ({fg_text})"
        )

    return {
        "symbol": symbol,
        "side": side,

        "score": round(
            score_data["score"],
            2,
        ),

        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,

        "risk_pct": (
            risk / entry * 100
        ),

        "btc_regime":
            btc_market["regime"],

        "fg_value":
            fg_value,

        "fg_text":
            fg_text,

        "timeframes": {
            "1D": analyses["1d"]["direction"],
            "4H": analyses["4h"]["direction"],
            "1H": analyses["1h"]["direction"],
        },

        "technical": {
            "1d_bull":
                analyses["1d"]["bullish"],
            "1d_bear":
                analyses["1d"]["bearish"],

            "4h_bull":
                analyses["4h"]["bullish"],
            "4h_bear":
                analyses["4h"]["bearish"],

            "1h_bull":
                analyses["1h"]["bullish"],
            "1h_bear":
                analyses["1h"]["bearish"],

            "rsi":
                analyses["1h"]["rsi"],

            "adx":
                analyses["1h"]["adx"],

            "volume_ratio":
                analyses["1h"]["volume_ratio"],
        },

        "derivatives":
            derivatives,

        "reasons":
            list(dict.fromkeys(reasons))[:10],

        "created_at":
            now_utc().isoformat(),

        "candle_time":
            analyses["1h"]["timestamp"].isoformat(),

        "signal_type":
            "CONFIRMED",
    }


# ============================================================
# SIGNAL FORMAT
# ============================================================

def format_signal(signal):
    side = signal["side"]

    if side == "LONG":
        emoji = "🟢"
        title = "LONG"
    else:
        emoji = "🔴"
        title = "SHORT"

    reasons = ""

    for reason in signal["reasons"][:8]:
        reasons += (
            f"• {html.escape(str(reason))}\n"
        )

    return (
        f"{emoji} <b>{title} CONFIRMED</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>Coin:</b> {html.escape(signal['symbol'])}\n"
        f"<b>Confluence:</b> {signal['score']:.0f}/100\n\n"

        f"<b>Entry:</b> {format_price(signal['entry'])}\n"
        f"<b>Stop Loss:</b> {format_price(signal['sl'])}\n"
        f"<b>TP1:</b> {format_price(signal['tp1'])}\n"
        f"<b>TP2:</b> {format_price(signal['tp2'])}\n"
        f"<b>TP3:</b> {format_price(signal['tp3'])}\n\n"

        f"<b>1D:</b> {signal['timeframes']['1D']}\n"
        f"<b>4H:</b> {signal['timeframes']['4H']}\n"
        f"<b>1H:</b> {signal['timeframes']['1H']}\n"
        f"<b>BTC:</b> {signal['btc_regime']}\n"
        f"<b>RSI:</b> {signal['technical']['rsi']:.1f}\n"
        f"<b>ADX:</b> {signal['technical']['adx']:.1f}\n"
        f"<b>Volume:</b> {signal['technical']['volume_ratio']:.2f}x\n\n"

        f"<b>Evidence:</b>\n"
        f"{reasons}\n"

        f"⚠️ <i>Demo/test signal. No profit guarantee.</i>"
    )


def format_early_alert(alert):
    side_emoji = (
        "🟢"
        if alert["side"] == "LONG"
        else "🔴"
    )

    reasons = ""

    for reason in alert["reasons"]:
        reasons += (
            f"• {html.escape(str(reason))}\n"
        )

    return (
        f"🚨 <b>POTENTIAL MOVE / EARLY MOMENTUM</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>{html.escape(alert['symbol'])}</b> "
        f"{side_emoji} <b>{alert['side']}</b>\n"
        f"<b>Radar score:</b> {alert['score']:.0f}/100\n"
        f"<b>Price:</b> {format_price(alert['price'])}\n\n"
        f"<b>Why it is on radar:</b>\n"
        f"{reasons}\n"
        f"⚠️ <i>Early watch only — wait for confirmation.</i>"
    )


# ============================================================
# BUILD / ANALYZE ONE COIN
# ============================================================

def analyze_coin(
    exchange,
    symbol,
    btc_market,
    btc_1h_df,
    breadth,
    fg_value,
    fg_text,
):
    analyses = {}

    for tf in TIMEFRAMES:

        df = get_closed_candles(
            exchange,
            symbol,
            tf,
            250,
        )

        if df is None:
            return {
                "status": "REJECTED",
                "reason": f"Missing {tf} data",
            }

        result = analyze_timeframe(
            df,
            tf,
        )

        if result is None:
            return {
                "status": "REJECTED",
                "reason": f"Unable to analyze {tf}",
            }

        analyses[tf] = result

    # Relative strength against BTC.
    coin_1h_df = get_closed_candles(
        exchange,
        symbol,
        "1h",
        80,
    )

    relative_score, relative_reason = (
        get_relative_strength(
            coin_1h_df,
            btc_1h_df,
        )
    )

    # Derivatives are fetched for stronger technical
    # candidates only later in the scanner.
    derivatives = {}

    long_data = calculate_side_score(
        analyses,
        "LONG",
        btc_market,
        breadth,
        derivatives,
        relative_score,
        relative_reason,
    )

    short_data = calculate_side_score(
        analyses,
        "SHORT",
        btc_market,
        breadth,
        derivatives,
        relative_score,
        relative_reason,
    )

    early = detect_early_momentum(
        symbol,
        analyses,
        btc_market,
    )

    return {
        "status": "ANALYZED",
        "symbol": symbol,
        "analyses": analyses,
        "long": long_data,
        "short": short_data,
        "early": early,
        "relative_score": relative_score,
        "relative_reason": relative_reason,
        "derivatives": derivatives,
    }


# ============================================================
# RE-EVALUATE DERIVATIVES FOR CANDIDATES
# ============================================================

def apply_derivatives_to_candidate(
    symbol,
    result,
    btc_market,
    breadth,
):
    try:
        base = symbol.split("/")[0]

        derivatives = get_swap_derivatives(
            base
        )

        result["derivatives"] = derivatives

        long_data = calculate_side_score(
            result["analyses"],
            "LONG",
            btc_market,
            breadth,
            derivatives,
            result["relative_score"],
            result["relative_reason"],
        )

        short_data = calculate_side_score(
            result["analyses"],
            "SHORT",
            btc_market,
            breadth,
            derivatives,
            result["relative_score"],
            result["relative_reason"],
        )

        result["long"] = long_data
        result["short"] = short_data

        return result

    except Exception as e:
        print(
            f"Derivative analysis error {symbol}:",
            e,
        )

        return result


# ============================================================
# FINAL SETUP FILTER
# ============================================================

def candidate_signal(
    symbol,
    result,
    btc_market,
    fg_value,
    fg_text,
):
    analyses = result["analyses"]

    long_data = result["long"]
    short_data = result["short"]

    candidates = []

    # -----------------------------
    # LONG
    # -----------------------------

    if (
        long_data["score"]
        >= MIN_CONFIRMED_SCORE
        and not long_data["veto"]
        and not is_overextended(
            analyses,
            "LONG",
        )
        and not is_on_cooldown(
            symbol,
            "LONG",
        )
    ):

        one_h = analyses["1h"]

        # Do not allow a long if 1H is clearly bearish.
        if not (
            one_h["bearish"]
            >= one_h["bullish"] + 12
        ):

            signal = make_signal(
                symbol,
                "LONG",
                long_data,
                analyses,
                btc_market,
                fg_value,
                fg_text,
                result.get(
                    "derivatives",
                    {},
                ),
                result.get(
                    "relative_reason",
                    "",
                ),
            )

            if signal:
                candidates.append(
                    signal
                )

    # -----------------------------
    # SHORT
    # -----------------------------

    if (
        short_data["score"]
        >= MIN_CONFIRMED_SCORE
        and not short_data["veto"]
        and not is_overextended(
            analyses,
            "SHORT",
        )
        and not is_on_cooldown(
            symbol,
            "SHORT",
        )
    ):

        one_h = analyses["1h"]

        if not (
            one_h["bullish"]
            >= one_h["bearish"] + 12
        ):

            signal = make_signal(
                symbol,
                "SHORT",
                short_data,
                analyses,
                btc_market,
                fg_value,
                fg_text,
                result.get(
                    "derivatives",
                    {},
                ),
                result.get(
                    "relative_reason",
                    "",
                ),
            )

            if signal:
                candidates.append(
                    signal
                )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    return candidates[0]


# ============================================================
# SCAN MARKET
# ============================================================

def scan_market(exchange):
    global daily_confirmed_sent
    global daily_early_sent

    reset_daily_counter()

    print()
    print("=" * 60)
    print(
        "V3 MARKET SCAN",
        now_utc().isoformat(),
    )
    print("=" * 60)

    # --------------------------------
    # BTC
    # --------------------------------

    btc_market = get_btc_market_analysis(
        exchange
    )

    print(
        "BTC regime:",
        btc_market["regime"],
        "long:",
        btc_market.get("long_score"),
        "short:",
        btc_market.get("short_score"),
    )

    btc_1h_df = get_closed_candles(
        exchange,
        "BTC/USDT",
        "1h",
        80,
    )

    # --------------------------------
    # Fear & Greed
    # --------------------------------

    fg_value, fg_text = get_fear_greed()

    print(
        "Fear & Greed:",
        fg_value,
        fg_text,
    )

    # --------------------------------
    # Universe
    # --------------------------------

    coins, available_count = (
        get_top_coins(exchange)
    )

    print(
        "Coins available:",
        available_count,
    )

    print(
        "Coins selected for technical scan:",
        len(coins),
    )

    if not coins:
        print(
            "No coins available."
        )
        return

    symbols = [
        item["symbol"]
        for item in coins
    ]

    # --------------------------------
    # Breadth
    # --------------------------------

    breadth = calculate_market_breadth(
        exchange,
        symbols,
        max_check=30,
    )

    print(
        "Market breadth:",
        breadth,
    )

    # --------------------------------
    # Scan
    # --------------------------------

    analyzed = 0
    rejected = 0

    rejection_reasons = {}

    confirmed_candidates = []
    early_candidates = []

    full_results = {}

    for index, item in enumerate(
        coins,
        start=1,
    ):

        symbol = item["symbol"]

        print(
            f"[{index}/{len(coins)}] {symbol}"
        )

        if has_active_same_coin(
            symbol
        ):
            rejected += 1

            rejection_reasons[
                "Active signal"
            ] = (
                rejection_reasons.get(
                    "Active signal",
                    0,
                )
                + 1
            )

            continue

        try:

            result = analyze_coin(
                exchange,
                symbol,
                btc_market,
                btc_1h_df,
                breadth,
                fg_value,
                fg_text,
            )

            if result["status"] != "ANALYZED":
                rejected += 1

                reason = result.get(
                    "reason",
                    "Unknown",
                )

                rejection_reasons[
                    reason
                ] = (
                    rejection_reasons.get(
                        reason,
                        0,
                    )
                    + 1
                )

                continue

            analyzed += 1

            full_results[
                symbol
            ] = result

            # Save strongest technical candidates for
            # derivatives analysis.
            long_score = result[
                "long"
            ]["score"]

            short_score = result[
                "short"
            ]["score"]

            best_score = max(
                long_score,
                short_score,
            )

            if best_score >= 62:

                result = (
                    apply_derivatives_to_candidate(
                        symbol,
                        result,
                        btc_market,
                        breadth,
                    )
                )

                full_results[
                    symbol
                ] = result

            signal = candidate_signal(
                symbol,
                result,
                btc_market,
                fg_value,
                fg_text,
            )

            if signal:
                confirmed_candidates.append(
                    signal
                )

            early = result.get(
                "early"
            )

            if early:
                early_candidates.append(
                    early
                )

        except Exception as e:

            rejected += 1

            reason = "Analysis exception"

            rejection_reasons[
                reason
            ] = (
                rejection_reasons.get(
                    reason,
                    0,
                )
                + 1
            )

            print(
                f"{symbol} error:",
                e,
            )

    # --------------------------------
    # Remove duplicates
    # --------------------------------

    confirmed_candidates.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    early_candidates.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    # --------------------------------
    # Send early radar
    # --------------------------------

    early_sent_this_scan = 0

    if (
        daily_early_sent
        < MAX_EARLY_PER_DAY
    ):

        for alert in early_candidates:

            if (
                early_sent_this_scan
                >= MAX_EARLY_PER_SCAN
            ):
                break

            # Don't send early alert if the coin already
            # has a confirmed signal candidate.
            if any(
                signal["symbol"]
                == alert["symbol"]
                for signal
                in confirmed_candidates
            ):
                continue

            message = format_early_alert(
                alert
            )

            if send_telegram(
                message
            ):

                daily_early_sent += 1
                early_sent_this_scan += 1

                journal_event(
                    {
                        "event":
                            "EARLY_ALERT",
                        "symbol":
                            alert["symbol"],
                        "side":
                            alert["side"],
                        "score":
                            alert["score"],
                    }
                )

    # --------------------------------
    # Send confirmed
    # --------------------------------

    confirmed_sent_this_scan = 0

    for signal in confirmed_candidates:

        if (
            confirmed_sent_this_scan
            >= MAX_CONFIRMED_PER_SCAN
        ):
            break

        if (
            daily_confirmed_sent
            >= MAX_CONFIRMED_PER_DAY
        ):
            break

        if has_active_same_coin(
            signal["symbol"]
        ):
            continue

        message = format_signal(
            signal
        )

        if send_telegram(
            message
        ):

            remember_signal(
                signal
            )

            daily_confirmed_sent += 1
            confirmed_sent_this_scan += 1

    # --------------------------------
    # Diagnostics
    # --------------------------------

    print()
    print("========== V3 AUDIT ==========")
    print(
        "Coins available:",
        available_count,
    )

    print(
        "Actually analyzed:",
        analyzed,
    )

    print(
        "Rejected:",
        rejected,
    )

    print(
        "Early opportunities:",
        len(early_candidates),
    )

    print(
        "Confirmed setups:",
        len(confirmed_candidates),
    )

    print(
        "Confirmed sent this scan:",
        confirmed_sent_this_scan,
    )

    print(
        "Early sent this scan:",
        early_sent_this_scan,
    )

    print(
        "Daily confirmed:",
        daily_confirmed_sent,
        "/",
        MAX_CONFIRMED_PER_DAY,
    )

    print(
        "Daily early:",
        daily_early_sent,
        "/",
        MAX_EARLY_PER_DAY,
    )

    print(
        "BTC regime:",
        btc_market["regime"],
    )

    print(
        "Breadth:",
        breadth["status"],
        breadth["breadth"],
    )

    if rejection_reasons:

        print()
        print(
            "Top rejection reasons:"
        )

        sorted_rejections = sorted(
            rejection_reasons.items(),
            key=lambda x: x[1],
            reverse=True,
        )

        for reason, count in (
            sorted_rejections[:10]
        ):
            print(
                f" - {reason}: {count}"
            )

    print(
        "=============================="
    )
    print()


# ============================================================
# STARTUP
# ============================================================

def send_startup_message():
    message = (
        "🤖 <b>Crypto Signal Bot V3 Started</b>\n\n"
        "🧠 Independent LONG/SHORT engine\n"
        "📊 1D + 4H + 1H analysis\n"
        "₿ BTC regime detection\n"
        "🚨 Early momentum radar\n"
        "🔄 1H reversal protection\n"
        "📈 Volume + structure + momentum\n"
        "💰 Funding/OI intelligence when available\n"
        "📝 Signal journal enabled\n\n"
        "⚠️ Demo/testing mode — no trade execution."
    )

    send_telegram(
        message
    )


# ============================================================
# MAIN LOOP
# ============================================================

def run_bot():

    load_journal()

    exchange = ccxt.okx(
        {
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
            },
        }
    )

    print()
    print(
        "=========================================="
    )
    print(
        "      CRYPTO SIGNAL BOT V3"
    )
    print(
        "=========================================="
    )

    print(
        "Exchange: OKX"
    )

    print(
        "Mode: MARKET DATA / SIGNAL ONLY"
    )

    print(
        "Scan interval:",
        SCAN_INTERVAL,
        "seconds",
    )

    print(
        "Max universe:",
        MAX_UNIVERSE,
    )

    print(
        "=========================================="
    )

    send_startup_message()

    while True:

        try:

            cleanup_active_signals(
                exchange
            )

            scan_market(
                exchange
            )

        except Exception as e:

            print(
                "MAIN LOOP ERROR:",
                e,
            )

            try:
                send_telegram(
                    "⚠️ <b>Bot Error</b>\n"
                    f"<code>{html.escape(str(e))}</code>\n"
                    "Bot will retry automatically."
                )
            except Exception:
                pass

        print(
            f"Sleeping {SCAN_INTERVAL} seconds..."
        )

        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    run_bot()
