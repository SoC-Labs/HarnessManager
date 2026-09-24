"""``channel.json`` v1: the signed index of harness and app releases on one channel.

Validation is strict: a wrong type, a missing required field, a malformed id,
hash or version, or a contradiction (an Arm-IP asset marked public, a ``current``
that names no release) refuses the whole channel. Unknown fields are ignored
but KEPT (``extra`` on every object, and ``Channel.raw``), so a newer publisher
can add fields without breaking older apps.

Shape (every field below is validated; see also the sample in the hand-back)::

    {
      "schema": "harness-manager-channel", "schema_version": 1,
      "channel": "stable",                 # must be the channel the app asked for
      "serial": 12,                        # strictly increasing per channel (anti-rollback)
      "issued_at": "2026-10-02T10:00:00Z",
      "expires_at": "2027-04-01T00:00:00Z", # the app WARNS after it, never blocks (offline labs)
      "signing_key_id": "E7620F1842B4E81F", # must be the key that signed this file
      "catalog": "mps3-harness",           # OTA-C: which catalogue (app, or one per board pack)
      "board": {"pack": "mps3", "part": "xcku115", "revisions": ["HBI0309C"]},
      "harness": {"current": "1.1.0", "releases": [<harness release>, ...]},
      "app": {"current": "0.2.0", "releases": [<app release>, ...]}
    }

A harness release::

    {"version": "1.1.0", "status": "current|superseded|withdrawn",
     "identity": {"static_id": "0x3F1A560F", "usercode": "0xD46FCDCB", "harness": "1.0.0",
                  "impl": "bare-metal|linux", "proto": "0.11", "features": [...],
                  "fw_sha": "d68dd0ed", "ver32": "0x01000000", "usr_access": "0x01000000"},
     "compat": {"min_app": "0.1.0", "board_revs": ["HBI0309C"], "mcc_fw_tested": ["1.3.2"],
                "net_protocol": "0.11", "replaces_static_ids": ["0xA8C1C535"]},
     "rekey": false, "vivado": "2024.1", "notes": "signed release notes (plain text)",
     "components": [{"name": "sd-HBI0309C", "target": "mcc_sd|ethernet|host-store|host-kit",
                     "kind": "sd|os-slot|legal-info|overlays|openocd|identity|dutfw|other|rm-kit",
                     "url": "...", "sha256": "...", "size": 1170000,
                     "ip_class": "open|arm-aaa|unknown", "access": "public|github-token",
                     "files": {"MB/HBI0309C/Nanosoc/nanosoc.bit": "<sha256>", ...}}]}

**Targets (doors).** ``mcc_sd`` and ``ethernet`` are the platform's Linux-bundle names
(FLOW_CONTRACT v1.6, ``fpga/dfx/tools/linux_bundle.py``): the MCC config SD, and what a
running harness takes over the network (the OS slot image). HM's older ``mcc-sd`` and
``user-usd`` are accepted and read as them. ``host-store`` is HM's content store
(overlays, openocd cfg, ...). ``host-kit`` carries the DUT build kit (kind ``rm-kit``,
KIT-STORE K4): fetched on demand by the kit service, NEVER by a harness install.

An app release::

    {"version": "0.2.0", "status": "current", "requires_python": ">=3.10", "notes": "...",
     "artifacts": [{"kind": "wheel", "name": "harness_manager-0.2.0-py3-none-any.whl", ...},
                   {"kind": "dep", "name": "mps3_pyverify-0.1.0-py3-none-any.whl", ...},
                   {"kind": "lock", "extras": ["app"],
                    "name": "harness_manager-0.2.0.app.lock.txt", ...}],
     "lock": {"name": "harness_manager-0.2.0.lock.txt", "extras": ["serial", "ina260"], ...}}

A ``dep`` is a wheel the lock needs that no package index has (pyverify): downloaded and
hash-checked like the wheel, then the lock is pinned to the verified local file.

**Locks per extras set** (OTA M6): ``lock`` is the base lock (its optional ``extras`` names
the extras it covers, the always-on small ones). A ``kind: lock`` artifact is a complete
lock for a larger extras set (``app``: pywebview). The stage takes the smallest lock that
covers the extras the install recorded (``AppRelease.lock_for``), else the base one.

**Catalogues** (HM_SELF_UPDATE §4.6, HARNESS_DISTRIBUTION §4.0): ``catalog`` names the
catalogue a document belongs to: ``hm-app`` (the app), or ``<pack>-harness`` (one per
board pack). Anti-rollback serials are kept per (catalogue, channel). A document without
the field gets one derived from its content (``derive_catalog``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from harness_manager.core.errors import IncompatibleError, RefusedError

from .version import is_version, parse_version

SCHEMA = "harness-manager-channel"
SCHEMA_VERSION = 1

#: The doors, named as the platform's Linux bundle names them (FLOW_CONTRACT v1.6 §0).
TARGET_MCC_SD = "mcc_sd"              # the MCC config SD (the static + baked boot code)
TARGET_ETHERNET = "ethernet"          # over the network to a running harness (OS slot)
TARGET_HOST_STORE = "host-store"      # HM's content store (overlays, openocd, ...)
TARGET_HOST_KIT = "host-kit"          # the DUT build kit: fetched on demand, never deployed
TARGET_USER_USD = TARGET_ETHERNET     # the old name of the OS-slot door (kept for importers)
TARGETS = (TARGET_MCC_SD, TARGET_ETHERNET, TARGET_HOST_STORE, TARGET_HOST_KIT)
#: HM's older spellings, read as the platform's.
TARGET_ALIASES = {"mcc-sd": TARGET_MCC_SD, "user-usd": TARGET_ETHERNET}
#: ``linux_bundle.json``'s ``targets`` keys -> the door a component goes through.
LINUX_BUNDLE_TARGETS = {"mcc_sd": TARGET_MCC_SD, "ethernet": TARGET_ETHERNET}

KIND_SD = "sd"
KIND_OS_SLOT = "os-slot"
KIND_LEGAL_INFO = "legal-info"        # the Buildroot legal-info that travels with an OS image
KIND_OVERLAYS = "overlays"
KIND_RM_KIT = "rm-kit"                # KIT-STORE's DUT build kit (kit.json v1 inside)
KINDS_BY_TARGET: dict[str, tuple[str, ...]] = {
    TARGET_MCC_SD: (KIND_SD,),
    TARGET_ETHERNET: (KIND_OS_SLOT, KIND_LEGAL_INFO),
    TARGET_HOST_STORE: (KIND_OVERLAYS, "openocd", "identity", "dutfw", "other"),
    TARGET_HOST_KIT: (KIND_RM_KIT,),
}
DEFAULT_KIND = {TARGET_MCC_SD: KIND_SD, TARGET_ETHERNET: KIND_OS_SLOT,
                TARGET_HOST_STORE: KIND_OVERLAYS, TARGET_HOST_KIT: KIND_RM_KIT}


def normalise_target(target: str) -> str:
    """A target in its canonical (platform) spelling; unknown names come back unchanged."""
    return TARGET_ALIASES.get(target, target)


#: The app's catalogue, and the harness catalogue of a board pack.
CATALOG_APP = "hm-app"
CATALOG_HARNESS_SUFFIX = "-harness"
#: Before catalogues, the one channel a client read was the MPS3 platform channel (T7's
#: default source, ``board.pack`` mps3): its serials are this catalogue's history.
LEGACY_CATALOG = "mps3" + CATALOG_HARNESS_SUFFIX
_CATALOG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def harness_catalog(pack: str) -> str:
    """The harness catalogue id of a board pack: ``mps3`` -> ``mps3-harness``."""
    return f"{(pack or 'mps3').lower()}{CATALOG_HARNESS_SUFFIX}"

ACCESS_PUBLIC = "public"
ACCESS_TOKEN = "github-token"
ACCESSES = (ACCESS_PUBLIC, ACCESS_TOKEN)

IP_OPEN = "open"
IP_ARM = "arm-aaa"
IP_CLASSES = (IP_OPEN, IP_ARM, "unknown")
_IP_ALIASES = {"arm_aaa": IP_ARM, "aaa": IP_ARM}

STATUS_CURRENT = "current"
STATUS_SUPERSEDED = "superseded"
STATUS_WITHDRAWN = "withdrawn"
STATUSES = (STATUS_CURRENT, STATUS_SUPERSEDED, STATUS_WITHDRAWN)

IMPLS = ("bare-metal", "linux")

_HEX32_RE = re.compile(r"^0[xX][0-9A-Fa-f]{8}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_KEY_ID_RE = re.compile(r"^[0-9A-Fa-f]{16}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
_URL_SCHEMES = ("https://", "http://")


class ChannelFormatError(RefusedError):
    """The channel is signed but malformed: refused whole, never partly used."""


# --- small validators ------------------------------------------------------------------


def _fail(where: str, what: str) -> ChannelFormatError:
    return ChannelFormatError(f"channel.json {where}: {what}",
                              hint="the channel is malformed; report it to the publisher")


def _obj(v: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(v, Mapping):
        raise _fail(where, "must be an object")
    return v


def _str(d: Mapping[str, Any], key: str, where: str, *, required: bool = True,
         default: str = "") -> str:
    if key not in d:
        if required:
            raise _fail(where, f"missing required field {key!r}")
        return default
    v = d[key]
    if not isinstance(v, str):
        raise _fail(f"{where}.{key}", "must be a string")
    return v


def _int(d: Mapping[str, Any], key: str, where: str, *, minimum: int = 0,
         required: bool = True, default: int = 0) -> int:
    if key not in d:
        if required:
            raise _fail(where, f"missing required field {key!r}")
        return default
    v = d[key]
    if not isinstance(v, int) or isinstance(v, bool) or v < minimum:
        raise _fail(f"{where}.{key}", f"must be an integer >= {minimum}")
    return v


def _bool(d: Mapping[str, Any], key: str, where: str, default: bool = False) -> bool:
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise _fail(f"{where}.{key}", "must be true or false")
    return v


def _str_list(d: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    v = d.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise _fail(f"{where}.{key}", "must be a list of strings")
    return tuple(v)


def _hex32(v: str, where: str, *, allow_empty: bool = False) -> str:
    if allow_empty and v == "":
        return ""
    if not _HEX32_RE.match(v):
        raise _fail(where, f"{v!r} is not a 32-bit hex id like 0x3F1A560F")
    return "0x" + v[2:].lower()


def _version(v: str, where: str, *, allow_empty: bool = False) -> str:
    if allow_empty and v == "":
        return ""
    if not is_version(v):
        raise _fail(where, f"{v!r} is not a version")
    return v


def parse_time(v: str, where: str) -> float:
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise _fail(where, f"{v!r} is not an ISO-8601 time") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _url(v: str, where: str) -> str:
    if not v:
        raise _fail(where, "is empty")
    if "://" in v and not v.startswith(_URL_SCHEMES):
        raise _fail(where, f"{v!r}: only https://, http:// or a path relative to the channel")
    if v.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", v):
        raise _fail(where, f"{v!r}: a relative URL is relative to channel.json, not absolute")
    if any(c in v for c in "\r\n\t "):
        raise _fail(where, f"{v!r} contains whitespace")
    return v


def _extra(d: Mapping[str, Any], known: set[str]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k not in known}


# --- the model -------------------------------------------------------------------------


@dataclass(frozen=True)
class Asset:
    """A downloadable file: where it is and what it must hash to."""

    name: str
    url: str
    sha256: str
    size: int
    access: str = ACCESS_PUBLIC
    repo: str = ""                        # the private repo for access=github-token


@dataclass(frozen=True)
class Component:
    name: str
    target: str
    kind: str
    asset: Asset
    ip_class: str = IP_OPEN
    fmt: str = "zip"                      # "zip" (extracted) or "raw" (used as one file)
    files: dict[str, str] = field(default_factory=dict)   # path inside -> sha256
    optional: bool = False
    static_id: str = ""                   # rm-kit: the static the kit links against
    vivado: str = ""                      # rm-kit: the Vivado release the kit needs
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_token(self) -> bool:
        return self.asset.access == ACCESS_TOKEN


@dataclass(frozen=True)
class HarnessIdentity:
    static_id: str
    usercode: str = ""
    harness: str = ""
    impl: str = ""
    proto: str = ""
    features: tuple[str, ...] = ()
    fw_sha: str = ""
    ver32: str = ""                       # HARNESS_VER32 the firmware reports (``version.ver32``)
    usr_access: str = ""                  # the fabric's USR_ACCESS the release expects
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Compat:
    min_app: str = ""
    board_revs: tuple[str, ...] = ()
    mcc_fw_tested: tuple[str, ...] = ()
    net_protocol: str = ""
    replaces_static_ids: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessRelease:
    version: str
    status: str
    identity: HarnessIdentity
    compat: Compat
    components: tuple[Component, ...]
    rekey: bool = False
    released_at: str = ""
    notes_url: str = ""
    vivado: str = ""                      # the Vivado release this static was built with
    notes: str = ""                       # signed release notes (plain text, shown inline)
    extra: dict[str, Any] = field(default_factory=dict)

    def by_target(self, target: str) -> list[Component]:
        want = normalise_target(target)
        return [c for c in self.components if c.target == want]

    def kits(self) -> list[Component]:
        """The DUT build kits this release carries (``host-kit``/``rm-kit``)."""
        return [c for c in self.components if c.kind == KIND_RM_KIT]

    def component(self, name: str) -> Component | None:
        return next((c for c in self.components if c.name == name), None)


@dataclass(frozen=True)
class ExtrasLock:
    """A complete hashed lock for one set of extras (``kind: lock`` artifact)."""

    asset: Asset
    extras: tuple[str, ...]               # normalised, sorted


@dataclass(frozen=True)
class AppRelease:
    version: str
    status: str
    wheel: Asset
    lock: Asset | None = None
    requires_python: str = ""
    min_harness: str = ""
    released_at: str = ""
    notes_url: str = ""
    deps: tuple[Asset, ...] = ()          # wheels no index has (pyverify), hash-pinned
    notes: str = ""                       # signed release notes (plain text, shown inline)
    lock_extras: tuple[str, ...] = ()     # the extras the base ``lock`` covers, when it says
    extra_locks: tuple[ExtrasLock, ...] = ()   # complete locks for larger extras sets (M6)
    extra: dict[str, Any] = field(default_factory=dict)

    def lock_for(self, extras: Iterable[str] = ()) -> Asset | None:
        """The lock to stage with for an install that recorded ``extras``: the smallest
        extras lock that covers them all, else the base lock (the stage then reports the
        extras it could not keep)."""
        want = {normalise_dist(e) for e in extras}
        if want and not want <= set(self.lock_extras):
            fits = [lk for lk in self.extra_locks if want <= set(lk.extras)]
            if fits:
                return min(fits, key=lambda lk: (len(lk.extras), lk.extras)).asset
        return self.lock

    def assets(self) -> list[Asset]:
        """Every file of the release: wheel, deps, locks."""
        return [self.wheel, *self.deps, *([self.lock] if self.lock else []),
                *(lk.asset for lk in self.extra_locks)]


@dataclass(frozen=True)
class BoardSpec:
    pack: str = ""
    part: str = ""
    revisions: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Channel:
    channel: str
    serial: int
    issued_at: str
    signing_key_id: str
    board: BoardSpec
    harness_current: str
    harness: tuple[HarnessRelease, ...]
    app_current: str
    app: tuple[AppRelease, ...]
    expires_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    extra: dict[str, Any] = field(default_factory=dict)
    catalog: str = ""                     # as the document states it ("" = not stated)

    @property
    def catalog_id(self) -> str:
        """The catalogue this document belongs to: stated, else derived (``derive_catalog``)."""
        return self.catalog or derive_catalog(self.board.pack, has_harness=bool(self.harness),
                                              has_app=bool(self.app))

    def harness_release(self, version: str | None = None) -> HarnessRelease | None:
        want = version or self.harness_current
        if not want:
            return None
        v = parse_version(want)
        return next((r for r in self.harness if parse_version(r.version) == v), None)

    def app_release(self, version: str | None = None) -> AppRelease | None:
        want = version or self.app_current
        if not want:
            return None
        v = parse_version(want)
        return next((r for r in self.app if parse_version(r.version) == v), None)

    def expired(self, now: float) -> bool:
        return bool(self.expires_at) and parse_time(self.expires_at, "expires_at") < now


def derive_catalog(pack: str, *, has_harness: bool, has_app: bool) -> str:
    """The catalogue of a document that does not state one.

    A harness document belongs to its board pack's catalogue (``mps3-harness``; T7's
    combined app + harness channel was the MPS3 platform channel, so it lands there too);
    an app-only document to ``hm-app``.
    """
    if has_app and not has_harness:
        return CATALOG_APP
    return harness_catalog(pack)


# --- parsers ---------------------------------------------------------------------------


def _asset(d: Mapping[str, Any], where: str, default_name: str = "") -> Asset:
    access = _str(d, "access", where, required=False, default=ACCESS_PUBLIC)
    if access not in ACCESSES:
        raise _fail(f"{where}.access", f"{access!r} is not one of {ACCESSES}")
    sha = _str(d, "sha256", where)
    if not _SHA_RE.match(sha):
        raise _fail(f"{where}.sha256", f"{sha!r} is not a sha256 digest")
    repo = _str(d, "repo", where, required=False)
    name = _str(d, "name", where, required=False, default=default_name) or default_name
    url = _url(_str(d, "url", where), f"{where}.url")
    if not name:
        name = url.rstrip("/").rsplit("/", 1)[-1]
    if not _NAME_RE.match(name):
        raise _fail(f"{where}.name", f"{name!r} is not a plain file name")
    return Asset(name=name, url=url, sha256=sha.lower(), size=_int(d, "size", where, minimum=1),
                 access=access, repo=repo)


_COMPONENT_KEYS = {"name", "target", "kind", "url", "sha256", "size", "ip_class", "access", "repo",
                   "format", "files", "optional", "static_id", "vivado"}
_RAW_KINDS = (KIND_OS_SLOT, KIND_LEGAL_INFO)


def _component(d: Any, where: str) -> Component:
    d = _obj(d, where)
    name = _str(d, "name", where)
    if not _NAME_RE.match(name):
        raise _fail(f"{where}.name", f"{name!r} is not a plain name")
    target = normalise_target(_str(d, "target", where))
    if target not in TARGETS:
        raise _fail(f"{where}.target", f"{target!r} is not one of {TARGETS}")
    kind = _str(d, "kind", where, required=False, default=DEFAULT_KIND[target])
    if kind not in KINDS_BY_TARGET[target]:
        raise _fail(f"{where}.kind", f"{kind!r} cannot go to target {target!r} "
                                     f"(allowed: {KINDS_BY_TARGET[target]})")
    ip_raw = _str(d, "ip_class", where, required=False, default=IP_OPEN)
    ip_class = _IP_ALIASES.get(ip_raw, ip_raw)
    if ip_class not in IP_CLASSES:
        raise _fail(f"{where}.ip_class", f"{ip_raw!r} is not one of {IP_CLASSES}")
    asset = _asset(d, where, default_name="")
    if ip_class == IP_ARM and asset.access != ACCESS_TOKEN:
        raise _fail(where, "an arm-aaa component must be access 'github-token', never public "
                           "(Arm Academic Access IP is not redistributable)")
    fmt = _str(d, "format", where, required=False,
               default="raw" if kind in _RAW_KINDS else "zip")
    if fmt not in ("zip", "raw"):
        raise _fail(f"{where}.format", f"{fmt!r} is not 'zip' or 'raw'")
    if kind in (KIND_SD, KIND_OVERLAYS, KIND_RM_KIT) and fmt != "zip":
        raise _fail(f"{where}.format", f"a {kind} component is a zip")
    static_id = _hex32(_str(d, "static_id", where, required=False), f"{where}.static_id",
                       allow_empty=True)
    vivado = _str(d, "vivado", where, required=False)
    files_raw = d.get("files", {})
    if not isinstance(files_raw, Mapping):
        raise _fail(f"{where}.files", "must be an object of path -> sha256")
    files: dict[str, str] = {}
    for path, sha in files_raw.items():
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise _fail(f"{where}.files[{path!r}]", "is not a sha256 digest")
        files[str(path)] = sha.lower()
    return Component(name=name, target=target, kind=kind, asset=asset, ip_class=ip_class,
                     fmt=fmt, files=files, optional=_bool(d, "optional", where),
                     static_id=static_id, vivado=vivado, extra=_extra(d, _COMPONENT_KEYS))


_IDENTITY_KEYS = {"static_id", "usercode", "harness", "impl", "proto", "features", "fw_sha",
                  "ver32", "usr_access"}


def _identity(d: Any, where: str) -> HarnessIdentity:
    d = _obj(d, where)
    impl = _str(d, "impl", where, required=False)
    if impl and impl not in IMPLS:
        raise _fail(f"{where}.impl", f"{impl!r} is not one of {IMPLS}")
    return HarnessIdentity(
        static_id=_hex32(_str(d, "static_id", where), f"{where}.static_id"),
        usercode=_hex32(_str(d, "usercode", where, required=False), f"{where}.usercode",
                        allow_empty=True),
        harness=_version(_str(d, "harness", where, required=False), f"{where}.harness",
                         allow_empty=True),
        impl=impl,
        proto=_str(d, "proto", where, required=False),
        features=_str_list(d, "features", where),
        fw_sha=_str(d, "fw_sha", where, required=False),
        ver32=_hex32(_str(d, "ver32", where, required=False), f"{where}.ver32",
                     allow_empty=True),
        usr_access=_hex32(_str(d, "usr_access", where, required=False), f"{where}.usr_access",
                          allow_empty=True),
        extra=_extra(d, _IDENTITY_KEYS),
    )


_COMPAT_KEYS = {"min_app", "board_revs", "mcc_fw_tested", "net_protocol", "replaces_static_ids"}


def _compat(d: Any, where: str) -> Compat:
    d = _obj(d if d is not None else {}, where)
    return Compat(
        min_app=_version(_str(d, "min_app", where, required=False), f"{where}.min_app",
                         allow_empty=True),
        board_revs=_str_list(d, "board_revs", where),
        mcc_fw_tested=_str_list(d, "mcc_fw_tested", where),
        net_protocol=_str(d, "net_protocol", where, required=False),
        replaces_static_ids=tuple(_hex32(s, f"{where}.replaces_static_ids")
                                  for s in _str_list(d, "replaces_static_ids", where)),
        extra=_extra(d, _COMPAT_KEYS),
    )


_HRELEASE_KEYS = {"version", "status", "identity", "compat", "components", "rekey",
                  "released_at", "notes_url", "vivado", "notes"}


def _status(d: Mapping[str, Any], where: str) -> str:
    status = _str(d, "status", where)
    if status not in STATUSES:
        raise _fail(f"{where}.status", f"{status!r} is not one of {STATUSES}")
    return status


def _harness_release(d: Any, where: str) -> HarnessRelease:
    d = _obj(d, where)
    comps_raw = d.get("components")
    if not isinstance(comps_raw, list) or not comps_raw:
        raise _fail(f"{where}.components", "must be a non-empty list")
    comps = tuple(_component(c, f"{where}.components[{i}]") for i, c in enumerate(comps_raw))
    names = [c.name for c in comps]
    if len(set(names)) != len(names):
        raise _fail(f"{where}.components", "component names must be unique")
    if len([c for c in comps if c.target == TARGET_MCC_SD]) > 1:
        raise _fail(f"{where}.components", "at most one mcc-sd component per release")
    if len([c for c in comps if c.kind == KIND_OS_SLOT]) > 1:
        raise _fail(f"{where}.components", "at most one os-slot component per release")
    identity = _identity(d.get("identity"), f"{where}.identity")
    if identity.impl == "bare-metal" and any(c.kind == KIND_OS_SLOT for c in comps):
        raise _fail(where, "a bare-metal harness has no OS slot image")
    vivado = _str(d, "vivado", where, required=False)
    kits = []
    for i, c in enumerate(comps):
        if c.kind != KIND_RM_KIT:
            continue
        # A kit links against the static this release fields: it defaults to it, and a
        # kit that names another static contradicts the release.
        if c.static_id and c.static_id != identity.static_id:
            raise _fail(f"{where}.components[{i}].static_id",
                        f"{c.static_id} is not the release's static_id {identity.static_id}")
        kits.append(replace(c, static_id=identity.static_id, vivado=c.vivado or vivado))
    if kits:
        by_name = {k.name: k for k in kits}
        comps = tuple(by_name.get(c.name, c) for c in comps)
    released_at = _str(d, "released_at", where, required=False)
    if released_at:
        parse_time(released_at, f"{where}.released_at")
    return HarnessRelease(
        version=_version(_str(d, "version", where), f"{where}.version"),
        status=_status(d, where),
        identity=identity,
        compat=_compat(d.get("compat"), f"{where}.compat"),
        components=comps,
        rekey=_bool(d, "rekey", where),
        released_at=released_at,
        notes_url=_str(d, "notes_url", where, required=False),
        vivado=vivado,
        notes=_str(d, "notes", where, required=False),
        extra=_extra(d, _HRELEASE_KEYS),
    )


_ARELEASE_KEYS = {"version", "status", "artifacts", "lock", "requires_python", "min_harness",
                  "released_at", "notes_url", "notes"}
#: A PEP 427 wheel file name: distribution, version, [build,] python, abi, platform.
_WHEEL_NAME_RE = re.compile(r"^(?P<dist>[A-Za-z0-9_.]+)-(?P<ver>[^-]+)(-\d[^-]*)?-[^-]+-[^-]+-[^-]+\.whl$")


def normalise_dist(name: str) -> str:
    """A distribution name as PEP 503 compares it (``mps3_pyverify`` == ``mps3-pyverify``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def wheel_dist(name: str) -> tuple[str, str] | None:
    """(normalised distribution, version) of a wheel file name, or None."""
    m = _WHEEL_NAME_RE.match(name)
    return (normalise_dist(m.group("dist")), m.group("ver")) if m else None


def _app_release(d: Any, where: str) -> AppRelease:
    d = _obj(d, where)
    arts = d.get("artifacts")
    if not isinstance(arts, list) or not arts:
        raise _fail(f"{where}.artifacts", "must be a non-empty list")
    wheels, deps, locks = [], [], []
    for i, a in enumerate(arts):
        a = _obj(a, f"{where}.artifacts[{i}]")
        kind = _str(a, "kind", f"{where}.artifacts[{i}]")
        if kind == "wheel":
            wheels.append(_asset(a, f"{where}.artifacts[{i}]"))
        elif kind == "dep":
            deps.append((i, _asset(a, f"{where}.artifacts[{i}]")))
        elif kind == "lock":
            extras = _extras(a, f"{where}.artifacts[{i}]")
            if not extras:
                raise _fail(f"{where}.artifacts[{i}].extras",
                            "a lock artifact names the extras it covers (the base lock is 'lock')")
            locks.append(ExtrasLock(_asset(a, f"{where}.artifacts[{i}]"), extras))
    if len({lk.extras for lk in locks}) != len(locks):
        raise _fail(f"{where}.artifacts", "two locks for the same extras set")
    if len(wheels) != 1:
        raise _fail(f"{where}.artifacts", "must list exactly one wheel")
    wheel = wheels[0]
    if not wheel.name.endswith(".whl"):
        raise _fail(f"{where}.artifacts", f"wheel name {wheel.name!r} does not end in .whl")
    # The app wheel may be private (david U1: private GitHub Releases, fetched with a token).
    seen: dict[str, str] = {}
    for i, dep in deps:
        parsed = wheel_dist(dep.name)
        if parsed is None:
            raise _fail(f"{where}.artifacts[{i}]", f"dep {dep.name!r} is not a wheel file name")
        dist = parsed[0]
        if dist == normalise_dist("harness-manager"):
            raise _fail(f"{where}.artifacts[{i}]", "a dep cannot be harness-manager itself")
        if dist in seen:
            raise _fail(f"{where}.artifacts", f"two deps for the distribution {dist!r}")
        seen[dist] = dep.name
    lock = _asset(_obj(d["lock"], f"{where}.lock"), f"{where}.lock") if "lock" in d else None
    lock_extras = _extras(_obj(d["lock"], f"{where}.lock"), f"{where}.lock") if lock else ()
    if locks and lock is None:
        raise _fail(f"{where}.lock", "extras locks need the base lock too")
    released_at = _str(d, "released_at", where, required=False)
    if released_at:
        parse_time(released_at, f"{where}.released_at")
    return AppRelease(
        version=_version(_str(d, "version", where), f"{where}.version"),
        status=_status(d, where),
        wheel=wheel,
        lock=lock,
        requires_python=_str(d, "requires_python", where, required=False),
        min_harness=_version(_str(d, "min_harness", where, required=False),
                             f"{where}.min_harness", allow_empty=True),
        released_at=released_at,
        notes_url=_str(d, "notes_url", where, required=False),
        deps=tuple(dep for _, dep in deps),
        notes=_str(d, "notes", where, required=False),
        lock_extras=lock_extras,
        extra_locks=tuple(locks),
        extra=_extra(d, _ARELEASE_KEYS),
    )


def _extras(d: Mapping[str, Any], where: str) -> tuple[str, ...]:
    """An ``extras`` list: names, normalised and sorted."""
    names = _str_list(d, "extras", where)
    if any(not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", n) for n in names):
        raise _fail(f"{where}.extras", "must be extra names")
    return tuple(sorted({normalise_dist(n) for n in names}))


def _section(doc: Mapping[str, Any], key: str, parse, where: str) -> tuple[str, tuple]:
    if key not in doc:
        return "", ()
    sec = _obj(doc[key], where)
    rels_raw = sec.get("releases", [])
    if not isinstance(rels_raw, list):
        raise _fail(f"{where}.releases", "must be a list")
    rels = tuple(parse(r, f"{where}.releases[{i}]") for i, r in enumerate(rels_raw))
    versions = [parse_version(r.version) for r in rels]
    if len(set(versions)) != len(versions):
        raise _fail(f"{where}.releases", "release versions must be unique")
    current = _str(sec, "current", where, required=False)
    if current:
        _version(current, f"{where}.current")
        match = [r for r in rels if parse_version(r.version) == parse_version(current)]
        if not match:
            raise _fail(f"{where}.current", f"{current!r} names no release in the list")
        if match[0].status == STATUS_WITHDRAWN:
            raise _fail(f"{where}.current", f"{current!r} is withdrawn and cannot be current")
    return current, rels


_CHANNEL_KEYS = {"schema", "schema_version", "channel", "serial", "issued_at", "expires_at",
                 "signing_key_id", "board", "harness", "app", "$schema", "catalog"}
_BOARD_KEYS = {"pack", "part", "revisions"}


def parse_channel(doc: Any) -> Channel:
    """Validate a decoded ``channel.json``. Raises ``ChannelFormatError`` / ``IncompatibleError``."""
    doc = _obj(doc, "top level")
    if doc.get("schema") != SCHEMA:
        raise _fail("schema", f"is {doc.get('schema')!r}, not {SCHEMA!r}")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise IncompatibleError(
            f"channel.json schema_version {doc.get('schema_version')!r} is not supported by "
            f"this app (it reads version {SCHEMA_VERSION})",
            hint="update the app by hand (`pip install -U harness-manager`), then check again")
    name = _str(doc, "channel", "top level")
    if not _NAME_RE.match(name):
        raise _fail("channel", f"{name!r} is not a channel name")
    issued = _str(doc, "issued_at", "top level")
    parse_time(issued, "issued_at")
    expires = _str(doc, "expires_at", "top level", required=False)
    if expires:
        parse_time(expires, "expires_at")
    kid = _str(doc, "signing_key_id", "top level")
    if not _KEY_ID_RE.match(kid):
        raise _fail("signing_key_id", f"{kid!r} is not a 16-hex-digit minisign key id")
    catalog = _str(doc, "catalog", "top level", required=False)
    if catalog and not _CATALOG_RE.match(catalog):
        raise _fail("catalog", f"{catalog!r} is not a catalogue id (lowercase, digits, '-')")
    board_raw = _obj(doc.get("board", {}), "board")
    board = BoardSpec(pack=_str(board_raw, "pack", "board", required=False),
                      part=_str(board_raw, "part", "board", required=False),
                      revisions=_str_list(board_raw, "revisions", "board"),
                      extra=_extra(board_raw, _BOARD_KEYS))
    h_current, harness = _section(doc, "harness", _harness_release, "harness")
    a_current, app = _section(doc, "app", _app_release, "app")
    if not harness and not app:
        raise _fail("top level", "lists no harness and no app releases")
    return Channel(
        channel=name, serial=_int(doc, "serial", "top level", minimum=1), issued_at=issued,
        signing_key_id=kid.upper(), board=board, harness_current=h_current, harness=harness,
        app_current=a_current, app=app, expires_at=expires, raw=dict(doc),
        extra=_extra(doc, _CHANNEL_KEYS), catalog=catalog,
    )
