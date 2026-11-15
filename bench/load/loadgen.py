"""Load generator for the orders service (stdlib only).

    python loadgen.py DURATION THREADS RATE --url http://127.0.0.1:PORT [--seed 42]
                      [--warmup 2] [--out result.json]

RATE 0 = saturated: every thread is a closed loop (send, wait, send).
RATE R > 0 = paced open loop: request i is scheduled at t0 + i/R; the next free thread takes
the next slot and sleeps until its time. Latency is reported both from the actual send
("latency_ms", service time) and from the scheduled time ("latency_co_ms", corrected for
coordinated omission: a stalled server cannot hide its queueing by slowing the client).

The request mix is deterministic: a seeded RNG pre-builds the request sequence and requests
are handed out by a shared counter, so the k-th request of every run is the same request (the
interleaving across threads and the total count in saturated mode are not deterministic).

Prints one JSON object (rps, p50/p95/p99, status-code counts, per-minute counts, ...) and
writes it to --out when given. Only requests that START (saturated) or are DUE (paced)
inside the measured window [mark, mark + duration) count, where mark = end of warmup (fixed
--warmup, or when the --warmup-until file appears). Warmup traffic is sent, summarized under
"warmup" and in the per-second counts, but not measured.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import random
import resource
import sys
import threading
import time
from collections import Counter, defaultdict
from urllib.parse import urlsplit

# Skewed endpoint mix (weights sum to 100).
MIX = (
    ("get_order", 35),
    ("create_order", 20),
    ("convert", 20),
    ("summary", 10),
    ("pay", 10),
    ("health", 5),
)
INVALID_ORDER_SHARE = 0.13
N_SPECS = 100_000
SEED_ORDERS = 500
CUSTOMERS = 50
CURRENCIES = (("EUR", 40), ("GBP", 25), ("JPY", 15), ("CAD", 10), ("AUD", 5), ("CHF", 5))
SWITCH_INTERVAL_S = 0.0002
JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
GET_HEADERS = {"Accept": "application/json"}


def _valid_order(rng: random.Random) -> dict:
    return {
        "customer_id": rng.randint(1, CUSTOMERS),
        "currency": rng.choice(["USD", "USD", "USD", "EUR", "GBP"]),
        "items": [
            {
                "sku": f"{rng.choice(['ABC', 'DEF', 'XYZ', 'QRS'])}-{rng.randrange(10000):04d}",
                "qty": rng.randint(1, 5),
                "unit_price": f"{rng.randint(100, 20000) / 100:.2f}",
            }
            for _ in range(rng.randint(1, 4))
        ],
        "note": rng.choice([None, None, None, "leave at the door", "gift wrap"]),
    }


def _invalidate(order: dict, rng: random.Random) -> dict:
    kind = rng.randrange(8)
    if kind == 0:
        order["items"][0]["qty"] = 0
    elif kind == 1:
        order["items"] = []
    elif kind == 2:
        order["items"][0]["sku"] = "bad sku"
    elif kind == 3:
        order["currency"] = "XXX"
    elif kind == 4:
        order["items"][0]["unit_price"] = "-3.50"
    elif kind == 5:
        order["coupon"] = "FREE"  # extra field, forbidden
    elif kind == 6:
        order["customer_id"] = 0
    else:
        order["note"] = "x" * 250
    return order


def parse_mix(text: str | None) -> tuple[tuple[str, float], ...]:
    """``name=weight,...`` (diagnostics: e.g. ``get_order=1`` drives one endpoint only)."""
    if not text:
        return MIX
    known = {m for m, _ in MIX}
    out = []
    for part in text.split(","):
        name, _, weight = part.partition("=")
        if name not in known:
            raise SystemExit(f"unknown endpoint {name!r} in --mix (known: {sorted(known)})")
        out.append((name, float(weight or 1)))
    return tuple(out)


def build_specs(seed: int, n: int = N_SPECS, mix=MIX) -> list[tuple[str, str, bytes | None, str]]:
    """(method, path, body, endpoint) for the first ``n`` requests of the mix."""
    rng = random.Random(seed)
    names = [m for m, _ in mix]
    weights = [w for _, w in mix]
    cur_names = [c for c, _ in CURRENCIES]
    cur_weights = [w for _, w in CURRENCIES]
    # Zipf-ish customer popularity; ids 51/52 do not exist (-> 404).
    cust_ids = list(range(1, CUSTOMERS + 3))
    cust_weights = [1.0 / k for k in range(1, CUSTOMERS + 1)] + [0.02, 0.02]
    specs = []
    for _ in range(n):
        ep = rng.choices(names, weights)[0]
        if ep == "get_order":
            r = rng.random()
            if r < 0.03:
                path = "/orders/" + rng.choice(["abc", "12x", "-1", "0x1f"])
            elif r < 0.10:
                path = f"/orders/{rng.randint(1_000_000, 9_999_999)}"
            else:
                path = f"/orders/{rng.randint(1, SEED_ORDERS)}"
            specs.append(("GET", path, None, ep))
        elif ep == "create_order":
            order = _valid_order(rng)
            if rng.random() < INVALID_ORDER_SHARE:
                order = _invalidate(order, rng)
            specs.append(("POST", "/orders", json.dumps(order).encode(), ep))
        elif ep == "convert":
            cur = rng.choices(cur_names, cur_weights)[0]
            amount = f"{rng.randint(100, 500000) / 100:.2f}"
            specs.append(("GET", f"/convert?amount={amount}&to={cur}", None, ep))
        elif ep == "summary":
            cid = rng.choices(cust_ids, cust_weights)[0]
            specs.append(("GET", f"/customers/{cid}/summary", None, ep))
        elif ep == "pay":
            specs.append(("POST", f"/orders/{rng.randint(1, SEED_ORDERS)}/pay", b"", ep))
        else:
            specs.append(("GET", "/healthz", None, ep))
    return specs


def _pct(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    rank = max(1, math.ceil(q * len(sorted_vals)))
    return sorted_vals[rank - 1]


def _lat_summary(values: list[float]) -> dict:
    vals = sorted(values)
    if not vals:
        return {"p50": None, "p95": None, "p99": None, "max": None, "mean": None}
    ms = 1000.0
    return {
        "p50": round(_pct(vals, 0.50) * ms, 3),
        "p95": round(_pct(vals, 0.95) * ms, 3),
        "p99": round(_pct(vals, 0.99) * ms, 3),
        "max": round(vals[-1] * ms, 3),
        "mean": round(sum(vals) / len(vals) * ms, 3),
    }


def _sleep_until(deadline: float) -> None:
    """Wake within ~0.3 ms of ``deadline``. Darwin gives a sleep up to 50% (max 10 ms) of
    timer leeway, so one long sleep lands up to 10 ms late; sleeping 1/1.6 of what is left,
    repeatedly, converges without spinning."""
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0.0003:
            return
        time.sleep(remaining / 1.6)


class _Shared:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.next_index = 0
        self.go = threading.Event()
        self.t0 = 0.0
        self.t_end = math.inf    # perf time the measured window ends; known once it starts
        self.mark = None         # perf time the measured window starts (end of warmup)
        self.mark_reason = None


def _wait_healthy(host: str, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/healthz", headers=GET_HEADERS)
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status == 200:
                return
            last = f"HTTP {resp.status}"
        except OSError as exc:
            last = repr(exc)
        time.sleep(0.2)
    raise SystemExit(f"service not healthy after {timeout}s: {last}")


def run(url: str, duration: float, threads: int, rate: float, seed: int, warmup: float,
        timeout: float = 30.0, grace: float = 10.0, mix=MIX, warmup_until: str | None = None,
        max_warmup: float | None = None) -> dict:
    """Drive load; the measured window starts after ``warmup`` s, or later: with
    ``warmup_until`` it starts when that file exists (at least ``warmup``, at most
    ``max_warmup`` s after t0). Load never pauses at the boundary."""
    # The client's own GIL hand-offs would otherwise add up to one switch interval (5 ms)
    # of lateness to every paced send; a short interval keeps the schedule tight.
    sys.setswitchinterval(SWITCH_INTERVAL_S)
    max_warmup = warmup if max_warmup is None or warmup_until is None else max(warmup,
                                                                                max_warmup)
    parts = urlsplit(url)
    host, port = parts.hostname or "127.0.0.1", parts.port or 80
    _wait_healthy(host, port, 30.0)
    specs = build_specs(seed, mix=mix)
    n_specs = len(specs)
    paced = rate > 0
    shared = _Shared()
    records: list[list[tuple]] = [[] for _ in range(threads)]

    def worker(k: int) -> None:
        rec = records[k]
        conn = None
        shared.go.wait()
        t0 = shared.t0
        perf = time.perf_counter
        while True:
            with shared.lock:
                i = shared.next_index
                shared.next_index += 1
            if paced:
                sched = t0 + i / rate
                if sched >= shared.t_end:
                    break
                now = perf()
                if now >= shared.t_end + grace:
                    break
                if sched > now:
                    _sleep_until(sched)
                    if sched >= shared.t_end:  # the window got placed while we slept
                        break
            else:
                sched = None
                if perf() >= shared.t_end:
                    break
            method, path, body, ep = specs[i % n_specs]
            start = perf()
            try:
                if conn is None:
                    conn = http.client.HTTPConnection(host, port, timeout=timeout)
                conn.request(method, path, body=body,
                             headers=JSON_HEADERS if body is not None else GET_HEADERS)
                resp = conn.getresponse()
                resp.read()
                status = resp.status
                if resp.will_close:
                    conn.close()
                    conn = None
            except Exception as exc:  # noqa: BLE001 - every failure is a data point
                status = "err:" + type(exc).__name__
                if conn is not None:
                    conn.close()
                conn = None
            end = perf()
            rec.append((
                (sched if paced else start) - t0,  # when it was due (paced) / sent
                end - start,
                (end - sched) if paced else None,
                status,
                ep,
                (start - sched) if paced else None,  # how late the client sent it
            ))
        if conn is not None:
            conn.close()

    def controller() -> None:
        shared.go.wait()
        t0 = shared.t0
        min_mark, max_mark = t0 + warmup, t0 + max_warmup
        reason = "fixed"
        while True:
            now = time.perf_counter()
            if now >= max_mark:
                reason = "max-warmup" if warmup_until else "fixed"
                break
            if now >= min_mark and warmup_until is not None and os.path.exists(warmup_until):
                reason = "signal"
                break
            time.sleep(0.02)
        shared.mark = max(time.perf_counter(), min_mark)
        shared.mark_reason = reason
        shared.t_end = shared.mark + duration

    pool = [threading.Thread(target=worker, args=(k,), name=f"load-{k}", daemon=True)
            for k in range(threads)]
    ctl = threading.Thread(target=controller, name="load-window", daemon=True)
    for t in pool + [ctl]:
        t.start()
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    wall0 = time.perf_counter()
    shared.t0 = wall0 + 0.05
    t0_wall = time.time() + (shared.t0 - time.perf_counter())
    shared.go.set()
    deadline = time.monotonic() + max_warmup + duration + grace + timeout + 60
    for t in [ctl] + pool:
        t.join(timeout=max(0.1, deadline - time.monotonic()))
    wall = time.perf_counter() - wall0
    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    stuck = sum(1 for t in pool if t.is_alive())

    mark_rel = (shared.mark - shared.t0) if shared.mark is not None else warmup
    span = mark_rel + duration
    every = [r for rec in records for r in rec]
    window = [r for r in every if mark_rel <= r[0] < span]
    before = [r for r in every if r[0] < mark_rel]
    statuses = Counter(str(r[3]) for r in window)
    per_ep: dict[str, Counter] = defaultdict(Counter)
    for r in window:
        per_ep[r[4]][str(r[3])] += 1
    per_minute = [0] * max(1, math.ceil(duration / 60.0))
    per_10s = [0] * max(1, math.ceil(duration / 10.0))
    for r in window:
        rel = r[0] - mark_rel
        per_minute[min(len(per_minute) - 1, int(rel // 60))] += 1
        per_10s[min(len(per_10s) - 1, int(rel // 10))] += 1
    per_second = [0] * max(1, math.ceil(span))
    for r in every:
        if 0 <= r[0] < span:
            per_second[int(r[0])] += 1
    errors = sum(n for s, n in statuses.items() if s.startswith("err:"))
    cpu = (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)
    result = {
        "mode": "paced" if paced else "saturated",
        "duration_s": duration,
        "warmup_s": round(mark_rel, 3),
        "warmup_min_s": warmup,
        "warmup_max_s": max_warmup,
        "warmup_end": shared.mark_reason,
        "threads": threads,
        "rate": rate,
        "seed": seed,
        "mix": dict(mix),
        "requests": len(window),
        "rps": round(len(window) / duration, 2),
        "ok_rps": round(sum(n for s, n in statuses.items() if s.isdigit() and int(s) < 500)
                        / duration, 2),
        "latency_ms": _lat_summary([r[1] for r in window]),
        "status_counts": dict(sorted(statuses.items())),
        "endpoint_status": {ep: dict(sorted(c.items())) for ep, c in sorted(per_ep.items())},
        "errors": errors,
        "per_minute": per_minute,
        "per_10s": per_10s,
        "warmup": {
            "requests": len(before),
            "rps": round(len(before) / mark_rel, 2) if mark_rel > 0 else None,
            "latency_ms": _lat_summary([r[1] for r in before]),
        },
        # whole run, warmup included: requests by start (saturated) / due (paced) second
        "per_second": per_second,
        "t0_wall": round(t0_wall, 3),
        "mark_wall": round(t0_wall + mark_rel, 3),
        "wall_s": round(wall, 3),
        "client_cpu_s": round(cpu, 3),
        "client_cpu_pct": round(100.0 * cpu / wall, 1) if wall else None,
        "stuck_threads": stuck,
    }
    if paced:
        expected = math.ceil(duration * rate)
        result["offered_rps"] = rate
        result["latency_co_ms"] = _lat_summary([r[2] for r in window])
        result["send_lateness_ms"] = _lat_summary([max(0.0, r[5]) for r in window])
        result["missed_slots"] = max(0, expected - len(window))
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="orders-service load generator")
    ap.add_argument("duration", type=float, help="measured seconds (after warmup)")
    ap.add_argument("threads", type=int)
    ap.add_argument("rate", type=float, help="requests/s across all threads; 0 = saturated")
    ap.add_argument("--url", required=True)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--warmup", type=float, default=2.0)
    ap.add_argument("--timeout", type=float, default=30.0, help="per-request socket timeout")
    ap.add_argument("--warmup-until", default=None, metavar="FILE",
                    help="end the warmup when FILE exists (at least --warmup s, at most "
                         "--max-warmup s); load continues without a pause")
    ap.add_argument("--max-warmup", type=float, default=None)
    ap.add_argument("--mix", default=None, help="override the mix: name=weight,...")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    if args.duration <= 0 or args.threads <= 0 or args.rate < 0:
        ap.error("duration and threads must be > 0, rate >= 0")
    result = run(args.url, args.duration, args.threads, args.rate, args.seed, args.warmup,
                 timeout=args.timeout, mix=parse_mix(args.mix),
                 warmup_until=args.warmup_until, max_warmup=args.max_warmup)
    text = json.dumps(result, indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
