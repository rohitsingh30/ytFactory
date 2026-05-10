"""Tests for pipeline.schemas.customization — per-channel UI schema."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pipeline.schemas.customization as custom_mod
from pipeline.schemas.customization import (
    CHANNEL_REGISTRY,
    ChannelSummary,
    CustomizationField,
    CustomizationSchema,
    FieldOption,
    RecentVideo,
    _humanize_slug,
    _list_variants,
    _load_yaml,
    _registry_entry,
    _sidecar_path,
    _voice_options_for,
    _default_voice_for,
    _default_length_for,
    _source_kinds_for,
    get_channel,
    get_customization_schema,
    list_channels,
    load_user_defaults,
    save_user_defaults,
)


# ── _load_yaml ────────────────────────────────────────────────────────────

class TestLoadYaml(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        result = _load_yaml(Path("/no/such/file.yaml"))
        self.assertEqual(result, {})

    def test_malformed_yaml_returns_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write("{invalid: yaml: nested: [}")
            f.flush()
            result = _load_yaml(Path(f.name))
        self.assertEqual(result, {})

    def test_valid_yaml_parsed(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write("key: value\n")
            f.flush()
            result = _load_yaml(Path(f.name))
        self.assertEqual(result, {"key": "value"})

    def test_empty_yaml_returns_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write("")
            f.flush()
            result = _load_yaml(Path(f.name))
        self.assertEqual(result, {})


# ── _sidecar_path ────────────────────────────────────────────────────────

class TestSidecarPath(unittest.TestCase):
    def test_known_channel_returns_yaml_parent_based_path(self):
        # Any channel in CHANNEL_REGISTRY should give a path within pipeline/channels/
        entry = CHANNEL_REGISTRY[0]
        p = _sidecar_path(entry["key"])
        self.assertTrue(str(p).endswith(".user_defaults.json"))

    def test_unknown_channel_falls_back_to_channel_root(self):
        p = _sidecar_path("no_such_channel_xyz")
        self.assertIn("no_such_channel_xyz", str(p))
        self.assertTrue(str(p).endswith(".user_defaults.json"))


# ── load/save user defaults ───────────────────────────────────────────────

class TestUserDefaults(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._root = Path(self._td.name)
        self._patcher = patch.object(custom_mod, "PROJECT_ROOT", self._root)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._td.cleanup()

    def test_load_missing_returns_empty(self):
        result = load_user_defaults("historyrecapped")
        self.assertEqual(result, {})

    def test_load_malformed_json_returns_empty(self):
        # Write bad JSON into the expected sidecar location
        entry = _registry_entry("historyrecapped")
        assert entry
        sidecar = self._root / Path(entry["yaml"]).parent / ".user_defaults.json"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("NOT JSON")
        result = load_user_defaults("historyrecapped")
        self.assertEqual(result, {})

    def test_load_valid_json(self):
        entry = _registry_entry("historyrecapped")
        assert entry
        sidecar = self._root / Path(entry["yaml"]).parent / ".user_defaults.json"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"voice": "am_michael"}))
        result = load_user_defaults("historyrecapped")
        self.assertEqual(result, {"voice": "am_michael"})

    def test_save_user_defaults(self):
        entry = _registry_entry("historyrecapped")
        assert entry
        save_user_defaults("historyrecapped", {"voice": "sarah", "length_s": 55})
        result = load_user_defaults("historyrecapped")
        self.assertEqual(result["voice"], "sarah")
        self.assertEqual(result["length_s"], 55)


# ── _registry_entry ───────────────────────────────────────────────────────

class TestRegistryEntry(unittest.TestCase):
    def test_known_channel_returns_entry(self):
        entry = _registry_entry("historyrecapped")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["key"], "historyrecapped")  # type: ignore[index]

    def test_unknown_channel_returns_none(self):
        self.assertIsNone(_registry_entry("no_such_channel_xyz"))


# ── _humanize_slug ────────────────────────────────────────────────────────

class TestHumanizeSlug(unittest.TestCase):
    def test_acronym_preserved(self):
        self.assertEqual(_humanize_slug("aita_animated"), "AITA Animated")

    def test_tifu(self):
        self.assertEqual(_humanize_slug("tifu"), "TIFU")

    def test_small_word_lowercase(self):
        result = _humanize_slug("today_in_history")
        self.assertEqual(result, "Today in History")

    def test_empty_words_skipped(self):
        result = _humanize_slug("__double__underscore__")
        self.assertNotIn("  ", result)  # no double spaces

    def test_regular_word_capitalized(self):
        self.assertEqual(_humanize_slug("cosmos"), "Cosmos")

    def test_hyphen_treated_as_separator(self):
        result = _humanize_slug("top-5-ranked")
        self.assertIn("Top", result)

    def test_small_word_at_start_capitalized(self):
        # "in" at start (index 0) → capitalized
        result = _humanize_slug("in_depth")
        self.assertTrue(result.startswith("In"))


# ── _list_variants ────────────────────────────────────────────────────────

class TestListVariants(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._root = Path(self._td.name)
        self._patcher = patch.object(custom_mod, "PROJECT_ROOT", self._root)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._td.cleanup()

    def test_fallback_to_default_format_when_no_variants_dir(self):
        entry = {"key": "historyrecapped", "variants_dir": None,
                 "default_format": "footage_only", "yaml": "pipeline/channels/historyrecapped.yaml"}
        result = _list_variants(entry)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].value, "footage_only")

    def test_reads_yaml_name_and_description(self):
        vd = self._root / "variants" / "testchan"
        vd.mkdir(parents=True)
        variant_yaml = vd / "my_variant.yaml"
        variant_yaml.write_text("name: My Great Variant\ndescription: A nice one\n")
        entry = {"key": "testchan", "variants_dir": "variants/testchan",
                 "default_format": "animated", "yaml": "pipeline/channels/testchan.yaml"}
        result = _list_variants(entry)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].label, "My Great Variant")
        self.assertEqual(result[0].description, "A nice one")

    def test_slug_derived_label_when_no_name(self):
        vd = self._root / "variants" / "testchan"
        vd.mkdir(parents=True)
        variant_yaml = vd / "aita_animated.yaml"
        variant_yaml.write_text("tts_voice: sarah.wav\n")
        entry = {"key": "testchan", "variants_dir": "variants/testchan",
                 "default_format": "animated", "yaml": "pipeline/channels/testchan.yaml"}
        result = _list_variants(entry)
        self.assertEqual(result[0].label, "AITA Animated")

    def test_description_none_when_absent(self):
        vd = self._root / "variants" / "testchan"
        vd.mkdir(parents=True)
        variant_yaml = vd / "my_var.yaml"
        variant_yaml.write_text("name: Test\n")  # no description
        entry = {"key": "testchan", "variants_dir": "variants/testchan",
                 "default_format": "animated", "yaml": "pipeline/channels/testchan.yaml"}
        result = _list_variants(entry)
        self.assertIsNone(result[0].description)

    def test_fallback_to_format_when_dir_not_exists(self):
        entry = {"key": "testchan", "variants_dir": "variants/no_such_dir",
                 "default_format": "split_screen", "yaml": "pipeline/channels/testchan.yaml"}
        result = _list_variants(entry)
        self.assertEqual(result[0].value, "split_screen")


# ── _voice_options_for ────────────────────────────────────────────────────

class TestVoiceOptionsFor(unittest.TestCase):
    def test_hindi_voice_options(self):
        opts = _voice_options_for("hi")
        values = [o.value for o in opts]
        self.assertIn("hf_alpha", values)

    def test_hindi_en_voice_options(self):
        opts = _voice_options_for("hi-en")
        values = [o.value for o in opts]
        # hi-en uses Hindi voices (hf_alpha, hf_beta, etc.)
        self.assertIn("hf_alpha", values)

    def test_english_voice_options(self):
        opts = _voice_options_for("en")
        values = [o.value for o in opts]
        self.assertIn("sarah", values)
        self.assertIn("am_michael", values)


# ── _default_voice_for ────────────────────────────────────────────────────

class TestDefaultVoiceFor(unittest.TestCase):
    def _entry(self, lang="en"):
        return {"key": "testchan", "language": lang}

    def test_wav_path_returns_stem(self):
        result = _default_voice_for(self._entry(), {"tts_voice": "sarah.wav"})
        self.assertEqual(result, "sarah")

    def test_non_wav_voice_returned_as_is(self):
        result = _default_voice_for(self._entry(), {"tts_voice": "am_michael"})
        self.assertEqual(result, "am_michael")

    def test_hindi_falls_back_to_hf_alpha(self):
        result = _default_voice_for(self._entry(lang="hi"), {})
        self.assertEqual(result, "hf_alpha")

    def test_english_falls_back_to_sarah(self):
        result = _default_voice_for(self._entry(), {})
        self.assertEqual(result, "sarah")

    def test_empty_raw_falls_through(self):
        result = _default_voice_for(self._entry(), {"tts_voice": ""})
        self.assertEqual(result, "sarah")


# ── _default_length_for ───────────────────────────────────────────────────

class TestDefaultLengthFor(unittest.TestCase):
    def _entry(self):
        return {"key": "x", "language": "en"}

    def test_list_returns_last_element(self):
        result = _default_length_for(self._entry(), {"duration_target_s": [40, 60]})
        self.assertEqual(result, 60)

    def test_fallback_55(self):
        result = _default_length_for(self._entry(), {})
        self.assertEqual(result, 55)

    def test_invalid_list_falls_back(self):
        result = _default_length_for(self._entry(), {"duration_target_s": ["not-int"]})
        self.assertEqual(result, 55)

    def test_empty_list_falls_back(self):
        result = _default_length_for(self._entry(), {"duration_target_s": []})
        self.assertEqual(result, 55)


# ── _source_kinds_for ─────────────────────────────────────────────────────

class TestSourceKindsFor(unittest.TestCase):
    def _entry(self, key: str):
        return {"key": key}

    def test_mystoriesanimated_includes_reddit(self):
        base = _source_kinds_for(self._entry("mystoriesanimated"))
        self.assertIn("reddit_url", base)

    def test_scrollpulse_includes_reddit(self):
        base = _source_kinds_for(self._entry("scrollpulse"))
        self.assertIn("reddit_url", base)

    def test_historyrecapped_includes_wikipedia_and_youtube(self):
        base = _source_kinds_for(self._entry("historyrecapped"))
        self.assertIn("wikipedia_topic", base)
        self.assertIn("youtube_video", base)

    def test_cosmosdecoded_includes_wikipedia(self):
        base = _source_kinds_for(self._entry("cosmosdecoded"))
        self.assertIn("wikipedia_topic", base)

    def test_hindutavaanimated_includes_wikipedia(self):
        base = _source_kinds_for(self._entry("hindutavaanimated"))
        self.assertIn("wikipedia_topic", base)

    def test_sportsrecapped_includes_youtube(self):
        base = _source_kinds_for(self._entry("sportsrecapped"))
        self.assertIn("youtube_video", base)

    def test_unknown_channel_returns_base_only(self):
        base = _source_kinds_for(self._entry("unknown_xyz"))
        self.assertEqual(set(base), {"auto", "user_text"})


# ── get_customization_schema ──────────────────────────────────────────────

class TestGetCustomizationSchema(unittest.TestCase):
    def test_unknown_channel_returns_none(self):
        result = get_customization_schema("no_such_channel_xyz")
        self.assertIsNone(result)

    def test_known_channel_returns_schema(self):
        entry = CHANNEL_REGISTRY[0]
        # Mock _personality_for to avoid network/filesystem
        fake_personality = {
            "avatar_url": None, "banner_url": None, "youtube_url": None,
            "custom_url": None, "subscribers": None, "youtube_video_count": None,
            "total_views": None, "recent_videos": [],
            "avatar_mirrored": False, "banner_mirrored": False,
        }
        with patch.object(custom_mod, "_personality_for", return_value=fake_personality):
            result = get_customization_schema(entry["key"])
        self.assertIsNotNone(result)
        self.assertEqual(result.channel, entry["key"])  # type: ignore[union-attr]
        self.assertIsInstance(result.fields, list)  # type: ignore[union-attr]
        self.assertIsInstance(result.variants, list)  # type: ignore[union-attr]

    def test_stale_saved_variant_dropped(self):
        """A sidecar default_variant pointing to a non-existent variant is dropped."""
        entry = CHANNEL_REGISTRY[0]
        fake_personality = {
            "avatar_url": None, "banner_url": None, "youtube_url": None,
            "custom_url": None, "subscribers": None, "youtube_video_count": None,
            "total_views": None, "recent_videos": [],
            "avatar_mirrored": False, "banner_mirrored": False,
        }
        with patch.object(custom_mod, "_personality_for", return_value=fake_personality), \
             patch.object(custom_mod, "load_user_defaults",
                          return_value={"default_variant": "no_such_variant_xyz"}):
            result = get_customization_schema(entry["key"])
        self.assertIsNone(result.default_variant)  # type: ignore[union-attr]

    def test_audio_mode_and_song_and_visual_source_fields_present(self):
        """Every channel exposes the audio_mode flip, the three Suno song
        fields, and the visual_source picker — these drive the Customize
        step's Voice/Song toggle and the Background-visuals card."""
        entry = CHANNEL_REGISTRY[0]
        fake_personality = {
            "avatar_url": None, "banner_url": None, "youtube_url": None,
            "custom_url": None, "subscribers": None, "youtube_video_count": None,
            "total_views": None, "recent_videos": [],
            "avatar_mirrored": False, "banner_mirrored": False,
        }
        with patch.object(custom_mod, "_personality_for", return_value=fake_personality):
            result = get_customization_schema(entry["key"])
        keys = {f.key for f in result.fields}  # type: ignore[union-attr]
        self.assertIn("audio_mode", keys)
        self.assertIn("song_style", keys)
        self.assertIn("song_vocal_gender", keys)
        self.assertIn("song_model", keys)
        self.assertIn("visual_source", keys)

    def test_audio_mode_default_song_for_song_channel(self):
        """rhymetimejunction's audio_provider is sunoapi/external_song,
        so the Customize form pre-selects the Song tab (default = 'song').
        Other channels default to 'voice'."""
        fake_personality = {
            "avatar_url": None, "banner_url": None, "youtube_url": None,
            "custom_url": None, "subscribers": None, "youtube_video_count": None,
            "total_views": None, "recent_videos": [],
            "avatar_mirrored": False, "banner_mirrored": False,
        }
        with patch.object(custom_mod, "_personality_for", return_value=fake_personality):
            rhyme = get_customization_schema("rhymetimejunction")
            mystories = get_customization_schema("mystoriesanimated")
        rhyme_audio_mode = next(
            (f for f in rhyme.fields if f.key == "audio_mode"), None  # type: ignore[union-attr]
        )
        my_audio_mode = next(
            (f for f in mystories.fields if f.key == "audio_mode"), None  # type: ignore[union-attr]
        )
        self.assertEqual(rhyme_audio_mode.default, "song")
        self.assertEqual(my_audio_mode.default, "voice")


# ── get_channel ───────────────────────────────────────────────────────────

class TestGetChannel(unittest.TestCase):
    def test_unknown_returns_none(self):
        fake_personality = {
            "avatar_url": None, "banner_url": None, "youtube_url": None,
            "custom_url": None, "subscribers": None, "youtube_video_count": None,
            "total_views": None, "recent_videos": [],
            "avatar_mirrored": False, "banner_mirrored": False,
        }
        with patch.object(custom_mod, "_personality_for", return_value=fake_personality):
            result = get_channel("no_such_channel_xyz")
        self.assertIsNone(result)

    def test_known_returns_summary(self):
        entry = CHANNEL_REGISTRY[0]
        fake_personality = {
            "avatar_url": None, "banner_url": None, "youtube_url": None,
            "custom_url": None, "subscribers": None, "youtube_video_count": None,
            "total_views": None, "recent_videos": [],
            "avatar_mirrored": False, "banner_mirrored": False,
        }
        with patch.object(custom_mod, "_personality_for", return_value=fake_personality):
            result = get_channel(entry["key"])
        self.assertIsNotNone(result)
        self.assertEqual(result.key, entry["key"])  # type: ignore[union-attr]


# ── _personality_for (line 480 coverage — skip video with no vid) ────────

class TestPersonalityFor(unittest.TestCase):
    def test_skip_video_missing_video_id(self):
        """Videos without video_id should be skipped (line 480)."""
        fake_account_data = {
            "channel": {
                "avatar_url": None,
                "banner_url": None,
                "id": "UCtest123",
            },
            "videos": [
                {"video_id": None, "title": "no id"},      # should be skipped (line 480)
                {"video_id": "abc123", "title": "good video"},
            ],
        }
        mock_yt = MagicMock()
        mock_yt.load_account.return_value = fake_account_data
        mock_channel_assets = MagicMock()
        mock_channel_assets.asset_path.return_value = None

        with patch.dict("sys.modules", {
            "pipeline.research.youtube": mock_yt,
            "pipeline.research.channel_assets": mock_channel_assets,
        }):
            # Force re-import of _personality_for with mocked modules
            result = custom_mod._personality_for("testchan")

        recent = result["recent_videos"]
        # The None-id video should have been skipped
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0].video_id, "abc123")

    def test_youtube_url_from_custom_url(self):
        fake_account_data = {
            "channel": {
                "custom_url": "@myhandle",
                "avatar_url": None, "banner_url": None,
            },
            "videos": [],
        }
        mock_yt = MagicMock()
        mock_yt.load_account.return_value = fake_account_data
        mock_channel_assets = MagicMock()
        mock_channel_assets.asset_path.return_value = None

        with patch.dict("sys.modules", {
            "pipeline.research.youtube": mock_yt,
            "pipeline.research.channel_assets": mock_channel_assets,
        }):
            result = custom_mod._personality_for("testchan")
        self.assertIn("myhandle", result["youtube_url"])  # type: ignore[operator]

    def test_youtube_url_from_channel_id(self):
        fake_account_data = {
            "channel": {
                "id": "UCfoo",
                "avatar_url": None, "banner_url": None,
            },
            "videos": [],
        }
        mock_yt = MagicMock()
        mock_yt.load_account.return_value = fake_account_data
        mock_channel_assets = MagicMock()
        mock_channel_assets.asset_path.return_value = None

        with patch.dict("sys.modules", {
            "pipeline.research.youtube": mock_yt,
            "pipeline.research.channel_assets": mock_channel_assets,
        }):
            result = custom_mod._personality_for("testchan")
        self.assertIn("UCfoo", result["youtube_url"])  # type: ignore[operator]


if __name__ == "__main__":
    unittest.main()
