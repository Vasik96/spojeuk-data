
#!/usr/bin/env python3

import argparse
import csv
import hashlib
import io
import json
import math
import os
import shutil
import sqlite3
import struct
import sys
import tarfile
import tempfile
import zipfile
import zlib


BBOX = (50.45, 50.85, 13.75, 14.40)
MARGIN = 0.02
EPS_M = 3.0


def log(message):
    print(message, flush=True)


def extract_gtfs(tar_path, dest):
    """Extract nested gtfs.zip from a TAR archive."""
    with tarfile.open(tar_path, "r:*") as tar:
        candidates = [
            entry for entry in tar
            if entry.isfile()
            and entry.name.replace("\\", "/").split("/")[-1] == "gtfs.zip"
        ]

        if not candidates:
            raise SystemExit(f"{tar_path}: archiv neobsahuje gtfs.zip")

        entry = candidates[0]
        source = tar.extractfile(entry)

        if source is None:
            raise SystemExit(f"{tar_path}: nelze přečíst {entry.name}")

        with source, open(dest, "wb") as output:
            shutil.copyfileobj(source, output)

    return dest


def rows(z, name):
    """Return CSV column indexes and a row iterator."""
    try:
        stream = z.open(name)
    except KeyError:
        raise SystemExit(f"GTFS neobsahuje {name}")

    text = io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")
    reader = csv.reader(text)

    try:
        header = next(reader)
    except StopIteration:
        raise SystemExit(f"{name} je prázdný")

    index = {column: i for i, column in enumerate(header)}
    return index, reader


def load_shapes(z, need, prefix=""):
    """Load only required shapes intersecting the configured BBOX."""
    if not need:
        return {}

    if "shapes.txt" not in z.namelist():
        raise SystemExit(
            f"GTFS ({prefix or 'JDF'}) neobsahuje shapes.txt"
        )

    la0, la1, lo0, lo1 = BBOX
    la0 -= MARGIN
    la1 += MARGIN
    lo0 -= MARGIN
    lo1 += MARGIN

    ix, reader = rows(z, "shapes.txt")

    required = (
        "shape_id",
        "shape_pt_lat",
        "shape_pt_lon",
        "shape_pt_sequence",
    )

    for column in required:
        if column not in ix:
            raise SystemExit(f"shapes.txt neobsahuje sloupec {column}")

    c_id = ix["shape_id"]
    c_lat = ix["shape_pt_lat"]
    c_lon = ix["shape_pt_lon"]
    c_seq = ix["shape_pt_sequence"]

    kept = {}
    finished = set()

    current = None
    buffer = []
    hit = False

    def flush():
        if current is not None and current in need and hit and len(buffer) >= 2:
            buffer.sort(key=lambda item: item[0])
            kept[prefix + current] = [
                (lat, lon) for _, lat, lon in buffer
            ]

    for row in reader:
        sid = row[c_id]

        if sid != current:
            flush()

            if current is not None:
                finished.add(current)

            if sid in finished:
                raise SystemExit(
                    f"shapes.txt není seřazený podle shape_id ({sid})"
                )

            current = sid
            buffer = []
            hit = False

        if sid not in need:
            continue

        try:
            lat = float(row[c_lat])
            lon = float(row[c_lon])
            sequence = int(row[c_seq])
        except (ValueError, IndexError):
            continue

        buffer.append((sequence, lat, lon))

        if not hit and la0 <= lat <= la1 and lo0 <= lon <= lo1:
            hit = True

    flush()
    return kept


def haversine_m(a, b):
    """Distance between two (lat, lon) points in meters."""
    lat1, lon1 = a
    lat2, lon2 = b

    radius = 6371000.0

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    h = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )

    return 2 * radius * math.asin(min(1.0, math.sqrt(h)))


def simplify(points, epsilon=EPS_M):
    """Douglas-Peucker simplification."""
    if len(points) <= 2:
        return points

    lat0 = math.radians(points[0][0])
    meters_per_lat = 111132.0
    meters_per_lon = 111320.0 * math.cos(lat0)

    xy = [
        (
            lon * meters_per_lon,
            lat * meters_per_lat,
        )
        for lat, lon in points
    ]

    def perpendicular_distance(point, start, end):
        x, y = point
        x1, y1 = start
        x2, y2 = end

        dx = x2 - x1
        dy = y2 - y1

        if dx == 0 and dy == 0:
            return math.hypot(x - x1, y - y1)

        t = (
            (x - x1) * dx + (y - y1) * dy
        ) / (dx * dx + dy * dy)

        t = max(0.0, min(1.0, t))

        px = x1 + t * dx
        py = y1 + t * dy

        return math.hypot(x - px, y - py)

    def recurse(start, end):
        maximum = 0.0
        index = None

        for i in range(start + 1, end):
            distance = perpendicular_distance(
                xy[i],
                xy[start],
                xy[end],
            )

            if distance > maximum:
                maximum = distance
                index = i

        if index is not None and maximum > epsilon:
            left = recurse(start, index)
            right = recurse(index, end)
            return left[:-1] + right

        return [start, end]

    indexes = recurse(0, len(points) - 1)
    return [points[i] for i in indexes]


def encode_varint(value):
    """Encode unsigned integer as varint."""
    output = bytearray()

    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7

    output.append(value)
    return output


def zigzag(value):
    return (value << 1) ^ (value >> 63)


def encode_points(points):
    """
    Encode coordinates as delta-coded zigzag varints.
    Coordinates are stored as integer microdegrees.
    """
    output = bytearray()

    previous_lat = 0
    previous_lon = 0

    for lat, lon in points:
        current_lat = round(lat * 1_000_000)
        current_lon = round(lon * 1_000_000)

        delta_lat = current_lat - previous_lat
        delta_lon = current_lon - previous_lon

        output.extend(encode_varint(zigzag(delta_lat)))
        output.extend(encode_varint(zigzag(delta_lon)))

        previous_lat = current_lat
        previous_lon = current_lon

    return zlib.compress(bytes(output), level=9)


def load_overrides(path):
    if not path or not os.path.exists(path):
        return {}

    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def matches_override(rule, trip_id, line, shape_id):
    """Check whether an override applies to a trip."""
    if "trip_ids" in rule and trip_id not in rule["trip_ids"]:
        return False

    if "lines" in rule and line not in rule["lines"]:
        return False

    if "shape_ids" in rule and shape_id not in rule["shape_ids"]:
        return False

    return True


def apply_overrides(trip_shape, trip_line, shapes, overrides):
    """
    Apply manual geometry overrides.

    Supported operations:
      - replace: replace a shape with a manually supplied point list
      - splice: replace a section of a shape with manually supplied points
    """
    rules = overrides.get("overrides", [])

    for rule in rules:
        operation = rule.get("type", rule.get("operation", "replace"))

        matching = []

        for trip_id, shape_id in trip_shape.items():
            line = trip_line.get(trip_id, "")

            if matches_override(rule, trip_id, line, shape_id):
                matching.append(shape_id)

        if not matching:
            continue

        points = rule.get("points")

        if not points:
            log(f"Override přeskočen – chybí points: {rule}")
            continue

        normalized = [
            (float(point[0]), float(point[1]))
            for point in points
        ]

        for shape_id in set(matching):
            if operation == "replace":
                shapes[shape_id] = normalized

            elif operation == "splice":
                original = shapes.get(shape_id)

                if not original:
                    continue

                start = int(rule.get("start", 0))
                end = int(rule.get("end", len(original)))

                start = max(0, min(start, len(original)))
                end = max(start, min(end, len(original)))

                shapes[shape_id] = (
                    original[:start]
                    + normalized
                    + original[end:]
                )

            else:
                log(f"Neznámý typ override: {operation}")


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--full", required=True)
    parser.add_argument("--filtered", required=True)
    parser.add_argument("--trains", required=True)

    parser.add_argument("--out", required=True)
    parser.add_argument("--meta", required=True)

    parser.add_argument("--upstream", default="")
    parser.add_argument("--prev", default="")
    parser.add_argument("--min-ratio", type=float, default=0.9)
    parser.add_argument("--min-trips", type=int, default=1000)
    parser.add_argument("--overrides", default="")

    args = parser.parse_args()

    work = tempfile.mkdtemp(prefix="build-shapes-")

    try:
        log("Rozbaluji GTFS ze všech archivů…")

        full_zip = extract_gtfs(
            args.full,
            os.path.join(work, "full-gtfs.zip"),
        )

        filtered_zip = extract_gtfs(
            args.filtered,
            os.path.join(work, "filtered-gtfs.zip"),
        )

        trains_zip = extract_gtfs(
            args.trains,
            os.path.join(work, "trains-gtfs.zip"),
        )

        zf = zipfile.ZipFile(full_zip)
        zl = zipfile.ZipFile(filtered_zip)
        zt = zipfile.ZipFile(trains_zip)

        # 1) Načtení spojů a linek z filtrovaného JDF feedu.
        log("Načítám filtrované JDF spoje…")

        ix, reader = rows(zl, "trips.txt")

        c_trip = ix["trip_id"]
        c_route = ix["route_id"]

        wanted = set()
        trip_line = {}

        route_ix, route_reader = rows(zl, "routes.txt")

        route_id_col = route_ix["route_id"]
        route_name_col = route_ix.get("route_short_name")

        routes = {}

        for row in route_reader:
            name = row[route_name_col] if route_name_col is not None else ""
            routes[row[route_id_col]] = name

        for row in reader:
            trip_id = row[c_trip]
            route_id = row[c_route]

            wanted.add(trip_id)
            trip_line[trip_id] = routes.get(route_id, "")

        log(f"JDF: {len(wanted)} spojů ve filtrovaném feedu")

        # 2) Autobusy: jejich shape_id z plného feedu.
        log("Páruji JDF trip_id na shape_id…")

        ix, reader = rows(zf, "trips.txt")

        c_trip = ix["trip_id"]
        c_shape = ix["shape_id"]

        trip_shape = {}

        for row in reader:
            trip_id = row[c_trip]
            shape_id = row[c_shape]

            if trip_id in wanted and shape_id:
                trip_shape[trip_id] = shape_id

        ratio_found = len(trip_shape) / max(1, len(wanted))

        log(
            f"JDF: spoje s tvarem {len(trip_shape)} "
            f"z {len(wanted)} ({ratio_found:.0%})"
        )

        if ratio_found < 0.5:
            raise SystemExit(
                "Většina JDF trip_id se v plném feedu nenašla."
            )

        # 3) Vlaky: CZPTT GTFS.
        log("Načítám vlakové spoje z CZPTT…")

        ix, reader = rows(zt, "trips.txt")

        c_trip = ix["trip_id"]
        c_shape = ix.get("shape_id")

        if c_shape is None:
            raise SystemExit(
                "CZPTT trips.txt neobsahuje shape_id"
            )

        train_shape = {}

        for row in reader:
            trip_id = row[c_trip]
            shape_id = row[c_shape]

            if not trip_id or not shape_id:
                continue

            if trip_id in trip_shape:
                raise SystemExit(
                    f"Kolize trip_id mezi JDF a CZPTT: {trip_id}"
                )

            train_shape[trip_id] = "czptt:" + shape_id

        log(
            f"CZPTT: vlakových spojů s tvarem {len(train_shape)}"
        )

        # Linky vlaků pro overrides.
        ix, reader = rows(zt, "routes.txt")

        route_id_col = ix["route_id"]
        route_name_col = ix.get("route_short_name")

        train_routes = {}

        for row in reader:
            name = (
                row[route_name_col]
                if route_name_col is not None
                else ""
            )

            train_routes[row[route_id_col]] = name

        ix, reader = rows(zt, "trips.txt")

        c_trip = ix["trip_id"]
        c_route = ix["route_id"]

        for row in reader:
            trip_id = row[c_trip]

            if trip_id in train_shape:
                trip_line[trip_id] = train_routes.get(
                    row[c_route],
                    "",
                )

        trip_shape.update(train_shape)

        log(
            f"Celkem spojů s přiřazeným tvarem: "
            f"{len(trip_shape)}"
        )

        # 4) Načtení geometrií z obou feedů.
        jdf_need = {
            shape_id
            for shape_id in trip_shape.values()
            if not shape_id.startswith("czptt:")
        }

        train_need = {
            shape_id.removeprefix("czptt:")
            for shape_id in trip_shape.values()
            if shape_id.startswith("czptt:")
        }

        log("Načítám geometrie JDF…")
        kept = load_shapes(zf, jdf_need)

        log("Načítám geometrie CZPTT…")
        train_kept = load_shapes(
            zt,
            train_need,
            prefix="czptt:",
        )

        kept.update(train_kept)

        log(f"JDF tvarů v oblasti: {len(kept) - len(train_kept)}")
        log(f"CZPTT tvarů v oblasti: {len(train_kept)}")
        log(f"Celkem tvarů v oblasti: {len(kept)}")


        # 5) Vybereme pouze spoje, jejichž geometrie zasahuje do BBOX.
        mapped_trips = {
            trip_id: shape_id
            for trip_id, shape_id in trip_shape.items()
            if shape_id in kept
        }

        if len(mapped_trips) < args.min_trips:
            raise SystemExit(
                f"Příliš málo spojů s geometrií v oblasti: "
                f"{len(mapped_trips)} < {args.min_trips}"
            )

        jdf_mapped = sum(
            1 for shape_id in mapped_trips.values()
            if not shape_id.startswith("czptt:")
        )

        train_mapped = len(mapped_trips) - jdf_mapped

        log(f"Spoje s geometrií v oblasti: {len(mapped_trips)}")
        log(f"  Autobusy: {jdf_mapped}")
        log(f"  Vlaky: {train_mapped}")


        # 6) Ruční overrides.
        overrides = load_overrides(args.overrides)

        if overrides:
            log("Aplikuji ruční overrides…")
            apply_overrides(
                mapped_trips,
                trip_line,
                kept,
                overrides,
            )

        # 7) Zjednodušení geometrií.
        log("Zjednodušuji geometrie…")

        simplified = {}

        for shape_id, points in kept.items():
            if len(points) < 2:
                continue

            result = simplify(points)

            if len(result) >= 2:
                simplified[shape_id] = result

        mapped_trips = {
            trip_id: shape_id
            for trip_id, shape_id in mapped_trips.items()
            if shape_id in simplified
        }

        # 8) SQLite databáze.
        log("Vytvářím SQLite databázi…")

        if os.path.exists(args.out):
            os.remove(args.out)

        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

        connection = sqlite3.connect(args.out)

        try:
            connection.execute(
                """
                CREATE TABLE meta (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE shape (
                    id INTEGER PRIMARY KEY,
                    pts BLOB NOT NULL
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE trip_shape (
                    gid TEXT PRIMARY KEY,
                    shape INTEGER NOT NULL
                ) WITHOUT ROWID
                """
            )

            shape_ids = sorted(simplified.keys())

            shape_to_int = {
                shape_id: index + 1
                for index, shape_id in enumerate(shape_ids)
            }

            for shape_id in shape_ids:
                points = simplified[shape_id]
                encoded = encode_points(points)

                connection.execute(
                    "INSERT INTO shape(id, pts) VALUES (?, ?)",
                    (
                        shape_to_int[shape_id],
                        encoded,
                    ),
                )

            for trip_id, shape_id in mapped_trips.items():
                connection.execute(
                    "INSERT OR REPLACE INTO trip_shape(gid, shape) VALUES (?, ?)",
                    (
                        trip_id,
                        shape_to_int[shape_id],
                    ),
                )

            metadata = {
                "upstream": args.upstream,
                "trip_count": len(mapped_trips),
                "shape_count": len(shape_ids),
                "jdf_shape_count": len(jdf_need),
                "train_shape_count": len(train_need),
                "bbox": list(BBOX),
                "margin": MARGIN,
                "epsilon_m": EPS_M,
            }

            for key, value in metadata.items():
                connection.execute(
                    "INSERT INTO meta(k, v) VALUES (?, ?)",
                    (
                        key,
                        json.dumps(value, ensure_ascii=False),
                    ),
                )

            connection.commit()

        finally:
            connection.close()

        # 9) Metadata JSON.
        metadata = {
            "upstream": args.upstream,
            "trip_count": len(mapped_trips),
            "shape_count": len(shape_ids),
            "jdf_shape_count": len(jdf_need),
            "train_shape_count": len(train_need),
            "bbox": list(BBOX),
            "margin": MARGIN,
            "epsilon_m": EPS_M,
        }

        os.makedirs(os.path.dirname(os.path.abspath(args.meta)), exist_ok=True)

        with open(args.meta, "w", encoding="utf-8") as file:
            json.dump(
                metadata,
                file,
                ensure_ascii=False,
                indent=2,
            )

        log("Hotovo.")
        log(f"Výstup: {args.out}")
        log(f"Metadata: {args.meta}")

    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
