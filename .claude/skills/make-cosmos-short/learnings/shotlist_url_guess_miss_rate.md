---
name: Shotlist Wikimedia URL guesses have a ~60% miss rate — class-of-bug
description: When /make-cosmos-decoder authors a shotlist, the SKILL.md prompts the model to write `source_url: https://commons.wikimedia.org/wiki/File:<guess>.jpg` per beat. ~60% of those guesses don't resolve (e.g. `Atacama_Pathfinder_Experiment_(APEX).jpg`, `Locations_of_EHT_array_telescopes.png`, `Helium-Filled_Hard_Drive.jpg` don't exist; the real File: pages have different naming conventions). The skill emits cleanly but the prep tool then prints a wall of MANUAL fallbacks, defeating the auto-prep contract.
type: feedback
---

# Shotlist Wikimedia URL guesses have a ~60% miss rate

**Rule:** when authoring a `cosmosdecoded/shotlist/<slug>.json`, do NOT pattern-guess Wikimedia File: URLs. Either (a) verify each File: page via the Wikimedia API before emit OR (b) leave the URL field as a `_search_query` string and let `pipeline/cosmos_footage_prep.py` resolve it via Wikimedia's `srsearch` endpoint at fetch time.

**Why:** the 2026-05-07 first-run on `eht-2019-m87` emitted 56 long-form clips. Of those, 35 (62.5%) returned MANUAL fallback because the guessed File: name didn't exist on Commons — the real photos exist under names like `ALMA_and_a_Starry_Night.jpg` instead of the obvious `Atacama_Large_Millimeter-submillimeter_Array.jpg`, `Gran_Telescopio_Milimétrico_3.jpg` instead of `Large_Millimeter_Telescope.jpg`. Only after I patched 6 URLs against the actual Wikimedia search-API hits did the fetched count rise from 0 to 5. Eddington 1919 had the same class-of-bug at lower volume (the auto_source_prep.md learning already noted it: "A handful of Wikimedia URLs were authored by pattern-guess; some won't resolve and need a real File:-page lookup."). EHT made the issue impossible to ignore — 35 MANUAL entries is not a "handful."

**How to apply:**

1. **Skill-side fix (preferred — SKILL.md update):** when /make-cosmos-decoder authors the shotlist, every Wikimedia URL guess MUST be accompanied by a `_wikimedia_search_query` field (e.g., `"_wikimedia_search_query": "ALMA Atacama starry night"`). The prep tool reads this on MANUAL fallback and runs `srsearch&srnamespace=6`, returning the top 2 File: candidates. Curator picks one and re-runs.

2. **Pipeline-side fix (requires `pipeline/cosmos_footage_prep.py` extension):** add a `--suggest-fallback` mode that, for every entry that returned MANUAL, runs the search query against the Wikimedia API (`https://commons.wikimedia.org/w/api.php?action=query&list=search&srsearch=<q>&srnamespace=6&srlimit=3`) and prints the candidates inline with the manual-fallback message. Saves the curator a manual web-search per entry.

3. **Author-discipline fix (no code change):** when authoring a long-form shotlist, do not invent File: URLs. Cap the guess effort at one explicit Wikimedia search per beat (via WebSearch), confirm the filename, then write it. Yes, this is slower; the alternative is that 35 of 56 clips are MANUAL after emit.

**Mitigation already shipped on 2026-05-07:** patched 6 URLs in `cosmosdecoded/shotlist/eht-2019-m87.json` from verified Wikimedia search results — the long-form fetched count went from 0/56 to 5/56. Remaining 35 MANUAL entries documented in this run's report; user/curator will substitute or supply manually.

**Escalation rule (per skill Section 9):** if this class fires again on the next /make-cosmos-decoder invocation (next slug after EHT), implement fix (1) or (2) above as a pre-emit step before the next run. Two strikes = mandatory automation.

---

**Project-doc mirror:** `cosmosdecoded/learnings/shotlist_url_guess_miss_rate.md` (this file's twin per CLAUDE.md dual-save rule).
