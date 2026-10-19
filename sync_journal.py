"""Solana wallet -> Notion trade journal sync.

Env vars: HELIUS_API_KEY, NOTION_TOKEN
Idempotent: safe to run on a schedule. Average-cost method per token; a trade
is "closed" when the token balance returns to ~0.
"""
import os, sys, time, requests
from datetime import datetime, timezone

WALLET = "Eiozy5kVAvzrY5h7juAREHa6B3qVsfYL246hQh4GJumq"
DB_EVENTS = "900ea75d-b46a-40cb-b35f-8703162a1c2f"
DB_OPEN = "5289c2ff-da55-421d-b27e-c2ca26314241"
DB_CLOSED = "0f6f708e-8a63-4682-8bbe-2029dcf39a17"

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
        params = {"api-key": HELIUS_KEY, "type": "SWAP", "limit": 100}
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
    ev = (tx.get("events") or {}).get("swap") or {}
    spent, got = {}, {}
    ni, no = ev.get("nativeInput"), ev.get("nativeOutput")
    if ni and ni.get("account") == WALLET:
        spent[SOL] = spent.get(SOL, 0) + int(ni["amount"]) / 1e9
    if no and no.get("account") == WALLET:
        got[SOL] = got.get(SOL, 0) + int(no["amount"]) / 1e9
    for side, bucket in (("tokenInputs", spent), ("tokenOutputs", got)):
        for t in ev.get(side) or []:
            if t.get("userAccount") == WALLET:
                raw = t["rawTokenAmount"]
                amt = int(raw["tokenAmount"]) / 10 ** raw["decimals"]
                bucket[t["mint"]] = bucket.get(t["mint"], 0) + amt
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
                               price=usd / qty if qty else 0, pnl=realised, cum=cum))
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

_sym = {}
def symbol(mint):
    if mint == SOL: return "SOL"
    if mint not in _sym:
        try:
            r = requests.get("https://lite-api.jup.ag/tokens/v2/search",
                             params={"query": mint}, timeout=15).json()
            _sym[mint] = (r[0].get("symbol") if r else None) or mint[:6]
        except Exception:
            _sym[mint] = mint[:6]
    return _sym[mint]

def price_now(mint):
    try:
        r = requests.get("https://lite-api.jup.ag/price/v3", params={"ids": mint}, timeout=15).json()
        return float(r[mint]["usdPrice"])
    except Exception:
        return None


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

def title(s): return {"title": [{"text": {"content": s}}]}
def text(s): return {"rich_text": [{"text": {"content": s}}]}
def num(x): return {"number": None if x is None else float(x)}
def date(ts): return {"date": {"start": iso(ts)}}


def main():
    if not HELIUS_KEY or not os.environ.get("NOTION_TOKEN"):
        sys.exit("Set HELIUS_API_KEY and NOTION_TOKEN")
    events, closed, open_pos = to_trades(fetch_swaps())

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
    for mint, p in open_pos.items():
        entry = p["cost"] / p["qty"]
        now = price_now(mint)
        upnl = (now - entry) * p["qty"] if now else None
        n_create(DB_OPEN, {
            "Token": title(symbol(mint)), "Mint": text(mint), "Entry Time": date(p["entry_ts"]),
            "Entry Price (USD)": num(entry), "Size (Tokens)": num(p["qty"]),
            "Cost Basis (USD)": num(p["cost"]), "Current Price (USD)": num(now),
            "Unrealised PnL (USD)": num(upnl),
            "Unrealised PnL %": num(upnl / p["cost"] if upnl is not None and p["cost"] else None),
            "Entry Tx": {"url": solscan(p["entry_sig"])}})
    print(f"events={len(events)} closed={len(closed)} open={len(open_pos)}")


if __name__ == "__main__":
    main()
