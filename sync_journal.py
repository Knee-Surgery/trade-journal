"""Solana wallet -> Notion trade journal sync.

Env vars: HELIUS_API_KEY, NOTION_TOKEN
Idempotent: safe to run on a schedule. Average-cost method per token; a trade
is "closed" when the token balance returns to ~0.
"""
import os, re, sys, time, requests
from datetime import datetime, timezone

WALLET = "Eiozy5kVAvzrY5h7juAREHa6B3qVsfYL246hQh4GJumq"
OTHER_WALLET = "77p7fw49wuZzuQNLomnBZFZ5dUvntW243jiBVnrNJSz9"   # the user's other wallet; transfers shown separately
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
def fetch_swaps(known=()):
    """Wallet history, newest first from Helius. Stops paging at the first already-known signature."""
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
        new = [t for t in batch if t["signature"] not in known]
        out += new
        if len(new) < len(batch):
            break
        before = batch[-1]["signature"]
    return sorted(out, key=lambda t: t["timestamp"])


def latest_signature():
    """Newest wallet signature via plain RPC: a cheap 'anything new?' check before the full fetch."""
    r = requests.post(f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}", timeout=20, json={
        "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
        "params": [WALLET, {"limit": 1}]})
    r.raise_for_status()
    res = r.json().get("result") or []
    return res[0]["signature"] if res else None


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
    """Real ticker in capitals (e.g. CATE). If no ticker can be found, the first 6 mint characters."""
    if mint == SOL: return "SOL"
    if mint not in _sym:
        _sym[mint] = raw_symbol(mint).upper()
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

def pct(x):
    return "n/a" if x is None else f"{x * 100:+.2f}%"

def update_breakeven(net, wins, losses, unreal, net_pct=None, unreal_pct=None):
    """Rewrite the 'Breakeven tracker' callout on the journal page."""
    need = max(0.0, -net)
    net_all = net + unreal
    need_all = max(0.0, -net_all)
    txt = (f"Breakeven tracker: net realised PnL {money(net)} ({pct(net_pct)}) (wins {money(wins)}, "
           f"losses {money(-losses)}). Needed in winning exits to turn positive: {money(need)}. "
           f"Counting open positions at current prices ({money(unreal)}, {pct(unreal_pct)}): "
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


def onchain_balances():
    """Real token balances straight from the wallet (both token programs), keyed by mint."""
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
    out = {}
    for prog in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                 "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        r = requests.post(url, timeout=30, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [WALLET, {"programId": prog}, {"encoding": "jsonParsed"}]})
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(j["error"])
        for a in j["result"]["value"]:
            info = a["account"]["data"]["parsed"]["info"]
            amt = float(info["tokenAmount"].get("uiAmountString") or 0)
            out[info["mint"]] = out.get(info["mint"], 0) + amt
    return out


_last_bal = {}

def flows(txs):
    """Money moved in and out of the wallet (not trades): txs where only SOL/stables move.
    Returns {"in": usd, "out": usd, "other_in": usd, "other_out": usd} (other_* = with OTHER_WALLET)."""
    f = {"in": 0.0, "out": 0.0, "other_in": 0.0, "other_out": 0.0}
    for tx in txs:
        spent, got = legs(tx)
        if any(m != SOL and m not in STABLES for m in list(spent) + list(got)):
            continue                                       # a token moved: that's a trade, not a transfer
        usd = sum(usd_value(m, a, tx["timestamp"]) or 0 for m, a in got.items()) \
            - sum(usd_value(m, a, tx["timestamp"]) or 0 for m, a in spent.items())
        if abs(usd) < 0.50:
            continue                                       # rent refunds, fees and dust
        parties = {t.get("fromUserAccount") for t in (tx.get("nativeTransfers") or []) + (tx.get("tokenTransfers") or [])} \
                | {t.get("toUserAccount") for t in (tx.get("nativeTransfers") or []) + (tx.get("tokenTransfers") or [])}
        key = "in" if usd > 0 else "out"
        f[key] += abs(usd)
        if OTHER_WALLET in parties:
            f["other_" + key] += abs(usd)
    return f


def wallet_cash():
    """USD value of the SOL and stablecoins sitting in the wallet now."""
    r = requests.post(f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}", timeout=20, json={
        "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [WALLET]})
    r.raise_for_status()
    sol = r.json()["result"]["value"] / 1e9
    px = prices_now([SOL]).get(SOL) or 0.0
    return sol * px + sum(q for m, q in _last_bal.items() if m in STABLES)


def reconcile(open_pos):
    """Make holdings match the wallet. Quantities come from the chain; cost basis from the trade history."""
    global _last_bal
    try:
        bal = _last_bal = onchain_balances()
    except Exception as ex:
        print("balance check failed, using tracked quantities:", repr(ex))
        return open_pos
    res = {}
    for m, q in bal.items():
        if m == SOL or m in STABLES or q <= DUST:
            continue
        p = open_pos.get(m)
        if p:
            if abs(q - p["qty"]) > max(1e-6 * p["qty"], 1e-9):
                print(f"qty fixed {symbol(m)}: tracked {p['qty']:.6f} -> on-chain {q:.6f}")
            res[m] = dict(p, cost=p["cost"] / p["qty"] * q, qty=q)
        else:
            res[m] = dict(qty=q, cost=None, entry_ts=int(time.time()), entry_sig=None, untracked=True)
    for m in open_pos:
        if m not in res:
            print(f"dropped {symbol(m)}: no balance in wallet")
    return res


# ---------- charts (PNG files, committed to the repo and shown in Notion) ----------
BG, CARD, TXT, MUTED, GRID = "#0F1419", "#171D26", "#E8EDF2", "#8B98A9", "#273041"
GREEN, RED, BLUE = "#2FD08A", "#FF5C6C", "#5B9DFF"

def _style(ax, fig):
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(CARD)
    for s in ax.spines.values(): s.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=10, length=0)
    ax.grid(color=GRID, linewidth=0.8, alpha=0.8)
    ax.set_axisbelow(True)

def _title(fig, title, sub):
    fig.text(0.035, 0.945, title, color=TXT, fontsize=17, fontweight="bold", va="top")
    fig.text(0.035, 0.885, sub, color=MUTED, fontsize=10.5, va="top")

def _usd(x, _=None):
    return f"-\\${abs(x):,.2f}" if x < 0 else f"\\${x:,.2f}"   # escaped so matplotlib does not read $..$ as math

def make_charts(events, open_rows, namef, outdir="charts"):
    """Render four dashboard PNGs. Never raises into the sync (caller wraps it)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    os.makedirs(outdir, exist_ok=True)
    plt.rcParams["font.family"] = ["Inter", "DejaVu Sans"]
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    # 1. cumulative PnL with entries and exits
    fig, ax = plt.subplots(figsize=(12, 6.2))
    fig.subplots_adjust(left=0.08, right=0.97, top=0.80, bottom=0.12)
    _style(ax, fig)
    xs, ys, run = [], [], 0.0
    ent_x, ent_y, win_x, win_y, loss_x, loss_y = [], [], [], [], [], []
    for e in events:
        t = datetime.fromtimestamp(e["ts"], tz=timezone.utc)
        if e["type"] == "Exit":
            xs += [t, t]; ys += [run, e["cum"]]; run = e["cum"]
            (win_x if e["pnl"] > 0 else loss_x).append(t)
            (win_y if e["pnl"] > 0 else loss_y).append(run)
        else:
            ent_x.append(t); ent_y.append(run)
    if xs:
        xs.append(max(ent_x + [xs[-1]])); ys.append(run)      # extend the line to the latest event
        ax.axhline(0, color=MUTED, linewidth=1, alpha=0.7)
        ax.plot(xs, ys, color=BLUE, linewidth=2.2, drawstyle="steps-post", zorder=3)
        ax.fill_between(xs, ys, 0, step="post", where=[y >= 0 for y in ys], color=GREEN, alpha=0.12)
        ax.fill_between(xs, ys, 0, step="post", where=[y < 0 for y in ys], color=RED, alpha=0.12)
        ax.scatter(ent_x, ent_y, marker="^", s=46, color="#C9D3E0", edgecolor=BG, linewidth=0.8, zorder=4, label="Entry")
        ax.scatter(win_x, win_y, marker="o", s=62, color=GREEN, edgecolor=BG, linewidth=1, zorder=5, label="Winning exit")
        ax.scatter(loss_x, loss_y, marker="o", s=62, color=RED, edgecolor=BG, linewidth=1, zorder=5, label="Losing exit")
        ax.legend(loc="lower right", bbox_to_anchor=(1, 1.01), frameon=False, labelcolor=TXT, fontsize=10, ncol=3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.yaxis.set_major_formatter(_usd)
    _title(fig, "Cumulative realised PnL", f"Every entry and exit on one line  |  now {_usd(run)}  |  updated {stamp}")
    fig.savefig(f"{outdir}/cumulative.png", dpi=150, facecolor=BG); plt.close(fig)

    # per-token realised totals
    tot = {}
    for e in events:
        if e["type"] == "Exit":
            n = namef(e["mint"]); tot[n] = tot.get(n, 0) + e["pnl"]

    def bars(fname, title, sub, items, color):
        fig, ax = plt.subplots(figsize=(8, 5.2))
        fig.subplots_adjust(left=0.24, right=0.93, top=0.78, bottom=0.10)
        _style(ax, fig); ax.grid(axis="y", visible=False)
        if items:
            names = [n for n, _ in items][::-1]; vals = [v for _, v in items][::-1]
            ax.barh(names, vals, color=color, height=0.62)
            span = max(abs(v) for v in vals) or 1
            for i, v in enumerate(vals):
                ax.text(v + (span * 0.02 if v >= 0 else -span * 0.02), i, _usd(v), color=TXT,
                        va="center", ha="left" if v >= 0 else "right", fontsize=10)
            lo, hi = min(0, min(vals)), max(0, max(vals))
            ax.set_xlim(lo - span * 0.25 * (lo < 0), hi + span * 0.25)
            ax.xaxis.set_major_formatter(_usd)
        else:
            ax.text(0.5, 0.5, "Nothing to show yet", color=MUTED, ha="center", va="center", transform=ax.transAxes)
            ax.set_xticks([]); ax.set_yticks([])
        ax.tick_params(axis="y", colors=TXT, labelsize=11)
        _title(fig, title, sub)
        fig.savefig(f"{outdir}/{fname}", dpi=150, facecolor=BG); plt.close(fig)

    gains = sorted([(n, v) for n, v in tot.items() if v > 0], key=lambda x: -x[1])[:8]
    losses = sorted([(n, v) for n, v in tot.items() if v < 0], key=lambda x: x[1])[:8]
    bars("gainers.png", "Top gainers", "Realised PnL by token", gains, GREEN)
    bars("losers.png", "Biggest losses", "Realised PnL by token", losses, RED)

    # 4. open positions worth more than $2
    big = sorted([r for r in open_rows if (r["value"] or 0) > 2], key=lambda r: -r["value"])
    fig, ax = plt.subplots(figsize=(8, 5.2))
    fig.subplots_adjust(left=0.24, right=0.93, top=0.78, bottom=0.10)
    _style(ax, fig); ax.grid(axis="y", visible=False)
    if big:
        names = [r["name"] for r in big][::-1]
        ax.barh(names, [r["cost"] for r in big][::-1], color=GRID, height=0.62, label="Cost")
        ax.barh(names, [r["value"] for r in big][::-1], color=BLUE, height=0.34, label="Value now")
        for i, r in enumerate(big[::-1]):
            ax.text(max(r["value"], r["cost"]) * 1.03, i, f"{_usd(r['value'])}  ({_usd(r['upnl'])})",
                    color=GREEN if r["upnl"] >= 0 else RED, va="center", fontsize=10)
        ax.set_xlim(0, max(max(r["value"], r["cost"]) for r in big) * 1.6)
        ax.xaxis.set_major_formatter(_usd)
        ax.legend(loc="lower right", frameon=False, labelcolor=TXT, fontsize=10)
    else:
        ax.text(0.5, 0.5, "No open position above \\$2", color=MUTED, ha="center", va="center", transform=ax.transAxes)
        ax.set_xticks([]); ax.set_yticks([])
    ax.tick_params(axis="y", colors=TXT, labelsize=11)
    _title(fig, "Open positions over \\$2", f"Current value vs cost  |  updated {stamp}")
    fig.savefig(f"{outdir}/open.png", dpi=150, facecolor=BG); plt.close(fig)


def refresh_page(kpi_text, names):
    """Update the 'Dashboard summary' callout and point the chart image blocks at the newest PNGs."""
    repo = os.environ.get("GITHUB_REPOSITORY", "Knee-Surgery/trade-journal")
    base = f"https://raw.githubusercontent.com/{repo}/main/charts"
    v = int(time.time())
    def walk(block_id, depth=0):
        r = requests.get(f"https://api.notion.com/v1/blocks/{block_id}/children",
                         headers=NOTION, params={"page_size": 100}, timeout=30)
        r.raise_for_status()
        for b in r.json()["results"]:
            t = b["type"]
            if t == "callout":
                first = "".join(x["plain_text"] for x in b["callout"]["rich_text"])
                if first.startswith("Dashboard summary"):
                    requests.patch(f"https://api.notion.com/v1/blocks/{b['id']}", headers=NOTION, timeout=30,
                                   json={"callout": {"rich_text": [{"text": {"content": kpi_text}}]}}).raise_for_status()
            elif t == "image":
                cap = "".join(x["plain_text"] for x in b["image"].get("caption", []))
                key = cap.strip().split(":", 1)[-1] if cap.startswith("chart:") else None
                if key in names:
                    requests.patch(f"https://api.notion.com/v1/blocks/{b['id']}", headers=NOTION, timeout=30,
                                   json={"image": {"external": {"url": f"{base}/{names[key]}?v={v}"}}}).raise_for_status()
            if depth < 3 and t in ("column_list", "column"):
                walk(b["id"], depth + 1)
    walk(PAGE_ID)


def sync_history(events, closed):
    """Append new rows to Trade Events, Wins/Losses and Closed Trades (keyed by tx), then self-heal names."""
    # events (append-only, keyed by signature); exits missing a PnL % get it filled in
    by_sig = {e["sig"]: e for e in events}
    have = set()
    for p in n_query(DB_EVENTS):
        sig = plain(p["properties"]["Tx Signature"], "rich_text")
        have.add(sig)
        e = by_sig.get(sig)
        if e and e["type"] == "Exit" and e["cost"] and (p["properties"].get("PnL %") or {}).get("number") is None:
            n_patch(p["id"], {"PnL %": num(e["pnl"] / e["cost"])})
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
            "PnL %": num(e["pnl"] / e["cost"] if e["type"] == "Exit" and e["cost"] else None),
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
    rename_pass(events)


def sync_open(open_pos):
    """Update Open Trades in place with live prices: patch existing rows, add new ones, archive sold ones.
    Returns (rows for charts, unrealised PnL, cost of priced positions, open value)."""
    open_pos = reconcile(open_pos)
    px = prices_now(open_pos.keys())
    existing, dupes = {}, []
    for p in n_query(DB_OPEN):
        m = plain(p["properties"]["Mint"], "rich_text")
        if m and m not in existing:
            existing[m] = p
        else:
            dupes.append(p)
    for m in list(open_pos):                      # holdings with no trade history: only keep real ones
        p = open_pos[m]
        if p.get("untracked"):
            now = px.get(m)
            if not now or now * p["qty"] < 0.10:
                del open_pos[m]; continue
            row = existing.get(m)
            first = row["properties"]["Entry Price (USD)"].get("number") if row else None
            if first:                             # keep the price from when it was first seen
                p["cost"] = first * p["qty"]
                start = (row["properties"]["Entry Time"].get("date") or {}).get("start")
                if start: p["entry_ts"] = int(datetime.fromisoformat(start).timestamp())
            else:
                p["cost"] = now * p["qty"]        # true cost unknown: PnL starts from now
                print(f"untracked holding {symbol(m)}: {p['qty']:.4f} tokens = ${p['cost']:.2f}")
    unpriced = [symbol(m) for m in open_pos if m not in px]
    if unpriced:
        print("no price found for:", ", ".join(unpriced))
    unreal = cost_priced = open_val = 0.0
    rows = []
    for mint, p in open_pos.items():
        entry = p["cost"] / p["qty"]
        now = px.get(mint)
        val = now * p["qty"] if now else None
        upnl = val - p["cost"] if now else None
        if upnl is not None:
            unreal += upnl; cost_priced += p["cost"]; open_val += val
        rows.append(dict(name=symbol(mint), value=val or 0.0, cost=p["cost"], upnl=upnl or 0.0))
        props = {
            "Token": title(symbol(mint)), "Mint": text(mint), "Entry Time": date(p["entry_ts"]),
            "Entry Price (USD)": num(entry), "Size (Tokens)": num(p["qty"]),
            "Cost Basis (USD)": num(p["cost"]), "Current Price (USD)": num(now),
            "Current Value (USD)": num(val),
            "Unrealised PnL (USD)": num(upnl),
            "Unrealised PnL %": num(upnl / p["cost"] if upnl is not None and p["cost"] else None)}
        if p.get("entry_sig"):
            props["Entry Tx"] = {"url": solscan(p["entry_sig"])}
        if p.get("untracked"):
            props["Notes"] = text("Bought outside tracked history: cost basis set at the price when first seen")
        row = existing.pop(mint, None)
        if row: n_patch(row["id"], props)
        else: n_create(DB_OPEN, props)
    for row in list(existing.values()) + dupes:   # positions no longer held
        n_archive(row["id"])
    return rows, unreal, cost_priced, open_val


def commit_charts():
    """On GitHub Actions, push the refreshed PNGs so Notion's image links pick them up."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    import subprocess
    def git(*a): return subprocess.run(["git", *a], capture_output=True, text=True)
    git("add", "charts")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-m", "Update charts")
    for _ in range(3):
        git("pull", "--rebase", "-X", "theirs")
        r = git("push")
        if r.returncode == 0: return
        time.sleep(5)
    print("chart push failed:", r.stderr.strip())


def tick(state, charts):
    """One sync pass. Trade history is re-read only when the wallet has a new transaction."""
    try:
        sig = latest_signature()
    except Exception as ex:
        print("signature check failed:", repr(ex)); sig = state.get("sig")
    fresh = "events" not in state or sig != state.get("sig")
    if fresh:
        new = fetch_swaps({t["signature"] for t in state.get("txs", [])})
        state["txs"] = sorted(state.get("txs", []) + new, key=lambda t: t["timestamp"])
        print(f"{len(new)} new txs ({len(state['txs'])} total)")
        state["events"], state["closed"], state["open"] = to_trades(state["txs"])
        sync_history(state["events"], state["closed"])
        state["flows"] = flows(state["txs"])
        state["sig"] = sig
    events = state["events"]
    rows, unreal, cost_priced, open_val = sync_open(state["open"])
    fl = state["flows"]
    try:
        cash = wallet_cash()
    except Exception as ex:
        print("cash check failed:", repr(ex)); cash = None

    exits = [e for e in events if e["type"] == "Exit"]
    wins = sum(e["pnl"] for e in exits if e["pnl"] > 0)
    losses = -sum(e["pnl"] for e in exits if e["pnl"] <= 0)
    net = wins - losses
    exit_cost = sum(e["cost"] for e in exits)
    net_pct = net / exit_cost if exit_cost else None
    unreal_pct = unreal / cost_priced if cost_priced else None
    total_pct = (net + unreal) / (exit_cost + cost_priced) if exit_cost + cost_priced else None
    update_breakeven(net, wins, losses, unreal, net_pct, unreal_pct)

    names = {}
    if charts or fresh:                           # chart images: on new trades and every CHART_MINUTES
        try:
            make_charts(events, rows, symbol, "charts")
            import shutil   # dated daily snapshot: the last run of each day is the one kept
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            os.makedirs(f"charts/history/{day}", exist_ok=True)
            for f in ("cumulative", "gainers", "losers", "open"):
                shutil.copy(f"charts/{f}.png", f"charts/history/{day}/{f}.png")
            commit_charts()
            names = {k: f"{k}.png" for k in ("cumulative", "gainers", "losers", "open")}
        except Exception as ex:
            print("chart step skipped:", repr(ex))
    n_exits = len(exits)
    wr = (sum(1 for e in exits if e["pnl"] > 0) / n_exits * 100) if n_exits else 0
    kpi = (f"Dashboard summary | Net realised PnL {money(net)} ({pct(net_pct)}) | Total won {money(wins)} | "
           f"Total lost {money(-losses)} | Win rate {wr:.0f}% ({n_exits} exits) | "
           f"Unrealised PnL {money(unreal)} ({pct(unreal_pct)}) | "
           f"Total PnL {money(net + unreal)} ({pct(total_pct)}) | "
           f"Open value {money(open_val)} across {len(rows)} positions | ")
    if cash is not None:
        value = open_val + cash
        wpnl = value + fl["out"] - fl["in"]
        kpi += (f"Wallet PnL (value now + withdrawn - deposited) {money(wpnl)} "
                f"({pct(wpnl / fl['in'] if fl['in'] else None)}) | Wallet value {money(value)} "
                f"(cash {money(cash)}) | Deposited {money(fl['in'])} | Withdrawn {money(fl['out'])} | "
                f"With 77p7 wallet: received {money(fl['other_in'])}, sent {money(fl['other_out'])} | ")
        print(f"wallet: value={value:.2f} cash={cash:.2f} flows={fl} pnl={wpnl:.2f}")
    kpi += (f"Updated {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M:%S UTC')}")
    try:
        refresh_page(kpi, names)
    except Exception as ex:
        print("summary update skipped:", repr(ex))
    print(f"{datetime.now(timezone.utc):%H:%M:%S} events={len(events)} open={len(rows)} "
          f"net_realised={net:.2f} ({pct(net_pct)}) unrealised={unreal:.2f} ({pct(unreal_pct)})")


_code_check = {"at": time.time(), "start": None}
def code_changed():
    """On Actions, every 5 minutes: has the script or workflow changed on main? Then end early so
    the next run (started by the workflow) uses the new code."""
    if not os.environ.get("GITHUB_ACTIONS") or time.time() - _code_check["at"] < 300:
        return False
    _code_check["at"] = time.time()
    import subprocess
    if not _code_check["start"]:   # the commit this run started from (HEAD moves when charts are pushed)
        _code_check["start"] = os.environ.get("GITHUB_SHA") or "HEAD"
    if subprocess.run(["git", "fetch", "-q", "origin", "main"], capture_output=True).returncode:
        return False
    changed = subprocess.run(["git", "diff", "--quiet", _code_check["start"], "origin/main", "--",
                              "sync_journal.py", ".github"]).returncode != 0
    if changed: print("new code on main: ending this run so the next one picks it up")
    return changed


def main():
    if not HELIUS_KEY or not os.environ.get("NOTION_TOKEN"):
        sys.exit("Set HELIUS_API_KEY and NOTION_TOKEN")
    loop_min = float(os.environ.get("LOOP_MINUTES", "0"))      # 0 = run once
    every = float(os.environ.get("EVERY_SECONDS", "60"))
    chart_every = float(os.environ.get("CHART_MINUTES", "15")) * 60
    end = time.time() + loop_min * 60
    state, last_chart, fails = {}, 0.0, 0
    while True:
        started = time.time()
        charts = started - last_chart >= chart_every
        try:
            tick(state, charts)
            fails = 0
            if charts: last_chart = started
        except Exception as ex:                    # network blips must not end a long run
            fails += 1
            print("sync pass failed:", repr(ex))
            if not loop_min or fails >= 10: raise
        if time.time() + every > end or code_changed():
            break
        time.sleep(max(0.0, every - (time.time() - started)))
    if loop_min and time.time() - last_chart > 60:  # leave fresh charts behind for the next run
        try: tick(state, True)
        except Exception as ex: print("final pass failed:", repr(ex))


if __name__ == "__main__":
    main()
