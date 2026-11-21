#!/usr/bin/env python3
"""build_stations.py: stanoviště (sloupky) zastávek IDS DÚK pro mapu v appce a na webu.

Jízdní řád (JDF → GTFS) stanoviště u spojů skoro nemá ("unspecified", nejvýš odhad dvou), proto appka i web
berou stanoviště a jejich odjezdy z portaba (odjezdové tabule DÚK): registr uzlů a sloupků se souřadnicemi
a u každého odjezdu číslo stanoviště, ze kterého jede. Tenhle skript připraví registr pro mapu:

- jen autobusová stanoviště (sloupek 1–89); vlaková nástupiště (90+) appka bere z vlakového jízdního řádu,
  souhrnný bod uzlu (999) není stanoviště,
- bez souřadnic a duplikáty (stejné místo) pryč,
- jen stanoviště, ze kterých během týdne něco jede: dotaz na nejbližší odjezd; když žádný, ještě po dnech na
  celý týden dopředu (víkendové, noční). Nepoužívaná stanoviště (např. Divadlo 1 a 8) by na mapě jen mátla.

Výstup: JSON {"source", "built", "nodes": [{"node": 1734, "n": "Ústí n.L.,Divadlo", "p": [[lat, lon, sloupek], …]}]}

Použití:  python3 tools/build_stations.py --out stations.json [--workers 2] [--bbox jih,západ,sever,východ]
"""
import argparse, collections, concurrent.futures as cf, datetime, json, math, sys, time, urllib.error, urllib.request

BASE = "https://tabule.portabo.cz/api/v1-tabule"
SUMMARY_POST = 999      # souhrnný bod uzlu, ne sloupek
RAIL_POST_MIN = 90      # 90+ = vlakové nástupiště (hl. nádr. 92, 101)
DUP_M = 2.0             # sloupky blíž než tohle jsou jedno místo
UA = {"User-Agent": "SpojeUK-data/1.0 (github.com/Vasik96/spojeuk-data)", "Accept": "application/json"}


PAUSE_S = 0.3           # pauza po každém dotazu (veřejné API odjezdových tabulí, nepřetěžovat)


def get(path, timeout=60, tries=5):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(f"{BASE}/{path}", headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            time.sleep(PAUSE_S)
            return data
        except urllib.error.HTTPError as ex:
            last = ex
            # přetížené (503 / 429): počkat déle, případně kolik řekne server
            wait = int(ex.headers.get("Retry-After") or 0) if ex.code in (429, 503) else 0
            time.sleep(max(wait, 5 * 2 ** attempt if ex.code in (429, 503) else 3 * (attempt + 1)))
        except Exception as ex:  # noqa: BLE001
            last = ex
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{path}: {last}")


def dist_m(a, b):
    k = math.cos(math.radians(a[0])) * 111_320.0
    return math.hypot((a[1] - b[1]) * k, (a[0] - b[0]) * 110_540.0)


def used_this_week(node, post, start):
    """Jede ze stanoviště během týdne aspoň jeden spoj? Nejdřív nejbližší odjezd, pak po dnech."""
    d = get(f"duk/GetStationDeparturesWCount/{node}/{post}/1/0")
    if d.get("DeparturesList"):
        return True
    for day in range(1, 8):
        t = start + datetime.timedelta(days=day)
        d = get(f"cis/GetStationDeparturesWTimeAndCount/{node}/{post}/1/0/{t.year}/{t.month}/{t.day}/0/0")
        if d.get("DeparturesList"):
            return True
    return False


RETRY_WAIT_S = 30       # stanoviště, které selhalo i po opakováních v get(): počkat a zkusit znovu
RETRY_ROUNDS = 10       # nejvýš tolik kol (portabo bývá přetížené jen chvíli)


def used_with_retry(node, post, start):
    """used_this_week, a když portabo nedá odpověď ani po opakováních, po RETRY_WAIT_S znovu (workflow pokračuje)."""
    for rnd in range(RETRY_ROUNDS):
        try:
            return used_this_week(node, post, start)
        except Exception as e:  # noqa: BLE001
            if rnd == RETRY_ROUNDS - 1:
                raise
            print(f"  ({node}, {post}): {e}; za {RETRY_WAIT_S} s znovu ({rnd + 2}/{RETRY_ROUNDS})", file=sys.stderr, flush=True)
            time.sleep(RETRY_WAIT_S)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--bbox", help="jen oblast jih,západ,sever,východ (zkoušení; jinak celý registr DÚK)")
    a = ap.parse_args()
    box = tuple(map(float, a.bbox.split(","))) if a.bbox else None

    items = get("duk/GetStations", timeout=120, tries=6).get("ItemList") or []
    if len(items) < 1000:
        raise SystemExit(f"podezřele málo záznamů z portaba ({len(items)}), nepublikuje se")

    names = collections.defaultdict(collections.Counter)
    posts = {}   # (uzel, sloupek) -> (lat, lon)
    for x in items:
        node, post, lat, lon = x.get("Node"), x.get("Post"), x.get("Latitude"), x.get("Longitude")
        if node is None or post is None or not lat or not lon or post == SUMMARY_POST or post >= RAIL_POST_MIN:
            continue
        if box and not (box[0] <= lat <= box[2] and box[1] <= lon <= box[3]):
            continue
        names[node][(x.get("Name") or "").strip()] += 1
        posts.setdefault((node, post), (round(lat, 7), round(lon, 7)))

    start = datetime.datetime.now()
    keys = sorted(posts)
    used, failed = {}, 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(used_with_retry, n, p, start): (n, p) for n, p in keys}
        for i, f in enumerate(cf.as_completed(futs), 1):
            k = futs[f]
            try:
                used[k] = f.result()
            except Exception as e:  # noqa: BLE001
                failed += 1
                used[k] = True   # nevíme: radši ukázat (chybějící stanoviště je horší než prázdné)
                print(f"  {k}: {e} (vzdáno, bere se jako používané)", file=sys.stderr, flush=True)
            if i % 500 == 0:
                print(f"  {i}/{len(keys)} stanovišť ({time.time() - t0:.0f} s)", file=sys.stderr, flush=True)
    if failed > len(keys) * 0.2:
        raise SystemExit(f"příliš mnoho chyb ({failed}/{len(keys)}), nepublikuje se")

    nodes = {}
    for (node, post) in keys:
        if not used[(node, post)]:
            continue
        pt = posts[(node, post)]
        nd = nodes.setdefault(node, {"node": node, "n": names[node].most_common(1)[0][0], "p": []})
        if any(dist_m(pt, q) < DUP_M for q in nd["p"]):
            continue
        nd["p"].append([pt[0], pt[1], post])
    out = [nodes[k] for k in sorted(nodes)]
    if len(out) < (20 if box else 500):
        raise SystemExit(f"podezřele málo uzlů ({len(out)}), nepublikuje se")
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"source": "IDS DÚK (portabo)", "built": start.strftime("%Y-%m-%dT%H:%M:%S"), "nodes": out},
                  f, ensure_ascii=False, separators=(",", ":"))
    total = sum(len(v["p"]) for v in out)
    print(f"{len(out)} uzlů, {total} stanovišť (nepoužívaných vyřazeno {sum(1 for v in used.values() if not v)},"
          f" chyb {failed}) za {time.time() - t0:.0f} s -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
