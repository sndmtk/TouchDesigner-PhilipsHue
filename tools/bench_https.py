"""Hue Bridge HTTPS (CLIP v2) control benchmark.

Measures, on your own Bridge and network:
  1. Single-request latency: new connection per request (current HueBridge.tox
     behaviour) vs. a persistent keep-alive connection.
  2. Rate sweep: sends light commands at increasing target rates and reports
     throughput, HTTP 429 (rate limit) counts and latency at each rate.

Only the Python standard library is used. Run from a normal Python 3.9+:

    python tools/bench_https.py --ip 192.168.0.100

Visual check (are the commands really reaching the lights?):

    python tools/bench_https.py --ip 192.168.0.100 --visual

  Lights alternate red/blue at each rate step; at the end of a step all lights
  turn green and you press Enter as soon as all of them are green. The delay
  (minus your own reaction time, calibrated first) shows how many commands were
  still queued in the Bridge. Type x before Enter if you saw stutter or misses.

The API key is asked interactively (copy it from the HueBridge "API Key"
parameter) so it does not end up in your shell history. You can also set the
environment variable HUE_APP_KEY.

The lights blink while the test runs (brightness alternates). Their previous
on/brightness/color state is restored at the end.

Note: latency here is "request sent -> Bridge HTTP response". The time until
the light visibly changes (Zigbee delivery + lamp) is not included.
"""
import argparse
import csv
import getpass
import http.client
import json
import os
import queue
import ssl
import statistics
import threading
import time
from pathlib import Path

CTX = ssl._create_unverified_context()  # Hue Bridges use a non-public certificate.


# ---------------------------------------------------------------- HTTP helpers
class Conn:
    """One HTTPS connection. keepalive=False reconnects for every request."""

    def __init__(self, host, key, keepalive, timeout=5.0, port=443):
        self.host, self.key, self.keepalive = host, key, keepalive
        self.timeout, self.port = timeout, port
        self.c = None

    def _open(self):
        self.c = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=CTX)

    def close(self):
        if self.c is not None:
            try:
                self.c.close()
            except Exception:
                pass
        self.c = None

    def request(self, method, path, body=None):
        """Returns (status, parsed_json_or_None, seconds). status 0 = transport error."""
        data = None if body is None else json.dumps(body).encode()
        headers = {"hue-application-key": self.key, "Content-Type": "application/json"}
        if self.keepalive:
            headers["Connection"] = "keep-alive"
        for attempt in (0, 1):
            if self.c is None or not self.keepalive:
                self.close()
                self._open()
            t0 = time.perf_counter()
            try:
                self.c.request(method, path, body=data, headers=headers)
                r = self.c.getresponse()
                raw = r.read()
                dt = time.perf_counter() - t0
                if not self.keepalive or r.getheader("Connection", "").lower() == "close":
                    self.close()
                try:
                    parsed = json.loads(raw) if raw else None
                except ValueError:
                    parsed = None
                return r.status, parsed, dt
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
                # A kept-alive socket closed by the Bridge: reconnect once.
                self.close()
                if attempt == 0 and self.keepalive:
                    continue
                return 0, None, time.perf_counter() - t0
            except Exception:
                self.close()
                return 0, None, time.perf_counter() - t0
        return 0, None, 0.0


def pct(values, p):
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def ms(v):
    return "   -  " if v != v else f"{v * 1000:6.1f}"


# ---------------------------------------------------------------- light setup
def fetch_lights(host, key, port):
    c = Conn(host, key, keepalive=False, port=port)
    status, data, _ = c.request("GET", "/clip/v2/resource/light")
    if status in (401, 403):
        raise SystemExit("API key rejected (HTTP %d)." % status)
    if status != 200 or not isinstance(data, dict):
        raise SystemExit("Could not read lights (HTTP %s). Is the IP correct and reachable?" % status)
    return data.get("data", [])


def pick_lights(all_lights, selector):
    dimmable = [l for l in all_lights if "dimming" in l]
    if selector in (None, "", "all"):
        return dimmable
    wanted = [s.strip() for s in selector.split(",") if s.strip()]
    out = []
    for w in wanted:
        match = [l for l in dimmable if l["id"].startswith(w) or l.get("metadata", {}).get("name") == w]
        if not match:
            raise SystemExit("No dimmable light matches: " + w)
        out.extend(m for m in match if m not in out)
    return out


def snapshot(lights):
    snap = {}
    for l in lights:
        body = {"on": {"on": l.get("on", {}).get("on", True)}}
        if "dimming" in l:
            body["dimming"] = {"brightness": l["dimming"].get("brightness", 100)}
        xy = l.get("color", {}).get("xy")
        if xy:
            body["color"] = {"xy": {"x": xy["x"], "y": xy["y"]}}
        snap[l["id"]] = body
    return snap


def restore(host, key, port, snap):
    c = Conn(host, key, keepalive=True, port=port)
    for lid, body in snap.items():
        body = dict(body, dynamics={"duration": 400})
        for _ in range(5):
            status, _, _ = c.request("PUT", "/clip/v2/resource/light/" + lid, body)
            if status != 429:
                break
            time.sleep(1)  # rate limited after the sweep: wait and retry
        time.sleep(0.15)  # stay well below the rate limit
    c.close()


def command(i, duration_ms):
    # Alternate brightness so every command is a real change.
    return {"on": {"on": True}, "dimming": {"brightness": 35.0 if i % 2 else 75.0},
            "dynamics": {"duration": duration_ms}}


# ---------------------------------------------------------------- phase 1
def single_latency(host, key, port, light_id, n, gap, keepalive, duration_ms):
    c = Conn(host, key, keepalive=keepalive, port=port)
    lat, codes = [], {}
    for i in range(n):
        status, _, dt = c.request("PUT", "/clip/v2/resource/light/" + light_id, command(i, duration_ms))
        codes[status] = codes.get(status, 0) + 1
        if status == 200:
            lat.append(dt)
        time.sleep(gap)
    c.close()
    return lat, codes


# ---------------------------------------------------------------- phase 2
def run_rate(host, key, port, light_ids, rate, seconds, conns, duration_ms, rows, make_cmd=None, phase="sweep"):
    """Paced open-loop load: a pacer enqueues jobs at `rate`; `conns` workers send them."""
    if make_cmd is None:
        make_cmd = lambda i, lid: command(i, duration_ms)
    jobs = queue.Queue()
    results = []
    lock = threading.Lock()
    stop = object()

    def worker():
        c = Conn(host, key, keepalive=True, port=port)
        while True:
            job = jobs.get()
            if job is stop:
                break
            i, lid, due = job
            start = time.perf_counter()
            status, data, dt = c.request("PUT", "/clip/v2/resource/light/" + lid, make_cmd(i, lid))
            done = time.perf_counter()
            with lock:
                results.append((i, lid, status, start - due, dt, done - due))
        c.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(conns)]
    for t in threads:
        t.start()

    t0 = time.perf_counter()
    total = int(rate * seconds)
    max_backlog = 0
    for i in range(total):
        due = t0 + i / rate
        now = time.perf_counter()
        if due > now:
            time.sleep(due - now)
        jobs.put((i, light_ids[i % len(light_ids)], due))
        max_backlog = max(max_backlog, jobs.qsize())
    for _ in threads:
        jobs.put(stop)
    for t in threads:
        t.join(timeout=30)
    elapsed = time.perf_counter() - t0

    ok = [r for r in results if r[2] == 200]
    n429 = sum(1 for r in results if r[2] == 429)
    nerr = sum(1 for r in results if r[2] not in (200, 429))
    for r in results:
        rows.append({"phase": phase, "target_rate": rate, "seq": r[0], "light_id": r[1], "status": r[2],
                     "queue_wait_ms": round(r[3] * 1000, 2), "service_ms": round(r[4] * 1000, 2),
                     "end_to_end_ms": round(r[5] * 1000, 2)})
    return {
        "rate": rate, "sent": len(results), "ok": len(ok), "429": n429, "err": nerr,
        "ok_per_s": len(ok) / elapsed if elapsed else 0,
        "svc50": pct([r[4] for r in ok], 50), "svc95": pct([r[4] for r in ok], 95),
        "e2e50": pct([r[5] for r in ok], 50), "e2e95": pct([r[5] for r in ok], 95),
        "e2emax": max([r[5] for r in ok], default=float("nan")),
        "backlog": max_backlog,
    }


# ---------------------------------------------------------------- phase 3 (visual)
RED = {"x": 0.675, "y": 0.322}
BLUE = {"x": 0.167, "y": 0.04}
GREEN = {"x": 0.17, "y": 0.7}
WHITE = {"x": 0.3227, "y": 0.329}


def put_all(c, ids, body):
    """Send the same body to every light sequentially; returns time after the last response."""
    for lid in ids:
        for _ in range(10):
            status, _, _ = c.request("PUT", "/clip/v2/resource/light/" + lid, body)
            if status != 429:
                break
            time.sleep(0.3)  # rate limited: retry so the green end marker is never lost
    return time.perf_counter()


def wait_enter(prompt):
    """Return (seconds until Enter, typed text)."""
    t0 = time.perf_counter()
    text = input(prompt)
    return time.perf_counter() - t0, text.strip().lower()


def visual(host, key, port, ids, rates, seconds, conns, with_on, rows, summary):
    n = len(ids)
    ctl = Conn(host, key, keepalive=True, port=port)
    base = {"on": {"on": True}, "dimming": {"brightness": 100.0}, "dynamics": {"duration": 0}}

    def neutral():
        put_all(ctl, ids, dict(base, color={"xy": WHITE}, dimming={"brightness": 30.0}))
        time.sleep(2.5)  # let the Bridge settle before the next measurement

    print("\n[V] 目視テスト：ライトを見ながら操作してください。")
    print("    各段階でライトは赤と青を交互に切り替えます（1件ごとに1台ずつ順番に）。")
    print("    最後に全ライトを緑にするので、全部が緑になった瞬間にEnterを押してください。")
    print("    カクつき・止まるライト・切り替えの抜けが見えたら、xを入力してからEnterを押してください。")

    # Calibrate the viewer's own reaction time with an idle Bridge.
    reactions = []
    for k in range(2):
        neutral()
        input(f"\n  [較正 {k + 1}/2] 準備ができたらEnter（押した後、ライトがすぐ緑になります）")
        time.sleep(0.8 + 0.7 * k)
        put_all(ctl, ids, dict(base, color={"xy": GREEN}))
        dt, _ = wait_enter("    全部が緑になったらEnter > ")
        reactions.append(dt)
    reaction = statistics.mean(reactions)
    print(f"    反応時間の基準：{reaction:.2f} 秒（以降の遅れからこの値を差し引きます）")
    summary["visual_reaction_s"] = reaction
    summary["visual"] = []

    for rate in rates:
        neutral()
        per_light = rate / n
        input(f"\n  [{rate:g} 件/秒] 1台あたり {per_light:.2f} 回/秒で切り替わるはずです。"
              f"{seconds:g}秒間。準備ができたらEnter")

        def make_cmd(i, lid):
            flip = (i // n) % 2
            body = {"color": {"xy": BLUE if flip else RED}, "dimming": {"brightness": 100.0},
                    "dynamics": {"duration": 0}}
            if with_on:
                body["on"] = {"on": True}
            return body

        s = run_rate(host, key, port, ids, rate, seconds, conns, 0, rows, make_cmd=make_cmd, phase="visual")
        put_all(ctl, ids, dict(base, color={"xy": GREEN}))
        dt, text = wait_enter("    全部が緑になったらEnter（乱れがあれば x を入力してEnter） > ")
        lag = max(0.0, dt - reaction)
        verdict = "NG" if "x" in text else "OK"
        print(f"    -> 緑になるまでの遅れ（反応時間を除く）：{lag:.2f} 秒 / 見た目：{verdict} / "
              f"HTTP ok {s['ok']}/{s['sent']}, 429 {s['429']}")
        summary["visual"].append({"rate": rate, "per_light_hz": per_light, "lag_s": lag, "verdict": verdict,
                                  "http_ok": s["ok"], "sent": s["sent"], "429": s["429"], "err": s["err"]})
        rows.append({"phase": "visual-result", "target_rate": rate, "status": verdict,
                     "end_to_end_ms": round(lag * 1000, 1)})

    ctl.close()
    print("\n    件/秒  1台あたり  遅れ(秒)  見た目")
    for v in summary["visual"]:
        print(f"    {v['rate']:6g}  {v['per_light_hz']:7.2f}   {v['lag_s']:6.2f}   {v['verdict']}")
    print("    遅れが0.3秒を超える、または見た目がNGになった段階が、ライトに届く実際の上限です。")


# ---------------------------------------------------------------- main
class _Done(Exception):
    pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", required=True, help="Bridge IPv4 address")
    ap.add_argument("--lights", default="all", help="'all' or comma list of light id prefixes / names")
    ap.add_argument("--rates", default="5,10,15,20,25,30,40", help="request/s steps for the sweep")
    ap.add_argument("--seconds", type=float, default=8, help="duration of each sweep step")
    ap.add_argument("--conns", type=int, default=4, help="parallel keep-alive connections in the sweep")
    ap.add_argument("--single", type=int, default=30, help="requests per single-latency test")
    ap.add_argument("--transition", type=int, default=0, help="dynamics.duration in ms sent with each command")
    ap.add_argument("--rest", type=float, default=5, help="pause between steps (s) so the Bridge recovers")
    ap.add_argument("--stop-429", type=float, default=0.3, help="stop sweep when 429 ratio exceeds this")
    ap.add_argument("--out", default="build/bench", help="folder for the CSV/JSON results")
    ap.add_argument("--visual", action="store_true",
                    help="interactive visual test only: watch the lights and press Enter (skips phases 1-2)")
    ap.add_argument("--visual-rates", default="5,10,15,20,25", help="request/s steps for --visual")
    ap.add_argument("--visual-seconds", type=float, default=6, help="duration of each --visual step")
    ap.add_argument("--with-on", action="store_true",
                    help="--visual: include on:true in every command (current HueBridge.tox behaviour)")
    ap.add_argument("--port", type=int, default=443, help=argparse.SUPPRESS)
    a = ap.parse_args()

    key = os.environ.get("HUE_APP_KEY") or getpass.getpass("Hue API key (input hidden): ").strip()
    if not key:
        raise SystemExit("No API key given.")

    all_lights = fetch_lights(a.ip, key, a.port)
    lights = pick_lights(all_lights, a.lights)
    if not lights:
        raise SystemExit("No dimmable lights found.")
    ids = [l["id"] for l in lights]
    print(f"Bridge {a.ip}: using {len(lights)} light(s):")
    for l in lights:
        print("  -", l.get("metadata", {}).get("name", ""), l["id"][:8])
    snap = snapshot(lights)

    rows, summary = [], {"bridge": a.ip, "lights": len(lights), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        if a.visual:
            vrates = [float(r) for r in a.visual_rates.split(",") if r.strip()]
            summary["visual_with_on"] = a.with_on
            visual(a.ip, key, a.port, ids, vrates, a.visual_seconds, a.conns, a.with_on, rows, summary)
            raise _Done
        print("\n[1] Single-request latency on one light (sequential, ~5 req/s)")
        print("    mode         ok   p50ms  p95ms  maxms  codes")
        for name, keep in (("new-conn", False), ("keep-alive", True)):
            lat, codes = single_latency(a.ip, key, a.port, ids[0], a.single, 0.2, keep, a.transition)
            print(f"    {name:11s} {len(lat):3d}  {ms(pct(lat, 50))} {ms(pct(lat, 95))} {ms(max(lat, default=float('nan')))}  {codes}")
            summary[name] = {"p50_ms": pct(lat, 50) * 1000 if lat else None,
                             "p95_ms": pct(lat, 95) * 1000 if lat else None, "codes": codes}
            for v in lat:
                rows.append({"phase": name, "service_ms": round(v * 1000, 2), "status": 200})
            time.sleep(a.rest)

        rates = [float(r) for r in a.rates.split(",") if r.strip()]
        print(f"\n[2] Rate sweep: {a.conns} keep-alive connection(s), {a.seconds:g}s per step, "
              f"round-robin over {len(ids)} light(s)")
        print("    target  ok/s   sent   ok  429  err | service p50  p95 | end-to-end p50  p95   max | backlog")
        summary["sweep"] = []
        for rate in rates:
            s = run_rate(a.ip, key, a.port, ids, rate, a.seconds, a.conns, a.transition, rows)
            summary["sweep"].append(s)
            print(f"    {rate:6g} {s['ok_per_s']:5.1f} {s['sent']:6d} {s['ok']:4d} {s['429']:4d} {s['err']:4d} |"
                  f"      {ms(s['svc50'])} {ms(s['svc95'])} |        {ms(s['e2e50'])} {ms(s['e2e95'])} {ms(s['e2emax'])} | {s['backlog']:5d}")
            if s["sent"] and (s["429"] + s["err"]) / s["sent"] > a.stop_429:
                print("    -> rate limit / errors exceeded threshold; stopping sweep.")
                break
            time.sleep(a.rest)
    except _Done:
        pass
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted.")
    finally:
        print("\nRestoring previous light state...")
        time.sleep(2)
        restore(a.ip, key, a.port, snap)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    with open(out / f"bench_{stamp}.csv", "w", newline="", encoding="utf-8") as f:
        fields = ["phase", "target_rate", "seq", "light_id", "status", "queue_wait_ms", "service_ms", "end_to_end_ms"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    (out / f"bench_{stamp}.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"Saved: {out / f'bench_{stamp}.csv'} and .json")


if __name__ == "__main__":
    main()
