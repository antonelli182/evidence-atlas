#!/usr/bin/env python3
"""Pre-builds the archive atlas: a Cosmos Reason second look plus OpenStreetMap placement for
every clip in one location of the archive.

Run from a shell where the team config is sourced (credentials never leave the environment):

    set -a; source /config/<team>.config; set +a
    python3 build_atlas.py --location toronto

Resumable: results append to atlas_build.jsonl and finished clips are skipped. The final
atlas.json.gz and osm_cache.json.gz are shipped with the app by deploy.sh.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

os.environ.setdefault("VSS_URL", os.environ.get("INGRESS_URL", ""))
os.environ.setdefault("VSS_USERNAME", os.environ.get("USERNAME", ""))
os.environ.setdefault("VSS_PASSWORD", os.environ.get("PASSWORD", ""))
os.environ.setdefault("OSM_BUDGET_S", "90")

import pipeline as P  # noqa: E402

HERE = Path(__file__).resolve().parent
JSONL = HERE / "atlas_build.jsonl"
OUT = HERE / "atlas.json.gz"
P.OSM.runtime_file = HERE / "osm_cache.json.gz"


def list_segments(location: str) -> list[dict]:
    chunks, off = [], 0
    while True:
        r = P.vss_request("GET", f"/api/v1/videos/explore?scope=all&limit=48&offset={off}&location={quote(location)}")
        r.raise_for_status()
        page = r.json().get("chunks") or []
        chunks += page
        if len(page) < 48:
            break
        off += 48
    segs = []
    for ch in chunks:
        r = P.vss_request("GET", f"/api/v1/tools/segments?original_video={quote(ch['original_video'], safe='')}")
        r.raise_for_status()
        for s in r.json().get("segments") or []:
            for key in ("original_video", "location", "camera_id", "capture_type"):
                s.setdefault(key, ch.get(key))
            segs.append(s)
    return segs


def process(seg: dict) -> dict:
    hit = P.public_hit(seg)
    hit["segment_number"] = seg.get("segment_number")
    err = ""
    for attempt in range(3):
        try:
            rl = P.build_relook(hit["source"], hit["caption"], hit["object_classes"])
            break
        except Exception as exc:
            err = str(exc)[:200]
            time.sleep(4 * (attempt + 1))
    else:
        return {"hit": hit, "error": err}
    return {"hit": hit, "relook": rl}


def load_rows() -> dict[str, dict]:
    rows = {}
    for line in JSONL.read_text().splitlines():
        r = json.loads(line)
        if "relook" in r:
            rl = r["relook"]
            rl["streets"], rl["landmarks"] = P.clues_from_signs(rl["signs_read"], rl["business_signs"])
        if r["hit"]["source"] not in rows or "error" not in r:
            rows[r["hit"]["source"]] = r
    return rows


def warm_osm(rows: dict[str, dict], street_batch: int = 15, landmark_batch: int = 60) -> None:
    """One patient Overpass pass over every unique name, so placement never waits on the network."""
    streets, landmarks = {}, {}
    for r in rows.values():
        if "relook" not in r:
            continue
        for s in r["relook"]["streets"][:4]:
            streets.setdefault(P.Overpass.street_key(s["osm_name"]), s["osm_name"])
        for l in [l for l in r["relook"]["landmarks"] if P.landmark_ok(l)][:3]:
            landmarks.setdefault(P.Overpass.landmark_key(l), l)
    todo_s = [n for k, n in streets.items() if k not in P.OSM.cache]
    todo_l = [n for k, n in landmarks.items() if k not in P.OSM.cache]
    print(f"OpenStreetMap: {len(streets)} streets, {len(landmarks)} landmarks, "
          f"{len(todo_s) + len(todo_l)} not cached", flush=True)
    batches = [(todo_s[i:i + street_batch], []) for i in range(0, len(todo_s), street_batch)]
    batches += [([], todo_l[i:i + landmark_batch]) for i in range(0, len(todo_l), landmark_batch)]
    for i, (bs, bl) in enumerate(batches):
        t0 = time.time()
        for attempt in range(4):
            try:
                P.OSM._fetch(bs, bl, time.time() + 150)
                break
            except RuntimeError as exc:
                print(f"  batch {i}: {exc}, retrying", flush=True)
                time.sleep(15 * (attempt + 1))
        print(f"  OSM batch {i + 1}/{len(batches)} ({len(bs)} streets, {len(bl)} landmarks) {time.time() - t0:.0f}s", flush=True)


POINT_STATUSES = ("verified_intersection", "landmark_match", "approximate_area")
MAX_SPEED_MPS = 25  # ~90 km/h; a city dashcam drive cannot move faster than this between clips
ROUTE_WINDOW_S = 120


def drive_time(e: dict) -> tuple[str, float] | None:
    m = re.search(r"_(set\d+)_video_chunk_(\d+)", str(e.get("original_video") or ""))
    if not m:
        return None
    return m.group(1), int(m.group(2)) * 30 + float(e.get("segment_start_sec") or 0)


def anchors(place: dict) -> list[tuple[float, float]]:
    if place.get("point"):
        return [tuple(place["point"])]
    return [(g[0], g[1]) for seg in place.get("geometry") or [] for g in seg[::3]]


def route_check(entries: list[dict]) -> int:
    """Physics check without GPS: placements from the same drive must be reachable from each other.

    A point placement with route neighbours, none of them reachable, and at least one of those
    neighbours independently supported, is rejected as a false match. Returns rejections.
    """
    by_drive: dict[str, list[tuple[float, dict]]] = {}
    for e in entries:
        dt = drive_time(e)
        if dt and e["place"]["status"] != "unlocated" and anchors(e["place"]):
            by_drive.setdefault(dt[0], []).append((dt[1], e))

    def compare(t: float, e: dict, others: list[tuple[float, dict]]):
        ok, bad = [], []
        for t2, n in others:
            if n is e or abs(t2 - t) > ROUTE_WINDOW_S or n["place"].get("route_rejected"):
                continue
            if n["place"]["label"] and n["place"]["label"].lower() == (e["place"]["label"] or "").lower():
                continue  # the same sign seen again is not independent evidence
            d = min(P.haversine(a, b) for a in anchors(e["place"]) for b in anchors(n["place"]))
            allowed = MAX_SPEED_MPS * max(abs(t2 - t), 5) + (e["place"].get("radius_m") or 0) + (n["place"].get("radius_m") or 0) + 300
            (ok if d <= allowed else bad).append((n, d, t2 - t))
        return ok, bad

    rejected = 0
    for drive in by_drive.values():
        support = {id(e): compare(t, e, drive)[0] for t, e in drive}
        for t, e in drive:
            p = e["place"]
            if p["status"] not in POINT_STATUSES:
                continue
            ok, bad = compare(t, e, drive)
            near = [(n, d, dt) for n, d, dt in ok
                    if d <= 400 + (p.get("radius_m") or 0) + (n["place"].get("radius_m") or 0)]
            if near:
                p["route_support"] = [
                    f"{n['place']['label']} {abs(int(dt))} s {'later' if dt > 0 else 'earlier'} in the same drive, {int(d)} m away"
                    for n, d, dt in sorted(near, key=lambda x: abs(x[2]))[:3]
                ]
            elif bad and any(support[id(n)] for n, _, _ in bad):
                n, d, dt = min(bad, key=lambda x: abs(x[2]))
                p["route_rejected"] = {
                    "label": p["label"], "point": p["point"], "status": p["status"],
                    "reason": (f"{abs(int(dt))} s {'later' if dt > 0 else 'earlier'} this drive was at {n['place']['label']}, "
                               f"{d / 1000:.1f} km away. A car cannot cover that, so this match is rejected."),
                }
                p["checks"].append({"check": "route continuity", "result": p["route_rejected"]["reason"]})
                p.update(status="unlocated", precision="none", point=None, radius_m=None, label=None,
                         coordinate_subject="none",
                         method="The sign matched OpenStreetMap, but the match contradicts where this drive was moments before or after.")
                rejected += 1
    return rejected


def finalize(location: str) -> None:
    rows = load_rows()
    warm_osm(rows)
    entries, stats = [], {"clips": 0, "failed": 0, "by_status": {}, "streets_read": set(), "riding_yes": 0}
    for r in rows.values():
        stats["clips"] += 1
        if "error" in r:
            stats["failed"] += 1
            continue
        place = P.resolve_place(r["relook"]["streets"], r["relook"]["landmarks"])
        entries.append({**r["hit"], "category": r["relook"]["category"], "relook": r["relook"], "place": place})
        stats["streets_read"].update(x["osm_name"] for x in r["relook"]["streets"])
        stats["riding_yes"] += r["relook"]["riding"] == "yes"
    stats["route_rejected"] = route_check(entries)
    for e in entries:
        e["place"]["has_geometry"] = bool(e["place"].pop("geometry", None))
        s = e["place"]["status"]
        stats["by_status"][s] = stats["by_status"].get(s, 0) + 1
    stats["route_supported"] = sum(bool(e["place"].get("route_support")) for e in entries)
    stats["streets_read"] = len(stats["streets_read"])
    atlas = {"location": location, "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "model": P.COSMOS_MODEL, "stats": stats, "entries": entries}
    OUT.write_bytes(gzip.compress(json.dumps(atlas, separators=(",", ":")).encode()))
    P.OSM._persist()
    print(json.dumps(stats), f"atlas {OUT.stat().st_size // 1024} KB, osm cache {P.OSM.runtime_file.stat().st_size // 1024} KB", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--location", default="toronto")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--finalize-only", action="store_true")
    args = ap.parse_args()
    if not (P.VSS_URL and P.VSS_USERNAME and P.GPU_BEARER_TOKEN):
        sys.exit("team config is not sourced")
    if not args.finalize_only:
        done = set()
        if JSONL.exists():
            done = {json.loads(l)["hit"]["source"] for l in JSONL.read_text().splitlines() if '"error"' not in l}
        segs = [s for s in list_segments(args.location) if s.get("source") not in done]
        if args.limit:
            segs = segs[: args.limit]
        print(f"{len(done)} clips already done, {len(segs)} to re-watch", flush=True)
        lock, t0, n = threading.Lock(), time.time(), 0
        with ThreadPoolExecutor(args.workers) as pool, JSONL.open("a") as out:
            for fut in as_completed([pool.submit(process, s) for s in segs]):
                r = fut.result()
                with lock:
                    out.write(json.dumps(r, separators=(",", ":")) + "\n")
                    out.flush()
                    n += 1
                    rl = r.get("relook") or {}
                    rate = (time.time() - t0) / n
                    clues = [s["read"] for s in rl.get("streets", [])] + rl.get("landmarks", [])
                    print(f"[{n}/{len(segs)}] {rate:.1f}s/clip eta {int(rate * (len(segs) - n) / 60)}m "
                          f"{r['hit']['source'].rsplit('/', 1)[-1]} -> {r.get('error') or clues}", flush=True)
    finalize(args.location)


if __name__ == "__main__":
    main()
