"""``python -m server`` runs the headless server (same as ``megachat serve``)."""

import sys

from server.main import serve

if __name__ == "__main__":
    sys.exit(serve())
