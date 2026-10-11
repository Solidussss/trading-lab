import asyncio
import pathlib
import re
import subprocess
import unittest

from tests.helpers import valid_env
from tradinglab.safety.selfcheck import SafetyCheckFailedError, run_selfcheck

ROOT = pathlib.Path(__file__).resolve().parents[1]


def check(env=None, **kw):
    kw.setdefault("system_proxies", dict)
    kw.setdefault("freeze", False)  # tests share one process-wide policy
    return run_selfcheck(valid_env() if env is None else env, **kw)


class SelfCheckTests(unittest.TestCase):
    def test_passes_with_valid_config(self):
        report = check()
        self.assertTrue(report.ok)
        names = {n for n, _, _ in report.checks}
        self.assertTrue({"network_guard_installed", "simulation_only", "brokerage_hosts_blocked",
                         "unknown_hosts_blocked", "no_proxies", "standard_asyncio_loop"} <= names)

    def test_fails_closed_on_bad_config(self):
        for env in (valid_env(TRADING_MODE="live"), {}, valid_env(ALPACA_API_KEY="x")):
            with self.subTest(env=sorted(env)), self.assertRaises(SafetyCheckFailedError):
                check(env)

    def test_fails_with_os_level_proxy(self):
        with self.assertRaises(SafetyCheckFailedError) as ctx:
            check(system_proxies=lambda: {"https": "http://corp:8080"})
        self.assertIn("no_proxies", str(ctx.exception))

    def test_fails_with_non_standard_event_loop(self):
        class FakeUvloopPolicy(asyncio.DefaultEventLoopPolicy):
            pass

        FakeUvloopPolicy.__module__ = "uvloop"
        original = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(FakeUvloopPolicy())
        try:
            with self.assertRaises(SafetyCheckFailedError) as ctx:
                check()
            self.assertIn("standard_asyncio_loop", str(ctx.exception))
        finally:
            asyncio.set_event_loop_policy(original)


class NoSecretsInRepoTests(unittest.TestCase):
    PATTERNS = [re.compile(r"\b(PK|AK)[A-Z0-9]{16,}\b"),  # broker-style key ids
                re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),  # Anthropic keys
                re.compile(r"\b[0-9a-f]{40}\b")]  # Tiingo-style hex tokens

    def _files(self):
        try:
            out = subprocess.run(  # noqa: S603, S607
                ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
            return [ROOT / p for p in out]
        except (OSError, subprocess.CalledProcessError):
            return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]

    def test_no_env_file(self):
        self.assertNotIn(".env", {p.name for p in self._files()})

    def test_no_real_looking_secrets(self):
        offenders = []
        for p in self._files():
            if not p.is_file() or p.suffix in {".png", ".jpg", ".zip"}:
                continue
            text = p.read_text(errors="ignore")
            for pat in self.PATTERNS:
                for m in pat.finditer(text):
                    tok = m.group(0)
                    if any(w in tok.lower() for w in ("fake", "test", "aaaa", "0000")):
                        continue
                    offenders.append(f"{p.relative_to(ROOT)}: {tok[:6]}...")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
