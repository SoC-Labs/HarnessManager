"""Spike (lane OTA): hold a board session lock from a live process, as a running deploy would.

    python ota_hold_lock.py BOARD_ID SECONDS

Uses the installed app's own ``SessionLock`` (so the lock file is exactly what a CLI
session writes under ``$HARNESS_MANAGER_STATE_DIR/locks``). T7's ``LocalBusyProbe``
must then refuse the app switch.
"""

import sys
import time

from harness_manager.core.session import SessionLock

lock = SessionLock(sys.argv[1], note="ota spike: a long deploy")
lock.acquire()
print(f"holding {lock.path}", flush=True)
try:
    time.sleep(float(sys.argv[2]))
finally:
    lock.release()
