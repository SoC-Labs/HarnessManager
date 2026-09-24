"""DUT build kits in the signed channel (KIT-STORE K4; david K1/K2): the seam for KIT-CORE.

A harness release may carry its static's kit as a component with ``target: host-kit``,
``kind: rm-kit``, ``access: public`` (david K2). The update planner NEVER downloads it
(a harness install fetches only ``mcc_sd``, ``ethernet`` and ``host-store`` parts): a kit
is 10-40 MB that most users never need. The kit service fetches it on demand, by static:

    kits = ChannelKits.for_service(update_service)         # or ChannelKits(client, downloader)
    kits.list("0x72BB0A36")   -> [KitAsset ...]              # newest release first
    kits.resolve("0x72BB0A36") -> Asset | None                # KIT-CORE's ChannelSource(resolve=)
    kits.fetch("0x72BB0A36")  -> Path                         # a verified blob in the update cache

**For KIT-CORE's ``ChannelSource``.** ``resolve`` returns the asset with its URL made
absolute, so ``ChannelSource(resolve=kits.resolve)`` works as written for a public host
and for mirrors. For the private GitHub repo (U1: a kit is public in the channel's sense,
but the repo is private, so the download still needs the token) prefer
``kits.fetch``: it goes through the update service's downloader (the token provider, the
GitHub API lookup, the mirrors by sha, the shared cache). Either way the blob's sha256 is
the signed one; KIT-CORE then checks the CRC-32 of the DCP against the static_id.

**Which kit.** The newest non-withdrawn release whose component names the static; a
withdrawn release's kit is used only when no other release has one (the static may still
be fielded). Every listed channel (``stable``, then ``beta``) is fetched and verified once
per ``ChannelKits`` (signature, schema, serial per catalogue); the harness catalogue of the
pack is the default (``mps3-harness``). A channel that cannot be fetched is skipped while
another one answers; when none does, the first error is raised.

**Wired into KIT-CORE** (``kit_channel_source``): ``KitService.for_state_dir`` (the CLI)
and the daemon's ``kit_api`` use a ``KitChannelSource``: the same interface as KIT-CORE's
``ChannelSource`` (``name``, ``reason``, ``describe``, ``fetch``, plus ``resolve``), backed
by an ``UpdateService`` over the same state dir, built on first use (no network before a
fetch). A fetch that finds no kit, cannot reach the channel, or lacks a token returns
None and says why in ``last_error``, so the kit service goes on to the hub source. A
refusal (a bad signature, a serial rollback, a kit that fails its sha256) is raised: it is
loud, never a quiet fall-through.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from harness_manager.core.errors import (
    AbsentError,
    HarnessError,
    UnavailableError,
    UnreachableError,
)

from .channel import ChannelClient, VerifiedChannel
from .download import Downloader, Progress, resolve_url
from .schema import STATUS_WITHDRAWN, Asset, harness_catalog
from .version import parse_version


def _u32(text: str) -> int | None:
    try:
        return int(text, 16)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class KitAsset:
    """One ``rm-kit`` component of one harness release, as the channel signs it."""

    static_id: str            # "0x72bb0a36"
    release: str              # the harness release that carries it
    status: str               # current | superseded | withdrawn
    component: str
    vivado: str               # the Vivado release the kit needs ("2024.1")
    usercode: str             # the release's static_usercode
    impl: str                 # bare-metal | linux
    asset: Asset              # url absolute (resolved against the channel)
    channel: str
    catalog: str
    channel_url: str

    def summary(self) -> dict[str, Any]:
        return {"static_id": self.static_id, "release": self.release, "status": self.status,
                "component": self.component, "vivado": self.vivado, "usercode": self.usercode,
                "impl": self.impl, "name": self.asset.name, "sha256": self.asset.sha256,
                "size": self.asset.size, "access": self.asset.access, "channel": self.channel,
                "catalog": self.catalog}


def kit_assets(verified: VerifiedChannel, static_id: str | None = None) -> list[KitAsset]:
    """The kits a verified channel lists (for one static, or all), best first: newest
    release first, withdrawn releases last."""
    want = _u32(static_id) if static_id else None
    out = []
    for rel in verified.channel.harness:
        for comp in rel.kits():
            sid = comp.static_id or rel.identity.static_id
            if want is not None and _u32(sid) != want:
                continue
            asset = replace(comp.asset, url=resolve_url(verified.url, comp.asset.url))
            out.append(KitAsset(static_id=sid.lower(), release=rel.version, status=rel.status,
                                component=comp.name, vivado=comp.vivado or rel.vivado,
                                usercode=rel.identity.usercode, impl=rel.identity.impl,
                                asset=asset, channel=verified.channel.channel,
                                catalog=verified.catalog, channel_url=verified.url))
    return _best_first(out)


def _best_first(kits: list[KitAsset]) -> list[KitAsset]:
    """Newest release first; withdrawn releases after every other (stable sorts)."""
    kits = sorted(kits, key=lambda k: parse_version(k.release), reverse=True)
    return sorted(kits, key=lambda k: k.status == STATUS_WITHDRAWN)


class ChannelKits:
    """Kits in the signed channel(s), by static: list, resolve (KIT-CORE seam), fetch."""

    def __init__(self, client: ChannelClient, downloader: Downloader, *,
                 channels: Iterable[str] = ("stable", "beta"), source: str | None = None,
                 catalog: str | None = None) -> None:
        self.client = client
        self.downloader = downloader
        self.channels = tuple(channels)
        self.source = source
        self.catalog = catalog
        self._verified: dict[str, VerifiedChannel] = {}

    @classmethod
    def for_service(cls, svc: Any, *, pack: str = "mps3", **kw: Any) -> ChannelKits:
        """From an ``UpdateService``: its client, its downloader (token, mirrors, cache)."""
        kw.setdefault("catalog", harness_catalog(pack))
        return cls(svc.channels, svc.downloader, **kw)

    def verified(self) -> list[VerifiedChannel]:
        """Every channel that fetches and verifies (each once). Raises the first error when
        none does."""
        errors: list[HarnessError] = []
        for name in self.channels:
            if name in self._verified:
                continue
            try:
                self._verified[name] = self.client.fetch(name, self.source, catalog=self.catalog)
            except HarnessError as exc:
                errors.append(exc)
        if not self._verified and errors:
            raise errors[0]
        return [self._verified[n] for n in self.channels if n in self._verified]

    def list(self, static_id: str | None = None) -> list[KitAsset]:
        """Every kit (for ``static_id``, or all) across the channels, best first. Raises
        ``HarnessError`` when a channel cannot be fetched or verified."""
        seen: set[str] = set()
        out: list[KitAsset] = []
        for v in self.verified():
            for k in kit_assets(v, static_id):
                if k.asset.sha256 not in seen:
                    seen.add(k.asset.sha256)
                    out.append(k)
        return _best_first(out)

    def best(self, static_id: str) -> KitAsset | None:
        found = self.list(static_id)
        return found[0] if found else None

    def resolve(self, static_id: str) -> Asset | None:
        """KIT-CORE's ``ChannelSource(resolve=...)``: the kit asset (absolute URL), or None
        when the verified channels list no kit for this static."""
        best = self.best(static_id)
        return best.asset if best else None

    def fetch(self, static_id: str, progress: Progress | None = None) -> Path:
        """Download and sha256-verify the best kit for ``static_id`` (cache, mirrors, then
        its URL, with the token where GitHub needs it). ``AbsentError`` when none is listed."""
        best = self.best(static_id)
        if best is None:
            raise AbsentError(f"no signed channel lists a DUT build kit for static {static_id}",
                              hint="try the hub mirror or a local kit (kit fetch --source)")
        return self.downloader.fetch(best.asset, base_url=best.channel_url, progress=progress)


# --- KIT-CORE's channel source, wired ------------------------------------------------------------


class KitChannelSource:
    """KIT-CORE's ``ChannelSource`` interface over the signed channel (``ChannelKits``).

    ``make_kits`` builds the ``ChannelKits`` on first use, so constructing a kit service
    never touches the network or the update state.
    """

    name = "channel"

    def __init__(self, make_kits: Callable[[], ChannelKits]) -> None:
        self._make = make_kits
        self._kits: ChannelKits | None = None
        self.last_error = ""

    @property
    def kits(self) -> ChannelKits:
        if self._kits is None:
            self._kits = self._make()
        return self._kits

    @property
    def reason(self) -> str:
        return ""                                  # available: the fetch says why it found none

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "available": True, "reason": ""}

    def resolve(self, static_id: str) -> Asset | None:
        return self.kits.resolve(static_id)

    def fetch(self, static_id: str, work: Path, progress: Progress | None) -> Path | None:
        """The kit zip for ``static_id`` (a copy under ``work``: the kit service deletes it
        after the import), or None with ``last_error`` saying why."""
        self.last_error = ""
        try:
            blob = self.kits.fetch(static_id, progress=progress)
        except (AbsentError, UnreachableError, UnavailableError) as exc:
            self.last_error = exc.message              # not there: the next source may have it
            return None
        work = Path(work)
        work.mkdir(parents=True, exist_ok=True)
        best = self.kits.best(static_id)
        out = work / (best.asset.name if best is not None else f"{static_id}.zip")
        tmp = out.with_name(f".{out.name}.tmp")
        tmp.write_bytes(blob.read_bytes())
        tmp.replace(out)
        return out


def kit_channel_source(state_dir: Path, *, pack: str = "mps3",
                       channels: Iterable[str] = ("stable", "beta"),
                       source: str | None = None) -> KitChannelSource:
    """The channel source ``KitService`` uses in the product: the pack's harness catalogue
    through an ``UpdateService`` over ``state_dir`` (its trust, token, mirrors and cache)."""
    chans = tuple(channels)

    def make() -> ChannelKits:
        from .service import UpdateService

        return ChannelKits.for_service(UpdateService(state_dir=Path(state_dir)), pack=pack,
                                       channels=chans, source=source)

    return KitChannelSource(make)
