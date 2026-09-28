"""What licence building a DUT needs, in words for the guide's Tools step (lane KIT-LIC).

The facts were measured on 28 September 2026 on srv03335 with both locked statics
(platform ``docs/planning/OVERLAY_BUILD_FLOW_PROPOSAL_2026-09-28.md`` §4, branch
docs/platform-guide a654f09): no-licence probes, Vivado's ``REQUIRES_LICENSE`` per IP, the
netlists' ``CHECK_LICENSE_TYPE`` and the build logs' checkouts.

- **No IP licence** is needed for either static: every IP instance is no-charge "Included"
  IP (``IP_FREE_STATICS``).
- **The device licence** for the xcku115 is needed from **synthesis** on: ``synth_design``,
  ``opt_design``, ``place_design``, ``route_design`` and ``write_bitstream`` all stop with
  ``[Common 17-345]`` without it. ``open_checkpoint`` and ``pr_verify`` need none.
  - Vivado **2024.1**: Enterprise (the free Standard covers only the KU025/KU035).
  - Vivado **2026.1**: **Core or higher**; the free Basic has neither the KU115 nor DFX.
    2026.1 does not even start without a licence file (exit 42: ``launch.py``).

HM cannot check the device licence without synthesising, so the guide shows it as
``unchecked``, and unchecked is never a pass.
"""

from __future__ import annotations

from .schema import release_major_minor

#: The error every licensed step stops with when the device licence is missing.
WATCH = "[Common 17-345] A valid license was not found"
#: device -> {release major.minor: the Vivado edition that covers the device and DFX}.
#: Only what was measured; another device or release is said to be unknown, never guessed.
EDITIONS: dict[str, dict[str, str]] = {
    "xcku115": {"2026.1": "Core or higher", "2024.1": "Enterprise"},
}
#: The free edition of each release, which does not cover the devices in ``EDITIONS``: the
#: tier a launch reports (``launch.Launch.tier``) is compared with it.
FREE_TIER = {"2026.1": "BASIC", "2024.1": "STANDARD"}
#: The statics whose IP was checked (every instance no-charge "Included" IP; no ILA or VIO).
IP_FREE_STATICS = frozenset({0x72BB0A36, 0x44EE76D5})


def device_of(part: str) -> str:
    """``"xcku115-flvb1760-1-c"`` -> ``"xcku115"``; ``""`` for no part."""
    return (part or "").strip().split("-")[0].lower()


def _static_int(static_id: str | int | None) -> int | None:
    if static_id in (None, ""):
        return None
    try:
        return static_id if isinstance(static_id, int) else int(str(static_id), 0)
    except ValueError:
        return None


def needs(part: str, release: str, static_id: str | int | None = None) -> str:
    """What licence the build needs, worded for the kit's release: ``"a device licence for
    xcku115 is needed from synthesis on: Vivado 2026.1 Core or higher (2024.1: Enterprise);
    the static's IP needs none"``."""
    dev = device_of(part)
    rel = release_major_minor(release) if release else ""
    table = EDITIONS.get(dev)
    if not dev:
        text = "a device licence is needed from synthesis on (the kit names the part)"
    elif table is None:
        text = (f"a device licence for {dev} may be needed from synthesis on (Harness Manager "
                f"knows the editions for {', '.join(sorted(EDITIONS))} only)")
    elif rel in table:
        others = "; ".join(f"{r}: {e}" for r, e in sorted(table.items(), reverse=True)
                           if r != rel)
        text = (f"a device licence for {dev} is needed from synthesis on: Vivado {rel} "
                f"{table[rel]}" + (f" ({others})" if others else ""))
    else:
        each = ", ".join(f"{r} {e}" for r, e in sorted(table.items(), reverse=True))
        text = (f"a device licence for {dev} is needed from synthesis on: Vivado {each}"
                + (f"; the edition for {rel} was not checked" if rel else ""))
    sid = _static_int(static_id)
    if sid is not None:
        text += ("; the static's IP needs none" if sid in IP_FREE_STATICS else
                 "; the static's IP licences were not checked")
    return text


def line(part: str, release: str, static_id: str | int | None = None) -> str:
    """The guide's licence line. It starts ``licence: unchecked (`` (the web UI's Tools card
    reads what follows ``; licence: ``), and it names the error to watch for."""
    return (f"licence: unchecked ({needs(part, release, static_id)}; unchecked is not a pass: "
            f"without it synthesis stops with {WATCH}; point XILINXD_LICENSE_FILE at the lab's "
            "licence server)")


def tier_note(part: str, release: str, tier: str) -> str:
    """What the licence tier a launch reported means for the device, or ``""``. Only the free
    tier of a measured release is known not to cover a device in ``EDITIONS``."""
    dev = device_of(part)
    rel = release_major_minor(release) if release else ""
    if not tier or dev not in EDITIONS or FREE_TIER.get(rel) != tier.strip().upper():
        return ""
    need = EDITIONS[dev].get(rel, "")
    return (f"the {tier.strip().title()} tier does not cover the {dev}"
            + (f" (it needs {need})" if need else "")
            + f": synthesis will stop with {WATCH}")
