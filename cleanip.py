"""Super JinX clean IP: configs that connect to a chosen edge IP instead of the domain.
- Anyone with a subscription link can upload IP files on their own subscription page (personal list).
- The owner can upload a list for everyone (API Keys > Owner access > Clean IP).
- Every IP is really tested from the server: TLS with SNI = panel domain + GET /healthz must reach THIS service.
  IPs that do not route to us are never used. Lists are re-tested every 6 hours, dead IPs drop out.
- Extra subscription link: /sub/<token>/jx-ip  (original link stays untouched, so nothing can break)."""
import base64, hashlib, ipaddress, json, os, random, re, socket, ssl, threading, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

DATA = os.getenv("JINX_DATA", "/var/lib/pasarguard")
DIR = f"{DATA}/cleanip"
BASE = "http://127.0.0.1:8000"
SUB_PATH = (os.getenv("SUBSCRIPTION_PATH") or "sub").strip("/")
MAX_CANDIDATES = 1500      # tested per upload
MAX_KEEP = 40              # working IPs kept per list
PORTS = (443, 2053, 2083, 2087, 2096, 8443)
WORKERS = 48
TIMEOUT = 3.0
RETEST = 6 * 3600
LOCK = threading.Lock()
JOBS = {}
_get_domain = lambda: ""
_log = print

def init(get_domain, log):
    global _get_domain, _log
    _get_domain, _log = get_domain, log
    os.makedirs(DIR, exist_ok=True)
    threading.Thread(target=_retest_loop, daemon=True).start()

# ---------------- parsing uploads ----------------
_V4 = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}"
_TOK = re.compile(r"\[([0-9A-Fa-f:]{2,39})\](?::(\d{2,5}))?"                       # [v6]:port
                  r"|(?<![\w.:])(" + _V4 + r")(?:/(\d{1,2}))?(?::(\d{2,5}))?(?![\w.])"   # v4, v4/cidr, v4:port
                  r"|(?<![\w:])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})(?:/(\d{1,3}))?(?![\w:])")  # v6, v6/cidr

def parse(text, limit=MAX_CANDIDATES):
    """IPv4, IPv6, ip:port, [v6]:port, CIDR ranges, CSV/JSON lines from IP scanners. Private/odd IPs are dropped."""
    seen, out, ranges = set(), [], []
    for m in _TOK.finditer(str(text or "")[:600000]):
        if m.group(1): val, pre, port = m.group(1), None, m.group(2)
        elif m.group(3): val, pre, port = m.group(3), m.group(4), m.group(5)
        else: val, pre, port = m.group(6), m.group(7), None
        port = int(port or 443)
        if port not in PORTS: port = 443
        try:
            if pre:
                net = ipaddress.ip_network(f"{val}/{pre}", strict=False)
                if net.prefixlen < (16 if net.version == 4 else 112): continue
                ranges.append((net, port)); continue
            ip = ipaddress.ip_address(val)
        except ValueError:
            continue
        if not ip.is_global: continue
        k = (str(ip), port)
        if k not in seen: seen.add(k); out.append(k)
        if len(out) >= limit: return out
    # ranges: sample evenly so a /16 does not flood the tester
    for net, port in ranges:
        size = net.num_addresses
        n = min(size - 2 if size > 2 else size, 64)
        hosts = [net.network_address + random.randrange(1, max(2, size - 1)) for _ in range(n * 2)]
        for ip in hosts:
            k = (str(ip), port)
            if ipaddress.ip_address(ip).is_global and k not in seen:
                seen.add(k); out.append(k)
                n -= 1
                if n <= 0: break
    return out[:limit]

# ---------------- the real test ----------------
_CTX = ssl.create_default_context()

def probe(ip, port, domain):
    """ms if this IP serves OUR panel for `domain` (valid cert + our /healthz), else None"""
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    t0 = time.perf_counter()
    try:
        with socket.socket(fam, socket.SOCK_STREAM) as raw:
            raw.settimeout(TIMEOUT)
            raw.connect((ip, port))
            with _CTX.wrap_socket(raw, server_hostname=domain) as s:
                ms = int((time.perf_counter() - t0) * 1000)
                s.sendall(f"GET /healthz HTTP/1.1\r\nHost: {domain}\r\nUser-Agent: jinx-ip-test\r\nConnection: close\r\n\r\n".encode())
                buf = b""
                while len(buf) < 2048:
                    chunk = s.recv(1024)
                    if not chunk: break
                    buf += chunk
                    if b"\r\n\r\n" in buf and buf.split(b"\r\n\r\n", 1)[1][:2] == b"ok": break
                head, _, body = buf.partition(b"\r\n\r\n")
                if re.match(rb"HTTP/1\.[01] 200", head) and body.strip()[:2] == b"ok":
                    return ms
    except Exception:
        return None
    return None

def test_many(cands, domain, job=None):
    res = []
    def one(c):
        ms = probe(c[0], c[1], domain)
        if job is not None:
            with LOCK:
                job["tested"] += 1
                if ms is not None: job["ok"] += 1
        return (c, ms)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for (ip, port), ms in ex.map(one, cands):
            if ms is not None: res.append({"ip": ip, "port": port, "ms": ms})
    res.sort(key=lambda x: x["ms"])
    return res

def start_job(scope, text):
    domain = _get_domain()
    if not domain: return None, "اول برای پنل دامنه بساز"
    cands = parse(text)
    if not cands: return None, "هیچ آی‌پی درستی توی فایل پیدا نشد"
    jid = hashlib.sha1(f"{scope}{time.time()}{random.random()}".encode()).hexdigest()[:12]
    job = {"scope": scope, "total": len(cands), "tested": 0, "ok": 0, "done": False, "results": [], "t": time.time()}
    with LOCK:
        for k in [k for k, v in JOBS.items() if time.time() - v["t"] > 1800 or v["scope"] == scope]: JOBS.pop(k, None)
        JOBS[jid] = job
    def run():
        try: job["results"] = test_many(cands, domain, job)[:200]
        finally: job["done"] = True
    threading.Thread(target=run, daemon=True).start()
    return jid, None

def job_status(scope, jid):
    j = JOBS.get(jid)
    if not j or j["scope"] != scope: return None
    return {"total": j["total"], "tested": j["tested"], "ok": j["ok"], "done": j["done"],
            "results": j["results"] if j["done"] else []}

# ---------------- storage ----------------
def _path(scope): return f"{DIR}/{scope}.json"

def scope_of(token): return "u_" + hashlib.sha256(("jx-ip|" + token).encode()).hexdigest()[:32]

def load(scope):
    try:
        d = json.load(open(_path(scope)))
        return d if isinstance(d, dict) else None
    except Exception: return None

def save(scope, ips, mode="both", per=2, share=None):
    clean = []
    for x in ips[:MAX_KEEP]:
        try:
            ip = str(ipaddress.ip_address(str(x.get("ip"))))
            port = int(x.get("port") or 443)
            if port not in PORTS: port = 443
            clean.append({"ip": ip, "port": port, "ms": int(x.get("ms") or 0), "fail": 0})
        except Exception: continue
    old = load(scope) or {}
    d = {"ips": clean, "mode": mode if mode in ("both", "ip") else "both", "per": max(1, min(10, int(per or 2))),
         "t": int(time.time()), "checked": int(time.time())}
    if scope == "global": d["share"] = bool(old.get("share", True) if share is None else share)
    tmp = _path(scope) + ".tmp"
    with open(tmp, "w") as f: json.dump(d, f)
    os.replace(tmp, _path(scope))
    return d

def clear(scope):
    try: os.remove(_path(scope))
    except FileNotFoundError: pass

def summary(scope):
    d = load(scope)
    if not d: return {"count": 0}
    return {"count": len(d["ips"]), "mode": d.get("mode"), "per": d.get("per"), "share": d.get("share"),
            "checked": d.get("checked"), "ips": [{"ip": x["ip"], "port": x["port"], "ms": x["ms"]} for x in d["ips"]]}

def _retest_loop():
    time.sleep(120)
    while True:
        try:
            dom = _get_domain()
            for fn in os.listdir(DIR) if dom else []:
                if not fn.endswith(".json"): continue
                scope = fn[:-5]; d = load(scope)
                if not d or time.time() - d.get("checked", 0) < RETEST: continue
                ok = {(x["ip"], x["port"]): x["ms"] for x in test_many([(x["ip"], x["port"]) for x in d["ips"]], dom)}
                keep = []
                for x in d["ips"]:
                    k = (x["ip"], x["port"])
                    if k in ok: x["ms"], x["fail"] = ok[k], 0
                    else: x["fail"] = x.get("fail", 0) + 1
                    if x["fail"] < 2: keep.append(x)      # one bad test is forgiven, two in a row drops it
                keep.sort(key=lambda x: (x["fail"], x["ms"]))
                d["ips"], d["checked"] = keep, int(time.time())
                with open(_path(scope) + ".tmp", "w") as f: json.dump(d, f)
                os.replace(_path(scope) + ".tmp", _path(scope))
                _log(f"clean IP re-test {scope[:10]}: {len(keep)} kept")
                time.sleep(2)
        except Exception as e: _log("clean IP re-test:", e)
        time.sleep(600)

# ---------------- subscription token check ----------------
_VALID = {}

def valid_token(token):
    if not re.fullmatch(r"[A-Za-z0-9_\-.=%]{8,300}", token or ""): return False
    c = _VALID.get(token)
    if c and time.time() - c < 300: return True
    try:
        with urllib.request.urlopen(f"{BASE}/{SUB_PATH}/{urllib.parse.quote(token, safe='')}/info", timeout=10) as r:
            ok = r.status == 200
    except Exception: ok = False
    if ok: _VALID[token] = time.time()
    return ok

# ---------------- rewriting links ----------------
def _b64d(s):
    s = s.strip(); s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.replace("+", "-").replace("/", "_"))

def _host_fmt(ip): return f"[{ip}]" if ":" in ip else ip

def rewrite(link, ip, port, tag):
    """same config, but the phone connects to `ip`; SNI and Host stay the real domain"""
    try:
        if link.startswith("vmess://"):
            j = json.loads(_b64d(link[8:]).decode())
            dom = j.get("sni") or j.get("host") or j.get("add")
            j["sni"] = j.get("sni") or dom; j["host"] = j.get("host") or dom
            j["add"], j["port"] = ip, str(port)
            j["ps"] = f"{j.get('ps', '')} · {tag}"
            return "vmess://" + base64.b64encode(json.dumps(j, ensure_ascii=False, separators=(",", ":")).encode()).decode()
        m = re.match(r"^([a-z0-9+.-]+)://([^@/?#]*@)?(\[[^\]]+\]|[^:/?#]+)(?::(\d+))?([^?#]*)(\?[^#]*)?(#.*)?$", link, re.I)
        if not m: return None
        scheme, cred, host, _, path, query, frag = m.groups()
        if scheme.lower() not in ("vless", "trojan", "ss", "hysteria2", "hy2", "tuic"): return None
        host = host.strip("[]")
        qs = urllib.parse.parse_qsl((query or "?")[1:], keep_blank_values=True)
        keys = {k.lower() for k, _ in qs}
        if "sni" not in keys and "peer" not in keys: qs.append(("sni", host))
        if "host" not in keys and any(k.lower() == "type" and v.lower() in ("ws", "httpupgrade", "xhttp", "splithttp", "h2", "http") for k, v in qs):
            qs.append(("host", host))
        name = urllib.parse.unquote((frag or "#")[1:])
        return (f"{scheme}://{cred or ''}{_host_fmt(ip)}:{port}{path}?{urllib.parse.urlencode(qs, safe='/:,')}"
                f"#{urllib.parse.quote(name + ' · ' + tag)}")
    except Exception:
        return None

def _fetch(token, ua):
    """links of this user straight from the panel + the panel's subscription headers"""
    for kind in ("links", "links-base64", "v2ray", ""):
        url = f"{BASE}/{SUB_PATH}/{urllib.parse.quote(token, safe='')}" + (f"/{kind}" if kind else "")
        try:
            r = urllib.request.Request(url, headers={"User-Agent": ua or "v2rayNG/1.8", "Accept": "text/plain"})
            with urllib.request.urlopen(r, timeout=20) as resp:
                body = resp.read().decode(errors="ignore"); hdr = dict(resp.headers.items())
        except urllib.error.HTTPError as e:
            if e.code in (403, 404) and kind == "": return None, {}
            continue
        except Exception: continue
        txt = body.strip()
        if txt and "://" not in txt[:200]:
            try: txt = _b64d(txt).decode(errors="ignore")
            except Exception: pass
        links = [l.strip() for l in txt.splitlines() if re.match(r"^[a-z][a-z0-9+.-]*://\S+$", l.strip(), re.I)]
        if links: return links, hdr
    return [], {}

def build_sub(token, ua=""):
    """(status, headers, body) for /sub/<token>/jx-ip"""
    if not valid_token(token): return 404, {}, b"not found"
    links, hdr = _fetch(token, ua)
    if links is None: return 404, {}, b"not found"
    d = load(scope_of(token))
    if not d or not d.get("ips"):
        g = load("global")
        d = g if g and g.get("share", True) and g.get("ips") else None
    out = []
    if d:
        ips = d["ips"][:d.get("per", 2)]
        for link in links:
            for n, x in enumerate(ips, 1):
                r = rewrite(link, x["ip"], x["port"], f"IP{n}")
                if r: out.append(r)
        if d.get("mode") != "ip" or not out: out += links
    else:
        out = links
    keep = {k: v for k, v in hdr.items() if k.lower() in ("subscription-userinfo", "profile-update-interval", "profile-title",
                                                          "support-url", "profile-web-page-url", "announce")}
    if "profile-title" not in {k.lower() for k in keep}: keep["profile-title"] = "base64:" + base64.b64encode("Super JinX · IP".encode()).decode()
    keep["Content-Type"] = "text/plain; charset=utf-8"
    keep["Cache-Control"] = "no-store"
    keep["Content-Disposition"] = 'attachment; filename="jinx-ip"'
    return 200, keep, base64.b64encode("\n".join(out).encode())
