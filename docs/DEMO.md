# First demo: run sheet (Saturday)

Goal: show that a requirement from the RFP becomes a verified, reviewable change in ERPNext without anyone writing code,
and that a human stays in control. Show only what is real. About 10 minutes.

## The day before (do these once)
1. Studio: Models screen shows a key, a cheap and a strong model. ERPNext screen shows 5 green steps (press **Prepare site** once more).
2. Requirements: import the RFP Excel (1,544 requirements, 629 gaps). The Overview shows the real numbers.
3. **Pre-run 3 requirements** the morning of the demo and keep them verified (a live run takes 3 to 10 minutes):
   pick simple field/validation gaps in SRM, for example "record a vendor risk rating", "preferred payment method with conditional UPI ID".
4. Export screen: save the GitHub repo and token, press **Test connection**, then **Publish pull request** once and merge it,
   so the demo shows a pull request that already has history. Do not merge during the demo unless the release build is the point.
5. Leave one run **unstarted**: it is the live moment.

## What is new since the first draft (show these)
- **SRM Home** (`/app/srm-home`): demo-pilot landing page (yellow band, black tiles, live count cards). Built by Studio, not by hand.
- **Supplier Portal pages** (web forms, login only): C-Form, CPS declaration, Bank Guarantee view, Source declaration.
- **Rebuild and health checks**: a broken DocType (missing table) is refused by a plan and repaired through Studio.
- Be honest about partial items: they show as **built** with the open part named. Only signed-off items are exported.

## The story
1. **Overview** (1 min): "This is the programme: 1,544 requirements, 629 gaps, what is verified, what needs a decision."
2. **Requirements** (1 min): filter to SRM gaps. Show the status chips. Explain: standard ERPNext is never rebuilt; only gaps.
3. **Start the live run** (kick it off, talk while it works, about 4 minutes):
   - Select one gap, press **Build**. Open the run.
   - Narrate the pipeline: rules first (free), cheap model analyses, strong model plans only when needed, a second model reviews, then policy decides.
   - Point at the tier counters: most steps are rules or engine, only a few are model calls.
4. **Approval** (1 min): open a pre-made high-risk run (permissions or workflow) waiting for approval. Show the exact change lines and risk. Approve or reject.
5. **Evidence** (1 min): open a verified run: readback checks plus saved-record tests, and the **Undo** button. Say: "nothing is trusted until it is read back from the live site".
6. **Export** (2 min): the pull request with fixtures, `studio_apply.py`, manifest and traceability. "Production is only changed by merging this and releasing the image, never by the Studio."
7. **Close** (30 s): roadmap on the Workbench preview (clearly labelled as preview): more change types, decision gateway, prompt studio.

## What to say if something goes wrong
- Live run is slow: "model calls take 20 to 40 seconds each; this is why most steps are rules". Switch to the pre-run one.
- Run ends as **built** or **needs input**: that is the point: "it will not claim verified without proof". Show the note.
- A verification fails: show that the change was **undone automatically**.
- GitHub is unreachable: show the **Download zip** instead; it is the same content.

## Do not show
- The Workbench as if it were live. It plays scripted data and is labelled so.
- Integration requirements (12, 19, 25, 413): shown as "needs integration", not built.
- Supplier Portal pages assume the supplier's login is linked to their Supplier record (User Permission); say so.
