#!/usr/bin/env python3
"""build_shapes.py: z obehy jdf.tar vyrobí malý shapes.sqlite s přesnými trasami spojů.

Vstup:  --full      jdf.tar          (obsahuje gtfs.zip s shapes.txt, ~1,1 GB)
        --filtered  jdf-filtered.tar (gtfs.zip, ze kterého appka staví jízdní řády, bez tvarů)
Výstup: --out shapes.sqlite  + --meta shapes.json (verze, počty, sha256)

Schéma:  shape(id, pts BLOB)  a  trip_shape(gid TEXT = GTFS trip_id, shape INTEGER)
pts = zlib( varint(počet) + zigzag-varint delty lat/lon v mikrostupních ), viz ShapeStore.kt.
Jen stdlib. Čte po řádcích, paměť v řádu desítek MB.
"""
import argparse, csv, hashlib, io, json, math, os, sqlite3, sys, tarfile, time, zipfile, zlib
from datetime import datetime, timezone

BBOX = (50.45, 50.85, 13.75, 14.40)   # stejné jako BBOX_LAT / BBOX_LON v appce
MARGIN = 0.02                         # ~2 km, ať se nepřijde o trasy těsně u okraje
EPS_M = 3.0                           # zjednodušení tvaru (Douglas-Peucker)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def extract_gtfs(tar_path, dest):
    with tarfile.open(tar_path, "r:") as tf:
        for m in tf:
            if m.isfile() and m.name.endswith("gtfs.zip"):
                with tf.extractfile(m) as src, open(dest, "wb") as out:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
                return dest
    raise SystemExit(f"{tar_path}: gtfs.zip nenalezen")


def rows(z, name):
    r = csv.reader(io.TextIOWrapper(z.open(name), "utf-8-sig", newline=""))
    ix = {h: i for i, h in enumerate(next(r))}
    return ix, r


def simplify(pts, eps):
    n = len(pts)
    if n < 3:
        return pts
    kx = 111320.0 * math.cos(math.radians(pts[0][0]))
    ky = 110540.0
    xy = [(p[1] * kx, p[0] * ky) for p in pts]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    e2 = eps * eps
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        dx, dy = xy[b][0] - ax, xy[b][1] - ay
        l2 = dx * dx + dy * dy
        dmax, idx = 0.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            if l2 == 0:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / l2
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
                qx, qy = ax + t * dx - px, ay + t * dy - py
                d2 = qx * qx + qy * qy
            if d2 > dmax:
                dmax, idx = d2, i
        if idx >= 0 and dmax > e2:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(pts, keep) if k]


def _varint(n, out):
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)


def _zz(v):
    return v << 1 if v >= 0 else ((-v) << 1) - 1


def encode(pts):
    out = bytearray()
    _varint(len(pts), out)
    pla = plo = 0
    for la, lo in pts:
        a, o = round(la * 1e6), round(lo * 1e6)
        _varint(_zz(a - pla), out)
        _varint(_zz(o - plo), out)
        pla, plo = a, o
    return zlib.compress(bytes(out), 9)


def decode(blob):
    raw = zlib.decompress(blob)
    p = 0

    def varint():
        nonlocal p
        r = s = 0
        while True:
            b = raw[p]
            p += 1
            r |= (b & 0x7F) << s
            if not b & 0x80:
                return r
            s += 7

    n = varint()
    la = lo = 0
    out = []
    for _ in range(n):
        d = varint(); la += (d >> 1) ^ -(d & 1)
        d = varint(); lo += (d >> 1) ^ -(d & 1)
        out.append((la / 1e6, lo / 1e6))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", required=True)
    ap.add_argument("--filtered", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--meta", required=True)
    ap.add_argument("--upstream", default="")
    ap.add_argument("--prev", help="shapes.json předchozí verze (kontrola, že nový build není podezřele malý)")
    ap.add_argument("--min-ratio", type=float, default=0.9)
    ap.add_argument("--min-trips", type=int, default=1000)
    a = ap.parse_args()
    t0 = time.time()
    work = os.path.dirname(os.path.abspath(a.out))

    log("Rozbaluji gtfs.zip z obou tarů…")
    zf = zipfile.ZipFile(extract_gtfs(a.full, os.path.join(work, "full-gtfs.zip")))
    zl = zipfile.ZipFile(extract_gtfs(a.filtered, os.path.join(work, "filtered-gtfs.zip")))
    if "shapes.txt" not in zf.namelist():
        raise SystemExit("jdf.tar neobsahuje shapes.txt, změnil se formát obehy?")

    # 1) spoje, které appka zná (filtrovaný feed)
    ix, r = rows(zl, "trips.txt")
    wanted = {row[ix["trip_id"]] for row in r}
    log(f"Spojů ve filtrovaném feedu: {len(wanted)}")

    # 2) jejich shape_id z plného feedu
    ix, r = rows(zf, "trips.txt")
    c_t, c_s = ix["trip_id"], ix["shape_id"]
    trip_shape = {}
    for row in r:
        if row[c_t] in wanted and row[c_s]:
            trip_shape[row[c_t]] = row[c_s]
    ratio_found = len(trip_shape) / max(1, len(wanted))
    log(f"Z nich s tvarem v plném feedu: {len(trip_shape)} ({ratio_found:.0%})")
    if ratio_found < 0.5:
        raise SystemExit("Většina trip_id se v plném feedu nenašla, změnilo se číslování? Končím.")
    need = set(trip_shape.values())

    # 3) tvary: jen ty, které zasahují do oblasti
    la0, la1, lo0, lo1 = BBOX
    la0 -= MARGIN; la1 += MARGIN; lo0 -= MARGIN; lo1 += MARGIN
    ix, r = rows(zf, "shapes.txt")
    c_i, c_la, c_lo, c_q = ix["shape_id"], ix["shape_pt_lat"], ix["shape_pt_lon"], ix["shape_pt_sequence"]
    kept, finished = {}, set()
    cur, buf, hit = None, [], False

    def flush():
        if cur is not None and cur in need and hit and len(buf) >= 2:
            buf.sort(key=lambda x: x[0])
            kept[cur] = [(la, lo) for _, la, lo in buf]

    for row in r:
        sid = row[c_i]
        if sid != cur:
            flush()
            if cur is not None:
                finished.add(cur)
            if sid in finished:
                raise SystemExit(f"shapes.txt není seřazený podle shape_id ({sid} se vrací), končím.")
            cur, buf, hit = sid, [], False
        if sid not in need:
            continue
        la, lo = float(row[c_la]), float(row[c_lo])
        buf.append((int(row[c_q]), la, lo))
        if not hit and la0 <= la <= la1 and lo0 <= lo <= lo1:
            hit = True
    flush()
    log(f"Tvarů v oblasti: {len(kept)}")

    # 4) zjednodušit, zakódovat, uložit
    if os.path.exists(a.out):
        os.remove(a.out)
    db = sqlite3.connect(a.out)
    db.executescript("""
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE shape(id INTEGER PRIMARY KEY, pts BLOB NOT NULL);
        CREATE TABLE trip_shape(gid TEXT PRIMARY KEY, shape INTEGER NOT NULL) WITHOUT ROWID;
    """)
    sid_of, pts_in, pts_out = {}, 0, 0
    for n, (g, pts) in enumerate(kept.items(), 1):
        simple = simplify(pts, EPS_M)
        pts_in += len(pts); pts_out += len(simple)
        db.execute("INSERT INTO shape VALUES(?,?)", (n, encode(simple)))
        sid_of[g] = n
    pairs = [(t, sid_of[s]) for t, s in trip_shape.items() if s in sid_of]
    db.executemany("INSERT INTO trip_shape VALUES(?,?)", pairs)
    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.executemany("INSERT INTO meta VALUES(?,?)", [
        ("built", built), ("upstream", a.upstream), ("trips", str(len(pairs))), ("shapes", str(len(sid_of)))])
    db.commit()
    db.execute("VACUUM")
    db.close()
    log(f"Body: {pts_in} -> {pts_out}; spojů s trasou: {len(pairs)}; velikost: {os.path.getsize(a.out) / 1e6:.1f} MB")

    # 5) pojistky proti špatnému buildu
    if len(pairs) < a.min_trips:
        raise SystemExit(f"Podezřele málo spojů ({len(pairs)} < {a.min_trips}), nepublikuji.")
    if a.prev and os.path.exists(a.prev):
        try:
            prev = json.load(open(a.prev))["trips"]
            if len(pairs) < prev * a.min_ratio:
                raise SystemExit(f"Spojů s trasou {len(pairs)} je méně než {a.min_ratio:.0%} z minula ({prev}), nepublikuji.")
        except (ValueError, KeyError):
            log("prev meta nečitelná, kontrola přeskočena")

    sha = hashlib.sha256(open(a.out, "rb").read()).hexdigest()
    json.dump({"schema": 1, "built": built, "upstream": a.upstream, "trips": len(pairs),
               "shapes": len(sid_of), "size": os.path.getsize(a.out), "sha256": sha}, open(a.meta, "w"))
    os.remove(os.path.join(work, "full-gtfs.zip"))
    os.remove(os.path.join(work, "filtered-gtfs.zip"))
    log(f"Hotovo za {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
