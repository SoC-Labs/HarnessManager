"""``python -m tools.hil``: the HIL runbooks, unattended, from a checkout (lane HIL-AUTO).

A thin command line. The runner, the plans and the reports are ``harness_manager.checks``
(in the wheel since HIL-GUI, so the service can run them: the app's Checks section,
``docs/HIL_AUTO.md`` "In the app"). ``tools.hil.run`` and ``tools.hil.plans`` are those modules
under their old names, so ``make hil-auto`` and every command in the runbooks still work.
``env_b2.sh`` (board 2's variables) stays here: it is lab data, not product.
"""
