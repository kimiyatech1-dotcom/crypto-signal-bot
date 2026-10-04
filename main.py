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

RSS=os.getenv(
    "NEWS_RSS_URLS",
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml,"
    "https://cointelegraph.com/rss,https://cryptopotato.com/feed/,"
    "https://cryptoslate.com/feed/,https://cryptonews.com/news/feed/,"
    "https://finance.yahoo.com/news/rssindex"
).split(",")

POS={"approval":2,"approved":2,"adoption":2,"partnership":2,"integration":2,
     "upgrade":1,"mainnet":2,"institutional":2,"inflows":2,"buyback":2,
     "burn":2,"tokenization":2,"etf":2,"listing":1,"launch":1}
NEG={"hack":-5,"exploit":-5,"breach":-5,"stolen":-5,"lawsuit":-2,"ban":-3,
     "delist":-4,"outflow":-2,"liquidation":-2,"unlock":-1,"attack":-5,
     "vulnerability":-4,"scam":-5,"bankrupt":-5}
MACRO={"fed":3,"fomc":4,"rate cut":4,"rate hike":-4,"interest rate":3,
       "pce":4,"cpi":4,"inflation":3,"payroll":4,"jobs":3,"unemployment":3,
       "gdp":3,"treasury yield":-3,"bond yield":-3,"dxy":-2,"recession":-4,
       "etf inflow":3,"etf outflow":-3,"institutional":2}
ALIASES={
 "BTC":["bitcoin","btc"],"ETH":["ethereum","ether","eth"],"AAVE":["aave"],
 "WLD":["worldcoin","world"],"VIRTUAL":["virtual","virtuals"],
 "VIRT":["virtual","virtuals"],"ONDO":["ondo"],"QNT":["quant"],
 "ZEC":["zcash","zec"],"CRV":["curve","crv"],"LINK":["chainlink","link"],
 "COMP":["compound","comp"],"SUI":["sui"],"DOGE":["dogecoin","doge"],
 "LTC":["litecoin","ltc"]
}

def now(): return datetime.now(timezone.utc)
def iso(): return now().isoformat()
def f(v,d=None):
    try: return float(v)
    except: return d
def clamp(x,a,b): return max(a,min(b,x))
def pct(a,b):
    return None if a is None or not b else (a/b-1)*100
def price(x):
    if x is None:return "n/a"
    if x>=1000:return f"{x:,.2f}"
    if x>=1:return f"{x:.4f}"
    if x>=.1:return f"{x:.5f}"
    if x>=.01:return f"{x:.6f}"
    return f"{x:.8f}"

def get(url,params=None,timeout=12):
    last=None
    for i in range(3):
        try:
            r=S.get(url,params=params,timeout=timeout)
            if r.status_code==429: raise RuntimeError("429 rate limit")
            r.raise_for_status(); return r
        except Exception as e:
            last=e; time.sleep(.4*(2**i))
    raise last

def okx(path,params=None):
    d=get(BASE+path,params).json()
    if d.get("code") not in (None,"0",0): raise RuntimeError(d.get("msg","OKX error"))
    return d.get("data",[])

def save():
    try:
        with open(STATE_FILE,"w",encoding="utf8") as x:
            json.dump({"active":ACTIVE,"cooldown":COOLDOWN,"early":EARLY_STATE,"daily":DAILY},x,indent=2)
    except: pass

def load():
    global ACTIVE,COOLDOWN,EARLY_STATE,DAILY
    try:
        x=json.load(open(STATE_FILE,encoding="utf8"))
        ACTIVE=x.get("active",{}); COOLDOWN=x.get("cooldown",{})
        EARLY_STATE=x.get("early",{}); DAILY=x.get("daily",DAILY)
    except: pass

def journal(event,data):
    try:
        with open(JOURNAL_FILE,"a",encoding="utf8") as x:
            x.write(json.dumps({"ts":iso(),"event":event,**data},ensure_ascii=False,default=str)+"\n")
    except: pass

def tg(msg):
    if not TOKEN or not CHAT_ID:
        print("Telegram credentials missing"); return False
    try:
        r=S.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                 json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"HTML",
                       "disable_web_page_preview":True},timeout=12)
        r.raise_for_status(); return True
    except Exception as e:
        print("Telegram:",e); return False

# ========================= MARKET =========================
def universe():
    ins=okx("/api/v5/public/instruments",{"instType":"SPOT"})
    live=[]
    for z in ins:
        i=z.get("instId","")
        if z.get("state")!="live" or not i.endswith("-USDT"): continue
        b=i[:-5]
        if b in STABLE or LEV.search(b): continue
        live.append(i)
    ticks=okx("/api/v5/market/tickers",{"instType":"SPOT"})
    vol={z.get("instId"):f(z.get("volCcy24h"),0) for z in ticks}
    live.sort(key=lambda i:vol.get(i,0),reverse=True)
    if "BTC-USDT" in live:
        live.remove("BTC-USDT"); live.insert(0,"BTC-USDT")
    return live[:MAX_UNIVERSE],len(live)

def candles(inst,bar,limit=220):
    d=okx("/api/v5/market/candles",{"instId":inst,"bar":bar,"limit":min(limit,300)})
    if not d:return pd.DataFrame()
    cols=["ts","open","high","low","close","volume","vb","vq","confirm"]
    df=pd.DataFrame(d,columns=cols[:len(d[0])])
    for c in cols[1:8]:
        if c in df: df[c]=pd.to_numeric(df[c],errors="coerce")
    df["ts"]=pd.to_datetime(pd.to_numeric(df["ts"]),unit="ms",utc=True)
    df=df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    if len(df)>2 and str(df.iloc[-1].get("confirm","1"))!="1": df=df.iloc[:-1]
    return df

# ========================= INDICATORS =========================
def ema(s,n): return s.ewm(span=n,adjust=False,min_periods=n).mean()
def rsi(s,n=14):
    d=s.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
    ag=up.ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    al=dn.ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    return (100-100/(1+ag/al.replace(0,math.nan))).fillna(50)
def atr(df,n=14):
    p=df.close.shift(1)
    tr=pd.concat([df.high-df.low,(df.high-p).abs(),(df.low-p).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n,adjust=False,min_periods=n).mean()
def macd(s):
    m=ema(s,12)-ema(s,26); sig=ema(m,9); return m,sig,m-sig
def adx(df,n=14):
    up=df.high.diff(); dn=-df.low.diff()
    p=up.where((up>dn)&(up>0),0); m=dn.where((dn>up)&(dn>0),0)
    prev=df.close.shift(1)
    tr=pd.concat([df.high-df.low,(df.high-prev).abs(),(df.low-prev).abs()],axis=1).max(axis=1)
    av=tr.ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    pi=100*p.ewm(alpha=1/n,adjust=False,min_periods=n).mean()/av
    mi=100*m.ewm(alpha=1/n,adjust=False,min_periods=n).mean()/av
    dx=100*(pi-mi).abs()/(pi+mi).replace(0,math.nan)
    return dx.ewm(alpha=1/n,adjust=False,min_periods=n).mean(),pi,mi

def structure(df):
    if len(df)<8:return "UNKNOWN"
    h,l=df.high,df.low
    hh=h.iloc[-1]>h.iloc[-4]; hl=l.iloc[-1]>l.iloc[-4]
    lh=h.iloc[-1]<h.iloc[-4]; ll=l.iloc[-1]<l.iloc[-4]
    if hh and hl:return "HH_HL"
    if lh and ll:return "LH_LL"
    if hh or hl:return "BULLISH_BUILD"
    if lh or ll:return "BEARISH_BUILD"
    return "RANGE"

def analyze(df):
    if df is None or len(df)<80:return None
    df=df.copy()
    df["e20"]=ema(df.close,20); df["e50"]=ema(df.close,50); df["e200"]=ema(df.close,200)
    df["rsi"]=rsi(df.close); df["atr"]=atr(df); df["macd"],df["msig"],df["mh"]=macd(df.close)
    # FIX: adx() ek hi baar calculate hota hai (pehle 3 baar hota tha)
    _adx,_dip,_dim=adx(df)
    df["adx"]=_adx; df["di+"]=_dip; df["di-"]=_dim
    df["vma"]=df.volume.rolling(20).mean(); df["vr"]=df.volume/df.vma.replace(0,math.nan)
    df["hi20"]=df.high.rolling(20).max().shift(1); df["lo20"]=df.low.rolling(20).min().shift(1)
    df["r1"]=df.close.pct_change()*100; df["r3"]=df.close.pct_change(3)*100; df["r6"]=df.close.pct_change(6)*100
    df["rng"]=(df.high-df.low).replace(0,math.nan)
    df["body"]=(df.close-df.open).abs()/df.rng
    x=df.iloc[-1]; p=df.iloc[-2]
    bull=bear=0; br=[]; sr=[]
    if x.close>x.e20:bull+=2;br.append("price>EMA20")
    else:bear+=2;sr.append("price<EMA20")
    if x.e20>x.e50:bull+=2;br.append("EMA20>EMA50")
    else:bear+=2;sr.append("EMA20<EMA50")
    if x.e50>x.e200:bull+=2;br.append("EMA50>EMA200")
    else:bear+=2;sr.append("EMA50<EMA200")
    if x.mh>0 and x.mh>p.mh:bull+=2;br.append("MACD rising")
    elif x.mh<0 and x.mh<p.mh:bear+=2;sr.append("MACD falling")
    if x["di+"]>x["di-"] and x.adx>=18:bull+=2
    elif x["di-"]>x["di+"] and x.adx>=18:bear+=2
    if x.rsi>=55:bull+=1
    elif x.rsi<=45:bear+=1
    if x.close>x.hi20:bull+=2;br.append("fresh breakout")
    if x.close<x.lo20:bear+=2;sr.append("fresh breakdown")
    st=structure(df)
    if st in ("HH_HL","BULLISH_BUILD"):bull+=2;br.append("bullish structure")
    elif st in ("LH_LL","BEARISH_BUILD"):bear+=2;sr.append("bearish structure")
    if x.body>=.60:
        if x.close>x.open:bull+=1
        else:bear+=1
    gap=bull-bear
    direction="BULLISH" if gap>=4 else "BEARISH" if gap<=-4 else "MIXED"
    return dict(direction=direction,bull=bull,bear=bear,gap=gap,close=f(x.close),
      e20=f(x.e20),e50=f(x.e50),e200=f(x.e200),rsi=f(x.rsi),atr=f(x.atr),
      mh=f(x.mh),adx=f(x.adx),dip=f(x["di+"]),dim=f(x["di-"]),vr=f(x.vr,1),
      r1=f(x.r1),r3=f(x.r3),r6=f(x.r6),breakout=bool(x.close>x.hi20),
      breakdown=bool(x.close<x.lo20),structure=st,hi20=f(x.hi20),lo20=f(x.lo20),
      bull_reasons=br,bear_reasons=sr,df=df)

# ========================= NEWS / MACRO =========================
def feed(xml,source):
    out=[]
    try: root=ET.fromstring(xml)
    except: return out
    for item in root.iter():
        if item.tag.lower().split("}")[-1] not in ("item","entry"):continue
        title=link=pub=""
        for c in list(item):
            t=c.tag.lower().split("}")[-1]; txt=(c.text or "").strip()
            if t=="title":title=txt
            elif t=="link":link=c.attrib.get("href","") or txt
            elif t in ("pubdate","published","updated"):pub=txt
        if title:out.append({"title":html.unescape(re.sub(r"\s+"," ",title)),
                             "link":link,"published":pub,"source":source})
    return out

def news():
    if not ENABLE_NEWS:return []
    if time.time()-NEWS_CACHE["ts"]<300:return NEWS_CACHE["items"]
    all=[]
    for u in RSS:
        try:all+=feed(get(u,timeout=8).text,u)
        except Exception as e:print("RSS:",e)
    uniq={}
    for x in all:
        k=re.sub(r"[^a-z0-9]+"," ",x["title"].lower()).strip();uniq[k]=x
    NEWS_CACHE.update(ts=time.time(),items=list(uniq.values())[:250])
    return NEWS_CACHE["items"]

def termscore(text,terms):
    t=text.lower(); s=0; hits=[]
    for k,v in terms.items():
        if k in t:s+=v;hits.append(k)
    return s,hits

def asset_news(sym,items):
    base=sym.split("/")[0].lower()
    words=ALIASES.get(base.upper(),[base])
    found=[]
    for x in items:
        if any(w in x["title"].lower() for w in words):
            ps,ph=termscore(x["title"],POS); ns,nh=termscore(x["title"],NEG)
            found.append({"title":x["title"],"score":ps+ns,"source":x["source"]})
    found.sort(key=lambda x:abs(x["score"]),reverse=True);found=found[:5]
    total=sum(x["score"] for x in found)
    return {"bias":"POSITIVE" if total>=3 else "NEGATIVE" if total<=-3 else "MIXED" if found else "NONE",
            "score":clamp(total,-8,8),"items":found}

def macro_news(items):
    arr=[]
    for x in items:
        s,h=termscore(x["title"],MACRO)
        if s:arr.append({"title":x["title"],"score":s})
    total=clamp(sum(x["score"] for x in arr),-10,10)
    arr.sort(key=lambda x:abs(x["score"]),reverse=True)
    return {"bias":"RISK_POSITIVE" if total>=3 else "RISK_NEGATIVE" if total<=-3 else "MIXED",
            "score":total,"headlines":arr[:5]}

# ========================= FEAR & GREED / DERIVATIVES =========================
def fng():
    if time.time()-FNG_CACHE["ts"]<900:return FNG_CACHE["value"]
    try:
        d=get("https://api.alternative.me/fng/",{"limit":1}).json()["data"][0]
        FNG_CACHE.update(ts=time.time(),value=(int(d["value"]),d["value_classification"]))
        return FNG_CACHE["value"]
    except:return None

def deriv(base):
    if base in DERIV_CACHE and time.time()-DERIV_CACHE[base][0]<300:return DERIV_CACHE[base][1]
    o={"funding":None,"oi":None,"available":False}
    inst=f"{base}-USDT-SWAP"
    try:
        z=okx("/api/v5/public/funding-rate",{"instId":inst})
        if z:o["funding"]=f(z[0].get("fundingRate"));o["available"]=True
    except:pass
    try:
        z=okx("/api/v5/public/open-interest",{"instType":"SWAP","instId":inst})
        if z:o["oi"]=f(z[0].get("oi"));o["available"]=True
    except:pass
    DERIV_CACHE[base]=(time.time(),o);return o

# ========================= REGIME =========================
def bval(x):return 1 if x and x["direction"]=="BULLISH" else -1 if x and x["direction"]=="BEARISH" else 0
def btc_regime(a,b,c,macro):
    base=.25*bval(a)+.35*bval(b)+.40*bval(c)
    if c["direction"]=="BEARISH" and c["gap"]<=-6:base-=.25
    if c["direction"]=="BULLISH" and c["gap"]>=6:base+=.20
    if macro["bias"]=="RISK_NEGATIVE":base-=.10
    if macro["bias"]=="RISK_POSITIVE":base+=.10
    down=c["direction"]=="BEARISH" and c["r1"]<=-1.5 and c["vr"]>=1.5
    up=c["direction"]=="BULLISH" and c["r1"]>=1.5 and c["vr"]>=1.5
    if down:reg="RISK_OFF"
    elif up:reg="RISK_ON"
    elif base>=.45:reg="RISK_ON"
    elif base<=-.45:reg="RISK_OFF"
    elif base>=.15:reg="RECOVERY"
    elif base<=-.15:reg="DISTRIBUTION"
    else:reg="TRANSITION"
    return {"regime":reg,"base":round(base,3),"shock_down":down,"shock_up":up}

def breadth(rows):
    x=[r["h1"] for r in rows if r.get("h1")]
    if not x:return {"status":"UNKNOWN","value":0,"bull":0,"bear":0}
    bu=sum(z["direction"]=="BULLISH" for z in x);be=sum(z["direction"]=="BEARISH" for z in x)
    v=(bu-be)/len(x)
    return {"status":"BULLISH" if v>=.25 else "BEARISH" if v<=-.25 else "MIXED",
            "value":round(v,3),"bull":bu,"bear":be}

def rs(a,b):return None if not a or not b or a["r6"] is None or b["r6"] is None else a["r6"]-b["r6"]

# ========================= RISK / SCORE =========================
def extended(a,b,side):
    if side=="LONG":
        if a["e20"] and a["close"]>a["e20"]*1.08:return True,"1H >8% above EMA20"
        if b["e20"] and b["close"]>b["e20"]*1.18:return True,"4H >18% above EMA20"
    else:
        if a["e20"] and a["close"]<a["e20"]*.92:return True,"1H >8% below EMA20"
        if b["e20"] and b["close"]<b["e20"]*.82:return True,"4H >18% below EMA20"
    return False,""

def veto(side,btc,a,b,n,br):
    reasons=[]
    if side=="LONG":
        if btc["regime"]=="RISK_OFF":reasons.append("BTC risk-off")
        if btc["shock_down"]:reasons.append("BTC downside shock")
        if br["status"]=="BEARISH":reasons.append("breadth bearish")
        if a["direction"]=="BEARISH" and a["gap"]<=-6:reasons.append("1H strong bearish")
        if a["e20"]<a["e50"] and a["mh"]<0 and a["rsi"]<45:reasons.append("1H bearish invalidation")
        if a["breakdown"] and a["mh"]<0:reasons.append("1H breakdown")
        if n["bias"]=="NEGATIVE" and n["score"]<=-5:reasons.append("negative catalyst")
    else:
        if btc["regime"]=="RISK_ON":reasons.append("BTC risk-on")
        if btc["shock_up"]:reasons.append("BTC upside shock")
        if br["status"]=="BULLISH":reasons.append("breadth bullish")
        if a["direction"]=="BULLISH" and a["gap"]>=6:reasons.append("1H strong bullish")
        if a["e20"]>a["e50"] and a["mh"]>0 and a["rsi"]>55:reasons.append("1H bullish invalidation")
        if a["breakout"] and a["mh"]>0:reasons.append("1H breakout")
        if n["bias"]=="POSITIVE" and n["score"]>=5:reasons.append("positive catalyst")
    hard=any(x in reasons for x in ("BTC downside shock","BTC upside shock","1H bearish invalidation","1H bullish invalidation","1H breakdown","1H breakout"))
    return hard,reasons

def score(side,d1,h4,h1,btc,br,rel,d,n,macro,fg):
    sc=50;ev=[];risk=[]
    # Keep timeframe contributions bounded; prevents score saturation at 100.
    for tf,w,name in ((d1,.20,"1D"),(h4,.30,"4H"),(h1,.35,"1H")):
        raw=tf["bull"]-tf["bear"]
        if side=="SHORT":
            raw=-raw
        tf_score=clamp(raw/12.0,-1.0,1.0)
        sc += w*30*tf_score
        if (side=="LONG" and tf["direction"]=="BULLISH") or (side=="SHORT" and tf["direction"]=="BEARISH"):
            ev.append(name+" aligned")
        else:
        if br["status"]=="BEARISH":sc+=8
        elif br["status"]=="BULLISH":sc-=8
        if rel is not None:sc+=clamp(-rel,-8,8);ev+=["relative weakness"] if rel<-2 else []
        if d["funding"] is not None:
            if d["funding"]>.0001:sc+=4;ev.append("positive funding")
            elif d["funding"]<-.0005:sc-=5;risk.append("crowded funding")
        if n["bias"]=="NEGATIVE":sc+=10;ev.append("negative catalyst")
        elif n["bias"]=="POSITIVE":sc-=10;risk.append("positive catalyst")
    if macro["bias"]=="RISK_POSITIVE":sc+=4 if side=="LONG" else -3
    elif macro["bias"]=="RISK_NEGATIVE":sc+=4 if side=="SHORT" else -5
    if fg:
        if side=="LONG" and fg[0]>80:sc-=3
        if side=="SHORT" and fg[0]<20:sc-=3
    return int(clamp(round(sc),0,100)),ev,risk

# ========================= EARLY RADAR =========================
def radar(side,a,b,btc,n,rel):
    sc=0;why=[]
    if a["vr"]>=1.8:sc+=20;why.append(f"1H unusual volume ({a['vr']:.2f}x)")
    if side=="LONG":
        if a["r1"]>=2:sc+=15;why.append(f"1H acceleration (+{a['r1']:.2f}%)")
        if a["breakout"]:sc+=20;why.append("fresh 1H breakout")
        if b["structure"] in ("HH_HL","BULLISH_BUILD"):sc+=15;why.append("4H bullish structure")
        if a["mh"]>0:sc+=10;why.append("positive MACD")
        if rel is not None and rel>=2:sc+=8;why.append("relative strength vs BTC")
        if n["bias"]=="POSITIVE":sc+=10;why.append("positive catalyst")
        if btc["regime"] in ("RISK_ON","RECOVERY"):sc+=5
    else:
        if a["r1"]<=-2:sc+=15;why.append(f"1H downside acceleration ({a['r1']:.2f}%)")
        if a["breakdown"]:sc+=20;why.append("fresh 1H breakdown")
        if b["structure"] in ("LH_LL","BEARISH_BUILD"):sc+=15;why.append("4H bearish structure")
        if a["mh"]<0:sc+=10;why.append("negative MACD")
        if rel is not None and rel<=-2:sc+=8;why.append("relative weakness vs BTC")
        if n["bias"]=="NEGATIVE":sc+=10;why.append("negative catalyst")
        if btc["regime"] in ("RISK_OFF","DISTRIBUTION"):sc+=5
    ex,reason=extended(a,b,side)
    return {"score":int(clamp(sc,0,100)),"why":why,"status":"EXTENDED" if ex else "EARLY" if sc>=60 else "NONE","extension":reason}

# ========================= COIN ANALYSIS =========================
def coin(inst,btc_h1,items,macro):
    sym=inst.replace("-USDT","/USDT")
    try:
        d1=analyze(candles(inst,"1D",220))
        h4=analyze(candles(inst,"4H",220))
        h1=analyze(candles(inst,"1H",260))
        # FIX: 15m data ab fetch hota hai (pehle missing tha -> KeyError 'm15')
        m15=analyze(candles(inst,"15m",260))
        if not d1 or not h4 or not h1 or not m15:
            return {"ok":False,"symbol":sym,"reason":"insufficient data"}
        n=asset_news(sym,items); di=deriv(sym.split("/")[0]); rel=rs(h1,btc_h1)
        return {"ok":True,"symbol":sym,"d1":d1,"h4":h4,"h1":h1,"m15":m15,"news":n,"deriv":di,"rel":rel}
    except Exception as e:return {"ok":False,"symbol":sym,"reason":str(e)[:120]}

# ========================= FORMATTING / STATE =========================
def levels(side,p,at):
    dist=(at or p*.01)*1.45
    if side=="LONG":return p-dist,p+dist*1.35,p+dist*2.25,p+dist*3.15
    return p+dist,p-dist*1.35,p-dist*2.25,p-dist*3.15

def confirmed_msg(c,btc,br,macro,fg):
    d=c["deriv"];n=c["news"];i="🟢" if c["side"]=="LONG" else "🔴"
    z=[f"{i} <b>{c['side']} CONFIRMED — V4</b>","━━━━━━━━━━━━━━",
       f"<b>Coin:</b> {html.escape(c['symbol'])}",f"<b>Model Score:</b> {c['score']}/100",
       f"<b>Entry:</b> {price(c['entry'])}",f"<b>SL:</b> {price(c['sl'])}",
       f"<b>TP1:</b> {price(c['tp1'])}",f"<b>TP2:</b> {price(c['tp2'])}",f"<b>TP3:</b> {price(c['tp3'])}",
       "",f"<b>BTC:</b> {btc['regime']}",f"<b>1D:</b> {c['d1']['direction']} | <b>4H:</b> {c['h4']['direction']} | <b>1H:</b> {c['h1']['direction']} | <b>15m:</b> {c['m15']['direction']}",
       f"<b>RSI:</b> {c['h1']['rsi']:.1f} | <b>ADX:</b> {c['h1']['adx']:.1f} | <b>Volume:</b> {c['h1']['vr']:.2f}x",
       f"<b>Breadth:</b> {br['status']} | <b>Macro:</b> {macro['bias']} | <b>News:</b> {n['bias']}"]
    if c["rel"] is not None:z.append(f"<b>6-candle RS vs BTC:</b> {c['rel']:+.2f}%")
    if d["funding"] is not None:z.append(f"<b>Funding:</b> {d['funding']*100:.4f}%")
    z+=["","<b>Evidence:</b>"]+["• "+html.escape(x) for x in c["ev"][:8]]
    if c["risk"]:z+=["","<b>Risks:</b>"]+["• "+html.escape(x) for x in c["risk"][:5]]
    z+=["","🧪 <i>Demo / signal-only. No profit guarantee.</i>"]
    return "\n".join(z)

def early_msg(r):
    i="🟢" if r["side"]=="LONG" else "🔴"
    z=["🚨 <b>POTENTIAL MOVE / EARLY MOMENTUM</b>","━━━━━━━━━━━━━━",
       f"<b>{html.escape(r['symbol'])}</b> {i} <b>{r['side']}</b>",
       f"<b>Radar score:</b> {r['score']}/100",f"<b>Price:</b> {price(r['price'])}","",
       "<b>Why it is on radar:</b>"]+["• "+html.escape(x) for x in r["why"][:7]]
    z+=["","⚠️ <b>Early watch only — wait for confirmation.</b>"]
    return "\n".join(z)

def register(c):
    k=c["symbol"]+":"+c["side"]
    ACTIVE[k]={"symbol":c["symbol"],"side":c["side"],"entry":c["entry"],"sl":c["sl"],
               "tp1":c["tp1"],"tp2":c["tp2"],"tp3":c["tp3"],"created":iso(),
               "best":c["entry"],"worst":c["entry"],"tp1_hit":False,"tp2_hit":False,"tp3_hit":False}
    # FIX: journal mein DataFrame wali heavy fields nahi jaati
    journal("SIGNAL",{x:c[x] for x in ("symbol","side","score","entry","sl","tp1","tp2","tp3","ev","risk")})

def cleanup():
    for k in list(ACTIVE):
        s=ACTIVE[k]
        try:
            df=candles(s["symbol"].replace("/","-"),"1H",5)
            x=df.iloc[-1];hi=float(x.high);lo=float(x.low)
            out=None
            if s["side"]=="LONG":
                s["best"]=max(s["best"],hi);s["worst"]=min(s["worst"],lo)
                if lo<=s["sl"]:out="SL"
                elif hi>=s["tp3"]:out="TP3"
                elif hi>=s["tp2"]:s["tp1_hit"]=s["tp2_hit"]=True
                elif hi>=s["tp1"]:s["tp1_hit"]=True
            else:
                s["best"]=min(s["best"],lo);s["worst"]=max(s["worst"],hi)
                if hi>=s["sl"]:out="SL"
                elif lo<=s["tp3"]:out="TP3"
                elif lo<=s["tp2"]:s["tp1_hit"]=s["tp2_hit"]=True
                elif lo<=s["tp1"]:s["tp1_hit"]=True
            if out:
                journal("OUTCOME",{**s,"outcome":out,"closed":iso()})
                if out=="SL":COOLDOWN[s["symbol"]+":"+s["side"]]=(now()+timedelta(hours=4)).isoformat()
                del ACTIVE[k]
        except:pass

def can_early(sym,side,score):
    k=sym+":"+side;p=EARLY_STATE.get(k)
    if p:
        try:
            if now()-datetime.fromisoformat(p["ts"])<timedelta(hours=2) and score<=p["score"]+3:return False
        except:pass
    EARLY_STATE[k]={"ts":iso(),"score":score};return True

def in_cooldown(key):
    # FIX: cooldown ab waqai expire hoti hai (pehle ek baar lagne ke baad kabhi hatati nahi thi)
    v=COOLDOWN.get(key)
    if not v:return False
    try:
        if now()<datetime.fromisoformat(v):return True
    except:pass
    COOLDOWN.pop(key,None)
    return False

# ========================= SCAN =========================
def scan():
    today=now().strftime("%Y-%m-%d")
    if DAILY["date"]!=today:DAILY.update(date=today,confirmed=0,early=0)
    cleanup()
    items=news();macro=macro_news(items);fgv=fng()
    uni,total=universe()
    b1=analyze(candles("BTC-USDT","1D",220));b4=analyze(candles("BTC-USDT","4H",220));bh=analyze(candles("BTC-USDT","1H",260))
    if not b1 or not b4 or not bh:
        print("BTC unavailable; no signals.");return
    btc=btc_regime(b1,b4,bh,macro)
    rows=[];errors={}
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        fs=[pool.submit(coin,x,bh,items,macro) for x in uni if x!="BTC-USDT"]
        for q in as_completed(fs):
            try:r=q.result()
            except Exception as e:r={"ok":False,"reason":str(e)}
            if r.get("ok"):rows.append(r)
            else:errors[r.get("reason","error")]=errors.get(r.get("reason","error"),0)+1
    br=breadth(rows)
    candidates=[];radars=[]
    for r in rows:
        for side in ("LONG","SHORT"):
            hard,vr=veto(side,btc,r["h1"],r["h4"],r["news"],br)
            sc,ev,risk=score(side,r["d1"],r["h4"],r["h1"],btc,br,r["rel"],r["deriv"],r["news"],macro,fgv)
            ex,er=extended(r["h1"],r["h4"],side)
            ck=r["symbol"]+":"+side
            # Confirmed setups require 4H alignment.
            # A strongly opposite 1D trend is also a veto.
            h4_aligned=(r["h4"]["direction"]=="BULLISH") if side=="LONG" else (r["h4"]["direction"]=="BEARISH")
            d1_conflict=(r["d1"]["direction"]=="BEARISH" and r["d1"]["gap"]<=-6) if side=="LONG" else (r["d1"]["direction"]=="BULLISH" and r["d1"]["gap"]>=6)
            m15d=r.get("m15")
            if side=="LONG":
                oneh=r["h1"]["direction"]=="BULLISH" and r["h1"]["rsi"]>=50
                m15=bool(m15d) and m15d["direction"]=="BULLISH" and m15d["rsi"]>=48
            else:
                oneh=r["h1"]["direction"]=="BEARISH" and r["h1"]["rsi"]<=50
                m15=bool(m15d) and m15d["direction"]=="BEARISH" and m15d["rsi"]<=52

            # 15m is the entry-timing layer, not a replacement for 1H/4H/1D.
            valid=(sc>=67 and not hard and not ex and ck not in ACTIVE and not in_cooldown(ck)
                   and len(risk)<=2 and h4_aligned and not d1_conflict and oneh and m15)
            if valid:
                sl,tp1,tp2,tp3=levels(side,r["h1"]["close"],r["h1"]["atr"])
                candidates.append({"symbol":r["symbol"],"side":side,"score":sc,"entry":r["h1"]["close"],
                    "sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"ev":ev,"risk":risk,"d1":r["d1"],"h4":r["h4"],"h1":r["h1"],
                    "m15":r["m15"],"news":r["news"],"deriv":r["deriv"],"rel":r["rel"]})
            rr=radar(side,r["h1"],r["h4"],btc,r["news"],r["rel"])
            if rr["status"]=="EARLY":
                radars.append({**rr,"symbol":r["symbol"],"side":side,"price":r["h1"]["close"]})
    candidates.sort(key=lambda x:x["score"],reverse=True);radars.sort(key=lambda x:x["score"],reverse=True)
    used=set();sent=0
    for c in candidates:
        if sent>=MAX_CONFIRMED or DAILY["confirmed"]>=DAILY_CONF or c["symbol"] in used:continue
        if c["side"]=="LONG" and btc["regime"]=="RISK_OFF":continue
        if c["side"]=="SHORT" and btc["regime"]=="RISK_ON":continue
        if tg(confirmed_msg(c,btc,br,macro,fgv)):
            register(c);used.add(c["symbol"]);sent+=1;DAILY["confirmed"]+=1
    es=0
    for r in radars:
        if es>=MAX_EARLY or DAILY["early"]>=DAILY_EARLY or r["symbol"] in used:continue
        if not can_early(r["symbol"],r["side"],r["score"]):continue
        if tg(early_msg(r)):
            journal("EARLY",r);es+=1;DAILY["early"]+=1
    print("\n"+"="*65)
    print("V4 AUDIT",iso())
    print("Coins available:",total,"Selected:",len(uni),"Analyzed:",len(rows))
    print("BTC:",btc,"Breadth:",br,"Macro:",macro["bias"])
    print("Early:",len(radars),"Confirmed candidates:",len(candidates),"Sent:",sent)
    print("Daily:",DAILY)
    if candidates:
        print("TOP:",[(x["symbol"],x["side"],x["score"]) for x in candidates[:10]])
    if radars:
        print("EARLY:",[(x["symbol"],x["side"],x["score"]) for x in radars[:10]])
    if errors:print("Errors:",sorted(errors.items(),key=lambda x:x[1],reverse=True)[:8])
    save()

def main():
    load()
    tg("🚀 <b>CRYPTO SIGNAL BOT V4 STARTED</b>\n"
       "Market-first • BTC regime • macro/news • breadth • derivatives • early radar\n"
       "🧪 Signal-only / demo testing — no order execution.")
    while True:
        try:scan()
        except KeyboardInterrupt:break
        except Exception as e:
            print("LOOP ERROR:",repr(e));traceback.print_exc()
            tg("⚠️ <b>V4 runtime error</b>\n<code>"+html.escape(repr(e)[:700])+"</code>")
        print("Sleeping",SCAN_INTERVAL,"seconds...")
        time.sleep(SCAN_INTERVAL)

if __name__=="__main__":main()
