#!/usr/bin/env python3
"""pid_lines.py: které CIS linky patří do PID.

jdf-filtered.tar z obehy vyhazuje linky integrovaných systémů, které "už publikují jiné feedy"
(PID, ODIS, IDS JMK), a právě proto v appce chybí pražské tramvaje, metro a autobusy PID, i
regionální linky PID (např. 100369, 100400). Plný jdf.tar je ale obsahuje (spolu s tvary tras).

Seznam linek PID je v jdf-filtered/line-snapshot.json: dotaz {"kind": "ids", "value": "30001", "lines": [...]}.
30001 je kód integrovaného systému PID (ostatní kódy v souboru jsou jiné IDS).
"""
import json
import tarfile

PID_IDS_CODE = "30001"
SNAPSHOT = "line-snapshot.json"


def route_line(route_id: str) -> str:
    """'jdf:route:100369:detour' -> '100369'."""
    parts = route_id.split(":")
    return parts[2] if len(parts) >= 3 and parts[0] == "jdf" and parts[1] == "route" else ""


def pid_lines_from_tar(filtered_tar: str, ids_code: str = PID_IDS_CODE) -> set:
    """Šestimístné CIS linky PID z line-snapshot.json uvnitř jdf-filtered.tar."""
    with tarfile.open(filtered_tar, "r:") as tf:
        member = next((m for m in tf.getmembers() if m.isfile() and m.name.endswith(SNAPSHOT)), None)
        if member is None:
            raise SystemExit(f"{filtered_tar}: {SNAPSHOT} nenalezen, změnil se formát obehy?")
        data = json.load(tf.extractfile(member))
    lines = set()
    for q in data.get("queries", []):
        if q.get("kind") == "ids" and str(q.get("value")) == str(ids_code):
            lines.update(str(x).zfill(6) for x in q.get("lines", []))
    if not lines:
        raise SystemExit(f"{SNAPSHOT}: dotaz ids/{ids_code} (PID) chybí nebo je prázdný, změnil se formát obehy?")
    return lines
