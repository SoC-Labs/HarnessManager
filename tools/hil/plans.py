"""``tools.hil.plans`` is ``harness_manager.checks.plans`` (HIL-GUI moved the plans into the wheel).

The module object itself, not a copy.
"""

import sys

from harness_manager.checks import plans as _plans

sys.modules[__name__] = _plans
