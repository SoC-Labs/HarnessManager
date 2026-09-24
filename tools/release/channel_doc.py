"""One ``channel.json`` for one (catalog, channel): load, merge, promote, withdraw, sign.

Rules the tool enforces (OTA §4.5-4.6, HARNESS-DIST §4.4-4.5):

- **Validate with the app's own parser before signing** (``schema.parse_channel``): the
  tool can never sign a channel the app would refuse.
- **The serial only goes up**: a new document is the base's serial + 1.
- **A published release is never rewritten**: adding a version that is already listed is
  refused (same bytes: "already published"; other bytes: "bump the version").
- **Never delete**: a release leaves only by ``withdraw`` (``status: withdrawn``).
- **Catalogues**: every document carries ``catalog`` (``hm-app``, ``mps3-harness``), and a
  document's anti-rollback serial is kept per (catalog, channel) by the client.
- **The base is verified**: the previous document is signature-checked (the signer's key,
  the app's pinned keys, or ``--trust-key``) before anything is added to it.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_manager.core.errors import HarnessError
from harness_manager.services.update import minisign, trust
from harness_manager.services.update.schema import (
    SCHEMA,
    SCHEMA_VERSION,
    STATUS_CURRENT,
    STATUS_SUPERSEDED,
    STATUS_WITHDRAWN,
    Channel,
    parse_channel,
)
from harness_manager.services.update.version import parse_version

from .common import EXIT_MISMATCH, EXPIRES_DAYS, ReleaseError, iso
from .signer import Signer

CATALOG_APP = "hm-app"
CATALOG_MPS3 = "mps3-harness"
SECTION = {CATALOG_APP: "app"}          # every other catalogue is a harness catalogue


def section_for(catalog: str) -> str:
    return SECTION.get(catalog, "harness")


# --- what the app's schema in THIS tree understands (OTA-C extends it) ------------------
#
# The tool emits what the current schema accepts, plus fields OTA-C will parse; the
# current parser keeps unknown fields, so they survive signing either way:
#   catalog (top level)      kept as Channel.extra until OTA-C parses it (serials per
#                            (catalog, channel), OTA §4.6)
#   artifacts[kind=dep]      ignored by the current parser; OTA-C: AppRelease deps
#   notes (a release)        kept in extra; OTA-C: the signed notes shown in the UI
#   target host-kit/rm-kit   REFUSED by the current parser: the kit is left out until K4
#   access on app assets     the current parser refuses a private app wheel: emitted as
#                            public until OTA-C allows github-token (U1)


def catalog_of(ch: Channel) -> str:
    return str(getattr(ch, "catalog", "") or ch.extra.get("catalog", ""))


def schema_allows_private_app() -> bool:
    """Does this tree's parser accept an app wheel with ``access: github-token``?"""
    probe = {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "channel": "dev", "serial": 1,
             "issued_at": "2026-01-01T00:00:00Z", "signing_key_id": "0" * 16,
             "app": {"current": "0.0.1", "releases": [{
                 "version": "0.0.1", "status": "current", "artifacts": [{
                     "kind": "wheel", "name": "harness_manager-0.0.1-py3-none-any.whl",
                     "url": "x.whl", "sha256": "0" * 64, "size": 1,
                     "access": "github-token", "repo": "o/r"}]}]}}
    try:
        parse_channel(probe)
    except HarnessError:
        return False
    return True


def schema_has_host_kit() -> bool:
    from harness_manager.services.update import schema as hm_schema

    return "host-kit" in getattr(hm_schema, "TARGETS", ())


def trust_store(public_keys: Iterable[minisign.PublicKey], *, with_pinned: bool = True,
                ) -> trust.TrustStore:
    """What the tool trusts when it reads a channel back: the given keys (release role, every
    channel) plus the app's pinned keys."""
    keys = [trust.TrustedKey(pk, trust.ROLE_RELEASE, trust.CHANNELS, "release tool")
            for pk in public_keys]
    ids = {k.id_hex for k in keys}
    if with_pinned:
        keys += [k for k in trust.PINNED_KEYS if k.id_hex not in ids]
    return trust.TrustStore(pinned=tuple(keys))


def pinned_role(public: minisign.PublicKey) -> str:
    """The role the app build in THIS tree pins ``public`` for, or '' (not pinned)."""
    return next((k.role for k in trust.PINNED_KEYS if k.id_hex == public.id_hex), "")


@dataclass
class ChannelDoc:
    catalog: str
    channel: str
    doc: dict[str, Any]
    base_serial: int = 0                   # 0 = a new channel
    base_sha256: str = ""
    notes: list[str] = field(default_factory=list)   # what changed, for the report

    # -- construction --

    @classmethod
    def new(cls, catalog: str, channel: str, *, board: dict[str, Any] | None = None) -> ChannelDoc:
        doc: dict[str, Any] = {"schema": SCHEMA, "schema_version": SCHEMA_VERSION,
                               "catalog": catalog, "channel": channel, "serial": 0,
                               "issued_at": "", "expires_at": "", "signing_key_id": ""}
        if board:
            doc["board"] = dict(board)
        return cls(catalog, channel, doc)

    @classmethod
    def load(cls, path: Path, catalog: str, channel: str,
             public_keys: Iterable[minisign.PublicKey]) -> ChannelDoc:
        """A published (or dry-run) channel, verified before it is used as a base."""
        import hashlib

        data = Path(path).read_bytes()
        sig_path = Path(path).with_name(Path(path).name + ".minisig")
        if not sig_path.is_file():
            raise ReleaseError(f"{path} has no .minisig beside it: an unsigned base is never used")
        store = trust_store(public_keys)
        try:
            store.verify_for_channel(data, sig_path.read_bytes(), channel, what=str(path))
            parsed = parse_channel(json.loads(data))
        except HarnessError as exc:
            raise ReleaseError(f"the base channel {path} does not verify: {exc.message}",
                               hint="pass the right key with --trust-key, or start from the "
                                    "live channel", code=EXIT_MISMATCH) from None
        except ValueError as exc:
            raise ReleaseError(f"the base channel {path} is not JSON: {exc}") from None
        if parsed.channel != channel:
            raise ReleaseError(f"{path} is the {parsed.channel!r} channel, not {channel!r}")
        if catalog_of(parsed) and catalog_of(parsed) != catalog:
            raise ReleaseError(f"{path} is catalogue {catalog_of(parsed)!r}, not {catalog!r}")
        doc = json.loads(data)
        doc["catalog"] = catalog
        return cls(catalog, channel, doc, base_serial=parsed.serial,
                   base_sha256=hashlib.sha256(data).hexdigest())

    @classmethod
    def load_or_new(cls, path: Path | None, catalog: str, channel: str,
                    public_keys: Iterable[minisign.PublicKey],
                    board: dict[str, Any] | None = None) -> ChannelDoc:
        if path is not None and Path(path).is_file():
            return cls.load(path, catalog, channel, public_keys)
        return cls.new(catalog, channel, board=board)

    # -- queries --

    @property
    def section(self) -> str:
        return section_for(self.catalog)

    def releases(self) -> list[dict[str, Any]]:
        return self.doc.get(self.section, {}).get("releases", [])

    def release(self, version: str) -> dict[str, Any] | None:
        v = parse_version(version)
        return next((r for r in self.releases() if parse_version(r["version"]) == v), None)

    def newest(self) -> str:
        vs = [r["version"] for r in self.releases()]
        return str(max(vs, key=parse_version)) if vs else ""

    @property
    def current(self) -> str:
        return self.doc.get(self.section, {}).get("current", "")

    # -- changes --

    def _set(self, releases: list[dict[str, Any]], current: str) -> None:
        releases = sorted(releases, key=lambda r: parse_version(r["version"]), reverse=True)
        for r in releases:
            if r.get("status") != STATUS_WITHDRAWN:
                r["status"] = STATUS_CURRENT if current and \
                    parse_version(r["version"]) == parse_version(current) else STATUS_SUPERSEDED
        sec: dict[str, Any] = {"releases": releases}
        if current:
            sec["current"] = current
        self.doc[self.section] = sec

    def add(self, entry: dict[str, Any], *, make_current: bool = True,
            allow_older: bool = False) -> None:
        """Add a NEW release. Refuses a version already listed, and (unless ``allow_older``) one
        that is not newer than every release already on this channel."""
        entry = copy.deepcopy(entry)
        version = entry["version"]
        old = self.release(version)
        if old is not None:
            same = _comparable(old) == _comparable(entry)
            raise ReleaseError(
                f"{self.catalog} {version} is already on the {self.channel!r} channel"
                + ("" if same else " with DIFFERENT content"),
                hint="nothing to publish" if same else
                     "a published release is never rewritten: bump the version")
        newest = self.newest()
        if newest and not allow_older and parse_version(version) <= parse_version(newest):
            raise ReleaseError(f"{version} is not newer than {newest}, already on the "
                               f"{self.channel!r} channel", hint="bump the version")
        if self.section == "harness":
            _derive_rekey(entry, self.releases())
        entry["status"] = STATUS_CURRENT if make_current else STATUS_SUPERSEDED
        self._set([*self.releases(), entry], version if make_current else self.current)
        self.notes.append(f"added {version}" + (" (current)" if make_current else ""))

    def promote_from(self, source: ChannelDoc, version: str) -> None:
        """Copy ``version``'s entry from ``source`` (e.g. beta) into this channel (e.g. stable),
        unchanged, and make it current. The assets are not touched: no re-upload."""
        entry = source.release(version)
        if entry is None:
            raise ReleaseError(f"{source.catalog} {version} is not on the {source.channel!r} "
                               "channel", hint="publish it there first")
        if entry.get("status") == STATUS_WITHDRAWN:
            raise ReleaseError(f"{version} is withdrawn on {source.channel!r}; not promoting it")
        waiver = (entry.get("source") or {}).get("dirty_waiver")
        if waiver and self.channel == "stable":
            raise ReleaseError(f"{version} was published from a dirty image ({waiver}); a dirty "
                               "release never reaches stable", hint="re-mint from a clean tree")
        mine = self.release(version)
        if mine is not None:
            if _comparable(mine) != _comparable(entry):
                raise ReleaseError(f"{version} is on {self.channel!r} with different content",
                                   code=EXIT_MISMATCH)
            if self.current == version:
                raise ReleaseError(f"{version} is already current on {self.channel!r}",
                                   hint="nothing to promote")
            self._set(self.releases(), version)
        else:
            e = copy.deepcopy(entry)
            if self.section == "harness":
                _derive_rekey(e, self.releases())
            self._set([*self.releases(), e], version)
        self.notes.append(f"promoted {version} from {source.channel} (current)")

    def withdraw(self, version: str, reason: str, *, new_current: str = "") -> None:
        entry = self.release(version)
        if entry is None:
            raise ReleaseError(f"{version} is not on the {self.channel!r} channel")
        if entry.get("status") == STATUS_WITHDRAWN:
            raise ReleaseError(f"{version} is already withdrawn", hint="nothing to do")
        if not reason.strip():
            raise ReleaseError("a withdrawal needs a --reason (it is shown to users)")
        entry["status"] = STATUS_WITHDRAWN
        entry["withdrawn_reason"] = reason.strip()
        current = self.current
        if current and parse_version(current) == parse_version(version):
            candidates = [r["version"] for r in self.releases()
                          if r.get("status") != STATUS_WITHDRAWN]
            if new_current:
                if new_current not in candidates:
                    raise ReleaseError(f"--current {new_current} is not a live release here")
                current = new_current
            else:
                older = [v for v in candidates if parse_version(v) < parse_version(version)]
                current = str(max(older, key=parse_version)) if older else ""
        self._set(self.releases(), current)
        self.notes.append(f"withdrew {version} ({reason.strip()}); current is now "
                          f"{current or '(none)'}")

    # -- sign + write --

    def finalize(self, signer: Signer, *, now: float | None = None,
                 expires_days: int = EXPIRES_DAYS) -> tuple[bytes, Channel]:
        """Serial + 1, times, key id; then validate with the APP's parser. Returns the bytes."""
        now = time.time() if now is None else now
        self.doc["serial"] = self.base_serial + 1
        self.doc["issued_at"] = iso(now)
        self.doc["expires_at"] = iso(now + expires_days * 86400)
        self.doc["signing_key_id"] = signer.public.id_hex
        self.doc["catalog"] = self.catalog
        data = (json.dumps(self.doc, indent=1, sort_keys=True) + "\n").encode("utf-8")
        try:
            parsed = parse_channel(json.loads(data))
        except HarnessError as exc:
            raise ReleaseError(f"the app would refuse this channel: {exc.message}",
                               hint="the tool never signs a channel the app refuses") from None
        return data, parsed

    def write_signed(self, path: Path, signer: Signer, *, now: float | None = None) -> Channel:
        data, parsed = self.finalize(signer, now=now)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(".channel.json.tmp")
        tmp.write_bytes(data)
        comment = (f"timestamp:{int(time.time() if now is None else now)}\tfile:channel.json\t"
                   f"catalog:{self.catalog}\tchannel:{self.channel}\tserial:{parsed.serial}")
        sig = signer.sign(tmp, comment)
        tmp.replace(path)
        path.with_name("channel.json.minisig").write_text(sig, encoding="utf-8")
        leftover = tmp.with_name(tmp.name + ".minisig")
        leftover.unlink(missing_ok=True)
        return parsed


def _comparable(entry: dict[str, Any]) -> str:
    """A release entry without the fields a channel rewrites around it."""
    e = {k: v for k, v in entry.items() if k not in ("status", "rekey", "withdrawn_reason")}
    if isinstance(e.get("compat"), dict):
        e["compat"] = {k: v for k, v in e["compat"].items() if k != "replaces_static_ids"}
    return json.dumps(e, sort_keys=True)


def _derive_rekey(entry: dict[str, Any], others: list[dict[str, Any]]) -> None:
    """``rekey`` + ``replaces_static_ids`` from the newest release older than ``entry``."""
    v = parse_version(entry["version"])
    older = [r for r in others if parse_version(r["version"]) < v]
    if not older:
        entry["rekey"] = False
        return
    prev = max(older, key=lambda r: parse_version(r["version"]))
    a = str(prev.get("identity", {}).get("static_id", "")).lower()
    b = str(entry.get("identity", {}).get("static_id", "")).lower()
    entry["rekey"] = bool(a and b and a != b)
    if entry["rekey"]:
        compat = dict(entry.get("compat") or {})
        compat["replaces_static_ids"] = [prev["identity"]["static_id"]]
        entry["compat"] = compat
