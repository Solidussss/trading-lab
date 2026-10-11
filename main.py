"""Railway entry point: runs the Trading Lab web service (simulation only)."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from tradinglab.web.server import serve  # noqa: E402

if __name__ == "__main__":
    serve()
