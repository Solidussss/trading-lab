import pathlib
import re
import subprocess
import unittest

from tests.helpers import valid_env
from tradinglab.safety.selfcheck import SafetyCheckFailedError, run_selfcheck

ROOT = pathlib.Path(__file__).resolve().parents[1]


class SelfCheckTests(unittest.TestCase):
    def test_passes_with_valid_paper_config(self):
        report = run_selfcheck(valid_env())
        self.assertTrue(report.ok)
        names = {name for name, _, _ in report.checks}
        self.assertTrue({"network_guard_installed", "trading_url_is_paper",
                         "live_hosts_blocked"} <= names)
        status = dict((n, d) for n, _, d in report.checks)
        self.assertEqual(status["order_submission"], "disabled (dry run)")

    def test_fails_closed_on_bad_config(self):
        with self.assertRaises(SafetyCheckFailedError):
            run_selfcheck(valid_env(TRADING_MODE="live"))
        with self.assertRaises(SafetyCheckFailedError):
            run_selfcheck({})


class NoSecretsInRepoTests(unittest.TestCase):
    # Alpaca key ids are ~20 uppercase alphanumerics starting PK/AK; secrets ~40 chars.
    KEY_RE = re.compile(r"\b(PK|AK)[A-Z0-9]{16,}\b")

    def _tracked_or_all_files(self):
        try:
            out = subprocess.run(  # noqa: S603, S607
                ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                cwd=ROOT, capture_output=True, text=True, check=True,
            ).stdout.split()
            return [ROOT / p for p in out]
        except (OSError, subprocess.CalledProcessError):
            return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]

    def test_no_env_file_committed(self):
        names = {p.name for p in self._tracked_or_all_files()}
        self.assertNotIn(".env", names)

    def test_no_real_looking_keys(self):
        offenders = []
        for p in self._tracked_or_all_files():
            if p.suffix in {".png", ".jpg", ".ico", ".zip"} or not p.is_file():
                continue
            text = p.read_text(errors="ignore")
            for m in self.KEY_RE.finditer(text):
                if "FAKE" in m.group(0) or "TEST" in m.group(0) or "LIVE" in m.group(0):
                    continue  # obvious test fixtures
                offenders.append(f"{p.relative_to(ROOT)}: {m.group(0)[:4]}...")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
