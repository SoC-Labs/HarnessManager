"""``python -m harness_manager.daemon``: run harness-manager-daemon in the foreground."""

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
