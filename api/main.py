import os, json, re, datetime as dt, urllib.parse
from collections import Counter
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
_cache = {}


# ------------------------------------------------------------ data loading
def get(path, **params):
    qs = dict(BASE_QS); qs["path"] = path; qs.update(params)
    last = None
    for _ in range(3):
        try:
            r = requests.get(BASE, params=qs, timeout=8)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
    raise last


def pdt(s):
    d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def load():
    if "data" in _cache:
        return _cache["data"]
    rates = get("/rates")["usd_per_unit"]          # USD value of one unit -> multiply
    first = get("/orders", page=1)
    pages = int(first.get("pages", 1))
    raw = list(first["orders"])
    if pages > 1:
        with ThreadPoolExecutor(12) as ex:
            for rows in ex.map(lambda p: get("/orders", page=p)["orders"], range(2, pages + 1)):
                raw += rows
    latest = {}                                     # dedupe: latest updated_at wins
    for r in raw:
        u = pdt(r["updated_at"])
        k = r["id"]
        if k not in latest or u >= latest[k][0]:
            latest[k] = (u, r)
    data = []
    for u, r in latest.values():
        rate = Decimal(str(rates.get(r.get("currency"), 1)))
        data.append({
            "id": r["id"],
            "status": str(r.get("status", "")).strip().lower(),
            "when": pdt(r["created_at"]).astimezone(IST),   # business dates in IST
            "region": str(r.get("region", "")).strip(),
            "product": str(r.get("product", "")).strip(),
            "customer": str(r.get("customer", "")).strip(),
            "qty": int(r.get("qty") or 0),
            "usd": Decimal(str(r["amount"])) * rate,
        })
    _cache["data"] = data
    _cache["raw_rows"] = len(raw)
    return data


def money(d):
    return float(Decimal(d).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ------------------------------------------------------------ date parsing
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
MIDX = {m: i + 1 for i, m in enumerate(MONTHS)}
MIDX.update({m[:3]: i + 1 for i, m in enumerate(MONTHS)})
MIDX["sept"] = 9
MN = "|".join(sorted(MIDX, key=len, reverse=True))


def month_range(y, m):
    a = dt.date(y, m, 1)
    b = dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
    return a, b


def parse_period(ql):
    iso = re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", ql)
    if len(iso) >= 2:
        a = dt.date(*map(int, iso[0])); b = dt.date(*map(int, iso[1]))
        return (min(a, b), max(a, b))
    if len(iso) == 1:
        d = dt.date(*map(int, iso[0])); return (d, d)
    m = re.search(r"\b(\d{4})-(\d{2})\b", ql)
    if m:
        return month_range(int(m[1]), int(m[2]))
    m = re.search(r"\bq([1-4])\b\s*(?:of\s*)?(\d{4})", ql) or re.search(r"\b(\d{4})\s*q([1-4])\b", ql)
    if m:
        if len(m[1]) == 4: y, q = int(m[1]), int(m[2])
        else: q, y = int(m[1]), int(m[2])
        return month_range(y, 3 * q - 2)[0], month_range(y, 3 * q)[1]
    m = re.search(rf"\b({MN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", ql)
    if m:
        d = dt.date(int(m[3]), MIDX[m[1]], int(m[2])); return (d, d)
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MN})\.?,?\s+(\d{{4}})\b", ql)
    if m:
        d = dt.date(int(m[3]), MIDX[m[2]], int(m[1])); return (d, d)
    m = re.search(rf"\b({MN})\b\.?,?\s*(?:of\s*)?(\d{{4}})\b", ql)
    if m:
        return month_range(int(m[2]), MIDX[m[1]])
    m = re.search(r"\b(20\d{2})\b", ql)
    if m:
        y = int(m[1]); return dt.date(y, 1, 1), dt.date(y, 12, 31)
    m = re.search(rf"\b(?:in|during|for)\s+({MN})\b", ql)
    if m:
        return ("month", MIDX[m[1]])
    return None


def find_entity(ql, values):
    for v in sorted(values, key=len, reverse=True):
        if v and re.search(rf"(?<![a-z0-9]){re.escape(v.lower())}(?:es|s)?(?![a-z0-9])", ql):
            return v
    return None


# ------------------------------------------------------------ answering
SUP = r"(?:most|highest|top|best|largest|biggest|greatest|leading|lowest|least|fewest|smallest|worst|max(?:imum)?|min(?:imum)?)"
NOUN = r"(product|item|region|customer|client|buyer)"


def answer(question):
    ql = question.lower().strip()
    data = load()
    period = parse_period(ql)
    region = find_entity(ql, {d["region"] for d in data})
    product = find_entity(ql, {d["product"] for d in data})
    m = re.search(r"\bc\d{3,}\b", ql)
    customer = m[0].upper() if m else None

    refund_q = "refund" in ql
    wants_units = bool(re.search(r"\b(units|quantity|quantities|pieces)\b", ql))
    wants_orders = bool(re.search(r"\borders?\b", ql))
    money_words = bool(re.search(r"dollar|usd|\$|revenue|worth|value|amount|sales|selling|earn|spent|spend|income|total", ql))

    status_ok = (lambda d: d["status"] == "refunded") if refund_q else (lambda d: d["status"] == "paid")

    def in_period(d):
        if not period: return True
        day = d["when"].date()
        if period[0] == "month": return day.month == period[1]
        return period[0] <= day <= period[1]

    rows = [d for d in data if status_ok(d) and in_period(d)
            and (not region or d["region"].lower() == region.lower())
            and (not product or d["product"].lower() == product.lower())
            and (not customer or d["customer"].upper() == customer)]

    def val(d):
        return Decimal(d["qty"]) if wants_units else d["usd"]

    # ---- ranking: which product / region / customer is top / lowest ...
    gm = re.search(rf"\b{SUP}\b[\w\s'\-]{{0,40}}?\b{NOUN}s?\b", ql) or \
         re.search(rf"\b(?:which|what)\s+{NOUN}s?\b", ql)
    group = None
    if gm:
        group = {"item": "product", "client": "customer", "buyer": "customer"}.get(gm[1], gm[1])
    elif re.search(r"\bwho\b", ql):
        group = "customer"
    has_sup = re.search(rf"\b{SUP}\b|top-selling|best-selling|bestselling", ql)
    already_filtered = (group == "region" and region) or (group == "product" and product) or \
                       (group == "customer" and customer)
    if group and has_sup and not already_filtered and \
            not re.search(r"how many\s+(?:distinct\s+|unique\s+|different\s+)?(customers|products|regions)", ql):
        order_mode = bool(re.search(r"(largest|biggest|highest|smallest|lowest)\s+(single\s+)?(order|purchase)", ql))
        count_mode = bool(re.search(r"most orders|fewest orders|number of orders|order count|most purchases", ql)) \
            or (wants_orders and not money_words and not wants_units)
        agg = {}
        for d in rows:
            k = d[group]
            if order_mode:
                agg[k] = max(agg.get(k, Decimal(0)), d["usd"])
            else:
                agg[k] = agg.get(k, Decimal(0)) + (Decimal(1) if count_mode else val(d))
        if not agg:
            return None
        low = bool(re.search(r"lowest|least|fewest|smallest|worst|\bmin", ql))
        ordered = sorted(agg.items(), key=lambda kv: (kv[1] if low else -kv[1], kv[0]))
        t = re.search(r"\btop\s+(\d+)", ql)
        if t:
            return [k for k, _ in ordered[:int(t[1])]]
        return ordered[0][0]

    # ---- distinct counts
    m = re.search(r"how many\s+(?:distinct\s+|unique\s+|different\s+)?(customers|products|regions)", ql)
    if m:
        col = m[1][:-1]
        return len({d[col] for d in rows})

    # ---- average / median / max / min order
    if re.search(r"average|\bavg\b|\bmean\b", ql):
        if not rows: return 0
        return money(sum((val(d) for d in rows), Decimal(0)) / len(rows))
    if "median" in ql:
        vals = sorted(val(d) for d in rows)
        if not vals: return 0
        n = len(vals)
        return money(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2)
    if re.search(r"(largest|biggest|highest|maximum|\bmax\b).*\b(order|sale|purchase)", ql) and not wants_units:
        return money(max((d["usd"] for d in rows), default=Decimal(0)))
    if re.search(r"(smallest|lowest|minimum|\bmin\b).*\b(order|sale|purchase)", ql) and not wants_units:
        return money(min((d["usd"] for d in rows), default=Decimal(0)))

    # ---- order counts
    if re.search(r"how many\s+(?:\w+\s+){0,3}orders|number of\s+(?:\w+\s+){0,3}orders|count of\s+(?:\w+\s+){0,3}orders|how many .*refund", ql) \
            and not wants_units:
        return len(rows)

    total = sum((val(d) for d in rows), Decimal(0))
    if wants_units:
        return int(total)
    return money(total)


# ------------------------------------------------------------ HTTP
def debug_info():
    data = load()
    return {
        "raw_rows": _cache.get("raw_rows"), "unique_orders": len(data),
        "statuses": Counter(d["status"] for d in data),
        "regions": sorted({d["region"] for d in data}),
        "products": sorted({d["product"] for d in data}),
        "first_date": str(min(d["when"] for d in data)),
        "last_date": str(max(d["when"] for d in data)),
    }


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
            self._send(200, {"status": "ok"})
        except Exception as e:
            self._send(200, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, {"answer": answer(body.get("question", ""))})
        except Exception as e:
            self._send(200, {"answer": None, "error": f"{type(e).__name__}: {e}"})
