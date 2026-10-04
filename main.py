import os, re, json, time, html, math, traceback, threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests, pandas as pd

# ========================= V4 CONFIG =========================
APP="CRYPTO SIGNAL BOT V4"
BASE=os.getenv("OKX_BASE_URL","https://www.okx.com").rstrip("/")
TOKEN=os.getenv("TELEGRAM_TOKEN","").strip()
CHAT_ID=os.getenv("CHAT_ID","").strip()
SCAN_INTERVAL=int(os.getenv("SCAN_INTERVAL","900"))
MAX_UNIVERSE=int(os.getenv("MAX_UNIVERSE","80"))
FETCH_WORKERS=int(os.getenv("FETCH_WORKERS","4"))
MAX_CONFIRMED=int(os.getenv("MAX_CONFIRMED_PER_SCAN","3"))
MAX_EARLY=int(os.getenv("MAX_EARLY_PER_SCAN","5"))
DAILY_CONF=int(os.getenv("DAILY_CONFIRMED_LIMIT","5"))
DAILY_EARLY=int(os.getenv("DAILY_EARLY_LIMIT","8"))
NEWS_HOURS=float(os.getenv("NEWS_LOOKBACK_HOURS","12"))
ENABLE_NEWS=os.getenv("ENABLE_NEWS","true").lower() in ("1","true","yes","on")
STATE_FILE=os.getenv("STATE_FILE","v4_state.json")
JOURNAL_FILE=os.getenv("JOURNAL_FILE","v4_journal.jsonl")
STABLE={"USDT","USDC","USDE","DAI","FDUSD","TUSD","USDD","USDG","PYUSD","EURC"}
LEV=re.compile(r"(^|[-_])(2L|2S|3L|3S|5L|5S)([-_]|$)",re.I)
S=requests.Session()
S.headers.update({"User-Agent":"CryptoSignalBotV4/4.0","Accept":"application/json,text/xml,*/*"})
LOCK=threading.Lock()
ACTIVE={}
COOLDOWN={}
EARLY_STATE={}
DAILY={"date":"","confirmed":0,"early":0}
NEWS_CACHE={"ts":0,"items":[]}
DERIV_CACHE={}
FNG_CACHE={"ts":0,"value":None}
