"""MPS3 telemetry sources. Team T9. ``make_telemetry_adapter(session)`` is the hook
``pack.py`` calls.

The MPS3 has no current or power sensor and no PMBus; its rail ADC is private to
the MCC (``CFG R V`` answers ERROR). What this adapter can offer, each reading
with its source, or ``Reading.unavailable`` with the exact missing prerequisite:

1. **FPGA die temperature, VCCINT, VCCAUX, VCCBRAM (+ max/min)** from SYSMON:
   - from the harness, when it reports feature ``sysmon`` (``stats.sysmon``, raw codes);
   - over JTAG (``sysmon.py``), when ``[boards.<id>.sysmon]`` is set in ``boards.toml``.
2. **LCD-module ambient** from the STMPE811, when the harness reports feature
   ``touch_temp`` (``telemetry.touch_temp_c``, harness handover A9).
3. **Board power** (W, V, A) from an external meter, when ``[boards.<id>.power]``
   is set (``socharness.power``).
4. **On-chip power ESTIMATE** for the loaded design, from Vivado routed power
   reports under ``[boards.<id>.estimates] vivado_reports``.

The MCC's temperature and oscillators are NOT read here: T1's TelemetryService
already reads them from ``session.controller``.

The control port 6900 takes one client at a time, and a held touch already starves
the firmware's superloop. So this adapter asks the shell only for features the
harness reports, at most every ``SHELL_MIN_INTERVAL_S``, in one connection; it
spawns the JTAG tool at most every ``min_interval_s`` (default 10 s). With nothing
configured and neither feature reported, it makes no network call at all
(after at most one identity read when the candidate carries none).

The wire shape of the harness keys is T9's proposal to the harness agent (CCR):
``telemetry`` gains ``"touch_temp_c": <degC float | null>``; ``stats`` gains
``"sysmon": {"temp", "vccint", "vccaux", "vccbram", "temp_max", ..., "vccbram_min",
"flag"}`` holding the raw 16-bit DRP codes, so one conversion (and the REF-bit
check) serves both the JTAG and the Ethernet path.

One codec: the requests are pyverify's own (``telemetry()``, and ``stats()`` from
the v0.11 codec). The additive keys are read from the reply line pyverify parsed,
through ``Mps3Shell.call_raw``'s tap, never from a hand-rolled request (the rule in
``shell.py``). An installed pyverify without ``stats()`` makes the harness SYSMON
rows unavailable with exactly that reason.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from socharness.core.errors import HarnessError, UnavailableError
from socharness.core.model import BoardIdentity, Candidate, Link, Reading
from socharness.power import vivado
from socharness.power.adapter import PowerAdapter
from socharness.power.adapter import make_power_adapter as _make_power_adapter
from socharness.power.config import BoardConfig, ConfigError, load_boards, power_link, with_links

from .sysmon import (
    CHANNELS,
    REG_FLAG,
    REG_MAX,
    REG_MIN,
    OpenOcdSysmon,
    SysmonSample,
    XsdbSysmon,
    make_sysmon_reader,
    sysmon_link,
    sysmon_readings,
)

log = logging.getLogger(__name__)

SHELL_MIN_INTERVAL_S = 5.0        # touch temperature: no faster than the research's 1 Hz, with margin
SYSMON_MIN_INTERVAL_S = 10.0      # an xsdb run costs ~3 s of start-up
IDENTITY_TTL_S = 10.0             # how stale the loaded design's name may be for the estimate
ESTIMATES_TTL_S = 60.0

TOUCH_SOURCE = "stmpe811 (harness telemetry)"
SHELL_SYSMON_SOURCE = "sysmon (harness stats)"
TOUCH_CAVEAT = ("LCD-module ambient, not the FPGA die; 0.44 K/LSB, ratiometric to the CLCD "
                "3V3 (about 3 K per 1 % supply error)")
NO_POWER_SENSOR = ("the MPS3 has no power sensor; add a metered plug or an INA260 "
                   "([boards.<id>.power] in boards.toml)")
NO_ESTIMATES = ("no Vivado power reports configured: set vivado_reports under "
                "[boards.<id>.estimates] in boards.toml")

# stats.sysmon key -> SYSMON DRP register (the proposed wire shape; raw 16-bit codes)
STATS_SYSMON_KEYS: dict[str, int] = {"flag": REG_FLAG}
for _name, _reg, *_ in CHANNELS:
    _key = "temp" if _name == "fpga_die_temp" else _name
    STATS_SYSMON_KEYS[_key] = _reg
    STATS_SYSMON_KEYS[f"{_key}_max"] = REG_MAX[_reg]
    STATS_SYSMON_KEYS[f"{_key}_min"] = REG_MIN[_reg]


NO_STATS_CODEC = ("the harness reports 'sysmon' but the installed pyverify has no stats() "
                  "(net-protocol v0.11 codec); update pyverify")


def harness_replies(client: Any, tap: Any, *, telemetry: bool,
                    stats: bool) -> tuple[dict[str, Any] | None, dict[str, Any] | str | None]:
    """The whole ``telemetry`` and ``stats`` replies, via pyverify on ONE connection.

    ``tap.last`` is the reply line pyverify just parsed (``Mps3Shell.call_raw``), so keys
    pyverify does not model (``touch_temp_c``, ``sysmon``) come from that same line.
    ``stats`` is a reason string when the installed pyverify cannot ask for it.
    """
    tel: dict[str, Any] | None = None
    st: dict[str, Any] | str | None = None
    if telemetry:
        client.telemetry()
        tel = dict(tap.last)
    if stats:
        ask = getattr(client, "stats", None)
        if not callable(ask):
            st = NO_STATS_CODEC
        else:
            resp = ask()
            raw = getattr(resp, "raw", None)
            st = dict(raw) if isinstance(raw, dict) and raw else dict(tap.last)
    return tel, st


# --- pure parsers (unit-tested directly) -------------------------------------------------


def touch_readings(reply: dict[str, Any] | None, error: str = "",
                   observed_at: float | None = None) -> list[Reading]:
    name, unit = "lcd_ambient_temp", "degC"
    if error or reply is None:
        return [Reading.unavailable(name, unit, error or "no telemetry reply", source=TOUCH_SOURCE)]
    if "touch_temp_c" not in reply:
        why = "the harness reports 'touch_temp' but its telemetry reply has no touch_temp_c"
        return [Reading.unavailable(name, unit, why, source=TOUCH_SOURCE)]
    value = reply["touch_temp_c"]
    if value is None:
        return [Reading.unavailable(name, unit, "the harness could not read the STMPE811 "
                                                "(touch_temp_c is null)", source=TOUCH_SOURCE)]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return [Reading.unavailable(name, unit, f"touch_temp_c is {value!r}, not a number",
                                    source=TOUCH_SOURCE)]
    stamp = {} if observed_at is None else {"observed_at": observed_at}
    return [Reading(name, float(value), unit, TOUCH_SOURCE, reason=TOUCH_CAVEAT, **stamp)]


def stats_sysmon_sample(reply: dict[str, Any] | str | None,
                        observed_at: float | None = None) -> SysmonSample | str:
    """The raw SYSMON codes in a ``stats`` reply, or the reason there are none."""
    if isinstance(reply, str):
        return reply
    if reply is None:
        return "no stats reply"
    if reply.get("ok") is False:
        return f"the harness reports 'sysmon' but stats failed: {reply.get('err') or 'no detail'}"
    sm = reply.get("sysmon")
    if not isinstance(sm, dict):
        return "the harness reports 'sysmon' but its stats reply has no sysmon object"
    codes: dict[int, int] = {}
    errors: dict[int, str] = {}
    for key, addr in STATS_SYSMON_KEYS.items():
        v = sm.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 0xFFFF:
            codes[addr] = v
        else:
            errors[addr] = "not reported by the harness" if v is None else f"reported as {v!r}"
    if not codes:
        return ("stats.sysmon has none of the expected raw-code keys "
                f"({', '.join(STATS_SYSMON_KEYS)})")
    return SysmonSample(codes, errors, SHELL_SYSMON_SOURCE,
                        observed_at if observed_at is not None else time.time())


def _unavailable_sysmon(reason: str, source: str) -> list[Reading]:
    return [Reading.unavailable(n, u, reason, source=source) for n, _r, u, _nom, _p in CHANNELS]


# --- the adapter ---------------------------------------------------------------------------


class Mps3Telemetry:
    """A ``TelemetryAdapter`` over every T9 source this session can reach."""

    def __init__(self, session: Any, *, board: BoardConfig | None = None, config_error: str = "",
                 sysmon: XsdbSysmon | OpenOcdSysmon | None = None, sysmon_error: str = "",
                 sysmon_interval_s: float = SYSMON_MIN_INTERVAL_S,
                 power: PowerAdapter | None = None, estimates_dir: Path | None = None,
                 estimates_error: str = "", clock: Callable[[], float] = time.time) -> None:
        self._session = session
        self.board = board
        self.config_error = config_error
        self.sysmon = sysmon
        self.sysmon_error = sysmon_error
        self.sysmon_interval_s = sysmon_interval_s
        self._power = power
        self.estimates_dir = estimates_dir
        self.estimates_error = estimates_error
        self._clock = clock
        self._lock = threading.RLock()
        self._ident: tuple[float, BoardIdentity | None, str] | None = None
        cand_ident = getattr(getattr(session, "candidate", None), "identity", None)
        if cand_ident is not None:
            self._ident = (clock(), cand_ident, "")
        self._shell_cache: tuple[float, dict | None, dict | str | None, str] | None = None
        self._sysmon_cache: tuple[float, list[Reading]] | None = None
        self._est_cache: tuple[float, dict[str, vivado.PowerEstimate]] | None = None

    # -- the TelemetryAdapter protocol ------------------------------------------------------

    def readings(self) -> list[Reading]:
        with self._lock:
            return (self._sysmon_readings() + self._touch_readings()
                    + self._power_readings() + self._estimate_readings())

    # -- identity (features, loaded design) ------------------------------------------------------

    @property
    def _shell(self) -> Any:
        return getattr(self._session, "shell", None)

    def _identity(self, max_age_s: float) -> tuple[BoardIdentity | None, str]:
        now = self._clock()
        if self._ident is not None:
            # A failed read is retried sooner, so a board that comes back is seen again.
            ttl = max_age_s if self._ident[1] is not None else min(max_age_s, SHELL_MIN_INTERVAL_S)
            if now - self._ident[0] <= ttl:
                return self._ident[1], self._ident[2]
        if self._shell is None:
            return None, "no Ethernet link to the shell"
        try:
            ident, err = self._session.identity(), ""
        except HarnessError as exc:
            ident, err = None, f"cannot ask the harness: {exc}"
        self._ident = (now, ident, err)
        return ident, err

    def _features(self) -> tuple[frozenset[str], str]:
        """The harness's features, or (empty, why they are unknown). No shell: none, no error."""
        if self._shell is None and self._ident is None:
            return frozenset(), ""
        ident, err = self._identity(max_age_s=300.0)
        return (frozenset(ident.features) if ident else frozenset()), err

    # -- the harness (6900) ------------------------------------------------------------------------

    def _harness(self, want_telemetry: bool,
                 want_stats: bool) -> tuple[dict | None, dict | str | None, str, float]:
        now = self._clock()
        cache = self._shell_cache
        if cache is not None and now - cache[0] < SHELL_MIN_INTERVAL_S:
            return cache[1], cache[2], cache[3], cache[0]
        try:
            tel, st = self._shell.call_raw(lambda client, tap: harness_replies(
                client, tap, telemetry=want_telemetry, stats=want_stats))
            err = ""
        except HarnessError as exc:
            tel, st, err = None, None, f"cannot ask the harness: {exc}"
        except Exception as exc:  # noqa: BLE001 - a codec surprise is a reason, not a crash
            tel, st, err = None, None, f"unexpected harness reply: {type(exc).__name__}: {exc}"
        self._shell_cache = (now, tel, st, err)
        return tel, st, err, now

    # -- sources -------------------------------------------------------------------------------------

    def _sysmon_readings(self) -> list[Reading]:
        feats, ident_err = self._features()
        rows: list[Reading] = []
        if "sysmon" in feats and self._shell is not None:
            _tel, st, err, when = self._harness("touch_temp" in feats, True)
            sample = err or stats_sysmon_sample(st, observed_at=when)
            rows += (_unavailable_sysmon(sample, SHELL_SYSMON_SOURCE) if isinstance(sample, str)
                     else sysmon_readings(sample))
        if self.sysmon is not None:
            rows += self._jtag_sysmon()
        elif self.sysmon_error:
            rows += _unavailable_sysmon(self.sysmon_error, "sysmon-jtag")
        if rows:
            return rows
        if self.config_error:
            reason = self.config_error
        elif ident_err:
            reason = f"{ident_err}; or configure [boards.<id>.sysmon] for a JTAG cable on J17"
        elif self._shell is None:
            reason = ("needs a JTAG cable on J17 ([boards.<id>.sysmon] in boards.toml), or the "
                      "Ethernet link and harness firmware with 'sysmon'")
        else:
            reason = ("needs a JTAG cable on J17 ([boards.<id>.sysmon] in boards.toml), or "
                      "harness firmware with 'sysmon'")
        return [Reading.unavailable("fpga_die_temp", "degC", reason, source="sysmon")]

    def _jtag_sysmon(self) -> list[Reading]:
        assert self.sysmon is not None
        now = self._clock()
        if self._sysmon_cache is not None and now - self._sysmon_cache[0] < self.sysmon_interval_s:
            return self._sysmon_cache[1]
        try:
            rows = sysmon_readings(self.sysmon.read())
        except UnavailableError as exc:
            rows = _unavailable_sysmon(exc.reason, self.sysmon.source)
        except Exception as exc:  # noqa: BLE001 - a tool surprise is a reason, never a 0
            log.warning("SYSMON read failed: %s", exc)
            rows = _unavailable_sysmon(f"{type(exc).__name__}: {exc}", self.sysmon.source)
        self._sysmon_cache = (now, rows)
        return rows

    def _touch_readings(self) -> list[Reading]:
        feats, ident_err = self._features()
        if "touch_temp" in feats and self._shell is not None:
            tel, _st, err, when = self._harness(True, "sysmon" in feats)
            return touch_readings(tel, err, observed_at=when)
        if ident_err:
            reason = ident_err
        elif self._shell is None:
            reason = "needs the Ethernet link to the shell and harness firmware with 'touch_temp'"
        else:
            reason = "needs harness firmware with 'touch_temp'"
        return [Reading.unavailable("lcd_ambient_temp", "degC", reason, source="stmpe811")]

    def _power_readings(self) -> list[Reading]:
        power = getattr(self._session, "power", None) or self._power
        if power is not None:
            try:
                return list(power.read())
            except Exception as exc:  # noqa: BLE001 - one bad meter must not hide the other rows
                return [Reading.unavailable("board_power", "W", f"{type(exc).__name__}: {exc}",
                                            source=getattr(power, "label", "power-meter"))]
        reason = self.config_error or NO_POWER_SENSOR
        return [Reading.unavailable("board_power", "W", reason, source="power-meter")]

    def _estimate_readings(self) -> list[Reading]:
        src = "estimate:vivado"
        if self.estimates_error or self.config_error:
            reason = self.estimates_error or self.config_error
            return [Reading.unavailable("onchip_power_estimate", "W", reason, source=src)]
        if self.estimates_dir is None:
            return [Reading.unavailable("onchip_power_estimate", "W", NO_ESTIMATES, source=src)]
        ident, err = self._identity(max_age_s=IDENTITY_TTL_S)
        rm = ident.rm_name if ident else ""
        if not rm:
            what = ident.rm_id if ident and ident.rm_id else ""
            why = (f"the loaded design {what} has no name" if what
                   else f"the loaded design is unknown ({err or 'the harness did not say'})")
            return [Reading.unavailable("onchip_power_estimate", "W", why, source=src)]
        estimates = self._estimates()
        est = estimates.get(rm)
        if est is None:
            have = ", ".join(sorted(estimates)) or "none"
            return [Reading.unavailable(
                "onchip_power_estimate", "W",
                f"no Vivado power report for {rm!r} under {self.estimates_dir} (found: {have})",
                source=f"{src} {rm}")]
        return est.readings()

    def _estimates(self) -> dict[str, vivado.PowerEstimate]:
        now = self._clock()
        if self._est_cache is None or now - self._est_cache[0] > ESTIMATES_TTL_S:
            assert self.estimates_dir is not None
            self._est_cache = (now, vivado.load_estimates(self.estimates_dir))
        return self._est_cache[1]


# --- configuration -> adapters and links ------------------------------------------------------------


def board_config(candidate: Candidate) -> tuple[BoardConfig | None, str]:
    """This board's ``boards.toml`` table, or (None, the reason the file is unusable)."""
    try:
        boards = load_boards()
    except ConfigError as exc:
        return None, str(exc)
    return boards.for_board(candidate.board_id, candidate.links), ""


def _sysmon_from(board: BoardConfig | None) -> tuple[XsdbSysmon | OpenOcdSysmon | None, str, float]:
    table = board.tables.get("sysmon") if board is not None else None
    if table is None:
        return None, "", SYSMON_MIN_INTERVAL_S
    try:
        reader = make_sysmon_reader(table)
    except (HarnessError, TypeError, ValueError) as exc:
        return None, f"boards.toml sysmon: {getattr(exc, 'message', exc)}", SYSMON_MIN_INTERVAL_S
    interval = table.get("min_interval_s", SYSMON_MIN_INTERVAL_S)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 0:
        interval = SYSMON_MIN_INTERVAL_S
    return reader, "", float(interval)


def _estimates_from(board: BoardConfig | None) -> tuple[Path | None, str]:
    table = board.tables.get("estimates") if board is not None else None
    if table is None:
        return None, ""
    raw = table.get("vivado_reports") if isinstance(table, dict) else None
    if not isinstance(raw, str) or not raw:
        return None, "boards.toml estimates.vivado_reports must be a directory path"
    path = Path(raw).expanduser()
    if not path.is_dir():
        return None, f"boards.toml estimates.vivado_reports: {path} is not a directory"
    return path, ""


def make_telemetry_adapter(session: Any) -> Mps3Telemetry:
    """The hook. Always returns an adapter: the rows it cannot fill say why."""
    candidate = session.candidate
    board, config_error = board_config(candidate)
    reader, sysmon_error, interval = _sysmon_from(board)
    est_dir, est_error = _estimates_from(board)
    return Mps3Telemetry(session, board=board, config_error=config_error, sysmon=reader,
                         sysmon_error=sysmon_error, sysmon_interval_s=interval,
                         power=_make_power_adapter(board), estimates_dir=est_dir,
                         estimates_error=est_error)


def make_power_adapter(session: Any) -> PowerAdapter | None:
    """Proposed hook (T9 CCR 1): ``BoardSession.power`` from ``[boards.<id>.power]``."""
    board, _err = board_config(session.candidate)
    return _make_power_adapter(board)


def config_links(candidate: Candidate) -> tuple[Link, ...]:
    """Links ``boards.toml`` gives this board: ``SMART_POWER`` for a meter, ``JTAG`` for SYSMON.

    Proposed use (T9 CCR 2): the pack adds these to every candidate it returns, so
    capability negotiation sees ``telemetry_power`` and the JTAG route of
    ``telemetry_temp``. Secret-free: no address here carries a credential.
    """
    board, _err = board_config(candidate)
    links: list[Link] = []
    link = power_link(board)
    if link is not None:
        links.append(link)
    reader, _serr, _i = _sysmon_from(board)
    if reader is not None:
        links.append(sysmon_link(reader))
    return tuple(links)


def with_config_links(candidate: Candidate) -> Candidate:
    """``candidate`` plus its ``boards.toml`` links (the one-line pack integration)."""
    return with_links(candidate, config_links(candidate))


__all__ = [
    "Mps3Telemetry",
    "config_links",
    "make_power_adapter",
    "make_telemetry_adapter",
    "harness_replies",
    "stats_sysmon_sample",
    "touch_readings",
    "with_config_links",
]
