"""Board-agnostic services built on board-pack adapters.

Planned modules and their owners (docs/TEAM_PLAN.md):
- console.py (T4): console broker; fan-out of single-client board ports.
- debug.py (T4): OpenOCD session manager (lifecycle, ports, orphan reaping).
- deploy.py (T2): guarded deploy pipeline (pre-flight checks, restore).
- telemetry.py (T1): reading aggregation with provenance.
- update/ (T7): channel client, signature verify, bundle install, self-update.
"""
