import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import time
import os
from datetime import datetime

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

SCAN_INTERVAL = 900
TOP_N = 30

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=10)
        data = r.json()['data'][0]
        return int(data['value']), data['value_classification']
    except:
        return None, "Unknown"

def get_technical_score(df):
    if len(df) < 50:
        return 0, []

    df['rsi'] = ta.rsi(df['close'], length=14)
    macd = ta.macd(df['close'])
    df = pd.concat([df, macd], axis=1)
    
    df['ema9'] = ta.ema(df['close'], length=9)
    df['ema21'] = ta.ema(df['close'], length=21)
    
    bb = ta.bbands(df['close'], length=20)
    df = pd.concat([df, bb], axis=1)
    
    stoch = ta.stochrsi(df['close'])
    df = pd.concat([df, stoch], axis=1)
    
    df['vol_ma'] = df['volume'].rolling(20).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0
    reasons = []

    if last['rsi'] < 30:
        score += 2
        reasons.append("RSI Oversold")
    elif last['rsi'] < 40:
        score += 1
        reasons.append("RSI Low")
    elif last['rsi'] > 70:
        score -= 2
        reasons.append("RSI Overbought")
    elif last['rsi'] > 60:
        score -= 1
        reasons.append("RSI High")

    if last['MACD_12_26_9'] > last['MACDs_12_26_9'] and prev['MACD_12_26_9'] <= prev['MACDs_12_26_9']:
        score += 2
        reasons.append("MACD Bullish Cross")
    elif last['MACD_12_26_9'] < last['MACDs_12_26_9'] and prev['MACD_12_26_9'] >= prev['MACDs_12_26_9']:
        score -= 2
        reasons.append("MACD Bearish Cross")

    if last['ema9'] > last['ema21'] and prev['ema9'] <= prev['ema21']:
        score += 2
        reasons.append("EMA Bullish Cross")
    elif last['ema9'] < last['ema21'] and prev['ema9'] >= prev['ema21']:
        score -= 2
        reasons.append("EMA Bearish Cross")

    if last['close'] < last['BBL_20_2.0']:
        score += 1
        reasons.append("Below Lower BB")
    elif last['close'] > last['BBU_20_2.0']:
        score -= 1
        reasons.append("Above Upper BB")

    if last['STOCHRSIk_14_14_3_3'] < 20:
        score += 1
        reasons.append("StochRSI Oversold")
    elif last['STOCHRSIk_14_14_3_3'] > 80:
        score -= 1
        reasons.append("StochRSI Overbought")

    if last['volume'] > last['vol_ma'] * 1.8:
        if score > 0:
            score += 1
        elif score < 0:
            score -= 1
        reasons.append("High Volume")

    return score, reasons

def get_btc_trend(exchange):
    try:
        score_1h = 0
        score_4h = 0

        for tf in ['1h', '4h']:
            ohlcv = exchange.fetch_ohlcv('BTC/USDT', timeframe=tf, limit=50)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            score, _ = get_technical_score(df)
            if tf == '1h':
                score_1h = score
            else:
                score_4h = score

        market_score = (score_1h + score_4h) / 2

        if market_score >= 2:
            return "Bullish", market_score
        elif market_score <= -2:
            return "Bearish", market_score
        else:
            return "Neutral", market_score
    except:
        return "Neutral", 0

def analyze_coin(df, timeframe, market_trend, market_score, fg_value, fg_text):
    tech_score, reasons = get_technical_score(df)

    if tech_score == 0:
        return None

    final_score = tech_score

    if tech_score > 0 and market_trend == "Bearish":
        final_score -= 1
        reasons.append("Market is Bearish (caution)")
    elif tech_score < 0 and market_trend == "Bullish":
        final_score += 1
        reasons.append("Market is Bullish (caution)")

    if fg_value is not None:
        if tech_score > 0 and fg_value <= 25:
            final_score += 1
            reasons.append(f"Fear & Greed: {fg_text}")
        elif tech_score < 0 and fg_value >= 75:
            final_score -= 1
            reasons.append(f"Fear & Greed: {fg_text}")

    if final_score >= 4:
        signal = "🟢 STRONG BUY"
    elif final_score >= 2:
        signal = "🟢 BUY"
    elif final_score <= -4:
        signal = "🔴 STRONG SELL"
    elif final_score <= -2:
        signal = "🔴 SELL"
    else:
        return None

    return {
        "signal": signal,
        "tech_score": tech_score,
        "final_score": final_score,
        "price": df.iloc[-1]['close'],
        "reasons": reasons,
        "timeframe": timeframe,
        "market_trend": market_trend,
        "fg_text": fg_text
    }

def get_top_coins(exchange):
    try:
        tickers = exchange.fetch_tickers()
        usdt_pairs = []
        for symbol, data in tickers.items():
            if symbol.endswith('/USDT') and data.get('quoteVolume'):
                usdt_pairs.append({
                    'symbol': symbol,
                    'volume': data['quoteVolume']
                })
        usdt_pairs = sorted(usdt_pairs, key=lambda x: x['volume'], reverse=True)
        return [x['symbol'] for x in usdt_pairs[:TOP_N]]
    except Exception as e:
        print("Error fetching top coins:", e)
        return ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT"]

def run_bot():
    # Bybit use kar rahe hain (Binance block ho raha tha)
    exchange = ccxt.okx({
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })

    print("Upgraded Bot Started (Bybit)...")
    send_telegram("✅ <b>Crypto Signal Bot Started (Bybit)</b>\nTechnical + Market Analysis active")

    while True:
        try:
            market_trend, market_score = get_btc_trend(exchange)
            fg_value, fg_text = get_fear_greed()

            coins = get_top_coins(exchange)
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Market: {market_trend} | Fear&Greed: {fg_text}")

            for symbol in coins:
                for tf in ['1h', '4h']:
                    try:
                        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=100)
                        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                        result = analyze_coin(df, tf.upper(), market_trend, market_score, fg_value, fg_text)

                        if result:
                            msg = f"""
<b>{result['signal']}</b>
━━━━━━━━━━━━━━━━
🪙 <b>Coin:</b> {symbol}
⏰ <b>Timeframe:</b> {result['timeframe']}
💰 <b>Price:</b> {result['price']:.6f}
📊 <b>Final Score:</b> {result['final_score']}
📈 <b>Market:</b> {result['market_trend']}
😱 <b>Fear & Greed:</b> {result['fg_text']}
📌 <b>Reasons:</b>
""" + "\n".join([f"• {r}" for r in result['reasons']]) + f"""
━━━━━━━━━━━━━━━━
⚠️ <i>Apni analysis zaroor karna</i>
"""
                            send_telegram(msg)
                            print(f"Signal: {symbol} → {result['signal']}")
                            time.sleep(1.5)

                    except Exception as e:
                        print(f"Error {symbol} {tf}:", e)
                        continue

            print(f"Scan complete. Next scan in {SCAN_INTERVAL//60} min...")
            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            print("Main error:", e)
            time.sleep(60)

if __name__ == "__main__":
    run_bot()
