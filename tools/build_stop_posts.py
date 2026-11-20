#!/usr/bin/env python3
"""build_stop_posts.py: sloupky zastávek (označníky IDS DÚK) v Ústí nad Labem a okolí pro mapu v appce.

Jízdní řád (CIS JŘ) má jen jeden bod na zastávku, JDF souřadnice ani sloupky vůbec nemá. Registr uzlů a sloupků
IDS DÚK (uzel / sloupek / zóna, např. Mírové nám. 1746/1–8) vystavuje portabo (odjezdové tabule DÚK). Portabo občas
nefunguje, proto ho nestahuje appka, ale tenhle skript ve workflow; při výpadku zůstane v release poslední verze.

Výstup: JSON {"source": ..., "nodes": [{"node": 1746, "n": "Ústí n.L.,Mírové nám.",
              "p": [[lat, lon, sloupek], ...],   autobusové sloupky (1–89)
              "r": [lat, lon]}]}                   vlakové nástupiště (sloupek 90+), jen když je

Použití:  python3 tools/build_stop_posts.py --out stop_posts.json
"""
import argparse, json, sys, time, urllib.request

URL = "https://tabule.portabo.cz/api/v1-tabule/cis/GetStations"
USTI_BBOX = (50.60, 13.95, 50.72, 14.12)   # jih, západ, sever, východ
SUMMARY_POST = 999      # souhrnný bod uzlu, ne sloupek
RAIL_POST_MIN = 90      # 90+ = vlakové nástupiště (např. hl. nádr. 92, 101)
DUP_M = 2.0             # sloupky blíž než tohle jsou duplikát


def fetch():
    last = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(URL, headers={"User-Agent": "SpojeUK-data/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read())
            items = d.get("ItemList") if isinstance(d, dict) else None
            if items:
                return items
            last = "prázdná odpověď"
        except Exception as ex:  # noqa: BLE001
            last = ex
        time.sleep(20 * (attempt + 1))
    raise SystemExit(f"portabo nedostupné: {last}")


def dist_m(a, b):
    import math
    k = math.cos(math.radians(a[0])) * 111_320.0
    return math.hypot((a[1] - b[1]) * k, (a[0] - b[0]) * 110_540.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--bbox", default=",".join(map(str, USTI_BBOX)), help="jih,západ,sever,východ")
    a = ap.parse_args()
    s, w, n, e = map(float, a.bbox.split(","))

    nodes = {}
    for x in fetch():
        lat, lon = x.get("Latitude"), x.get("Longitude")
        post, node = x.get("Post"), x.get("Node")
        if not lat or not lon or node is None or post is None or post == SUMMARY_POST:
            continue
        if not (s <= lat <= n and w <= lon <= e):
            continue
        nd = nodes.setdefault(node, {"node": node, "n": "", "p": [], "r": None})
        pt = (round(lat, 7), round(lon, 7))
        if post >= RAIL_POST_MIN:
            nd["r"] = nd["r"] or list(pt)
            continue
        if not nd["n"] or x.get("Name", "").startswith(nd["n"][:3]):
            nd["n"] = nd["n"] or (x.get("Name") or "").strip()
        if any(dist_m(pt, q) < DUP_M for q in nd["p"]):
            continue
        nd["p"].append([pt[0], pt[1], post])

    out = []
    for nd in sorted(nodes.values(), key=lambda v: v["node"]):
        if not nd["p"]:
            continue
        nd["p"].sort(key=lambda p: p[2])
        if nd["r"] is None:
            del nd["r"]
        out.append(nd)
    if len(out) < 50:
        raise SystemExit(f"podezřele málo uzlů ({len(out)}), nepublikuje se")
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"source": "IDS DÚK (portabo)", "bbox": [s, w, n, e], "nodes": out},
                  f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(out)} uzlů, {sum(len(v['p']) for v in out)} sloupků -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
