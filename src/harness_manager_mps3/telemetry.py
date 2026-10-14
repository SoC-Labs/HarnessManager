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
   is set (``harness_manager.power``).
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

from harness_manager.core.errors import (
    ActionFailedError,
    HarnessError,
    UnavailableError,
    UsageError,
)
from harness_manager.core.model import BoardIdentity, Candidate, Link, Reading
from harness_manager.power import vivado
from harness_manager.power.adapter import PowerAdapter
from harness_manager.power.adapter import make_power_adapter as _make_power_adapter
from harness_manager.power.config import (
    BOARDS_FILE,
    BoardConfig,
    ConfigError,
    load_boards,
    power_link,
    with_links,
)

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


def _ask_sensors(client: Any) -> tuple[dict[str, Any] | None, str]:
    """`sensors` get on the open connection: (the good reply, "") or (None, why not). Never
    raises: a board without it, or an old pyverify, only means the raw stats path is used."""
    ask = getattr(client, "sensors", None)
    if not callable(ask):
        return None, "the installed pyverify has no sensors()"
    try:
        reply = ask()
    except Exception as exc:  # noqa: BLE001 - the raw stats path still works
        return None, f"{type(exc).__name__}: {exc}"
    if isinstance(reply, dict) and reply.get("ok") is True:
        return reply, ""
    return None, str((reply or {}).get("err") or "no reply") if isinstance(reply, dict) else "no reply"


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


# --- mint 4 `sensors` (net-protocol v0.20): SYSMON, converted by the harness ----------------------

SENSORS_SOURCE = "sensors (harness)"
SENSORS_CAPABILITY = "mps3.sensors"
#: The latched alarm bits, in SYSMON's order, in plain words.
ALARM_WORDS = ("Over temperature", "Temperature above its alarm level",
               "VCCINT outside its window", "VCCAUX outside its window",
               "VCCBRAM outside its window")
#: `sensors set` ranges (net-protocol.md "sensors"); the k range depends on the reference.
TEMP_MAX_C = 125.0
VCCINT_MIN_V, VCCINT_MAX_V = 0.5, 1.2
K_RANGE = {"external": (0.5, 2.0), "internal": (-20.0, 20.0)}
SENSORS_SET_KEYS = ("temp_hi_c", "temp_ot_c", "vccint_lo_v", "vccint_hi_v", "k")
#: Plain sentences for the new verbs' error codes.
SENSORS_CODE_WORDS = {
    "not_supported": "This board's harness does not have sensors (it needs the mint-4 hardware).",
    "no_hw": "The board's temperature and voltage sensor did not answer.",
    "invalid": "The board refused the values",
    "denied": "The board does not allow that.",
    "busy": "The board is busy; try again in a moment.",
    "timeout": "The sensor did not answer in time; try again.",
}


def alarm_words(alm: Any) -> list[str]:
    """The latched alarm bits as words (bit 0 first); an unreadable word says nothing."""
    if isinstance(alm, bool) or not isinstance(alm, int) or alm < 0:
        return []
    return [w for i, w in enumerate(ALARM_WORDS) if alm >> i & 1]


def _num(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def check_sensors_set(fields: Any, ref: str | None) -> dict[str, float]:
    """The values of a `sensors set` request, checked against the contract. All or nothing: any
    refused value raises ``UsageError`` and nothing is sent. ``ref`` is the board's reference
    ("internal"/"external"; None = unknown, the wider range is allowed and the board decides)."""
    if not isinstance(fields, dict) or not fields:
        raise UsageError("nothing to set", hint="give at least one threshold or the calibration")
    unknown = sorted(set(fields) - set(SENSORS_SET_KEYS))
    if unknown:
        raise UsageError(f"cannot set {unknown[0]!r}", hint=", ".join(SENSORS_SET_KEYS))
    out: dict[str, float] = {}
    for k, v in fields.items():
        if not _num(v):
            raise UsageError(f"{k} must be a number")
        out[k] = float(v)
    hi, ot = out.get("temp_hi_c"), out.get("temp_ot_c")
    for name, val in (("temp_hi_c", hi), ("temp_ot_c", ot)):
        if val is not None and not 0 < val <= TEMP_MAX_C:
            raise UsageError(f"{name} must be above 0 and at most {TEMP_MAX_C:g} degC")
    if hi is not None and ot is not None and not hi < ot:
        raise UsageError("the temperature alarm level must be below the over-temperature level")
    lo, up = out.get("vccint_lo_v"), out.get("vccint_hi_v")
    for name, val in (("vccint_lo_v", lo), ("vccint_hi_v", up)):
        if val is not None and not VCCINT_MIN_V <= val <= VCCINT_MAX_V:
            raise UsageError(f"{name} must be between {VCCINT_MIN_V:g} and {VCCINT_MAX_V:g} V")
    if lo is not None and up is not None and not lo < up:
        raise UsageError("the VCCINT lower limit must be below its upper limit")
    if "k" in out:
        lo_k, hi_k = K_RANGE.get(ref or "internal", K_RANGE["internal"])   # unknown: board decides
        if not lo_k <= out["k"] <= hi_k:
            what = "scale" if ref == "external" else "offset in degC"
            raise UsageError(f"the calibration {what} must be between {lo_k:g} and {hi_k:g}")
    return out


def check_sensors_set_against(fields: dict[str, float], thr: dict[str, Any] | None) -> None:
    """One-sided pairs are checked against the thresholds in force (the board does the same)."""
    if not thr:
        return
    hi = fields.get("temp_hi_c", thr.get("temp_hi_c"))
    ot = fields.get("temp_ot_c", thr.get("temp_ot_c"))
    if _num(hi) and _num(ot) and not hi < ot and ("temp_hi_c" in fields or "temp_ot_c" in fields):
        raise UsageError("the temperature alarm level must be below the over-temperature level",
                         hint=f"now {thr.get('temp_hi_c')} and {thr.get('temp_ot_c')} degC")
    lo = fields.get("vccint_lo_v", thr.get("vccint_lo_v"))
    up = fields.get("vccint_hi_v", thr.get("vccint_hi_v"))
    if _num(lo) and _num(up) and not lo < up and ("vccint_lo_v" in fields or "vccint_hi_v" in fields):
        raise UsageError("the VCCINT lower limit must be below its upper limit",
                         hint=f"now {thr.get('vccint_lo_v')} and {thr.get('vccint_hi_v')} V")


def sensors_error(reply: dict[str, Any], what: str) -> HarnessError:
    """A refused `sensors` request as the error Harness Manager shows (the reply's ``code``)."""
    code = str(reply.get("code") or "")
    err = str(reply.get("err") or "")
    words = SENSORS_CODE_WORDS.get(code)
    if code == "locked" or (not code and "board claimed" in err):
        from harness_manager.services.claim import lock_error

        return lock_error(err, what)
    if code == "not_supported" or "unknown op" in err:
        return UnavailableError(SENSORS_CAPABILITY, SENSORS_CODE_WORDS["not_supported"])
    if code == "no_hw":
        return UnavailableError(SENSORS_CAPABILITY, f"{words} [harness: {err or code}]")
    if code == "invalid":
        return UsageError(f"{words}: {err.removeprefix('invalid ') or 'invalid'}; nothing was "
                          "changed")
    return ActionFailedError(f"{what} failed: {words or err or code or 'no reason given'}"
                             + (f" [harness: {err}]" if words and err else ""))


def sensors_body(reply: dict[str, Any]) -> dict[str, Any]:
    """The sensors reply in the daemon's words: the contract keys plus ``alarms`` (plain
    words for the latched bits) and ``cal_words``."""
    body = {k: v for k, v in reply.items() if k not in ("ok", "op", "act")}
    body["alarms"] = alarm_words(reply.get("alm"))
    body["calibrated"] = reply.get("cal") not in (None, "none")
    return body


def sensors_reply_readings(reply: dict[str, Any], observed_at: float | None = None) -> list[Reading]:
    """Converted readings from a good `sensors` reply (calibrated by the board)."""
    stamp = {} if observed_at is None else {"observed_at": observed_at}
    note = ("calibrated by the harness" if reply.get("cal") not in (None, "none")
            else "not calibrated")
    rows: list[Reading] = []
    for name, key, unit in (("fpga_die_temp", "temp_c", "degC"),
                            ("fpga_die_temp_max", "temp_max_c", "degC"),
                            ("fpga_die_temp_min", "temp_min_c", "degC"),
                            ("vccint", "vccint_v", "V"), ("vccaux", "vccaux_v", "V"),
                            ("vccbram", "vccbram_v", "V")):
        v = reply.get(key)
        if _num(v):
            extra = ("maximum since power-up; " if name.endswith("_max")
                     else "minimum since power-up; " if name.endswith("_min") else "")
            rows.append(Reading(name, float(v), unit, SENSORS_SOURCE, reason=extra + note, **stamp))
        else:
            rows.append(Reading.unavailable(name, unit, f"the harness's sensors reply has no {key}",
                                            source=SENSORS_SOURCE))
    return rows


def merge_sensors(base: list[Reading], conv: list[Reading]) -> list[Reading]:
    """The converted rows win by name; rows only the raw path has (rail min/max) are kept."""
    by = {r.name: r for r in conv if r.value is not None}
    out = [by.pop(r.name, r) for r in base]
    out += list(by.values())
    return out


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
        #: the `sensors` reply read with the last harness read: (reply or None, why not)
        self._sensors_last: tuple[dict[str, Any] | None, str] = (None, "")
        self._est_cache: tuple[float, dict[str, vivado.PowerEstimate]] | None = None

    # -- the TelemetryAdapter protocol ------------------------------------------------------

    def readings(self) -> list[Reading]:
        with self._lock:
            return (self._sysmon_readings() + self._touch_readings()
                    + self._power_readings() + self._estimate_readings())

    # -- UI2 G4: the facts beside a board read (services.history.facts_of) ------------------------

    def readings_facts(self) -> dict[str, Any] | None:
        """The last ``stats`` reply this adapter read (it reads one with the telemetry when the
        harness reports ``sysmon``), for ``GET /boards/{bid}``'s ``uptime_s`` and ``stats``
        (docs/API.md "Readings kept by this service"). Never asks the board: None when no
        ``stats`` was read, or its read failed."""
        with self._lock:
            cache = self._shell_cache
        if cache is None:
            return None
        at, _tel, st, err = cache
        if err or not isinstance(st, dict) or st.get("ok") is False:
            return None
        out: dict[str, Any] = {"stats": st, "at": at, "source": "stats (6900)"}
        if st.get("os_up_ms") is not None:          # a Linux harness that sends it
            out["os_up_ms"] = st.get("os_up_ms")
        return out

    # -- the Readings page's Sensors card (daemon/readings_api.py) -------------------------------

    def sensors_get(self) -> dict[str, Any]:
        """``GET /boards/{bid}/sensors``: the `sensors` body (open to any peer). Raises
        ``UnavailableError`` with a plain sentence on a board without feature ``sysmon``."""
        with self._lock:
            self._need_sensors()
            reply = self._shell.call_raw(lambda c, _t: c.sensors())
            if not isinstance(reply, dict) or not reply.get("ok"):
                raise sensors_error(reply if isinstance(reply, dict) else {}, "reading the sensors")
            return sensors_body(reply)

    def sensors_act(self, act: str, fields: dict[str, Any] | None = None) -> dict[str, Any]:
        """``set`` or ``clear_alm`` (CLAIM-LOCKED): validated here, nothing sent if any value is
        refused; sent through the claim forward when the claim is this Harness Manager's."""
        with self._lock:
            self._need_sensors()
            if act not in ("set", "clear_alm"):
                raise UsageError(f"unknown sensors action {act!r}", hint="set or clear_alm")
            what = "changing the alarm thresholds" if act == "set" else "clearing the alarms"
            want: dict[str, float] = {}
            if act == "set":
                state = self.sensors_get()
                want = check_sensors_set(fields, state.get("ref"))
                check_sensors_set_against(want, state.get("thr"))
            elif fields:
                raise UsageError("clear_alm takes no values")

            def msg(c: Any) -> dict[str, Any]:
                return c.sensors_set(**want) if act == "set" else c.sensors_clear_alarms()

            reply = self._sensors_send(msg, what)
            if not isinstance(reply, dict) or not reply.get("ok"):
                raise sensors_error(reply if isinstance(reply, dict) else {}, what)
            self._shell_cache = None          # the next telemetry read sees the new state
            body = sensors_body(reply)
            body["persisted"] = bool(reply.get("persisted")) if act == "set" else None
            return body

    def _sensors_send(self, build: Callable[[Any], dict[str, Any]], what: str) -> dict[str, Any]:
        """The claim-locked path of ``HarnessdSetter``: direct unless the claim is ours."""
        from harness_manager.services.claim import lock_error

        from . import slot_words
        from .net_identity import _route

        def locked(reply: Any) -> bool:
            return (isinstance(reply, dict) and not reply.get("ok")
                    and slot_words.is_claim_lock(str(reply.get("err") or ""),
                                                 slot_words.reply_code(reply)))

        route = _route(self._session, what)
        if route != "board-ssh":
            reply = self._shell.call_raw(lambda c, _t: build(c))
            if not locked(reply):
                return reply
            _route(self._session, what, claimed=True)
        from .card import _forwarded, _gate_key
        from .os_slots import claim_forward
        from .shell import Mps3Shell

        with claim_forward(self._session, "sensors", what) as (host, ctl, _push):
            shell = _forwarded(Mps3Shell(host, ctl, gate_key=_gate_key(self._shell)))
            reply = shell.call_raw(lambda c, _t: build(c))
        if locked(reply):                      # refused even from the board itself
            raise lock_error(str(reply.get("err") or ""), what)
        return reply

    def _need_sensors(self) -> None:
        feats, err = self._features()
        if self._shell is None:
            raise UnavailableError(SENSORS_CAPABILITY, "no Ethernet link to the board's harness")
        if "sysmon" not in feats:
            why = (f"{err}; " if err else "")
            raise UnavailableError(SENSORS_CAPABILITY, why + "this board's harness has no "
                                   "temperature and voltage sensors (it needs the mint-4 hardware)")

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

    def _harness(self, want_telemetry: bool, want_stats: bool,
                 want_sensors: bool = False) -> tuple[dict | None, dict | str | None, str, float]:
        now = self._clock()
        cache = self._shell_cache
        if cache is not None and now - cache[0] < SHELL_MIN_INTERVAL_S:
            return cache[1], cache[2], cache[3], cache[0]

        def ask(client: Any, tap: Any) -> Any:
            replies = harness_replies(client, tap, telemetry=want_telemetry, stats=want_stats)
            return replies, (_ask_sensors(client) if want_sensors else (None, ""))

        try:
            (tel, st), sensors = self._shell.call_raw(ask)
            err = ""
        except HarnessError as exc:
            tel, st, err, sensors = None, None, f"cannot ask the harness: {exc}", (None, "")
        except Exception as exc:  # noqa: BLE001 - a codec surprise is a reason, not a crash
            tel, st, sensors = None, None, (None, "")
            err = f"unexpected harness reply: {type(exc).__name__}: {exc}"
        self._shell_cache = (now, tel, st, err)
        self._sensors_last = sensors
        return tel, st, err, now

    # -- sources -------------------------------------------------------------------------------------

    def _sysmon_readings(self) -> list[Reading]:
        feats, ident_err = self._features()
        rows: list[Reading] = []
        if "sysmon" in feats and self._shell is not None:
            _tel, st, err, when = self._harness("touch_temp" in feats, True, True)
            sample = err or stats_sysmon_sample(st, observed_at=when)
            rows += (_unavailable_sysmon(sample, SHELL_SYSMON_SOURCE) if isinstance(sample, str)
                     else sysmon_readings(sample))
            conv, _why = self._sensors_last
            if conv is not None:          # mint 4: the harness's converted, calibrated values
                rows = merge_sensors(rows, sensors_reply_readings(conv, when))
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
                      "Ethernet link and a release with 'sysmon'")
        else:
            reason = ("needs a JTAG cable on J17 ([boards.<id>.sysmon] in boards.toml), or "
                      "a release with 'sysmon'")
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
            tel, _st, err, when = self._harness(True, "sysmon" in feats, "sysmon" in feats)
            return touch_readings(tel, err, observed_at=when)
        if ident_err:
            reason = ident_err
        elif self._shell is None:
            reason = "needs the Ethernet link to the shell and a release with 'touch_temp'"
        else:
            reason = "needs a release with 'touch_temp'"
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


def board_config(candidate: Candidate,
                 state_dir: Path | None = None) -> tuple[BoardConfig | None, str]:
    """This board's ``boards.toml`` table, or (None, the reason the file is unusable).

    ``state_dir`` is the engine's, when the pack knows it (T9 CCR 5); otherwise the
    state-dir rule (``$HARNESS_MANAGER_STATE_DIR``, else ``~/.config/harness-manager``) applies.
    """
    try:
        boards = load_boards(Path(state_dir) / BOARDS_FILE if state_dir is not None else None)
    except ConfigError as exc:
        return None, str(exc)
    return boards.for_board(candidate.board_id, candidate.links), ""


def _state_dir(session: Any) -> Path | None:
    return getattr(session, "state_dir", None)


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
    board, config_error = board_config(session.candidate, _state_dir(session))
    reader, sysmon_error, interval = _sysmon_from(board)
    est_dir, est_error = _estimates_from(board)
    return Mps3Telemetry(session, board=board, config_error=config_error, sysmon=reader,
                         sysmon_error=sysmon_error, sysmon_interval_s=interval,
                         power=_make_power_adapter(board), estimates_dir=est_dir,
                         estimates_error=est_error)


def make_power_adapter(session: Any) -> PowerAdapter | None:
    """Proposed hook (T9 CCR 1): ``BoardSession.power`` from ``[boards.<id>.power]``."""
    board, _err = board_config(session.candidate, _state_dir(session))
    return _make_power_adapter(board)


def config_links(candidate: Candidate, state_dir: Path | None = None) -> tuple[Link, ...]:
    """Links ``boards.toml`` gives this board: ``SMART_POWER`` for a meter, ``JTAG`` for SYSMON.

    Proposed use (T9 CCR 2): the pack adds these to every candidate it returns, so
    capability negotiation sees ``telemetry_power`` and the JTAG route of
    ``telemetry_temp``. Secret-free: no address here carries a credential.
    """
    board, _err = board_config(candidate, state_dir)
    links: list[Link] = []
    link = power_link(board)
    if link is not None:
        links.append(link)
    reader, _serr, _i = _sysmon_from(board)
    if reader is not None:
        links.append(sysmon_link(reader))
    return tuple(links)


def with_config_links(candidate: Candidate, state_dir: Path | None = None) -> Candidate:
    """``candidate`` plus its ``boards.toml`` links (the one-line pack integration)."""
    return with_links(candidate, config_links(candidate, state_dir))


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
