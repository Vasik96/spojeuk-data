#!/usr/bin/env python3
"""
Vyrobí train_shapes.sqlite (trasy vlaků) z czptt gtfs.zip z Oběhů.

Stejný formát jako shapes.sqlite, který čte ShapeStore:
  shape(id INTEGER PRIMARY KEY, pts BLOB)       pts = zlib(varint(n) + zigzag-varint delty lat/lon v mikrostupních)
  trip_shape(gid TEXT PRIMARY KEY, shape INTEGER)   gid = trip_id z trips.txt

Bere jen spoje, které se dotknou oblasti (stejný BBOX jako RAIL_BBOX v Config.kt).

Použití:
  python build_train_shapes.py czptt.tar|gtfs.zip  [-o out_dir]
Výstup: train_shapes.sqlite + train_shapes.json (built, trips, sha256)
"""
import argparse, csv, hashlib, io, json, os, sqlite3, sys, tarfile, tempfile, zipfile, zlib
from datetime import datetime, timezone

RAIL_LAT = (50.10, 51.10)   # drž v souladu s RAIL_BBOX_LAT / RAIL_BBOX_LON v Config.kt
RAIL_LON = (12.90, 14.60)


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def encode(points):
    raw = bytearray(varint(len(points)))
    la = lo = 0
    for lat, lon in points:
        ila, ilo = round(lat * 1e6), round(lon * 1e6)
        raw += varint(zigzag(ila - la)) + varint(zigzag(ilo - lo))
        la, lo = ila, ilo
    return zlib.compress(bytes(raw), 9)


def rows(zf, name):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))


def open_gtfs(path, tmp):
    if path.endswith(".tar"):
        with tarfile.open(path) as t:
            m = next(x for x in t.getmembers() if x.name.endswith("gtfs.zip"))
            t.extract(m, tmp)
            return os.path.join(tmp, m.name)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("-o", "--out", default=".")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        zf = zipfile.ZipFile(open_gtfs(a.src, tmp))

        # 1) zastávky v oblasti (jako v appce: vlastní souřadnice, jinak rodič)
        st, parent = {}, {}
        for r in rows(zf, "stops.txt"):
            try:
                st[r["stop_id"]] = (float(r["stop_lat"]), float(r["stop_lon"]))
            except ValueError:
                pass
            parent[r["stop_id"]] = r.get("parent_station", "")
        def inreg(sid):
            c = st.get(sid) or st.get(parent.get(sid, ""))
            return bool(c) and RAIL_LAT[0] <= c[0] <= RAIL_LAT[1] and RAIL_LON[0] <= c[1] <= RAIL_LON[1]
        region = {s for s in st if inreg(s)} | {s for s, p in parent.items() if p and inreg(p)}

        # 2) spoje, které se oblasti dotknou
        touch = set()
        for r in rows(zf, "stop_times.txt"):
            if r["stop_id"] in region:
                touch.add(r["trip_id"])
        trip_shape = {}
        for r in rows(zf, "trips.txt"):
            if r["trip_id"] in touch and r.get("shape_id"):
                trip_shape[r["trip_id"]] = r["shape_id"]
        need = set(trip_shape.values())
        print(f"spojů v oblasti: {len(trip_shape)}, tvarů: {len(need)}", file=sys.stderr)

        # 3) tvary (shapes.txt je velký, jede se po řádcích)
        pts = {}
        for r in rows(zf, "shapes.txt"):
            sid = r["shape_id"]
            if sid in need:
                pts.setdefault(sid, []).append((int(r["shape_pt_sequence"]), float(r["shape_pt_lat"]), float(r["shape_pt_lon"])))

    db_path = os.path.join(a.out, "train_shapes.sqlite")
    if os.path.exists(db_path):
        os.remove(db_path)
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE shape(id INTEGER PRIMARY KEY, pts BLOB)")
    db.execute("CREATE TABLE trip_shape(gid TEXT PRIMARY KEY, shape INTEGER)")
    ids = {}
    for i, (sid, p) in enumerate(pts.items(), 1):
        p.sort()
        db.execute("INSERT INTO shape VALUES(?,?)", (i, encode([(la, lo) for _, la, lo in p])))
        ids[sid] = i
    n = 0
    for gid, sid in trip_shape.items():
        if sid in ids:
            db.execute("INSERT INTO trip_shape VALUES(?,?)", (gid, ids[sid]))
            n += 1
    db.commit()
    db.execute("VACUUM")
    db.close()

    sha = hashlib.sha256(open(db_path, "rb").read()).hexdigest()
    meta = {"built": datetime.now(timezone.utc).isoformat(timespec="seconds"), "trips": n, "sha256": sha}
    json.dump(meta, open(os.path.join(a.out, "train_shapes.json"), "w"))
    print(json.dumps(meta), f"{os.path.getsize(db_path)//1024} kB", file=sys.stderr)


if __name__ == "__main__":
    main()
