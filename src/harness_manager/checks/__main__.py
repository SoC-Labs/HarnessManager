"""``python -m harness_manager.checks run --plan … --board … --evidence DIR`` (docs/HIL_AUTO.md)."""

import sys

from .run import main

sys.exit(main())
