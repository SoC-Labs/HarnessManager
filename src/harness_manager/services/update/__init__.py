"""Updates from GitHub, safely: the harness (two targets) and the app itself. Team T7.

Modules:

- ``minisign``: Ed25519 minisign signatures (verify; sign for tooling/tests);
- ``trust``: pinned keys, per-channel roles, root-signed ``keys.json`` rotation;
- ``schema``: ``channel.json`` v1, strictly validated;
- ``channel``: fetch + verify (signature, schema, channel name, serial, expiry);
- ``download``: resumable, sha256-checked, cached; the GitHub token for private assets;
- ``bitheader``: the Xilinx ``.bit`` header (part, USERID);
- ``bundle``: safe extraction and the domain checks;
- ``planner``: board vs channel -> a plan (steps, warnings, blockers, re-key consent);
- ``executor``: the approved install (backup -> install -> witnessed reboot -> identity) and rollback;
- ``os_slots``: the OS A/B slot seam for Linux harnesses (interface only);
- ``app``: side-by-side ``uv`` venvs, switch pointer, rollback, busy refusal;
- ``service``: ``UpdateService``, what the front-ends use.

Safety rails (code, not docs): signature, then serial, then every asset's
sha256, then the domain checks; never an SD write without a verified backup
(``session.storage`` enforces it); never an ``.ebf`` or MCC command file; never
an install without an approved plan; never "installed" until the board reports
the new identity after a witnessed reboot.
"""

from .channel import ChannelClient, VerifiedChannel, channel_url
from .executor import (
    RESULT_INSTALLED,
    RESULT_RESTORED,
    RESULT_RESTORED_UNCONFIRMED,
    RESULT_STORED,
    RESULT_UP_TO_DATE,
    RESULT_WRITTEN,
    HarnessInstaller,
    UpdateOutcome,
    confirm_identity,
)
from .planner import Approval, BoardView, Plan, make_plan
from .schema import Channel, parse_channel
from .service import UpdateService

__all__ = [
    "RESULT_INSTALLED", "RESULT_RESTORED", "RESULT_RESTORED_UNCONFIRMED", "RESULT_STORED",
    "RESULT_UP_TO_DATE", "RESULT_WRITTEN",
    "Approval", "BoardView", "Channel", "ChannelClient", "HarnessInstaller", "Plan",
    "UpdateOutcome", "UpdateService", "VerifiedChannel", "channel_url", "confirm_identity",
    "make_plan", "parse_channel",
]
