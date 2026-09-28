"""``python -m tools.hil run --plan … --board … --evidence DIR`` (docs/HIL_AUTO.md).

The same as ``python -m harness_manager.checks``: with a Harness Manager service running for
this state dir the run goes through it (the service holds the board and the lease); without
one, in this process (the CLI's in-process engine)."""

import sys

from harness_manager.checks.run import main

sys.exit(main())
