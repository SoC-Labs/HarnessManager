"""The HIL runbooks as unattended checks (lanes HIL-AUTO, HIL-GUI).

- ``plans``: ``docs/HIL_LINUX.md`` and ``docs/HIL_B0.md`` as data, one ``Check`` per runbook
  check, and ``auto_plan``: which plan fits a board;
- ``run``: the runner (``Runner``) and its command line (``python -m harness_manager.checks``,
  ``python -m tools.hil``). It drives Harness Manager's own CLI, one subprocess per check, and
  writes the evidence and ``REPORT.md``;
- ``harness_manager.services.hil_runs``: the service's run manager (one run per board, the
  lease held for the run's length), behind ``daemon/hil_api.py`` and the app's Checks section.

In the wheel (not only in ``tools/hil``) because the service runs them; ``docs/HIL_AUTO.md``
says how.
"""
