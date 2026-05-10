---
name: Sports footage cut ends at the broadcast's first natural editorial cut, not an arbitrary out_s
description: Class-of-bug — sports footage windows that overshoot into post-call replay segments (desaturated B&W, "GOAL" text graphics, slow-mo) drop viewer energy hard; cut at the FIRST natural editorial transition in the source clip
type: feedback
originSessionId: 6d719008-9aa3-4424-b0ab-164cfc9e91a1
---
The sports broadcast clips on YouTube typically have a known internal structure: live action with commentator's call → first natural editorial cut → stylized post-call replay (often desaturated, with a graphic overlay like "AGUEROOOOO!" or slow-mo). The first cut is the moment to land our footage `out_s`.

**Why:** When our footage window extends past that first editorial cut, the viewer's emotional energy peaks during the live action / call, then drops as the broadcast switches into its lower-energy stylized replay. The viewer feels "the air go out of the moment" before our cartoon resumes — even if technically the broadcast clip is still playing iconic content. Caught on v8 Aguero 93:20 render: window was 4-12s (8s); the live action ends at source ~7.8s when the broadcast switches to a B&W replay with "AGUEROOOOO" text overlay, so 3-4 seconds of low-energy footage trailed off before our cartoon resumed.

**How to apply:**
- When picking `out_s` for a sports footage cut, sample the source clip every 0.5s INSIDE the window and look for: desaturation, text graphics overlay (the broadcast's own "GOAL" / player-name supers), slow-motion, or a wide-angle camera change. The first one of these AFTER the audio peak (Tyler call etc.) is your `out_s`.
- For the iconic football moments where the broadcast mode-shifts cleanly, `out_s ≈ audio_peak + 0.5s` is usually right.
- Better: extend `pipeline/footage.py` with a `detect_natural_cut_point(source_path, in_s, out_s)` helper that runs `ffmpeg -filter:v "select='gt(scene,0.4)',showinfo"` to find scene changes. Log a warning (or auto-suggest a tighter `out_s`) when the first natural cut is >1s before the requested out_s.
- Combined with the existing audio-peak-RMS detection (PIPELINE.md "Choosing optimal footage cut-in points"), this gives both audio and video heuristics for picking the right window.
- Also test the IN-CONTINUITY: the cartoon beat AFTER the footage should match the emotional state the broadcast left us in (celebration → cartoon celebration). On v8 we cut out of celebration into an empty trophy plinth — wrong tonal hand-off.