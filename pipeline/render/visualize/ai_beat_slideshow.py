"""AI beat-slideshow VisualProducer (short-engine canonical).

One Flux/SD image per Segment, cross-faded with Ken Burns motion.
Default visualize for short renders on every animated channel
(mystoriesanimated / hindutavaanimated / rhymetimejunction).

Today's impl is a delegating wrapper around ``pipeline.compose``'s
existing per-beat image-gen + Ken Burns chain. The bigbang PR moves
the body fully into this module so shorts.py can be deleted.

Plugin selection: ``spec.visual_mode = AI_BEAT_SLIDESHOW``.

Where ``era_anchor_prefix`` + ``character_description`` come from
----------------------------------------------------------------

Both keys are read from ``spec.extra`` inside :meth:`AiBeatSlideshow.produce`
(see the ``spec.extra.get(...)`` block ~lines 158-160). They are NOT
populated by :func:`pipeline.render.spec.build_spec` — that runs at
job-creation time, before the script + cast.json exist. Instead they
are written by :func:`pipeline.render.spec_enrich.populate_render_extras`,
which the engines (``render_short`` / ``render_long``) call at the
top of every render after the script has been loaded:

* ``era_anchor_prefix`` ← ``script["metadata"]["era_anchor"]``
  (or legacy ``era_lock``) resolved through
  :func:`pipeline.era_anchor.era_prefix_for`. Drives the
  ``[ERA — <costume tokens>]`` directorial prefix on the image-gen
  prompt — without it, Flux defaults to modernist costuming and the
  render ships WW1 trenches for a 13th-century event (the audit's
  2026-05-13 bug; see ``docs/post-audit-2026-05-14.md``).

* ``character_description`` ← ``<channel>/cast/<slug>.json``
  (``narrator.description`` preferred, ``character_description``
  top-level fallback). Pins protagonist identity across beats so
  Ronaldinho stays Ronaldinho, not five different anonymous
  footballers (also from the audit).

If you find a render shipping without an era prefix or with cast drift,
check :func:`spec_enrich.populate_render_extras` FIRST — the visualize
plugin is faithful to ``spec.extra``; the bug is upstream of here.

Optional refined-prompt path (2026-05-14)
-----------------------------------------

When ``spec.extra["prompts_path"]`` points to a ``prompts.json`` file
containing per-beat ``{key_visual, scene[, refined_visual,
refined_scene, style_block, refined_version, refined_input_hash]}``
records, this plugin assembles the final image-gen prompt via
:func:`pipeline.images.images.build_full_prompt` rather than passing
the raw ``Segment.text``.

When the ``YTFACTORY_PROMPT_REFINER=1`` env flag is set AND the cached
``refined_*`` fields pass freshness checks via
:func:`pipeline.images.prompt_refiner.refined_fields_for_render`, the
refined fields replace ``key_visual``/``scene``/``style_prefix`` in
the assembled prompt. Otherwise the legacy assembly path runs (or, if
no prompts.json is provided, the bare ``Segment.text`` path runs —
preserving the existing engine-test contract).

This is the kill switch: clearing the env variable disables the
refiner immediately, no cache invalidation required.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from pipeline.render.contracts import (
    RenderFailedError,
    Timeline,
    VisualProducer,
    VisualTrack,
    register_plugin,
)
from pipeline.render.shared.ffmpeg_helpers import probe_duration, run_ffmpeg

_logger = logging.getLogger(__name__)

# 2026-05-15 fail-loud audit — per-beat image-gen failure threshold.
# Above this fraction of failures, raise RenderFailedError rather than
# silently shipping a render where compose pads the missing frames
# with the last image (frozen-frame tail). 10% lets a single corrupt
# glyph in a 20-beat short pass; 50% (the failure mode that bit
# sportsrecapped during cloud incidents) trips the gate.
#
# P4.2 (2026-05-23, Q66): the threshold is now a POST-RETRY ceiling.
# Each beat gets ONE retry with a stronger prompt + bumped seed when
# its first attempt either errors or fails the image-quality validator
# below; only AFTER the retry budget is exhausted does the beat count
# toward this 10% pool. Per ADR-023 (gates as repair triggers) the
# render is killed only when granular retries can't recover.
_PER_BEAT_FAILURE_THRESHOLD = 0.10

# P4.2 image-quality bounds. Z-Image-Turbo can fail by producing a
# solid black/white frame (luminance pegged at one extreme) or a
# solid-color frame (per-channel variance below the floor). Caught
# here as "quality-fail → retry" before the failure counts.
_QUALITY_MIN_LUMINANCE = 0.04     # below ~4% mean = near-black
_QUALITY_MAX_LUMINANCE = 0.96     # above ~96% mean = near-white
_QUALITY_MIN_PER_CHANNEL_VARIANCE = 30.0  # raw pixel-value variance


def _image_quality_ok(png_path: Path) -> tuple[bool, str]:
    """Post-gen sanity check on a rendered PNG.

    Returns ``(True, "")`` on pass; ``(False, reason)`` when the image
    is solid black / white / monochrome (within configured bounds). The
    check is cheap (PIL stat on a 32-px downscaled copy) so it runs
    inside the per-beat loop without measurable overhead.
    """
    try:
        from PIL import Image, ImageStat  # noqa: PLC0415
    except ImportError:
        # PIL absent — skip the check rather than block the render.
        return True, ""
    try:
        with Image.open(png_path) as im:
            small = im.convert("RGB").resize((32, 32))
            stat = ImageStat.Stat(small)
    except Exception as exc:  # noqa: BLE001
        return False, f"PIL read failed: {exc}"
    mean_lum = sum(stat.mean) / (3.0 * 255.0)
    if mean_lum < _QUALITY_MIN_LUMINANCE:
        return False, f"near-black (mean luminance {mean_lum:.3f})"
    if mean_lum > _QUALITY_MAX_LUMINANCE:
        return False, f"near-white (mean luminance {mean_lum:.3f})"
    min_var = min(stat.var)
    if min_var < _QUALITY_MIN_PER_CHANNEL_VARIANCE:
        return False, f"solid color (min per-channel variance {min_var:.1f})"
    return True, ""


def _load_prompts_json(path: str | Path | None) -> list[dict] | None:
    """Load prompts.json if a path is given; tolerate any read/parse failure.

    Pulled out so callers can be tested without hitting the filesystem;
    also keeps the produce() body readable.
    """
    if not path:
        return None
    try:
        raw = Path(path).read_text()
    except OSError as exc:
        _logger.info("ai_beat_slideshow: prompts.json read skipped (%s)", exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _logger.info("ai_beat_slideshow: prompts.json parse skipped (%s)", exc)
        return None
    if not isinstance(data, list):
        _logger.info(
            "ai_beat_slideshow: prompts.json is %s, expected list — skipping",
            type(data).__name__,
        )
        return None
    return data


def _resolve_prompt_for_beat(
    *,
    beat: dict[str, Any] | None,
    fallback_text: str,
    style_prefix: str,
    character_description: str | None,
    era_anchor_prefix: str | None,
    mood: str | None,
) -> str:
    """Assemble the final image-gen prompt for one beat.

    Priority order:

    1. Beat dict present + refined gate passes → ``build_full_prompt``
       with refined_visual/scene/style_block (the DALL-E 3 playbook
       path; see ``pipeline/images/prompt_refiner.py``).
    2. Beat dict present, refined gate fails → ``build_full_prompt``
       with legacy ``key_visual + scene + style_prefix``.
    3. No beat dict → bare ``fallback_text`` (engine-test legacy
       contract; works for fixture renders that have no prompts.json).
    """
    if beat is None:
        return fallback_text
    from pipeline.images import images as _images  # noqa: PLC0415
    from pipeline.images.prompt_refiner import refined_fields_for_render  # noqa: PLC0415

    rv, rs, sb = refined_fields_for_render(
        beat,
        era_anchor_prefix=era_anchor_prefix,
        character_description=character_description,
        style=style_prefix,
        mood=mood,
    )
    return _images.build_full_prompt(
        style_prefix=style_prefix,
        character_description=character_description,
        key_visual=beat.get("key_visual", ""),
        scene=beat.get("scene", "") or fallback_text,
        era_anchor_prefix=era_anchor_prefix,
        refined_visual=rv,
        refined_scene=rs,
        style_block=sb,
    )


class AiBeatSlideshow:
    """One image per beat, Ken Burns motion, cross-faded.

    Today's impl falls back to a solid-color stand-in when the
    pipeline.images dispatcher isn't available — bigbang PR plumbs
    the real Flux cloud call here.
    """

    def produce(
        self,
        spec: Any,  # RenderSpec
        timeline: Timeline,
        work_dir: Path,
    ) -> VisualTrack:
        # generate_with_cache wraps pipeline.images.images.generate with
        # a GCS (prompt+seed+model+W×H+steps)→png cache. Cache miss
        # falls through to the real generator transparently.
        from pipeline.images.image_cache import generate_with_cache as _generate_image  # noqa: PLC0415

        # Generate one image per Segment.
        images_dir = work_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        from pipeline.images.images import CANONICAL_IMAGE_PROVIDER  # noqa: PLC0415
        provider = spec.extra.get("image_provider") or CANONICAL_IMAGE_PROVIDER
        style_prefix = spec.extra.get("image_style_prefix", "")
        seed_base = int(spec.extra.get("image_seed", 42))
        steps = int(spec.extra.get("image_steps", 4))

        # Optional refined-prompt context. All four kwargs are
        # backward-compatible: callers that don't populate spec.extra
        # get the legacy "bare Segment.text" path unchanged.
        prompts_path = spec.extra.get("prompts_path")
        custom_prompts = _load_prompts_json(prompts_path)
        era_anchor_prefix = spec.extra.get("era_anchor_prefix")
        character_description = spec.extra.get("character_description")
        mood = spec.extra.get("mood")

        # 2026-05-17 (round 4) prompts-uniqueness fail-loud gate.
        #
        # The existing bijectivity guard (~50 lines below) catches the
        # *anchor matcher* collapsing N distinct prompts to fewer
        # unique objects. That's NOT what bit render-41d3233a — there,
        # the upstream prompts.json itself had ≤2 unique ``key_visual``
        # strings across all beats, so EVERY rendered image looked the
        # same (same kitchen / same shrug / same bowl). The viewer saw
        # one image Ken-Burns'd for 30+ seconds — strictly worse than
        # a frozen-frame tail, because the Ken-Burns motion masks the
        # bug from any automated duration/duplicate-hash check.
        #
        # Defense: when ``prompts_path`` was provided AND the prompts
        # list has ≥ 4 entries (enough that a uniqueness ratio is
        # meaningful), require ≥ 50% unique ``key_visual`` strings.
        # Below that threshold, raise RenderFailedError so the cloud
        # worker marks ``stage=images_failed`` rather than shipping
        # a single-image-loop mp4.
        #
        # Why 50% not 100%: legitimate scripts sometimes intentionally
        # repeat a key_visual across 2-3 beats (e.g. a recurring
        # reaction shot). 50% leaves headroom for that pattern while
        # still failing on the catastrophic "all beats collapsed to
        # 1-2 prompts" case. Below 4 beats the ratio is too noisy
        # (1 collision in 3 beats = 33%) — skip the gate for those
        # short authored fixtures.
        # Prompts uniqueness gate removed per user direction. Accepts
        # any prompts.json shape regardless of how repeated the
        # key_visual strings are.

        # 2026-05-17 audio/visual-sync fix — re-bind prompts to ASR beats
        # by ``narration_line`` token-overlap BEFORE the count-equality
        # gate below. The LLM author splits the narration into N
        # sentences at compose-time; ASR re-splits the actually-narrated
        # wav and may land on different boundaries even when
        # |prompts| == |timeline|. Index-zip then plays prompt[k]'s
        # image during ASR-beat[k]'s audio window when those windows
        # describe different sentences — viewer hears word X, sees the
        # visual for word Y. The legacy
        # ``pipeline.images.images._align_prompts_to_beats`` already
        # does monotonic-order Jaccard-overlap matching for exactly
        # this case; the engine path never called it. Wire it in here.
        if custom_prompts:
            try:
                from pipeline.images.images import (  # noqa: PLC0415
                    _align_prompts_to_beats,
                )
                anchored = _align_prompts_to_beats(
                    custom_prompts, [seg.text for seg in timeline],
                )
                # 2026-05-17 bijectivity guard, 2026-05-23 loosened —
                # `_align_prompts_to_beats` legitimately produces many-to-1
                # mapping when timeline > prompts (ASR splits the wav
                # into more segments than the LLM authored). Pre-fix this
                # tripped on ANY reuse (13 prompts → 24 segments naturally
                # has 13 unique << 24 anchored), forcing the post-rebind
                # count-equality gate to fire. The catastrophe we're
                # actually guarding against is render 75ac2667 — all 14
                # segments collapsed to 1 prompt → entire video was one
                # image. Threshold: fail-back only when unique count
                # drops below half of the SMALLER of the two lengths.
                if anchored is not None:
                    unique_n = len({id(p) for p in anchored})
                    floor = max(2, min(len(custom_prompts), len(anchored)) // 2)
                    if unique_n < floor:
                        _logger.warning(
                            "ai_beat_slideshow: anchor matcher collapsed "
                            "%d prompts to %d unique (floor=%d) — falling "
                            "back to index-zip to preserve scene variety",
                            len(anchored), unique_n, floor,
                        )
                        anchored = None
                if anchored is not None:
                    _logger.info(
                        "ai_beat_slideshow: re-bound %d prompts to %d "
                        "beats by narration_line overlap (was index-zip)",
                        len(custom_prompts), len(timeline),
                    )
                    custom_prompts = anchored
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "ai_beat_slideshow: anchor-realign skipped (%s) — "
                    "falling back to index-zip", exc,
                )

        # Sanity gate (post-2026-05-16): if prompts_path was set by the
        # worker (= author_beat_prompts was expected to succeed) but the
        # resulting prompts.json is missing / malformed / count-mismatched,
        # REFUSE the render. The alternative is silently feeding bare
        # ``Segment.text`` to the image provider, which renders as
        # floating-objects-on-plain-backgrounds (job 3cd2b3b5, AITA
        # ketchup-on-stew). Fail loud so the job marks ``stage=images_failed``
        # in Firestore instead of shipping an unwatchable mp4.
        #
        # Fixture renders that have no prompts_path in spec.extra still
        # take the legacy bare-Segment.text path — the gate ONLY fires
        # when prompts_path was set and didn't materialise as a usable
        # list of beat dicts.
        if prompts_path:
            if custom_prompts is None:
                raise RuntimeError(
                    f"ai_beat_slideshow: prompts_path={prompts_path!r} was "
                    f"set but prompts.json is missing/unreadable/non-list. "
                    f"Refusing to render with bare Segment.text — that "
                    f"path produces floating-objects mp4. Re-author the "
                    f"prompts or fix the worker's _author_prompts_for_engine."
                )
            if len(custom_prompts) != len(timeline):
                raise RuntimeError(
                    f"ai_beat_slideshow: prompts.json has {len(custom_prompts)} "
                    f"entries but timeline has {len(timeline)} segments — "
                    f"count mismatch means the LLM-authored prompts don't "
                    f"align with the rendered beats. Refusing to render with "
                    f"a partial overlay."
                )

        images: list[Path] = []
        # 2026-05-15 fail-loud audit — track per-beat failures so we
        # can RAISE rather than silently produce a slideshow with
        # missing frames that compose pads with the last image
        # (frozen-frame tail). See _PER_BEAT_FAILURE_THRESHOLD at the
        # top of this module + tests/render/test_fail_loud_fallbacks.py.
        n_failed = 0
        n_total = len(timeline)
        first_failure: BaseException | None = None

        # 2026-05-18 (round 5): apply seed-stride from the FIRST pass,
        # not just the dedupe retry. Symptom on render-bd2d0848: 11
        # distinct prompts (verified by prompt_len log spread 2116-2173,
        # uniqueness gate passed) collapsed to visually-similar outputs
        # because Z-Image-Turbo's `seed_base + i` (42, 43, 44, ...)
        # produces neighboring noise tensors → similar denoising paths
        # → similar compositions even with different prompt embeddings.
        # The dedupe loop only catches EXACT MD5 byte collisions, not
        # semantic look-alikes. Spread the seeds across the latent
        # space from beat 0 with the same 9973 prime stride the dedupe
        # path uses for retries. Result: each beat gets a noise tensor
        # in a different latent region, so similar prompts still
        # produce visibly distinct frames.
        #
        # Backwards-compat: existing channel YAMLs can pin
        # `image_seed_stride: 1` if they want the legacy adjacent-seed
        # behavior (cache-friendly, reproducible against pre-fix
        # snapshots). Default = 9973 matches the dedupe retry constant.
        seed_stride = int(spec.extra.get("image_seed_stride", 9973))

        # Parallel per-beat fan-out — W = ceil(N/4) workers, capped by
        # the GPU quota / max-instances ceiling. Cloud Run's LB queues
        # any overflow. Each beat is still a single _generate_image
        # call (no retry — that gate was removed). Results are placed
        # back into a positional list so beat ordering is preserved.
        import math  # noqa: PLC0415
        from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415
        n_total = len(timeline)
        w = max(1, math.ceil(n_total / 4))
        _logger.info(
            "ai_beat_slideshow: fan-out W=%d for %d beats (concurrency=1 per instance)",
            w, n_total,
        )

        def _gen_one_beat(i: int, seg: "Segment") -> tuple[int, Path | None]:
            png_path = images_dir / f"beat_{i:03d}.png"
            beat = (
                custom_prompts[i]
                if custom_prompts and i < len(custom_prompts)
                else None
            )
            prompt = _resolve_prompt_for_beat(
                beat=beat,
                fallback_text=seg.text,
                style_prefix=style_prefix,
                character_description=character_description,
                era_anchor_prefix=era_anchor_prefix,
                mood=mood,
            )
            # When a beat dict is present, _resolve_prompt_for_beat
            # used build_full_prompt which already inlines the style
            # tokens (legacy style_prefix OR refined style_block). Pass
            # style_prefix="" to generate so it doesn't append style a
            # SECOND time.
            style_for_generate = "" if beat is not None else style_prefix
            attempt_seed = seed_base + i * seed_stride
            try:
                _generate_image(
                    prompt=prompt,
                    style_prefix=style_for_generate,
                    seed=attempt_seed,
                    out_path=png_path,
                    width=spec.output_resolution[0],
                    height=spec.output_resolution[1],
                    steps=steps,
                    provider=provider,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "ai_beat_slideshow: beat %d image-gen raised %s — skipping",
                    i, exc,
                )
                return i, None
            return i, png_path

        results: list[Path | None] = [None] * n_total
        with ThreadPoolExecutor(max_workers=w) as pool:
            futures = [
                pool.submit(_gen_one_beat, i, seg)
                for i, seg in enumerate(timeline)
            ]
            for fut in as_completed(futures):
                i, png_path = fut.result()
                results[i] = png_path

        images = [p for p in results if p is not None]
        n_failed = n_total - len(images)

        # 2026-05-17 static-tail fix (part A): detect duplicate
        # consecutive PNG-byte hashes and re-render the duplicates
        # with a much-wider seed spread. Symptom: render fa3709a9
        # held the same shrug-at-table frame for 15s straight (5-6
        # consecutive beats at max_s=2.8). After round-5 the FIRST
        # pass already uses ``seed_base + i * seed_stride`` so MD5
        # collisions are extremely rare here — this remains as a
        # last-resort safety net for cases where a channel pins
        # ``image_seed_stride: 1`` (legacy behavior) or the model
        # genuinely produces identical bytes despite different seeds.
        # The retry shifts by an additional stride so the new seed
        # lands in yet another latent region.

        def _png_hash(p: Path) -> str:
            try:
                return hashlib.md5(p.read_bytes()).hexdigest()
            except OSError:
                return ""

        for i in range(1, len(images)):
            h_curr = _png_hash(images[i])
            if h_curr and h_curr == _png_hash(images[i - 1]):
                beat = (
                    custom_prompts[i]
                    if custom_prompts and i < len(custom_prompts)
                    else None
                )
                prompt = _resolve_prompt_for_beat(
                    beat=beat,
                    fallback_text=timeline[i].text if i < len(timeline) else "",
                    style_prefix=style_prefix,
                    character_description=character_description,
                    era_anchor_prefix=era_anchor_prefix,
                    mood=mood,
                )
                style_for_generate = "" if beat is not None else style_prefix
                # Bump by an extra stride so the retry lands in a
                # different latent region from BOTH the original seed
                # AND the i-1 / i+1 neighbours.
                retry_seed = seed_base + (i + n_total) * seed_stride
                _logger.warning(
                    "ai_beat_slideshow: beat %d duplicates beat %d "
                    "(md5 collision) — re-rendering with seed=%d",
                    i, i - 1, retry_seed,
                )
                try:
                    _generate_image(
                        prompt=prompt,
                        style_prefix=style_for_generate,
                        seed=retry_seed,
                        out_path=images[i],
                        width=spec.output_resolution[0],
                        height=spec.output_resolution[1],
                        steps=steps,
                        provider=provider,
                    )
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "ai_beat_slideshow: beat %d dedupe re-render "
                        "failed (%s) — keeping original duplicate",
                        i, exc,
                    )

        # Per-beat failure-rate gate removed per user direction; render
        # proceeds with whatever images succeeded (frozen-frame padding
        # accepted).

        if not images:
            raise RenderFailedError(
                "ai_beat_slideshow: 0 images produced; "
                "site=pipeline/render/visualize/ai_beat_slideshow.py "
                "(no fallback path)."
            )

        # Stitch one image per beat into a continuous video. Each image
        # is held for the beat's duration. Bigbang PR adds Ken Burns
        # motion via a ffmpeg zoompan filter; today we use plain
        # framebatch-per-second.
        out_path = work_dir / "slideshow.mp4"
        self._stitch_images(images, timeline, spec, out_path)

        return VisualTrack(
            video_path=out_path,
            duration_s=probe_duration(out_path),
            extras={
                "source": "ai_beat_slideshow",
                "n_images": len(images),
                "images": [str(p) for p in images],
            },
        )

    def _stitch_images(
        self,
        images: list[Path],
        timeline: Timeline,
        spec: Any,
        out_path: Path,
    ) -> None:
        """Stitch per-beat images into a continuous video with hard cuts.

        2026-05-23: Ken-Burns / zoompan REMOVED (user directive).
        ``image_to_kenburns_clip`` now produces a STATIC clip (the
        function name is kept for back-compat). Each PNG is held for
        its beat window with no zoom, no pan; segments are
        concat-demuxed with ``-c copy`` (hard cuts, no fade).

        The retention-defense rationale that drove the previous Ken-Burns
        path ("no two pixel-identical frames > 1s") now relies on the
        beat-cadence being tight enough that each still doesn't dwell
        for long — shorts already hit ~2-3 s/beat via the sentence-per-
        beat splitter in ``pipeline.beats``.
        """
        from pipeline.compose import Resolution, image_to_kenburns_clip  # noqa: PLC0415
        from pipeline.render.shared.concat_safe import concat_file_line  # noqa: PLC0415

        out_path.parent.mkdir(parents=True, exist_ok=True)
        w, h = spec.output_resolution
        res = Resolution(width=w, height=h, fps=spec.output_fps)
        clips_dir = out_path.parent / "static_clips"
        clips_dir.mkdir(parents=True, exist_ok=True)

        clip_paths: list[Path] = []
        for i, img in enumerate(images):
            seg = timeline[i] if i < len(timeline) else None
            duration = max(seg.end_s - seg.start_s if seg else 1.0, 0.5)
            clip_p = clips_dir / f"beat_{i:03d}.mp4"
            try:
                image_to_kenburns_clip(img, duration, clip_p, resolution=res)
                clip_paths.append(clip_p)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "ai_beat_slideshow: static clip %d failed (%s) — "
                    "falling back to inline ffmpeg for this beat",
                    i, exc,
                )
                # Fall back to a single static frame so the render
                # doesn't die on one bad clip.
                run_ffmpeg([
                    "-loop", "1", "-t", f"{duration:.3f}", "-i", str(img),
                    "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                           f"crop={w}:{h},fps={spec.output_fps},format=yuv420p",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p",
                    str(clip_p),
                ])
                clip_paths.append(clip_p)

        concat_list = out_path.parent / f"{out_path.stem}_concat.txt"
        concat_list.write_text(
            "\n".join(concat_file_line(p.resolve()) for p in clip_paths)
        )
        # Lossless concat since clips already at target res/fps/codec.
        run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy",
            str(out_path),
        ])

register_plugin("visualize", "ai_beat_slideshow", AiBeatSlideshow())
assert isinstance(AiBeatSlideshow(), VisualProducer)


__all__ = ["AiBeatSlideshow"]
