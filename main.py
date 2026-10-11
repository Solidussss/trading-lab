"""Railway entry point (simulation only).

    python main.py           web service: dashboard + in-app engine scheduler
    python main.py nightly   run one engine check and exit (optional Railway cron service)
"""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

if __name__ == "__main__":
    if sys.argv[1:] == ["nightly"]:
        from tradinglab.engine.runtime import run_nightly_once

        sys.exit(run_nightly_once(os.environ))
    from tradinglab.web.server import serve

    serve()
