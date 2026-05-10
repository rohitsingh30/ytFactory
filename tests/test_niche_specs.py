"""Tests for pipeline.niche_specs — file-system niche store."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import niche_specs
from pipeline.niche_specs import (
    NicheDoc,
    _channel_root,
    _niche_path,
    _niches_dir,
    delete_niche,
    get_niche,
    list_niches,
    save_niche,
    slugify_label,
)


def _minimal_doc(key="test_niche", **kwargs) -> NicheDoc:
    return NicheDoc(key=key, label="Test Niche", **kwargs)


class TestNicheDocModel(unittest.TestCase):
    def test_create_minimal(self):
        doc = NicheDoc(key="my_niche", label="My Niche")
        self.assertEqual(doc.key, "my_niche")
        self.assertEqual(doc.voice, "sarah")
        self.assertEqual(doc.format, "animated")

    def test_invalid_key_raises(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            NicheDoc(key="Bad Key!", label="x")

    def test_invalid_key_starts_with_underscore_raises(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            NicheDoc(key="_bad", label="x")

    def test_all_optional_fields(self):
        doc = NicheDoc(
            key="full_niche",
            label="Full",
            description="desc",
            prompt_style_guide="guide",
            length_kind="long",
            voice="am_michael",
            format="footage_only",
            source_kind="reddit",
            source_ref="https://example.com",
            hook_template="hook tmpl",
            closer_template="closer tmpl",
            image_style="oil painting",
            music_bed="lo_fi",
            created_by="backfill",
        )
        self.assertEqual(doc.length_kind, "long")
        self.assertEqual(doc.source_kind, "reddit")

    def test_extra_fields_ignored(self):
        doc = NicheDoc(key="niche1", label="N", target_length_s=55)
        self.assertFalse(hasattr(doc, "target_length_s"))


class TestChannelRootValidation(unittest.TestCase):
    def test_valid_key_returns_path(self):
        root = _channel_root("mystoriesanimated")
        self.assertIn("mystoriesanimated", str(root))

    def test_invalid_key_raises(self):
        with self.assertRaises(ValueError):
            _channel_root("bad-channel-key")


class TestNichePathHelpers(unittest.TestCase):
    def test_niches_dir(self):
        nd = _niches_dir("historyrecapped")
        self.assertTrue(str(nd).endswith("historyrecapped/niches"))

    def test_niche_path(self):
        p = _niche_path("historyrecapped", "history_short")
        self.assertTrue(str(p).endswith("history_short.json"))

    def test_invalid_niche_key_raises(self):
        with self.assertRaises(ValueError):
            _niche_path("historyrecapped", "Bad Key!")


class TestListNiches(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._root = Path(self._td.name)
        self._patcher = patch.object(niche_specs, "PROJECT_ROOT", self._root)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._td.cleanup()

    def _niches_dir(self, channel):
        d = self._root / channel / "niches"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_returns_empty_when_no_dir(self):
        result = list_niches("nosuchchannel")
        self.assertEqual(result, [])

    def test_returns_sorted_by_label(self):
        nd = self._niches_dir("chan1")
        for key, label in [("b_niche", "Zebra"), ("a_niche", "Apple")]:
            doc = NicheDoc(key=key, label=label)
            (nd / f"{key}.json").write_text(doc.model_dump_json())
        result = list_niches("chan1")
        self.assertEqual([r.label for r in result], ["Apple", "Zebra"])

    def test_skips_malformed_json(self):
        nd = self._niches_dir("chan2")
        (nd / "bad.json").write_text("{not valid json!!!}")
        doc = NicheDoc(key="good_niche", label="Good")
        (nd / "good_niche.json").write_text(doc.model_dump_json())
        result = list_niches("chan2")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].key, "good_niche")

    def test_skips_invalid_schema(self):
        nd = self._niches_dir("chan3")
        (nd / "invalid_schema.json").write_text(json.dumps({"key": "x", "no_label": True}))
        result = list_niches("chan3")
        self.assertEqual(result, [])


class TestGetNiche(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._root = Path(self._td.name)
        self._patcher = patch.object(niche_specs, "PROJECT_ROOT", self._root)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._td.cleanup()

    def _write(self, channel, doc: NicheDoc):
        nd = self._root / channel / "niches"
        nd.mkdir(parents=True, exist_ok=True)
        (nd / f"{doc.key}.json").write_text(doc.model_dump_json())

    def test_returns_none_when_missing(self):
        self.assertIsNone(get_niche("ch", "no_key"))

    def test_returns_doc_when_present(self):
        doc = NicheDoc(key="test_niche", label="Test")
        self._write("ch", doc)
        got = get_niche("ch", "test_niche")
        self.assertIsNotNone(got)
        self.assertEqual(got.label, "Test")  # type: ignore[union-attr]

    def test_returns_none_on_malformed(self):
        nd = self._root / "ch" / "niches"
        nd.mkdir(parents=True, exist_ok=True)
        (nd / "bad_niche.json").write_text("NOT JSON")
        self.assertIsNone(get_niche("ch", "bad_niche"))


class TestSaveNiche(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._root = Path(self._td.name)
        self._patcher = patch.object(niche_specs, "PROJECT_ROOT", self._root)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._td.cleanup()

    def test_creates_dirs_and_writes(self):
        doc = NicheDoc(key="my_niche", label="My Niche")
        returned = save_niche("ch", doc)
        self.assertEqual(returned.key, "my_niche")
        p = self._root / "ch" / "niches" / "my_niche.json"
        self.assertTrue(p.exists())

    def test_overwrites_existing(self):
        doc = NicheDoc(key="my_niche", label="Old")
        save_niche("ch", doc)
        doc2 = NicheDoc(key="my_niche", label="New")
        save_niche("ch", doc2)
        got = get_niche("ch", "my_niche")
        self.assertEqual(got.label, "New")  # type: ignore[union-attr]

    def test_round_trips_cleanly(self):
        doc = NicheDoc(key="my_niche", label="Round Trip", source_kind="reddit")
        save_niche("ch", doc)
        got = get_niche("ch", "my_niche")
        self.assertEqual(got.source_kind, "reddit")  # type: ignore[union-attr]


class TestDeleteNiche(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._root = Path(self._td.name)
        self._patcher = patch.object(niche_specs, "PROJECT_ROOT", self._root)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._td.cleanup()

    def test_returns_false_when_missing(self):
        self.assertFalse(delete_niche("ch", "no_key"))

    def test_deletes_and_returns_true(self):
        doc = NicheDoc(key="del_me", label="Delete Me")
        save_niche("ch", doc)
        result = delete_niche("ch", "del_me")
        self.assertTrue(result)
        self.assertIsNone(get_niche("ch", "del_me"))


# ---------------------------------------------------------------------------
# GCS-backend behaviour. We don't talk to a real bucket — the GCS helpers
# are patched at module scope so we can verify routing precedence
# (GCS-first, disk-fallback) and write-through semantics.
# ---------------------------------------------------------------------------


class _FakeGcsBackend:
    """In-memory stand-in for the four ``_gcs_*`` helpers."""

    def __init__(self, bucket: str = "test-bucket") -> None:
        self.bucket = bucket
        # store: { (channel, key): NicheDoc }
        self.store: dict[tuple[str, str], NicheDoc] = {}
        self.fail_load = False
        self.fail_save = False

    def install(self, mod):
        self._patches = [
            patch.object(mod, "_state_bucket", side_effect=lambda: self.bucket),
            patch.object(mod, "_gcs_load", side_effect=self._load),
            patch.object(mod, "_gcs_list", side_effect=self._list),
            patch.object(mod, "_gcs_save", side_effect=self._save),
            patch.object(mod, "_gcs_delete", side_effect=self._delete),
        ]
        for p in self._patches:
            p.start()
        return self

    def uninstall(self):
        for p in self._patches:
            p.stop()

    def _load(self, channel, key):
        if self.fail_load:
            return None
        return self.store.get((channel, key))

    def _list(self, channel):
        if self.fail_load:
            return []
        return [d for (c, _), d in self.store.items() if c == channel]

    def _save(self, channel, doc):
        if self.fail_save:
            return False
        self.store[(channel, doc.key)] = doc
        return True

    def _delete(self, channel, key):
        return self.store.pop((channel, key), None) is not None


class TestNicheSpecsGcsBackend(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._root = Path(self._td.name)
        self._root_patch = patch.object(niche_specs, "PROJECT_ROOT", self._root)
        self._root_patch.start()
        self.gcs = _FakeGcsBackend().install(niche_specs)

    def tearDown(self):
        self.gcs.uninstall()
        self._root_patch.stop()
        self._td.cleanup()

    def _disk_write(self, channel: str, doc: NicheDoc) -> Path:
        nd = self._root / channel / "niches"
        nd.mkdir(parents=True, exist_ok=True)
        p = nd / f"{doc.key}.json"
        p.write_text(doc.model_dump_json(indent=2))
        return p

    # --- get -----------------------------------------------------------------

    def test_get_prefers_gcs_over_disk(self):
        gcs_doc = NicheDoc(key="aita", label="From GCS")
        disk_doc = NicheDoc(key="aita", label="From Disk")
        self.gcs.store[("ch", "aita")] = gcs_doc
        self._disk_write("ch", disk_doc)
        got = get_niche("ch", "aita")
        self.assertIsNotNone(got)
        self.assertEqual(got.label, "From GCS")  # type: ignore[union-attr]

    def test_get_falls_back_to_disk_on_gcs_miss(self):
        disk_doc = NicheDoc(key="aita", label="Disk Only")
        self._disk_write("ch", disk_doc)
        got = get_niche("ch", "aita")
        self.assertIsNotNone(got)
        self.assertEqual(got.label, "Disk Only")  # type: ignore[union-attr]

    def test_get_falls_back_when_gcs_unavailable(self):
        # Simulate auth/network failure: GCS load returns None for every
        # call. Disk fallback should still serve the doc.
        self.gcs.fail_load = True
        disk_doc = NicheDoc(key="aita", label="Disk Survives")
        self._disk_write("ch", disk_doc)
        got = get_niche("ch", "aita")
        self.assertIsNotNone(got)
        self.assertEqual(got.label, "Disk Survives")  # type: ignore[union-attr]

    def test_get_returns_none_when_neither_backend_has_it(self):
        self.assertIsNone(get_niche("ch", "missing"))

    # --- list ----------------------------------------------------------------

    def test_list_merges_gcs_and_disk_dedup_by_key(self):
        # GCS has a, b. Disk has b (different label) + c. List returns
        # a, b (GCS wins), c — sorted by label.
        self.gcs.store[("ch", "a")] = NicheDoc(key="a", label="Alpha (cloud)")
        self.gcs.store[("ch", "b")] = NicheDoc(key="b", label="Beta (cloud)")
        self._disk_write("ch", NicheDoc(key="b", label="Beta (disk)"))
        self._disk_write("ch", NicheDoc(key="c", label="Charlie (disk)"))
        out = list_niches("ch")
        self.assertEqual([d.key for d in out], ["a", "b", "c"])
        # Beta from GCS wins over disk
        beta = next(d for d in out if d.key == "b")
        self.assertEqual(beta.label, "Beta (cloud)")

    # --- save ----------------------------------------------------------------

    def test_save_writes_through_to_both_backends(self):
        doc = NicheDoc(key="new_niche", label="New")
        save_niche("ch", doc)
        # GCS got it
        self.assertIn(("ch", "new_niche"), self.gcs.store)
        # Disk got it
        on_disk = self._root / "ch" / "niches" / "new_niche.json"
        self.assertTrue(on_disk.exists())

    def test_save_disk_failure_does_not_blow_up_in_cloud_mode(self):
        # Simulate read-only FS: replace _niches_dir's mkdir with one that
        # raises. With a bucket configured, save should swallow the OSError.
        doc = NicheDoc(key="cloud_only", label="Cloud Only")
        with patch.object(niche_specs.Path, "mkdir", side_effect=OSError("read-only")):
            save_niche("ch", doc)  # must not raise
        self.assertIn(("ch", "cloud_only"), self.gcs.store)

    # --- delete --------------------------------------------------------------

    def test_delete_removes_from_both(self):
        doc = NicheDoc(key="bye", label="Goodbye")
        save_niche("ch", doc)
        self.assertTrue(delete_niche("ch", "bye"))
        self.assertNotIn(("ch", "bye"), self.gcs.store)
        self.assertFalse((self._root / "ch" / "niches" / "bye.json").exists())

    def test_delete_returns_true_for_gcs_only_doc(self):
        # Doc only exists in GCS — delete still reports success.
        self.gcs.store[("ch", "ghost")] = NicheDoc(key="ghost", label="Ghost")
        self.assertTrue(delete_niche("ch", "ghost"))

    def test_delete_returns_false_when_neither_backend_has_it(self):
        self.assertFalse(delete_niche("ch", "never_existed"))


class TestNicheSpecsBucketEnvOff(unittest.TestCase):
    """When YTFACTORY_STATE_BUCKET is unset, behaviour is pure-disk
    (the existing tests above cover the happy path; this just guards
    against a regression where the GCS branch fires without a bucket)."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._root = Path(self._td.name)
        self._root_patch = patch.object(niche_specs, "PROJECT_ROOT", self._root)
        self._root_patch.start()
        # Force-clear bucket env in case the test runner inherited one.
        self._env_patch = patch.dict(
            niche_specs.os.environ, {niche_specs._BUCKET_ENV: ""}, clear=False,
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._root_patch.stop()
        self._td.cleanup()

    def test_bucket_env_unset_returns_none(self):
        self.assertIsNone(niche_specs._state_bucket())

    def test_no_gcs_calls_attempted_when_bucket_unset(self):
        # If the env is unset, _gcs_load / _gcs_save / _gcs_delete should
        # be no-ops (and never try to import google.cloud.storage).
        self.assertIsNone(niche_specs._gcs_load("ch", "x"))
        self.assertEqual(niche_specs._gcs_list("ch"), [])
        self.assertFalse(niche_specs._gcs_save("ch", NicheDoc(key="x", label="X")))
        self.assertFalse(niche_specs._gcs_delete("ch", "x"))


class TestSlugifyLabel(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(slugify_label("My Niche"), "my_niche")

    def test_strips_special_chars(self):
        self.assertEqual(slugify_label("Hello, World!"), "hello_world")

    def test_collapses_underscores(self):
        self.assertEqual(slugify_label("foo  --  bar"), "foo_bar")

    def test_fallback_when_empty(self):
        self.assertEqual(slugify_label(""), "niche")

    def test_fallback_when_no_alnum(self):
        self.assertEqual(slugify_label("!!!"), "niche")

    def test_leading_non_alpha_stripped(self):
        result = slugify_label("123test")
        self.assertTrue(result[0].isalnum())

    def test_truncates_at_64(self):
        long_label = "word " * 20
        result = slugify_label(long_label)
        self.assertLessEqual(len(result), 64)

    def test_numbers_ok(self):
        self.assertEqual(slugify_label("top 5"), "top_5")


if __name__ == "__main__":
    unittest.main()
