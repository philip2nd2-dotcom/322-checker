#!/usr/bin/env python3
"""
CS2 Polymarket-overvågning (GitHub Actions-version)
Køres automatisk hvert 5. minut af GitHub. Tjekker åbne CS2-markeder én gang
og sender Discord-besked ved mistænkelig aktivitet.

Finder to slags mønstre:
 1. Én ny wallet med en stor indsats i forhold til markedets volumen.
 2. En "klynge": flere nye wallets der alle satser på samme udfald i samme kamp.
"""
import os
import json
import time
from datetime import datetime, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"

# ---------------- Indstillinger ----------------
KEYWORDS = ["counter-strike", "cs2"]
MIN_BET_USD = 1000          # enkelt wallet: mindste samlede indsats
VOLUME_SHARE = 0.30         # enkelt wallet: mindst 30 % af markedets volumen
MAX_PRIOR_MARKETS = 2       # "ny" wallet = har handlet i højst så mange markeder
CLUSTER_MIN_WALLETS = 3     # klynge: mindst så mange nye wallets på samme udfald
CLUSTER_MIN_EACH_USD = 200  # klynge: hver wallet skal have sat mindst dette
STATE_FILE = "state.json"
MAX_STATE = 3000
# -----------------------------------------------

WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
session = requests.Session()
session.headers["User-Agent"] = "cs2-monitor/2.0"
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
            s.setdefault("alerted", [])
            return s
    except (FileNotFoundError, json.JSONDecodeError):
        return {"alerted": []}


def save_state(state):
    state["alerted"] = state["alerted"][-MAX_STATE:]
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def send_discord(title, url, lines, color):
    print(f"\n=== {title} ===\n" + "\n".join(lines))
    if not WEBHOOK.startswith("https://"):
        print("(Ingen webhook sat - besked ikke sendt)")
        return
    payload = {"embeds": [{
        "title": title[:250],
        "url": url,
        "description": "\n".join(lines)[:4000],
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }]}
    try:
        r = session.post(WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            time.sleep(2)
            session.post(WEBHOOK, json=payload, timeout=10)
        time.sleep(1)
    except requests.RequestException as e:
        print(f"Discord-fejl: {e}")


def find_cs2_markets():
    markets, offset = [], 0
    while offset < 10000:
        batch = get(f"{GAMMA}/markets",
                    {"active": "true", "closed": "false", "limit": 500, "offset": offset})
        if not batch:
            break
        for m in batch:
            if any(k in (m.get("question") or "").lower() for k in KEYWORDS):
                markets.append(m)
        if len(batch) < 500:
            break
        offset += 500
    return markets


def wallet_market_count(wallet):
    if wallet not in wallet_cache:
        trades = get(f"{DATA}/trades", {"user": wallet, "limit": 500}) or []
        wallet_cache[wallet] = len({t.get("conditionId") for t in trades})
    return wallet_cache[wallet]


def market_link(m):
    slug = ((m.get("events") or [{}])[0] or {}).get("slug") or m.get("slug")
    return f"https://polymarket.com/event/{slug}"


def check_market(m, state):
    cid = m.get("conditionId")
    if not cid:
        return
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
        key = (t["proxyWallet"], t.get("outcome"))
        a = totals.setdefault(key, {"usd": 0.0, "shares": 0.0, "last": 0})
        a["usd"] += size * price
        a["shares"] += size
        a["last"] = max(a["last"], int(t.get("timestamp") or 0))

    title = m.get("question") or "Ukendt kamp"
    link = market_link(m)
    new_by_outcome = {}

    for (wallet, outcome), a in totals.items():
        if a["usd"] < CLUSTER_MIN_EACH_USD:
            continue
        n = wallet_market_count(wallet)
        if n > MAX_PRIOR_MARKETS:
            continue
        new_by_outcome.setdefault(outcome, []).append((wallet, a, n))

        # Mønster 1: én stor ny wallet
        share = a["usd"] / volume if volume else 1.0
        akey = f"single|{cid}|{wallet}|{outcome}"
        if a["usd"] >= MIN_BET_USD and share >= VOLUME_SHARE and akey not in state["alerted"]:
            avg = a["usd"] / a["shares"] if a["shares"] else 0
            when = datetime.fromtimestamp(a["last"], timezone.utc).strftime("%d-%m %H:%M UTC") if a["last"] else "?"
            send_discord(
                f"🚨 Stor indsats fra ny wallet: {title}", link,
                [f"**Udfald:** {outcome}",
                 f"**Indsats:** ${a['usd']:,.0f} til gns. {avg*100:.0f} %",
                 f"**Andel af volumen:** {share*100:.0f} % (volumen ${volume:,.0f})",
                 f"**Wallet har handlet i:** {n} marked(er)",
                 f"**Seneste køb:** {when}",
                 f"[Se wallet](https://polymarket.com/profile/{wallet})"],
                0xE74C3C)
            state["alerted"].append(akey)

    # Mønster 2: klynge af nye wallets på samme udfald
    for outcome, group in new_by_outcome.items():
        if len(group) < CLUSTER_MIN_WALLETS:
            continue
        level = len(group) // CLUSTER_MIN_WALLETS  # ny besked hver gang klyngen vokser med 3
        ckey = f"cluster|{cid}|{outcome}|{level}"
        if ckey in state["alerted"]:
            continue
        total = sum(a["usd"] for _, a, _ in group)
        group.sort(key=lambda g: g[1]["usd"], reverse=True)
        lines = [f"**Udfald:** {outcome}",
                 f"**{len(group)} nye wallets** har tilsammen sat **${total:,.0f}**",
                 f"**Markedets volumen:** ${volume:,.0f}", ""]
        for wallet, a, n in group[:10]:
            lines.append(f"• ${a['usd']:,.0f} – [{wallet[:6]}…{wallet[-4:]}](https://polymarket.com/profile/{wallet}) ({n} marked(er))")
        send_discord(f"⚠️ Klynge af nye wallets: {title}", link, lines, 0xF39C12)
        state["alerted"].append(ckey)


def main():
    state = load_state()
    markets = find_cs2_markets()
    print(f"Fandt {len(markets)} åbne CS2-markeder")
    if not state.get("started"):
        send_discord("✅ CS2-overvågningen kører nu", None,
                     ["Jeg tjekker Polymarket hvert ~5. minut og skriver her, når noget ser mistænkeligt ud.",
                      f"Lige nu er der {len(markets)} åbne CS2-markeder."], 0x2ECC71)
        state["started"] = True
    for m in markets:
        check_market(m, state)
        time.sleep(0.3)
    save_state(state)


if __name__ == "__main__":
    main()
