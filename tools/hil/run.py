"""``tools.hil.run`` is ``harness_manager.checks.run`` (HIL-GUI moved the runner into the wheel).

The module object itself, not a copy: a test that patches ``tools.hil.run`` patches the runner.
"""

import sys

from harness_manager.checks import run as _run

sys.modules[__name__] = _run
