from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from tests._helpers import PROJECT_ROOT  # noqa: F401

from pipeline.llm import anatomy_check as ac


class CheckAnatomyTest(unittest.TestCase):
    def test_ok_response_returns_clean_pass_and_forwards_options(self):
        png = PROJECT_ROOT / "tests" / "sample.png"
        with patch.object(ac.llm, "call_claude_cli", return_value={"ok": True, "reason": "fine"}) as call:
            ok, reason = ac.check_anatomy(png, model="sonnet", timeout_s=12)
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        kwargs = call.call_args.kwargs
        self.assertTrue(kwargs["output_json"])
        self.assertEqual(kwargs["allowed_tools"], ["Read"])
        self.assertEqual(kwargs["add_dirs"], [png.parent])
        self.assertEqual(kwargs["model"], "sonnet")
        self.assertEqual(kwargs["timeout_s"], 12)

    def test_failed_response_includes_truncated_reason(self):
        long_reason = "x" * 200
        with patch.object(ac.llm, "call_claude_cli", return_value={"ok": False, "reason": long_reason}):
            ok, reason = ac.check_anatomy(Path("frame.png"))
        self.assertFalse(ok)
        self.assertEqual(reason, "anatomy: " + ("x" * 120))

    def test_failed_response_without_reason_uses_default(self):
        with patch.object(ac.llm, "call_claude_cli", return_value={"ok": False, "reason": ""}):
            self.assertEqual(ac.check_anatomy(Path("frame.png")), (False, "anatomy: glitch detected"))

    def test_non_dict_and_exceptions_fail_open(self):
        with patch.object(ac.llm, "call_claude_cli", return_value=["bad"]):
            self.assertEqual(ac.check_anatomy(Path("frame.png")), (True, ""))
        with patch.object(ac.llm, "call_claude_cli", side_effect=RuntimeError("offline")):
            self.assertEqual(ac.check_anatomy(Path("frame.png")), (True, ""))


if __name__ == "__main__":
    unittest.main()
