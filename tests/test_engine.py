#!/usr/bin/env python3
"""End-to-end tests of the vaultsync sync engine against tests/fakezfs.py.

    python3 tests/test_engine.py
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = tempfile.mkdtemp(prefix="vstest-")
BIN = os.path.join(TMP, "bin")
os.makedirs(BIN)
for name in ("zfs", "zpool"):
    os.symlink(os.path.join(HERE, "fakezfs.py"), os.path.join(BIN, name))
os.chmod(os.path.join(HERE, "fakezfs.py"), 0o755)
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]
os.environ["FAKEZFS_STATE"] = os.path.join(TMP, "zfs.json")
os.environ["VAULTSYNC_CONFIG"] = os.path.join(TMP, "config.json")
os.environ["VAULTSYNC_STATE"] = os.path.join(TMP, "state")
os.environ["VAULTSYNC_RUN"] = os.path.join(TMP, "run")
os.environ["VAULTSYNC_STALL_CHECK"] = "2"

loader = importlib.machinery.SourceFileLoader("vaultsync", os.path.join(ROOT, "host", "vaultsync"))
spec = importlib.util.spec_from_loader("vaultsync", loader)
vs = importlib.util.module_from_spec(spec)
loader.exec_module(vs)


def zstate():
    return json.load(open(os.environ["FAKEZFS_STATE"]))


def zsave(st):
    json.dump(st, open(os.environ["FAKEZFS_STATE"], "w"), indent=1)


def ds(data):
    return {"data": data, "snaps": [], "bms": [], "props": {}, "token": None}


def drive_pool(st, pid, label, size=6_000_000_000):
    pool = f"vs-{pid}"
    st["pools"][pool] = {"imported": False, "size": size, "devices": ["/dev/disk/by-id/usb-X"]}
    st["ds"][pool] = ds(0)
    st["ds"][pool]["props"] = {"vaultsync:id": pid, "vaultsync:label": label}
    return pool


def setup():
    st = {"txg": 1, "pools": {"tank": {"imported": True, "size": 12_000_000_000, "devices": []}},
          "ds": {"tank": ds(1000), "tank/backups": ds(50_000), "tank/piwigo": ds(1000),
                 "tank/piwigo/galleries": ds(213), "tank/piwigo/upload": ds(3_000_000)}}
    drive_pool(st, "aaaa1111", "Alpha")
    drive_pool(st, "bbbb2222", "Bravo")
    drive_pool(st, "cccc3333", "Tiny", size=1000)
    zsave(st)
    vs.save_config(dict(vs.DEFAULT_CONFIG, token="t"))


def sync(pool, expect="success"):
    cfg = vs.load_config()
    job = vs.Job.create("sync", {"pool": pool})
    vs.execute(job, cfg)
    d = job.data
    assert d["status"] == expect, f"expected {expect}, got {d['status']}: {d['message']}"
    return d


def targets(st, pool):
    return {n: st["ds"][n] for n in st["ds"] if n.startswith(pool + "/")}


def check_clean(pool, pid, current=True):
    st = zstate()
    assert not st["pools"][pool]["imported"], "drive pool should be exported"
    for n, D in targets(st, pool).items():
        names = [s["name"] for s in D["snaps"]]
        assert len(names) == 1, f"{n} should keep exactly one snapshot, has {names}"
        assert D["token"] is None, f"{n} has a leftover resume token"
    src_snaps = [s["name"] for n in st["ds"] if n.startswith("tank") for s in st["ds"][n]["snaps"]
                 if s["name"].startswith(f"vaultsync-{pid}-")]
    assert not src_snaps, f"source still has snapshots {src_snaps}"
    for n in st["ds"]:
        if n == "tank" or n.startswith("tank/"):
            bms = [b["name"] for b in st["ds"][n]["bms"] if b["name"].startswith(f"vaultsync-{pid}-")]
            assert len(bms) == 1, f"{n} should have one bookmark for {pid}, has {bms}"
            tgt = pool + "/data" + n[4:]
            assert tgt in st["ds"], f"{tgt} missing on drive"
            if current:
                assert st["ds"][tgt]["data"] == st["ds"][n]["data"], f"{tgt} data mismatch"
            assert st["ds"][tgt]["snaps"][0]["guid"] == st["ds"][n]["bms"][-1]["guid"] or \
                any(b["guid"] == st["ds"][tgt]["snaps"][0]["guid"] for b in st["ds"][n]["bms"])


def bump(name, delta):
    st = zstate()
    st["ds"][name]["data"] += delta
    zsave(st)


def test():
    setup()
    A, B, C = "vs-aaaa1111", "vs-bbbb2222", "vs-cccc3333"

    d = sync(A)
    assert d["result"]["mode"] == "full", d["result"]
    check_clean(A, "aaaa1111")
    print("ok  full sync")

    bump("tank/piwigo/upload", 500_000)
    d = sync(A)
    assert d["result"]["mode"] == "incremental", d["result"]
    assert d["result"]["bytes"] < 600_000, d["result"]
    check_clean(A, "aaaa1111")
    print("ok  incremental sync copies only the change")

    st = zstate()
    st["ds"]["tank/piwigo/new"] = ds(7000)
    zsave(st)
    d = sync(A)
    assert d["result"]["mode"] == "incremental", "a new dataset alone doesn't make it a full sync"
    check_clean(A, "aaaa1111")
    print("ok  new dataset picked up")

    st = zstate()
    del st["ds"]["tank/backups"]
    zsave(st)
    sync(A)
    assert "vs-aaaa1111/data/backups" not in zstate()["ds"]
    check_clean(A, "aaaa1111")
    print("ok  dataset removed from source is removed from drive")

    sync(B)
    check_clean(B, "bbbb2222")
    bump("tank/piwigo/upload", 1000)
    d = sync(A)
    assert d["result"]["mode"] == "incremental"
    check_clean(A, "aaaa1111")
    check_clean(B, "bbbb2222", current=False)
    print("ok  two drives rotate independently")

    bump("tank/piwigo/upload", 800_000)
    os.environ["FAKEZFS_FAIL_RECV"] = f"{A}/data/piwigo/upload"
    d = sync(A, expect="failed")
    del os.environ["FAKEZFS_FAIL_RECV"]
    st = zstate()
    assert st["ds"][A]["props"].get("vaultsync:pending"), "pending marker should be set"
    assert st["ds"][f"{A}/data/piwigo/upload"]["token"], "resume token expected"
    print("ok  interrupted sync leaves a resumable state:", d["message"][:60])

    bump("tank/piwigo/upload", 10)
    d = sync(A)
    assert d["result"]["mode"] == "resume", d["result"]
    st = zstate()
    assert "vaultsync:pending" not in st["ds"][A]["props"]
    check_clean(A, "aaaa1111")
    print("ok  next sync resumes and then catches up")

    bump("tank/piwigo/upload", 300_000)
    os.environ["FAKEZFS_STALL_RECV"] = f"{A}/data/piwigo/upload"
    d = sync(A, expect="failed")
    del os.environ["FAKEZFS_STALL_RECV"]
    assert "stopped responding" in d["message"], d["message"]
    assert zstate()["pools"][A]["health"] == "SUSPENDED"
    try:
        vs.ensure_imported(A)
        raise AssertionError("sync onto a suspended pool should be refused")
    except vs.VSError as e:
        assert "reconnect" in str(e)
    vs.reconnect_pool(A)
    assert zstate()["pools"][A]["health"] == "ONLINE"
    st = zstate()
    st["ds"][f"{A}/data/piwigo/upload"]["token"] = None   # the fake doesn't save partial state on a stall
    zsave(st)
    d = sync(A)
    check_clean(A, "aaaa1111")
    print("ok  unplugged mid-sync: stall detected, reconnect, then resume:", d["result"]["mode"])

    d = sync(C, expect="failed")
    assert "needs about" in d["message"], d["message"]
    st = zstate()
    assert not any(s["name"].startswith("vaultsync-cccc3333") for s in st["ds"]["tank"]["snaps"])
    assert "vaultsync:pending" not in st["ds"][C]["props"]
    print("ok  too-small drive is refused cleanly:", d["message"][:70])

    st = zstate()
    st["ds"][f"{A}/data/piwigo"]["snaps"][0]["guid"] = "999"
    zsave(st)
    d = sync(A)
    assert d["result"]["mode"] == "full"
    check_clean(A, "aaaa1111")
    print("ok  diverged drive is rebuilt with a full copy")

    p = vs.plan_drive(A, vs.load_config())
    assert p["mode"] == "incremental" and p["fits"], p
    assert not zstate()["pools"][A]["imported"]
    assert not any(s["name"].startswith("vaultsync-plan") for s in zstate()["ds"]["tank"]["snaps"])
    print("ok  plan:", p["mode"], p["bytes"], "bytes")

    st = zstate()
    st["ds"]["tank"]["snaps"].append({"name": "vaultsync-dddd4444-20200101T000000Z", "guid": "1", "txg": 2, "data": 0})
    zsave(st)
    removed = vs.cleanup_stale(vs.load_config(), log=lambda *a: None)
    assert removed == ["tank@vaultsync-dddd4444-20200101T000000Z"], removed
    print("ok  stale snapshot cleanup")

    st = zstate()
    st["pools"][B]["imported"] = False
    zsave(st)
    r = vs.forget_drive(B, vs.load_config())
    st = zstate()
    assert not any(b["name"].startswith("vaultsync-bbbb2222") for n in st["ds"] if n.startswith("tank")
                   for b in st["ds"][n]["bms"]), "bookmarks for the forgotten drive should be gone"
    assert not any(j.get("pool") == B for j in vs.list_jobs())
    check_clean(A, "aaaa1111")
    print(f"ok  forget a retired drive: {r}")

    jobs = vs.list_jobs()
    assert all(j["status"] in ("success", "failed") for j in jobs)
    print(f"\nall engine tests passed ({len(jobs)} jobs)")


if __name__ == "__main__":
    try:
        test()
    finally:
        if "--keep" not in sys.argv:
            shutil.rmtree(TMP, ignore_errors=True)
