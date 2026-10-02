# Evidence Atlas — 3-minute demo script

App: `http://<team host>/app` (see the submission for the live link)

**The point in one line:** 1,080 Toronto dashcam clips with no GPS. We put 86 of them on the map
using only what the camera saw, and we show the evidence for every pin, including the 18 we rejected.

Before going on stage, open the app once so the map tiles are cached, and clear the shortlist.

---

## 0:00 – 0:25 · The problem

> "The VAST stack can search this archive. Ask for 'cyclists near storefronts' and you get good clips.
> But a planner's next question is *where?* The footage has no GPS. The location filter just says
> 'toronto'. A pin at the city centroid is a lie, and asking an LLM for coordinates is a worse lie."

Point at the header: *Toronto dashcam archive · placed without GPS*, and the map note in the corner:
*Every pin is earned.*

## 0:25 – 1:05 · The whole-archive atlas (the wow)

The page opens on **Whole archive**, with the overview on the right. Read it:

> "Cosmos Reason re-watched all 1,080 clips. It read 3,658 signs and business names, and 83 different
> street names. Every name went to OpenStreetMap, not to a model, for coordinates. 86 clips earned a
> place on the map. 831 had nothing placeable, so they stay off the map, and we say so."

Point at **Show on map** on the left. It is the legend and the filter: verified intersection (10,
green), landmark (72, blue), approximate area (4, dashed), street only (145, drawn when selected),
**rejected by route check (18, red ✕)**, no place found (831, off by default).

## 1:05 – 1:45 · One clip, one evidence chain

Zoom to Yonge and Charles. Click the **Charles Street × Yonge Street** pin
(`20261001_062744_set02_video_chunk_0022`, 10–15 s). Press play.

Walk down **How we know** on the right:

1. **Cosmos Reason re-watched the clip**: it read "Charles St" and "Yonge St" as *corner* street-name
   signs, plus Kobi Korean Restaurant, Metro Cigar, Tim Hortons… (open *Everything it read*).
2. **Matched to OpenStreetMap**: the two streets share a node here, so it's a verified intersection
   (about 60 m). Coordinates come from OpenStreetMap, never from the model.
3. **Other evidence agrees**: 5 s earlier in the same drive, "CAA Theatre" (a unique OpenStreetMap
   feature) is 85 m away. Two independent signs agree.
4. **Your review**: Accept or reject the place.

Below the chain, **Index caption vs second look** shows what the ingest caption missed.

> "Corner street-name signs give a pin. Overhead guide signs only point toward streets, so they give
> a dashed area, never a pin."

(Optional: click **Jameson Avenue / Lake Shore Boulevard**, the dashed circle by the lake. The index
caption says someone is riding a bike; the second look says no one is. We flag the disagreement and
trust neither blindly. 304 clips in the archive have this kind of conflict.)

## 1:45 – 2:15 · The physics check (no GPS needed)

Click the red ✕ at Dundas and McCaul, **Art Gallery of Ontario**. It's also in the clip list, tagged
"✕ route conflict". The panel opens with **Not placed** and a red **Route check failed** step.

> "Cosmos really did read 'Art Gallery of Ontario', and OpenStreetMap has exactly one. A naive system
> drops a pin. But 5 seconds earlier this same drive was at the Royal Ontario Museum, 1.6 km away.
> A car can't do that. It was a banner, not the building. Rejected, and we show why."

Other rejections in the list: "University of Toronto" (13.9 km off route), "Sakura Sushi",
"Great Gulf" (a developer's sign on a hoarding).

## 2:15 – 2:50 · Who pays for this → shortlist → deliverable

Scroll the overview to **Who this helps**: six use cases, each with the problem, who pays, and a
real count from this archive.

| Use case | Who pays | In this archive |
|---|---|---|
| 📣 Ad & retail scouting | Agencies per seat, or per corridor brief | 807 clips with storefront or ad signs |
| 🛡️ Claims & fleet incidents | Per-clip API in claims and telematics tools | 86 placed, 231 on a known street, no GPS |
| 🚧 Roadwork audit | City or utility contract, using fleet cameras | 158 clips with roadwork |
| 🚚 Curb & deliveries | Data licensing to curb platforms | found by search, placed the same way |
| 🚲 Cycling & pedestrian safety | Vision Zero studies, consultancies | 164 riding, 276 parked bikes |
| 📰 Footage verification | Newsroom verification desks | 18 impossible matches rejected |

> "Same evidence engine, six buyers. The only thing that changes is the search and the deliverable."

Click **🛡️ Claims & fleet incidents** (VSS search takes about 30 s; run it once before going on
stage). The right panel explains the problem, what we do, and who pays. Save a landmark clip and a
street-only clip. Open **Shortlist**: *Write it as* is already set to **Location report**. Generate it
(W&B Inference, Llama 3.3 70B, about 9 s). It keeps the evidence chain, states the claim-specific
limit (*confirm against telematics, police or witness records*), and ends with the required
disclaimer. Switch *Write it as* to **Site brief** to show the same clips rewritten for a media
planner.

## 2:50 – 3:00 · Close

> "Search tells you *what*. Evidence Atlas tells you *where*, how sure we are, and why, for footage
> with no GPS. VAST VSS for search and storage, Cosmos Reason for the second look, OpenStreetMap
> for coordinates, physics for the sanity check."

---

## If asked

- **Live, not pre-computed?** Select any clip and press **Re-watch live**. Cosmos re-reads the 5 s
  clip in about 9 s, and the panel updates with the fresh read.
- **Did you improve ingest?** Yes. We re-ingested one chunk with a sign-reading prompt
  (**How it works → Show re-ingest before / after**). Its captions gained 3 names. On the same 6 clips the targeted
  second look read 36 signs. The index is for search; the second look is for evidence.
- **How do you avoid false pins?** Single words and stock phrases ("FOR LEASE", "DENTAL CLINIC")
  are never landmarks. Names with several OpenStreetMap matches are ambiguous. Street pairs that
  cross in two places are ambiguous. Then the route check runs. Repeated sightings of the same sign
  don't count as support.
- **What if search goes down?** If VSS search returns a server error (for example, Cosmos-Embed1
  restarting), the app falls back to keyword matches over the 1,080 re-watched clips and labels
  them as such. The map, evidence, shortlist and reports keep working.
- **Rebuild:** `python3 build_atlas.py --location toronto` (resumable), then `./deploy.sh`.
