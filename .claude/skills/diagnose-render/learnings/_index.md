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

(none yet — first real invocation will append here)
