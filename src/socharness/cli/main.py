"""``socharness``: CLI entry point.

Output contract (inherited from the HAPS helper contract):
- ``--json`` prints one JSON object on stdout; ``--tsv`` prints tab-separated
  fields whose columns are append-only;
- human output otherwise;
- the exit code comes from ``socharness.core.errors.ExitCode``, never from an
  ad-hoc number;
- errors go to stderr as ``socharness: <message> — <next action>``.

Scaffold verbs: ``version``, ``packs``, ``info``, ``probe``. Team T5 owns the rest.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Sequence
from enum import Enum
from typing import Any

from socharness import __version__
from socharness.core.capabilities import negotiate
from socharness.core.errors import ExitCode, HarnessError, UsageError
from socharness.core.model import BoardInfo
from socharness.core.pack import ProbeHints
from socharness.core.registry import get_pack, load_packs


def _jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (frozenset, set, tuple, list)):
        items = [_jsonable(v) for v in obj]
        return sorted(items) if isinstance(obj, (frozenset, set)) else items
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    return obj


def cmd_version(args: argparse.Namespace) -> int:
    print(__version__)
    return ExitCode.OK


def cmd_packs(args: argparse.Namespace) -> int:
    packs = load_packs()
    if args.json:
        print(json.dumps({n: p.title for n, p in sorted(packs.items())}))
    else:
        for name, pack in sorted(packs.items()):
            print(f"{name}\t{pack.title}")
    return ExitCode.OK


def build_info(pack_name: str, target: str) -> BoardInfo:
    pack = get_pack(pack_name)
    if not hasattr(pack, "candidate_for_host"):
        raise UsageError(f"pack {pack_name!r} cannot open a board by address")
    candidate = pack.candidate_for_host(target)  # type: ignore[attr-defined]
    with pack.open(candidate) as session:
        identity = session.identity()
        health = session.health()
    links = [lk.kind for lk in candidate.links]
    available, unavailable = negotiate(pack.capability_specs(), links, identity.features)
    return BoardInfo(candidate, identity, health, available, unavailable)


def cmd_info(args: argparse.Namespace) -> int:
    info = build_info(args.pack, args.target)
    if args.json:
        print(json.dumps(_jsonable(info), sort_keys=True))
    elif args.tsv:
        ident = info.identity
        # Append-only columns. Never reorder.
        print("\t".join([
            info.candidate.board_id, ident.board_type, ident.shell_id, ident.rm_id,
            ident.rm_name or "-", ident.harness_version or "-", ident.build_check.value,
            info.health.control_channel,
        ]))
    else:
        ident = info.identity
        print(f"board      {info.candidate.label}")
        print(f"shell      {ident.shell_id}   harness {ident.harness_version or '?'}"
              f" ({ident.firmware_sha or '?'}{' dirty' if ident.firmware_dirty else ''})")
        print(f"design     {ident.rm_name or '?'} ({ident.rm_id})")
        print(f"build      firmware/fabric check: {ident.build_check.value}")
        print(f"features   {', '.join(ident.features) or '-'}")
        print(f"control    {info.health.control_channel}")
        for note in info.health.notes:
            print(f"note       {note}")
        print(f"can        {', '.join(sorted(info.capabilities))}")
        for cap, why in sorted(info.unavailable.items()):
            print(f"cannot     {cap}: {why}")
    return ExitCode.OK


def cmd_probe(args: argparse.Namespace) -> int:
    pack = get_pack(args.pack)
    hints = ProbeHints(hosts=tuple(args.host), timeout_s=args.timeout)
    found = pack.probe(hints)
    if args.json:
        print(json.dumps([_jsonable(c) for c in found]))
    else:
        for c in found:
            print(f"{c.board_id}\t{c.label}\t{c.evidence}")
    return ExitCode.OK if found else ExitCode.ABSENT


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="socharness", description="SoC Labs Harness Manager")
    p.add_argument("--pack", default="mps3", help="board pack (default: mps3)")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="one JSON object on stdout")
    fmt.add_argument("--tsv", action="store_true", help="tab-separated, append-only columns")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("version", help="print the Harness Manager version").set_defaults(fn=cmd_version)
    sub.add_parser("packs", help="list installed board packs").set_defaults(fn=cmd_packs)

    info = sub.add_parser("info", help="identity, health and capabilities of one board")
    info.add_argument("target", help="shell address, host[:port]")
    info.set_defaults(fn=cmd_info)

    probe = sub.add_parser("probe", help="look for boards")
    probe.add_argument("--host", action="append", default=[], help="shell address to try")
    probe.add_argument("--timeout", type=float, default=2.0)
    probe.set_defaults(fn=cmd_probe)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return ExitCode.USAGE if exc.code else ExitCode.OK
    try:
        return int(args.fn(args))
    except HarnessError as exc:
        print(f"socharness: {exc}", file=sys.stderr)
        return int(exc.code)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

