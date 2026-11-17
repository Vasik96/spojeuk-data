#!/usr/bin/env python3
"""add_pid_lines.py: do filtrovaného GTFS z obehy přidá spoje PID z plného feedu.

  python add_pid_lines.py jdf-filtered.tar jdf.tar OUT.zip

Proč: jdf-filtered vyhazuje linky, které "už publikují jiné feedy", tedy celou PID (pražské tramvaje,
metro, autobusy a regionální linky PID jako 100369). Appka pak pro PID vozidlo nemá žádný spoj ani trasu.
Plný jdf.tar je obsahuje. Linky PID se berou z jdf-filtered/line-snapshot.json (viz pid_lines.py).

Pozor na ID: filtrovaný a plný feed vznikly z různých běhů, takže se NEshodují
  - service_id (kalendáře se nekryjí vůbec),
  - stop_id (zhruba 3 % zastávek má pod stejným ID jiné jméno nebo souřadnice).
Proto se zastávka z plného feedu použije pod původním ID jen tehdy, když ve filtrovaném feedu neexistuje nebo
je shodná (jméno + souřadnice); jinak dostane předponu "pid:". Stejně se zachází se service_id a agency_id.
trip_id a route_id se nemění (linky PID ve filtrovaném feedu nejsou), takže klíč do shapes.sqlite zůstává stejný.

Výstup je GTFS zip ve stejném tvaru jako filtrovaný feed; dál ho zpracuje build_region_gtfs.py.
Jen stdlib. stop_times.txt (~0,9 GB) se čte proudově.
"""
import argparse, csv, io, os, shutil, sys, tarfile, tempfile, time, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pid_lines import PID_IDS_CODE, pid_lines_from_tar, route_line

PREFIX = "pid:"
MIN_ADDED_TRIPS = 1000   # pojistka: míň znamená, že se změnil formát obehy


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def extract_gtfs(tar_path, dest):
    with tarfile.open(tar_path, "r:") as tf:
        for m in tf:
            if m.isfile() and m.name.endswith("gtfs.zip"):
                with tf.extractfile(m) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                return dest
    raise SystemExit(f"{tar_path}: gtfs.zip nenalezen")


def reader(z, name):
    """(hlavička, csv.reader) nebo (None, prázdný iterátor), když soubor chybí."""
    if name not in z.namelist():
        return None, iter(())
    r = csv.reader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig", newline=""))
    return next(r), r


def dict_rows(z, name):
    if name not in z.namelist():
        return []
    return list(csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig", newline="")))


def header_of(z, name):
    h, _ = reader(z, name)
    return h


class Out:
    """Výstupní zip: obsah filtrovaného souboru + řádky navíc (sloupce podle hlavičky filtrovaného)."""

    def __init__(self, path, zl):
        self.z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True)
        self.zl = zl

    def copy_through(self, name):
        with self.zl.open(name) as src, self.z.open(name, "w", force_zip64=True) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)

    def append(self, name, new_rows):
        """Zkopíruje `name` z filtrovaného feedu a připíše řádky (iterátor dictů nebo seznamů podle hlavičky)."""
        header = header_of(self.zl, name)
        if header is None:
            raise SystemExit(f"filtrovaný feed nemá {name}")
        n = 0
        with self.zl.open(name) as src, self.z.open(name, "w", force_zip64=True) as dst:
            last = b"\n"
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
                last = chunk[-1:]
            if last != b"\n":
                dst.write(b"\n")
            text = io.TextIOWrapper(dst, encoding="utf-8", newline="")
            w = csv.writer(text, lineterminator="\n")
            for row in new_rows:
                w.writerow(row if isinstance(row, list) else [row.get(h, "") for h in header])
                n += 1
            text.flush()
            text.detach()
        return n

    def close(self):
        self.z.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("filtered_tar")
    ap.add_argument("full_tar")
    ap.add_argument("out")
    ap.add_argument("--ids-code", default=PID_IDS_CODE, help="kód IDS v line-snapshot.json (PID = 30001)")
    a = ap.parse_args()
    t0 = time.time()

    pid_lines = pid_lines_from_tar(a.filtered_tar, a.ids_code)
    log(f"Linek PID podle line-snapshot.json: {len(pid_lines)}")

    work = tempfile.mkdtemp(prefix="pid-merge-", dir=os.path.dirname(os.path.abspath(a.out)))
    try:
        log("Rozbaluji gtfs.zip z obou tarů…")
        zl = zipfile.ZipFile(extract_gtfs(a.filtered_tar, os.path.join(work, "filtered.zip")))
        zf = zipfile.ZipFile(extract_gtfs(a.full_tar, os.path.join(work, "full.zip")))

        # ---- co už filtrovaný feed má
        f_routes = {r["route_id"] for r in dict_rows(zl, "routes.txt")}
        f_agency = {r["agency_id"]: r["agency_name"] for r in dict_rows(zl, "agency.txt")}
        f_stops = {r["stop_id"]: (r["stop_name"], r["stop_lat"], r["stop_lon"]) for r in dict_rows(zl, "stops.txt")}
        f_services = {r["service_id"] for r in dict_rows(zl, "calendar.txt")}
        f_services |= {r["service_id"] for r in dict_rows(zl, "calendar_dates.txt")}
        h, rr = reader(zl, "trips.txt")
        k = h.index("trip_id")
        f_trips = {row[k] for row in rr}

        # ---- linky a spoje PID v plném feedu
        full_routes = dict_rows(zf, "routes.txt")
        pid_routes = {r["route_id"]: r for r in full_routes
                      if route_line(r["route_id"]) in pid_lines and r["route_id"] not in f_routes}
        log(f"Linek PID v plném feedu: {len(pid_routes)} (už ve filtrovaném: "
            f"{sum(1 for r in full_routes if route_line(r['route_id']) in pid_lines and r['route_id'] in f_routes)})")

        trips, services = {}, set()
        h, r = reader(zf, "trips.txt")
        ix = {k: n for n, k in enumerate(h)}
        for row in r:
            if row[ix["route_id"]] in pid_routes and row[ix["trip_id"]] not in f_trips:
                trips[row[ix["trip_id"]]] = dict(zip(h, row))
                services.add(row[ix["service_id"]])
        log(f"Spojů PID k přidání: {len(trips)}, služeb: {len(services)}")
        if len(trips) < MIN_ADDED_TRIPS:
            raise SystemExit(f"Podezřele málo spojů PID ({len(trips)} < {MIN_ADDED_TRIPS}), změnil se formát obehy?")

        # ---- přemapování ID
        agency_map, new_agency = {}, []
        pid_agencies = {x["agency_id"] for x in pid_routes.values()}
        for r_ in dict_rows(zf, "agency.txt"):
            aid = r_["agency_id"]
            if aid not in pid_agencies:
                continue
            if aid not in f_agency:
                agency_map[aid] = aid
                new_agency.append(r_)
            elif f_agency[aid] == r_["agency_name"]:
                agency_map[aid] = aid  # stejný dopravce, použije se ten z filtrovaného feedu
            else:
                agency_map[aid] = PREFIX + aid
                new_agency.append({**r_, "agency_id": PREFIX + aid})

        service_map = {s: (PREFIX + s if s in f_services else s) for s in services}
        new_calendar = [{**c, "service_id": service_map[c["service_id"]]}
                        for c in dict_rows(zf, "calendar.txt") if c["service_id"] in services]
        new_dates = [{**c, "service_id": service_map[c["service_id"]]}
                     for c in dict_rows(zf, "calendar_dates.txt") if c["service_id"] in services]

        full_stops = {r_["stop_id"]: r_ for r_ in dict_rows(zf, "stops.txt")}
        stop_map, new_stops, stats = {}, [], {"reused": 0, "added": 0, "renamed": 0, "unknown": 0}

        def map_stop(sid):
            if not sid:
                return sid
            if sid in stop_map:
                return stop_map[sid]
            s = full_stops.get(sid)
            if s is None:
                stats["unknown"] += 1
                stop_map[sid] = sid
                return sid
            sig = (s["stop_name"], s["stop_lat"], s["stop_lon"])
            if sid not in f_stops:
                new_id = sid
                stats["added"] += 1
            elif f_stops[sid] == sig:
                stop_map[sid] = sid
                stats["reused"] += 1
                return sid
            else:
                new_id = PREFIX + sid
                stats["renamed"] += 1
            stop_map[sid] = new_id
            new_stops.append({**s, "stop_id": new_id, "parent_station": map_stop(s.get("parent_station", ""))})
            return new_id

        # ---- zápis
        out = Out(a.out, zl)
        try:
            for name in zl.namelist():
                if name not in ("agency.txt", "routes.txt", "trips.txt", "stop_times.txt", "stops.txt",
                                "calendar.txt", "calendar_dates.txt"):
                    out.copy_through(name)

            out.append("agency.txt", new_agency)
            out.append("routes.txt", ({**r_, "agency_id": agency_map.get(r_["agency_id"], r_["agency_id"])}
                                      for r_ in pid_routes.values()))
            out.append("trips.txt", ({**t, "service_id": service_map[t["service_id"]]} for t in trips.values()))
            out.append("calendar.txt", new_calendar)
            out.append("calendar_dates.txt", new_dates)

            fh = header_of(zl, "stop_times.txt")
            h, r = reader(zf, "stop_times.txt")
            ix = {k: n for n, k in enumerate(h)}
            ti, si = ix["trip_id"], ix["stop_id"]
            order = [ix.get(k) for k in fh]   # sloupec filtrovaného feedu -> index v plném (nebo None)

            def st_rows():
                n = 0
                for row in r:
                    if row[ti] in trips:
                        row[si] = map_stop(row[si])
                        n += 1
                        yield [row[i] if i is not None and i < len(row) else "" for i in order]
                log(f"stop_times PID: {n} řádků")

            out.append("stop_times.txt", st_rows())
            out.append("stops.txt", new_stops)   # až po stop_times: tam se zjistí, které zastávky jsou potřeba
        finally:
            out.close()
        log(f"Zastávky: přidáno {stats['added']}, přejmenováno (kolize ID) {stats['renamed']}, "
            f"použito z filtrovaného {stats['reused']}, neznámé {stats['unknown']}")
        log(f"Hotovo za {time.time() - t0:.0f} s, {os.path.getsize(a.out) / 1e6:.0f} MB")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
