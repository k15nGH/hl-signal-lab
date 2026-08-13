#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HL Signal Lab — Hyperliquid 自動売買bot スターター v0.1
========================================================
シグナルラボで検証した設定（hl_bot_config.json）をそのまま動かす最小構成。

安全設計:
  - 既定は MODE=paper（発注しない。判定とサイズ計算をDiscordに報告するだけ）
  - 1銘柄×1戦略×同時1ポジションのみ
  - エントリーと同時に取引所側TP/SLトリガーを設置（bot停止中も損切りが生きる）
  - 1トレードのリスク＝口座の riskPctPerTrade %（損切り幅から逆算してサイズ決定）
  - 日次損失が dailyLossStopPct % に達したら当日停止（キルスイッチ）

必須の順序: paper（2週間）→ testnet → 本番最小サイズ → 段階増額。
秘密鍵は .env のみに置き、絶対にgitへコミットしないこと（.gitignore設定済み）。
"""
import os, json, time, math, logging, datetime as dt
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ====== 設定読込 ======
CFG_PATH = os.environ.get("HL_CONFIG", "hl_bot_config.json")
with open(CFG_PATH, encoding="utf-8") as f:
    CFG = json.load(f)

MODE       = os.environ.get("MODE", CFG.get("mode", "paper")).lower()   # paper | live
TESTNET    = os.environ.get("TESTNET", "0") == "1"
API_URL    = "https://api.hyperliquid-testnet.xyz" if TESTNET else "https://api.hyperliquid.xyz"
ADDRESS    = os.environ.get("HL_ACCOUNT_ADDRESS", "")     # 本体ウォレットの公開アドレス
AGENT_KEY  = os.environ.get("HL_AGENT_PRIVATE_KEY", "")   # APIウォレット(エージェント)の秘密鍵
WEBHOOK    = os.environ.get("DISCORD_WEBHOOK", "")

COIN     = CFG["coin"]
IV       = CFG["interval"]
ST       = CFG["strategy"]
IV_SEC   = {"1m":60,"5m":300,"15m":900,"1h":3600,"4h":14400,"1d":86400}[IV]
RISK_PCT = float(CFG.get("risk", {}).get("riskPctPerTrade", 0.5))
DAY_STOP = float(CFG.get("risk", {}).get("dailyLossStopPct", 3))
LEV      = int(CFG.get("leverage", 2))

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("hl_bot.log", encoding="utf-8"), logging.StreamHandler()])
log = logging.getLogger("hlbot")

# ====== Discord ======
def discord(title, desc, color=0xF5B84B):
    if not WEBHOOK: return
    try:
        requests.post(WEBHOOK, json={"username":"HL Bot",
            "embeds":[{"title":title,"description":desc,"color":color,
                       "timestamp":dt.datetime.utcnow().isoformat()+"Z"}]}, timeout=10)
    except Exception as e:
        log.warning(f"discord送信失敗: {e}")

# ====== 市場データ（公開info・キー不要） ======
def info_post(body, tries=3):
    for k in range(tries):
        r = requests.post(API_URL + "/info", json=body, timeout=15)
        if r.status_code == 429:
            time.sleep(2 * (k + 1)); continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("info API 429が続いています")

def candles(coin, iv, count):
    end = int(time.time() * 1000)
    start = end - IV_SEC * 1000 * (count + 2)
    raw = info_post({"type":"candleSnapshot",
                     "req":{"coin":coin,"interval":iv,"startTime":start,"endTime":end}})
    arr = [{"t":int(k["t"]), "o":float(k["o"]), "h":float(k["h"]),
            "l":float(k["l"]), "c":float(k["c"]), "v":float(k["v"])} for k in raw]
    arr.sort(key=lambda x: x["t"])
    return arr

def sz_decimals(coin):
    meta = info_post({"type":"meta"})
    for u in meta["universe"]:
        if u["name"] == coin:
            return int(u.get("szDecimals", 3))
    return 3

# ====== 指標 ======
def sma(a, p):
    out=[math.nan]*len(a); s=0.0
    for i,v in enumerate(a):
        s+=v
        if i>=p: s-=a[i-p]
        if i>=p-1: out[i]=s/p
    return out

def ema(a, p):
    out=[math.nan]*len(a)
    if not a: return out
    k=2/(p+1); e=a[0]; out[0]=e
    for i in range(1,len(a)):
        e=a[i]*k+e*(1-k); out[i]=e
    return out

def rsi(a, p):
    out=[math.nan]*len(a)
    if len(a)<=p: return out
    g=l=0.0
    for i in range(1,p+1):
        d=a[i]-a[i-1]
        if d>0: g+=d
        else: l-=d
    g/=p; l/=p
    out[p]=100-100/(1+(1e9 if l==0 else g/l))
    for i in range(p+1,len(a)):
        d=a[i]-a[i-1]
        g=(g*(p-1)+max(d,0))/p; l=(l*(p-1)+max(-d,0))/p
        out[i]=100-100/(1+(1e9 if l==0 else g/l))
    return out

def atr(arr, p):
    n=len(arr); out=[math.nan]*n
    if n<=p: return out
    def tr(i):
        if i==0: return arr[0]["h"]-arr[0]["l"]
        return max(arr[i]["h"]-arr[i]["l"],
                   abs(arr[i]["h"]-arr[i-1]["c"]), abs(arr[i]["l"]-arr[i-1]["c"]))
    s=sum(tr(i) for i in range(1,p+1)); a=s/p; out[p]=a
    for i in range(p+1,n):
        a=(a*(p-1)+tr(i))/p; out[i]=a
    return out

# ====== シグナル（ツールと同一ロジック） ======
def session_ok(t_ms):
    ses = CFG.get("session","none")
    if ses=="none": return True
    d = dt.datetime.utcfromtimestamp(t_ms/1000); h=d.hour; w=d.weekday()  # 月=0
    if ses=="us":     return 13<=h<21
    if ses=="asia":   return 0<=h<8
    if ses=="eu":     return 7<=h<15
    if ses=="monday": return (w==6 and h>=23) or (w==0 and h<23)
    return True

def signal(arr, i, ind):
    """iはクローズ済み足のインデックス。+1=ロング基点 / -1=ショート基点 / 0=なし"""
    c=[k["c"] for k in arr]; o=[k["o"] for k in arr]
    h=[k["h"] for k in arr]; l=[k["l"] for k in arr]; v=[k["v"] for k in arr]
    cond=ST["cond"]
    if cond=="momentum":
        K=ST["K"]; ch=(c[i]-c[i-K])/c[i-K]*100
        return 1 if ch>=ST["X"] else (-1 if ch<=-ST["X"] else 0)
    if cond=="emacross":
        eF,eS=ind["eF"],ind["eS"]
        if math.isnan(eF[i-1]) or math.isnan(eS[i-1]): return 0
        if eF[i]>eS[i] and eF[i-1]<=eS[i-1]: return 1
        if eF[i]<eS[i] and eF[i-1]>=eS[i-1]: return -1
        return 0
    if cond=="donchian":
        N=ST["DN"]
        hh=max(h[i-N:i]); ll=min(l[i-N:i])
        return 1 if c[i]>hh else (-1 if c[i]<ll else 0)
    if cond=="volbreak":
        rg=h[i-1]-l[i-1]
        if rg<=0: return 0
        k=ST["VBk"]
        if c[i]>c[i-1]+k*rg: return 1
        if c[i]<c[i-1]-k*rg: return -1
        return 0
    if cond=="boll":
        mid,up,lo=ind["mid"],ind["up"],ind["lo"]
        if math.isnan(up[i]) or math.isnan(up[i-1]): return 0
        if c[i]>up[i] and c[i-1]<=up[i-1]: return 1
        if c[i]<lo[i] and c[i-1]>=lo[i-1]: return -1
        return 0
    if cond=="rsi":
        r=ind["rsi"]
        if math.isnan(r[i]): return 0
        if ST.get("rsiMode")=="cross50":
            if math.isnan(r[i-1]): return 0
            if r[i]>50 and r[i-1]<=50: return 1
            if r[i]<50 and r[i-1]>=50: return -1
            return 0
        if r[i]<=ST["rsiLo"]: return 1
        if r[i]>=ST["rsiHi"]: return -1
        return 0
    if cond=="streak":
        N=ST["N"]; up=all(c[j]>c[j-1] for j in range(i-N+1,i+1))
        dn=all(c[j]<c[j-1] for j in range(i-N+1,i+1))
        return 1 if up else (-1 if dn else 0)
    if cond=="volspike":
        M=ST["Vm"]
        if i<M+1: return 0
        av=sum(v[i-M:i])/M
        if av<=0 or v[i]<ST["Vk"]*av: return 0
        return 1 if c[i]>o[i] else (-1 if c[i]<o[i] else 0)
    return 0

def build_ind(arr):
    c=[k["c"] for k in arr]
    ind={}
    if ST["cond"]=="emacross": ind["eF"]=ema(c,ST["F"]); ind["eS"]=ema(c,ST["S"])
    if ST["cond"]=="rsi":      ind["rsi"]=rsi(c,ST["rsiP"])
    if ST["cond"]=="boll":
        P=ST["BP"]; K=ST["BK"]; mid=sma(c,P)
        up=[math.nan]*len(c); lo=[math.nan]*len(c)
        for i in range(P-1,len(c)):
            m=mid[i]; var=sum((c[j]-m)**2 for j in range(i-P+1,i+1))/P
            sd=math.sqrt(var); up[i]=m+K*sd; lo[i]=m-K*sd
        ind["mid"]=mid; ind["up"]=up; ind["lo"]=lo
    flt=CFG.get("filter","none")
    if flt=="sma200": ind["f"]=sma(c,200)
    elif flt=="sma50": ind["f"]=sma(c,50)
    st=CFG.get("stop",{})
    if st.get("unit")=="atr": ind["atr"]=atr(arr, int(st.get("atrP",14)))
    return ind

# ====== 発注（liveのみ・APIウォレット署名） ======
EXCHANGE=None; INFO=None
def init_exchange():
    global EXCHANGE, INFO
    from hyperliquid.info import Info
    from hyperliquid.exchange import Exchange
    import eth_account
    INFO = Info(API_URL, skip_ws=True)
    wallet = eth_account.Account.from_key(AGENT_KEY)
    # account_address=本体アドレス、署名はエージェントキー（出金不可のAPIウォレット）
    EXCHANGE = Exchange(wallet, API_URL, account_address=ADDRESS)
    try:
        EXCHANGE.update_leverage(LEV, COIN, is_cross=True)
    except Exception as e:
        log.warning(f"レバレッジ設定スキップ: {e}")

def account_value():
    st = info_post({"type":"clearinghouseState","user":ADDRESS})
    return float(st["marginSummary"]["accountValue"])

def has_open_position():
    st = info_post({"type":"clearinghouseState","user":ADDRESS})
    for ap in st.get("assetPositions", []):
        p = ap.get("position", {})
        if p.get("coin")==COIN and abs(float(p.get("szi","0")))>0:
            return True, p
    return False, None

def round_sz(sz, dec): return math.floor(sz*10**dec)/10**dec

def place_bracket(is_buy, sz, sl_px, tp_px):
    """成行エントリー＋取引所側SL/TPトリガー（reduce_only）を設置。
    ※SDKの書式は hyperliquid-python-sdk の examples/basic_tpsl.py 準拠。実行前に必ず確認。"""
    res = EXCHANGE.market_open(COIN, is_buy, sz, None, 0.01)
    log.info(f"entry result: {res}")
    close_side = not is_buy
    if sl_px:
        EXCHANGE.order(COIN, close_side, sz, sl_px,
            {"trigger":{"triggerPx":sl_px,"isMarket":True,"tpsl":"sl"}}, reduce_only=True)
    if tp_px:
        EXCHANGE.order(COIN, close_side, sz, tp_px,
            {"trigger":{"triggerPx":tp_px,"isMarket":True,"tpsl":"tp"}}, reduce_only=True)
    return res

# ====== メインループ ======
def px_round(p):
    # Hyperliquidの価格は有効5桁（整数部が大きい場合は整数）目安。安全側の丸め。
    if p<=0: return p
    import decimal
    q = max(0, 5-len(str(int(p))))
    return float(round(p, q))

def main():
    assert ADDRESS.startswith("0x") and len(ADDRESS)==42, "HL_ACCOUNT_ADDRESS を .env に設定してください"
    if MODE=="live":
        assert AGENT_KEY, "liveモードには HL_AGENT_PRIVATE_KEY（APIウォレット鍵）が必要です"
        init_exchange()
    st=CFG.get("stop",{}); unit=st.get("unit","pct"); slV=float(st.get("sl",0)); tpV=float(st.get("tp",0))
    dec = sz_decimals(COIN)
    warm = 260  # SMA200等を賄う十分な本数
    day0 = dt.date.today(); day_eq0 = account_value() if ADDRESS else 0.0
    halted=False; pending=None; last_bar=0
    discord("🤖 bot起動", f"{COIN} {IV} / 条件 {ST['cond']} / MODE={MODE} / レバ{LEV}x / リスク{RISK_PCT}%/回", 0x2FD48C)
    log.info(f"起動 MODE={MODE} {COIN} {IV} {ST}")

    while True:
        try:
            # 日次キルスイッチ
            if dt.date.today()!=day0:
                day0=dt.date.today(); day_eq0=account_value(); halted=False
            if not halted and day_eq0>0:
                eq=account_value()
                if (day_eq0-eq)/day_eq0*100 >= DAY_STOP:
                    halted=True
                    discord("🛑 日次損失上限に到達", f"本日の新規エントリーを停止（-{DAY_STOP}%）", 0xFF5C5C)

            arr = candles(COIN, IV, warm+10)
            # 最後の足は形成中の可能性があるため、確定足のみ使う
            now_ms=int(time.time()*1000)
            while arr and arr[-1]["t"]+IV_SEC*1000 > now_ms: arr.pop()
            if len(arr)<warm:
                time.sleep(10); continue
            i=len(arr)-1
            if arr[i]["t"]==last_bar:
                time.sleep(5); continue
            last_bar=arr[i]["t"]
            ind=build_ind(arr)

            # 待機エントリーの執行
            if pending and arr[i]["t"]>=pending["due"]:
                s=pending["s"]; pending=None
                do_entry(arr,i,ind,s,unit,slV,tpV,dec,halted)

            # 新規シグナル判定（確定足）
            s=signal(arr,i,ind)
            if s and CFG.get("trend")=="counter": s=-s
            if s and CFG.get("side")=="long" and s<0: s=0
            if s and CFG.get("side")=="short" and s>0: s=0
            if s and not session_ok(arr[i]["t"]): s=0
            if s and "f" in ind:
                fv=ind["f"][i]
                if math.isnan(fv) or (s>0 and arr[i]["c"]<fv) or (s<0 and arr[i]["c"]>fv): s=0
            if s:
                w=int(CFG.get("wait",0))
                if w>0:
                    pending={"s":s,"due":arr[i]["t"]+IV_SEC*1000*w}
                    log.info(f"シグナル検知→{w}本待機 due={pending['due']}")
                else:
                    do_entry(arr,i,ind,s,unit,slV,tpV,dec,halted)
        except Exception as e:
            log.exception("loop error")
            discord("⚠ botエラー", str(e)[:500], 0xFF5C5C)
            time.sleep(30)
        time.sleep(10)

def do_entry(arr,i,ind,s,unit,slV,tpV,dec,halted):
    if halted:
        log.info("キルスイッチ作動中のためスキップ"); return
    opened,_=has_open_position()
    if opened:
        log.info("既存ポジションありスキップ"); return
    px=arr[i]["c"]
    if unit=="atr":
        a=ind.get("atr",[math.nan]*len(arr))[i]
        if math.isnan(a) or a<=0: return
        sl_d = slV*a/px if slV>0 else 0
        tp_d = tpV*a/px if tpV>0 else 0
    else:
        sl_d = slV/100 if slV>0 else 0
        tp_d = tpV/100 if tpV>0 else 0
    if sl_d<=0:
        log.info("損切りなし設定のためエントリー拒否（botは必ずSL必須）")
        discord("⏸ エントリー見送り","SL=0の設定はbotでは許可していません（設定を見直してください）",0xF5B84B)
        return
    av=account_value()
    risk_usd = av*RISK_PCT/100
    sz = round_sz(risk_usd/(sl_d*px), dec)
    notional = sz*px
    if sz<=0 or notional<10:
        log.info(f"サイズ不足 sz={sz} notional={notional:.2f}"); return
    if notional > av*LEV*0.9:
        sz = round_sz(av*LEV*0.9/px, dec)
    is_buy = s>0
    sl_px = px_round(px*(1-sl_d) if is_buy else px*(1+sl_d))
    tp_px = px_round(px*(1+tp_d) if is_buy else px*(1-tp_d)) if tp_d>0 else None
    side = "🟢ロング" if is_buy else "🔴ショート"
    desc = (f"{COIN} {side}\nエントリー(成行想定): {px}\nサイズ: {sz}（約${notional:,.0f}・実効レバ{notional/av:.1f}x）\n"
            f"損切り: {sl_px}\n利確: {tp_px or '—（時間/裁量決済）'}\nリスク: 口座の{RISK_PCT}%")
    if MODE=="paper":
        log.info(f"[PAPER] {desc}")
        discord("📝 PAPERエントリー", desc, 0x8fb4d6)
        return
    res = place_bracket(is_buy, sz, sl_px, tp_px)
    discord("✅ エントリー執行", desc+f"\n\napi応答: {str(res)[:300]}", 0x2FD48C)

if __name__=="__main__":
    main()
