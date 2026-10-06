#!/usr/bin/env python3
"""
CS2 Polymarket-overvågning v5.1 (GitHub Actions) - "mistankescore", fokus på lave ligaer

I stedet for faste regler giver botten hvert hold i hver kamp en MISTANKESCORE.
Pengene vægtes efter hvor mistænkelige de ser ud:
  * Pris:   penge på underdog (lav pris) tæller meget, penge på en klar favorit
            midt i kampen (typisk folk der følger streamen) tæller lidt.
  * Wallet: nye konti tæller dobbelt, store professionelle konti tæller mindre.
  * Klynge: flere nye konti på samme hold giver ekstra vægt.
Det vægtede beløb sammenlignes med hvad der NORMALT handles på den type marked
(median af andre tier 2-3-markeder). Top-kampe springes over.

Alarm:  🟠 score >= 1   (mistænkeligt)
        🔴 score >= 2   (meget mistænkeligt, pinger Analytiker)
        👁️ overvågningsliste (altid)
"""
import os
import json
import time
import statistics
from datetime import datetime, timezone, timedelta

import requests

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"

# ---------------- Indstillinger ----------------
CHECKS_PER_RUN = 20           # tjek pr. GitHub-kørsel (ca. ét i minuttet, ~20 min)
                              # GitHub starter kun en ny kørsel hvert 15.-20. minut,
                              # så lange kørsler giver næsten konstant overvågning
SECONDS_BETWEEN = 60
HOURS_BEFORE = 2              # tjek kampe der starter inden for så mange timer
HOURS_AFTER = 6               # ... og kampe der startede for højst så mange timer siden
BASELINE_HOURS = 96           # spillede kampe fra de sidste 96 t bruges til at beregne "normal" volumen

SCORE_ORANGE = 1.0            # 🟠
SCORE_RED = 2.0               # 🔴 (ping)
MIN_SUSPICIOUS_USD = 1000     # mindste vægtede beløb før der overhovedet kan komme alarm
MIN_BASELINE_USD = 2000       # "normal" volumen sættes aldrig lavere end dette
LOOKUP_MIN_USD = 300          # wallets med mindre end dette på et hold slås ikke op

MAX_MARKET_VOLUME = 150000    # større markeder = top-kampe -> kun overvågningsliste
LOW_TIER_MAX_MEDIAN = 30000   # turneringer hvis kampe normalt omsætter mere end dette = ikke lav liga
TIER1_KEYWORDS = [
    "major", "iem ", "blast", "esl pro league", "pgl ", "esports world cup",
    "starladder", "thunderpick world", "fissure", "betboom dacha", "perfect world",
]
WATCHLIST = [
    "0xe53f33f58574723549c8d32a06bb03c592e82f56",
]
WATCH_STEP_USD = 1000

STATE_FILE = "state.json"
MAX_STATE = 3000
# -----------------------------------------------

WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
ROLE_ID = os.environ.get("DISCORD_ROLE_ID", "").strip() or "1556578221695434773"
session = requests.Session()
session.headers["User-Agent"] = "cs2-monitor/5.1"
wallet_cache = {}
WATCH = {w.lower() for w in WATCHLIST}


# ---------- hjælpefunktioner ----------
def get(url, params=None):
    for attempt in range(3):
        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            print(f"Fejl ved {url}: {e}")
            time.sleep(2)
    return None


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("alerted", [])
    s.pop("last_run", None)
    return s


def save_state(state):
    state["alerted"] = state["alerted"][-MAX_STATE:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def send_discord(embed, ping=False):
    print(f"\n=== {embed.get('title')} ===")
    if not WEBHOOK.startswith("https://"):
        print("(Ingen webhook sat - besked ikke sendt)")
        return
    payload = {"username": "Match Watch", "embeds": [embed],
               "allowed_mentions": {"roles": [ROLE_ID] if ROLE_ID else []}}
    if ping and ROLE_ID:
        payload["content"] = f"<@&{ROLE_ID}>"
    for _ in range(2):
        try:
            r = session.post(WEBHOOK, json=payload, timeout=10)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                continue
            break
        except requests.RequestException as e:
            print(f"Discord-fejl: {e}")
            break
    time.sleep(1)


def parse_time(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None


def short(w):
    return f"{w[:6]}…{w[-4:]}"


def market_type(question):
    q = (question or "").lower()
    if "map" in q:
        return "map"
    if any(k in q for k in ("o/u", "total", "handicap", "spread")):
        return "other"
    return "match"


def tournament_key(event):
    t = event.get("title") or ""
    suffix = t.split(" - ", 1)[1] if " - " in t else t
    return " ".join(suffix.lower().split()[:2])


def is_top_match(event, volume):
    t = (event.get("title") or "").lower()
    return volume > MAX_MARKET_VOLUME or any(k in t for k in TIER1_KEYWORDS)


def wallet_market_count(wallet):
    if wallet not in wallet_cache:
        trades = get(f"{DATA}/trades", {"user": wallet, "limit": 500}) or []
        wallet_cache[wallet] = len({t.get("conditionId") for t in trades})
    return wallet_cache[wallet]


# ---------- vægte ----------
def price_weight(price, live):
    """Penge på underdog er mistænkelige. Penge på klar favorit under kampen er normale."""
    if price < 0.35:
        return 2.0
    if price < 0.50:
        return 1.5
    if price < 0.70:
        return 1.0
    return 0.3 if live else 0.6


def wallet_weight(n_markets):
    if n_markets is None:
        return 1.0
    if n_markets <= 2:
        return 2.0
    if n_markets <= 10:
        return 1.3
    if n_markets <= 60:
        return 1.0
    return 0.5   # meget aktive konti (pro-bettere / market makers)


# ---------- markeder ----------
def fetch_events(closed, max_pages):
    out = []
    for page in range(max_pages):
        params = {"tag_slug": "counter-strike-2", "closed": "true" if closed else "false",
                  "limit": 100, "offset": page * 100}
        if closed:
            params.update({"order": "startTime", "ascending": "false"})
        events = get(f"{GAMMA}/events", params)
        if not events:
            break
        out.extend(e for e in events if (e.get("title") or "").startswith("Counter-Strike:"))
        if len(events) < 100:
            break
    return out


def scan_markets():
    """Returnerer (markeder der skal tjekkes, normal-volumen).
    Normal-volumen beregnes ud fra kampe der ER spillet (færdige eller i gang),
    så kommende kampe uden handel ikke trækker gennemsnittet ned."""
    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(hours=HOURS_AFTER), now + timedelta(hours=HOURS_BEFORE)
    blo = now - timedelta(hours=BASELINE_HOURS)
    check, base, tourn = [], {"match": [], "map": [], "other": []}, {}

    def add_baseline(e, start):
        if not (blo <= start <= now):
            return
        for m in e.get("markets") or []:
            vol = float(m.get("volumeNum") or m.get("volume") or 0)
            if vol <= 0:
                continue
            mtype = market_type(m.get("question"))
            if not is_top_match(e, vol):
                base[mtype].append(vol)
            if mtype == "match":
                tourn.setdefault(tournament_key(e), []).append(vol)

    for e in fetch_events(closed=True, max_pages=3):
        start = parse_time(e.get("startTime"))
        if start:
            add_baseline(e, start)

    for e in fetch_events(closed=False, max_pages=30):
        start = parse_time(e.get("startTime"))
        if not start:
            continue
        add_baseline(e, start)
        if lo <= start <= hi:
            for m in e.get("markets") or []:
                if not m.get("conditionId"):
                    continue
                m["_event"], m["_start"] = e, start
                m["_vol"] = float(m.get("volumeNum") or m.get("volume") or 0)
                m["_type"] = market_type(m.get("question"))
                check.append(m)

    normal = {k: max(MIN_BASELINE_USD, statistics.median(v)) if v else MIN_BASELINE_USD
              for k, v in base.items()}
    tmed = {k: statistics.median(v) for k, v in tourn.items() if len(v) >= 3}
    # drop kampe fra turneringer der normalt omsætter meget (= ikke lave ligaer)
    low = [m for m in check if tmed.get(tournament_key(m["_event"]), 0) <= LOW_TIER_MAX_MEDIAN]
    normal["_tourn"] = tmed
    return low, normal


# ---------- analyse ----------
def analyse_market(m, normal, state):
    e, start, volume, mtype = m["_event"], m["_start"], m["_vol"], m["_type"]
    cid = m["conditionId"]
    top = is_top_match(e, volume)
    trades = get(f"{DATA}/trades", {"market": cid, "limit": 500}) or []
    start_ts = int(start.timestamp())

    # saml køb pr. (wallet, udfald)
    pos = {}
    for t in trades:
        if t.get("side") != "BUY" or not t.get("proxyWallet"):
            continue
        try:
            size, price = float(t["size"]), float(t["price"])
        except (KeyError, TypeError, ValueError):
            continue
        ts = int(t.get("timestamp") or 0)
        key = (t["proxyWallet"].lower(), t.get("outcome"))
        p = pos.setdefault(key, {"usd": 0.0, "shares": 0.0, "wusd_price": 0.0, "live_usd": 0.0})
        usd = size * price
        p["usd"] += usd
        p["shares"] += size
        p["wusd_price"] += usd * price_weight(price, ts >= start_ts)
        if ts >= start_ts:
            p["live_usd"] += usd

    match = (e.get("title") or "").replace("Counter-Strike: ", "")
    market_name = (m.get("question") or "").replace("Counter-Strike: ", "")
    link = f"https://polymarket.com/event/{e.get('slug')}"
    start_txt = f"<t:{start_ts}:R>"
    status = "🔒 Marked lukket" if m.get("closed") else ("🔴 LIVE" if time.time() >= start_ts else "⏳ Før kampstart")
    open_link = {"name": "\u200b", "value": f"**[➜ Åbn kampen på Polymarket]({link})**", "inline": False}

    # overvågningsliste (også i top-kampe)
    for (wallet, outcome), p in pos.items():
        if wallet not in WATCH:
            continue
        step = int(p["usd"] // WATCH_STEP_USD)
        wkey = f"watch|{cid}|{wallet}|{outcome}|{step}"
        if wkey in state["alerted"]:
            continue
        avg = p["usd"] / p["shares"] if p["shares"] else 0
        send_discord({
            "title": "👁️ Wallet på overvågningslisten har satset",
            "url": link, "description": f"**{match}**\n{market_name}", "color": 0x3498DB,
            "fields": [
                {"name": "Udfald", "value": str(outcome), "inline": True},
                {"name": "Samlet indsats", "value": f"${p['usd']:,.0f}", "inline": True},
                {"name": "Købt til", "value": f"{avg*100:.0f} %", "inline": True},
                {"name": "Wallet", "value": f"[{short(wallet)}](https://polymarket.com/profile/{wallet})", "inline": True},
                {"name": "Status", "value": status, "inline": True},
                {"name": "Kampstart", "value": start_txt, "inline": True},
                open_link,
            ],
            "footer": {"text": "Match Watch · overvågningsliste"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, ping=True)
        state["alerted"].append(wkey)

    if top:
        return

    # mistankescore pr. udfald
    by_outcome = {}
    for (wallet, outcome), p in pos.items():
        by_outcome.setdefault(outcome, []).append((wallet, p))

    base = normal.get(mtype, MIN_BASELINE_USD)
    tm = normal.get("_tourn", {}).get(tournament_key(e))
    if mtype == "match" and tm:
        base = max(MIN_BASELINE_USD, tm)   # sammenlign med turneringens egne kampe
    for outcome, rows in by_outcome.items():
        suspicious, fresh, details = 0.0, 0, []
        for wallet, p in rows:
            n = wallet_market_count(wallet) if p["usd"] >= LOOKUP_MIN_USD else None
            if n is not None and n <= 2:
                fresh += 1
            s = p["wusd_price"] * wallet_weight(n)
            suspicious += s
            details.append((s, wallet, p, n))
        if fresh >= 3:
            suspicious *= 1.5
        if suspicious < MIN_SUSPICIOUS_USD:
            continue
        score = suspicious / base
        if score < SCORE_ORANGE:
            continue
        level = int(min(score, 10))
        skey = f"score|{cid}|{outcome}|{level}"
        if skey in state["alerted"]:
            continue

        red = score >= SCORE_RED
        details.sort(key=lambda d: d[0], reverse=True)
        lines = []
        for s, wallet, p, n in details[:6]:
            avg = p["usd"] / p["shares"] if p["shares"] else 0
            tag = "🆕 ny" if n is not None and n <= 2 else (f"{n} mkt" if n is not None else "lille")
            lines.append(f"`${p['usd']:>7,.0f}` til {avg*100:.0f}% · "
                         f"[{short(wallet)}](https://polymarket.com/profile/{wallet}) · {tag}")
        outcome_usd = sum(p["usd"] for _, p in rows)
        send_discord({
            "title": f"{'🔴 MEGET MISTÆNKELIGT' if red else '🟠 Mistænkeligt'} · score {score:.1f}",
            "url": link, "description": f"**{match}**\n{market_name}",
            "color": 0xE74C3C if red else 0xF39C12,
            "fields": [
                {"name": "Pengene går på", "value": str(outcome), "inline": True},
                {"name": "Satset på dette hold", "value": f"${outcome_usd:,.0f}", "inline": True},
                {"name": "Volumen vs. normalt", "value": f"${volume:,.0f} ({volume/base:.1f}× normalt)", "inline": True},
                {"name": "Nye wallets på holdet", "value": str(fresh), "inline": True},
                {"name": "Status", "value": status, "inline": True},
                {"name": "Kampstart", "value": start_txt, "inline": True},
                {"name": "Største indsatser", "value": "\n".join(lines)[:1024] or "-", "inline": False},
                open_link,
            ],
            "footer": {"text": "Match Watch v5.1 · score = vægtede mistænkelige penge ÷ normal volumen"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, ping=red)
        state["alerted"].append(skey)


def main():
    state = load_state()
    markets, normal = scan_markets()
    print(f"Tjekker {len(markets)} markeder i lave ligaer. Normal volumen: "
          f"{ {k: v for k, v in normal.items() if k != '_tourn'} }")
    if state.get("version") != 51:
        send_discord({
            "title": "✅ Match Watch v5.1 kører",
            "description": ("Ny metode: hvert hold får en **mistankescore**.\n"
                            "Penge på underdogs og fra nye konti vægter mest, penge på klare favoritter "
                            "under kampen vægter lidt.\n"
                            "Fokus på **lave ligaer**: turneringer hvor kampe normalt omsætter over "
                            f"${LOW_TIER_MAX_MEDIAN:,} springes over, og hver kamp sammenlignes med sin egen turnering.\n"
                            f"Lige nu tjekker jeg **{len(markets)}** markeder (live + næste {HOURS_BEFORE} timer)."),
            "color": 0x2ECC71, "footer": {"text": "Match Watch"},
        })
        state["version"] = 51
    for i in range(CHECKS_PER_RUN):
        t0 = time.time()
        if i and i % 5 == 0:   # opdater kamplisten hvert 5. minut, så nye kampe kommer med
            markets, normal = scan_markets() or (markets, normal)
            wallet_cache.clear()
        for m in markets:
            analyse_market(m, normal, state)
        save_state(state)
        if i < CHECKS_PER_RUN - 1:
            time.sleep(max(0, SECONDS_BETWEEN - (time.time() - t0)))


if __name__ == "__main__":
    main()
