import os, json, re, math, datetime as dt, urllib.parse
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
import requests

LEDGER_URL = os.environ.get("LEDGER_URL", "")
_p = urllib.parse.urlparse(LEDGER_URL)
BASE = f"{_p.scheme}://{_p.netloc}{_p.path}"
BASE_QS = dict(urllib.parse.parse_qsl(_p.query))
IST = ZoneInfo("Asia/Kolkata")
UTC = dt.timezone.utc
_cache = {}


# ---------------------------------------------------------------- fetching
def get(path="/", **params):
    key = (path, tuple(sorted(params.items())))
    if key in _cache:
        return _cache[key]
    qs = dict(BASE_QS); qs["path"] = path; qs.update(params)
    last = None
    for _ in range(3):
        try:
            r = requests.get(BASE, params=qs, timeout=8)
            r.raise_for_status()
            try: val = r.json()
            except Exception: val = r.text
            _cache[key] = val
            return val
        except Exception as e:
            last = e
    raise last


def rows_of(resp):
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("orders", "data", "items", "results", "rows", "records"):
            if isinstance(resp.get(k), list):
                return resp[k]
        for v in resp.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def all_orders():
    if "ALL" in _cache:
        return _cache["ALL"]
    out, seen, page = [], set(), 1
    done = False
    while not done and page < 500:
        pages = list(range(page, page + 8))
        with ThreadPoolExecutor(8) as ex:
            results = list(ex.map(lambda p: rows_of(get("/orders", page=p)), pages))
        for rows in results:
            if not rows:
                done = True; break
            sig = json.dumps(rows[0], sort_keys=True, default=str)
            if sig in seen:
                done = True; break
            seen.add(sig)
            out.extend(rows)
        page += 8
    _cache["ALL"] = out
    return out


# ---------------------------------------------------------------- helpers
def norm(k): return re.sub(r"[^a-z0-9]", "", str(k).lower())


def pick(row, cands):
    m = {norm(k): k for k in row}
    for c in cands:
        if c in m and row[m[c]] not in (None, ""):
            return row[m[c]]
    return None


def D(x):
    try:
        if isinstance(x, str):
            x = re.sub(r"[,$\s]", "", x)
        return Decimal(str(x))
    except Exception:
        return None


def money(d):
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_dt(v):
    if v is None: return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d{10,13}(\.\d+)?", v)):
            x = float(v)
            if x > 1e11: x /= 1000
            return dt.datetime.fromtimestamp(x, UTC)
        s = str(v).strip().replace("Z", "+00:00")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s): s += "T00:00:00"
        d = dt.datetime.fromisoformat(s.replace(" ", "T", 1) if " " in s and "T" not in s else s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d
    except Exception:
        for f in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%b %d, %Y", "%B %d, %Y"):
            try: return dt.datetime.strptime(str(v), f).replace(tzinfo=UTC)
            except Exception: pass
    return None


# ---------------------------------------------------------------- rates
def build_rates():
    if "RATES" in _cache:
        return _cache["RATES"]
    raw = get("/rates")
    text = json.dumps(raw).lower()
    table = {}   # cur -> list of (date or None, Decimal)

    def add(cur, rate, date=None):
        r = D(rate)
        if cur and r and r > 0:
            table.setdefault(str(cur).upper(), []).append((parse_dt(date) if date else None, r))

    def walk(o, date=None):
        if isinstance(o, dict):
            if all(isinstance(v, (int, float, str)) and D(v) is not None for v in o.values()) and \
               all(re.fullmatch(r"[A-Za-z]{3}", str(k)) for k in o):
                for k, v in o.items(): add(k, v, date)
                return
            for k, v in o.items():
                if re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", str(k)) and isinstance(v, dict):
                    walk(v, k)
                else:
                    walk(v, date)
        elif isinstance(o, list):
            for e in o:
                if isinstance(e, dict):
                    cur = pick(e, ["currency", "code", "ccy", "from", "symbol"])
                    rate = pick(e, ["rate", "value", "usdrate", "rateusd", "perusd", "tousd", "usdper"])
                    d = pick(e, ["date", "effectivedate", "asof", "month", "validfrom", "from_date"])
                    if cur and rate is not None:
                        add(cur, rate, d)
                    else:
                        walk(e, date)
    walk(raw)

    # direction: True => value is "units of currency per 1 USD" (divide)
    per_usd = True
    if any(h in text for h in ("to_usd", "tousd", "usd_per", "usdper", "usd_value")):
        per_usd = False
    elif any(h in text for h in ("per_usd", "perusd", "from_usd", "fromusd")):
        per_usd = True
    else:
        probe = {c: v[-1][1] for c, v in table.items()}
        if "INR" in probe: per_usd = probe["INR"] > 5
        elif "JPY" in probe: per_usd = probe["JPY"] > 5
        elif "EUR" in probe: per_usd = probe["EUR"] < 1.0
    for c in table: table[c].sort(key=lambda t: (t[0] is not None, t[0] or dt.datetime.min.replace(tzinfo=UTC)))
    res = (table, per_usd)
    _cache["RATES"] = res
    return res


def to_usd(amount, cur, when):
    if amount is None: return None
    if not cur or str(cur).upper() == "USD": return amount
    table, per_usd = build_rates()
    lst = table.get(str(cur).upper())
    if not lst: return amount
    rate = lst[-1][1]
    dated = [t for t in lst if t[0] is not None]
    if dated and when is not None:
        ok = [t for t in dated if t[0] <= when]
        rate = (ok[-1] if ok else dated[0])[1]
    return amount / rate if per_usd else amount * rate


# ---------------------------------------------------------------- normalize
ID = ["orderid", "id", "order", "ordernumber", "orderno"]
UPD = ["updatedat", "modifiedat", "lastupdated", "updated", "updatedon"]
DATE = ["orderdate", "createdat", "orderedat", "placedat", "date", "timestamp", "paidat", "created", "createdon", "ordertime"]
STATUS = ["status", "orderstatus", "state"]
REGION = ["region", "salesregion", "territory", "area"]
PRODUCT = ["product", "productname", "item", "itemname", "sku", "productid"]
CUST = ["customername", "customer", "customerid", "customeremail", "client", "buyer"]
CUR = ["currency", "currencycode", "ccy"]
AMT_USD = ["amountusd", "usdamount", "totalusd", "priceusd", "revenueusd"]
AMT = ["amount", "total", "totalamount", "grandtotal", "netamount", "revenue", "value", "price", "subtotal", "ordertotal"]
AMT_C = ["amountcents", "totalcents", "pricecents"]
QTY = ["quantity", "qty", "units", "count"]
UNIT = ["unitprice", "priceperunit", "unitcost"]
REF = ["refundamount", "refundedamount", "refund", "refundtotal", "amountrefunded", "refundusd"]


def normalize():
    if "NORM" in _cache:
        return _cache["NORM"]
    latest = {}
    for i, r in enumerate(all_orders()):
        if not isinstance(r, dict): continue
        oid = pick(r, ID)
        key = oid if oid is not None else ("row", i)
        u = parse_dt(pick(r, UPD)) or dt.datetime.min.replace(tzinfo=UTC)
        if key not in latest or u >= latest[key][0]:
            latest[key] = (u, r)
    out = []
    for u, r in latest.values():
        when = parse_dt(pick(r, DATE))
        cur = pick(r, CUR)
        qty = D(pick(r, QTY)) if pick(r, QTY) is not None else None
        a = pick(r, AMT_USD)
        if a is not None:
            usd = D(a)
        else:
            a = pick(r, AMT); amt = D(a) if a is not None else None
            if amt is None and pick(r, AMT_C) is not None:
                amt = D(pick(r, AMT_C)) / 100
            if amt is None and pick(r, UNIT) is not None and qty is not None:
                amt = D(pick(r, UNIT)) * qty
            usd = to_usd(amt, cur, when)
        rf = pick(r, REF)
        status = str(pick(r, STATUS) or "").strip().lower()
        refund = None
        if rf is not None and D(rf) is not None:
            refund = to_usd(D(rf), cur, when)
        elif "refund" in status:
            refund = usd
        out.append({
            "status": status,
            "when": when.astimezone(IST) if when else None,
            "region": pick(r, REGION), "product": pick(r, PRODUCT), "customer": pick(r, CUST),
            "usd": usd, "refund": refund, "qty": qty,
            "refunded_flag": ("refund" in status) or (refund is not None and refund > 0),
        })
    _cache["NORM"] = out
    return out


# ---------------------------------------------------------------- question parsing
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}
MABBR = {k[:3]: v for k, v in MONTHS.items()}


def parse_period(q):
    ql = q.lower()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", ql)
    if m:
        d = dt.date(int(m[1]), int(m[2]), int(m[3])); return (d, d)
    m = re.search(r"\b(\d{4})-(\d{2})\b", ql)
    if m: return month_range(int(m[1]), int(m[2]))
    m = re.search(r"\bq([1-4])\s*(?:of\s*)?(\d{4})\b", ql)
    if m:
        s = (int(m[1]) - 1) * 3 + 1
        return (dt.date(int(m[2]), s, 1), month_range(int(m[2]), s + 2)[1])
    names = "|".join(list(MONTHS) + list(MABBR))
    m = re.search(rf"\b({names})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", ql)
    if m:
        mo = MONTHS.get(m[1]) or MABBR[m[1][:3]]; d = dt.date(int(m[3]), mo, int(m[2])); return (d, d)
    m = re.search(rf"\b({names})\.?,?\s+(\d{{4}})\b", ql)
    if m:
        mo = MONTHS.get(m[1]) or MABBR[m[1][:3]]; return month_range(int(m[2]), mo)
    m = re.search(r"\b(20\d{2})\b", ql)
    if m: return (dt.date(int(m[1]), 1, 1), dt.date(int(m[1]), 12, 31))
    return None


def month_range(y, mo):
    first = dt.date(y, mo, 1)
    nxt = dt.date(y + (mo == 12), (mo % 12) + 1, 1)
    return (first, nxt - dt.timedelta(days=1))


def match_entity(q, values):
    ql = " " + re.sub(r"[^a-z0-9 ]", " ", q.lower()) + " "
    best = None
    for v in {x for x in values if x}:
        vv = " " + re.sub(r"[^a-z0-9 ]", " ", str(v).lower()).strip() + " "
        if vv.strip() and vv in ql and (best is None or len(str(v)) > len(str(best))):
            best = v
    return best


def answer(question):
    q = question.strip()
    ql = q.lower()
    data = normalize()
    period = parse_period(q)
    region = match_entity(q, [d["region"] for d in data])
    product = match_entity(q, [d["product"] for d in data])
    customer = match_entity(q, [d["customer"] for d in data])

    refund_q = "refund" in ql
    metric = "refund" if refund_q else "revenue"
    if re.search(r"\b(units|quantity|pieces|sold)\b", ql) and not re.search(r"revenue|earn|sales", ql):
        metric = "qty"
    count_q = bool(re.search(r"how many|number of|count of|\bcount\b", ql))
    avg_q = bool(re.search(r"average|avg|mean", ql))

    def keep(d, need_paid):
        if need_paid and d["status"] != "paid": return False
        if period:
            if not d["when"]: return False
            day = d["when"].date()
            if not (period[0] <= day <= period[1]): return False
        if region and str(d["region"]).lower() != str(region).lower(): return False
        if product and str(d["product"]).lower() != str(product).lower(): return False
        if customer and str(d["customer"]).lower() != str(customer).lower(): return False
        return True

    need_paid = not refund_q
    rows = [d for d in data if keep(d, need_paid)]
    if refund_q:
        rows = [d for d in rows if d["refunded_flag"]]

    def val(d):
        if metric == "refund": return d["refund"] or Decimal(0)
        if metric == "qty": return d["qty"] or Decimal(0)
        return d["usd"] or Decimal(0)

    # which / top / list intent
    group = None
    if re.search(r"\b(which|what|top|best|highest|lowest|most|least|who)\b.*\b(product|item)s?\b", ql) or "best-selling" in ql or "best selling" in ql:
        group = "product"
    elif re.search(r"\b(which|what|top|best|highest|lowest|most|least)\b.*\bregion", ql):
        group = "region"
    elif re.search(r"\b(who|which|top|best)\b.*\b(customer|client|buyer)s?\b", ql) or ql.startswith("who"):
        group = "customer"
    if group and not re.search(r"how many|number of", ql):
        agg = {}
        for d in rows:
            k = d[group]
            if k is None: continue
            if re.search(r"most orders|number of orders|order count|most frequent", ql):
                agg[k] = agg.get(k, 0) + 1
            else:
                agg[k] = agg.get(k, Decimal(0)) + val(d)
        if not agg: return None
        low = bool(re.search(r"lowest|least|fewest|smallest|worst|minimum|min\b", ql))
        ordered = sorted(agg.items(), key=lambda kv: (kv[1], str(kv[0])) if low else (-kv[1], str(kv[0])))
        m = re.search(r"\btop\s+(\d+)|\b(\d+)\s+(?:best|highest|top)", ql)
        if m:
            n = int(m[1] or m[2]); return [k for k, _ in ordered[:n]]
        if re.search(r"\blist\b|\ball\b", ql): return [k for k, _ in ordered]
        return ordered[0][0]

    # distinct counts
    if count_q:
        for col, pat in (("customer", r"customers?|clients?|buyers?"), ("product", r"products?|items?"), ("region", r"regions?")):
            if re.search(rf"how many (?:distinct |unique |different )?(?:{pat})", ql):
                return len({d[col] for d in rows if d[col] is not None})
        return len(rows)

    total = sum((val(d) for d in rows), Decimal(0))
    if avg_q:
        return money(total / len(rows)) if rows else 0
    if metric == "qty":
        return int(total) if total == total.to_integral() else float(total)
    return money(total)


# ---------------------------------------------------------------- HTTP
def debug_info():
    info = {}
    for name, fn in (("root", lambda: get("/")), ("rates", lambda: get("/rates")),
                     ("export", lambda: get("/export")),
                     ("orders_page1", lambda: get("/orders", page=1))):
        try:
            v = fn(); s = json.dumps(v, default=str)
            info[name] = s[:1500]
        except Exception as e:
            info[name] = f"ERROR {e}"
    try:
        o = all_orders()
        info["total_raw_rows"] = len(o)
        n = normalize()
        info["unique_orders"] = len(n)
        info["statuses"] = sorted({d["status"] for d in n})
        info["regions"] = sorted({str(d["region"]) for d in n})[:30]
        info["products"] = sorted({str(d["product"]) for d in n})[:50]
        info["sample_normalized"] = [{k: str(v) for k, v in d.items()} for d in n[:3]]
        info["rates_direction_per_usd"] = build_rates()[1]
    except Exception as e:
        info["normalize_error"] = f"{type(e).__name__}: {e}"
    return info


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        try:
            if not LEDGER_URL:
                return self._send(200, {"error": "LEDGER_URL env var missing"})
            if "q" in qs:
                return self._send(200, {"answer": answer(qs["q"])})
            if "debug" in qs:
                return self._send(200, debug_info())
            self._send(200, {"status": "ok", "LEDGER_URL_set": True})
        except Exception as e:
            self._send(200, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, {"answer": answer(data.get("question", ""))})
        except Exception as e:
            self._send(200, {"answer": None, "error": f"{type(e).__name__}: {e}"})
