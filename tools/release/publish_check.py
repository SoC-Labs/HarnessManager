"""The checks before a BUILT harness release goes to GitHub (``scripts/publish_harness_release.sh``).

``python -m tools.release harness-release`` builds and signs a release tree; the publish
script uploads it. Between the two, this module answers "may this tree go to OWNER/REPO?"
with the client's own code, and writes what the script needs (``publish.env`` + file lists):

1. **The tree was built for this repo**: ``<root>/<OWNER>/<REPO>/releases/download/
   channel-<catalog>-<channel>/channel.json`` and its ``.minisig`` are there. (The asset URLs
   are relative to the channel, but an Arm-IP asset's URL names the AAA repo it was built for:
   rebuild with ``--repo``/``--aaa-repo`` rather than upload elsewhere.)
2. **Who signed it**: a TEST key (``7E57C0DE…``, or a ``test`` marker in the document) is
   never published. For ``--for-publish`` the key must be pinned in ``trust.PINNED_KEYS``
   for that channel; a dry run only warns.
3. **The client reads it**: ``smoke.verify`` with that key (the pinned one, else the tree's
   TEST key, else ``--public-key``): the client's fetch, signature, schema, anti-rollback,
   then every asset of the release by size + sha256 and HM's bundle checks.
4. **Every asset is on disk where the channel says**, by size and sha256, and nothing else is
   in the version's release dirs.
5. **The live channel** (``--live FILE``, downloaded read-only by the script): ours must have
   a HIGHER serial and keep every live release unchanged (a channel never loses a release;
   withdraw is the only way out). Otherwise clients refuse it (anti-rollback) or lose
   releases.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from harness_manager.core.errors import HarnessError
from harness_manager.services.update import minisign, trust
from harness_manager.services.update.schema import parse_channel

from . import smoke
from .channel_doc import _comparable, section_for
from .common import EXIT_MISMATCH, Layout, ReleaseError, sha256_file
from .signer import is_test_key, read_public_key

TEST_PUBLIC_KEY = "TEST-KEY.pub"          # harness-release --test-key leaves it at the tree root


@dataclass
class PublishCheck:
    root: Path
    repo: str
    aaa_repo: str
    catalog: str
    channel: str
    version: str = ""
    tag: str = ""
    serial: int = 0
    key_id: str = ""
    test: bool = False
    pinned: bool = False
    assets: list[Path] = field(default_factory=list)
    aaa_assets: list[Path] = field(default_factory=list)
    channel_files: list[Path] = field(default_factory=list)
    notes: str = ""
    warnings: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)

    @property
    def channel_tag(self) -> str:
        return Layout.channel_tag(self.catalog, self.channel)

    def write(self, out: Path) -> Path:
        """``publish.env`` (shell-quoted; the script sources it) and the file lists."""
        out.mkdir(parents=True, exist_ok=True)
        lists = {"ASSETS_FILE": ("assets.txt", self.assets),
                 "AAA_ASSETS_FILE": ("aaa_assets.txt", self.aaa_assets),
                 "CHANNEL_FILES_FILE": ("channel_files.txt", self.channel_files)}
        env: dict[str, str] = {}
        for key, (name, paths) in lists.items():
            for p in paths:
                if "\n" in str(p):
                    raise ReleaseError(f"refusing a file name with a newline: {p!r}")
            (out / name).write_text("".join(f"{p}\n" for p in paths), encoding="utf-8")
            env[key] = str(out / name)
        (out / "notes.md").write_text(self.notes.strip() + "\n", encoding="utf-8")
        env.update(REPO=self.repo, AAA_REPO=self.aaa_repo, CATALOG=self.catalog,
                   CHANNEL=self.channel, CHANNEL_TAG=self.channel_tag, VERSION=self.version,
                   TAG=self.tag, TITLE=f"{self.catalog} {self.version}",
                   SERIAL=str(self.serial), KEY_ID=self.key_id, TEST="1" if self.test else "0",
                   PINNED="1" if self.pinned else "0", NOTES_FILE=str(out / "notes.md"))
        path = out / "publish.env"
        path.write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in sorted(env.items())),
                        encoding="utf-8")
        return path


def _pinned_key(key_id: str, channel: str) -> trust.TrustedKey | None:
    k = next((k for k in trust.PINNED_KEYS if k.id_hex == key_id.upper()), None)
    return k if k is not None and k.may_sign(channel) else None


def check(root: Path, repo: str, *, catalog: str = "mps3-harness", channel: str = "beta",
          aaa_repo: str = Layout.aaa_repo, for_publish: bool = False,
          public_key: Path | None = None, live: Path | None = None,
          version: str = "") -> PublishCheck:
    root = Path(root).resolve()
    layout = Layout(root, repo=repo, aaa_repo=aaa_repo)
    pc = PublishCheck(root, repo, aaa_repo, catalog, channel)
    cfile = layout.channel_file(catalog, channel)
    sig = cfile.with_name("channel.json.minisig")
    if not cfile.is_file():
        built = sorted({"/".join(p.relative_to(root).parts[:2]) for p in
                        root.glob(f"*/*/releases/download/{pc.channel_tag}/channel.json")})
        raise ReleaseError(f"no {pc.channel_tag}/channel.json for {repo} under {root}",
                           hint=(f"this tree was built for {', '.join(built)}: rebuild with "
                                 f"--repo {repo}") if built else
                                "build it first: make harness-release ...")
    if not sig.is_file():
        raise ReleaseError(f"{cfile} has no .minisig: an unsigned channel is never published")
    data = cfile.read_bytes()
    try:
        doc = json.loads(data)
        ch = parse_channel(doc)
    except (ValueError, HarnessError) as exc:
        raise ReleaseError(f"{cfile} is not a channel the app reads: {exc}") from None
    pc.serial, pc.key_id = ch.serial, ch.signing_key_id.upper()
    pc.channel_files = [cfile, sig]
    for extra in ("keys.json", "keys.json.minisig"):
        if (cfile.parent / extra).is_file():
            pc.channel_files.append(cfile.parent / extra)
    try:
        signed_by = minisign.parse_signature(sig.read_bytes()).key_id_hex
    except minisign.SignatureError as exc:
        raise ReleaseError(f"{sig.name} is unreadable: {exc}") from None
    if signed_by != pc.key_id:
        raise ReleaseError(f"channel.json names key {pc.key_id} but {signed_by} signed it")

    # 2. who signed it
    pc.test = "test" in doc or is_test_key(pc.key_id)
    if pc.test:
        msg = (f"this is a TEST release (key {pc.key_id}, signed by harness-release "
               "--test-key): it is never published")
        if for_publish:
            raise ReleaseError(msg, hint="build it with the release key: --key FILE")
        pc.warnings.append(msg)
    pinned = _pinned_key(pc.key_id, channel)
    pc.pinned = pinned is not None
    if not pc.pinned:
        msg = (f"key {pc.key_id} is not pinned for the {channel!r} channel in this tree's "
               f"trust.PINNED_KEYS ({len(trust.PINNED_KEYS)} pinned): no client build would "
               "trust this channel")
        if for_publish:
            raise ReleaseError(msg, hint="pin the release key first (docs/KEYS.md), then "
                                         "release an app build that carries it")
        pc.warnings.append(msg)

    # 3. the client reads it (signature, schema, assets, bundle checks)
    if pinned is not None:
        key = pinned.key
    elif public_key is not None:
        key = read_public_key(Path(public_key))
    elif (root / TEST_PUBLIC_KEY).is_file():
        key = read_public_key(root / TEST_PUBLIC_KEY)
    else:
        raise ReleaseError(f"no key to verify {pc.key_id} with",
                           hint="pass --public-key FILE (the release key's public half)")
    if key.id_hex != pc.key_id:
        raise ReleaseError(f"the verify key is {key.id_hex}, the channel is signed by "
                           f"{pc.key_id}", code=EXIT_MISMATCH)
    section = section_for(catalog)
    releases = (doc.get(section) or {}).get("releases") or []
    want = version or (doc.get(section) or {}).get("current", "")
    entry = next((r for r in releases if r.get("version") == want), None)
    if entry is None:
        raise ReleaseError(f"the channel lists no release {want or '(no current release)'}")
    pc.version = entry["version"]
    pc.tag = str(entry.get("tag") or f"{catalog}-v{pc.version}")
    pc.notes = str(entry.get("notes") or f"{catalog} {pc.version}")
    rep = smoke.verify(layout, catalog, channel, [key], pc.version)
    pc.checks += rep.checks

    # 4. every asset where the channel says, and nothing else beside them
    named: dict[Path, dict[str, Any]] = {}
    for item in [*entry.get("components", []),
                 *([entry["legal_info"]] if isinstance(entry.get("legal_info"), dict) else [])]:
        rel = PurePosixPath(cfile.parent.relative_to(root).as_posix()) / item["url"]
        parts: list[str] = []
        for part in rel.parts:
            if part == "..":
                if not parts:
                    raise ReleaseError(f"asset URL {item['url']} leaves the tree")
                parts.pop()
            elif part != ".":
                parts.append(part)
        path = root.joinpath(*parts)
        if not path.is_file():
            raise ReleaseError(f"{item['name']} is not in the tree at {path}")
        if path.stat().st_size != item["size"] or sha256_file(path) != item["sha256"]:
            raise ReleaseError(f"{path} is not the {item['name']} the channel signed "
                               "(size or sha256 differ)", code=EXIT_MISMATCH)
        named[path.resolve()] = item
    main_dir = layout.release_dir(pc.tag).resolve()
    aaa_dir = layout.release_dir(pc.tag, aaa_repo).resolve()
    for p in sorted(named):
        if p.parent == main_dir:
            pc.assets.append(p)
        elif p.parent == aaa_dir:
            pc.aaa_assets.append(p)
        else:
            raise ReleaseError(f"{p} is outside the release dirs of {pc.tag} ({repo}, "
                               f"{aaa_repo})")
    for d in (main_dir, aaa_dir):
        stray = sorted(f for f in d.glob("*") if f.is_file() and f.resolve() not in named) \
            if d.is_dir() else []
        if stray:
            raise ReleaseError(f"{d} holds files the channel does not name: "
                               f"{', '.join(f.name for f in stray[:3])}",
                               hint="a release uploads exactly what was signed")
    pc.checks.append(f"{len(pc.assets)} assets for {repo} and {len(pc.aaa_assets)} for "
                     f"{aaa_repo}: every one where the channel says, by size and sha256")

    # 5. the live channel
    if live is not None:
        pc.checks += check_live(doc, Path(live), section)
    elif for_publish:
        pc.warnings.append("no live channel was compared (none published yet?)")
    return pc


def check_live(ours: dict[str, Any], live_path: Path, section: str) -> list[str]:
    try:
        live = json.loads(Path(live_path).read_bytes())
        parse_channel(live)
    except (OSError, ValueError, HarnessError) as exc:
        raise ReleaseError(f"the live channel {live_path} is unreadable: {exc}") from None
    for key in ("catalog", "channel"):
        if live.get(key) != ours.get(key):
            raise ReleaseError(f"the live channel is {key} {live.get(key)!r}, ours is "
                               f"{ours.get(key)!r}")
    if int(ours["serial"]) <= int(live["serial"]):
        raise ReleaseError(f"our channel has serial {ours['serial']}, the live one "
                           f"{live['serial']}: clients would refuse ours as a rollback",
                           hint="rebuild on the live channel: download it with `gh release "
                                "download` and pass --base DIR")
    mine = {r["version"]: r for r in (ours.get(section) or {}).get("releases", [])}
    gone, changed = [], []
    for r in (live.get(section) or {}).get("releases", []):
        m = mine.get(r["version"])
        if m is None:
            gone.append(r["version"])
        elif _comparable(m) != _comparable(r):
            changed.append(r["version"])
    if gone or changed:
        raise ReleaseError("publishing this would rewrite the live channel's history: "
                           + "; ".join(x for x in (
                               f"missing {', '.join(gone)}" if gone else "",
                               f"changed {', '.join(changed)}" if changed else "") if x),
                           hint="rebuild on the live channel (--base DIR)")
    return [f"live channel serial {live['serial']} -> ours {ours['serial']}; every live "
            f"release kept ({len((live.get(section) or {}).get('releases', []))})"]
