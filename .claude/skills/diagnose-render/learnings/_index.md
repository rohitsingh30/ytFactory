# /diagnose-render — learnings index

One line per invocation. Format:

```
- <iso8601> <job_id> (<slug>) — <n findings>, <new-frag-IDs>, <new-O-IDs>, notes
```

Per-job detail lives at `learnings/<job_id>.md`.

---

## Bootstrap

The skill was authored 2026-05-23 with job
`76508d12a4a84e638fb44a45294711d3` (MyStoriesAnimated TIFU) as the
canonical worked example. Findings on that job included:

- **F24** — Dockerfile-COPY drift detected indirectly via body-capture
  absence on `tts.server` events before render-worker fix shipped.
- **F2** + memory `project_z_image_turbo_verb_led_prompts.md` —
  prompt monotony. 7 panels, ~85% character-for-character identical
  in `final_prompt`. Verb-led `key_visual` buried at char 800+ of
  ~2,016-char prompt. Negatives stuffed into positive prompt;
  `negative_prompt` field empty.
- **O8** flagged as the structural fix (Z-Image-Turbo refiner
  rewrite: 100-200 word structured prompt per beat, 8-section
  template, positive-only framing, verb-led first).

This bootstrap is here so the first real `/diagnose-render` run on
this repo starts with one prior data point and doesn't have to
re-derive the pattern.

---

## Runs

- 2026-05-24T15:55Z `39d1ec2ddb9141c199b2b2719c867864` (mystoriesanimated/tifu, tifu-by-reporting-a-pervert-...-39d1ec2d) — 8 findings, **NEW-FRAG F32 candidate** (shorts author_gate silently discards refined_count=0/16 with empty reason), **NEW-O O41 candidate** (author_gate hard-fail + reason emission). Confirms third instance of [[verify-refiner-with-fallback-count]] pattern (after 88d98126 + 845bdb0d). The fdc5f65 long-form fix doesn't reach shorts; the shorts path silently ships through legacy `build_full_prompt(character_description, …)`.
