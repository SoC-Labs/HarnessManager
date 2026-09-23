"""``python -m socharness.daemon``: run socharnessd in the foreground."""

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
