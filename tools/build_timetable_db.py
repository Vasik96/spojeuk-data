#!/usr/bin/env python3
"""Postaví hotovou databázi jízdních řádů (static.sqlite) pro appku SpojeUK.

Dřív appka stahovala jdf-region.zip + czptt-region.zip a v telefonu je importovala
(stop_times.txt má přes 500 MB => minuty CPU a "Zpracovávám jízdní řády…"). Teď to samé
udělá tenhle skript v GitHub Actions a appka si jen stáhne a rozbalí hotovou DB.

Je to věrný port GtfsImporter (app/.../Data.kt) a pairRailStations (StationPairing.kt):
stejné tabulky, stejné pořadí ID, stejné parsování CSV a časů. Při změně importeru v appce
je potřeba změnit i tohle (a zvednout SCHEMA).

Použití:
  build_timetable_db.py --bus jdf-region.zip --rail czptt-region.zip --tag TAG --out static.sqlite
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import re
import sqlite3
import struct
import sys
import time
import unicodedata
import zipfile
from zoneinfo import ZoneInfo

# Verze formátu DB; appka stáhne hotovou DB jen pokud ji umí (jinak importuje zipy jako dřív).
SCHEMA = 1

PRAGUE = ZoneInfo("Europe/Prague")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ───────────────────────── CSV jako v appce (CsvScanner) ─────────────────────────
# Řádky po \n, koncové \r se odřízne, prázdné řádky se přeskočí. Pole v uvozovkách končí první
# samostatnou uvozovkou (řádky přes víc řádků appka nepodporuje, tady také ne).

def _split_quoted(line: bytes, max_col: int) -> list[tuple[int, int]]:
    """Přesná kopie smyčky v CsvScanner.nextRow (offsety polí, uvozovky bez \"\" náhrady)."""
    out = []
    le = len(line)
    p = 0
    col = 0
    while col <= max_col and p <= le:
        if p < le and line[p] == 34:
            s = p + 1
            q = s
            while q < le:
                if line[q] == 34:
                    if q + 1 < le and line[q + 1] == 34:
                        q += 2
                    else:
                        break
                else:
                    q += 1
            out.append((s, q))
            c = q + 1
            while c < le and line[c] != 44:
                c += 1
            p = c + 1
        else:
            c = p
            while c < le and line[c] != 44:
                c += 1
            out.append((p, c))
            p = c + 1
        col += 1
    while col <= max_col:
        out.append((0, 0))
        col += 1
    return out


class Csv:
    """Iterace řádků; každý řádek = seznam surových bajtů požadovaných sloupců (b"" když chybí)."""

    def __init__(self, zf: zipfile.ZipFile, name: str, wanted: list[str]):
        self.zf, self.name, self.wanted = zf, name, wanted
        self.idx: list[int] = []

    def exists(self) -> bool:
        return self.name in self.zf.namelist()

    def has(self, k: int) -> bool:
        return self.idx[k] >= 0

    def rows(self):
        if not self.exists():
            return
        with self.zf.open(self.name) as f:
            header = None
            for raw in f:
                line = raw[:-1] if raw.endswith(b"\n") else raw
                if line.endswith(b"\r"):
                    line = line[:-1]
                if header is None:
                    h = line.decode("utf-8", "replace")
                    if h.startswith("﻿"):
                        h = h[1:]
                    names = [x.strip().strip('"') for x in h.split(",")]
                    self.idx = [names.index(w) if w in names else -1 for w in self.wanted]
                    header = names
                    max_col = max(self.idx) if self.idx else -1
                    if max_col < 0:
                        return
                    continue
                if not line:
                    continue
                if 34 not in line:
                    parts = line.split(b",", max_col + 1)
                    yield [parts[i] if 0 <= i < len(parts) else b"" for i in self.idx]
                else:
                    offs = _split_quoted(line, max_col)
                    yield [line[offs[i][0]:offs[i][1]] if i >= 0 else b"" for i in self.idx]


def s(b: bytes) -> str:
    """CsvScanner.str: UTF-8 a "" → \"."""
    r = b.decode("utf-8", "replace")
    return r.replace('""', '"') if '"' in r else r


# ───────────────────────── pomocné funkce jako v appce ─────────────────────────

def parse_time_sec(b: bytes) -> int:
    """parseTimeSec: "HH:MM:SS" (i >24 h) → sekundy, "H:MM" → *60, jinak -1. Mezery se ignorují."""
    total = cur = digits = colons = 0
    for c in b:
        if 48 <= c <= 57:
            cur = cur * 10 + (c - 48)
            digits += 1
        elif c == 58:
            if digits == 0:
                return -1
            total = total * 60 + cur
            cur = digits = 0
            colons += 1
        elif c == 32:
            pass
        else:
            return -1
    if digits == 0 or colons == 0:
        return -1
    total = total * 60 + cur
    return total * 60 if colons == 1 else total


def parse_int_bytes(b: bytes) -> int:
    """parseIntBytes: nečíselné => 0."""
    neg = False
    i = 0
    if b[:1] == b"-":
        neg, i = True, 1
    r = 0
    for c in b[i:]:
        d = c - 48
        if d < 0 or d > 9:
            return 0
        r = r * 10 + d
    return -r if neg else r


_INT_RE = re.compile(r"[+-]?[0-9]+")


def to_int_or_none(x: str) -> int | None:
    """Kotlin String.toIntOrNull (bez mezer, s volitelným znaménkem, v rozsahu Int)."""
    if not _INT_RE.fullmatch(x):
        return None
    v = int(x)
    return v if -2**31 <= v < 2**31 else None


_FLOAT_RE = re.compile(r"[\x00-\x20]*[+-]?(NaN|Infinity|((\d+\.?\d*|\.\d+)([eE][+-]?\d+)?)[fFdD]?)[\x00-\x20]*")


def to_double_or_none(x: str) -> float | None:
    """Kotlin String.toDoubleOrNull (Java syntaxe, bez hex tvarů, které GTFS nemá)."""
    if not _FLOAT_RE.fullmatch(x):
        return None
    t = x.strip("".join(chr(i) for i in range(0x21))).rstrip("fFdD")
    return float(t.replace("Infinity", "inf"))


def kt_isspace(ch: str) -> bool:
    return ch.isspace() or unicodedata.category(ch) in ("Zs", "Zl", "Zp")


def kt_trim(x: str) -> str:
    a, b = 0, len(x)
    while a < b and kt_isspace(x[a]):
        a += 1
    while b > a and kt_isspace(x[b - 1]):
        b -= 1
    return x[a:b]


def split_two_words(x: str):
    """splitTwoWords: "123456 7" → ("123456", "7"), jinak None."""
    t = kt_trim(x)
    i = 0
    while i < len(t) and not kt_isspace(t[i]):
        i += 1
    if i == 0 or i == len(t):
        return None
    j = i
    while j < len(t) and kt_isspace(t[j]):
        j += 1
    for k in range(j, len(t)):
        if kt_isspace(t[k]):
            return None
    return t[:i], t[j:]


def is_digit(ch: str) -> bool:
    return unicodedata.category(ch) == "Nd"


def ints_to_blob(a: list[int]) -> bytes:
    return struct.pack(f"<{len(a)}i", *a)


# ───────────────────────── párování nádraží (StationPairing.kt) ─────────────────────────

PAIR_MAX_M = 400.0
PAIR_FALLBACK_M = 250.0
PAIR_AMBIGUOUS_M = 40.0
CELL_DEG = 0.01
M_PER_DEG_LAT = 110540.0
M_PER_DEG_LNG = 111320.0

STATION_PAIR_OVERRIDES: dict[str, str | None] = {
    "usti nad labem hlavni nadrazi jih": None,
}

STATION_WORDS = sorted([
    "hlavni nadrazi", "vlakove nadrazi", "zeleznicni stanice", "zeleznicni zastavka",
    "hl nadrazi", "hl nadr", "hl n",
    "zel stanice", "zel zastavka", "zel st", "zel zast",
    "nadrazi", "nadr", "zst",
], key=len, reverse=True)  # sorted je stabilní jako sortedByDescending

QUALIFIERS = {
    "jih", "sever", "zapad", "vychod", "stred",
    "zastavka", "predmesti", "mesto", "dolni", "horni",
    "odstavne", "nakladove", "seradovaci", "vlecka", "depo", "dilny",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def fold_text(x: str) -> str:
    norm = unicodedata.normalize("NFD", x.lower())
    return "".join(ch for ch in norm if unicodedata.category(ch) != "Mn")


def pair_tokens(x: str) -> list[str]:
    return [t for t in _NON_ALNUM.split(fold_text(x)) if t]


def has_station_word(tokens: list[str]) -> bool:
    j = " " + " ".join(tokens) + " "
    return any(f" {w} " in j for w in STATION_WORDS)


def core_tokens(tokens: list[str]) -> list[str]:
    j = " " + " ".join(tokens) + " "
    for w in STATION_WORDS:
        j = j.replace(f" {w} ", " ")
    return [t for t in j.split(" ") if t]


def distance_m(la1, lo1, la2, lo2) -> float:
    return math.hypot((lo2 - lo1) * M_PER_DEG_LNG * math.cos(math.radians(la1)), (la2 - la1) * M_PER_DEG_LAT)


def pair_rail_stations(stops: list[tuple[str, str, str, float | None, float | None, bool]]) -> dict[str, str]:
    """stops: (id, name, place, lat, lon, rail). Vrací stop_id vlakové zastávky → place autobusového místa."""
    bus_idx: dict[str, int] = {}
    buses: list[tuple[str, list[str], bool, frozenset]] = []
    grid: dict[tuple[int, int], list[tuple[int, float, float]]] = {}
    for sid, name, place, la, lo, rail in stops:
        if rail or la is None or lo is None:
            continue
        i = bus_idx.get(place)
        if i is None:
            t = pair_tokens(place)
            c = core_tokens(t)
            buses.append((place, c, has_station_word(t), frozenset(x for x in c if x in QUALIFIERS)))
            i = len(buses) - 1
            bus_idx[place] = i
        grid.setdefault((math.floor(la / CELL_DEG), math.floor(lo / CELL_DEG)), []).append((i, la, lo))

    out: dict[str, str] = {}
    paired = ambiguous = 0
    rail_by_place: dict[str, list] = {}
    for st in stops:
        if st[5]:
            rail_by_place.setdefault(st[2], []).append(st)

    for r_place, members in rail_by_place.items():
        rt = pair_tokens(r_place)
        rail_key = " ".join(rt)
        if rail_key in STATION_PAIR_OVERRIDES:
            target = STATION_PAIR_OVERRIDES[rail_key]
            if target is not None:
                for m in members:
                    out[m[0]] = target
                paired += 1
            continue
        r_core = core_tokens(rt)
        if not r_core:
            continue
        r_qual = frozenset(x for x in r_core if x in QUALIFIERS)

        best: dict[int, float] = {}
        for m in members:
            la, lo = m[3], m[4]
            if la is None or lo is None:
                continue
            ci, cj = math.floor(la / CELL_DEG), math.floor(lo / CELL_DEG)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for (b, pla, plo) in grid.get((ci + dy, cj + dx), ()):
                        d = distance_m(la, lo, pla, plo)
                        if d > PAIR_MAX_M:
                            continue
                        old = best.get(b)
                        if old is None or d < old:
                            best[b] = d

        cands = [(buses[b], d) for b, d in best.items()
                 if (buses[b][1][0] if buses[b][1] else None) == r_core[0] and buses[b][3] == r_qual]
        named = sorted([c for c in cands if c[0][2]], key=lambda c: c[1])
        if named:
            pool = named
        elif not r_qual:
            pool = sorted([c for c in cands if c[1] <= PAIR_FALLBACK_M], key=lambda c: c[1])
        else:
            pool = []
        if not pool:
            continue
        if len(pool) > 1 and pool[1][1] - pool[0][1] < PAIR_AMBIGUOUS_M:
            ambiguous += 1
            log(f'Párování nádraží: nejednoznačné "{r_place}" ↔ "{pool[0][0][0]}" / "{pool[1][0][0]}" (přidej výjimku)')
            continue
        target = pool[0][0][0]
        for m in members:
            out[m[0]] = target
        paired += 1

    log(f"Párování nádraží: spárováno {paired}, nejednoznačných {ambiguous}, vlakových míst {len(rail_by_place)}")
    return out


# ───────────────────────── import (GtfsImporter) ─────────────────────────

def build(feeds: list[tuple[str, bool]], tag: str, out_path: str) -> int:
    t_start = time.time()
    tmp = out_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.execute("PRAGMA journal_mode = OFF")
    db.execute("PRAGMA synchronous = OFF")
    db.execute("PRAGMA page_size = 4096")
    # Android SQLiteDatabase si tuhle tabulku zakládá sám; ať je DB stejná jako z importu v telefonu
    db.execute("CREATE TABLE android_metadata (locale TEXT)")
    db.execute("INSERT INTO android_metadata VALUES ('cs_CZ')")
    db.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);")
    db.execute("CREATE TABLE stop(id INTEGER PRIMARY KEY, name TEXT, place TEXT, lat REAL, lon REAL, inreg INTEGER);")
    db.execute("CREATE TABLE trip(id INTEGER PRIMARY KEY, line TEXT, agency TEXT, cis TEXT, num TEXT, head TEXT, svc INTEGER, wheel INTEGER, t0 INTEGER, t1 INTEGER, gid TEXT DEFAULT '');")
    db.execute("CREATE TABLE trip_times(trip INTEGER PRIMARY KEY, stops BLOB, arr BLOB, dep BLOB);")
    db.execute("CREATE TABLE svc(id INTEGER PRIMARY KEY, dow TEXT, d0 TEXT, d1 TEXT);")
    db.execute("CREATE TABLE svc_date(svc INTEGER, d TEXT, type INTEGER);")

    stations: dict[str, tuple[str, float | None, float | None]] = {}
    posts: dict[str, tuple[str, str, float | None, float | None]] = {}
    region: set[str] = set()
    rail_ids: set[str] = set()
    svc_index: dict[str, int] = {}
    stop_code: dict[bytes, int] = {}     # StopInterner: surový stop_id ze stop_times → code
    stop_names: list[str] = []
    stop_db_id: list[int] = []           # code → DB id (0 = zatím nepřidělené)
    order: list[int] = []                # pořadí přidělení DB id
    count_trips = 0

    trip_rows: list[tuple] = []
    times_rows: list[tuple] = []

    def flush():
        if trip_rows:
            db.executemany("INSERT INTO trip VALUES(?,?,?,?,?,?,?,?,?,?,?)", trip_rows)
            db.executemany("INSERT INTO trip_times VALUES(?,?,?,?)", times_rows)
            trip_rows.clear()
            times_rows.clear()

    def db_id_for(code: int) -> int:
        i = stop_db_id[code]
        if i == 0:
            order.append(code)
            i = len(order)
            stop_db_id[code] = i
        return i

    for path, rail in feeds:
        zf = zipfile.ZipFile(path)
        log(f"{os.path.basename(path)}: zastávky…")
        for r in Csv(zf, "stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"]).rows():
            stop_id = s(r[0])
            if not stop_id:
                continue
            if rail:
                rail_ids.add(stop_id)
            name = s(r[1])
            lat = to_double_or_none(s(r[2]))
            lon = to_double_or_none(s(r[3]))
            # strOr(4, "0"): chybějící sloupec = "0"
            loc_type = s(r[4]) if r_has_col(zf, "stops.txt", "location_type") else "0"
            parent = s(r[5])
            if loc_type == "1":
                stations[stop_id] = (name, lat, lon)
            else:
                posts[stop_id] = (parent, name, lat, lon)
        region.update(posts.keys())

        agencies: dict[str, str] = {}
        for r in Csv(zf, "agency.txt", ["agency_id", "agency_name"]).rows():
            agencies[s(r[0])] = s(r[1])

        routes: dict[str, tuple[str, str]] = {}
        has_short = r_has_col(zf, "routes.txt", "route_short_name")
        for r in Csv(zf, "routes.txt", ["route_id", "route_short_name", "agency_id"]).rows():
            rid = s(r[0])
            if not rid:
                continue
            routes[rid] = (s(r[1]) if has_short else "?", agencies.get(s(r[2]), ""))

        log(f"{os.path.basename(path)}: spoje…")
        trips_meta: dict[str, tuple[str, str, str, str, int]] = {}
        for r in Csv(zf, "trips.txt", ["trip_id", "route_id", "service_id", "trip_headsign", "trip_short_name", "wheelchair_accessible"]).rows():
            tid = s(r[0])
            if not tid:
                continue
            w = to_int_or_none(s(r[5]))
            trips_meta[tid] = (s(r[1]), s(r[2]), s(r[3]), s(r[4]), w if w is not None else 0)

        def process_trip(trip_id: str, meta, codes, seqs, arrs, deps) -> bool:
            nonlocal count_trips
            n = len(codes)
            if n < 2:
                return False
            srt = all(seqs[i] >= seqs[i - 1] for i in range(1, n))
            idxs = range(n) if srt else sorted(range(n), key=lambda i: seqs[i])
            m = sum(1 for i in range(n) if arrs[i] >= 0 or deps[i] >= 0)
            if m < 2:
                return False
            st, ar, de = [], [], []
            for i in idxs:
                a, d = arrs[i], deps[i]
                if a < 0 and d < 0:
                    continue
                st.append(db_id_for(codes[i]))
                ar.append(a if a >= 0 else d)
                de.append(d if d >= 0 else a)
            tid = count_trips + 1
            route_id, service_id, headsign, short_name, wheel = meta
            if rail:
                cis = ""
                last = short_name.rsplit(" ", 1)[-1]
                num = "".join(ch for ch in last if is_digit(ch)).lstrip("0")
            else:
                sn = split_two_words(short_name)
                if sn is not None and all(is_digit(ch) for ch in sn[0]):
                    cis, num = sn[0].rjust(6, "0"), sn[1]
                else:
                    p = trip_id.split(":")
                    if len(p) >= 5:
                        cis, num = p[2].rjust(6, "0"), p[-1]
                    else:
                        cis = num = ""
            line, agency = routes.get(route_id, ("?", ""))
            svc = svc_index.get(service_id)
            if svc is None:
                svc = len(svc_index) + 1
                svc_index[service_id] = svc
            trip_rows.append((tid, line, agency, cis, num, headsign, svc, wheel, de[0], ar[-1], trip_id))
            times_rows.append((tid, ints_to_blob(st), ints_to_blob(ar), ints_to_blob(de)))
            if len(trip_rows) >= 20000:
                flush()
            return True

        log(f"{os.path.basename(path)}: časy zastavení…")
        cur_raw = None
        cur_id = ""
        cur_meta = None
        codes, seqs, arrs, deps = [], [], [], []
        rows = 0
        for r in Csv(zf, "stop_times.txt", ["trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"]).rows():
            t_raw, s_raw = r[0], r[1]
            if not t_raw or not s_raw:
                continue
            if t_raw != cur_raw:
                if cur_meta is not None and process_trip(cur_id, cur_meta, codes, seqs, arrs, deps):
                    count_trips += 1
                codes, seqs, arrs, deps = [], [], [], []
                cur_raw = t_raw
                cur_id = t_raw.decode("utf-8", "replace")
                cur_meta = trips_meta.get(cur_id)
            if cur_meta is None:
                continue
            c = stop_code.get(s_raw)
            if c is None:
                c = len(stop_names)
                stop_code[s_raw] = c
                stop_names.append(s_raw.decode("utf-8", "replace"))
                stop_db_id.append(0)
            codes.append(c)
            seqs.append(parse_int_bytes(r[2]))
            arrs.append(parse_time_sec(r[3]))
            deps.append(parse_time_sec(r[4]))
            rows += 1
            if rows % 2_000_000 == 0:
                log(f"  {rows // 1_000_000} M řádků, spojů {count_trips}")
        if cur_meta is not None and process_trip(cur_id, cur_meta, codes, seqs, arrs, deps):
            count_trips += 1
        flush()

        dow_cols = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        cal = Csv(zf, "calendar.txt", ["service_id", *dow_cols, "start_date", "end_date"])
        svc_rows = []
        for r in cal.rows():
            sid = svc_index.get(s(r[0]))
            if sid is None:
                continue
            dow = "".join(s(r[k]) if cal.has(k) else "0" for k in range(1, 8))
            svc_rows.append((sid, dow, s(r[8]), s(r[9])))
        db.executemany("INSERT INTO svc VALUES(?,?,?,?)", svc_rows)

        sd_rows = []
        for r in Csv(zf, "calendar_dates.txt", ["service_id", "date", "exception_type"]).rows():
            sid = svc_index.get(s(r[0]))
            if sid is None:
                continue
            t = to_int_or_none(s(r[2]))
            sd_rows.append((sid, s(r[1]), t if t is not None else 1))
        db.executemany("INSERT INTO svc_date VALUES(?,?,?)", sd_rows)
        zf.close()
        log(f"{os.path.basename(path)} hotový, spojů celkem: {count_trips}")

    def coords(g: str):
        p = posts.get(g)
        if p is None:
            return None, None
        if p[2] is not None and p[3] is not None:
            return p[2], p[3]
        par = stations.get(p[0])
        return (par[1] if par else None), (par[2] if par else None)

    pair_input = []
    for code in order:
        g = stop_names[code]
        post = posts.get(g)
        parent_name = stations[post[0]][0] if post is not None and post[0] in stations else None
        name = post[1] if post is not None else g
        la, lo = coords(g)
        pair_input.append((g, name, parent_name if parent_name else name, la, lo, g in rail_ids))
    rail_groups = pair_rail_stations(pair_input)

    stop_rows = []
    for i, code in enumerate(order):
        g = stop_names[code]
        post = posts.get(g)
        parent_name = stations[post[0]][0] if post is not None and post[0] in stations else None
        paired = rail_groups.get(g)
        name = paired if paired is not None else (post[1] if post is not None else g)
        place = paired if paired is not None else (parent_name if parent_name else name)
        la, lo = coords(g)
        stop_rows.append((i + 1, name, place, la, lo, 1 if g in region else 0))
    db.executemany("INSERT INTO stop VALUES(?,?,?,?,?,?)", stop_rows)

    built = dt.datetime.now(PRAGUE).replace(tzinfo=None).isoformat()
    db.executemany("INSERT INTO meta VALUES(?,?)", [("tag", tag), ("built", built), ("trips", str(count_trips))])
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(tmp, out_path)
    log(f"Hotovo: {count_trips} spojů, {len(order)} zastávek, {time.time() - t_start:.0f} s")
    return count_trips


_header_cache: dict[tuple[str, str], list[str]] = {}


def r_has_col(zf: zipfile.ZipFile, name: str, col: str) -> bool:
    """Má soubor ve hlavičce sloupec? (strOr v appce vrací default jen když sloupec CHYBÍ)."""
    key = (str(zf.filename), name)
    if key not in _header_cache:
        cols: list[str] = []
        if name in zf.namelist():
            with zf.open(name) as f:
                h = f.readline().decode("utf-8", "replace").rstrip("\n").rstrip("\r")
                if h.startswith("﻿"):
                    h = h[1:]
                cols = [x.strip().strip('"') for x in h.split(",")]
        _header_cache[key] = cols
    return col in _header_cache[key]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bus", required=True, help="jdf-region.zip")
    ap.add_argument("--rail", help="czptt-region.zip")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    feeds = [(a.bus, False)] + ([(a.rail, True)] if a.rail else [])
    trips = build(feeds, a.tag, a.out)
    if trips <= 0:
        sys.exit("žádné spoje")


if __name__ == "__main__":
    main()
