"""Orders service: the target app for PyMolt's phase-2 load gate (bench/load/).

A small but realistic HTTP service: Flask + SQLAlchemy (sqlite, WAL) + pydantic v2 +
requests (to a local stub upstream), served by waitress with 8 threads, plus a background
sweeper thread. ``bench/load/run.py`` copies this file into a fresh per-run project dir
and runs it either bare (``nocap``) or under ``pymolt capture`` (``capture``).

Written the way a lean production Flask app usually is (request-scoped ``scoped_session``
removed on teardown, prebuilt SQL statements, handlers returning dicts that Flask
serializes), which keeps the dependency-boundary call density near the ~5-7 calls/request
of the service behind the phase-0 measurements in docs/design/tier1-observation-substrate.md.

Endpoints
    POST /orders                    create (pydantic validation; invalid body -> 422)
    GET  /orders/<id>               bad id -> 400, unknown -> 404, else 200
    GET  /customers/<id>/summary    SQL aggregates per status (unknown customer -> 404)
    GET  /convert?amount=&to=       USD -> currency; rate cache (TTL), miss -> stub upstream;
                                    the stub answers 503 for CHF -> 502 here
    POST /orders/<id>/pay           ids divisible by 5 raise RuntimeError -> 500 (unhandled);
                                    expired -> 409; pending -> paid; paid -> 200 (idempotent)
    GET  /healthz                   liveness + sweeper/cache stats

Environment
    BENCH_READY_FILE   where to write {"pid", "port", ...} once listening (used by run.py)
    BENCH_UPSTREAM     stub base URL (default http://127.0.0.1:8001)
    BENCH_SEED         seed for the initial data set (default 1234)
    BENCH_THREADS      waitress threads (default 8)
    BENCH_DB           sqlite path, relative to cwd (default orders.db; recreated on start)

SIGINT or SIGTERM stops the server cleanly, so interpreter exit hooks (the PyMolt watcher's
flush + metadata) run.
"""
from __future__ import annotations

import json
import logging
import os
import random
import signal
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

import requests
from flask import Flask, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    bindparam,
    create_engine,
    event,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, scoped_session, sessionmaker
from waitress import create_server

DB_PATH = os.environ.get("BENCH_DB", "orders.db")
UPSTREAM = os.environ.get("BENCH_UPSTREAM", "http://127.0.0.1:8001").rstrip("/")
SEED = int(os.environ.get("BENCH_SEED", "1234"))
THREADS = int(os.environ.get("BENCH_THREADS", "8"))
READY_FILE = os.environ.get("BENCH_READY_FILE")
RATE_TTL_S = float(os.environ.get("BENCH_RATE_TTL", "5"))
SWEEP_INTERVAL_S = float(os.environ.get("BENCH_SWEEP_INTERVAL", "1"))
PENDING_TTL_S = float(os.environ.get("BENCH_PENDING_TTL", "20"))
N_CUSTOMERS = 50
N_SEED_ORDERS = 500

CENTS = Decimal("0.01")
Currency = Literal["USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF"]

log = logging.getLogger("orders")


# ── persistence ──────────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(120), unique=True)
    tier: Mapped[str] = mapped_column(String(16), default="standard")


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    items: Mapped[list] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def make_engine():
    engine = create_engine(
        f"sqlite:///{DB_PATH}",
        pool_size=THREADS + 2,
        max_overflow=4,
        connect_args={"check_same_thread": False, "timeout": 15},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    return engine


engine = make_engine()
SessionFactory = sessionmaker(engine, expire_on_commit=False)
# SQLite has one writer at a time. Serializing write transactions in-process turns lock
# contention into a short queue instead of SQLITE_BUSY retries, whose busy handler sleeps in
# 1-100 ms steps and collapses throughput as soon as anything slows a writer down.
write_lock = threading.Lock()
db = scoped_session(SessionFactory)  # one session per request thread, removed on teardown

# Prebuilt statements (compiled once, cached by SQLAlchemy).
SUMMARY_STMT = (
    select(Order.status, func.count(Order.id), func.coalesce(func.sum(Order.total), 0))
    .where(Order.customer_id == bindparam("cid"))
    .group_by(Order.status)
    .order_by(Order.status)
)
EXPIRE_STMT = (
    update(Order)
    .where(Order.status == "pending", Order.created_at < bindparam("cutoff"))
    .values(status="expired")
)


def seed_database() -> None:
    """Recreate the schema and a deterministic data set (seeded RNG)."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except FileNotFoundError:
            pass
    Base.metadata.create_all(engine)
    rng = random.Random(SEED)
    now = datetime.now(UTC)
    tiers = ["standard"] * 6 + ["gold"] * 3 + ["platinum"]
    customers = [
        {"id": cid, "name": f"Customer {cid:03d}", "email": f"c{cid:03d}@example.test",
         "tier": rng.choice(tiers)}
        for cid in range(1, N_CUSTOMERS + 1)
    ]
    orders = []
    for oid in range(1, N_SEED_ORDERS + 1):
        items = [
            {"sku": f"{rng.choice(['ABC', 'DEF', 'XYZ', 'QRS'])}-{rng.randrange(10000):04d}",
             "qty": rng.randint(1, 5),
             "unit_price": f"{rng.randint(100, 20000) / 100:.2f}"}
            for _ in range(rng.randint(1, 4))
        ]
        total = sum(Decimal(i["unit_price"]) * i["qty"] for i in items)
        orders.append({
            "id": oid,
            "customer_id": rng.randint(1, N_CUSTOMERS),
            "status": rng.choices(["pending", "paid", "shipped"], weights=[3, 5, 2])[0],
            "currency": rng.choice(["USD", "USD", "EUR", "GBP"]),
            "total": total.quantize(CENTS),
            "items": items,
            "note": None,
            "created_at": now - timedelta(seconds=rng.randint(0, 3600)),
            "paid_at": None,
        })
    with SessionFactory.begin() as s:
        s.execute(insert(Customer), customers)
        s.execute(insert(Order), orders)


# ── API schemas (pydantic v2) ────────────────────────────────────────────────


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(pattern=r"^[A-Z]{3}-\d{4}$")
    qty: int = Field(gt=0, le=100)
    unit_price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)


class OrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: int = Field(ge=1, le=N_CUSTOMERS)
    currency: Currency = "USD"
    items: list[ItemIn] = Field(min_length=1, max_length=20)
    note: str | None = Field(default=None, max_length=200)


class ItemOut(BaseModel):
    sku: str
    qty: int
    unit_price: Decimal


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    customer_id: int
    status: str
    currency: str
    total: Decimal
    note: str | None = None
    created_at: datetime
    paid_at: datetime | None = None
    items: list[ItemOut]


class StatusBucket(BaseModel):
    status: str
    orders: int
    revenue: Decimal


class SummaryOut(BaseModel):
    customer_id: int
    name: str
    tier: str
    orders: int
    revenue: Decimal
    average_order: Decimal
    by_status: list[StatusBucket]


class ConvertQuery(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    to: Currency


class ConvertOut(BaseModel):
    amount: Decimal
    currency: str
    converted: Decimal
    rate: float
    cached: bool


def validation_error(exc: ValidationError):
    return {"errors": json.loads(exc.json(include_url=False))}, 422


# ── upstream rates (requests + TTL cache) ────────────────────────────────────


class UpstreamUnavailable(Exception):
    pass


_local = threading.local()
_rate_cache: dict[str, tuple[float, float]] = {}
_rate_lock = threading.Lock()
_stats = {"cache_hits": 0, "cache_misses": 0, "sweeps": 0, "expired": 0}
_stats_lock = threading.Lock()


def _bump(key: str, n: int = 1) -> None:
    with _stats_lock:
        _stats[key] += n


def _http() -> requests.Session:
    sess = getattr(_local, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers["User-Agent"] = "orders-service/1.0"
        _local.session = sess
    return sess


def usd_rate(currency: str) -> tuple[float, bool]:
    if currency == "USD":
        return 1.0, True
    now = time.monotonic()
    with _rate_lock:
        hit = _rate_cache.get(currency)
    if hit is not None and hit[1] > now:
        _bump("cache_hits")
        return hit[0], True
    _bump("cache_misses")
    resp = _http().get(f"{UPSTREAM}/rates/{currency}", timeout=2.0)
    if resp.status_code == 503:
        raise UpstreamUnavailable(currency)
    resp.raise_for_status()
    rate = float(resp.json()["rate"])
    with _rate_lock:
        _rate_cache[currency] = (rate, now + RATE_TTL_S)
    return rate, False


# ── HTTP app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.teardown_appcontext
def _remove_session(_exc=None):
    db.remove()


@app.post("/orders")
def create_order():
    try:
        payload = OrderIn.model_validate_json(request.get_data())
    except ValidationError as exc:
        return validation_error(exc)
    data = payload.model_dump(mode="json")
    total = sum((i.unit_price * i.qty for i in payload.items), Decimal("0"))
    order = Order(
        customer_id=payload.customer_id,
        status="pending",
        currency=payload.currency,
        total=total.quantize(CENTS, rounding=ROUND_HALF_UP),
        items=data["items"],
        note=payload.note,
        created_at=datetime.now(UTC),
    )
    with write_lock:
        db.add(order)
        db.commit()
    return OrderOut.model_validate(order).model_dump(mode="json"), 201


@app.get("/orders/<order_id>")
def get_order(order_id: str):
    if not order_id.isdigit():
        return {"error": "order id must be an integer", "got": order_id}, 400
    order = db.get(Order, int(order_id))
    if order is None:
        return {"error": "order not found", "id": int(order_id)}, 404
    return OrderOut.model_validate(order).model_dump(mode="json")


@app.get("/customers/<int:customer_id>/summary")
def customer_summary(customer_id: int):
    customer = db.get(Customer, customer_id)
    if customer is None:
        return {"error": "customer not found", "id": customer_id}, 404
    rows = db.execute(SUMMARY_STMT, {"cid": customer_id}).all()
    buckets = [
        StatusBucket(status=st, orders=n, revenue=Decimal(str(rev)).quantize(CENTS))
        for st, n, rev in rows
    ]
    n_orders = sum(b.orders for b in buckets)
    revenue = sum((b.revenue for b in buckets), Decimal("0"))
    avg = (revenue / n_orders).quantize(CENTS) if n_orders else Decimal("0.00")
    return SummaryOut(
        customer_id=customer_id, name=customer.name, tier=customer.tier,
        orders=n_orders, revenue=revenue, average_order=avg, by_status=buckets,
    ).model_dump(mode="json")


@app.get("/convert")
def convert():
    try:
        query = ConvertQuery.model_validate(request.args.to_dict())
    except ValidationError as exc:
        return validation_error(exc)
    try:
        rate, cached = usd_rate(query.to)
    except UpstreamUnavailable:
        return {"error": "rate upstream unavailable", "currency": query.to}, 502
    except requests.RequestException as exc:
        return {"error": "rate upstream failed", "detail": type(exc).__name__}, 504
    converted = (query.amount * Decimal(str(rate))).quantize(CENTS, rounding=ROUND_HALF_UP)
    return ConvertOut(
        amount=query.amount, currency=query.to, converted=converted, rate=rate, cached=cached,
    ).model_dump(mode="json")


@app.post("/orders/<int:order_id>/pay")
def pay_order(order_id: int):
    order = db.get(Order, order_id)
    if order is None:
        return {"error": "order not found", "id": order_id}, 404
    if order_id % 5 == 0:
        # The payment gateway's session for these accounts is broken: an unhandled
        # error that Flask turns into a 500 (and logs with a traceback).
        raise RuntimeError(f"payment gateway rejected session for order {order_id}")
    status = order.status
    if status == "expired":
        return {"error": "order expired", "id": order_id}, 409
    if status == "pending":
        with write_lock:
            order.status = status = "paid"
            order.paid_at = datetime.now(UTC)
            db.commit()
    return {"id": order_id, "status": status}


@app.get("/healthz")
def healthz():
    with _stats_lock:
        stats = dict(_stats)
    return {"ok": True, **stats}


# ── background sweeper ───────────────────────────────────────────────────────


def sweeper(stop: threading.Event) -> None:
    """Expire stale pending orders and prune the rate cache, every SWEEP_INTERVAL_S."""
    while not stop.wait(SWEEP_INTERVAL_S):
        try:
            cutoff = datetime.now(UTC) - timedelta(seconds=PENDING_TTL_S)
            with write_lock, SessionFactory.begin() as s:
                expired = s.execute(EXPIRE_STMT, {"cutoff": cutoff}).rowcount or 0
            now = time.monotonic()
            with _rate_lock:
                for cur in [c for c, (_, exp) in _rate_cache.items() if exp <= now]:
                    del _rate_cache[cur]
            _bump("sweeps")
            _bump("expired", expired)
        except Exception:  # noqa: BLE001 - a sweeper must survive a locked db
            log.exception("sweep failed")


# ── entry point ──────────────────────────────────────────────────────────────


def _write_ready(port: int) -> None:
    if not READY_FILE:
        return
    watcher = sorted(
        m for m in sys.modules
        if m in ("boundary_tracer", "sitecustomize", "_backend", "_sinks", "_monitoring")
    )
    info = {
        "pid": os.getpid(),
        "port": port,
        "python": sys.version.split()[0],
        "threads": THREADS,
        "watcher_modules": watcher,
        "profile_hook": sys.getprofile() is not None,
    }
    tmp = READY_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(info, fh)
    os.replace(tmp, READY_FILE)


def main() -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # waitress warns on every queued task once all threads are busy; at saturation that is
    # thousands of lines per second of log I/O that no real deployment would keep.
    logging.getLogger("waitress.queue").setLevel(logging.ERROR)

    def _terminate(signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _terminate)
    # Explicit: a parent started from a non-interactive shell may hand us SIGINT=SIG_IGN.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    seed_database()
    stop = threading.Event()
    sweep_thread = threading.Thread(target=sweeper, args=(stop,), name="sweeper", daemon=True)
    sweep_thread.start()
    server = create_server(
        app, host="127.0.0.1", port=0, threads=THREADS,
        connection_limit=2000, backlog=2048, channel_timeout=120, ident="orders",
    )
    _write_ready(int(server.effective_port))
    started = time.monotonic()
    try:
        server.run()  # returns on SIGINT (KeyboardInterrupt) / SIGTERM (SystemExit)
    finally:
        stop.set()
        sweep_thread.join(timeout=5)
        engine.dispose()
        with _stats_lock:
            stats = dict(_stats)
        print(json.dumps({"event": "shutdown", "uptime_s": round(time.monotonic() - started, 3),
                          **stats}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
