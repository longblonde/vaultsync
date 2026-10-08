#!/usr/bin/env python3
"""A tiny stand-in for `zfs` and `zpool`, good enough to exercise vaultsync's
sync logic (snapshots, bookmarks, guids, full/incremental/resumable streams)
without real ZFS. State lives in $FAKEZFS_STATE (JSON).

Failure injection: FAKEZFS_FAIL_RECV=<target dataset> makes `zfs recv` into
that dataset stop halfway and leave a resume token."""
import json
import os
import random
import sys

STATE = os.environ["FAKEZFS_STATE"]
SCALE = 1  # stream bytes per data unit


def load():
    with open(STATE) as f:
        return json.load(f)


def save(st):
    with open(STATE, "w") as f:
        json.dump(st, f, indent=1)


def die(msg, code=1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def pool_of(name):
    return name.split("/")[0].split("@")[0].split("#")[0]


def visible(st, ds):
    p = st["pools"].get(pool_of(ds))
    return p and p["imported"] and ds in st["ds"]


def children(st, ds):
    return sorted(n for n in st["ds"] if n == ds or n.startswith(ds + "/"))


def newguid():
    return str(random.randrange(10**15, 10**16))


def txg(st):
    st["txg"] += 1
    return st["txg"]


def split_obj(name):
    if "@" in name:
        d, s = name.split("@", 1)
        return d, "@", s
    if "#" in name:
        d, s = name.split("#", 1)
        return d, "#", s
    return name, "", ""


def find_obj(st, name):
    d, sep, s = split_obj(name)
    if not visible(st, d):
        return None
    D = st["ds"][d]
    if not sep:
        return D
    lst = D["snaps"] if sep == "@" else D["bms"]
    return next((x for x in lst if x["name"] == s), None)


def parse_flags(args, with_value=()):
    flags, rest, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and len(a) > 1 and not a[1:].isdigit():
            for j, ch in enumerate(a[1:]):
                if ch in with_value:
                    val = a[j + 2:] or args[i + 1]
                    if not a[j + 2:]:
                        i += 1
                    flags.setdefault(ch, []).append(val)
                    break
                flags[ch] = True
        else:
            rest.append(a)
        i += 1
    return flags, rest


def size_of(D):
    return D["data"]


def pool_used(st, pool):
    return sum(st["ds"][n]["data"] + sum(s.get("held", 0) for s in st["ds"][n]["snaps"])
               for n in st["ds"] if pool_of(n) == pool)


# ------------------------------------------------------------------ zfs


def zfs(args):
    st = load()
    cmd, args = args[0], args[1:]
    if cmd == "list":
        f, names = parse_flags(args, "tods")
        types = ",".join(f.get("t", ["filesystem"])).split(",")
        cols = f["o"][0].split(",")
        rows = []
        for name in names:
            d, sep, s = split_obj(name)
            if sep:
                o = find_obj(st, name)
                if not o:
                    die(f"cannot open '{name}': dataset does not exist")
                rows.append((name, o, "snap"))
                continue
            if not visible(st, d):
                die(f"cannot open '{name}': dataset does not exist")
            scope = children(st, d) if "r" in f else [d]
            for n in scope:
                D = st["ds"][n]
                if "filesystem" in types or "volume" in types:
                    rows.append((n, D, "fs"))
                if "snapshot" in types:
                    for s in D["snaps"]:
                        rows.append((f"{n}@{s['name']}", s, "snap"))
                if "bookmark" in types:
                    for b in D["bms"]:
                        rows.append((f"{n}#{b['name']}", b, "bm"))
        if "s" in f:
            rows.sort(key=lambda r: r[1].get("txg", 0))
        for name, o, kind in rows:
            vals = []
            for c in cols:
                if c == "name":
                    vals.append(name)
                elif c in ("used", "refer"):
                    vals.append(str(o.get("data", 0)))
                elif c == "avail":
                    p = st["pools"][pool_of(name)]
                    vals.append(str(p["size"] - pool_used(st, pool_of(name))))
                elif c == "guid":
                    vals.append(o.get("guid", "-"))
                elif c == "createtxg":
                    vals.append(str(o.get("txg", 0)))
            print("\t".join(vals))
        return
    if cmd == "get":
        f, rest = parse_flags(args, "o")
        props, name = rest[0].split(","), rest[1]
        o = find_obj(st, name)
        if o is None:
            die(f"cannot open '{name}'")
        for p in props:
            if p == "receive_resume_token":
                v = o.get("token") or "-"
            else:
                v = o.get("props", {}).get(p, "-")
            if f.get("o") == ["value"]:
                print(v)
            else:
                print(f"{p}\t{v}")
        return
    if cmd == "set":
        name = args[-1]
        D = find_obj(st, name)
        if D is None:
            die("no such dataset")
        for kv in args[:-1]:
            k, v = kv.split("=", 1)
            D.setdefault("props", {})[k] = v
        return save(st)
    if cmd == "inherit":
        D = find_obj(st, args[-1])
        D.get("props", {}).pop(args[0], None)
        return save(st)
    if cmd == "snapshot":
        f, rest = parse_flags(args)
        d, _, s = split_obj(rest[0])
        if not visible(st, d):
            die("no such dataset")
        t = txg(st)
        for n in (children(st, d) if "r" in f else [d]):
            D = st["ds"][n]
            if any(x["name"] == s for x in D["snaps"]):
                die(f"snapshot {n}@{s} exists")
            D["snaps"].append({"name": s, "guid": newguid(), "txg": t, "data": D["data"]})
        return save(st)
    if cmd == "bookmark":
        o = find_obj(st, args[0])
        if not o:
            die("no snapshot")
        d, _, b = split_obj(args[1])
        st["ds"][d]["bms"].append({"name": b, "guid": o["guid"], "txg": o["txg"], "data": o["data"]})
        return save(st)
    if cmd == "destroy":
        f, rest = parse_flags(args)
        name = rest[0]
        d, sep, s = split_obj(name)
        if not visible(st, d):
            die(f"could not find any snapshots to destroy; check snapshot names.")
        if sep == "@":
            for n in (children(st, d) if "r" in f else [d]):
                st["ds"][n]["snaps"] = [x for x in st["ds"][n]["snaps"] if x["name"] != s]
        elif sep == "#":
            st["ds"][d]["bms"] = [x for x in st["ds"][d]["bms"] if x["name"] != s]
        else:
            kids = children(st, d)
            if len(kids) > 1 and "r" not in f:
                die("has children")
            for n in kids:
                del st["ds"][n]
        return save(st)
    if cmd == "send":
        f, rest = parse_flags(args, "it")
        if "t" in f:
            hdr = json.loads(bytes.fromhex(f["t"][0]).decode())
            hdr["resume"] = True
            remaining = hdr["data"] - hdr["got"]
        else:
            d, _, s = split_obj(rest[0])
            snap = find_obj(st, rest[0])
            if not snap:
                die(f"cannot open '{rest[0]}': snapshot does not exist")
            if "i" in f:
                base = find_obj(st, f["i"][0])
                if not base:
                    die(f"incremental source {f['i'][0]} does not exist")
                data = max(100, abs(snap["data"] - base["data"]))
                hdr = {"type": "inc", "from": base["guid"], "guid": snap["guid"], "snap": s,
                       "data": data, "total": snap["data"], "txg": snap["txg"], "got": 0}
            else:
                hdr = {"type": "full", "guid": snap["guid"], "snap": s, "data": snap["data"],
                       "total": snap["data"], "txg": snap["txg"], "got": 0}
            remaining = hdr["data"]
        if "n" in f:
            print(f"size\t{remaining // SCALE}")
            return
        out = sys.stdout.buffer
        out.write((json.dumps(hdr) + "\n").encode())
        out.write(b"\0" * (remaining // SCALE))
        out.flush()
        return
    if cmd == "recv":
        f, rest = parse_flags(args)
        tgt = rest[0]
        if "A" in f:
            D = st["ds"].get(tgt)
            if D:
                D["token"] = None
                if not D["snaps"]:
                    del st["ds"][tgt]
            return save(st)
        line = sys.stdin.buffer.readline()
        hdr = json.loads(line)
        start_got = hdr["got"] if hdr.get("resume") else 0
        need = (hdr["data"] - start_got) // SCALE
        if os.environ.get("FAKEZFS_STALL_RECV") == tgt:
            sys.stdin.buffer.read(max(1, need // 2))
            st["pools"][pool_of(tgt)]["health"] = "SUSPENDED"
            save(st)
            import time
            time.sleep(3600)  # like a process blocked on a suspended pool
        fail = os.environ.get("FAKEZFS_FAIL_RECV") == tgt
        got = 0
        limit = need // 2 if fail else need
        while got < limit:
            chunk = sys.stdin.buffer.read(min(65536, limit - got))
            if not chunk:
                break
            got += len(chunk)
        parent = tgt.rsplit("/", 1)[0]
        if not visible(st, parent):
            die(f"cannot receive: parent {parent} does not exist")
        exists = visible(st, tgt)
        if hdr["type"] == "full" and not hdr.get("resume"):
            if exists:
                die(f"cannot receive new filesystem stream: destination '{tgt}' exists")
        if hdr["type"] == "inc" and not hdr.get("resume"):
            if not exists:
                die(f"cannot receive incremental stream: destination '{tgt}' does not exist")
            latest = st["ds"][tgt]["snaps"][-1] if st["ds"][tgt]["snaps"] else None
            if not latest or latest["guid"] != hdr["from"]:
                die("cannot receive incremental stream: most recent snapshot of "
                    f"{tgt} does not match incremental source")
        if fail or got < limit:
            hdr["got"] = start_got + got * SCALE
            tok = json.dumps({k: v for k, v in hdr.items() if k != "resume"}).encode().hex()
            if not exists:
                st["ds"][tgt] = {"data": 0, "snaps": [], "bms": [], "props": {}, "token": tok}
            else:
                st["ds"][tgt]["token"] = tok
            save(st)
            die("cannot receive: failed to read from stream (partial state saved)")
        if not exists:
            st["ds"][tgt] = {"data": 0, "snaps": [], "bms": [], "props": {}, "token": None}
        D = st["ds"][tgt]
        D["data"] = hdr["total"]
        D["token"] = None
        D["snaps"].append({"name": hdr["snap"], "guid": hdr["guid"], "txg": hdr["txg"], "data": hdr["total"]})
        return save(st)
    die(f"fakezfs: unsupported zfs {cmd}")


# ------------------------------------------------------------------ zpool


def zpool(args):
    st = load()
    cmd, args = args[0], args[1:]
    if cmd == "list":
        f, rest = parse_flags(args, "o")
        if "v" in f:
            p = st["pools"].get(rest[0])
            if not p or not p["imported"]:
                die("no such pool")
            print(f"{rest[0]}\t-\t-")
            for dev in p["devices"]:
                print(f"\t{dev}\t-\t-")
            return
        cols = f["o"][0].split(",")
        for name, p in sorted(st["pools"].items()):
            if not p["imported"] or (rest and name not in rest):
                continue
            used = pool_used(st, name)
            vals = {"name": name, "size": str(p["size"]), "alloc": str(used),
                    "free": str(p["size"] - used), "health": p.get("health", "ONLINE")}
            print("\t".join(vals[c] for c in cols))
        return
    if cmd == "import":
        f, rest = parse_flags(args, "o")
        p = st["pools"].get(rest[0])
        if not p:
            die(f"cannot import '{rest[0]}': no such pool available")
        if p["imported"]:
            die("pool already imported")
        p["imported"] = True
        return save(st)
    if cmd == "export":
        p = st["pools"].get(args[0])
        if not p or not p["imported"]:
            die("no such pool")
        if p.get("health", "ONLINE") != "ONLINE":
            die(f"cannot export '{args[0]}': pool I/O is currently suspended")
        p["imported"] = False
        return save(st)
    if cmd == "status":
        name = args[-1]
        print(f"  pool: {name}\n state: ONLINE\n  scan: scrub repaired 0B in 00:00:01 with 0 errors on Thu\n")
        return
    if cmd == "scrub":
        return
    if cmd == "create":
        f, rest = parse_flags(args, "oO")
        pool, dev = rest[0], rest[1]
        st["pools"][pool] = {"imported": True, "size": int(os.environ.get("FAKEZFS_NEWPOOL_SIZE", "6000000000000")),
                             "devices": [dev]}
        props = {}
        for o in f.get("O", []):
            k, v = o.split("=", 1)
            props[k] = v
        st["ds"][pool] = {"data": 0, "snaps": [], "bms": [], "props": props, "token": None}
        return save(st)
    if cmd == "clear":
        st["pools"][args[0]]["health"] = "ONLINE"
        return save(st)
    if cmd == "set":
        return
    if cmd == "labelclear":
        return
    die(f"fakezfs: unsupported zpool {cmd}")


if __name__ == "__main__":
    prog = os.path.basename(sys.argv[0])
    (zfs if prog == "zfs" else zpool)(sys.argv[1:])
