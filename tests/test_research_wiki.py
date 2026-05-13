"""Tests for pipeline/research/wiki.py — Wikipedia event dossier.

All HTTP and LLM calls are mocked.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.research import wiki as wiki_mod


# ---------------------------------------------------------------------------
# _slug_to_title
# ---------------------------------------------------------------------------


class SlugToTitleTest(unittest.TestCase):

    def test_replaces_hyphens_with_spaces(self):
        result = wiki_mod._slug_to_title("man-city-vs-liverpool-2019")
        self.assertIn("man city", result.lower())

    def test_replaces_underscores(self):
        result = wiki_mod._slug_to_title("aguero_93_20")
        self.assertIn("aguero 93 20", result)

    def test_empty_slug_returns_none(self):
        result = wiki_mod._slug_to_title("")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _wiki_search
# ---------------------------------------------------------------------------


class WikiSearchTest(unittest.TestCase):

    def test_returns_top_hit_title(self):
        resp = MagicMock()
        resp.json.return_value = {
            "query": {"search": [{"title": "Sergio Aguero"}, {"title": "Other"}]}
        }
        resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=resp):
            result = wiki_mod._wiki_search("aguero goal")
        self.assertEqual(result, "Sergio Aguero")

    def test_returns_none_when_no_hits(self):
        resp = MagicMock()
        resp.json.return_value = {"query": {"search": []}}
        resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=resp):
            result = wiki_mod._wiki_search("zzz_nonexistent_query")
        self.assertIsNone(result)

    def test_returns_none_on_request_exception(self):
        import requests as req
        with patch("requests.get", side_effect=req.RequestException("timeout")):
            result = wiki_mod._wiki_search("test query")
        self.assertIsNone(result)

    def test_returns_none_on_json_decode_error(self):
        resp = MagicMock()
        resp.json.side_effect = ValueError("bad json")
        resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=resp):
            result = wiki_mod._wiki_search("test")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _wiki_extract
# ---------------------------------------------------------------------------


class WikiExtractTest(unittest.TestCase):

    def test_returns_extract_text(self):
        resp = MagicMock()
        resp.json.return_value = {
            "query": {"pages": {"1": {"extract": "Some article text here."}}}
        }
        resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=resp):
            result = wiki_mod._wiki_extract("Test Article")
        self.assertEqual(result, "Some article text here.")

    def test_returns_none_when_extract_missing(self):
        resp = MagicMock()
        resp.json.return_value = {
            "query": {"pages": {"1": {"title": "T"}}}  # no extract
        }
        resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=resp):
            result = wiki_mod._wiki_extract("Test Article")
        self.assertIsNone(result)

    def test_returns_none_on_request_exception(self):
        import requests as req
        with patch("requests.get", side_effect=req.RequestException("error")):
            result = wiki_mod._wiki_extract("Test Article")
        self.assertIsNone(result)

    def test_returns_none_on_json_decode_error(self):
        resp = MagicMock()
        resp.json.side_effect = ValueError("bad json")
        resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=resp):
            result = wiki_mod._wiki_extract("Title")
        self.assertIsNone(result)

    def test_picks_lowest_pageid_on_disambiguation(self):
        """Audit D3.74 — pre-fix this returned the FIRST page in
        ``pages.values()`` iteration order, which is API-response
        dependent (Wikipedia returns page-ids in arbitrary order on
        disambiguation/redirect chains). Now sorted by page-id so
        the same query always returns the same extract."""
        resp = MagicMock()
        # Three pages — 5 (lowest), 100, 42. Pre-fix any of these
        # could be picked. Now: page 5's extract is the canonical
        # winner.
        resp.json.return_value = {
            "query": {
                "pages": {
                    "100": {"extract": "page-100 extract"},
                    "5":   {"extract": "page-5 extract"},
                    "42":  {"extract": "page-42 extract"},
                }
            }
        }
        resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=resp):
            result = wiki_mod._wiki_extract("Test Article")
        self.assertEqual(result, "page-5 extract")

    def test_skips_pageid_with_no_extract(self):
        # Page 1 has no extract; page 9 does. Result: page 9 is picked
        # despite having a higher id, because page 1 yields nothing.
        resp = MagicMock()
        resp.json.return_value = {
            "query": {
                "pages": {
                    "1": {"title": "no-extract"},
                    "9": {"extract": "page-9 extract"},
                }
            }
        }
        resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=resp):
            result = wiki_mod._wiki_extract("Test Article")
        self.assertEqual(result, "page-9 extract")

    def test_negative_pageid_missing_pages_handled(self):
        # Wikipedia uses negative page-ids for missing pages.
        # Negative sorts BEFORE positive but typically has no extract,
        # so the real positive-id page wins.
        resp = MagicMock()
        resp.json.return_value = {
            "query": {
                "pages": {
                    "-1": {"missing": True},  # no extract
                    "7": {"extract": "page-7 extract"},
                }
            }
        }
        resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=resp):
            result = wiki_mod._wiki_extract("Test Article")
        self.assertEqual(result, "page-7 extract")

    def test_unparseable_pageid_falls_back_to_string_sort(self):
        # Audit D3.74 — defensive: if Wikipedia ever returns a
        # non-numeric page-id (it doesn't today, but the schema is
        # technically string-typed), fall back to lexicographic sort
        # rather than crashing on int(k). Both pages have an extract;
        # bucket-1 (string-sorted) entries come AFTER bucket-0
        # (int-sorted) so the int-id page wins. This pins the helper
        # branch.
        resp = MagicMock()
        resp.json.return_value = {
            "query": {
                "pages": {
                    "weird-id": {"extract": "weird page extract"},
                    "5": {"extract": "page-5 extract"},
                }
            }
        }
        resp.raise_for_status = MagicMock()
        with patch("requests.get", return_value=resp):
            result = wiki_mod._wiki_extract("Test Article")
        # Numeric-id page sorts first (bucket 0), so it wins.
        self.assertEqual(result, "page-5 extract")


# ---------------------------------------------------------------------------
# fetch_article
# ---------------------------------------------------------------------------


class FetchArticleTest(unittest.TestCase):

    def test_returns_none_when_search_fails(self):
        with patch.object(wiki_mod, "_wiki_search", return_value=None):
            result = wiki_mod.fetch_article("test query")
        self.assertIsNone(result)

    def test_returns_none_when_extract_fails(self):
        with patch.object(wiki_mod, "_wiki_search", return_value="Some Article"):
            with patch.object(wiki_mod, "_wiki_extract", return_value=None):
                result = wiki_mod.fetch_article("test query")
        self.assertIsNone(result)

    def test_returns_title_and_text(self):
        with patch.object(wiki_mod, "_wiki_search", return_value="Aguero"):
            with patch.object(wiki_mod, "_wiki_extract", return_value="Article text."):
                result = wiki_mod.fetch_article("aguero")
        self.assertEqual(result, ("Aguero", "Article text."))


# ---------------------------------------------------------------------------
# _seed_for
# ---------------------------------------------------------------------------


class SeedForTest(unittest.TestCase):

    def test_deterministic(self):
        s1 = wiki_mod._seed_for("Sergio Aguero")
        s2 = wiki_mod._seed_for("Sergio Aguero")
        self.assertEqual(s1, s2)

    def test_different_names_differ(self):
        s1 = wiki_mod._seed_for("Aguero")
        s2 = wiki_mod._seed_for("Messi")
        self.assertNotEqual(s1, s2)

    def test_case_insensitive(self):
        s1 = wiki_mod._seed_for("Aguero")
        s2 = wiki_mod._seed_for("AGUERO")
        self.assertEqual(s1, s2)

    def test_in_valid_range(self):
        s = wiki_mod._seed_for("Test Player")
        self.assertGreaterEqual(s, 0)
        self.assertLess(s, 2**31)


# ---------------------------------------------------------------------------
# _attach_seeds
# ---------------------------------------------------------------------------


class AttachSeedsTest(unittest.TestCase):

    def test_attaches_seed_to_each_person(self):
        dossier = {
            "people": [
                {"name": "Aguero", "role": "striker"},
                {"name": "Dzeko", "role": "striker"},
            ]
        }
        result = wiki_mod._attach_seeds(dossier)
        for p in result["people"]:
            self.assertIn("seed", p)
            self.assertIsInstance(p["seed"], int)

    def test_skips_non_dict_people(self):
        dossier = {"people": ["not_a_dict"]}
        # Should not raise
        wiki_mod._attach_seeds(dossier)

    def test_skips_people_without_name(self):
        dossier = {"people": [{"role": "striker"}]}
        # Should not raise
        wiki_mod._attach_seeds(dossier)

    def test_empty_people_list(self):
        dossier = {"people": []}
        result = wiki_mod._attach_seeds(dossier)
        self.assertEqual(result["people"], [])


# ---------------------------------------------------------------------------
# load_dossier
# ---------------------------------------------------------------------------


class LoadDossierTest(unittest.TestCase):

    def test_returns_none_when_file_missing(self):
        result = wiki_mod.load_dossier(Path("/nonexistent/__dossier__.json"))
        self.assertIsNone(result)

    def test_returns_none_on_bad_json(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("{bad}")
            tmp = Path(f.name)
        try:
            result = wiki_mod.load_dossier(tmp)
            self.assertIsNone(result)
        finally:
            tmp.unlink(missing_ok=True)

    def test_returns_none_when_no_people_key(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write(json.dumps({"match": {"title": "test"}}))
            tmp = Path(f.name)
        try:
            result = wiki_mod.load_dossier(tmp)
            self.assertIsNone(result)
        finally:
            tmp.unlink(missing_ok=True)

    def test_returns_dossier_when_valid(self):
        dossier = {
            "people": [{"name": "Aguero", "role": "striker", "seed": 123}],
            "match": {"title": "Man City vs QPR", "date": "2012-05-13", "summary": ""},
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write(json.dumps(dossier))
            tmp = Path(f.name)
        try:
            result = wiki_mod.load_dossier(tmp)
            self.assertIsNotNone(result)
            self.assertIn("people", result)
        finally:
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# author_dossier
# ---------------------------------------------------------------------------


def _make_raw_story(**kw) -> dict:
    return {
        "slug": kw.get("slug", "aguero-93-20"),
        "title": kw.get("title", "Aguero 93:20"),
        "body": kw.get("body", "Aguero scored in stoppage time."),
        "source": "manual",
        "url": "",
        "metadata": {},
    }


def _make_llm_response() -> dict:
    return {
        "match": {
            "title": "Man City vs QPR",
            "date": "2012-05-13",
            "venue": "Etihad Stadium",
            "competition": "Premier League",
            "result": "Man City 3-2 QPR",
            "summary": "Aguero scored in the 93rd minute.",
        },
        "people": [
            {
                "name": "Sergio Aguero",
                "aliases": ["Kun"],
                "role": "striker",
                "team": "Man City",
                "visual": {
                    "body": "muscular",
                    "hair": "dark short",
                    "kit": "sky blue",
                    "shirt_number": "16",
                },
                "pronunciation_phonetic": "ah-GWAIR-oh",
            }
        ],
        "key_moments": [
            {"time": "ninety-third minute", "description": "Aguero scores."}
        ],
        "pronunciation_dict": {"Aguero": "ah-GWAIR-oh", "Kun": "KOON"},
    }


class AuthorDossierTest(unittest.TestCase):

    def test_raises_when_no_title_or_body(self):
        raw = _make_raw_story(title="", body="")
        with self.assertRaises(ValueError):
            wiki_mod.author_dossier(
                raw_story=raw, channel_cfg={}, out_path=Path("/dev/null")
            )

    def test_raises_when_no_wiki_query_derivable(self):
        raw = {"slug": "", "title": "", "body": "Some body", "source": "", "url": "", "metadata": {}}
        with self.assertRaises(ValueError):
            wiki_mod.author_dossier(
                raw_story=raw, channel_cfg={}, out_path=Path("/dev/null")
            )

    def test_raises_when_no_article_found(self):
        with patch.object(wiki_mod, "fetch_article", return_value=None):
            with patch("builtins.print"):
                with self.assertRaises(RuntimeError):
                    wiki_mod.author_dossier(
                        raw_story=_make_raw_story(),
                        channel_cfg={},
                        out_path=Path("/dev/null"),
                    )

    def test_raises_when_llm_returns_bad_shape(self):
        with patch.object(wiki_mod, "fetch_article", return_value=("T", "Article text")):
            with patch.object(wiki_mod.llm, "call_claude_cli", return_value={"no_people": True}):
                with patch("builtins.print"):
                    with self.assertRaises(ValueError):
                        wiki_mod.author_dossier(
                            raw_story=_make_raw_story(),
                            channel_cfg={},
                            out_path=Path("/dev/null"),
                        )

    def test_writes_dossier_and_returns_dict(self):
        llm_response = _make_llm_response()

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "dossiers" / "aguero.json"

            with patch.object(wiki_mod, "fetch_article", return_value=("Sergio Aguero", "A" * 100)):
                with patch.object(wiki_mod.llm, "call_claude_cli", return_value=llm_response):
                    with patch.object(wiki_mod.llm, "model_for", return_value="opus"):
                        with patch("builtins.print"):
                            result = wiki_mod.author_dossier(
                                raw_story=_make_raw_story(),
                                channel_cfg={},
                                out_path=out_path,
                            )

            self.assertTrue(out_path.exists())
            self.assertIn("people", result)
            self.assertIn("wiki", result)
            # Seeds should have been attached
            for p in result["people"]:
                self.assertIn("seed", p)

    def test_uses_wiki_query_override(self):
        llm_response = _make_llm_response()
        captured_queries = []

        def fake_fetch_article(q):
            captured_queries.append(q)
            return ("Custom Title", "Article.")

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "dossier.json"
            with patch.object(wiki_mod, "fetch_article", side_effect=fake_fetch_article):
                with patch.object(wiki_mod.llm, "call_claude_cli", return_value=llm_response):
                    with patch.object(wiki_mod.llm, "model_for", return_value="opus"):
                        with patch("builtins.print"):
                            wiki_mod.author_dossier(
                                raw_story=_make_raw_story(),
                                channel_cfg={},
                                out_path=out_path,
                                wiki_query="Man City QPR 1989",
                            )

            self.assertIn("Man City QPR 1989", captured_queries)


# ---------------------------------------------------------------------------
# CLI main()
# ---------------------------------------------------------------------------


class WikiCLITest(unittest.TestCase):

    def test_cli_runs_and_writes_output(self):
        raw_story = _make_raw_story()
        llm_response = _make_llm_response()

        with tempfile.TemporaryDirectory() as tmp:
            raw_path = Path(tmp) / "raw.json"
            out_path = Path(tmp) / "dossier.json"
            raw_path.write_text(json.dumps(raw_story))

            with patch("sys.argv", [
                "wiki",
                "--raw", str(raw_path),
                "--out", str(out_path),
            ]):
                with patch.object(wiki_mod, "fetch_article",
                                   return_value=("Aguero", "Article text")):
                    with patch.object(wiki_mod.llm, "call_claude_cli",
                                       return_value=llm_response):
                        with patch.object(wiki_mod.llm, "model_for", return_value="opus"):
                            with patch("builtins.print"):
                                wiki_mod.main()

            self.assertTrue(out_path.exists())

    def test_cli_with_channel_yaml(self):
        raw_story = _make_raw_story()
        llm_response = _make_llm_response()

        with tempfile.TemporaryDirectory() as tmp:
            raw_path = Path(tmp) / "raw.json"
            out_path = Path(tmp) / "dossier.json"
            chan_yaml = Path(tmp) / "config.yaml"
            raw_path.write_text(json.dumps(raw_story))
            chan_yaml.write_text("image_style_prefix: tifo\n")

            with patch("sys.argv", [
                "wiki",
                "--raw", str(raw_path),
                "--out", str(out_path),
                "--channel-yaml", str(chan_yaml),
            ]):
                with patch.object(wiki_mod, "fetch_article",
                                   return_value=("Aguero", "Article text")):
                    with patch.object(wiki_mod.llm, "call_claude_cli",
                                       return_value=llm_response):
                        with patch.object(wiki_mod.llm, "model_for", return_value="opus"):
                            with patch("builtins.print"):
                                wiki_mod.main()

            self.assertTrue(out_path.exists())

    def test_cli_with_wiki_query_override(self):
        raw_story = _make_raw_story()
        llm_response = _make_llm_response()

        with tempfile.TemporaryDirectory() as tmp:
            raw_path = Path(tmp) / "raw.json"
            out_path = Path(tmp) / "dossier.json"
            raw_path.write_text(json.dumps(raw_story))

            with patch("sys.argv", [
                "wiki",
                "--raw", str(raw_path),
                "--out", str(out_path),
                "--wiki-query", "Man City QPR 1989",
            ]):
                with patch.object(wiki_mod, "fetch_article",
                                   return_value=("Aguero", "Article text")):
                    with patch.object(wiki_mod.llm, "call_claude_cli",
                                       return_value=llm_response):
                        with patch.object(wiki_mod.llm, "model_for", return_value="opus"):
                            with patch("builtins.print"):
                                wiki_mod.main()


if __name__ == "__main__":
    unittest.main()


class EnvOverridesTest(unittest.TestCase):
    """Audit D3.45 / D3.46 — USER_AGENT and MAX_ARTICLE_CHARS are
    now driven by env vars. Reload the module under patched env
    to verify the overrides take effect."""

    def test_user_agent_default_when_env_unset(self):
        import importlib
        import os as _os
        env = {k: v for k, v in _os.environ.items()
               if k != "YTFACTORY_WIKI_USER_AGENT"}
        with patch.dict(_os.environ, env, clear=True):
            importlib.reload(wiki_mod)
            try:
                self.assertIn("ytFactory", wiki_mod.USER_AGENT)
                self.assertIn("channel research", wiki_mod.USER_AGENT)
            finally:
                # Restore the module as the test framework loaded it.
                importlib.reload(wiki_mod)

    def test_user_agent_env_override(self):
        import importlib
        import os as _os
        with patch.dict(_os.environ,
                         {"YTFACTORY_WIKI_USER_AGENT": "custom/1.2 (test)"}):
            importlib.reload(wiki_mod)
            try:
                self.assertEqual(wiki_mod.USER_AGENT, "custom/1.2 (test)")
            finally:
                importlib.reload(wiki_mod)

    def test_max_article_chars_default_when_env_unset(self):
        import importlib
        import os as _os
        env = {k: v for k, v in _os.environ.items()
               if k != "YTFACTORY_WIKI_MAX_ARTICLE_CHARS"}
        with patch.dict(_os.environ, env, clear=True):
            importlib.reload(wiki_mod)
            try:
                self.assertEqual(wiki_mod.MAX_ARTICLE_CHARS, 15000)
            finally:
                importlib.reload(wiki_mod)

    def test_max_article_chars_env_override(self):
        import importlib
        import os as _os
        with patch.dict(_os.environ,
                         {"YTFACTORY_WIKI_MAX_ARTICLE_CHARS": "30000"}):
            importlib.reload(wiki_mod)
            try:
                self.assertEqual(wiki_mod.MAX_ARTICLE_CHARS, 30000)
            finally:
                importlib.reload(wiki_mod)
