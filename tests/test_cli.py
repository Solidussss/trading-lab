import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tradinglab.research import cli


class BaselineCliSmokeTest(unittest.TestCase):
    def test_synthetic_baseline_writes_report_without_touching_test(self):
        tiny = {"fast": [50], "slow": [150, 200], "atr_period": [20], "atr_mult": [3.0]}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cli, "GRID", tiny):
            rc = cli.main(["baseline", "--synthetic", "--reports", tmp])
            self.assertEqual(rc, 0)
            md = (Path(tmp) / "baseline_synthetic.md").read_text()
            payload = json.loads((Path(tmp) / "baseline_synthetic.json").read_text())
        self.assertIn("SYNTHETIC DATA", md)
        self.assertIn("**untouched**", md)
        self.assertTrue(payload["synthetic"])
        self.assertNotIn("TEST (one-time)", payload["metrics"])

    def test_final_test_requires_reason(self):
        with self.assertRaises(SystemExit):
            cli.main(["baseline", "--synthetic", "--final-test"])


if __name__ == "__main__":
    unittest.main()
