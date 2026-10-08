"""vaultsync dashboard: drive registry, custody, history, and a UI over the host agent."""
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

AGENT_URL = os.environ.get("AGENT_URL", "http://127.0.0.1:8710").rstrip("/")
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "/data/vaultsync.db")
STATIC = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="vaultsync")

# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS drives (
  id TEXT PRIMARY KEY, pool TEXT UNIQUE, label TEXT NOT NULL, serial TEXT, model TEXT,
  capacity INTEGER, custodian TEXT DEFAULT '', location TEXT DEFAULT '', notes TEXT DEFAULT '',
  created_at TEXT, retired_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT, drive_id TEXT, pool TEXT, status TEXT, mode TEXT,
  started TEXT, finished TEXT, bytes INTEGER, message TEXT, origin TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS custody (
  id INTEGER PRIMARY KEY AUTOINCREMENT, drive_id TEXT, custodian TEXT, location TEXT,
  notes TEXT, started_at TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS pending_inits (
  job_id TEXT PRIMARY KEY, label TEXT, custodian TEXT, location TEXT, notes TEXT,
  start_sync INTEGER, done INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS jobs_drive ON jobs(drive_id, finished);
"""
DEFAULT_SETTINGS = {"warn_days": "30", "stale_days": "60"}
_db_lock = threading.Lock()


@contextmanager
def db():
    with _db_lock:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as con:
        con.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def settings():
    with db() as con:
        return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}


# ---------------------------------------------------------------- agent client


class AgentError(Exception):
    def __init__(self, msg, status=502):
        super().__init__(msg)
        self.status = status


def agent(method, path, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(AGENT_URL + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {AGENT_TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", str(e))
        except Exception:  # noqa: BLE001
            msg = str(e)
        raise AgentError(msg, 409 if e.code == 409 else 502) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise AgentError(f"can't reach the host agent at {AGENT_URL}: {getattr(e, 'reason', e)}") from None


def call(method, path, body=None, timeout=15):
    try:
        return agent(method, path, body, timeout)
    except AgentError as e:
        raise HTTPException(e.status, str(e)) from None


# ---------------------------------------------------------------- job ingest


def ingest(jobs):
    """Copy agent job records into the local history, and finish pending drive setups."""
    started = []
    with db() as con:
        for j in jobs:
            res = j.get("result") or {}
            pool = j.get("pool") or res.get("pool")
            drive_id = res.get("drive_id") or (pool[3:] if pool and pool.startswith("vs-") else None)
            con.execute(
                "INSERT INTO jobs(id, kind, drive_id, pool, status, mode, started, finished, bytes, message, origin, data)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
                " mode=excluded.mode, started=excluded.started, finished=excluded.finished, bytes=excluded.bytes,"
                " message=excluded.message, drive_id=excluded.drive_id, pool=excluded.pool, data=excluded.data",
                (j["id"], j.get("kind"), drive_id, pool, j.get("status"), res.get("mode") or j.get("mode"),
                 j.get("started"), j.get("finished"), res.get("bytes") or j.get("bytes_done"),
                 j.get("message"), j.get("origin"), json.dumps(j)))
            if j.get("kind") == "init" and j.get("status") == "success" and res.get("drive_id"):
                pend = con.execute("SELECT * FROM pending_inits WHERE job_id=?", (j["id"],)).fetchone()
                exists = con.execute("SELECT 1 FROM drives WHERE id=?", (res["drive_id"],)).fetchone()
                if not exists:
                    custodian = pend["custodian"] if pend else ""
                    location = pend["location"] if pend else ""
                    con.execute(
                        "INSERT INTO drives(id, pool, label, serial, model, capacity, custodian, location, notes, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (res["drive_id"], res["pool"], res.get("label") or (pend and pend["label"]) or res["pool"],
                         res.get("serial"), res.get("model"), res.get("size"), custodian, location,
                         pend["notes"] if pend else "", j.get("finished") or now_iso()))
                    con.execute("INSERT INTO custody(drive_id, custodian, location, notes, started_at) VALUES (?,?,?,?,?)",
                                (res["drive_id"], custodian, location, "Drive set up", j.get("finished") or now_iso()))
                if pend and not pend["done"]:
                    con.execute("UPDATE pending_inits SET done=1 WHERE job_id=?", (j["id"],))
                    if pend["start_sync"]:
                        started.append(res["pool"])
    for pool in started:
        try:
            agent("POST", "/api/jobs", {"kind": "sync", "pool": pool})
        except AgentError:
            pass


def poller():
    while True:
        try:
            ingest(agent("GET", "/api/jobs?limit=200").get("jobs", []))
        except Exception:  # noqa: BLE001
            pass
        time.sleep(5)


@app.on_event("startup")
def _startup():
    init_db()
    threading.Thread(target=poller, daemon=True).start()


# ---------------------------------------------------------------- state


def drive_rows(con):
    rows = [dict(r) for r in con.execute("SELECT * FROM drives ORDER BY retired_at IS NOT NULL, label COLLATE NOCASE")]
    for r in rows:
        last = con.execute("SELECT finished, mode, bytes FROM jobs WHERE drive_id=? AND kind='sync' AND status='success'"
                           " ORDER BY finished DESC LIMIT 1", (r["id"],)).fetchone()
        lastjob = con.execute("SELECT id, kind, status, finished, message FROM jobs WHERE drive_id=? AND kind IN ('sync','verify')"
                              " ORDER BY COALESCE(finished, started) DESC LIMIT 1", (r["id"],)).fetchone()
        verify = con.execute("SELECT finished, status FROM jobs WHERE drive_id=? AND kind='verify' AND status IN ('success','failed')"
                             " ORDER BY finished DESC LIMIT 1", (r["id"],)).fetchone()
        r["last_sync"] = last["finished"] if last else None
        r["last_sync_bytes"] = last["bytes"] if last else None
        r["last_job"] = dict(lastjob) if lastjob else None
        r["last_verify"] = dict(verify) if verify else None
    return rows


@app.get("/api/state")
def get_state():
    agent_ok, agent_err, a = True, None, {}
    try:
        a = agent("GET", "/api/state", timeout=10)
        ingest(a.get("jobs", []))
    except AgentError as e:
        agent_ok, agent_err = False, str(e)
    with db() as con:
        drives = drive_rows(con)
    by_pool = {d["pool"]: d for d in drives}
    active = [j for j in a.get("jobs", []) if j.get("status") in ("running", "queued")]
    for d in drives:
        d["attached"] = None
        d["active_job"] = next((j for j in active if j.get("pool") == d["pool"]), None)
    unknown = []
    for disk in a.get("disks", []):
        if disk.get("vaultsync") and disk["pool"] in by_pool:
            by_pool[disk["pool"]]["attached"] = disk
        elif disk.get("port_mode") != "ignored":
            busy_init = next((j for j in active if j.get("kind") == "init"
                              and (j.get("params") or {}).get("serial") == disk.get("serial")), None)
            disk["active_job"] = busy_init
            unknown.append(disk)
    return {
        "agent_ok": agent_ok, "agent_error": agent_err, "agent_url": AGENT_URL,
        "host": a.get("host"), "source": a.get("source"), "ports": a.get("ports", []),
        "config": a.get("config", {}), "known": a.get("known", {}),
        "drives": drives, "unknown_disks": unknown, "active_jobs": active,
        "settings": settings(), "time": now_iso(),
    }


# ---------------------------------------------------------------- drives


def get_drive(con, drive_id):
    r = con.execute("SELECT * FROM drives WHERE id=?", (drive_id,)).fetchone()
    if not r:
        raise HTTPException(404, "unknown drive")
    return dict(r)


@app.get("/api/drives/{drive_id}")
def drive_detail(drive_id: str):
    with db() as con:
        d = get_drive(con, drive_id)
        d["custody"] = [dict(r) for r in con.execute(
            "SELECT * FROM custody WHERE drive_id=? ORDER BY started_at DESC, id DESC", (drive_id,))]
        d["jobs"] = [dict(r) for r in con.execute(
            "SELECT id, kind, status, mode, started, finished, bytes, message, origin FROM jobs"
            " WHERE drive_id=? ORDER BY COALESCE(started, finished) DESC LIMIT 200", (drive_id,))]
    return d


class NewDrive(BaseModel):
    device: str
    serial: str
    label: str
    confirm: str
    custodian: str = ""
    location: str = ""
    notes: str = ""
    start_sync: bool = True


@app.post("/api/drives")
def create_drive(body: NewDrive):
    label = body.label.strip()
    with db() as con:
        if con.execute("SELECT 1 FROM drives WHERE label=? COLLATE NOCASE AND retired_at IS NULL", (label,)).fetchone():
            raise HTTPException(409, f"a drive named '{label}' already exists")
    job = call("POST", "/api/jobs", {"kind": "init", "device": body.device, "serial": body.serial,
                                     "label": label, "confirm": body.confirm.strip()})
    with db() as con:
        con.execute("INSERT INTO pending_inits(job_id, label, custodian, location, notes, start_sync) VALUES (?,?,?,?,?,?)",
                    (job["id"], label, body.custodian.strip(), body.location.strip(), body.notes.strip(),
                     1 if body.start_sync else 0))
    return job


class Adopt(BaseModel):
    pool: str
    label: str
    custodian: str = ""
    location: str = ""
    notes: str = ""


@app.post("/api/drives/adopt")
def adopt_drive(body: Adopt):
    if not body.pool.startswith("vs-"):
        raise HTTPException(400, "not a vaultsync pool")
    drive_id = body.pool[3:]
    with db() as con:
        if con.execute("SELECT 1 FROM drives WHERE id=?", (drive_id,)).fetchone():
            con.execute("UPDATE drives SET retired_at=NULL WHERE id=?", (drive_id,))
            return {"ok": True, "id": drive_id}
        con.execute("INSERT INTO drives(id, pool, label, custodian, location, notes, created_at) VALUES (?,?,?,?,?,?,?)",
                    (drive_id, body.pool, body.label.strip() or body.pool, body.custodian.strip(),
                     body.location.strip(), body.notes.strip(), now_iso()))
        con.execute("INSERT INTO custody(drive_id, custodian, location, notes, started_at) VALUES (?,?,?,?,?)",
                    (drive_id, body.custodian.strip(), body.location.strip(), "Added to the registry", now_iso()))
    return {"ok": True, "id": drive_id}


class DriveEdit(BaseModel):
    label: str | None = None
    notes: str | None = None
    retired: bool | None = None


@app.patch("/api/drives/{drive_id}")
def edit_drive(drive_id: str, body: DriveEdit):
    with db() as con:
        get_drive(con, drive_id)
        if body.label is not None and body.label.strip():
            con.execute("UPDATE drives SET label=? WHERE id=?", (body.label.strip(), drive_id))
        if body.notes is not None:
            con.execute("UPDATE drives SET notes=? WHERE id=?", (body.notes.strip(), drive_id))
        if body.retired is not None:
            con.execute("UPDATE drives SET retired_at=? WHERE id=?", (now_iso() if body.retired else None, drive_id))
    return drive_detail(drive_id)


class Custody(BaseModel):
    custodian: str
    location: str = ""
    notes: str = ""


@app.post("/api/drives/{drive_id}/custody")
def hand_off(drive_id: str, body: Custody):
    t = now_iso()
    with db() as con:
        get_drive(con, drive_id)
        con.execute("UPDATE custody SET ended_at=? WHERE drive_id=? AND ended_at IS NULL", (t, drive_id))
        con.execute("INSERT INTO custody(drive_id, custodian, location, notes, started_at) VALUES (?,?,?,?,?)",
                    (drive_id, body.custodian.strip(), body.location.strip(), body.notes.strip(), t))
        con.execute("UPDATE drives SET custodian=?, location=? WHERE id=?",
                    (body.custodian.strip(), body.location.strip(), drive_id))
    return drive_detail(drive_id)


def _pool(drive_id):
    with db() as con:
        return get_drive(con, drive_id)["pool"]


@app.post("/api/drives/{drive_id}/plan")
def plan(drive_id: str):
    return call("POST", "/api/plan", {"pool": _pool(drive_id)}, timeout=120)


@app.post("/api/drives/{drive_id}/{action}")
def drive_action(drive_id: str, action: str):
    if action not in ("sync", "verify", "eject"):
        raise HTTPException(404, "unknown action")
    return call("POST", "/api/jobs", {"kind": action, "pool": _pool(drive_id)})


# ---------------------------------------------------------------- jobs, history, settings


@app.get("/api/jobs/{job_id}/log")
def job_log(job_id: str):
    return call("GET", f"/api/jobs/{job_id}/log?tail=400")


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    return call("POST", f"/api/jobs/{job_id}/cancel")


@app.get("/api/history")
def history(limit: int = 300):
    with db() as con:
        rows = con.execute(
            "SELECT j.id, j.kind, j.status, j.mode, j.started, j.finished, j.bytes, j.message, j.origin,"
            " j.drive_id, j.pool, d.label FROM jobs j LEFT JOIN drives d ON d.id = j.drive_id"
            " ORDER BY COALESCE(j.started, j.finished) DESC LIMIT ?", (limit,))
        return {"jobs": [dict(r) for r in rows]}


class SettingsBody(BaseModel):
    warn_days: int | None = None
    stale_days: int | None = None
    max_jobs: int | None = None
    verify_after_sync: bool | None = None
    stale_snapshot_days: int | None = None
    ports: dict | None = None


@app.put("/api/settings")
def put_settings(body: SettingsBody):
    if body.warn_days is not None or body.stale_days is not None:
        cur = settings()
        warn = body.warn_days if body.warn_days is not None else int(cur["warn_days"])
        stale = body.stale_days if body.stale_days is not None else int(cur["stale_days"])
        if not (1 <= warn < stale <= 3650):
            raise HTTPException(400, "warning days must be at least 1 and less than stale days")
        with db() as con:
            con.execute("UPDATE settings SET value=? WHERE key='warn_days'", (str(warn),))
            con.execute("UPDATE settings SET value=? WHERE key='stale_days'", (str(stale),))
    agent_body = {k: v for k, v in body.model_dump().items()
                  if k in ("max_jobs", "verify_after_sync", "stale_snapshot_days", "ports") and v is not None}
    if agent_body:
        call("PUT", "/api/config", agent_body)
    return {"ok": True}


@app.post("/api/cleanup")
def cleanup():
    return call("POST", "/api/cleanup", {}, timeout=120)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
