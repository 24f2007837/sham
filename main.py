import os, json, time, io, contextlib, traceback, urllib.parse, collections
import datetime, decimal, statistics, re, threading
import requests
from http.server import BaseHTTPRequestHandler

LEDGER_URL = os.environ["LEDGER_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
DEADLINE = 13.0

_p = urllib.parse.urlparse(LEDGER_URL)
BASE = f"{_p.scheme}://{_p.netloc}{_p.path}"
BASE_QS = dict(urllib.parse.parse_qsl(_p.query))
HOST = _p.netloc

_cache = {}
_lock = threading.Lock()


def _fetch(url, params=None):
    key = (url, tuple(sorted((params or {}).items())))
    if key in _cache:
        return _cache[key]
    last = None
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=8)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            try:
                val = r.json()
            except Exception:
                val = r.text
            _cache[key] = val
            return val
        except Exception as e:
            last = e
    raise last


def get(path="/", **params):
    qs = dict(BASE_QS)
    qs["path"] = path
    qs.update(params)
    return _fetch(BASE, qs)


def raw_get(url):
    if urllib.parse.urlparse(url).netloc != HOST:
        raise ValueError("Only the ledger host is allowed")
    return _fetch(url)


def _rows(resp):
    """Find the list of records inside a response."""
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
    """Every raw order row from every page (not deduplicated)."""
    if "ALL_ORDERS" in _cache:
        return _cache["ALL_ORDERS"]
    out, seen_first = [], set()
    for page in range(1, 400):
        resp = get("/orders", page=page)
        rows = _rows(resp)
        if not rows:
            break
        sig = json.dumps(rows[0], sort_keys=True, default=str)
        if sig in seen_first:
            break
        seen_first.add(sig)
        out.extend(rows)
        if isinstance(resp, dict):
            more = resp.get("has_more", resp.get("hasMore"))
            nxt = resp.get("next") or resp.get("next_page") or resp.get("nextPage")
            tp = resp.get("total_pages") or resp.get("totalPages") or resp.get("pages")
            if more is False or (more is None and nxt in (None, "", False) and
                                 ("next" in resp or "next_page" in resp or "nextPage" in resp)):
                break
            if tp and page >= int(tp):
                break
    _cache["ALL_ORDERS"] = out
    return out


def _warm():
    try:
        get("/")
        get("/rates")
        all_orders()
    except Exception:
        pass


threading.Thread(target=_warm, daemon=True).start()


def _short(x, n):
    s = x if isinstance(x, str) else json.dumps(x, default=str)
    return s[:n]


def context_text():
    parts = []
    try:
        parts.append("ROOT (get('/')):\n" + _short(get("/"), 1500).replace(LEDGER_URL.split("&path")[0], "<URL>"))
    except Exception as e:
        parts.append(f"root error {e}")
    try:
        parts.append("RATES (get('/rates')):\n" + _short(get("/rates"), 2500))
    except Exception as e:
        parts.append(f"rates error {e}")
    try:
        p1 = get("/orders", page=1)
        if isinstance(p1, dict):
            meta = {k: v for k, v in p1.items() if not isinstance(v, list)}
            parts.append("ORDERS page-1 envelope: " + _short(meta, 600))
        rows = _rows(p1)
        parts.append(f"ORDERS page-1 has {len(rows)} rows; first 3:\n" + _short(rows[:3], 1800))
    except Exception as e:
        parts.append(f"orders error {e}")
    try:
        parts.append("EXPORT (get('/export')) sample:\n" + _short(get("/export"), 1200))
    except Exception as e:
        parts.append(f"export error {e}")
    return "\n\n".join(parts)


SYSTEM = """You answer questions about Acme Appliances' order ledger (revenue, refunds, products, customers).
You have a Python tool `run_python`. Helpers available in it:
  all_orders() -> list of ALL raw order rows from all pages (cached, instant, NOT deduplicated)
  get(path, **params) -> parsed JSON/text from a ledger path e.g. get('/rates'), get('/export'), get('/orders', page=2)
  raw_get(url) -> same for a full URL
Pre-imported: json, re, collections, datetime, decimal (Decimal), statistics, zoneinfo via datetime.
Variables persist between calls. print() results; output is truncated.

MANDATORY RULES from the ledger docs:
- Business dates use Asia/Kolkata. Convert every timestamp to that timezone (zoneinfo.ZoneInfo('Asia/Kolkata')) BEFORE deciding day/month/year. Parse ISO strings with a trailing Z correctly.
- An order id can appear multiple times; the row with the LATEST updated_at is the current one. Deduplicate by id FIRST (compare parsed datetimes), then filter.
- Only orders with status 'paid' (after dedupe) count as revenue.
- Money is USD to the cent. If rows are in other currencies, convert with /rates exactly as that endpoint describes (check its direction: units per USD vs USD per unit). Use Decimal and round half up to 2 dp at the end (or per the data's own rounding if rates docs say so).
- Match region/product/customer text case-insensitively and whitespace-trimmed, but return product names exactly as the API spells them.
- Refund questions: inspect the fields/statuses for refunds (e.g. status values like 'refunded', refund amount fields) and any /export differences; decide by the data.

Workflow: first call run_python ONCE to inspect (print distinct values of status/currency/region fields, key names, counts, sample of duplicates, date formats). Then compute and call submit_answer. If the question is simple and field names are clear from the samples, compute directly in the first call and print the result, then submit. You have about 10 seconds total, so be quick, with at most 3 tool calls.

submit_answer(answer_json): JSON value. Number for numeric answers (money with 2 decimals), string for names, list for lists, boolean for yes/no. No units or extra text.

=== LEDGER CONTEXT ===
{ctx}
"""

TOOLS = [
    {"name": "run_python", "description": "Run Python; returns stdout.",
     "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "submit_answer", "description": "Submit final answer as JSON-encoded value.",
     "input_schema": {"type": "object", "properties": {"answer_json": {"type": "string"}}, "required": ["answer_json"]}},
]


def run_code(code, ns):
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(code, ns)
    except Exception:
        buf.write("\nERROR:\n" + traceback.format_exc()[-1500:])
    out = buf.getvalue()
    return out[:5000] + ("\n...[truncated]" if len(out) > 5000 else "")


def parse_answer(s):
    try:
        v = json.loads(s)
    except Exception:
        return s
    if isinstance(v, float):
        v = round(v, 2)
    return v


def llm(messages, system, remaining):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 3000, "system": system,
              "tools": TOOLS, "messages": messages},
        timeout=max(3, remaining))
    r.raise_for_status()
    return r.json()


_ctx = {}


def answer_question(question):
    t0 = time.time()
    if "t" not in _ctx or time.time() - _ctx["t"] > 600:
        _ctx["v"] = context_text()
        _ctx["t"] = time.time()
    system = SYSTEM.format(ctx=_ctx["v"])
    ns = {"get": get, "raw_get": raw_get, "all_orders": all_orders, "json": json, "re": re,
          "collections": collections, "datetime": datetime, "decimal": decimal,
          "Decimal": decimal.Decimal, "statistics": statistics}
    import zoneinfo
    ns["zoneinfo"] = zoneinfo
    messages = [{"role": "user", "content": f"Question: {question}"}]
    for step in range(5):
        remaining = DEADLINE - (time.time() - t0)
        if remaining < 2:
            break
        resp = llm(messages, system, remaining)
        messages.append({"role": "assistant", "content": resp["content"]})
        results = []
        for block in resp["content"]:
            if block["type"] != "tool_use":
                continue
            if block["name"] == "submit_answer":
                return parse_answer(block["input"].get("answer_json", "null"))
            out = run_code(block["input"].get("code", ""), ns)
            results.append({"type": "tool_result", "tool_use_id": block["id"], "content": out or "(no output)"})
        if not results:
            messages.append({"role": "user", "content": "Call submit_answer now."})
        else:
            messages.append({"role": "user", "content": results})
    return None


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
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
        self._send(200, {"status": "ok", "usage": 'POST {"question": "..."}'})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, {"answer": answer_question(data.get("question", ""))})
        except Exception as e:
            self._send(200, {"answer": None, "error": str(e)})
