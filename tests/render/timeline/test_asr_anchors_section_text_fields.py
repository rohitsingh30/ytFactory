"""Pin the 2026-05-15 cosmos hubble solid-color regression.

cosmosdecoded long-form scripts (and several historyrecapped
variants) populate section bodies in a ``text`` field — NOT ``body``
or ``narration``. Pre-fix, ``asr_anchors`` only read the latter two,
so every Segment's ``text`` came back as the empty string. The
downstream ``longform_panels`` plugin then emitted panel dicts with
``"scene": ""``, which made ``_generate_panel_stills`` raise
``ValueError("panel 0 missing 'scene' field")`` → the
``except Exception`` in ``LongformPanels.produce`` caught it →
solid-color fallback for the entire 26-minute render.

Surfaced by job f37bb01a ("The Hubble Space Telescope: Thirty Years
of Looking Deeper Than Any Eye Ever Has").

Fix: ``asr_anchors`` now resolves section bodies via
``sec.get("body") or sec.get("text") or sec.get("narration") or ""``
at all four sites (cloud-aligned, local-aligned, chunk_timings
fallback, equal-share fallback). These tests pin every site against
the regression.

Belt-and-braces partner: ``LongformPanels._panels_from_timeline``
also derives a non-empty scene fallback when ``seg.text`` is empty,
so any future schema drift can't reintroduce the same crash. That
half is pinned in ``tests/render/visualize/test_long_form_fallback.py``.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.render.contracts import AudioResult, Segment
from pipeline.render.timeline.asr_anchors import AsrAnchors


def _audio(duration_s: float = 100.0, *, chunk_timings=None) -> AudioResult:
    return AudioResult(
        narration_path=Path("/tmp/narration.wav"),
        duration_s=duration_s,
        chunk_timings=chunk_timings or [],
    )


class CloudAlignedTextAliasTest(unittest.TestCase):
    """When the cloud whisper service IS up, asr_anchors uses the
    cloud-returned segments and overlays sec body. Pin the alias here."""

    def test_cloud_path_prefers_body_then_text_then_narration(self):
        # Cloud returns segments with whisper-derived text.
        cloud_seg = Segment(
            start_s=0.0, end_s=50.0,
            text="cloud-whisper-derived narration",
            anchor_id="ignored", kind="section",
        )
        cloud_seg2 = Segment(
            start_s=50.0, end_s=100.0,
            text="second segment from whisper",
            anchor_id="ignored", kind="section",
        )

        sections = [
            # Section 1 has only ``text`` (cosmos hubble shape) —
            # MUST overlay cloud_seg.text with this.
            {"id": "s1", "title": "Before Hubble", "text": "Authored text body 1"},
            # Section 2 has ``body`` (legacy mystoriesanimated shape) —
            # body wins over a hypothetical text alias.
            {"id": "s2", "title": "Launch", "body": "Body wins", "text": "Loser"},
        ]
        script = {"sections": sections}

        with patch(
            "pipeline.asr_cloudrun.align_via_cloud",
            return_value=[cloud_seg, cloud_seg2],
        ):
            result = AsrAnchors().build(spec=MagicMock(), script=script,
                                        audio=_audio())

        self.assertEqual(result[0].text, "Authored text body 1",
                         "section.text alias must overlay cloud-whisper "
                         "segment.text — pre-fix this stayed empty/wrong.")
        self.assertEqual(result[1].text, "Body wins",
                         "section.body must take precedence over .text.")
        self.assertEqual(result[0].anchor_id, "s1")
        self.assertEqual(result[1].anchor_id, "s2")

    def test_cloud_path_falls_through_to_cloud_text_when_no_aliases(self):
        cloud_seg = Segment(
            start_s=0.0, end_s=50.0,
            text="cloud-whisper-derived narration",
            anchor_id="ignored", kind="section",
        )
        # Section has neither body, text, nor narration — only title.
        sections = [{"id": "s1", "title": "Title only"}]
        script = {"sections": sections}

        with patch(
            "pipeline.asr_cloudrun.align_via_cloud",
            return_value=[cloud_seg],
        ):
            result = AsrAnchors().build(spec=MagicMock(), script=script,
                                        audio=_audio())

        self.assertEqual(result[0].text, "cloud-whisper-derived narration",
                         "Without any authored alias the cloud-derived "
                         "segment text remains intact.")


class LocalAnchoredTextAliasTest(unittest.TestCase):
    """When cloud whisper is unavailable but local whisper succeeds.
    This is the path that tripped the cosmos hubble render."""

    def _patch_cloud_unavailable(self):
        return patch(
            "pipeline.asr_cloudrun.align_via_cloud",
            side_effect=RuntimeError("cloud whisper unavailable"),
        )

    def _fake_transcribe_for_hubble(self, **_):
        # Word-level transcription that contains the section titles
        # so _find_anchor_start can match them. The *titles* are read
        # from the script (we only need them to align — body text is
        # taken from the script itself).
        return {
            "segments": [
                {"words": [
                    {"word": w, "start": float(i), "end": float(i) + 0.4}
                    for i, w in enumerate(
                        "before hubble the century-long dream "
                        "building the most ambitious telescope".split()
                    )
                ]},
            ],
        }

    def test_local_anchored_path_picks_up_text_alias(self):
        sections = [
            {"id": "s1", "title": "Before Hubble: The Century-Long Dream",
             "text": "Hubble was a dream a hundred years in the making."},
            {"id": "s2", "title": "Building the Most Ambitious Telescope",
             "text": "Construction began in 1977 and took 13 years."},
        ]
        script = {"sections": sections}

        with self._patch_cloud_unavailable(), patch(
            "pipeline.asr.transcribe",
            side_effect=self._fake_transcribe_for_hubble,
        ):
            result = AsrAnchors().build(spec=MagicMock(), script=script,
                                        audio=_audio())

        # Both segments must carry the text-field body — pre-fix they
        # came back as "" and longform_panels then crashed on scene="".
        self.assertEqual(len(result), 2)
        self.assertEqual(
            result[0].text,
            "Hubble was a dream a hundred years in the making.",
            "Section's ``text`` alias must populate Segment.text in "
            "the local-anchored fallback path. Pre-fix this was empty "
            "and longform_panels emitted scene=\"\" → 26-min black render.",
        )
        self.assertEqual(
            result[1].text,
            "Construction began in 1977 and took 13 years.",
        )

    def test_local_anchored_path_body_still_wins_over_text(self):
        sections = [
            {"id": "s1", "title": "Before Hubble: The Century-Long Dream",
             "body": "BODY wins.", "text": "TEXT loses."},
        ]
        script = {"sections": sections}
        with self._patch_cloud_unavailable(), patch(
            "pipeline.asr.transcribe",
            side_effect=self._fake_transcribe_for_hubble,
        ):
            result = AsrAnchors().build(spec=MagicMock(), script=script,
                                        audio=_audio())
        self.assertEqual(result[0].text, "BODY wins.")

    def test_local_anchored_path_narration_picked_up_when_no_body_or_text(self):
        sections = [
            {"id": "s1", "title": "Before Hubble: The Century-Long Dream",
             "narration": "Narration field used."},
        ]
        script = {"sections": sections}
        with self._patch_cloud_unavailable(), patch(
            "pipeline.asr.transcribe",
            side_effect=self._fake_transcribe_for_hubble,
        ):
            result = AsrAnchors().build(spec=MagicMock(), script=script,
                                        audio=_audio())
        self.assertEqual(result[0].text, "Narration field used.")


class FallbackChunkTimingsTextAliasTest(unittest.TestCase):
    """When BOTH cloud and local whisper are down, asr_anchors falls
    back to chunk_timings (if matching) or equal-share. Pin the alias
    on both fallback branches."""

    def test_chunk_timings_fallback_picks_up_text_alias(self):
        sections = [
            {"id": "s1", "title": "T1", "text": "Section 1 text alias body."},
            {"id": "s2", "title": "T2", "text": "Section 2 text alias body."},
        ]
        script = {"sections": sections}
        audio = _audio(chunk_timings=[(0.0, 50.0), (50.0, 100.0)])

        # Both whisper paths down → fall through to chunk_timings.
        with patch(
            "pipeline.asr_cloudrun.align_via_cloud",
            side_effect=RuntimeError("cloud down"),
        ), patch(
            "pipeline.asr.transcribe",
            side_effect=RuntimeError("local down"),
        ):
            result = AsrAnchors().build(spec=MagicMock(), script=script,
                                        audio=audio)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].text, "Section 1 text alias body.")
        self.assertEqual(result[1].text, "Section 2 text alias body.")
        # Timings come from chunk_timings.
        self.assertEqual(result[0].start_s, 0.0)
        self.assertEqual(result[0].end_s, 50.0)
        self.assertEqual(result[1].start_s, 50.0)
        self.assertEqual(result[1].end_s, 100.0)

    def test_equal_share_fallback_picks_up_text_alias(self):
        sections = [
            {"id": "s1", "title": "T1", "text": "Sec 1 from text alias."},
            {"id": "s2", "title": "T2", "text": "Sec 2 from text alias."},
            {"id": "s3", "title": "T3", "text": "Sec 3 from text alias."},
        ]
        script = {"sections": sections}
        # No chunk_timings → equal-share branch.
        audio = _audio(duration_s=300.0, chunk_timings=[])

        with patch(
            "pipeline.asr_cloudrun.align_via_cloud",
            side_effect=RuntimeError("cloud down"),
        ), patch(
            "pipeline.asr.transcribe",
            side_effect=RuntimeError("local down"),
        ):
            result = AsrAnchors().build(spec=MagicMock(), script=script,
                                        audio=audio)

        self.assertEqual(len(result), 3)
        for i, seg in enumerate(result):
            self.assertEqual(
                seg.text, f"Sec {i + 1} from text alias.",
                f"Equal-share fallback for section {i} must pick up "
                f"the ``text`` alias — pre-fix this was empty and the "
                f"long-form rendered solid color.",
            )
        # Verify equal-share timings
        self.assertAlmostEqual(result[0].start_s, 0.0)
        self.assertAlmostEqual(result[0].end_s, 100.0)
        self.assertAlmostEqual(result[2].end_s, 300.0)


class ChaptersAliasTest(unittest.TestCase):
    """Same alias rules apply when the script uses ``chapters`` instead
    of ``sections`` (some sports/cosmos variants do)."""

    def test_chapters_use_text_alias_on_local_path(self):
        chapters = [{
            "id": "c1",
            "title": "Chapter the first",
            "text": "Chapter body via text alias.",
        }]
        script = {"chapters": chapters}

        with patch(
            "pipeline.asr_cloudrun.align_via_cloud",
            side_effect=RuntimeError("cloud down"),
        ), patch(
            "pipeline.asr.transcribe",
            return_value={"segments": [{"words": [
                {"word": w, "start": float(i), "end": float(i) + 0.4}
                for i, w in enumerate("chapter the first".split())
            ]}]},
        ):
            result = AsrAnchors().build(spec=MagicMock(), script=script,
                                        audio=_audio())

        self.assertEqual(result[0].kind, "chapter")
        self.assertEqual(result[0].text, "Chapter body via text alias.")


class AnchoredSegmentsAreNonOverlappingTest(unittest.TestCase):
    """v16 — pin the 2-pass start/end refactor.

    Pre-v16, when section titles couldn't be matched in narration
    (the common case — TTS paraphrases titles), every segment's
    ``end_s`` defaulted to ``total_s`` and only narrowed if a LATER
    section's anchor was found. With NO later anchors → every segment
    overlapped → ``hold_s = end_s - start_s`` produced massive
    1400+ s holds for sub-30s sections → ffmpeg rendered 42,456 frames
    per panel → cloud-run JOB hit its 1-hour wall and was killed.

    Surfaced by Pompeii long-form (job ba3e7578) on the v15 deploy
    after asr_anchors started successfully populating segment text
    (which previously short-circuited to solid color via ValueError).
    """

    def _patch_cloud_unavailable(self):
        return patch(
            "pipeline.asr_cloudrun.align_via_cloud",
            side_effect=RuntimeError("cloud whisper unavailable"),
        )

    def test_no_anchors_match_produces_non_overlapping_segments(self):
        # Worst case from production: 10 sections, no titles match,
        # narration is 1500s. Pre-fix ALL 10 had end_s=1500 →
        # hold_s = 1500, 1350, 1200, ... → 1500+1350+...+150 = 8250s
        # of "video" for 1500s of narration. Post-fix end_s[i] =
        # start_s[i+1] = (i+1)/10 * 1500 = clean 150s per section.
        sections = [
            {"id": f"s{i}", "title": f"Title {i} that wont match",
             "text": f"Body {i}"}
            for i in range(10)
        ]
        script = {"sections": sections}

        with self._patch_cloud_unavailable(), patch(
            "pipeline.asr.transcribe",
            return_value={"segments": [{"words": [
                {"word": w, "start": float(i), "end": float(i) + 0.4}
                for i, w in enumerate("completely unrelated narration words".split())
            ]}]},
        ):
            result = AsrAnchors().build(spec=MagicMock(), script=script,
                                        audio=_audio(duration_s=1500.0))

        self.assertEqual(len(result), 10)
        # Every segment's end MUST equal the next segment's start.
        for i in range(len(result) - 1):
            self.assertEqual(
                result[i].end_s, result[i + 1].start_s,
                f"segments {i}→{i+1} overlap: end_s={result[i].end_s}, "
                f"next start_s={result[i + 1].start_s}",
            )
        # Last segment ends at total_s.
        self.assertEqual(result[-1].end_s, 1500.0)
        # First segment starts at 0.
        self.assertEqual(result[0].start_s, 0.0)
        # Sum of (end - start) MUST equal total (no double-counting,
        # no missing time).
        total_hold = sum(s.end_s - s.start_s for s in result)
        self.assertAlmostEqual(total_hold, 1500.0, places=3,
                               msg=f"sum of holds {total_hold} != narration 1500.0")
        # No individual segment should claim more than ~total/n + a
        # tolerance. Pre-fix segment 0 claimed total_s = 1500.0.
        for i, s in enumerate(result):
            self.assertLess(
                s.end_s - s.start_s, 200.0,
                f"segment {i} hold={s.end_s - s.start_s}s exceeds "
                f"sane equal-share max (~150s for 10/1500)",
            )

    def test_partial_anchors_match_still_non_overlapping(self):
        # Mix: section 0 anchors, sections 1-8 don't, section 9
        # anchors. Pre-fix sections 1-8 all had end_s=total_s.
        sections = [
            {"id": "s0", "title": "before hubble", "text": "B0"},
            {"id": "s1", "title": "no match here", "text": "B1"},
            {"id": "s2", "title": "no match either", "text": "B2"},
            {"id": "s3", "title": "the most ambitious telescope", "text": "B3"},
        ]
        script = {"sections": sections}

        # Words contain titles 0 and 3 verbatim.
        words = [
            {"word": w, "start": float(i * 10), "end": float(i * 10) + 0.4}
            for i, w in enumerate(
                "before hubble was launched in 1990 the most ambitious telescope".split()
            )
        ]
        with self._patch_cloud_unavailable(), patch(
            "pipeline.asr.transcribe",
            return_value={"segments": [{"words": words}]},
        ):
            result = AsrAnchors().build(spec=MagicMock(), script=script,
                                        audio=_audio(duration_s=200.0))

        self.assertEqual(len(result), 4)
        # Non-overlapping always.
        for i in range(len(result) - 1):
            self.assertEqual(result[i].end_s, result[i + 1].start_s)
        # Last ends at total.
        self.assertEqual(result[-1].end_s, 200.0)
        # First section IS anchored (start_s=0, the position of "before").
        self.assertEqual(result[0].start_s, 0.0)
        # Sum equals total.
        self.assertAlmostEqual(
            sum(s.end_s - s.start_s for s in result), 200.0, places=3,
        )

    def test_starts_are_monotonically_increasing(self):
        # Defensive: even if equal-share fallback would compute a
        # start earlier than the previous section's start (which can
        # happen if section i is anchored very late), monotonicity
        # is enforced.
        sections = [
            {"id": "s0", "title": "anchored late", "text": "B0"},
            {"id": "s1", "title": "no match", "text": "B1"},
            {"id": "s2", "title": "no match either", "text": "B2"},
        ]
        script = {"sections": sections}
        # "anchored late" appears at word offset 50s.
        words = [
            {"word": w, "start": float(50 + i), "end": float(50 + i) + 0.4}
            for i, w in enumerate("anchored late and stays late".split())
        ]
        with self._patch_cloud_unavailable(), patch(
            "pipeline.asr.transcribe",
            return_value={"segments": [{"words": words}]},
        ):
            result = AsrAnchors().build(spec=MagicMock(), script=script,
                                        audio=_audio(duration_s=100.0))

        # s0 anchored at 50; s1's equal-share would be 33s (1/3 * 100)
        # < 50 → must be clamped to 50. s2's equal-share would be 66
        # > prev s1, so 66.
        self.assertGreaterEqual(result[1].start_s, result[0].start_s)
        self.assertGreaterEqual(result[2].start_s, result[1].start_s)
        # And non-overlapping
        for i in range(len(result) - 1):
            self.assertEqual(result[i].end_s, result[i + 1].start_s)


if __name__ == "__main__":
    unittest.main()
