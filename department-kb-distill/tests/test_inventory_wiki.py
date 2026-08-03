from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from inventory_wiki import run_dws  # noqa: E402


class DwsRetryTests(unittest.TestCase):
    def test_retry_with_verbose_after_transient_failure(self) -> None:
        failed = SimpleNamespace(returncode=1, stdout="", stderr="temporary failure")
        succeeded = SimpleNamespace(returncode=0, stdout='{"success": true, "nodes": []}', stderr="")
        with patch("inventory_wiki.subprocess.run", side_effect=[failed, succeeded]) as mocked:
            payload = run_dws(["doc", "list", "--workspace", "SPACE1"])

        self.assertTrue(payload["success"])
        self.assertEqual(mocked.call_count, 2)
        self.assertNotIn("--verbose", mocked.call_args_list[0].args[0])
        self.assertIn("--verbose", mocked.call_args_list[1].args[0])

    def test_second_failure_keeps_command_and_real_error(self) -> None:
        failed = SimpleNamespace(returncode=1, stdout="", stderr="service unavailable")
        with patch("inventory_wiki.subprocess.run", side_effect=[failed, failed]):
            with self.assertRaisesRegex(RuntimeError, "SPACE1.*service unavailable"):
                run_dws(["doc", "list", "--folder", "SPACE1"])


if __name__ == "__main__":
    unittest.main()
