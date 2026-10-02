# Evidence Atlas

**Puts GPS-less dashcam footage on the map using only what the camera saw.**
Cosmos Reason reads the signs, OpenStreetMap supplies the coordinates, and a route-continuity
check rejects impossible matches. Every pin opens the evidence behind it.

Built for the VAST Builders Challenge (Real-Time Video Agents Hack) on top of the VAST video
search stack (VSS).

## The problem

Dashcam and fleet archives often arrive without GPS. VAST VSS finds clips by meaning
("cyclists near storefronts"), but its location filter only knows the city. A pin at the city
centroid is wrong, and asking a language model for coordinates is worse.

## How a clip earns a pin

1. **Read.** Cosmos Reason re-watches each 5-second clip and lists the street-name signs,
   overhead guide signs, business names and venues it can read, plus whether anyone is riding
   or parking a bike, roadwork, and ad surfaces.
2. **Match.** Names go to OpenStreetMap (Overpass), never to a model, for coordinates.
   - Two corner street-name signs whose streets share a node: **verified intersection** (~60 m).
   - A uniquely named venue with tightly clustered matches: **landmark** (~150 m).
   - Overhead guide signs naming streets that meet: **approximate area** (~350 m), never a pin.
   - Stock phrases ("FOR LEASE", "DENTAL CLINIC"), names with several far-apart matches, and
     street pairs that cross in two places are treated as ambiguous.
3. **Check.** Clips come from continuous drives. A placement that would need the car to cover
   more than ~25 m/s between neighbouring clips is **rejected**, and the reason is shown
   (for example, an "Art Gallery of Ontario" banner read 5 s after the Royal Ontario Museum,
   1.6 km away). Seeing the same sign twice never counts as support.
4. **Review.** Accept or reject places, save clips to a shortlist, and generate a report that
   keeps the evidence and its limits attached.

On the Toronto archive: 1,080 clips re-watched, 3,658 signs and names read, 83 distinct streets,
86 clips placed, 18 rejected by the route check, 831 left off the map rather than guessed.

## Use cases built in

Each use case runs a tested search and writes its own report (W&B Inference, Llama 3.3 70B),
from the saved evidence only.

| Use case | Report | Who pays |
|---|---|---|
| Out-of-home ad and retail site scouting | Site brief | Agencies per seat, or per corridor |
| Insurance and fleet: where did this clip happen? | Location report | Per-clip API in claims and telematics tools |
| Roadwork and lane-closure audit | Street audit | City or utility contracts using existing fleet cameras |
| Curb and delivery management | Curb report | Data licensing to curb platforms |
| Cycling and pedestrian safety | Safety observations | Vision Zero studies, planning consultancies |
| Verify where footage was filmed | Verification note | Newsroom verification desks |

Places, not people: nothing identifies faces, licence plates or individuals.

## Stack

- **VAST Data**: VSS semantic search and ingest, VastDB, S3 clip storage
- **NVIDIA**: Cosmos Reason (second look, sign reading), Cosmos-Embed1 (VSS search), YOLO object
  classes from the index metadata
- **CoreWeave / Weights & Biases**: W&B Inference (Llama 3.3 70B, falling back to 3.1 8B) for reports
- **OpenStreetMap** via Overpass for every coordinate; Leaflet with a muted OSM basemap
- **App**: Flask + vanilla JavaScript, deployed to Kubernetes; built with Cursor

## Files

| File | What it does |
|---|---|
| `pipeline.py` | VSS client, Cosmos Reason second look, sign parsing, OpenStreetMap matching, place resolution |
| `build_atlas.py` | Re-watches the whole archive, runs the route check, writes `atlas.json.gz` and `osm_cache.json.gz` |
| `main.py` | Flask routes: atlas, search (with a keyword fallback when VSS search is down), live re-watch, clip proxy, reports |
| `index.html` | The single-page app: map, evidence chain, use cases, shortlist, reports |
| `deploy.sh` | Deploys to the team Kubernetes namespace under `/app` |
| `DEMO.md` | Three-minute demo script |

The built atlas and OpenStreetMap cache are not committed, because they contain the team's
storage paths. Rebuild them with `build_atlas.py`.

## Run it

Requires the VAST Builders Challenge environment (a team config under `/config`, with VSS,
W&B and GPU credentials in environment variables) and `COSMOS_URL` set to the Cosmos Reason
endpoint.

```bash
pip install -r requirements.txt
python3 build_atlas.py --location toronto   # resumable
./deploy.sh                                  # or: DATA_DIR=. python3 main.py
```

## Limits

- Precision is stated for every place; "street only" clips are never pinned.
- Search results are top-k, not a census. Counts are clips, not people or events.
- Reports are archive-based hypotheses, not verified ad inventory or audience measurement.

Map data © OpenStreetMap contributors (ODbL).
