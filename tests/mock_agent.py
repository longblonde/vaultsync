#!/usr/bin/env python3
"""Fake host agent for working on the dashboard without a ZFS host.

    python3 tests/mock_agent.py            # listens on 127.0.0.1:8710, token "dev"

Simulates: two ports on a USB 3 controller plus USB 2 ports, one registered
drive attached, one blank drive attached, and jobs that progress over time.
POST /mock/plug {"disk": "alpha"|"blank"|"bravo", "attached": bool} toggles disks."""
import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TB = 1000 ** 4
GB = 1000 ** 3
lock = threading.Lock()


def iso(dt_):
    return dt_.replace(microsecond=0).isoformat()


NOW = datetime.now(timezone.utc)
CONFIG = {"max_jobs": 2, "verify_after_sync": False, "stale_snapshot_days": 14, "source": "tank", "exclude": [],
          "ports": {"0000:04:00.0:1": {"name": "Rear USB 3 left", "mode": "active"},
                    "0000:00:1a.0:1.1": {"name": "Front USB 2", "mode": "slow"}}}
PORTS = [
    {"key": "0000:04:00.0:1", "controller": "0000:04:00.0", "chain": "1", "max_speed": 5000, "buses": [3, 4]},
    {"key": "0000:04:00.0:2", "controller": "0000:04:00.0", "chain": "2", "max_speed": 5000, "buses": [3, 4]},
    {"key": "0000:05:00.0:1", "controller": "0000:05:00.0", "chain": "1", "max_speed": 5000, "buses": [5, 6]},
    {"key": "0000:05:00.0:2", "controller": "0000:05:00.0", "chain": "2", "max_speed": 5000, "buses": [5, 6]},
    {"key": "0000:00:1a.0:1.1", "controller": "0000:00:1a.0", "chain": "1.1", "max_speed": 480, "buses": [1]},
    {"key": "0000:00:1a.0:1.2", "controller": "0000:00:1a.0", "chain": "1.2", "max_speed": 480, "buses": [1]},
    {"key": "0000:00:1d.0:1.7", "controller": "0000:00:1d.0", "chain": "1.7", "max_speed": 480, "buses": [2]},
]
DISKS = {
    "alpha": {"device": "/dev/sds", "name": "sds", "model": "WDC WD60EZRZ-00GZ5B1", "serial": "WD-WX11D38NJ8AN",
              "size": 6001175126016, "port": "0000:04:00.0:1", "speed": 5000, "pool": "vs-3fa9c210",
              "vaultsync": True, "foreign_zfs": False, "imported": False, "drive_id": "3fa9c210", "label": "Alpha",
              "partitions": [{"name": "sds1", "size": 6001165000000, "fstype": "zfs_member", "label": "vs-3fa9c210"}],
              "mounted": []},
    "blank": {"device": "/dev/sdt", "name": "sdt", "model": "Seagate Expansion HDD", "serial": "NAAB12CD",
              "size": 8001563222016, "port": "0000:05:00.0:1", "speed": 5000, "pool": None, "vaultsync": False,
              "foreign_zfs": False, "imported": False, "drive_id": None, "label": None,
              "partitions": [{"name": "sdt1", "size": 8001562000000, "fstype": "ntfs", "label": "Seagate"}],
              "mounted": []},
    "bravo": {"device": "/dev/sdu", "name": "sdu", "model": "WD Elements 25A3", "serial": "WX52D7123456",
              "size": 12000138625024, "port": "0000:00:1a.0:1.1", "speed": 480, "pool": "vs-81c0d4e2",
              "vaultsync": True, "foreign_zfs": False, "imported": False, "drive_id": "81c0d4e2", "label": "Bravo",
              "partitions": [], "mounted": []},
}
ATTACHED = {"alpha": True, "blank": True, "bravo": False}
KNOWN = {"vs-3fa9c210": {"label": "Alpha"}, "vs-81c0d4e2": {"label": "Bravo"}}


def past_job(kind, pool, label, days_ago, status="success", mode="incremental", nbytes=2 * GB, msg=None):
    fin = NOW - timedelta(days=days_ago)
    jid = (fin - timedelta(minutes=12)).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    return {"id": jid, "kind": kind, "status": status, "phase": "done", "origin": "cli", "pool": pool,
            "label": label, "created": iso(fin - timedelta(minutes=12)), "started": iso(fin - timedelta(minutes=12)),
            "finished": iso(fin), "bytes_total": nbytes, "bytes_done": nbytes, "params": {"pool": pool},
            "message": msg or f"{mode} sync of {label} complete. Safe to unplug.",
            "result": {"drive_id": pool[3:], "pool": pool, "label": label, "mode": mode, "bytes": nbytes}}


JOBS = [
    past_job("init", "vs-3fa9c210", "Alpha", 40, mode=None, nbytes=0, msg="drive 'Alpha' ready as vs-3fa9c210"),
    past_job("sync", "vs-3fa9c210", "Alpha", 40, mode="full", nbytes=36 * GB),
    past_job("sync", "vs-3fa9c210", "Alpha", 33),
    past_job("init", "vs-81c0d4e2", "Bravo", 75, mode=None, nbytes=0, msg="drive 'Bravo' ready as vs-81c0d4e2"),
    past_job("sync", "vs-81c0d4e2", "Bravo", 75, mode="full", nbytes=30 * GB),
    past_job("sync", "vs-81c0d4e2", "Bravo", 71, status="failed", msg="tank/piwigo/upload failed: cannot receive"),
    past_job("sync", "vs-81c0d4e2", "Bravo", 70, mode="resume"),
]
JOBS[0]["result"].update(serial="WD-WX11D38NJ8AN", model="WDC WD60EZRZ-00GZ5B1", size=6001175126016)
JOBS[3]["result"].update(serial="WX52D7123456", model="WD Elements 25A3", size=12000138625024)
LOGS = {}
PROBLEMS = []


def ports_view():
    out = []
    for p in PORTS:
        q = dict(p)
        c = CONFIG["ports"].get(p["key"], {})
        q["name"], q["mode"] = c.get("name", ""), c.get("mode", "unconfigured")
        dev = None
        for k, d in DISKS.items():
            if ATTACHED[k] and d["port"] == p["key"]:
                dev = {"usb_id": "4-1", "product": d["model"], "manufacturer": "", "speed": d["speed"], "storage": True}
        if p["key"] == "0000:00:1d.0:1.7":
            dev = {"usb_id": "2-1.7", "product": "Bluetooth Radio", "manufacturer": "Intel", "speed": 12, "storage": False}
        q["device"] = dev
        out.append(q)
    return out


def disks_view():
    out = []
    active = {j["pool"] for j in JOBS if j["status"] in ("running", "queued")}
    for k, d in DISKS.items():
        if not ATTACHED[k]:
            continue
        e = dict(d)
        c = CONFIG["ports"].get(d["port"], {})
        e["port_name"], e["port_mode"] = c.get("name", ""), c.get("mode", "unconfigured")
        e["busy"] = d["pool"] in active
        e["by_id"] = f"/dev/disk/by-id/usb-{d['serial']}"
        out.append(e)
    return out


def tick():
    while True:
        time.sleep(1)
        with lock:
            for j in JOBS:
                if j["status"] == "queued":
                    j.update(status="running", started=iso(datetime.now(timezone.utc)), phase="importing")
                    LOGS[j["id"]].append(f"{time.strftime('%H:%M:%S')} importing {j['pool']}")
                elif j["status"] == "running":
                    if j.get("cancel"):
                        j.update(status="cancelled", phase="cancelled", finished=iso(datetime.now(timezone.utc)),
                                 message="cancelled")
                        continue
                    rate = 160e6 if j["kind"] == "sync" else 400e6
                    if j["kind"] in ("init", "eject"):
                        rate = j["bytes_total"] / 4
                    j["bytes_done"] = min(j["bytes_total"], j["bytes_done"] + rate * 1)
                    j["rate"] = rate
                    j["percent"] = round(100 * j["bytes_done"] / j["bytes_total"], 1)
                    j["eta"] = (j["bytes_total"] - j["bytes_done"]) / rate
                    j["phase"] = {"sync": "copying tank/piwigo/upload", "verify": "verifying",
                                  "init": "creating", "eject": "exporting"}[j["kind"]]
                    if j["bytes_done"] >= j["bytes_total"]:
                        finish(j)


def finish(j):
    t = iso(datetime.now(timezone.utc))
    j.update(status="success", phase="done", finished=t, eta=None)
    label = j.get("label")
    if j["kind"] == "sync":
        j["result"] = {"drive_id": j["pool"][3:], "pool": j["pool"], "label": label, "mode": j["mode"],
                       "bytes": j["bytes_total"]}
        j["message"] = f"{j['mode']} sync of {label} complete, {j['bytes_total'] / GB:.1f} GB copied. Safe to unplug."
    elif j["kind"] == "verify":
        j["result"] = {"drive_id": j["pool"][3:], "pool": j["pool"], "label": label, "verify_errors": 0}
        j["message"] = "verify complete, no errors found. Safe to unplug."
    elif j["kind"] == "init":
        did = secrets.token_hex(4)
        pool = f"vs-{did}"
        d = DISKS["blank"]
        d.update(pool=pool, vaultsync=True, drive_id=did, label=label,
                 partitions=[{"name": "sdt1", "size": d["size"], "fstype": "zfs_member", "label": pool}])
        KNOWN[pool] = {"label": label}
        j["pool"] = pool
        j["result"] = {"drive_id": did, "pool": pool, "label": label, "serial": d["serial"], "model": d["model"],
                       "size": d["size"]}
        j["message"] = f"drive '{label}' ready as {pool}"
    elif j["kind"] == "eject":
        for k, d in DISKS.items():
            if d["pool"] == j["pool"]:
                ATTACHED[k] = False
        j["result"] = {"pool": j["pool"], "drive_id": j["pool"][3:]}
        j["message"] = f"{label} ejected. Safe to unplug."
    LOGS[j["id"]].append(f"{time.strftime('%H:%M:%S')} SUCCESS: {j['message']}")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        with lock:
            p = self.path.split("?")[0]
            if p == "/api/state":
                act = [j for j in JOBS if j["status"] in ("running", "queued")]
                return self.send(200, {
                    "host": "snowowl", "version": "0.1.0-mock", "time": iso(datetime.now(timezone.utc)),
                    "source": {"name": "tank", "used": 35_800_000_000, "avail": 10_200_000_000_000,
                               "backup_bytes": 35_300_000_000,
                               "pool": {"size": 15_900_000_000_000, "alloc": 70_000_000_000, "free": 15_830_000_000_000,
                                        "health": "ONLINE"},
                               "datasets": [{"name": "tank", "used": 35_800_000_000, "refer": 100_000, "avail": 0, "excluded": False},
                                            {"name": "tank/backups", "used": 481_000_000, "refer": 481_000_000, "avail": 0, "excluded": False},
                                            {"name": "tank/piwigo", "used": 35_300_000_000, "refer": 100_000, "avail": 0, "excluded": False},
                                            {"name": "tank/piwigo/galleries", "used": 213_000, "refer": 213_000, "avail": 0, "excluded": False},
                                            {"name": "tank/piwigo/upload", "used": 35_300_000_000, "refer": 35_300_000_000, "avail": 0, "excluded": False}]},
                    "ports": ports_view(), "disks": disks_view(), "jobs": sorted(JOBS, key=lambda j: j["id"], reverse=True)[:50],
                    "known": KNOWN, "config": CONFIG, "problems": PROBLEMS})
            if p == "/api/jobs":
                return self.send(200, {"jobs": sorted(JOBS, key=lambda j: j["id"], reverse=True)})
            if p.endswith("/log"):
                jid = p.split("/")[3]
                return self.send(200, {"lines": LOGS.get(jid, ["(no log kept for this mock job)"])})
            return self.send(404, {"error": "not found"})

    def do_PUT(self):
        with lock:
            b = self.body()
            for k in ("max_jobs", "verify_after_sync", "stale_snapshot_days", "ports"):
                if k in b:
                    CONFIG[k] = b[k]
            return self.send(200, CONFIG)

    def do_POST(self):
        with lock:
            p = self.path
            b = self.body()
            if p == "/mock/plug":
                ATTACHED[b["disk"]] = bool(b.get("attached", True))
                return self.send(200, ATTACHED)
            if p == "/api/plan":
                d = next(d for d in DISKS.values() if d["pool"] == b["pool"])
                return self.send(200, {"pool": b["pool"], "label": d["label"], "mode": "incremental" if d["label"] != "Ivy" else "full",
                                       "reset": False, "bytes": 1_240_000_000 if d["label"] != "Ivy" else 35_800_000_000,
                                       "drive_free": d["size"] - 36 * GB, "drive_size": d["size"], "fits": True, "pending": None,
                                       "datasets": []})
            if p == "/mock/problem":
                PROBLEMS[:] = [{"pool": b["pool"], "health": "SUSPENDED", "label": KNOWN.get(b["pool"], {}).get("label"),
                                "attached": True}] if b.get("on", True) else []
                return self.send(200, {"ok": True})
            if p == "/api/forget":
                if any(ATTACHED[k] and d["pool"] == b["pool"] for k, d in DISKS.items()):
                    return self.send(409, {"error": "unplug the drive before deleting it"})
                JOBS[:] = [j for j in JOBS if j["pool"] != b["pool"]]
                KNOWN.pop(b["pool"], None)
                return self.send(200, {"pool": b["pool"], "zfs_objects_removed": 5, "jobs_removed": 3})
            if p == "/api/reconnect":
                PROBLEMS[:] = [x for x in PROBLEMS if x["pool"] != b["pool"]]
                return self.send(200, {"pool": b["pool"], "health": "ONLINE"})
            if p == "/api/cleanup":
                return self.send(200, {"removed": []})
            if p.endswith("/cancel"):
                jid = p.split("/")[3]
                for j in JOBS:
                    if j["id"] == jid:
                        j["cancel"] = True
                return self.send(200, {"ok": True})
            if p == "/api/jobs":
                active = [j for j in JOBS if j["status"] in ("running", "queued")]
                if len(active) >= int(CONFIG["max_jobs"]) and b["kind"] != "eject":
                    return self.send(409, {"error": f"{len(active)} job(s) already running (limit {CONFIG['max_jobs']})"})
                if b["kind"] == "init":
                    if b.get("confirm") != b.get("label"):
                        return self.send(409, {"error": "type the drive label to confirm erasing it"})
                    pool, label, total = None, b["label"], 4
                else:
                    pool = b["pool"]
                    if any(j["pool"] == pool for j in active):
                        return self.send(409, {"error": f"a job is already running for {pool}"})
                    label = KNOWN.get(pool, {}).get("label")
                    total = {"sync": 6 * GB, "verify": 36 * GB, "eject": 4}[b["kind"]]
                jid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
                j = {"id": jid, "kind": b["kind"], "status": "queued", "phase": "queued", "origin": "dashboard",
                     "pool": pool, "label": label, "created": iso(datetime.now(timezone.utc)), "started": None,
                     "finished": None, "bytes_total": total, "bytes_done": 0, "rate": 0, "eta": None, "percent": 0,
                     "message": "", "result": {}, "params": b, "mode": "incremental"}
                JOBS.append(j)
                LOGS[jid] = [f"{time.strftime('%H:%M:%S')} queued {b['kind']}"]
                return self.send(200, j)
            return self.send(404, {"error": "not found"})


if __name__ == "__main__":
    threading.Thread(target=tick, daemon=True).start()
    print("mock agent on 127.0.0.1:8710")
    ThreadingHTTPServer(("127.0.0.1", 8710), H).serve_forever()
