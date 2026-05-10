"""100% coverage for control/routes/voices_routes.py."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from fastapi import FastAPI

from tests._helpers import PROJECT_ROOT
import control.routes.voices_routes as voices_mod
from control.routes.voices_routes import VoiceInfo, router


SCRATCH = PROJECT_ROOT / "tests" / "_scratch_routes_voices"


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


class _FakeUpload:
    def __init__(self, filename: str | None, chunks: list[bytes]) -> None:
        self.filename = filename
        self._chunks = list(chunks)

    async def read(self, _n: int = -1) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _NamedTemp:
    def __init__(self, suffix: str = "", delete: bool = False) -> None:  # noqa: ARG002
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.path = SCRATCH / f"upload{suffix or '.bin'}"
        self.name = str(self.path)
        self._fh = None

    def __enter__(self):
        self._fh = self.path.open("wb")
        return self._fh

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self._fh:
            self._fh.close()


class VoicesBase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(SCRATCH, ignore_errors=True)
        (SCRATCH / "refs").mkdir(parents=True)
        (SCRATCH / "samples").mkdir(parents=True)
        self.refs = SCRATCH / "refs"
        self.clones = self.refs / "clones"
        self.catalog = self.refs / "catalog.yaml"
        self.samples = SCRATCH / "samples"
        self.patchers = [
            patch.object(voices_mod, "VOICE_REFS_DIR", self.refs),
            patch.object(voices_mod, "CLONES_DIR", self.clones),
            patch.object(voices_mod, "CATALOG_PATH", self.catalog),
            patch.object(voices_mod, "KOKORO_SAMPLES_DIR", self.samples),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patchers):
            p.stop()
        shutil.rmtree(SCRATCH, ignore_errors=True)


class VoiceHelperTest(VoicesBase):
    def test_famous_templates_and_closest_voice_helpers(self) -> None:
        with patch.object(voices_mod, "_voice_url", return_value="/sample.wav") as vu:
            self.assertEqual(voices_mod._closest_voice_url("morgan-freeman"), "/sample.wav")
            vu.assert_called()
        self.assertIsNone(voices_mod._closest_voice_url("not-famous"))

    def test_slug_gender_and_tones(self) -> None:
        self.assertEqual(voices_mod._slugify("  My!! Voice__Name  "), "my-voice-name")
        self.assertEqual(voices_mod._slugify("!!!"), "voice")
        self.assertEqual(voices_mod._gender_from_register("Warm female narrator"), "female")
        self.assertEqual(voices_mod._gender_from_register("Deep MALE narrator"), "male")
        self.assertEqual(voices_mod._gender_from_register("neutral"), "")
        tones = voices_mod._derive_tones("deep warm BBC", "Indian narrator")
        self.assertIn("deep", tones)
        self.assertIn("warm", tones)
        self.assertIn("british", tones)
        self.assertIn("indian", tones)

    def test_voice_preview_ref_and_urls(self) -> None:
        (self.refs / "alpha").mkdir()
        (self.refs / "alpha" / "ref.wav").write_bytes(b"wav")
        self.assertEqual(voices_mod._voice_path("alpha"), self.refs / "alpha" / "ref.wav")
        self.assertEqual(voices_mod._ref_url("alpha"), "/api/voices/sample/alpha.wav")
        self.assertEqual(voices_mod._voice_url("alpha"), "/api/voices/sample/alpha.wav")

        (self.refs / "alpha" / "preview.wav").write_bytes(b"preview")
        self.assertEqual(voices_mod._preview_path("alpha"), self.refs / "alpha" / "preview.wav")
        self.assertEqual(voices_mod._voice_url("alpha"), "/api/voices/sample/alpha.wav?preview=1")

        (self.clones / "clone1").mkdir(parents=True)
        (self.clones / "clone1" / "ref.wav").write_bytes(b"clone")
        self.assertEqual(voices_mod._voice_path("clone1"), self.clones / "clone1" / "ref.wav")

        (self.refs / "web" / "web1").mkdir(parents=True)
        (self.refs / "web" / "web1" / "ref.wav").write_bytes(b"web")
        self.assertEqual(voices_mod._voice_path("web1"), self.refs / "web" / "web1" / "ref.wav")

        (self.refs / "flat.wav").write_bytes(b"flat")
        self.assertEqual(voices_mod._voice_path("flat"), self.refs / "flat.wav")

        (self.samples / "kok.wav").write_bytes(b"kok")
        self.assertEqual(voices_mod._voice_path("kok"), self.samples / "kok.wav")
        self.assertIsNone(voices_mod._voice_path("missing"))
        self.assertIsNone(voices_mod._preview_path("missing"))
        self.assertIsNone(voices_mod._voice_url("missing"))
        self.assertIsNone(voices_mod._ref_url("missing"))

    def test_load_catalog_missing_parse_error_and_valid(self) -> None:
        self.assertEqual(voices_mod._load_catalog_voices(), [])
        with patch.object(voices_mod, "CATALOG_PATH") as cp:
            cp.exists.return_value = True
            cp.read_text.side_effect = RuntimeError("bad")
            self.assertEqual(voices_mod._load_catalog_voices(), [])
        self.catalog.write_text(
            "voices:\n"
            "  english-male:\n"
            "    language: en\n"
            "    register: Deep male narrator\n"
            "    notes: note\n"
            "    duration_s: 4.5\n"
            "    use_cases: [documentary]\n"
            "  english-female:\n"
            "    language: en\n"
            "    register: Warm female narrator\n"
        )
        voices = voices_mod._load_catalog_voices()
        self.assertEqual({v.key for v in voices}, {"english-male", "english-female"})
        self.assertEqual(voices[0].provider, "human")

    def test_load_clone_voices_all_paths(self) -> None:
        self.assertEqual(voices_mod._load_clone_voices(), [])
        self.clones.mkdir(parents=True)
        (self.clones / "skipfile").write_text("x")
        (self.clones / "badmeta").mkdir()
        (self.clones / "badmeta" / "ref.wav").write_bytes(b"wav")
        (self.clones / "badmeta" / "meta.yaml").write_text("bad")
        with patch.object(voices_mod.yaml, "safe_load", side_effect=RuntimeError("bad yaml")):
            bad = voices_mod._load_clone_voices()
        self.assertEqual(bad[0].label, "Badmeta")
        (self.clones / "badmeta" / "meta.yaml").write_text("{}")

        (self.clones / "good").mkdir()
        (self.clones / "good" / "ref.wav").write_bytes(b"wav")
        (self.clones / "good" / "meta.yaml").write_text(
            "label: Good Clone\nlanguage: hi\ngender: female\nstyle: Calm\nnotes: Nice\nduration_s: 5.25\n"
        )
        voices = voices_mod._load_clone_voices()
        good = [v for v in voices if v.key == "good"][0]
        self.assertTrue(good.is_clone)
        self.assertEqual(good.language, "hi")
        self.assertEqual(good.use_cases, ["clone"])

    def test_load_web_voices_all_paths(self) -> None:
        self.assertEqual(voices_mod._load_web_voices(), [])
        web_dir = self.refs / "web"
        web_dir.mkdir()
        (web_dir / "skip").write_text("x")
        (web_dir / "badmeta").mkdir()
        (web_dir / "badmeta" / "ref.wav").write_bytes(b"wav")
        (web_dir / "badmeta" / "meta.yaml").write_text("bad")
        with patch.object(voices_mod.yaml, "safe_load", side_effect=RuntimeError("bad yaml")):
            bad = voices_mod._load_web_voices()
        self.assertEqual(bad[0].label, "Badmeta")
        (web_dir / "badmeta" / "meta.yaml").write_text("{}")

        (web_dir / "goodweb").mkdir()
        (web_dir / "goodweb" / "ref.wav").write_bytes(b"wav")
        (web_dir / "goodweb" / "meta.yaml").write_text(
            "label: Web Voice\nlanguage: en\ngender: male\nstyle: British warm\nsource: Archive\nduration_s: 7\nuse_cases: [sleep]\n"
        )
        voices = voices_mod._load_web_voices()
        good = [v for v in voices if v.key == "goodweb"][0]
        self.assertEqual(good.notes, "Archive")
        self.assertEqual(good.use_cases, ["sleep"])

    def test_load_kokoro_voices_known_unknown_and_missing(self) -> None:
        shutil.rmtree(self.samples)
        self.assertEqual(voices_mod._load_kokoro_voices(), [])
        self.samples.mkdir()
        (self.samples / "af_bella.wav").write_bytes(b"wav")
        (self.samples / "custom_voice.wav").write_bytes(b"wav")
        voices = voices_mod._load_kokoro_voices()
        by_key = {v.key: v for v in voices}
        self.assertEqual(by_key["af_bella"].provider, "kokoro")
        self.assertEqual(by_key["custom_voice"].style, "Kokoro preset")

    def test_all_voices_dedupes_and_enriches(self) -> None:
        base = VoiceInfo(key="same", label="Old", language="en", gender="", style="deep", provider="human")
        newer = VoiceInfo(key="same", label="New", language="en", gender="", style="warm", provider="human")
        with patch.object(voices_mod, "_load_kokoro_voices", return_value=[]), \
             patch.object(voices_mod, "_load_web_voices", return_value=[]), \
             patch.object(voices_mod, "_load_catalog_voices", return_value=[base]), \
             patch.object(voices_mod, "_load_clone_voices", return_value=[newer]), \
             patch.object(voices_mod, "_EXTRA_VOICES", []), \
             patch.object(voices_mod, "_voice_url", return_value="/u.wav"):
            voices = voices_mod._all_voices()
        self.assertEqual(len(voices), 1)
        self.assertEqual(voices[0].label, "New")
        self.assertEqual(voices[0].sample_url, "/u.wav")
        self.assertIn("warm", voices[0].tones)

    def test_ffprobe_duration_success_empty_and_failure(self) -> None:
        cp = subprocess.CompletedProcess(["ffprobe"], 0, stdout="12.5\n", stderr="")
        with patch.object(voices_mod.subprocess, "run", return_value=cp):
            self.assertEqual(voices_mod._ffprobe_duration(Path("x.wav")), 12.5)
        cp_empty = subprocess.CompletedProcess(["ffprobe"], 0, stdout="", stderr="")
        with patch.object(voices_mod.subprocess, "run", return_value=cp_empty):
            self.assertEqual(voices_mod._ffprobe_duration(Path("x.wav")), 0.0)
        with patch.object(voices_mod.subprocess, "run", side_effect=RuntimeError("boom")):
            self.assertEqual(voices_mod._ffprobe_duration(Path("x.wav")), 0.0)

    def test_ffmpeg_convert_builds_expected_command(self) -> None:
        with patch.object(voices_mod.subprocess, "run") as run:
            voices_mod._ffmpeg_convert(Path("in.mp3"), Path("out.wav"), max_seconds=9.0)
        args, kwargs = run.call_args
        self.assertIn("ffmpeg", args[0][0])
        self.assertIn("9.0", args[0])
        self.assertTrue(kwargs["check"])


class VoiceRoutesTest(VoicesBase, unittest.IsolatedAsyncioTestCase):
    async def _client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=_make_app()), base_url="http://test")

    async def test_famous_catalog_and_sample_routes(self) -> None:
        (self.refs / "sarah").mkdir()
        (self.refs / "sarah" / "ref.wav").write_bytes(b"RIFFdata")
        with patch.object(voices_mod, "_all_voices", return_value=[VoiceInfo(key="sarah", label="Sarah", language="en", gender="female", style="warm")]):
            async with await self._client() as c:
                famous = await c.get("/api/voices/famous")
                catalog = await c.get("/api/voices/catalog")
                sample = await c.get("/api/voices/sample/sarah.wav")
                missing = await c.get("/api/voices/sample/missing.wav")
        self.assertEqual(famous.status_code, 200)
        self.assertGreater(len(famous.json()["templates"]), 0)
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(catalog.json()["voices"][0]["key"], "sarah")
        self.assertEqual(sample.status_code, 200)
        self.assertEqual(missing.status_code, 404)

    async def test_delete_clone_missing_and_success(self) -> None:
        self.clones.mkdir(parents=True)
        (self.clones / "mine").mkdir()
        (self.clones / "mine" / "ref.wav").write_bytes(b"wav")
        async with await self._client() as c:
            missing = await c.delete("/api/voices/clone/absent")
            ok = await c.delete("/api/voices/clone/mine")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(ok.status_code, 200)
        self.assertFalse((self.clones / "mine").exists())


class CloneVoiceTest(VoicesBase, unittest.IsolatedAsyncioTestCase):
    async def test_rejects_missing_tools_no_filename_bad_extension_empty_name_and_duplicate(self) -> None:
        with patch.object(voices_mod.shutil, "which", return_value=None):
            with self.assertRaises(Exception) as ctx:
                await voices_mod.clone_voice(name="Voice", audio=_FakeUpload("a.wav", [b"x"]))
            self.assertEqual(ctx.exception.status_code, 500)

        with patch.object(voices_mod.shutil, "which", return_value="/bin/tool"):
            with self.assertRaises(Exception) as ctx:
                await voices_mod.clone_voice(name="Voice", audio=_FakeUpload("", [b"x"]))
            self.assertEqual(ctx.exception.status_code, 422)
            with self.assertRaises(Exception) as ctx:
                await voices_mod.clone_voice(name="Voice", audio=_FakeUpload("a.exe", [b"x"]))
            self.assertEqual(ctx.exception.status_code, 415)

        with patch.object(voices_mod.shutil, "which", return_value="/bin/tool"), \
             patch.object(voices_mod, "_slugify", return_value=""):
            with self.assertRaises(Exception) as ctx:
                await voices_mod.clone_voice(name="!!!", audio=_FakeUpload("a.wav", [b"x"]))
            self.assertEqual(ctx.exception.status_code, 422)

        self.clones.mkdir(parents=True)
        (self.clones / "voice").mkdir()
        with patch.object(voices_mod.shutil, "which", return_value="/bin/tool"):
            with self.assertRaises(Exception) as ctx:
                await voices_mod.clone_voice(name="voice", audio=_FakeUpload("a.wav", [b"x"]))
            self.assertEqual(ctx.exception.status_code, 409)

    async def test_clone_upload_too_large_conversion_failure_and_success_notes(self) -> None:
        with patch.object(voices_mod.shutil, "which", return_value="/bin/tool"), \
             patch.object(voices_mod.tempfile, "NamedTemporaryFile", _NamedTemp), \
             patch.object(voices_mod, "MAX_UPLOAD_BYTES", 3):
            with self.assertRaises(Exception) as ctx:
                await voices_mod.clone_voice(name="Huge", audio=_FakeUpload("a.wav", [b"12", b"34"]))
            self.assertEqual(ctx.exception.status_code, 413)
            self.assertFalse((self.clones / "huge").exists())

        err = subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"bad audio")
        with patch.object(voices_mod.shutil, "which", return_value="/bin/tool"), \
             patch.object(voices_mod.tempfile, "NamedTemporaryFile", _NamedTemp), \
             patch.object(voices_mod, "_ffmpeg_convert", side_effect=err):
            with self.assertRaises(Exception) as ctx:
                await voices_mod.clone_voice(name="Bad", audio=_FakeUpload("a.wav", [b"data"]))
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertFalse((self.clones / "bad").exists())

        def convert(_src: Path, dst: Path) -> None:
            dst.write_bytes(b"wav")

        with patch.object(voices_mod.shutil, "which", return_value="/bin/tool"), \
             patch.object(voices_mod.tempfile, "NamedTemporaryFile", _NamedTemp), \
             patch.object(voices_mod, "_ffmpeg_convert", side_effect=convert), \
             patch.object(voices_mod, "_ffprobe_duration", return_value=3.2):
            short = await voices_mod.clone_voice(
                name="Short Voice", language="hi", transcript="hello transcript",
                audio=_FakeUpload("a.wav", [b"data"]),
            )
        self.assertIn("shorter than 4s", short.note)
        self.assertEqual(short.voice.key, "short-voice")
        self.assertTrue((self.clones / "short-voice" / "ref.txt").exists())

        with patch.object(voices_mod.shutil, "which", return_value="/bin/tool"), \
             patch.object(voices_mod.tempfile, "NamedTemporaryFile", _NamedTemp), \
             patch.object(voices_mod, "_ffmpeg_convert", side_effect=convert), \
             patch.object(voices_mod, "_ffprobe_duration", return_value=15.5):
            long = await voices_mod.clone_voice(name="Long Voice", language="en", transcript="", audio=_FakeUpload("a.mp3", [b"data"]))
        self.assertIn("trimmed", long.note)
        self.assertEqual(long.duration_s, 15.5)

        with patch.object(voices_mod.shutil, "which", return_value="/bin/tool"), \
             patch.object(voices_mod.tempfile, "NamedTemporaryFile", _NamedTemp), \
             patch.object(voices_mod, "_ffmpeg_convert", side_effect=convert), \
             patch.object(voices_mod, "_ffprobe_duration", return_value=8.0):
            normal = await voices_mod.clone_voice(name="Normal Voice", language="en", transcript="", audio=_FakeUpload("a.ogg", [b"data"]))
        self.assertEqual(normal.note, "")


if __name__ == "__main__":
    unittest.main()
