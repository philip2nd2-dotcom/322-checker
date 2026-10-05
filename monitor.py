#!/usr/bin/env python3
"""
CS2 Polymarket-overvågning v3 (GitHub Actions)
GitHub starter scriptet ca. hvert 5. minut. Scriptet bliver kørende i ~5 minutter
og tjekker kommende og igangværende CS2-kampe hvert minut.

Finder to mønstre:
 1. 🟠 Én ny wallet med en stor indsats i forhold til markedets volumen.
 2. 🔴 En klynge: flere nye wallets der satser på samme udfald i samme kamp.
"""
import os
import json
import time
from datetime import datetime, timezone, timedelta

import requests

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"

# ---------------- Indstillinger ----------------
CHECKS_PER_RUN = 5          # antal tjek pr. GitHub-kørsel
SECONDS_BETWEEN = 60        # tid mellem tjek
HOURS_BEFORE = 48           # kampe der starter inden for så mange timer
HOURS_AFTER = 8             # ... eller startede for højst så mange timer siden
MIN_BET_USD = 1000          # enkelt wallet: mindste samlede indsats
VOLUME_SHARE = 0.30         # enkelt wallet: mindst 30 % af markedets volumen
MAX_PRIOR_MARKETS = 2       # "ny" wallet = har handlet i højst så mange markeder
CLUSTER_MIN_WALLETS = 3     # klynge: mindst så mange nye wallets på samme udfald
CLUSTER_MIN_EACH_USD = 200  # klynge: hver wallet skal have sat mindst dette
STATE_FILE = "state.json"
MAX_STATE = 3000
# -----------------------------------------------

WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
ROLE_ID = os.environ.get("DISCORD_ROLE_ID", "").strip()   # Analytiker-rollen (valgfri)
session = requests.Session()
session.headers["User-Agent"] = "cs2-monitor/3.0"
wallet_cache = {}


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
        return datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None


def find_cs2_markets():
    """Åbne markeder i CS2-kampe der starter snart eller er i gang."""
    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(hours=HOURS_AFTER), now + timedelta(hours=HOURS_BEFORE)
    found, offset = [], 0
    while offset < 2000:
        events = get(f"{GAMMA}/events", {"tag_slug": "counter-strike-2", "closed": "false",
                                          "limit": 100, "offset": offset})
        if not events:
            break
        for e in events:
            if not (e.get("title") or "").startswith("Counter-Strike:"):
                continue
            start = parse_time(e.get("startTime"))
            if not start or not (lo <= start <= hi):
                continue
            for m in e.get("markets") or []:
                if m.get("closed") or not m.get("conditionId"):
                    continue
                m["_event"] = e
                found.append(m)
        if len(events) < 100:
            break
        offset += 100
    return found


def wallet_market_count(wallet):
    if wallet not in wallet_cache:
        trades = get(f"{DATA}/trades", {"user": wallet, "limit": 500}) or []
        wallet_cache[wallet] = len({t.get("conditionId") for t in trades})
    return wallet_cache[wallet]


def short(w):
    return f"{w[:6]}…{w[-4:]}"


def check_market(m, state):
    e = m["_event"]
    cid = m["conditionId"]
    volume = float(m.get("volumeNum") or m.get("volume") or 0)
    trades = get(f"{DATA}/trades", {"market": cid, "limit": 500}) or []

    totals = {}
    for t in trades:
        if t.get("side") != "BUY" or not t.get("proxyWallet"):
            continue
        try:
            size, price = float(t["size"]), float(t["price"])
        except (KeyError, TypeError, ValueError):
            continue
        a = totals.setdefault((t["proxyWallet"], t.get("outcome")), {"usd": 0.0, "shares": 0.0, "last": 0})
        a["usd"] += size * price
        a["shares"] += size
        a["last"] = max(a["last"], int(t.get("timestamp") or 0))

    match = (e.get("title") or "").replace("Counter-Strike: ", "")
    market_name = (m.get("question") or "").replace("Counter-Strike: ", "")
    link = f"https://polymarket.com/event/{e.get('slug')}"
    start = parse_time(e.get("startTime"))
    start_txt = f"<t:{int(start.timestamp())}:R>" if start else "?"
    new_by_outcome = {}

    for (wallet, outcome), a in totals.items():
        if a["usd"] < CLUSTER_MIN_EACH_USD:
            continue
        n = wallet_market_count(wallet)
        if n > MAX_PRIOR_MARKETS:
            continue
        new_by_outcome.setdefault(outcome, []).append((wallet, a, n))

        share = a["usd"] / volume if volume else 1.0
        akey = f"single|{cid}|{wallet}|{outcome}"
        if a["usd"] >= MIN_BET_USD and share >= VOLUME_SHARE and akey not in state["alerted"]:
            avg = a["usd"] / a["shares"] if a["shares"] else 0
            send_discord({
                "title": f"🟠 Stor indsats fra ny wallet",
                "url": link,
                "description": f"**{match}**\n{market_name}",
                "color": 0xF39C12,
                "fields": [
                    {"name": "Udfald", "value": str(outcome), "inline": True},
                    {"name": "Indsats", "value": f"${a['usd']:,.0f}", "inline": True},
                    {"name": "Købt til", "value": f"{avg*100:.0f} %", "inline": True},
                    {"name": "Andel af volumen", "value": f"{share*100:.0f} % af ${volume:,.0f}", "inline": True},
                    {"name": "Wallet", "value": f"[{short(wallet)}](https://polymarket.com/profile/{wallet}) · {n} marked(er)", "inline": True},
                    {"name": "Kampstart", "value": start_txt, "inline": True},
                    {"name": "\u200b", "value": f"**[➜ Åbn kampen på Polymarket]({link})**", "inline": False},
                ],
                "footer": {"text": "Match Watch · enkelt wallet"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            state["alerted"].append(akey)

    for outcome, group in new_by_outcome.items():
        if len(group) < CLUSTER_MIN_WALLETS:
            continue
        level = len(group) // CLUSTER_MIN_WALLETS
        ckey = f"cluster|{cid}|{outcome}|{level}"
        if ckey in state["alerted"]:
            continue
        total = sum(a["usd"] for _, a, _ in group)
        group.sort(key=lambda g: g[1]["usd"], reverse=True)
        wallets = "\n".join(
            f"`${a['usd']:>7,.0f}` [{short(w)}](https://polymarket.com/profile/{w}) · {n} mkt"
            for w, a, n in group[:10])
        send_discord({
            "title": f"🔴 KLYNGE: {len(group)} nye wallets på samme udfald",
            "url": link,
            "description": f"**{match}**\n{market_name}",
            "color": 0xE74C3C,
            "fields": [
                {"name": "Udfald", "value": str(outcome), "inline": True},
                {"name": "Samlet indsats", "value": f"${total:,.0f}", "inline": True},
                {"name": "Markedets volumen", "value": f"${volume:,.0f}", "inline": True},
                {"name": "Kampstart", "value": start_txt, "inline": True},
                {"name": "Wallets", "value": wallets[:1024], "inline": False},
                {"name": "\u200b", "value": f"**[➜ Åbn kampen på Polymarket]({link})**", "inline": False},
            ],
            "footer": {"text": "Match Watch · klynge"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, ping=True)
        state["alerted"].append(ckey)


def main():
    state = load_state()
    markets = find_cs2_markets()
    print(f"Fandt {len(markets)} åbne markeder i kommende/igangværende CS2-kampe")
    if state.get("version") != 3:
        send_discord({
            "title": "✅ Match Watch v3 kører",
            "description": (f"Jeg tjekker kommende og igangværende CS2-kampe **hvert minut**.\n"
                            f"Lige nu holder jeg øje med **{len(markets)}** markeder."),
            "color": 0x2ECC71,
            "footer": {"text": "Match Watch"},
        })
        state["version"] = 3
    for i in range(CHECKS_PER_RUN):
        t0 = time.time()
        for m in markets:
            check_market(m, state)
        save_state(state)
        if i < CHECKS_PER_RUN - 1:
            time.sleep(max(0, SECONDS_BETWEEN - (time.time() - t0)))


if __name__ == "__main__":
    main()
