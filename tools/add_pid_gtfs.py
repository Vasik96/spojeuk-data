#!/usr/bin/env python3
"""add_pid_gtfs.py: doplní linky PID, které v CIS JDF nejsou, z oficiálního PID GTFS (data.pid.cz).

  python add_pid_gtfs.py IN.zip jdf-filtered.tar PID_GTFS.zip OUT.zip [--shapes-out DIR]

Proč: PID linky se do jízdních řádů berou z plného CIS JDF (add_pid_lines.py). Některé linky PID ale v CIS vůbec
nejsou: Airport Express (AE), výlukové a náhradní linky (X9, XS35 …), "MHD 1–9", BB1/BB2, přívoz P1, IKEA …
(desítky linek, tisíce spojů). Vozy těchto linek pak v appce nemají spoj, trasu ani cíl.

Co se přidá: spoje PID GTFS linek, jejichž označení (route_short_name) mezi linkami PID v IN.zip není.
Vlaky (route_type 2) ne, ty jsou v czptt. Všechna ID dostanou předponu "pidgtfs:" (PID GTFS má vlastní řady
zastávek, kalendářů i spojů, nesmí se plést s CIS). Zastávky se přednostně namapují na existující zastávky IN.zip
(stejné jméno do 150 m, jinak nejbližší do 40 m), aby fungovaly přestupy; ostatní se přidají jako nové.

--shapes-out: vedle zipu vyrobí pid_shapes.sqlite + pid_shapes.json (tvary tras doplněných spojů, stejný formát
jako shapes.sqlite / train_shapes.sqlite; klíč gid = trip_id s předponou).
Jen stdlib.
"""
import argparse, csv, hashlib, io, json, math, os, sqlite3, sys, unicodedata, zipfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pid_lines import pid_lines_from_tar, route_line
from build_train_shapes import encode

P = "pidgtfs:"
TRAIN = "2"
PRAGUE_ZONES = {"P", "0", "B"}
MIN_ADDED = 100   # pojistka: míň doplněných spojů = změnil se formát


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def dict_rows(z, name):
    if name not in z.namelist():
        return
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))


def header(z, name):
    with z.open(name) as f:
        return next(csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig", newline="")))


def norm_line(s):
    return " ".join(s.split()).upper()


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn").strip()


def dist_m(a, b, c, d):
    return math.hypot((d - b) * 111320.0 * math.cos(math.radians(a)), (c - a) * 110540.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("filtered_tar")
    ap.add_argument("pid_gtfs")
    ap.add_argument("out")
    ap.add_argument("--shapes-out")
    a = ap.parse_args()

    zin = zipfile.ZipFile(a.inp)
    zp = zipfile.ZipFile(a.pid_gtfs)

    # ── linky PID, které IN.zip už má (CIS linky PID podle line-snapshot + overlay spoje PID z obehy) ──
    pid_cis = pid_lines_from_tar(a.filtered_tar)
    have = set()
    for r in dict_rows(zin, "routes.txt"):
        if route_line(r["route_id"]) in pid_cis:
            have.add(norm_line(r["route_short_name"]))
    log(f"linek PID v IN.zip: {len(have)}")

    routes = {}
    for r in dict_rows(zp, "routes.txt"):
        if r.get("route_type") == TRAIN:
            continue
        if norm_line(r["route_short_name"]) not in have:
            routes[r["route_id"]] = r
    log(f"linek PID GTFS k doplnění: {len(routes)}: " + ", ".join(sorted({r['route_short_name'] for r in routes.values()})))

    trips = {r["trip_id"]: r for r in dict_rows(zp, "trips.txt") if r["route_id"] in routes}
    services = {t["service_id"] for t in trips.values()}
    log(f"spojů kandidátů: {len(trips)}")

    # ── zastávky: mapování na existující zastávky IN.zip ──
    used_stops = set()
    st_rows = []
    for r in dict_rows(zp, "stop_times.txt"):
        if r["trip_id"] in trips:
            st_rows.append(r)
            used_stops.add(r["stop_id"])
    pid_stops = {r["stop_id"]: r for r in dict_rows(zp, "stops.txt") if r["stop_id"] in used_stops}

    cell = 0.01
    grid = {}
    jdf_names = {}
    for r in dict_rows(zin, "stops.txt"):
        if r.get("location_type", "0") not in ("", "0"):
            continue
        try:
            la, lo = float(r["stop_lat"]), float(r["stop_lon"])
        except ValueError:
            continue
        grid.setdefault((int(la / cell), int(lo / cell)), []).append((r["stop_id"], la, lo, fold(r["stop_name"])))
        jdf_names[r["stop_id"]] = r["stop_name"]

    def jdf_style(r):
        n = r["stop_name"].strip()
        return n if "," in n or r.get("zone_id", "") not in PRAGUE_ZONES else f"Praha,{n}"

    stop_map, new_stops, by_name, by_near = {}, [], 0, 0
    for sid, r in pid_stops.items():
        la, lo = float(r["stop_lat"]), float(r["stop_lon"])
        want = fold(jdf_style(r))
        best_name, best_any = None, None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for (jid, jla, jlo, jname) in grid.get((int(la / cell) + di, int(lo / cell) + dj), ()):
                    d = dist_m(la, lo, jla, jlo)
                    if jname == want and d <= 150 and (best_name is None or d < best_name[0]):
                        best_name = (d, jid)
                    if d <= 40 and (best_any is None or d < best_any[0]):
                        best_any = (d, jid)
        if best_name:
            stop_map[sid] = best_name[1]; by_name += 1
        elif best_any:
            stop_map[sid] = best_any[1]; by_near += 1
        else:
            stop_map[sid] = P + sid
            new_stops.append(r)
    log(f"zastávek: {len(pid_stops)} (stejné jméno {by_name}, nejbližší {by_near}, nové {len(new_stops)})")

    # ── duplicity: stejné označení linky (401, 700 …) bývá v IN.zip pod jiným CIS číslem, které v seznamu PID není.
    #    Linka PID GTFS se přeskočí, když linka stejného označení v IN.zip obsluhuje aspoň 2 stejné zastávky. ──
    cand_names = {norm_line(r["route_short_name"]) for r in routes.values()}
    in_routes = {r["route_id"]: norm_line(r["route_short_name"]) for r in dict_rows(zin, "routes.txt")
                 if norm_line(r["route_short_name"]) in cand_names}
    in_trip_line = {t["trip_id"]: in_routes[t["route_id"]] for t in dict_rows(zin, "trips.txt") if t["route_id"] in in_routes}
    in_line_stops = {}
    for r in dict_rows(zin, "stop_times.txt"):
        ln = in_trip_line.get(r["trip_id"])
        if ln:
            in_line_stops.setdefault(ln, set()).add(r["stop_id"])
    pid_route_stops = {}
    for r in st_rows:
        pid_route_stops.setdefault(trips[r["trip_id"]]["route_id"], set()).add(stop_map[r["stop_id"]])
    dup = {rid for rid, r in routes.items()
           if len(pid_route_stops.get(rid, set()) & in_line_stops.get(norm_line(r["route_short_name"]), set())) >= 2}
    if dup:
        log("už v IN.zip (jiné CIS číslo), přeskočeno: " + ", ".join(sorted({routes[r]["route_short_name"] for r in dup})))
        for rid in dup:
            del routes[rid]
        trips = {k: t for k, t in trips.items() if t["route_id"] in routes}
        services = {t["service_id"] for t in trips.values()}
        st_rows = [r for r in st_rows if r["trip_id"] in trips]
        still = {r["stop_id"] for r in st_rows}
        new_stops = [r for r in new_stops if r["stop_id"] in still]
    log(f"doplňuje se {len(routes)} linek, {len(trips)} spojů: " + ", ".join(sorted({r['route_short_name'] for r in routes.values()})))
    if len(trips) < MIN_ADDED:
        raise SystemExit(f"doplněno jen {len(trips)} spojů, změnil se formát PID GTFS?")

    # ── výstup: IN.zip + připsané řádky (sloupce podle hlaviček IN.zip) ──
    zout = zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True)
    extra = {
        "agency.txt": [{"agency_id": P + r["agency_id"], "agency_name": r["agency_name"], "agency_url": r.get("agency_url", ""),
                        "agency_timezone": r.get("agency_timezone", "Europe/Prague"), "agency_lang": "cs"}
                       for r in dict_rows(zp, "agency.txt") if any(x["agency_id"] == r["agency_id"] for x in routes.values())],
        "routes.txt": [{"route_id": P + r["route_id"], "agency_id": P + r["agency_id"], "route_short_name": r["route_short_name"],
                        "route_long_name": r.get("route_long_name", ""), "route_type": r["route_type"],
                        "route_color": r.get("route_color", ""), "route_text_color": r.get("route_text_color", "")}
                       for r in routes.values()],
        "trips.txt": [{"route_id": P + t["route_id"], "service_id": P + t["service_id"], "trip_id": P + t["trip_id"],
                       "trip_headsign": t.get("trip_headsign", ""), "direction_id": t.get("direction_id", ""),
                       "shape_id": (P + t["shape_id"]) if t.get("shape_id") else "",
                       "wheelchair_accessible": t.get("wheelchair_accessible", "")}
                      for t in trips.values()],
        "stop_times.txt": [{"trip_id": P + r["trip_id"], "arrival_time": r["arrival_time"], "departure_time": r["departure_time"],
                            "stop_id": stop_map[r["stop_id"]], "stop_sequence": r["stop_sequence"],
                            "pickup_type": r.get("pickup_type", ""), "drop_off_type": r.get("drop_off_type", "")}
                           for r in st_rows],
        "stops.txt": [{"stop_id": P + r["stop_id"], "stop_name": jdf_style(r), "stop_lat": r["stop_lat"], "stop_lon": r["stop_lon"],
                       "location_type": "0", "wheelchair_boarding": r.get("wheelchair_boarding", "")}
                      for r in new_stops],
        "calendar.txt": [dict(r, service_id=P + r["service_id"]) for r in dict_rows(zp, "calendar.txt") if r["service_id"] in services],
        "calendar_dates.txt": [dict(r, service_id=P + r["service_id"]) for r in dict_rows(zp, "calendar_dates.txt") if r["service_id"] in services],
    }
    for name in zin.namelist():
        with zin.open(name) as src, zout.open(name, "w", force_zip64=True) as dst:
            last = b"\n"
            while chunk := src.read(1 << 20):
                dst.write(chunk)
                last = chunk[-1:]
            rows_ = extra.pop(name, None)
            if rows_:
                cols = header(zin, name)
                buf = io.StringIO()
                w = csv.writer(buf, lineterminator="\n")
                for r in rows_:
                    w.writerow([r.get(c, "") for c in cols])
                if last != b"\n":
                    dst.write(b"\n")   # původní soubor nekončil novým řádkem
                dst.write(buf.getvalue().encode("utf-8"))
    if extra:
        raise SystemExit(f"IN.zip nemá soubory {sorted(extra)}")
    zout.close()
    log(f"hotovo: {a.out}")

    if a.shapes_out:
        build_shapes(zp, trips, a.shapes_out)


def build_shapes(zp, trips, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    need = {t["shape_id"] for t in trips.values() if t.get("shape_id")}
    pts = {}
    for r in dict_rows(zp, "shapes.txt"):
        if r["shape_id"] in need:
            pts.setdefault(r["shape_id"], []).append((int(r["shape_pt_sequence"]), float(r["shape_pt_lat"]), float(r["shape_pt_lon"])))
    db_path = os.path.join(out_dir, "pid_shapes.sqlite")
    if os.path.exists(db_path):
        os.remove(db_path)
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE shape(id INTEGER PRIMARY KEY, pts BLOB)")
    db.execute("CREATE TABLE trip_shape(gid TEXT PRIMARY KEY, shape INTEGER)")
    ids = {}
    for i, (sid, p) in enumerate(pts.items(), 1):
        p.sort()
        if len(p) < 2:
            continue
        db.execute("INSERT INTO shape VALUES(?,?)", (i, encode([(la, lo) for _, la, lo in p])))
        ids[sid] = i
    n = 0
    for tid, t in trips.items():
        if t.get("shape_id") in ids:
            db.execute("INSERT INTO trip_shape VALUES(?,?)", (P + tid, ids[t["shape_id"]]))
            n += 1
    db.commit()
    db.execute("VACUUM")
    db.close()
    sha = hashlib.sha256(open(db_path, "rb").read()).hexdigest()
    meta = {"built": datetime.now(timezone.utc).isoformat(timespec="seconds"), "trips": n, "sha256": sha}
    json.dump(meta, open(os.path.join(out_dir, "pid_shapes.json"), "w"))
    log(f"pid_shapes: {len(ids)} tvarů, {n} spojů, {os.path.getsize(db_path) // 1024} kB")


if __name__ == "__main__":
    main()
