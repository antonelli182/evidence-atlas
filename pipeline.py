#!/usr/bin/env python3
"""Evidence Atlas pipeline: VSS client, Cosmos second look, OpenStreetMap placement. No web framework."""

from __future__ import annotations

import base64
import gzip
import itertools
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import requests

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE)
PORT = int(os.environ.get("PORT", "8080"))
VSS_URL = os.environ.get("VSS_URL", "").rstrip("/")
VSS_USERNAME = os.environ.get("VSS_USERNAME", "")
VSS_PASSWORD = os.environ.get("VSS_PASSWORD", "")
WANDB_API_KEY = os.environ.get("WANDB_API_KEY", "")
WANDB_TEAM = os.environ.get("WANDB_TEAM", "")
WANDB_PROJECT = os.environ.get("WANDB_PROJECT", "")
WANDB_BASE = "https://api.inference.wandb.ai/v1"
WANDB_MODELS = [
    os.environ.get("WANDB_MODEL", "meta-llama/Llama-3.3-70B-Instruct"),
    "meta-llama/Llama-3.1-8B-Instruct",
]
GPU_BEARER_TOKEN = os.environ.get("GPU_BEARER_TOKEN", "")
COSMOS_URL = os.environ.get("COSMOS_URL", "").rstrip("/")
COSMOS_MODEL = os.environ.get("COSMOS_MODEL", "nvidia/cosmos3-nano-reasoner")

ALLOWED_PREFIX = f"s3://{VSS_USERNAME}-vss-chunks-segments/" if VSS_USERNAME else "s3://"
S3_RE = re.compile(r"^s3://[A-Za-z0-9._\-/]+$")
UA = "EvidenceAtlas/0.2 (VAST Builders Challenge team-48)"
TORONTO_BBOX = "43.58,-79.64,43.86,-79.11"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
OSM_BUDGET_S = float(os.environ.get("OSM_BUDGET_S", "25"))
BEFORE_FILE = HERE / "before_reingest_set02_0022.json"

_token_lock = threading.Lock()
_token: str | None = None
_token_at = 0.0
_cosmos_slots = threading.BoundedSemaphore(3)


def _safe_text(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def allowed_source(source: str) -> bool:
    return bool(S3_RE.match(source)) and source.startswith(ALLOWED_PREFIX)


# --------------------------------------------------------------------------- VSS


def vss_login(force: bool = False) -> str:
    global _token, _token_at
    with _token_lock:
        if _token and not force and (time.time() - _token_at) < 20 * 60:
            return _token
        if not VSS_URL or not VSS_USERNAME or not VSS_PASSWORD:
            raise RuntimeError("VSS credentials are not configured")
        r = requests.post(
            f"{VSS_URL}/api/v1/auth/login",
            json={"username": VSS_USERNAME, "password": VSS_PASSWORD},
            timeout=30,
            headers={"User-Agent": UA},
        )
        r.raise_for_status()
        _token = r.json()["access_token"]
        _token_at = time.time()
        return _token


def vss_request(method: str, path: str, **kwargs: Any) -> requests.Response:
    timeout = kwargs.pop("timeout", 90)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("User-Agent", UA)
    url = path if path.startswith("http") else f"{VSS_URL}{path}"
    headers["Authorization"] = f"Bearer {vss_login()}"
    r = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    if r.status_code == 401:
        headers["Authorization"] = f"Bearer {vss_login(force=True)}"
        r = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    return r


def fetch_clip_bytes(source: str, limit: int = 30_000_000) -> bytes:
    url = f"{VSS_URL}/api/v1/videos/stream?source={quote(source, safe='')}&token={vss_login()}"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=120)
    if r.status_code == 401:
        url = f"{VSS_URL}/api/v1/videos/stream?source={quote(source, safe='')}&token={vss_login(force=True)}"
        r = requests.get(url, headers={"User-Agent": UA}, timeout=120)
    r.raise_for_status()
    if len(r.content) > limit:
        raise RuntimeError("clip too large for re-watch")
    return r.content


# ------------------------------------------------------------- caption claims

RIDING_RE = re.compile(
    r"\b(cyclists?|riding (?:a |their )?(?:bicycles?|bikes?)|rides (?:a )?(?:bicycle|bike)|on (?:a )?(?:bicycle|bike)\b|biking)",
    re.I,
)
PARKED_RE = re.compile(
    r"\b(parked (?:bicycles?|bikes?)|(?:bicycles?|bikes?) (?:are |is )?(?:parked|locked)|bike racks?)", re.I
)


def caption_claims(caption: str) -> dict[str, bool]:
    return {
        "riding": bool(RIDING_RE.search(caption or "")),
        "parked": bool(PARKED_RE.search(caption or "")),
    }


def category_for(hit: dict[str, Any]) -> str:
    classes = hit.get("object_classes") or []
    if isinstance(classes, str):
        classes = [classes]
    text = (hit.get("reasoning_content") or "").lower()
    claims = caption_claims(text)
    if "construction" in text or "roadwork" in text or "excavator" in text:
        return "construction"
    if claims["riding"]:
        return "cyclist"
    if "storefront" in text or "shop" in text or "retail" in text:
        return "storefront"
    if claims["parked"] or "bicycle" in " ".join(str(c).lower() for c in classes):
        return "parked_bicycles"
    if "truck" in text or "delivery" in text:
        return "delivery"
    if "person" in text or "pedestrian" in text:
        return "pedestrian"
    return "other"


def public_hit(hit: dict[str, Any]) -> dict[str, Any]:
    source = hit.get("source") or ""
    caption = _safe_text(hit.get("reasoning_content"), 2500)
    classes = hit.get("object_classes") or []
    if isinstance(classes, str):
        classes = [c.strip() for c in classes.split(",") if c.strip()]
    return {
        "id": source,
        "source": source,
        "original_video": hit.get("original_video"),
        "caption": caption,
        "caption_claims": caption_claims(caption),
        "similarity": hit.get("similarity_score"),
        "location": hit.get("location"),
        "camera_id": hit.get("camera_id"),
        "capture_type": hit.get("capture_type"),
        "segment_start_sec": hit.get("segment_start_sec"),
        "segment_end_sec": hit.get("segment_end_sec"),
        "object_classes": classes,
        "category": category_for(hit),
        "location_status": "city_only" if hit.get("location") else "unlocated",
        "clip_url": f"/app/api/clip?source={quote(source, safe='')}" if source else None,
    }


# ------------------------------------------------------- Cosmos second look

RELOOK_PROMPT = (
    "You are verifying a Toronto dashcam clip for a street-scouting tool. Return only JSON with keys: "
    '"street_signs": list of {"text": exact text read, "kind": "corner_name" for a street-name sign posted at a corner, '
    '"overhead_guide" for a green directional or overhead sign, "other"}; '
    '"business_signs": list of exact storefront, venue or business names read; '
    '"people_riding_bicycles": "yes" | "no" | "unclear"; "parked_bicycles": "yes" | "no" | "unclear"; '
    '"ad_surfaces": list of short descriptions of billboards, bus-shelter ads or posters; '
    '"roadwork": "yes" | "no" | "unclear". '
    "List each distinct sign once, at most 10 street_signs and 10 business_signs. "
    "Only include text you can actually read. Never guess."
)

SUFFIX = {
    "st": "Street", "street": "Street", "ave": "Avenue", "av": "Avenue", "avenue": "Avenue",
    "blvd": "Boulevard", "boulevard": "Boulevard", "rd": "Road", "road": "Road", "dr": "Drive",
    "drive": "Drive", "cres": "Crescent", "crescent": "Crescent", "pl": "Place", "place": "Place",
    "ln": "Lane", "lane": "Lane", "ct": "Court", "court": "Court", "way": "Way", "pkwy": "Parkway",
    "parkway": "Parkway", "terr": "Terrace", "terrace": "Terrace", "sq": "Square", "square": "Square",
    "gdns": "Gardens", "gardens": "Gardens", "cir": "Circle", "circle": "Circle",
}
DIRS = {"e": "East", "east": "East", "w": "West", "west": "West", "n": "North", "north": "North",
        "s": "South", "south": "South"}
STREET_RE = re.compile(
    r"((?:[A-Z][A-Za-z'\-]+\.?\s){1,3}?)"
    r"(St|Street|Ave|Av|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Cres|Crescent|Pl|Place|Ln|Lane|Ct|Court|Way|"
    r"Pkwy|Parkway|Terr|Terrace|Sq|Square|Gdns|Gardens|Cir|Circle)\b\.?"
    r"(?:\s(E|W|N|S|East|West|North|South)\b\.?)?"
)
NOT_STREET_BASE = {"one", "no", "do", "not", "the", "bike", "bicycle", "bus", "fire", "a", "this", "all", "any", "each"}
LEAD_WORDS = {"on", "at", "to", "onto", "via", "from", "formerly", "u-turn", "use", "for", "near", "and", "exit", "into"}
STREET_RANK = {"corner_name": 0, "overhead_guide": 1, "other": 2}
REGULATORY_RE = re.compile(
    r"^(\d+|[A-Z]{1,2})$|one way|stop|slow|\bno\b|do not|parking|turn|yield|speed|\blane\b|keep|area|"
    r"residential|only|except|tow|\bbus\b|taxi|\bbike\b|crossing|here on red|school|zone|detour|closed|ahead|"
    r"\bexit\b|max|km/h|right|left|caution|entrance|exit|\d ?(am|pm)\b|\bmon ?- ?fri\b",
    re.I,
)


GENERIC_SIGN_RE = re.compile(
    r"^(for (lease|sale|rent)|pay here|now open|open( now)?|grand opening|sale|welcome|new patients|always welcome|"
    r"(walk in )?dental( clinic| office| centre)?|(family )?(medical|health) (clinic|centre)|nails?( (&|and) spa)?|"
    r"hair( salon| studio)?|barber ?shop|vape( store| shop)?|ice cream|pizza|sushi|coffee( shop)?|cafe|pharmacy|"
    r"convenience( store)?|variety( store)?|bicycle shop|bike shop|car wash|gas station|bus stop|bank|atm|"
    r"fresh (fruit|produce)|fruit market|grocery|restaurant|bar|pub|bakery|deli|laundromat|dry cleaners?|"
    r"real estate|condos?( for sale)?|apartments?( for rent)?|office space|retail space|coming soon|sold)$",
    re.I,
)


def landmark_ok(name: str) -> bool:
    """Single words ("EAST", "STUCCO") and stock phrases ("FOR LEASE", "DENTAL CLINIC") name no particular place."""
    words = re.findall(r"[A-Za-z0-9&']+", name)
    return (len(words) >= 2 and len(name) >= 5 and not REGULATORY_RE.search(name)
            and not GENERIC_SIGN_RE.match(" ".join(words)))


def parse_model_json(text: str) -> tuple[dict[str, Any], bool]:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    start = t.find("{")
    if start >= 0:
        body = t[start:]
        end = body.rfind("}")
        if end >= 0:
            try:
                parsed = json.loads(body[: end + 1])
                if isinstance(parsed, dict):
                    return parsed, True
            except Exception:
                pass
    out: dict[str, Any] = {"street_signs": [], "business_signs": [], "ad_surfaces": []}
    for m in re.finditer(r'\{\s*"text"\s*:\s*"([^"]+)"\s*,\s*"kind"\s*:\s*"([^"]+)"', t):
        out["street_signs"].append({"text": m.group(1), "kind": m.group(2)})
    bs = re.search(r'"business_signs"\s*:\s*\[(.*?)(?:\]|$)', t, re.S)
    if bs:
        out["business_signs"] = re.findall(r'"([^"]+)"', bs.group(1))
    for key in ("people_riding_bicycles", "parked_bicycles", "roadwork"):
        m = re.search(rf'"{key}"\s*:\s*"?(yes|no|unclear|true|false)', t, re.I)
        if m:
            out[key] = m.group(1).lower()
    return out, False


def yn(value: Any) -> str:
    v = str(value).strip().lower()
    if v in ("yes", "true"):
        return "yes"
    if v in ("no", "false"):
        return "no"
    return "unclear"


def parse_streets(text: str, kind: str) -> list[dict[str, str]]:
    src = text.title() if text.isupper() else text
    found = []
    for m in STREET_RE.finditer(src):
        words = re.sub(r"[^A-Za-z' \-]", "", m.group(1)).split()
        for i in range(len(words) - 1, -1, -1):
            if words[i].lower() in LEAD_WORDS:
                words = words[i + 1:]
                break
        base = " ".join(words)
        if not base or base.split()[0].lower() in NOT_STREET_BASE:
            continue
        osm = f"{base} {SUFFIX[m.group(2).lower()]}"
        if m.group(3):
            osm += f" {DIRS[m.group(3).lower()]}"
        found.append({"read": m.group(0).strip(), "osm_name": osm, "kind": kind})
    return found


def clues_from_signs(signs_read: list[dict[str, str]], business: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    """Street names (best sign kind wins) and landmark candidates from the text Cosmos read."""
    streets: dict[str, dict[str, str]] = {}
    landmarks: list[str] = []
    for sign in signs_read:
        text, kind = sign["text"], sign["kind"]
        parsed = parse_streets(text, kind)
        for s in parsed:
            prev = streets.get(s["osm_name"])
            if not prev or STREET_RANK[s["kind"]] < STREET_RANK[prev["kind"]]:
                streets[s["osm_name"]] = s
        if not parsed and kind == "other":
            landmarks.append(text)
    for b in business:
        parsed = parse_streets(b, "other")
        for s in parsed:
            streets.setdefault(s["osm_name"], s)
        if not parsed and b not in landmarks:
            landmarks.append(b)
    ordered = sorted(streets.values(), key=lambda s: STREET_RANK[s["kind"]])[:6]
    return ordered, [l for l in landmarks if landmark_ok(l)][:4]


def cosmos_relook(video: bytes) -> tuple[str, int]:
    body = {
        "model": COSMOS_MODEL,
        "max_tokens": 600,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": RELOOK_PROMPT},
                    {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + base64.b64encode(video).decode()}},
                ],
            }
        ],
    }
    t0 = time.time()
    with _cosmos_slots:
        r = requests.post(
            f"{COSMOS_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {GPU_BEARER_TOKEN}", "User-Agent": UA},
            json=body,
            timeout=150,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Cosmos Reason HTTP {r.status_code}")
    return r.json()["choices"][0]["message"]["content"] or "", int((time.time() - t0) * 1000)


def build_relook(source: str, caption: str, object_classes: list[Any]) -> dict[str, Any]:
    video = fetch_clip_bytes(source)
    raw, latency = cosmos_relook(video)
    data, clean = parse_model_json(raw)

    signs_read: list[dict[str, str]] = []
    seen_text: set[str] = set()
    for item in data.get("street_signs") or []:
        if isinstance(item, str):
            item = {"text": item, "kind": "other"}
        text = _safe_text(item.get("text"), 120).strip()
        kind = item.get("kind") if item.get("kind") in STREET_RANK else "other"
        if not text or text.lower() in seen_text:
            continue
        seen_text.add(text.lower())
        signs_read.append({"text": text, "kind": kind})
    business = []
    for b in data.get("business_signs") or []:
        b = _safe_text(b, 120).strip()
        if b and b.lower() not in seen_text and not REGULATORY_RE.search(b):
            seen_text.add(b.lower())
            business.append(b)
    streets, landmarks = clues_from_signs(signs_read, business)

    riding = yn(data.get("people_riding_bicycles"))
    parked = yn(data.get("parked_bicycles"))
    roadwork = yn(data.get("roadwork"))
    claims = caption_claims(caption)
    classes = " ".join(str(c).lower() for c in object_classes or [])
    disagreements = []
    if claims["riding"] and riding == "no":
        disagreements.append("The index caption mentions someone riding. The second look saw no one riding.")
    if not claims["riding"] and riding == "yes":
        disagreements.append("The second look saw someone riding that the index caption does not mention.")
    if "bicycle" in classes and riding != "yes" and parked == "yes":
        disagreements.append("YOLO detected a bicycle. The second look says it is parked, not ridden.")
    cap_low = (caption or "").lower()
    new_signs = [s["text"] for s in signs_read if s["text"].lower() not in cap_low and not REGULATORY_RE.search(s["text"])]
    new_signs += [b for b in business if b.lower() not in cap_low]
    if new_signs:
        disagreements.append(f"The second look read {len(new_signs)} sign(s) the index caption never mentions.")

    if riding == "yes":
        category = "cyclist"
    elif business or landmarks:
        category = "storefront"
    elif roadwork == "yes":
        category = "construction"
    elif parked == "yes":
        category = "parked_bicycles"
    else:
        category = "other"

    return {
        "source": source,
        "model": COSMOS_MODEL,
        "latency_ms": latency,
        "clip_bytes": len(video),
        "clean_json": clean,
        "signs_read": signs_read[:16],
        "streets": streets,
        "landmarks": landmarks,
        "business_signs": business[:10],
        "ad_surfaces": [_safe_text(a, 160) for a in (data.get("ad_surfaces") or [])][:6],
        "riding": riding,
        "parked": parked,
        "roadwork": roadwork,
        "caption_claims": claims,
        "disagreements": disagreements,
        "category": category,
    }


# --------------------------------------------------------- OpenStreetMap check


def _clean_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9' \-&]", "", name).strip()


def _name_regex(osm_name: str) -> str:
    n = _clean_name(osm_name)
    if re.search(r" (East|West|North|South)$", n):
        return f"^{n}$"
    return f"^{n}( (East|West|North|South))?$"


class Overpass:
    """Per-name OpenStreetMap cache. Missing names are fetched in one batched query.

    Street entry: {"ways": [{"n": [node ids], "g": [[lat, lon], ...]}]} (g aligned with n).
    Landmark entry: {"pts": [[lat, lon], ...]}.
    Public Overpass mirrors are slow and rate limited, so the app ships a pre-warmed
    cache and every request gets a hard time budget.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last = 0.0
        self.cache: dict[str, dict[str, Any]] = {}
        self.runtime_file = Path("/tmp/osm_cache.json.gz")
        for path in (DATA_DIR / "osm_cache.json.gz", self.runtime_file):
            try:
                self.cache.update(json.loads(gzip.decompress(path.read_bytes())))
            except Exception:
                pass

    def _persist(self) -> None:
        try:
            self.runtime_file.write_bytes(gzip.compress(json.dumps(self.cache, separators=(",", ":")).encode()))
        except Exception:
            pass

    @staticmethod
    def street_key(name: str) -> str:
        return "s:" + _clean_name(name).lower()

    @staticmethod
    def landmark_key(name: str) -> str:
        return "l:" + _clean_name(name).lower()

    def _fetch(self, streets: list[str], landmarks: list[str], deadline: float) -> None:
        # One alternation per kind: Overpass scans the name index once per regex, not once per name.
        parts = []
        if streets:
            parts.append(f'way["highway"]["name"~"{"|".join(_name_regex(s) for s in streets)}"]({TORONTO_BBOX});out body geom;')
        if landmarks:
            parts.append(f'nwr["name"~"^({"|".join(_clean_name(l) for l in landmarks)})$",i]({TORONTO_BBOX});out center;')
        err = "no endpoint"
        for url in OVERPASS_ENDPOINTS:
            wait = 1.2 - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            remaining = deadline - time.time()
            if remaining < 3:
                err = "time budget used up"
                break
            q = f"[out:json][timeout:{int(min(remaining, 90))}];" + "".join(parts)
            try:
                r = requests.post(url, data={"data": q}, headers={"User-Agent": UA}, timeout=remaining)
                self.last = time.time()
                if r.status_code != 200:
                    err = f"HTTP {r.status_code}"
                    continue
                elements = r.json().get("elements", [])
            except Exception as exc:
                self.last = time.time()
                err = type(exc).__name__
                continue
            for s in streets:
                rx = re.compile(_name_regex(s))
                ways = [
                    {"n": e.get("nodes", []), "g": [[round(p["lat"], 6), round(p["lon"], 6)] for p in e.get("geometry", [])]}
                    for e in elements
                    if e["type"] == "way" and "highway" in e.get("tags", {}) and rx.match(e["tags"].get("name", ""))
                ]
                self.cache[self.street_key(s)] = {"ways": ways}
            for l in landmarks:
                want = _clean_name(l).lower()
                pts = [p for p in (_el_point(e) for e in elements if _clean_name(e.get("tags", {}).get("name", "")).lower() == want) if p]
                self.cache[self.landmark_key(l)] = {"pts": [list(p) for p in pts]}
            self._persist()
            return
        raise RuntimeError(f"OpenStreetMap lookup failed ({err})")

    def lookup(self, streets: list[str], landmarks: list[str], budget_s: float = OSM_BUDGET_S) -> str | None:
        """Fills the cache for these names. Returns an error string if live lookup failed."""
        deadline = time.time() + budget_s
        miss_s = [s for s in dict.fromkeys(streets) if self.street_key(s) not in self.cache]
        miss_l = [l for l in dict.fromkeys(landmarks) if self.landmark_key(l) not in self.cache]
        if not miss_s and not miss_l:
            return None
        if not self.lock.acquire(timeout=max(0.0, deadline - time.time() - 3)):
            return "OpenStreetMap busy, try again"
        try:
            miss_s = [s for s in miss_s if self.street_key(s) not in self.cache]
            miss_l = [l for l in miss_l if self.landmark_key(l) not in self.cache]
            if miss_s or miss_l:
                self._fetch(miss_s, miss_l, deadline)
            return None
        except RuntimeError as exc:
            return str(exc)
        finally:
            self.lock.release()

    def street(self, name: str) -> list[dict[str, Any]] | None:
        entry = self.cache.get(self.street_key(name))
        return None if entry is None else entry["ways"]

    def landmark(self, name: str) -> list[tuple[float, float]] | None:
        entry = self.cache.get(self.landmark_key(name))
        return None if entry is None else [tuple(p) for p in entry["pts"]]


def cluster_points(pts: list[tuple[float, float]], within_m: float) -> list[tuple[float, float]]:
    """Greedy clustering; returns one representative (the first point) per cluster."""
    sites: list[tuple[float, float]] = []
    for p in pts:
        if not any(haversine(p, s) <= within_m for s in sites):
            sites.append(p)
    return sites


def shared_nodes(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[tuple[float, float]]:
    coords: dict[int, tuple[float, float]] = {}
    for w in a:
        for nid, g in zip(w["n"], w["g"]):
            coords[nid] = (g[0], g[1])
    out, seen = [], set()
    for w in b:
        for nid in w["n"]:
            if nid in coords and nid not in seen:
                seen.add(nid)
                out.append(coords[nid])
    return out


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h))


def _el_point(e: dict[str, Any]) -> tuple[float, float] | None:
    if "lat" in e:
        return (e["lat"], e["lon"])
    c = e.get("center")
    if c:
        return (c["lat"], c["lon"])
    return None


OSM = Overpass()


def resolve_place(streets: list[dict[str, str]], landmarks: list[str]) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    result: dict[str, Any] = {
        "status": "unlocated",
        "precision": "none",
        "label": None,
        "method": "No readable street or landmark matched OpenStreetMap. The clip stays in the unlocated tray.",
        "point": None,
        "radius_m": None,
        "geometry": None,
        "coordinate_subject": "none",
        "reference": "OpenStreetMap (© OpenStreetMap contributors, ODbL)",
        "checks": checks,
    }
    names = [s for s in streets if s.get("osm_name")][:4]
    landmarks = [l for l in landmarks if landmark_ok(l)][:3]
    err = OSM.lookup([n["osm_name"] for n in names], landmarks)
    if err:
        checks.append({"check": "OpenStreetMap", "result": f"{err}; names not in the local cache stay unchecked"})
        result["osm_error"] = err

    found = []
    for a, b in itertools.combinations(names, 2):
        label = f"{a['osm_name']} × {b['osm_name']}"
        wa, wb = OSM.street(a["osm_name"]), OSM.street(b["osm_name"])
        if wa is None or wb is None:
            checks.append({"check": label, "result": "not checked (OpenStreetMap unavailable)"})
            continue
        pts = shared_nodes(wa, wb)
        if not pts:
            checks.append({"check": label, "result": "these streets do not meet"})
            continue
        sites = cluster_points(pts, 300)
        if len(sites) > 1:
            checks.append({"check": label, "result": f"these streets meet at {len(sites)} separate places in Toronto, too ambiguous"})
            continue
        checks.append({"check": label, "result": f"{len(pts)} shared OpenStreetMap node(s), one place"})
        found.append((a, b, sites[0]))

    along_street = False
    corner_pairs = [f for f in found if f[0]["kind"] == "corner_name" and f[1]["kind"] == "corner_name"]
    spread = max((haversine(f[2], g[2]) for f in found for g in found), default=0)
    if corner_pairs:
        a, b, pt = corner_pairs[0]
        result.update(
            status="verified_intersection", precision="intersection", point=pt, radius_m=60,
            label=f"{a['osm_name']} × {b['osm_name']}", coordinate_subject="camera viewpoint at or near this intersection",
            method=(f"Read corner street-name signs “{a['read']}” and “{b['read']}” in this clip. "
                    f"The OpenStreetMap streets {a['osm_name']} and {b['osm_name']} share a node here."),
        )
    elif found and spread <= 700:
        lat = sum(f[2][0] for f in found) / len(found)
        lon = sum(f[2][1] for f in found) / len(found)
        result.update(
            status="approximate_area", precision="area", point=(lat, lon), radius_m=350,
            label=" / ".join(sorted({f[0]["osm_name"] for f in found} | {f[1]["osm_name"] for f in found})),
            coordinate_subject="area the camera was approaching",
            method=("Signs in this clip name streets that meet here. Guide signs point toward streets, "
                    "so the camera was near this area, not at a known spot."),
        )
    elif found:
        along_street = True
        checks.append({"check": "street pairs", "result": f"the named crossings are {int(spread)} m apart, so the signs point along a street, not to one spot"})
        common = max(names, key=lambda n: sum(n in (f[0], f[1]) for f in found))
        names = [common] + [n for n in names if n is not common]

    landmark_hits = []
    for name in landmarks:
        pts = OSM.landmark(name)
        if pts is None:
            checks.append({"check": f"landmark “{name}”", "result": "not checked (OpenStreetMap unavailable)"})
            continue
        if not pts:
            checks.append({"check": f"landmark “{name}”", "result": "no OpenStreetMap feature with this name"})
            continue
        spread = max(haversine(pts[0], p) for p in pts)
        if spread <= 300:
            checks.append({"check": f"landmark “{name}”", "result": "one OpenStreetMap feature in Toronto"})
            landmark_hits.append((name, pts[0]))
        else:
            checks.append({"check": f"landmark “{name}”", "result": f"{len(pts)} matches across Toronto, too ambiguous"})

    if landmark_hits and result["point"]:
        name, pt = landmark_hits[0]
        d = haversine(result["point"], pt)
        checks.append({"check": f"“{name}” vs street match", "result": f"{int(d)} m apart"})
        if d <= 350:
            result["corroborated_by"] = [f"landmark “{name}” {int(d)} m away"]
    elif landmark_hits:
        name, pt = landmark_hits[0]
        result.update(
            status="landmark_match", precision="landmark", point=pt, radius_m=150, label=name,
            coordinate_subject="landmark in view; camera within viewing distance",
            method=f"Read “{name}” in this clip. Exactly one OpenStreetMap feature in Toronto has that name.",
        )

    if result["status"] == "landmark_match":
        for n in names:
            ways = OSM.street(n["osm_name"]) or []
            d = min((haversine(result["point"], (g[0], g[1])) for w in ways for g in w["g"]), default=None)
            if d is not None:
                ok = d <= 250
                checks.append({"check": f"“{result['label']}” vs street sign “{n['read']}”",
                               "result": f"{n['osm_name']} passes {int(d)} m from it" + ("" if ok else ", too far to agree")})
                if ok:
                    result.setdefault("corroborated_by", []).append(f"street sign “{n['read']}”: {n['osm_name']} passes {int(d)} m away")

    if result["status"] == "unlocated" and names:
        s = names[0] if along_street else next((n for n in names if n["kind"] == "corner_name"), names[0])
        ways = OSM.street(s["osm_name"])
        if ways is None:
            result.update(status="street_only", precision="street", label=s["osm_name"],
                          method=f"Read “{s['read']}”. OpenStreetMap was unavailable, so the street was not checked.")
            return result
        geometry = [w["g"] for w in ways if w["g"]]
        points = sum(len(g) for g in geometry)
        if geometry and points <= 2500:
            checks.append({"check": s["osm_name"], "result": f"{len(geometry)} OpenStreetMap segment(s)"})
            result.update(
                status="approximate_street", precision="street", geometry=geometry, label=s["osm_name"],
                coordinate_subject="somewhere along this street",
                method=f"Read “{s['read']}” ({s['kind'].replace('_', ' ')} sign). Position along the street is unknown.",
            )
        else:
            checks.append({"check": s["osm_name"], "result": "too long or no geometry to highlight usefully" if geometry else "no OpenStreetMap street with this name"})
            result.update(status="street_only", precision="street", label=s["osm_name"],
                          method=f"Read “{s['read']}”, but it cannot be narrowed to a useful area.")
    return result
