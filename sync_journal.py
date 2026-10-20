"""Solana wallet -> Notion trade journal sync.

Env vars: HELIUS_API_KEY, NOTION_TOKEN
Idempotent: safe to run on a schedule. Average-cost method per token; a trade
is "closed" when the token balance returns to ~0.
"""
import os, re, sys, time, requests
from datetime import datetime, timezone

WALLET = "Eiozy5kVAvzrY5h7juAREHa6B3qVsfYL246hQh4GJumq"
DB_EVENTS = "900ea75d-b46a-40cb-b35f-8703162a1c2f"
DB_OPEN = "5289c2ff-da55-421d-b27e-c2ca26314241"
DB_CLOSED = "0f6f708e-8a63-4682-8bbe-2029dcf39a17"
DB_WINS = "99884c76-e9fd-45c5-9fc9-730e774bae18"
DB_LOSSES = "56c34621-39f8-4352-a0fd-b00df1f3e5b4"
PAGE_ID = "3fe93233-c104-81ec-a5c3-cb7f2d29e721"   # Solana Trade Journal page

SOL = "So11111111111111111111111111111111111111112"
STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
HELIUS_KEY = os.environ.get("HELIUS_API_KEY", "")
NOTION = {
    "Authorization": f"Bearer {os.environ.get('NOTION_TOKEN', '')}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
DUST = 1e-9


# ---------- data fetch ----------
def fetch_swaps():
    out, before = [], None
    while True:
        url = f"https://api.helius.xyz/v0/addresses/{WALLET}/transactions"
        params = {"api-key": HELIUS_KEY, "limit": 100}
        if before:
            params["before"] = before
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out += batch
        before = batch[-1]["signature"]
    return sorted(out, key=lambda t: t["timestamp"])


_sol_cache = {}
def sol_usd(ts):
    """SOL/USD close at the minute of the tx (Binance public klines)."""
    k = ts // 60
    if k not in _sol_cache:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": "SOLUSDT", "interval": "1m",
                                 "startTime": k * 60000, "limit": 1}, timeout=20)
        r.raise_for_status()
        d = r.json()
        _sol_cache[k] = float(d[0][4]) if d else None
    return _sol_cache[k]


def legs(tx):
    """Return (spent, received) as {mint: amount} for the wallet."""
    # Net balance change for the wallet, regardless of how Helius labels the tx.
    if tx.get("transactionError"):
        return {}, {}
    delta = {}
    for t in tx.get("tokenTransfers") or []:
        mint, amt = t.get("mint"), float(t.get("tokenAmount") or 0)
        if not mint or mint == SOL:        # wrapped SOL: native balance below covers it
            continue
        if t.get("toUserAccount") == WALLET:
            delta[mint] = delta.get(mint, 0) + amt
        if t.get("fromUserAccount") == WALLET:
            delta[mint] = delta.get(mint, 0) - amt
    for a in tx.get("accountData") or []:
        if a.get("account") == WALLET:
            lamports = int(a.get("nativeBalanceChange") or 0)
            if tx.get("feePayer") == WALLET:
                lamports += int(tx.get("fee") or 0)   # ignore the network fee
            delta[SOL] = delta.get(SOL, 0) + lamports / 1e9
    spent = {m: -d for m, d in delta.items() if d < -1e-9}
    got = {m: d for m, d in delta.items() if d > 1e-9}
    return spent, got


def usd_value(mint, amt, ts):
    if mint in STABLES:
        return amt
    if mint == SOL:
        p = sol_usd(ts)
        return amt * p if p else None
    return None


def to_trades(txs):
    """Walk swaps in time order -> events, closed trades, open positions."""
    pos = {}      # mint -> {qty, cost, entry_ts, entry_sig, entry_price0}
    events, closed = [], []
    cum = 0.0
    for tx in txs:
        spent, got = legs(tx)
        ts, sig = tx["timestamp"], tx["signature"]
        quote_spent = [(m, a) for m, a in spent.items() if m == SOL or m in STABLES]
        quote_got = [(m, a) for m, a in got.items() if m == SOL or m in STABLES]
        tok_got = [(m, a) for m, a in got.items() if m != SOL and m not in STABLES]
        tok_spent = [(m, a) for m, a in spent.items() if m != SOL and m not in STABLES]

        if quote_spent and tok_got:                       # BUY
            mint, qty = tok_got[0]
            usd = sum(usd_value(m, a, ts) or 0 for m, a in quote_spent)
            p = pos.setdefault(mint, {"qty": 0.0, "cost": 0.0, "entry_ts": ts,
                                      "entry_sig": sig, "first_cost": 0.0, "first_qty": 0.0})
            if p["qty"] <= DUST:
                p.update(qty=0.0, cost=0.0, entry_ts=ts, entry_sig=sig,
                         first_cost=0.0, first_qty=0.0)
            p["qty"] += qty
            p["cost"] += usd
            p["first_cost"] += usd
            p["first_qty"] += qty
            events.append(dict(sig=sig, ts=ts, type="Entry", mint=mint,
                               price=usd / qty if qty else 0, pnl=None))
        elif quote_got and tok_spent:                     # SELL
            mint, qty = tok_spent[0]
            p = pos.get(mint)
            if not p or p["qty"] <= DUST:
                continue                                   # sold something bought before history window
            usd = sum(usd_value(m, a, ts) or 0 for m, a in quote_got)
            sell_qty = min(qty, p["qty"])
            avg = p["cost"] / p["qty"]
            realised = usd * (sell_qty / qty) - avg * sell_qty
            p["cost"] -= avg * sell_qty
            p["qty"] -= sell_qty
            p.setdefault("real", 0.0)
            p["real"] += realised
            p.setdefault("proceeds", 0.0)
            p["proceeds"] += usd * (sell_qty / qty)
            p.setdefault("sold_qty", 0.0)
            p["sold_qty"] += sell_qty
            cum += realised
            events.append(dict(sig=sig, ts=ts, type="Exit", mint=mint,
                               price=usd / qty if qty else 0, pnl=realised, cum=cum,
                               # pairing info: the entry this exit belongs to
                               entry_ts=p["entry_ts"], entry_sig=p["entry_sig"],
                               entry_price=avg, size=sell_qty, cost=avg * sell_qty,
                               proceeds=usd * (sell_qty / qty), full=p["qty"] <= DUST))
            if p["qty"] <= DUST:
                basis = p["first_cost"]
                closed.append(dict(
                    mint=mint, entry_ts=p["entry_ts"], exit_ts=ts,
                    entry_price=p["first_cost"] / p["first_qty"] if p["first_qty"] else 0,
                    exit_price=p["proceeds"] / p["sold_qty"] if p["sold_qty"] else 0,
                    size=p["first_qty"], pnl=p["real"],
                    pct=p["real"] / basis if basis else 0,
                    entry_sig=p["entry_sig"], exit_sig=sig))
                for k in ("real", "proceeds", "sold_qty"):
                    p.pop(k, None)
                p["qty"] = 0.0
    open_pos = {m: p for m, p in pos.items() if p["qty"] > DUST}
    return events, closed, open_pos


# ---------- helpers ----------
def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

def solscan(sig):
    return f"https://solscan.io/tx/{sig}"

def looks_random(s):
    """True for names that are not words: digit/letter mixes or odd mixed-case strings."""
    if not s or s.isdigit():
        return True
    if re.search(r"\d", s) and re.search(r"[A-Za-z]", s):
        return True
    if s.isalpha():
        if s.islower() or s.isupper():
            return False
        if re.fullmatch(r"(?:[A-Z][a-z]{2,})+", s):      # Pepe, PepeBot
            return False
        return True
    return False                                           # symbols like $WIF


def pretty(raw):
    """Random-looking names become 'Token ' + first 2 + last 2 characters."""
    if looks_random(raw):
        return f"Token {raw[:2]}{raw[-2:]}" if len(raw) > 4 else f"Token {raw}"
    return raw


_sym = {}
def raw_symbol(mint):
    """Token ticker from Jupiter (matched on the exact mint, with retries), then DexScreener."""
    for attempt in range(3):
        try:
            r = requests.get("https://lite-api.jup.ag/tokens/v2/search",
                             params={"query": mint}, timeout=15)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1)); continue
            hit = next((t for t in (r.json() or []) if t.get("id") == mint), None)
            if hit and hit.get("symbol"):
                return hit["symbol"].strip()
            break
        except Exception:
            time.sleep(1)
    try:
        pairs = requests.get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}", timeout=15).json()
        for p in pairs if isinstance(pairs, list) else []:
            b = p.get("baseToken") or {}
            if b.get("address") == mint and b.get("symbol"):
                return b["symbol"].strip()
    except Exception:
        pass
    return mint[:6]

def symbol(mint):
    if mint == SOL: return "SOL"
    if mint not in _sym:
        raw = raw_symbol(mint)
        # a failed lookup falls back to the first 6 mint characters, which is never a real name
        _sym[mint] = f"Token {raw[:2]}{raw[-2:]}" if raw == mint[:6] else pretty(raw)
    return _sym[mint]

def prices_now(mints):
    """Batch price lookup: Jupiter first, DexScreener (best-liquidity pair) for anything missing."""
    mints = list(mints)
    out = {}
    for i in range(0, len(mints), 50):
        chunk = mints[i:i + 50]
        for attempt in range(3):
            try:
                r = requests.get("https://lite-api.jup.ag/price/v3",
                                 params={"ids": ",".join(chunk)}, timeout=20)
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1)); continue
                r.raise_for_status()
                for m, v in (r.json() or {}).items():
                    if v and v.get("usdPrice") is not None:
                        out[m] = float(v["usdPrice"])
                break
            except Exception:
                time.sleep(1)
    missing = [m for m in mints if m not in out]
    for i in range(0, len(missing), 30):
        chunk = missing[i:i + 30]
        try:
            pairs = requests.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk),
                                 timeout=20).json()
            best = {}
            for p in pairs if isinstance(pairs, list) else []:
                m = (p.get("baseToken") or {}).get("address")
                liq = ((p.get("liquidity") or {}).get("usd")) or 0
                if m in chunk and p.get("priceUsd") and liq >= best.get(m, (-1, 0))[0]:
                    best[m] = (liq, float(p["priceUsd"]))
            for m, (_, px) in best.items():
                out[m] = px
        except Exception:
            pass
    return out


# ---------- notion ----------
def n_query(db):
    res, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor: body["start_cursor"] = cursor
        r = requests.post(f"https://api.notion.com/v1/databases/{db}/query",
                          headers=NOTION, json=body, timeout=30)
        r.raise_for_status()
        d = r.json()
        res += d["results"]
        if not d.get("has_more"): break
        cursor = d["next_cursor"]
    return res

def n_create(db, props):
    r = requests.post("https://api.notion.com/v1/pages", headers=NOTION, timeout=30,
                      json={"parent": {"database_id": db}, "properties": props})
    r.raise_for_status()
    time.sleep(0.35)

def n_archive(page_id):
    requests.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=NOTION,
                   json={"archived": True}, timeout=30).raise_for_status()
    time.sleep(0.35)

def money(x):
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"

def update_breakeven(net, wins, losses, unreal):
    """Rewrite the 'Breakeven tracker' callout on the journal page."""
    need = max(0.0, -net)
    net_all = net + unreal
    need_all = max(0.0, -net_all)
    txt = (f"Breakeven tracker: net realised PnL {money(net)} (wins {money(wins)}, "
           f"losses {money(-losses)}). Needed in winning exits to turn positive: {money(need)}. "
           f"Counting open positions at current prices ({money(unreal)}): "
           f"net {money(net_all)}, needed {money(need_all)}.")
    def find(block_id, depth=0):
        r = requests.get(f"https://api.notion.com/v1/blocks/{block_id}/children",
                         headers=NOTION, params={"page_size": 100}, timeout=30)
        r.raise_for_status()
        for b in r.json()["results"]:
            if b["type"] == "callout":
                plain = "".join(t["plain_text"] for t in b["callout"]["rich_text"])
                if plain.startswith("Breakeven tracker"):
                    return b["id"]
            if depth < 2 and b["type"] in ("column_list", "column"):
                hit = find(b["id"], depth + 1)
                if hit: return hit
        return None
    bid = find(PAGE_ID)
    if bid:
        requests.patch(f"https://api.notion.com/v1/blocks/{bid}", headers=NOTION, timeout=30,
                       json={"callout": {"rich_text": [{"text": {"content": txt}}]}}
                       ).raise_for_status()

def n_patch(page_id, props):
    requests.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=NOTION, timeout=30,
                   json={"properties": props}).raise_for_status()
    time.sleep(0.35)

def plain(prop, key):
    return "".join(t["plain_text"] for t in prop.get(key, []))

def rename_pass(events):
    """Self-healing: re-derive each row's token name from its mint and fix any that differ."""
    mint_by_sig = {e["sig"]: e["mint"] for e in events}
    fixed = 0
    for db in (DB_CLOSED, DB_WINS, DB_LOSSES):
        for p in n_query(db):
            mint = plain(p["properties"]["Mint"], "rich_text")
            if not mint: continue
            want = symbol(mint)
            if plain(p["properties"]["Token"], "title") != want:
                n_patch(p["id"], {"Token": title(want)}); fixed += 1
    for p in n_query(DB_EVENTS):
        sig = plain(p["properties"]["Tx Signature"], "rich_text")
        mint = mint_by_sig.get(sig)
        sel = (p["properties"]["Type"].get("select") or {}).get("name")
        if not mint or not sel: continue
        want = symbol(mint)
        if plain(p["properties"]["Event"], "title") != f"{sel} {want}":
            n_patch(p["id"], {"Event": title(f"{sel} {want}"), "Token": text(want)}); fixed += 1
    if fixed: print(f"renamed {fixed} rows")

def title(s): return {"title": [{"text": {"content": s}}]}
def text(s): return {"rich_text": [{"text": {"content": s}}]}
def num(x): return {"number": None if x is None else float(x)}
def date(ts): return {"date": {"start": iso(ts)}}


def main():
    if not HELIUS_KEY or not os.environ.get("NOTION_TOKEN"):
        sys.exit("Set HELIUS_API_KEY and NOTION_TOKEN")
    txs = fetch_swaps()
    types = {}
    for t in txs:
        types[t.get("type")] = types.get(t.get("type"), 0) + 1
    print(f"fetched {len(txs)} txs; types={types}")
    if txs:
        print(f"range {iso(txs[0]['timestamp'])} -> {iso(txs[-1]['timestamp'])}")
    events, closed, open_pos = to_trades(txs)

    # events (append-only, keyed by signature)
    have = {"".join(t["plain_text"] for t in p["properties"]["Tx Signature"]["rich_text"])
            for p in n_query(DB_EVENTS)}
    cum = 0.0
    for e in events:
        if e["type"] == "Exit": cum = e["cum"]
        if e["sig"] in have: continue
        sym = symbol(e["mint"])
        n_create(DB_EVENTS, {
            "Event": title(f"{e['type']} {sym}"), "Time": date(e["ts"]),
            "Type": {"select": {"name": e["type"]}}, "Token": text(sym),
            "Price (USD)": num(e["price"]),
            "Realised PnL (USD)": num(e["pnl"]),
            "Cumulative PnL (USD)": num(cum if e["type"] == "Exit" else None),
            "Tx": {"url": solscan(e["sig"])}, "Tx Signature": text(e["sig"])})

    # wins / losses: every exit paired with its entry (append-only, keyed by exit tx url)
    have = set()
    for db in (DB_WINS, DB_LOSSES):
        have |= {p["properties"]["Exit Tx"]["url"] for p in n_query(db)}
    for e in events:
        if e["type"] != "Exit" or solscan(e["sig"]) in have: continue
        n_create(DB_WINS if e["pnl"] > 0 else DB_LOSSES, {
            "Token": title(symbol(e["mint"])), "Mint": text(e["mint"]),
            "Entry Time": date(e["entry_ts"]), "Exit Time": date(e["ts"]),
            "Entry Price (USD)": num(e["entry_price"]), "Exit Price (USD)": num(e["price"]),
            "Size Sold (Tokens)": num(e["size"]), "Cost (USD)": num(e["cost"]),
            "Proceeds (USD)": num(e["proceeds"]), "PnL (USD)": num(e["pnl"]),
            "PnL %": num(e["pnl"] / e["cost"] if e["cost"] else None),
            "Exit Type": {"select": {"name": "Full" if e["full"] else "Partial"}},
            "Entry Tx": {"url": solscan(e["entry_sig"])}, "Exit Tx": {"url": solscan(e["sig"])}})

    # closed trades (append-only, keyed by exit tx url)
    have = {p["properties"]["Exit Tx"]["url"] for p in n_query(DB_CLOSED)}
    for c in closed:
        if solscan(c["exit_sig"]) in have: continue
        n_create(DB_CLOSED, {
            "Token": title(symbol(c["mint"])), "Mint": text(c["mint"]),
            "Entry Time": date(c["entry_ts"]), "Exit Time": date(c["exit_ts"]),
            "Entry Price (USD)": num(c["entry_price"]), "Exit Price (USD)": num(c["exit_price"]),
            "Size (Tokens)": num(c["size"]), "Realised PnL (USD)": num(c["pnl"]),
            "PnL %": num(c["pct"]),
            "Result": {"select": {"name": "Win" if c["pnl"] > 0 else "Loss" if c["pnl"] < 0 else "Breakeven"}},
            "Hold Time (hrs)": num((c["exit_ts"] - c["entry_ts"]) / 3600),
            "Entry Tx": {"url": solscan(c["entry_sig"])}, "Exit Tx": {"url": solscan(c["exit_sig"])}})

    # open trades: refresh fully each run (prices change)
    for p in n_query(DB_OPEN): n_archive(p["id"])
    unreal = 0.0
    px = prices_now(open_pos.keys())
    unpriced = [symbol(m) for m in open_pos if m not in px]
    if unpriced:
        print("no price found for:", ", ".join(unpriced))
    for mint, p in open_pos.items():
        entry = p["cost"] / p["qty"]
        now = px.get(mint)
        upnl = (now - entry) * p["qty"] if now else None
        if upnl is not None: unreal += upnl
        n_create(DB_OPEN, {
            "Token": title(symbol(mint)), "Mint": text(mint), "Entry Time": date(p["entry_ts"]),
            "Entry Price (USD)": num(entry), "Size (Tokens)": num(p["qty"]),
            "Cost Basis (USD)": num(p["cost"]), "Current Price (USD)": num(now),
            "Current Value (USD)": num(now * p["qty"] if now else None),
            "Unrealised PnL (USD)": num(upnl),
            "Unrealised PnL %": num(upnl / p["cost"] if upnl is not None and p["cost"] else None),
            "Entry Tx": {"url": solscan(p["entry_sig"])}})
    rename_pass(events)
    exits = [e["pnl"] for e in events if e["type"] == "Exit"]
    wins = sum(x for x in exits if x > 0)
    losses = -sum(x for x in exits if x <= 0)
    update_breakeven(wins - losses, wins, losses, unreal)
    print(f"events={len(events)} closed={len(closed)} open={len(open_pos)} "
          f"net_realised={wins - losses:.2f} unrealised={unreal:.2f}")


if __name__ == "__main__":
    main()
