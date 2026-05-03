# Next-session prompt — frontend polish

Paste the block below into a fresh Claude Code session. It's self-contained: it tells Claude where to start reading, what's already done, what specifically to build, and what to NOT touch (so a parallel backend session can keep running without merge pain).

---

```
You're picking up the ytFactory project at /Users/rohit/ytFactory mid-build.
Read /Users/rohit/ytFactory/HANDOFF.md first — that's the source of truth
for what already exists. DO NOT rebuild any of it; everything in HANDOFF.md
is shipped and working.

Your job this session is FRONTEND-ONLY polish on the web UI. The backend
will keep evolving in parallel — DO NOT touch /pipeline/, make_shorts.py,
pull_stories.py, channels/, or web/server.py. Your turf is exactly:

  web/static/index.html
  (and you may add web/static/app.js, web/static/styles.css if you want
  to split the inline script/style out)

Stack:
- Tailwind CSS via CDN (already loaded in index.html). Stay on the CDN.
- Vanilla JS using fetch / EventSource. No bundler, no framework.
- Single-page app — three sections (#picker, #lobby, #result-was-folded-into-#lobby).

Backend already provides (verified):
- GET  /api/niches             → 6 niches
- GET  /api/voices             → { languages: [{code,label,flag}], voices: [...], default }
                                 38 voices across 9 languages, each with sample_url
- GET  /api/voices/{id}/sample.wav (lazy-cached, ~3s first time)
- POST /api/jobs               body: { niche, options:{voice} }, returns {job_id}
- GET  /api/jobs/{id}/events   SSE stream of typed stage events
- GET  /api/jobs/{id}          job snapshot (events log + state + beat_prompts)
- GET  /api/jobs/{id}/audio    narration.wav (from tts.done onward)
- GET  /api/jobs/{id}/short    final mp4 (from compose.done onward)
- GET  /api/jobs/{id}/thumb/N  per-beat illustrated scene (img_NN.png)
- GET  /api/jobs/{id}/closer   closer_panel.png

Auth: token via cookie or ?token=, set once at /auth?token=<TOKEN>.

WHAT TO BUILD (priority order):

1. **Language tabs** above the voice grid.
   - Read languages from /api/voices' `languages` array.
   - Render a horizontal pill row: All (default selected), 🇺🇸 EN-US, 🇬🇧 EN-GB,
     🇪🇸 ES, 🇫🇷 FR, 🇮🇹 IT, 🇧🇷 PT, 🇮🇳 HI, 🇯🇵 JA, 🇨🇳 ZH.
   - Clicking a pill filters the voice grid to voices where v.lang === pill.code.
     "All" shows everything.
   - Show a small count badge per pill ("EN-US · 12").
   - Persist last-picked tab in localStorage.

2. **Voice grid usability**
   - Currently 38 voices in a 4-col grid is overwhelming. With language tab,
     it'll usually be 1-12 voices visible. Verify spacing still works.
   - The selected voice card should keep showing even when the active language
     tab would hide it (badge it with the lang flag). Otherwise the user picks
     Bella, switches to Mandarin to peek, and loses the selection visually.
   - Current play button is "▶". Keep that. Spinner → "⏳" while first sample
     synthesises (the first request is up to 3s; subsequent are instant).

3. **Wire the "Run auto-critic" button** on the result panel.
   - Currently it just alerts placeholder text.
   - Backend doesn't yet expose a critique endpoint — see TODO below. For now,
     make the button visually obvious that it's pending, OR hide it until a
     `/api/jobs/{id}/critique` endpoint lands. (Coordinate with backend session.)

4. **Right preview panel polish**
   - On `compose.start` (when ffmpeg starts), show the closer_panel image
     (via /api/jobs/{id}/closer) as a third intermediate state — between
     audio mode and final video mode. It's a nice "the CTA panel just
     finished baking" moment.
   - Currently transitions are abrupt. Add a subtle fade between
     idle → audio → video states (200ms opacity transition is enough).

5. **Job history sidebar / dropdown**
   - GET /api/jobs/{id} works for any past job_id you've seen this session.
     Cache job_ids in localStorage as you create them. Expose a small dropdown
     "Past jobs" with the last 10. Click → load that job's snapshot into the
     lobby (replay events from the snapshot's `events` array, jump straight to
     the result panel if the job is `done`).

6. **Tailwind production build (optional, only if 1-5 are done)**
   - The CDN warning in console is cosmetic. If you want it gone, run
     `npx tailwindcss -i input.css -o web/static/tailwind.css --minify`
     and swap the <script> tag for <link rel=stylesheet>. Keep the same
     classes the markup already uses.

WHAT NOT TO DO:

- Do not change the SSE event names or shape — they're tied to
  pipeline/parse_stdout_line in web/server.py. Backend will own that.
- Do not move state from index.html to a framework (React/Svelte/etc).
  Vanilla is intentional — matches the user's trading project pattern.
- Do not wire features that need new backend endpoints unless you also
  create a stub returning fake data so the frontend can be tested in
  isolation. Coordinate by leaving a TODO comment with the expected
  payload shape so the backend session can land it cleanly.
- Do not touch /pipeline/, make_shorts.py, pull_stories.py, channels/,
  data/, or server.py. If you find yourself wanting to, stop and write
  the request as a TODO at the bottom of HANDOFF.md instead.

VERIFY YOUR WORK:

- Hit http://127.0.0.1:8765/auth?token=$(cat /tmp/ytfactory_token) once,
  then http://127.0.0.1:8765/.
- Each language pill should filter voices and play samples. The non-English
  voices read AITA hooks IN their own language.
- Generate a job (any niche, English voice for fastest test). Confirm:
  - Stage cards animate idle→active→done as events arrive
  - Right panel shows audio at ~5s in (TTS is fast)
  - Right panel swaps to video when ffmpeg finishes (~7min total)
- Restart server (it's running on port 8765 with token from /tmp/ytfactory_token):
    pkill -f "uvicorn web.server"
    YTFACTORY_TOKEN=$(cat /tmp/ytfactory_token) \
      .venv/bin/uvicorn web.server:app --host 127.0.0.1 --port 8765 \
      --reload --reload-dir web

  --reload --reload-dir web makes uvicorn pick up your HTML/JS edits
  without manual restart. Server.py edits are out of scope for this session.

REPORT BACK with: which TODOs you completed, any new TODOs you uncovered,
and any backend endpoints you stubbed (so the backend session can pick them
up). Keep the report short — the user wants the work, not a manifesto.
```

---

## Side-channel notes (don't include in the prompt above)

- The token is at `/tmp/ytfactory_token` — frontend session needs it once for the cookie.
- If you want both sessions to coordinate without merge pain, the rule of thumb is: backend owns `web/server.py` + `pipeline/`, frontend owns `web/static/`. If the frontend needs a new endpoint, leave a TODO comment with payload shape and ping the backend session.
- The shofferai pattern (your closest production deploy) uses Next.js + Tailwind — but for ytFactory we deliberately stayed on vanilla JS (matches the trading-project pattern, no build step, fastest hand-off). Don't let the next session "upgrade" this to React/Next unless you explicitly ask for it.
