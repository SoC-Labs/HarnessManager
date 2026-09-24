"""Markdown tables from ``q2_soak`` runs: one table per sampled metric, runs side by side.

    python -m tests.soak.q2_tables base=/tmp/hm-q2-soak-base/soak.json fixed=/tmp/hm-q2-soak-fix/soak.json
"""

from __future__ import annotations

import json
import sys

METRICS = [
    ("fds", "open file descriptors"),
    ("sockets", "of which sockets"),
    ("ptmx", "of which PTY masters"),
    ("inotify", "inotify instances"),
    ("threads", "threads"),
    ("rss_kb", "resident memory (KiB)"),
    ("children", "child processes (ssh + OpenOCD)"),
    ("state_bytes", "state dir size (bytes)"),
    ("log_bytes", "daemon.log size (bytes)"),
]


def rows(samples: list[dict], every_min: float = 5.0) -> list[tuple[float, dict]]:
    t0 = samples[0]["t"]
    out, next_at = [], 0.0
    for s in samples:
        minute = (s["t"] - t0) / 60
        if minute + 0.01 >= next_at or s.get("idle"):
            out.append((minute, s))
            next_at = (int(minute / every_min) + 1) * every_min
    return out


def main(argv: list[str]) -> int:
    runs = {}
    for arg in argv:
        name, _, path = arg.partition("=")
        with open(path) as fh:
            runs[name] = json.load(fh)["samples"]
    names = list(runs)
    for key, title in METRICS:
        print(f"**{title}** (`{key}`)\n")
        print("| minute | " + " | ".join(names) + " |")
        print("|---:|" + "---:|" * len(names))
        cols = {n: rows(runs[n]) for n in names}
        depth = max(len(c) for c in cols.values())
        for i in range(depth):
            cells, minute = [], ""
            for n in names:
                if i < len(cols[n]):
                    m, s = cols[n][i]
                    minute = minute or (f"{m:.0f}" + (" (idle)" if s.get("idle") else ""))
                    cells.append(str(s.get(key, "")))
                else:
                    cells.append("")
            print(f"| {minute} | " + " | ".join(cells) + " |")
        for n in names:
            vals = [s.get(key, 0) for s in runs[n]]
            print(f"\n{n}: min {min(vals)}, max {max(vals)}, first {vals[0]}, last {vals[-1]}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
