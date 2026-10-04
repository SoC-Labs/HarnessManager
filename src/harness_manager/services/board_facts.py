"""What the board is, read only: its revision, the MCC's firmware, the user microSD.

``facts_of_session(session, identity)`` reads the optional seams of an open board's session
and returns the ``BoardInfo.facts`` dict, or None when nothing is known:

- ``revision`` ("C"): ``storage.board_revision(boot_board=...)``: the MCC boot witness, the
  config SD's LOG.TXT ("MotherBoard Revision C Variant A"), a card with one MB/HBI0309* folder;
- ``mcc_fw`` ("v1.3.2"): ``storage.mcc_firmware(boot_fw=...)``: the boot witness, else LOG.TXT
  ("ARM V2M-MPS3 Firmware v1.3.2");
- ``user_sd`` (True / False / None): the Linux harness's card status, as last read
  (``os_slots.last_card``); None = not known yet, or no Linux.

Each ``*_tested`` flag says whether that value is one the platform was tested on. This only
CHECKS: an untested value is flagged "untested", and Harness Manager never offers an MCC update.
Nothing here raises: a failed read is a fact left unknown.
"""

from __future__ import annotations

from typing import Any

#: The revision letters and MCC firmware versions the platform was tested on (a release's
#: ``compat.board_revs`` / ``mcc_fw_tested`` say the same per release).
TESTED_REVISIONS = ("C",)
TESTED_MCC_FW = ("v1.3.2",)


def revision_letter(rev: str) -> str:
    """``C`` from ``HBI0309C`` (or ``C``); "" for nothing."""
    rev = (rev or "").strip().upper()
    return rev[-1] if rev else ""


def revision_chip(facts: dict[str, Any] | None) -> str:
    """The Overview chip text for the revision: "Rev C" / "Rev B: untested"; "" unknown."""
    f = facts or {}
    if not f.get("revision"):
        return ""
    return f"Rev {f['revision']}" + ("" if f.get("revision_tested") else ": untested")


def mcc_chip(facts: dict[str, Any] | None) -> str:
    """"MCC v1.3.2" / "MCC v1.4.1: untested"; "" unknown."""
    f = facts or {}
    if not f.get("mcc_fw"):
        return ""
    return f"MCC {f['mcc_fw']}" + ("" if f.get("mcc_fw_tested") else ": untested")


def sd_chip(facts: dict[str, Any] | None) -> str:
    """"microSD: yes" / "microSD: no"; "" when the harness has not said."""
    u = (facts or {}).get("user_sd")
    return "" if u is None else f"microSD: {'yes' if u else 'no'}"


def build(revision: str = "", revision_from: str = "", mcc_fw: str = "", mcc_fw_from: str = "",
          user_sd: bool | None = None) -> dict[str, Any] | None:
    letter = revision_letter(revision)
    fw = (mcc_fw or "").strip()
    if fw and fw[:1] not in "vV":
        fw = f"v{fw}"
    if not letter and not fw and user_sd is None:
        return None
    return {"revision": letter, "revision_from": revision_from if letter else "",
            "revision_tested": letter in TESTED_REVISIONS if letter else None,
            "mcc_fw": fw, "mcc_fw_from": mcc_fw_from if fw else "",
            "mcc_fw_tested": fw in TESTED_MCC_FW if fw else None,
            "user_sd": user_sd}


def facts_of_session(session: Any, identity: Any = None) -> dict[str, Any] | None:
    storage = getattr(session, "storage", None)
    boot = getattr(getattr(getattr(session, "controller", None), "last_reboot", None), "boot", None)
    rev = rev_from = fw = fw_from = ""
    if storage is not None:
        probe = getattr(storage, "board_revision", None)
        if callable(probe):
            try:
                rev, rev_from = probe(boot_board=str(getattr(boot, "board", "") or ""))
            except Exception:  # noqa: BLE001 - a fact that cannot be read is unknown
                rev = rev_from = ""
        probe = getattr(storage, "mcc_firmware", None)
        if callable(probe):
            try:
                fw, fw_from = probe(boot_fw=str(getattr(boot, "firmware", "") or ""))
            except Exception:  # noqa: BLE001
                fw = fw_from = ""
    user_sd = None
    if getattr(identity, "harness_impl", "") == "linux":
        card = getattr(getattr(session, "os_slots", None), "last_card", None)
        user_sd = card if isinstance(card, bool) else None
    return build(rev, rev_from, fw, fw_from, user_sd)
