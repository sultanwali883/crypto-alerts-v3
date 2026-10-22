#!/usr/bin/env python3
"""
Early-gainer ALERT BOT (daily/intraday trading) - v3.

Alerts you get on your phone (ntfy.sh and/or Telegram):
  BUY SIGNAL   volume 3x+ and price just starting to climb (rules unchanged), with
               Stop %, Target %, Size %. Uses the LIVE price, not the old candle close.
  TOO LATE     the coin passed the buy rules but already ran >1% since the signal.
               You are told instead of being left in silence. Don't chase it.
  MOVED FAST   volume 3x+ and +10% in the last hour: a move you missed. Watch only.
  WATCH        early interest: volume rising and most of it is BUYING. NOT a buy signal.
  TRIGGER      each WATCH alert carries a buy-trigger price (just above recent resistance). If the
               price reaches it within 6h you get a BUY TRIGGERED alert with stop/target/size.
  HOURLY       every hour: price, profit/loss, distance to stop and target for each open trade.
  STOP/TARGET  detected using every 1-minute candle since entry, so a spike that touched
               your target and came back is still reported (with the time it happened).
  SCORECARD    daily results of all finished trades.
  FLAGS        every coin alert also shows risk flags: Binance SEED / MONITORING tag (if it
               can be read), HIGH VOLATILITY (big 24h range), LOW LIQUIDITY.

ntfy setup: install the "ntfy" app, subscribe to a long random topic name, then
    export NTFY_TOPIC=that-name
Run once: python alert_bot.py        Loop: python alert_bot.py --loop
No Binance key needed. Not financial advice; always use your stop-loss.
"""
import os, sys, json, time, requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BASE = "https://data-api.binance.vision"
STATE, HIST = "state.json", "history.json"
FEE_RT = 0.4            # round-trip fees + slippage, in %
MIN_VOL, COOLDOWN_H, MAX_AGE_H = 3_000_000, 6, 24
WATCH_COOLDOWN_H = 3
MAX_CHASE_PCT = 1.0     # skip a BUY if live price is already this far above the signal price
SKIP = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "AEUR", "USDE", "XUSD", "BFUSD",
        "USD1", "RLUSD", "PAXG", "XAUT", "UST"}


def get(path, **p):
    r = requests.get(BASE + path, params=p, timeout=20)
    r.raise_for_status()
    return r.json()


def tg(msg, title="Crypto signal", prio="high"):
    print(msg, "\n")
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=msg.encode("utf-8"),
                          headers={"Title": title, "Priority": prio}, timeout=15)
        except Exception as e:
            print("ntfy error:", e)
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": msg}, timeout=15)
        except Exception as e:
            print("Telegram error:", e)


def ema(v, n):
    k, e = 2 / (n + 1), v[0]
    for x in v[1:]:
        e = x * k + e * (1 - k)
    return e


def rsi(c, n=14):
    g = l = 0.0
    for i in range(-n, 0):
        d = c[i] - c[i - 1]
        g += max(d, 0); l += max(-d, 0)
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


# Binance risk tags. We try Binance's public (unofficial) product list; if that is blocked or
# has no tag data, we fall back to this manual list. Edit it from Binance announcements.
MANUAL_TAGS = {"FARM": "MONITORING", "HIGH": "MONITORING", "MLN": "MONITORING",
               "RESOLV": "MONITORING", "SYS": "MONITORING", "TRU": "MONITORING",
               "VELODROME": "MONITORING"}
_tags = {"t": 0.0, "map": None}


def binance_tags():
    """Return {BASE: 'SEED'|'MONITORING'} from Binance, or None if it could not be read."""
    if time.time() - _tags["t"] < 6 * 3600: return _tags["map"]
    _tags["t"] = time.time()
    try:
        r = requests.get("https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products",
                         params={"includeEtf": "true"}, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        m = {}
        for p in r.json().get("data") or []:
            txt = " ".join(str(x) for x in (p.get("tags") or [])).lower()
            base = p.get("b")
            if base and "monitor" in txt: m[base] = "MONITORING"
            elif base and "seed" in txt: m.setdefault(base, "SEED")
        if not m: raise ValueError("no tag data in response")
        _tags["map"] = m
    except Exception as e:
        print("Binance tag lookup failed (using manual list):", e)
        _tags["map"] = None
    return _tags["map"]


def flags(r):
    """One line of risk flags for a coin."""
    m = binance_tags()
    tag = (m.get(r["sym"]) if m is not None else MANUAL_TAGS.get(r["sym"]))
    out = []
    if tag == "SEED": out.append("SEED TAG (early-stage, higher risk)")
    elif tag == "MONITORING": out.append("MONITORING TAG (very volatile, delisting risk)")
    elif m is None: out.append("Binance tag unchecked, see the coin page")
    if r["rng24"] >= 15: out.append(f"HIGH VOLATILITY (24h range {r['rng24']:.0f}%)")
    if r["qvol"] < 10_000_000: out.append(f"LOW LIQUIDITY (24h volume ${r['qvol']/1e6:.1f}M)")
    return "Flags: " + " | ".join(out) if out else "Flags: none"


def analyze(t):
    """Classify a coin as BUY / MOVED / WATCH (or None)."""
    sym = t["symbol"]
    raw = get("/api/v3/klines", symbol=sym, interval="15m", limit=100)
    live = float(raw[-1][4])                       # forming candle = current price
    k = raw[:-1]                                   # closed candles only for the signal
    c = [float(x[4]) for x in k]; h = [float(x[2]) for x in k]
    lo = [float(x[3]) for x in k]; v = [float(x[7]) for x in k]
    tb = [float(x[10]) for x in k]                 # taker-buy quote volume
    spike = (sum(v[-4:]) / 4) / ((sum(v[-84:-4]) / 80) or 1)
    chg1h = (c[-1] / c[-5] - 1) * 100
    buyers = sum(tb[-4:]) / (sum(v[-4:]) or 1) * 100
    chg24 = float(t["priceChangePercent"])
    e20, e50, r = ema(c, 20), ema(c, 50), rsi(c)
    atr = sum(max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
              for i in range(-14, 0)) / 14
    sp = max(2 * atr / c[-1] * 100, 1.0)           # stop never tighter than 1%
    trig = max(max(h[-16:]) * 1.001, live * 1.003)  # just above the 4h high
    out = dict(sym=sym[:-4], price=c[-1], live=live, chg24=chg24, chg1h=chg1h,
               spike=spike, buyers=buyers, stop_pct=sp,
               rng24=(float(t["highPrice"]) / float(t["lowPrice"]) - 1) * 100,
               qvol=float(t["quoteVolume"]), trig=trig, trig_pct=(trig / live - 1) * 100)
    if (spike >= 3 and 1.5 <= chg1h <= 10 and c[-1] > e20 > e50 and r <= 80
            and sp <= 6 and 0 <= chg24 <= 20):
        out["kind"] = "BUY"
    elif spike >= 3 and chg1h > 10:
        out["kind"] = "MOVED"
    elif (spike >= 1.8 and buyers >= 58 and 0.5 <= chg1h <= 10
          and -2 <= chg24 <= 25 and c[-1] > e20):
        out["kind"] = "WATCH"
    else:
        out["kind"] = None
    return out


def scan(state, hist):
    now = time.time()
    note = hist.setdefault("notified", {})

    def fresh(key, hours):
        if now - note.get(key, 0) < hours * 3600: return False
        note[key] = now
        return True

    try:
        btc = [float(x[4]) for x in get("/api/v3/klines", symbol="BTCUSDT", interval="15m", limit=20)]
        btc_ok = btc[-1] / btc[-17] - 1 >= -0.02
    except Exception:
        btc_ok = True
    if not btc_ok: print("BTC falling >2% in 4h: no new BUY signals.")

    pool = [t for t in get("/api/v3/ticker/24hr")
            if t["symbol"].endswith("USDT") and t["symbol"][:-4].isascii()
            and t["symbol"][:-4].isalnum() and t["symbol"][:-4] not in SKIP
            and not t["symbol"][:-4].endswith(("UP", "DOWN"))
            and float(t["quoteVolume"]) >= MIN_VOL
            and -3 <= float(t["priceChangePercent"]) <= 60]
    cand = {t["symbol"]: t for t in
            sorted(pool, key=lambda t: -float(t["quoteVolume"]))[:120]
            + sorted(pool, key=lambda t: -float(t["priceChangePercent"]))[:15]}.values()

    def safe(t):
        try: return analyze(t)
        except Exception: return None
    with ThreadPoolExecutor(8) as ex:
        scanned = [r for r in ex.map(safe, cand) if r]
    hits = [r for r in scanned if r["kind"]]
    cnt = lambda n: sum(1 for r in hits if r["kind"] == n)
    print(f"Scanned {len(scanned)}/{len(cand)} coins | BUY {cnt('BUY')} WATCH {cnt('WATCH')} "
          f"MOVED {cnt('MOVED')} | BTC {'ok' if btc_ok else 'falling'}")
    for r in sorted(scanned, key=lambda x: -x["spike"])[:3]:
        print(f"   busiest: {r['sym']} volume {r['spike']:.1f}x, buyers {r['buyers']:.0f}%, "
              f"1h {r['chg1h']:+.1f}%, 24h {r['chg24']:+.1f}%")

    def is_open(sym): return state.get(sym, {}).get("open")

    # ---- BUY (and TOO LATE) ----
    if btc_ok:
        for r in sorted((x for x in hits if x["kind"] == "BUY"), key=lambda x: -x["spike"])[:3]:
            old = state.get(r["sym"])
            if old and (old.get("open") or now - old["t"] < COOLDOWN_H * 3600): continue
            drift = (r["live"] / r["price"] - 1) * 100
            if drift > MAX_CHASE_PCT:
                if fresh("LATE:" + r["sym"], COOLDOWN_H):
                    tg(f"TOO LATE: {r['sym']}/USDT passed the buy rules, but the price is already "
                       f"+{drift:.1f}% above the signal ({r['live']:.6g} now). Not a buy: don't chase. "
                       f"Keep an eye on it only.\n{flags(r)}", "Too late", "default")
                continue
            sp, entry = r["stop_pct"], r["live"]
            stop, target = entry * (1 - sp / 100), entry * (1 + 2 * sp / 100)
            state[r["sym"]] = dict(entry=entry, stop=stop, target=target, t=now,
                                   checked=int(now * 1000), open=True)
            tg(f"BUY SIGNAL: {r['sym']}/USDT\n"
               f"Price now {entry:.6g} | 24h {r['chg24']:+.1f}% | last hour {r['chg1h']:+.1f}%\n"
               f"Volume {r['spike']:.1f}x normal, {r['buyers']:.0f}% of it is buying\n"
               f"{flags(r)}\n"
               f"Stop: -{sp:.1f}% ({stop:.6g})\n"
               f"Target: +{2*sp:.1f}% ({target:.6g})\n"
               f"Size: {min(20, 100/sp):.0f}% of account (stop-out = ~1% loss)\n"
               f"I'll update you hourly and alert the moment stop or target is touched.")

    # ---- MOVED FAST (a move you already missed) ----
    for r in sorted((x for x in hits if x["kind"] == "MOVED"), key=lambda x: -x["chg1h"])[:3]:
        if not is_open(r["sym"]) and fresh("MOVED:" + r["sym"], 6):
            tg(f"MOVED FAST: {r['sym']}/USDT is up {r['chg1h']:+.1f}% in the last hour "
               f"(24h {r['chg24']:+.1f}%), volume {r['spike']:.1f}x normal. Too late to buy; "
               f"watch only.\n{flags(r)}", "Moved fast", "default")

    # ---- WATCH (early interest, NOT a buy signal) ----
    for r in sorted((x for x in hits if x["kind"] == "WATCH"), key=lambda x: -x["spike"])[:3]:
        if not is_open(r["sym"]) and fresh("WATCH:" + r["sym"], WATCH_COOLDOWN_H):
            fl, trig_line = flags(r), ""
            trigs = hist.setdefault("triggers", {})
            if r["trig_pct"] <= 6 and (r["sym"] in trigs or len(trigs) < 5):
                trigs[r["sym"]] = dict(price=r["trig"], sp=r["stop_pct"], t=now,
                                       checked=int(now * 1000), flags=fl, chg24=r["chg24"])
                trig_line = (f"\nBuy trigger: {r['trig']:.6g} ({r['trig_pct']:+.1f}% from now). "
                             f"If the price reaches it, I'll send a BUY alert (valid 6h).")
            tg(f"WATCH: {r['sym']}/USDT - buyers are showing interest.\n"
               f"Volume {r['spike']:.1f}x normal, {r['buyers']:.0f}% of it is buying, "
               f"{r['chg1h']:+.1f}% in the last hour (24h {r['chg24']:+.1f}%).\n"
               f"{fl}{trig_line}\nNOT a buy signal yet. Just keep an eye on it.", "Watch", "default")


def check_open(state, hist):
    """Use every 1-minute candle since entry, so brief touches of stop/target are caught."""
    now = time.time()
    for sym, p in state.items():
        if not p.get("open"): continue
        try:
            k = get("/api/v3/klines", symbol=sym + "USDT", interval="1m",
                    startTime=p.get("checked") or int(p["t"] * 1000), limit=1000)
        except Exception:
            continue
        why = px = ts = None
        for x in k:
            if float(x[3]) <= p["stop"]:
                why, px, ts = "STOP hit", p["stop"], x[0]; break       # stop first if same minute
            if float(x[2]) >= p["target"]:
                why, px, ts = "TARGET hit", p["target"], x[0]; break
        if k: p["checked"] = k[-1][0]
        if not why and now - p["t"] > MAX_AGE_H * 3600:
            why, px = "24h passed, close the trade", float(k[-1][4]) if k else p["entry"]
        if why:
            p["open"] = False
            pnl = (px / p["entry"] - 1) * 100
            hist["trades"].append(dict(sym=sym, entry=p["entry"], exit=px, t=now,
                                       pnl=pnl - FEE_RT, kind=why.split()[0],
                                       src=p.get("src", "signal")))
            when = f" at {datetime.fromtimestamp(ts/1000, timezone.utc):%H:%M} UTC" if ts else ""
            tg(f"SELL {sym}/USDT: {why}{when}\n"
               f"Entry {p['entry']:.6g} -> {px:.6g} ({pnl:+.1f}%)\n"
               f"If your OCO order is set, it has already filled. Otherwise close it now.",
               "Sell", "urgent")


def check_triggers(state, hist):
    """Fire a BUY alert when a watched coin's price reaches its trigger (checks every 1m candle)."""
    now = time.time()
    trigs = hist.setdefault("triggers", {})
    for sym, g in list(trigs.items()):
        if now - g["t"] > 6 * 3600 or state.get(sym, {}).get("open"):
            del trigs[sym]; continue
        try:
            k = get("/api/v3/klines", symbol=sym + "USDT", interval="1m",
                    startTime=g["checked"], limit=1000)
        except Exception:
            continue
        hit = next((x for x in k if float(x[2]) >= g["price"]), None)
        if k: g["checked"] = k[-1][0]
        if not hit: continue
        del trigs[sym]
        live = float(k[-1][4])
        when = f"{datetime.fromtimestamp(hit[0]/1000, timezone.utc):%H:%M} UTC"
        if live > g["price"] * (1 + MAX_CHASE_PCT / 100):
            tg(f"TOO LATE: {sym}/USDT reached its trigger {g['price']:.6g} at {when}, but it is "
               f"already {live:.6g} now. Not a buy: don't chase.", "Too late", "default")
            continue
        sp, entry = g["sp"], live
        stop, target = entry * (1 - sp / 100), entry * (1 + 2 * sp / 100)
        state[sym] = dict(entry=entry, stop=stop, target=target, t=now, checked=int(now * 1000),
                          open=True, src="trigger")
        tg(f"BUY TRIGGERED: {sym}/USDT reached {g['price']:.6g} (the level from the WATCH alert)\n"
           f"Price now {entry:.6g} | 24h {g['chg24']:+.1f}%\n{g['flags']}\n"
           f"Stop: -{sp:.1f}% ({stop:.6g})\n"
           f"Target: +{2*sp:.1f}% ({target:.6g})\n"
           f"Size: {min(20, 100/sp):.0f}% of account (stop-out = ~1% loss)\n"
           f"I'll update you hourly and alert the moment stop or target is touched.")


def hourly_status(state, hist):
    key = datetime.now(timezone.utc).strftime("%Y-%m-%d %H")
    if hist.get("last_status") == key: return
    hist["last_status"] = key
    lines = []
    for sym, p in state.items():
        if not p.get("open"): continue
        try:
            price = float(get("/api/v3/ticker/price", symbol=sym + "USDT")["price"])
        except Exception:
            continue
        lines.append(f"{sym}: {price:.6g} ({(price/p['entry']-1)*100:+.1f}%) | "
                     f"stop {(p['stop']/price-1)*100:.1f}% away | "
                     f"target {(p['target']/price-1)*100:+.1f}% away | "
                     f"open {(time.time()-p['t'])/3600:.1f}h")
    if lines:
        tg("HOURLY UPDATE (open trades)\n" + "\n".join(lines), "Hourly update", "default")


def summarize(trades):
    if not trades: return "no closed trades"
    p = [x["pnl"] for x in trades]
    w = [x for x in p if x > 0]; l = [x for x in p if x <= 0]
    pf = sum(w) / abs(sum(l)) if l else 9.99
    k = lambda n: sum(1 for x in trades if x["kind"] == n)
    return (f"{len(p)} trades | win {len(w)/len(p)*100:.0f}% | avg {sum(p)/len(p):+.2f}% "
            f"(after fees) | profit factor {pf:.2f} | targets {k('TARGET')}, "
            f"stops {k('STOP')}, time-exits {k('24h')} | worst {min(p):.1f}%")


def daily_scorecard(hist):
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour < 19 or hist.get("last_summary") == today: return
    hist["last_summary"] = today
    if not hist["trades"]: return
    wk = [x for x in hist["trades"] if time.time() - x["t"] < 7 * 86400]
    sg = [x for x in wk if x.get("src") != "trigger"]
    tr = [x for x in wk if x.get("src") == "trigger"]
    tg(f"SCORECARD {today}\nBUY signals, 7 days: {summarize(sg)}\n"
       f"Triggered buys, 7 days: {summarize(tr)}\nAll time: {summarize(hist['trades'])}\n"
       f"(Profit factor above 1.3 over 50+ trades = worth real money. Below 1 = stop.)",
       "Scorecard", "default")


def run():
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    hist = json.load(open(HIST)) if os.path.exists(HIST) else {}
    hist.setdefault("trades", [])
    for step in (lambda: check_open(state, hist), lambda: check_triggers(state, hist),
                 lambda: hourly_status(state, hist),
                 lambda: scan(state, hist), lambda: daily_scorecard(hist)):
        try:
            step()
        except Exception as e:
            print("Run error:", e)
    json.dump(state, open(STATE, "w"))
    json.dump(hist, open(HIST, "w"))
    print(datetime.now(timezone.utc).strftime("Checked %Y-%m-%d %H:%M UTC"))


if __name__ == "__main__":
    if "--loop" in sys.argv:
        tg("Scanner started. It checks every 3 minutes and will send WATCH, BUY, hourly and "
           "SELL alerts here. If you see this, your phone alerts work.", "Scanner started", "low")
        while True:
            run(); time.sleep(int(os.getenv("LOOP_SECONDS", "180")))
    else:
        run()
