#!/usr/bin/env python3
"""Evidence Atlas web app — turns GPS-less dashcam search hits into audited places."""

from __future__ import annotations

import gzip
import json
import math
import re
import threading
import time
from typing import Any
from urllib.parse import quote, unquote

import requests
from flask import Flask, Response, jsonify, request, stream_with_context

from pipeline import (
    BEFORE_FILE, DATA_DIR, GPU_BEARER_TOKEN, HERE, OSM, PORT, UA, VSS_URL, VSS_USERNAME, WANDB_API_KEY, WANDB_BASE,
    WANDB_MODELS, WANDB_PROJECT, WANDB_TEAM, _safe_text, allowed_source, build_relook, public_hit,
    resolve_place, vss_login, vss_request,
)

app = Flask(__name__)
_relook_cache: dict[str, dict[str, Any]] = {}
_live_slots = threading.BoundedSemaphore(2)


def load_atlas() -> dict[str, Any]:
    try:
        atlas = json.loads(gzip.decompress((DATA_DIR / "atlas.json.gz").read_bytes()))
    except Exception:
        return {"entries": [], "stats": {}}
    for e in atlas.get("entries", []):
        _relook_cache[e["source"]] = {**e["relook"], "from_atlas": True}
    return atlas


ATLAS = load_atlas()


def archive_signals(entries: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Per-signal clip counts across the whole archive, and among clips with at least a known street."""
    tests = {
        "ad_surfaces": lambda r: bool(r.get("ad_surfaces") or r.get("business_signs")),
        "roadwork": lambda r: str(r.get("roadwork")).lower() == "yes",
        "riding": lambda r: r.get("riding") == "yes",
        "parked_bicycles": lambda r: r.get("parked") == "yes",
    }
    out = {}
    for name, test in tests.items():
        hits = [e for e in entries if test(e.get("relook") or {})]
        out[name] = {"all": len(hits), "with_street": sum(e["place"]["status"] != "unlocated" for e in hits)}
    return out


SIGNALS = archive_signals(ATLAS.get("entries", []))

STOPWORDS = set("a an and are at by for from in into is near next of on or the to with without".split())
HIT_FIELDS = ("id", "source", "original_video", "caption", "caption_claims", "location", "camera_id", "capture_type",
              "segment_start_sec", "segment_end_sec", "segment_number", "object_classes", "category", "location_status", "clip_url")


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z]+", text.lower()) if w not in STOPWORDS and len(w) > 2}


def _atlas_doc(e: dict[str, Any]) -> set[str]:
    rl = e.get("relook") or {}
    parts = [e.get("caption") or "", e.get("category") or "", " ".join(map(str, rl.get("business_signs") or [])),
             " ".join(str(s.get("text", "")) for s in rl.get("signs_read") or []), " ".join(map(str, rl.get("ad_surfaces") or []))]
    if rl.get("riding") == "yes":
        parts.append("cyclist cyclists riding bike bicycle")
    if rl.get("parked") == "yes":
        parts.append("parked bicycles bike rack")
    if str(rl.get("roadwork")).lower() == "yes":
        parts.append("roadwork road work construction cones lane closure")
    if rl.get("ad_surfaces") or rl.get("business_signs"):
        parts.append("storefront storefronts signs billboard billboards advertising shop")
    return _stems(" ".join(parts))


ATLAS_DOCS = [(e, _atlas_doc(e)) for e in ATLAS.get("entries", [])]


def atlas_search(query: str, top_k: int) -> list[dict[str, Any]]:
    """Keyword fallback over the re-watched archive, used only when VSS search is down."""
    terms = _stems(query)
    if not terms or not ATLAS_DOCS:
        return []
    df = {t: sum(t in d for _, d in ATLAS_DOCS) for t in terms}
    n = len(ATLAS_DOCS)
    idf = {t: math.log((n + 1) / (df[t] + 1)) + 1 for t in terms}
    best = sum(idf.values())
    scored = []
    for e, doc in ATLAS_DOCS:
        s = sum(idf[t] for t in terms if t in doc)
        if s:
            placed = e["place"]["status"] != "unlocated"
            scored.append((s + (0.25 if placed else 0), e))
    scored.sort(key=lambda x: -x[0])
    return [{**{k: e.get(k) for k in HIT_FIELDS}, "similarity": round(min(s / best, 1.0), 3)} for s, e in scored[:top_k]]


# ------------------------------------------------------------------- routes


@app.get("/health")
def health() -> Any:
    return jsonify({
        "ok": True,
        "app": "evidence-atlas",
        "vss_configured": bool(VSS_URL and VSS_USERNAME),
        "wandb_configured": bool(WANDB_API_KEY),
        "cosmos_configured": bool(GPU_BEARER_TOKEN),
        "osm_cached_names": len(OSM.cache),
        "atlas_clips": len(ATLAS.get("entries", [])),
    })


@app.get("/api/atlas")
def atlas() -> Any:
    """Placed and route-rejected clips in full. Other unlocated clips stay off the map, so only their count is sent."""
    entries = ATLAS.get("entries", [])
    placed = [e for e in entries if e["place"]["status"] != "unlocated" or e["place"].get("route_rejected")]
    return jsonify({
        **{k: ATLAS.get(k) for k in ("location", "built_at", "model", "stats")},
        "signals": SIGNALS,
        "entries": placed,
        "unlocated": len(entries) - len(placed),
        "unlocated_sources": [e["source"] for e in entries if e["place"]["status"] == "unlocated" and not e["place"].get("route_rejected")],
    })


@app.after_request
def compress(resp: Response) -> Response:
    if (resp.mimetype == "application/json" and not resp.direct_passthrough
            and "gzip" in request.headers.get("Accept-Encoding", "") and resp.content_length
            and resp.content_length > 20_000):
        resp.set_data(gzip.compress(resp.get_data()))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Vary"] = "Accept-Encoding"
    return resp


@app.get("/")
def index() -> Any:
    return Response((HERE / "index.html").read_text(), mimetype="text/html")


@app.get("/api/suggestions")
def suggestions() -> Any:
    try:
        r = vss_request("GET", "/api/v1/suggestions")
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        return jsonify({"prompts": [], "error": str(exc)}), 200
    raw = data.get("prompts") or data.get("suggestions") or data.get("items") or []
    if isinstance(raw, dict):
        raw = raw.get("prompts") or []
    prompts = []
    for item in raw[:12]:
        text = item if isinstance(item, str) else (item.get("prompt") or item.get("text") or item.get("query"))
        if text:
            prompts.append(text)
    return jsonify({"prompts": prompts})


@app.post("/api/search")
def search() -> Any:
    body = request.get_json(silent=True) or {}
    query = _safe_text(body.get("query"), 500).strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    location = _safe_text(body.get("location") or "toronto", 80).strip() or "toronto"
    top_k = max(1, min(int(body.get("top_k") or 15), 40))
    t0 = time.time()
    try:
        r = vss_request("POST", "/api/v1/search", json={
            "query": query, "top_k": top_k, "llm_top_n": 1, "min_similarity": 0.15, "time_filter": "all",
            "include_public": True, "metadata_filters": {"location": location},
        })
        failure = f"HTTP {r.status_code}" if r.status_code >= 500 else None
    except (requests.RequestException, RuntimeError) as exc:
        r, failure = None, type(exc).__name__
    if failure:
        results = atlas_search(query, top_k)
        return jsonify({
            "query": query, "location_filter": location, "fallback": True,
            "scope_label": f"{location} archive · re-watched clips · keyword match",
            "provider": "Evidence Atlas (VSS search unavailable)",
            "fallback_reason": f"VAST VSS search is unavailable right now ({failure}). These are keyword matches "
                               "against what Cosmos Reason read and saw when it re-watched the archive.",
            "results": results, "count": len(results), "latency_ms": int((time.time() - t0) * 1000),
        })
    if r.status_code >= 400:
        return jsonify({"error": "search_failed", "detail": r.text[:400]}), r.status_code
    data = r.json()
    results = [public_hit(h) for h in (data.get("results") or [])]
    syn = data.get("llm_synthesis") or {}
    return jsonify({
        "query": query,
        "location_filter": location,
        "scope_label": f"{location} archive · top {top_k} · not exhaustive",
        "provider": "VAST VSS",
        "synthesis_model": syn.get("model"),
        "synthesis": _safe_text(syn.get("response"), 1500),
        "results": results,
        "count": len(results),
        "latency_ms": int((time.time() - t0) * 1000),
    })


@app.post("/api/relook")
def relook() -> Any:
    body = request.get_json(silent=True) or {}
    source = _safe_text(body.get("source"), 400)
    if not allowed_source(source):
        return jsonify({"error": "invalid source"}), 400
    if source in _relook_cache and not body.get("fresh"):
        return jsonify({**_relook_cache[source], "cached": True})
    if not _live_slots.acquire(blocking=False):
        return jsonify({"error": "busy", "detail": "Two live re-watches are already running. Try again in a few seconds."}), 429
    try:
        classes = body.get("object_classes") or []
        if isinstance(classes, str):
            classes = [c.strip() for c in classes.split(",")]
        result = build_relook(source, _safe_text(body.get("caption"), 4000), classes)
    except Exception as exc:
        return jsonify({"error": "relook_failed", "detail": str(exc)[:300]}), 502
    finally:
        _live_slots.release()
    _relook_cache[source] = result
    return jsonify(result)


@app.post("/api/place")
def place() -> Any:
    body = request.get_json(silent=True) or {}
    streets = []
    for s in (body.get("streets") or [])[:6]:
        if isinstance(s, dict) and s.get("osm_name"):
            kind = s.get("kind") if s.get("kind") in ("corner_name", "overhead_guide", "other") else "other"
            streets.append({"osm_name": _safe_text(s["osm_name"], 80), "read": _safe_text(s.get("read"), 80), "kind": kind})
    landmarks = [_safe_text(x, 80) for x in (body.get("landmarks") or [])[:4] if x]
    t0 = time.time()
    result = resolve_place(streets, landmarks)
    result["latency_ms"] = int((time.time() - t0) * 1000)
    return jsonify(result)


@app.get("/api/clip")
def clip() -> Any:
    source = unquote(request.args.get("source") or "")
    if not allowed_source(source):
        return jsonify({"error": "invalid source"}), 400
    headers = {"User-Agent": UA}
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]

    def open_stream(force: bool = False) -> requests.Response:
        url = f"{VSS_URL}/api/v1/videos/stream?source={quote(source, safe='')}&token={vss_login(force)}"
        return requests.get(url, headers=headers, stream=True, timeout=120)

    upstream = open_stream()
    if upstream.status_code == 401:
        upstream = open_stream(True)

    def generate():
        try:
            for chunk in upstream.iter_content(64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    out_headers = {k: upstream.headers[k] for k in ("Content-Length", "Content-Range", "Accept-Ranges") if upstream.headers.get(k)}
    out_headers["Content-Type"] = "video/mp4"
    out_headers["Cache-Control"] = "no-store"
    return Response(stream_with_context(generate()), status=upstream.status_code, headers=out_headers)


@app.get("/api/reingest-compare")
def reingest_compare() -> Any:
    try:
        before = json.loads(BEFORE_FILE.read_text())
    except Exception:
        return jsonify({"error": "no before snapshot"}), 404
    ov = before["original_video"]
    r = vss_request("GET", f"/api/v1/tools/segments?original_video={quote(ov, safe='')}")
    if r.status_code >= 400:
        return jsonify({"error": "segments_failed", "detail": r.text[:300]}), r.status_code
    after = {s.get("segment_number"): s for s in (r.json().get("segments") or [])}
    rows = []
    for b in before["segments"]:
        a = after.get(b.get("segment_number")) or {}
        rows.append({
            "segment_number": b.get("segment_number"),
            "before": b.get("reasoning_content"),
            "after": a.get("reasoning_content"),
            "after_source": a.get("source"),
            "reingested": "/reingest/" in str(a.get("source") or ""),
        })
    return jsonify({"original_video": ov, "rows": rows, "reingested": all(x["reingested"] for x in rows)})


def wandb_chat(messages: list[dict[str, str]], max_tokens: int = 700) -> tuple[str, str]:
    if not WANDB_API_KEY:
        raise RuntimeError("W&B inference is not configured")
    headers = {
        "Authorization": f"Bearer {WANDB_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 EvidenceAtlas",
    }
    if WANDB_TEAM and WANDB_PROJECT:
        headers["OpenAI-Project"] = f"{WANDB_TEAM}/{WANDB_PROJECT}"
    last = "no model"
    for model in WANDB_MODELS:
        r = requests.post(f"{WANDB_BASE}/chat/completions", headers=headers, json={
            "model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2,
        }, timeout=90)
        if r.status_code < 400:
            return (r.json()["choices"][0]["message"]["content"] or "").strip(), model
        last = f"{model} HTTP {r.status_code}"
    raise RuntimeError(f"W&B {last}")


CARD_DISCLAIMER = "Archive-based scouting hypothesis. Not verified ad inventory or audience measurement."
# id -> (deliverable title, reader, what to focus on, label of the third bullet, extra limit stated under Unknowns)
USE_CASES = {
    "scouting": ("Scouting card", "a campaign or retail site planner",
                 "visible ad surfaces, venue entrances, the storefront mix and street activity",
                 "Why it might matter", "Audience size, inventory ownership and availability are not known from footage."),
    "claims": ("Location report", "an insurance adjuster or fleet safety manager checking where a clip was recorded",
               "how the location was established and how precise it is, traffic control, lanes and other road users in view",
               "Relevance to the review", "Location comes from the footage only; confirm against telematics, police or witness records."),
    "roadwork": ("Street audit", "a city or utility inspector",
                 "cones, lane closures, work zones and signage, and whether they block drivers, cyclists or pedestrians",
                 "Possible issue", "Conditions may have changed since recording; permits are not checked."),
    "curb": ("Curb report", "a curb-management or city logistics planner",
             "delivery vehicles stopped in live lanes or bike lanes, loading activity and double parking",
             "Possible issue", "One clip is one moment; it is not a measure of how often this happens."),
    "safety": ("Safety observations", "a road-safety (Vision Zero) planner",
               "people riding or walking, crossings, bike lanes, parked bicycles and close interactions with vehicles",
               "Possible safety concern", "Clips are examples, not counts of people, trips or collisions."),
    "verify": ("Verification note", "a newsroom fact-checker confirming where footage was filmed",
               "which signs anchor the location, whether the route check agrees, and what could make the placement wrong",
               "Confidence", "This places locations only; it does not identify people or vehicles."),
}


def card_system(use_case: str) -> str:
    title, reader, focus, third, limit = USE_CASES.get(use_case, USE_CASES["scouting"])
    return (
        f"You write a short '{title}' for {reader}, from archived dashcam evidence. Use only the JSON. "
        "Each clip has an index caption (written at ingest), a second_look (Cosmos Reason re-watched the clip and read signs), "
        "a place (how the location was earned: signs read, matched to OpenStreetMap, checked against the drive's route), "
        "and a reviewer verdict. Write plain language for that reader: never print internal codes or field names. "
        "Use the place's `precision` phrase as given. Never invent coordinates, dates, times, counts, footfall, demographics, "
        "prices, ROI, fault, or that a business is open. Never describe or identify people, faces or licence plates. "
        f"Focus on {focus}. Format, in markdown:\n"
        "One `## <place label or 'Research lead'>` block per clip, each with four short bullets: "
        "**Where** (precision phrase, then the evidence chain in one sentence: what sign was read, what OpenStreetMap confirmed, "
        "and route support or a route rejection if present); **Seen** (specific things from the second look relevant to the focus; "
        "cite the clip file name and seconds; mention any disagreement with the index caption); "
        f"**{third}** (one specific, labeled point tied to what was seen, not generic talk); "
        "**Check on site** (one or two concrete checks).\n"
        "Unlocated clips are research leads: say the location is unknown.\n"
        f"Then `## Unknowns` with one line that includes: {limit} "
        f"End with exactly: {CARD_DISCLAIMER}"
    )
PRECISION = {
    "verified_intersection": "verified intersection (within about 60 m)",
    "landmark_match": "near a named landmark (camera within viewing distance, about 150 m)",
    "approximate_area": "approximate area (within about 350 m)",
    "approximate_street": "somewhere along this street (position unknown)",
    "street_only": "street name only (cannot be narrowed down)",
    "unlocated": "location unknown",
}


@app.post("/api/card")
def card() -> Any:
    body = request.get_json(silent=True) or {}
    evidence = body.get("evidence") or []
    if not evidence:
        return jsonify({"error": "select at least one clip"}), 400
    slim = []
    for item in evidence[:8]:
        rl = item.get("relook") or {}
        pl = item.get("place") or {}
        slim.append({
            "clip": _safe_text(item.get("id"), 300).rsplit("/", 1)[-1],
            "seconds": [item.get("segment_start_sec"), item.get("segment_end_sec")],
            "camera_id": _safe_text(item.get("camera_id"), 60),
            "index_caption": _safe_text(item.get("caption"), 700),
            "second_look": {
                "signs_read": rl.get("signs_read"), "business_signs": rl.get("business_signs"),
                "riding": rl.get("riding"), "parked_bicycles": rl.get("parked"), "roadwork": rl.get("roadwork"),
                "ad_surfaces": rl.get("ad_surfaces"), "disagreements": rl.get("disagreements"),
            } if rl else "not re-watched",
            "place": {
                "precision": PRECISION.get(pl.get("status"), "location unknown"), "label": pl.get("label"),
                "how": pl.get("method"),
                "corroborated_by": (pl.get("corroborated_by") or []) + (item.get("corroborated_by") or []),
                "route_support": pl.get("route_support") or [],
                "route_rejected": (pl.get("route_rejected") or {}).get("reason"),
            } if pl else {"precision": "location unknown", "how": "not checked"},
            "reviewer": item.get("review") or "not reviewed",
            "planner_note": _safe_text(item.get("note"), 300),
        })
    use_case = body.get("use_case") if body.get("use_case") in USE_CASES else "scouting"
    title = USE_CASES[use_case][0]
    payload = {"query": _safe_text(body.get("query"), 400), "archive_scope": "Toronto location filter, top-k search, recorded footage", "evidence": slim}
    t0 = time.time()
    try:
        text, model = wandb_chat([{"role": "system", "content": card_system(use_case)}, {"role": "user", "content": json.dumps(payload)}])
        provider, ok = f"W&B Inference · {model}", True
    except Exception as exc:
        lines = [f"# {title} (model unavailable, evidence only)", f"**Query:** {payload['query']}", "## Observations"]
        for s in slim:
            lines.append(f"- `{s['clip']}` {s['seconds']} · {s['place'].get('precision')} {s['place'].get('label') or ''}")
        lines += ["## Unknowns", f"Current condition and recording date. {USE_CASES[use_case][4]}",
                  "", CARD_DISCLAIMER, f"_Card model error: {exc}_"]
        text, provider, ok = "\n".join(lines), "fallback (no model)", False
    return jsonify({
        "ok": ok, "provider": provider, "latency_ms": int((time.time() - t0) * 1000), "card": text,
        "title": title, "use_case": use_case, "disclaimer": CARD_DISCLAIMER,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
