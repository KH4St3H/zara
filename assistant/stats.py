"""zara resource stats from the local Prometheus, packed for a Telegram widget (chat_data <= 1024 bytes)."""
import base64, json, time, urllib.parse, urllib.request

PROM = "http://127.0.0.1:9090/api/v1/"
SEL = 'instance="zara"'
NET = 'device!~"lo|veth.*|docker.*|br-.*"'
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400}
POINTS = 60

SERIES = {  # key -> PromQL; all but "n" are already 0-100
    "c": '100 * (1 - avg(rate(node_cpu_seconds_total{%s,mode="idle"}[%%s])))' % SEL,
    "m": "100 * (1 - node_memory_MemAvailable_bytes{%s} / node_memory_MemTotal_bytes{%s})" % (SEL, SEL),
    "d": '100 * (1 - node_filesystem_avail_bytes{%s,mountpoint="/"} / node_filesystem_size_bytes{%s,mountpoint="/"})' % (SEL, SEL),
    "T": "max(node_thermal_zone_temp{%s})" % SEL,
    "n": "sum(rate(node_network_receive_bytes_total{%s,%s}[%%s])) + sum(rate(node_network_transmit_bytes_total{%s,%s}[%%s]))" % (SEL, NET, SEL, NET),
}
NOW = {
    "c": SERIES["c"] % "1m",
    "m": SERIES["m"],
    "mu": "(node_memory_MemTotal_bytes{%s} - node_memory_MemAvailable_bytes{%s}) / 1e9" % (SEL, SEL),
    "mt": "node_memory_MemTotal_bytes{%s} / 1e9" % SEL,
    "d": SERIES["d"],
    "du": '(node_filesystem_size_bytes{%s,mountpoint="/"} - node_filesystem_avail_bytes{%s,mountpoint="/"}) / 1e9' % (SEL, SEL),
    "dt": 'node_filesystem_size_bytes{%s,mountpoint="/"} / 1e9' % SEL,
    "T": SERIES["T"],
    "rx": "sum(rate(node_network_receive_bytes_total{%s,%s}[1m]))" % (SEL, NET),
    "tx": "sum(rate(node_network_transmit_bytes_total{%s,%s}[1m]))" % (SEL, NET),
    "l": "node_load1{%s}" % SEL,
    "up": "(time() - node_boot_time_seconds{%s}) / 3600" % SEL,
}


def _get(path, **params):
    u = PROM + path + "?" + urllib.parse.urlencode(params)
    return json.load(urllib.request.urlopen(u, timeout=15))["data"]["result"]


def _instant(expr):
    r = _get("query", query=expr)
    return round(float(r[0]["value"][1]), 2) if r else None


def _range(expr, start, end, step):
    r = _get("query_range", query=expr, start=start, end=end, step=step)
    vals = {}
    if r:
        for ts, v in r[0]["values"]:
            vals[int(round((float(ts) - start) / step))] = float(v)
    return [vals.get(i) for i in range(POINTS)]


def _pack(vals, scale=1.0):
    """0-100 ints as bytes (255 = no data), base64."""
    b = bytes(255 if v is None else max(0, min(100, int(round(v * scale)))) for v in vals)
    return base64.b64encode(b).decode()


def collect(rng="6h"):
    rng = rng if rng in RANGES else "6h"
    span = RANGES[rng]
    end = int(time.time())
    step = span // POINTS
    start = end - step * (POINTS - 1)
    win = "%ds" % max(60, step * 2)  # rate window must cover at least one step
    s, net_max = {}, 0.0
    for k, expr in SERIES.items():
        e = expr % ((win,) * expr.count("%s")) if "%s" in expr else expr
        vals = _range(e, start, end, step)
        if k == "n":
            net_max = max([v for v in vals if v is not None] or [0])
            s[k] = _pack(vals, 100.0 / net_max if net_max else 0)
        else:
            s[k] = _pack(vals)
    cur = {k: _instant(e) for k, e in NOW.items()}
    data = {"r": rng, "t": end, "s": s, "nm": round(net_max), "cur": cur}
    out = json.dumps(data, separators=(",", ":"))
    if len(out.encode()) > 1024:  # never exceed Telegram's chat_data limit
        data["s"].pop("n", None)
        out = json.dumps(data, separators=(",", ":"))
    caption = "zara · CPU %s%% · RAM %s%% · Disk %s%% · %s°C" % tuple(
        "–" if cur.get(k) is None else round(cur[k]) for k in ("c", "m", "d", "T"))
    return out, caption
