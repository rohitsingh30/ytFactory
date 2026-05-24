"""Pin the 2026-05-24 long-form caption synthesis fix.

Bug C in
``data/critiques/i-ve-been-flying-for-almost-thirty-hours-and-the-flight-atte-845bdb0d.bugs.md``
+ finding 3 in
``.claude/skills/diagnose-render/learnings/845bdb0df20e4ba3885ca33c7749e74d.md``:

Long-form skips ASR — it uses authored TTS chunk timings or the
asr_anchors title-match path. Either way, every Segment shipped with
``words=None``. Downstream, the ``overlays.word_caption_pngs`` plugin
detected the missing word timings, fell through to per-segment PNGs,
and rendered ONE 146-second-wide PNG per section. From the viewer's
perspective, that read as a single full-width text strip across the
bottom of the frame for the entire 26-minute video.

Fix: every Segment returned by ``timeline.asr_anchors`` now carries
synthesized uniform per-word timings derived from
``Segment.text`` and ``[start_s, end_s]``. Real ASR-emitted word
timings (the short-engine ``asr_beats`` path) are preserved verbatim;
synthesis only fires when ``words is None or len(words) == 0``.

These tests pin:

A) Real ASR-emitted word timings pass through verbatim (no synthesis).
B) Long-form sections with absent word timings get per-word entries
   summing to the segment duration ±1ms.
C) The producer in ``overlays.word_caption_pngs`` emits N caption
   events for a Segment with N words (whether ASR-emitted or
   synthesized) — never 1 strip per segment.
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.beats import Word
from pipeline.render.contracts import AudioResult, Segment
from pipeline.render.spec import CaptionStyleConfig
from pipeline.render.timeline.asr_anchors import (
    AsrAnchors,
    _ensure_word_timings,
    _synthesize_word_timings,
)


def _audio(duration_s: float = 100.0, *, chunk_timings=None) -> AudioResult:
    return AudioResult(
        narration_path=Path("/tmp/narration.wav"),
        duration_s=duration_s,
        chunk_timings=chunk_timings or [],
    )


class SynthesizeWordTimingsHelperTest(unittest.TestCase):
    """Unit tests for the synthesis primitive."""

    def test_uniform_distribution_across_span(self):
        words = _synthesize_word_timings(
            "one two three four", start_s=0.0, end_s=4.0,
        )
        self.assertEqual([w.text for w in words], ["one", "two", "three", "four"])
        # Uniform spacing → 1.0s per word.
        self.assertAlmostEqual(words[0].start, 0.0)
        self.assertAlmostEqual(words[0].end, 1.0)
        self.assertAlmostEqual(words[1].start, 1.0)
        self.assertAlmostEqual(words[2].end, 3.0)
        # Last word ends exactly at end_s (not at start + n*per_word, to
        # absorb float drift over very long spans).
        self.assertAlmostEqual(words[-1].end, 4.0)

    def test_durations_sum_to_segment_span(self):
        # 7 words over a 13.7s span — sum of (end-start) must equal span.
        text = "one two three four five six seven"
        words = _synthesize_word_timings(text, start_s=10.0, end_s=23.7)
        total = sum(w.end - w.start for w in words)
        self.assertAlmostEqual(total, 13.7, places=3)

    def test_punctuation_only_tokens_are_dropped(self):
        # ASR sometimes emits "/" or "," as standalone tokens; the
        # synthesizer must not turn them into a 0.5s floating glyph.
        words = _synthesize_word_timings(
            "hello / world . foo", start_s=0.0, end_s=4.0,
        )
        texts = [w.text for w in words]
        self.assertEqual(texts, ["hello", "world", "foo"])

    def test_attached_punctuation_kept(self):
        # "1990." and "10,000" must NOT be stripped — those contain
        # alnum chars. Only PURE-punctuation tokens are dropped.
        words = _synthesize_word_timings(
            "It launched in 1990.", start_s=0.0, end_s=2.0,
        )
        self.assertIn("1990.", [w.text for w in words])

    def test_empty_text_returns_empty(self):
        self.assertEqual(_synthesize_word_timings("", 0.0, 1.0), [])
        self.assertEqual(_synthesize_word_timings("   ", 0.0, 1.0), [])

    def test_non_positive_span_returns_empty(self):
        # Defensive: a zero-duration or inverted segment can't have
        # word timings synthesized — caller must handle the empty
        # list (overlays.word_caption_pngs already does).
        self.assertEqual(_synthesize_word_timings("hi", 1.0, 1.0), [])
        self.assertEqual(_synthesize_word_timings("hi", 2.0, 1.0), [])


class EnsureWordTimingsTest(unittest.TestCase):
    """Test A — real ASR-emitted word timings pass through verbatim.
    Synthesis only fires when ``words`` is None or empty.
    """

    def test_real_asr_word_timings_preserved_verbatim(self):
        # Short-engine asr_beats path emits real Word objects. Synthesis
        # must be a no-op on those.
        real_words = [
            Word(text="hello", start=0.0, end=0.45),
            Word(text="world", start=0.45, end=1.0),
        ]
        seg = Segment(
            start_s=0.0, end_s=1.0,
            text="hello world", anchor_id="b0", kind="beat",
            words=real_words,
        )
        out = _ensure_word_timings([seg])
        self.assertIs(out[0].words, real_words,
                      "Real ASR word timings must pass through verbatim "
                      "— synthesis MUST NOT replace them.")
        # And the contents are byte-identical (defence-in-depth).
        self.assertEqual(out[0].words[0].end, 0.45)
        self.assertEqual(out[0].words[1].text, "world")

    def test_none_words_get_synthesized(self):
        seg = Segment(
            start_s=0.0, end_s=4.0,
            text="alpha beta gamma delta", anchor_id="s0", kind="section",
            words=None,
        )
        _ensure_word_timings([seg])
        self.assertIsNotNone(seg.words)
        self.assertEqual(len(seg.words), 4)
        self.assertEqual(seg.words[0].text, "alpha")
        self.assertEqual(seg.words[-1].text, "delta")

    def test_empty_words_list_gets_synthesized(self):
        seg = Segment(
            start_s=0.0, end_s=2.0,
            text="one two", anchor_id="s0", kind="section",
            words=[],
        )
        _ensure_word_timings([seg])
        self.assertEqual(len(seg.words), 2)


class LongFormSegmentsGetSynthesizedTimingsTest(unittest.TestCase):
    """Test B — long-form path: end-to-end through AsrAnchors.build,
    every returned Segment has per-word entries summing to its
    duration ±1ms. Exercise all four branches.
    """

    def _assert_segments_have_words_summing_to_span(self, result):
        for seg in result:
            self.assertIsNotNone(
                seg.words,
                f"Segment {seg.anchor_id} (text={seg.text!r}) has no "
                f"words — long-form caption overlay would render one "
                f"static PNG per ~150s section (Bug C).",
            )
            self.assertGreater(
                len(seg.words), 0,
                f"Segment {seg.anchor_id} has zero words synthesized — "
                f"text={seg.text!r}, span={seg.end_s - seg.start_s}",
            )
            span = seg.end_s - seg.start_s
            total = sum(w.end - w.start for w in seg.words)
            self.assertAlmostEqual(
                total, span, places=3,
                msg=(f"Synthesized word durations sum to {total:.3f}s but "
                     f"segment span is {span:.3f}s — drift > 1ms"),
            )

    def test_single_section_fallback_synthesizes(self):
        # Script has no sections → single-section fallback.
        script = {"narration": "One two three four five six seven eight"}
        result = AsrAnchors().build(
            spec=MagicMock(), script=script, audio=_audio(duration_s=80.0),
        )
        self._assert_segments_have_words_summing_to_span(result)
        self.assertEqual(len(result[0].words), 8)

    def test_cloud_anchored_path_synthesizes(self):
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
            {"id": "s1", "title": "T1", "text": "Hubble was a dream a hundred years."},
            {"id": "s2", "title": "T2", "text": "Construction began in 1977."},
        ]
        script = {"sections": sections}

        with patch(
            "pipeline.asr_cloudrun.align_via_cloud",
            return_value=[cloud_seg, cloud_seg2],
        ):
            result = AsrAnchors().build(
                spec=MagicMock(), script=script, audio=_audio(),
            )

        self._assert_segments_have_words_summing_to_span(result)
        # Words must derive from the AUTHORED body, not the cloud-
        # whisper-derived narration string (the body-overlay logic
        # already pinned in test_asr_anchors_section_text_fields.py).
        self.assertEqual(result[0].words[0].text, "Hubble")
        self.assertEqual(result[1].words[0].text, "Construction")

    def test_local_anchored_path_synthesizes(self):
        sections = [
            {"id": "s1", "title": "before hubble",
             "text": "Hubble was a dream a hundred years in the making."},
            {"id": "s2", "title": "building the telescope",
             "text": "Construction began in 1977 and took 13 years."},
        ]
        script = {"sections": sections}

        with patch(
            "pipeline.asr_cloudrun.align_via_cloud",
            side_effect=RuntimeError("cloud whisper unavailable"),
        ), patch(
            "pipeline.asr.transcribe",
            return_value={"segments": [{"words": [
                {"word": w, "start": float(i), "end": float(i) + 0.4}
                for i, w in enumerate(
                    "before hubble was launched in 1990 building the telescope".split()
                )
            ]}]},
        ):
            result = AsrAnchors().build(
                spec=MagicMock(), script=script, audio=_audio(duration_s=100.0),
            )

        self._assert_segments_have_words_summing_to_span(result)
        # Section 1: 10 words ("Hubble was a dream a hundred years in the making.")
        self.assertEqual(len(result[0].words), 10)

    def test_chunk_timings_fallback_synthesizes(self):
        sections = [
            {"id": "s1", "title": "T1", "text": "alpha beta gamma delta epsilon"},
            {"id": "s2", "title": "T2", "text": "one two three"},
        ]
        script = {"sections": sections}
        audio = _audio(chunk_timings=[(0.0, 50.0), (50.0, 100.0)])

        with patch(
            "pipeline.asr_cloudrun.align_via_cloud",
            side_effect=RuntimeError("cloud down"),
        ), patch(
            "pipeline.asr.transcribe",
            side_effect=RuntimeError("local down"),
        ):
            result = AsrAnchors().build(
                spec=MagicMock(), script=script, audio=audio,
            )

        self._assert_segments_have_words_summing_to_span(result)
        self.assertEqual(len(result[0].words), 5)
        self.assertEqual(len(result[1].words), 3)

    def test_equal_share_fallback_synthesizes(self):
        sections = [
            {"id": f"s{i}", "title": f"T{i}",
             "text": f"section {i} narration body words here"}
            for i in range(3)
        ]
        script = {"sections": sections}
        audio = _audio(duration_s=300.0, chunk_timings=[])

        with patch(
            "pipeline.asr_cloudrun.align_via_cloud",
            side_effect=RuntimeError("cloud down"),
        ), patch(
            "pipeline.asr.transcribe",
            side_effect=RuntimeError("local down"),
        ):
            result = AsrAnchors().build(
                spec=MagicMock(), script=script, audio=audio,
            )

        self._assert_segments_have_words_summing_to_span(result)
        for seg in result:
            # 6 words per section.
            self.assertEqual(len(seg.words), 6)


# -- Test C — producer emits N elements, not 1 -------------------------


@dataclass
class _FakeSpec:
    output_resolution: tuple[int, int] = (1080, 1920)
    caption_style: CaptionStyleConfig = field(default_factory=CaptionStyleConfig)
    captions_density: str = "standard"


class WordCaptionProducerEmitsPerWordTest(unittest.TestCase):
    """Test C — given a Segment with N words (synthesized or ASR-
    emitted), the producer emits N Dialogue events (or N PNG overlays
    when ASS isn't built). Never 1.

    This is the integration check that pins the externally-visible
    fix: pre-fix the producer received a Segment with words=None,
    fell through to per-segment rendering, and emitted ONE PNG over
    the segment's full 146s duration. Post-fix the Segment carries
    word timings (synthesized in asr_anchors), so the ASS path emits
    one Dialogue per word.
    """

    def test_segment_with_synthesized_words_emits_per_word_events(self):
        from pipeline.render.overlays.word_caption_pngs import WordCaptionPngs

        # A long-form-shaped Segment whose words got synthesized by
        # asr_anchors._ensure_word_timings.
        seg = Segment(
            start_s=0.0, end_s=4.0,
            text="alpha beta gamma delta",
            anchor_id="s0", kind="section",
            words=None,
        )
        _ensure_word_timings([seg])
        self.assertEqual(len(seg.words), 4)

        with tempfile.TemporaryDirectory() as td:
            ass_path = WordCaptionPngs()._build_word_caption_ass(
                _FakeSpec(), [seg], font_size=160, out_dir=Path(td),
            )
            body = ass_path.read_text()

        # One Dialogue line per word. Pre-fix would have been zero
        # (no word timings) → producer fell through to per-segment
        # PNG rendering instead.
        n_dialogue = body.count("\nDialogue: ")
        self.assertEqual(
            n_dialogue, 4,
            f"Expected 4 Dialogue events (one per synthesized word) but "
            f"got {n_dialogue}. ASS body:\n{body}",
        )

    def test_segment_with_real_asr_words_still_emits_per_word_events(self):
        # Sanity check — real ASR words also produce N events. This
        # pins that synthesis didn't accidentally drop the existing
        # short-engine behaviour.
        from pipeline.render.overlays.word_caption_pngs import WordCaptionPngs

        seg = Segment(
            start_s=0.0, end_s=2.0,
            text="hello world there",
            anchor_id="b0", kind="beat",
            words=[
                Word(text="hello", start=0.0, end=0.7),
                Word(text="world", start=0.7, end=1.4),
                Word(text="there", start=1.4, end=2.0),
            ],
        )
        with tempfile.TemporaryDirectory() as td:
            ass_path = WordCaptionPngs()._build_word_caption_ass(
                _FakeSpec(), [seg], font_size=160, out_dir=Path(td),
            )
            body = ass_path.read_text()
        n_dialogue = body.count("\nDialogue: ")
        self.assertEqual(n_dialogue, 3)


if __name__ == "__main__":
    unittest.main()
