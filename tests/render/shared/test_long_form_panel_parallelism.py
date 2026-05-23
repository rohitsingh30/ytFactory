"""Pin the 2026-05-23 long-form serial-panel-gen disaster.

Backstory: job a0aac53ea01949178afd50ac2b254ef7 (mystoriesanimated/nosleep
long-form, slug ``the-climb-a0aac53e``) hit the Cloud Run 60-minute task
timeout and was SIGKILLed mid-compose at seg 17/60. Root cause: the
short-engine image path (``pipeline/render/visualize/ai_beat_slideshow.py``)
fans out per-beat image generation via ``ThreadPoolExecutor`` against the
``cloudrun_z_image_turbo`` service (deployed with ``max-instances=4``),
but the long-form panel path (``_generate_panel_stills`` in
``pipeline/render/shared/long_form_lib.py``) was a plain serial ``for``
loop. 60 panels × ~30s each = 36 minutes of wall time spent generating
images serially while three of the four Cloud Run GPU instances sat
idle. The remaining ~25 minutes weren't enough for the rest of the
pipeline (compose dies at seg 17/60), so the entire render is lost
(₹15-20 burned, mp4 never written).

Fix: ``_generate_panel_stills`` now mirrors the short-engine fan-out
pattern — ``ThreadPoolExecutor(max_workers=ceil(N/4))`` for cloud
providers, capped at 8, overridable via ``YTFACTORY_CLOUD_IMAGE_WORKERS``.
Local providers (anything not starting with ``cloudrun_``) stay serial
because concurrent diffusion ops on M2 Max unified memory serialise +
fragment VRAM. A single ``/readyz`` prewarm before the fan-out
amortises cold-start across all parallel requests.

Class-of-bug pin: anytime the codebase has an "outer parallel" pattern
(StageOverlap, pipeline.parallel.run_parallel, ThreadPoolExecutor) for
a fan-out workload, ALL the corresponding inner loops MUST also fan out
unless there's an explicit (commented) reason for serialization. This
test fails if anyone reverts the long-form panel path to a serial loop.
"""
from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


class GeneratePanelStillsParallelTest(unittest.TestCase):
    """Pin that _generate_panel_stills fans out for cloud providers."""

    def _panels(self, n: int) -> list[dict]:
        return [{"scene": f"scene {i}", "seed_offset": i} for i in range(n)]

    def _run_with_fake_generator(
        self,
        *,
        n_panels: int,
        provider: str,
        per_call_sleep_s: float = 0.2,
    ) -> tuple[float, int]:
        """Run _generate_panel_stills with images.generate stubbed.

        Returns (wall_time_s, max_in_flight). max_in_flight reveals
        the actual parallelism — 1 = serial, >1 = parallel.
        """
        from pipeline.render.shared import long_form_lib as lib

        in_flight = 0
        max_in_flight = 0
        lock = threading.Lock()

        def fake_generate(*, out_path: Path, **_kwargs):
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                if in_flight > max_in_flight:
                    max_in_flight = in_flight
            time.sleep(per_call_sleep_s)
            out_path.write_bytes(b"\x89PNG" + b"\x00" * 8192)
            with lock:
                in_flight -= 1

        with patch("pipeline.images.generate", side_effect=fake_generate), \
             patch(
                "pipeline.images.images_cloudrun._service_url",
                return_value="https://fake.example.com",
             ), \
             patch(
                "pipeline.cloud.cloudrun_auth.get_id_token",
                return_value="fake-token",
             ), \
             patch("urllib.request.urlopen", side_effect=Exception("test: skip prewarm")):
            with TemporaryDirectory() as tmp:
                t0 = time.monotonic()
                pngs = lib._generate_panel_stills(
                    panels=self._panels(n_panels),
                    style_prefix="",
                    image_provider=provider,
                    image_seed=42,
                    image_steps=4,
                    image_width=1920,
                    image_height=1080,
                    cache_dir=Path(tmp),
                )
                wall = time.monotonic() - t0
                self.assertEqual(len(pngs), n_panels)
                for png in pngs:
                    self.assertTrue(png.exists(), f"missing: {png}")
        return wall, max_in_flight

    def test_cloud_provider_runs_panels_in_parallel(self):
        """Cloud provider with N=8 panels MUST have >1 concurrent calls.

        This is the regression pin for the 2026-05-23 disaster. Before
        the fix, max_in_flight was always 1 (serial for-loop). After
        the fix, ceil(8/4) = 2 workers, so max_in_flight should reach 2.
        """
        _, max_in_flight = self._run_with_fake_generator(
            n_panels=8,
            provider="cloudrun_z_image_turbo",
            per_call_sleep_s=0.15,
        )
        self.assertGreater(
            max_in_flight, 1,
            f"cloud panel gen is serial (max_in_flight={max_in_flight}). "
            f"_generate_panel_stills must use ThreadPoolExecutor for "
            f"cloud providers — mirrors ai_beat_slideshow.py:447 and "
            f"matches the max-instances=4 ceiling on z-image-turbo. "
            f"Bug docket: B1 in 2026-05-23 timeout investigation.",
        )

    def test_cloud_provider_respects_worker_env_override(self):
        """YTFACTORY_CLOUD_IMAGE_WORKERS=4 with N=8 → up to 4 concurrent."""
        with patch.dict(os.environ, {"YTFACTORY_CLOUD_IMAGE_WORKERS": "4"}, clear=False):
            _, max_in_flight = self._run_with_fake_generator(
                n_panels=8,
                provider="cloudrun_z_image_turbo",
                per_call_sleep_s=0.2,
            )
        self.assertGreaterEqual(
            max_in_flight, 3,
            f"override YTFACTORY_CLOUD_IMAGE_WORKERS=4 not honored "
            f"(max_in_flight={max_in_flight})",
        )

    def test_local_provider_stays_serial(self):
        """Local providers MUST stay serial — concurrent M2 Max diffusion
        ops fragment unified memory. Pin so nobody helpfully ‘fixes’ this.
        """
        _, max_in_flight = self._run_with_fake_generator(
            n_panels=4,
            provider="local_diffusers",
            per_call_sleep_s=0.05,
        )
        self.assertEqual(
            max_in_flight, 1,
            f"local provider went parallel (max_in_flight={max_in_flight}). "
            f"Local providers must remain serial — see docstring for why.",
        )

    def test_force_serial_via_env_override(self):
        """YTFACTORY_CLOUD_IMAGE_WORKERS=1 forces serial even for cloud."""
        with patch.dict(os.environ, {"YTFACTORY_CLOUD_IMAGE_WORKERS": "1"}, clear=False):
            _, max_in_flight = self._run_with_fake_generator(
                n_panels=8,
                provider="cloudrun_z_image_turbo",
                per_call_sleep_s=0.05,
            )
        self.assertEqual(
            max_in_flight, 1,
            f"YTFACTORY_CLOUD_IMAGE_WORKERS=1 not honored "
            f"(max_in_flight={max_in_flight})",
        )

    def test_already_cached_panels_are_skipped(self):
        """Cache-skip: panels already on disk → images.generate not called."""
        from pipeline.render.shared import long_form_lib as lib

        call_count = 0

        def fake_generate(*, out_path: Path, **_kwargs):
            nonlocal call_count
            call_count += 1
            out_path.write_bytes(b"\x89PNG" + b"\x00" * 8192)

        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "panels").mkdir()
            # Pre-populate panel_002.png so it must be skipped.
            (cache / "panels" / "panel_002.png").write_bytes(
                b"\x89PNG" + b"\x00" * 8192
            )

            with patch("pipeline.images.generate", side_effect=fake_generate):
                pngs = lib._generate_panel_stills(
                    panels=self._panels(4),
                    style_prefix="",
                    image_provider="local_diffusers",
                    image_seed=42,
                    image_steps=4,
                    image_width=1920,
                    image_height=1080,
                    cache_dir=cache,
                )

            self.assertEqual(len(pngs), 4)
            self.assertEqual(
                call_count, 3,
                f"expected 3 generate calls (1 cached, 3 generated), "
                f"got {call_count}",
            )

    def test_missing_scene_still_raises_value_error(self):
        """Preserve the pre-fix contract: panel with empty scene → ValueError."""
        from pipeline.render.shared import long_form_lib as lib

        panels = [
            {"scene": "ok", "seed_offset": 0},
            {"scene": "", "seed_offset": 1},
        ]
        with TemporaryDirectory() as tmp:
            with patch("pipeline.images.generate"):
                with self.assertRaises(ValueError) as cm:
                    lib._generate_panel_stills(
                        panels=panels,
                        style_prefix="",
                        image_provider="local_diffusers",
                        image_seed=42,
                        image_steps=4,
                        image_width=1920,
                        image_height=1080,
                        cache_dir=Path(tmp),
                    )
                self.assertIn("missing 'scene' field", str(cm.exception))


class PersistArtifactHookTest(unittest.TestCase):
    """B2 — every generated panel must call ``cache.persist_artifact``.

    Without this hook, the GCS persistence module exists but no panels
    ever get uploaded, and a worker SIGKILL still loses the cache. Pin
    both the parallel and serial paths.
    """

    def _panels(self, n: int) -> list[dict]:
        return [{"scene": f"scene {i}", "seed_offset": i} for i in range(n)]

    def _run(self, *, n_panels: int, provider: str
             ) -> tuple[list[Path], list[Path]]:
        """Run with a stub persist_artifact + stub images.generate.

        Returns (generated_pngs, persist_calls) where persist_calls is
        the list of paths passed to ``persist_artifact``.
        """
        from pipeline.render.shared import long_form_lib as lib

        persist_calls: list[Path] = []

        def fake_persist(path, *, kind, name=None):
            persist_calls.append(Path(path))
            self.assertEqual(kind, "panels")

        def fake_generate(*, out_path: Path, **_kwargs):
            out_path.write_bytes(b"\x89PNG" + b"\x00" * 8192)

        with patch("pipeline.images.generate", side_effect=fake_generate), \
             patch(
                "pipeline.images.images_cloudrun._service_url",
                return_value="https://fake.example.com",
             ), \
             patch(
                "pipeline.cloud.cloudrun_auth.get_id_token",
                return_value="fake-token",
             ), \
             patch("urllib.request.urlopen", side_effect=Exception("test: skip prewarm")), \
             patch("pipeline.cloud.cache.persist_artifact", side_effect=fake_persist):
            with TemporaryDirectory() as tmp:
                pngs = lib._generate_panel_stills(
                    panels=self._panels(n_panels),
                    style_prefix="",
                    image_provider=provider,
                    image_seed=42,
                    image_steps=4,
                    image_width=1920,
                    image_height=1080,
                    cache_dir=Path(tmp),
                )
                return pngs, persist_calls

    def test_parallel_path_calls_persist_for_each_panel(self) -> None:
        """Cloud provider (parallel path) — N=4 panels → 4 persist calls."""
        pngs, persist_calls = self._run(n_panels=4, provider="cloudrun_z_image_turbo")
        self.assertEqual(len(pngs), 4)
        self.assertEqual(
            len(persist_calls), 4,
            f"parallel path missing persist_artifact hook "
            f"(persist_calls={persist_calls})",
        )
        # The paths persisted must match the panels actually generated.
        self.assertEqual(
            sorted(p.name for p in persist_calls),
            sorted(p.name for p in pngs),
        )

    def test_serial_path_calls_persist_for_each_panel(self) -> None:
        """Local provider (serial path) — N=3 panels → 3 persist calls."""
        pngs, persist_calls = self._run(n_panels=3, provider="local_diffusers")
        self.assertEqual(len(pngs), 3)
        self.assertEqual(
            len(persist_calls), 3,
            f"serial path missing persist_artifact hook "
            f"(persist_calls={persist_calls})",
        )

    def test_partial_hydrate_only_persists_freshly_generated(self) -> None:
        """If hydrate landed 2 of 4 panels, only 2 fresh generates AND
        only 2 persist calls should happen.

        This pins the end-to-end B2 contract:
          hydrated panel → cache-skip → no images.generate, no persist.
          missing panel → images.generate → persist_artifact.
        """
        from pipeline.render.shared import long_form_lib as lib

        gen_calls: list[Path] = []
        persist_calls: list[Path] = []

        def fake_generate(*, out_path: Path, **_kwargs):
            gen_calls.append(out_path)
            out_path.write_bytes(b"\x89PNG" + b"\x00" * 8192)

        def fake_persist(path, *, kind, name=None):
            persist_calls.append(Path(path))

        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "panels").mkdir()
            # Simulate hydrate: panels 0 and 2 already on disk.
            for idx in (0, 2):
                (cache / "panels" / f"panel_{idx:03d}.png").write_bytes(
                    b"\x89PNG" + b"\x00" * 8192
                )

            with patch("pipeline.images.generate", side_effect=fake_generate), \
                 patch(
                    "pipeline.cloud.cache.persist_artifact",
                    side_effect=fake_persist,
                 ):
                pngs = lib._generate_panel_stills(
                    panels=self._panels(4),
                    style_prefix="",
                    image_provider="local_diffusers",
                    image_seed=42,
                    image_steps=4,
                    image_width=1920,
                    image_height=1080,
                    cache_dir=cache,
                )

            self.assertEqual(len(pngs), 4)
            # Only 2 fresh generates (the missing ones).
            self.assertEqual(
                len(gen_calls), 2,
                f"hydrate not honored: {len(gen_calls)} generate calls "
                f"({gen_calls})",
            )
            # Only 2 persist calls — hydrated panels MUST NOT re-upload
            # (we already downloaded them from GCS in the first place).
            self.assertEqual(
                len(persist_calls), 2,
                f"hydrated panels re-uploaded: persist_calls={persist_calls}",
            )
            persisted_names = sorted(p.name for p in persist_calls)
            self.assertEqual(persisted_names, ["panel_001.png", "panel_003.png"])

    def test_persist_failure_does_not_break_render(self) -> None:
        """If persist_artifact raises, the render still completes."""
        from pipeline.render.shared import long_form_lib as lib

        def fake_generate(*, out_path: Path, **_kwargs):
            out_path.write_bytes(b"\x89PNG" + b"\x00" * 8192)

        def boom(path, *, kind, name=None):
            raise RuntimeError("simulated cache failure")

        with patch("pipeline.images.generate", side_effect=fake_generate), \
             patch("pipeline.cloud.cache.persist_artifact", side_effect=boom):
            with TemporaryDirectory() as tmp:
                pngs = lib._generate_panel_stills(
                    panels=self._panels(2),
                    style_prefix="",
                    image_provider="local_diffusers",
                    image_seed=42,
                    image_steps=4,
                    image_width=1920,
                    image_height=1080,
                    cache_dir=Path(tmp),
                )
                self.assertEqual(len(pngs), 2)
                for png in pngs:
                    self.assertTrue(png.exists())


if __name__ == "__main__":
    unittest.main()
