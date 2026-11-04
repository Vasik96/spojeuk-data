#!/usr/bin/env python3
"""
Zmenší GTFS na oblast: nechá jen spoje, které se dotknou BBOX (celé, se všemi zastávkami),
a k nim jen potřebné zastávky, linky, dopravce a kalendáře. Bez shapes.txt a transfers.txt.
Appka (GtfsImporter) čte přesně tyhle soubory, takže na její straně se nic nemění.

  python build_region_gtfs.py IN.(zip|tar) OUT.zip --lat 50.45 50.85 --lon 13.75 14.40
  python build_region_gtfs.py czptt.tar czptt-region.zip --lat 50.10 51.10 --lon 12.90 14.60
Stejné rozsahy jako BBOX_* / RAIL_BBOX_* v Config.kt.
"""
import argparse, csv, io, os, sys, tarfile, tempfile, zipfile


def rows(zf, name):
    if name not in zf.namelist():
        return None, iter(())
    f = io.TextIOWrapper(zf.open(name), encoding="utf-8-sig", newline="")
    r = csv.reader(f)
    return next(r), r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("out")
    ap.add_argument("--lat", nargs=2, type=float, required=True)
    ap.add_argument("--lon", nargs=2, type=float, required=True)
    a = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        src = a.src
        if src.endswith(".tar"):
            with tarfile.open(src) as t:
                m = next(x for x in t.getmembers() if x.name.endswith("gtfs.zip"))
                t.extract(m, tmp); src = os.path.join(tmp, m.name)
        zf = zipfile.ZipFile(src)

        # zastávky v oblasti (vlastní souřadnice, jinak rodič), stejně jako v appce
        h, r = rows(zf, "stops.txt")
        i = {k: n for n, k in enumerate(h)}
        stop_rows, coord, parent = [], {}, {}
        for x in r:
            stop_rows.append(x)
            sid = x[i["stop_id"]]
            parent[sid] = x[i["parent_station"]] if "parent_station" in i else ""
            try: coord[sid] = (float(x[i["stop_lat"]]), float(x[i["stop_lon"]]))
            except ValueError: pass
        def inreg(sid):
            c = coord.get(sid) or coord.get(parent.get(sid, ""))
            return bool(c) and a.lat[0] <= c[0] <= a.lat[1] and a.lon[0] <= c[1] <= a.lon[1]
        region = {s for s in parent if inreg(s)}

        # 1. průchod: které spoje se oblasti dotknou
        h, r = rows(zf, "stop_times.txt")
        ti, si = h.index("trip_id"), h.index("stop_id")
        keep = {x[ti] for x in r if x[si] in region}

        # 2. průchod: jejich stop_times + použité zastávky
        out = zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED, compresslevel=9)
        def writer(name, header):
            buf = io.StringIO(); w = csv.writer(buf, lineterminator="\n"); w.writerow(header)
            return buf, w
        used_stops = set()
        buf, w = writer("stop_times.txt", h)
        _, r = rows(zf, "stop_times.txt")
        for x in r:
            if x[ti] in keep:
                w.writerow(x); used_stops.add(x[si])
        out.writestr("stop_times.txt", buf.getvalue())

        # trips
        h, r = rows(zf, "trips.txt"); ti2 = h.index("trip_id"); ri = h.index("route_id"); sv = h.index("service_id")
        buf, w = writer("trips.txt", h)
        routes, services = set(), set()
        for x in r:
            if x[ti2] in keep:
                w.writerow(x); routes.add(x[ri]); services.add(x[sv])
        out.writestr("trips.txt", buf.getvalue())

        # stops (+ rodiče)
        need = set(used_stops)
        for s in list(used_stops):
            if parent.get(s): need.add(parent[s])
        h, r = rows(zf, "stops.txt"); buf, w = writer("stops.txt", h)
        for x in stop_rows:
            if x[i["stop_id"]] in need: w.writerow(x)
        out.writestr("stops.txt", buf.getvalue())

        # routes, agency, calendar, calendar_dates
        h, r = rows(zf, "routes.txt"); ri2 = h.index("route_id"); buf, w = writer("routes.txt", h)
        agencies = set(); ai = h.index("agency_id") if "agency_id" in h else None
        for x in r:
            if x[ri2] in routes:
                w.writerow(x)
                if ai is not None: agencies.add(x[ai])
        out.writestr("routes.txt", buf.getvalue())

        h, r = rows(zf, "agency.txt"); buf, w = writer("agency.txt", h)
        gi = h.index("agency_id") if "agency_id" in h else None
        for x in r:
            if gi is None or x[gi] in agencies: w.writerow(x)
        out.writestr("agency.txt", buf.getvalue())

        for name in ("calendar.txt", "calendar_dates.txt"):
            h, r = rows(zf, name)
            if h is None: continue
            k = h.index("service_id"); buf, w = writer(name, h)
            for x in r:
                if x[k] in services: w.writerow(x)
            out.writestr(name, buf.getvalue())
        out.close()
    print(f"spojů: {len(keep)}, zastávek: {len(need)}, {os.path.getsize(a.out)//1024} kB", file=sys.stderr)


if __name__ == "__main__":
    main()
