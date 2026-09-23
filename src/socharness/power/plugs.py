"""Networked metered outlets: Shelly Gen2, Tasmota, NETIO (JSON API).

Each maps its device's reply onto ``board_power``/``supply_voltage``/``supply_current``
(see ``base``). The wire facts, with their sources:

- **Shelly Gen2** (Plus/Pro, RPC over HTTP; shelly-api-docs.shelly.cloud gen2
  Switch component): ``GET /rpc/Switch.GetStatus?id=N`` answers ``output``,
  ``apower`` (W), ``voltage`` (V), ``current`` (A). ``GET /rpc/Switch.Set?id=N&on=false&toggle_after=S``
  switches off and flips back on after S seconds. Authentication is HTTP Digest,
  SHA-256, user ``admin``.
- **Tasmota** (tasmota.github.io/docs/Commands): ``GET /cm?cmnd=Status%208`` answers
  ``StatusSNS.ENERGY.{Power,Voltage,Current}`` (a list per channel on multi-channel
  devices). ``Power<n>`` answers ``{"POWER<n>":"ON"}`` (``POWER`` on single-relay
  devices). ``Backlog Power<n> Off; Delay <0.1 s units>; Power<n> On`` runs on the
  device. A password goes in the query (``user=..&password=..``); a wrong one gets
  HTTP 401 or ``{"WARNING":"Need user=<username>&password=<password>"}``.
- **NETIO PowerPDU** (NETIO JSON API, "M2M API Protocol JSON"): ``GET /netio.json``
  answers ``GlobalMeasure.Voltage`` (V) and ``Outputs[]`` with ``ID``, ``State``
  (0/1), ``Load`` (W) and ``Current`` (mA). ``POST /netio.json``
  ``{"Outputs":[{"ID":n,"Action":2,"Delay":ms}]}`` is "short off": off for Delay
  ms, then on. Authentication is HTTP Basic.
"""

from __future__ import annotations

from typing import Any

from socharness.core.model import Reading

from .base import AC_CAVEAT, CURRENT, OFF_CAVEAT, POWER, VOLTAGE, SwitchedPlug, number, reading
from .http import HttpFailure


def _caveat(off: bool) -> str:
    return f"{OFF_CAVEAT}; {AC_CAVEAT}" if off else AC_CAVEAT


class ShellyGen2(SwitchedPlug):
    kind = "shelly_gen2"
    scheme = "digest"

    def _status(self) -> dict[str, Any]:
        st = self.http.get("/rpc/Switch.GetStatus", {"id": str(self.cfg.outlet)})
        if not isinstance(st, dict) or "output" not in st:
            raise HttpFailure("reply", f"{self.label}: Switch.GetStatus did not describe a switch")
        return st

    def _measure(self) -> list[Reading]:
        st = self._status()
        caveat = _caveat(st.get("output") is False)
        return [
            reading(*POWER, number(st, "apower"), self.label, what="apower", caveat=caveat),
            reading(*VOLTAGE, number(st, "voltage"), self.label, what="voltage", caveat=caveat),
            reading(*CURRENT, number(st, "current"), self.label, what="current", caveat=caveat),
        ]

    def _is_on(self) -> bool:
        return self._status().get("output") is True

    def _schedule_cycle(self, off_s: float) -> None:
        self.http.get("/rpc/Switch.Set", {"id": str(self.cfg.outlet), "on": "false",
                                          "toggle_after": f"{off_s:g}"})


class Tasmota(SwitchedPlug):
    kind = "tasmota"
    scheme = "none"               # the password travels in the query, hidden from logs

    def _cmd(self, command: str) -> dict[str, Any]:
        auth = self.cfg.auth
        query = {"cmnd": command} if auth is None else {
            "user": auth.user, "password": auth.password.reveal(), "cmnd": command}
        reply = self.http.get("/cm", query, secret_query=auth is not None)
        if isinstance(reply, dict) and "user=" in str(reply.get("WARNING", "")):
            if auth is None:
                raise HttpFailure("auth", f"{self.http.label} needs a password: add power.auth in boards.toml")
            raise HttpFailure("auth", f"{self.http.label} rejected the credentials")
        if not isinstance(reply, dict):
            raise HttpFailure("reply", f"{self.label}: '{command.split()[0]}' did not answer a JSON object")
        return reply

    def _channel(self, energy: dict[str, Any], key: str) -> float | None:
        value = energy.get(key)
        if isinstance(value, list):
            idx = self.cfg.outlet - 1
            return number({key: value[idx]}, key) if 0 <= idx < len(value) else None
        return number(energy, key)

    def _measure(self) -> list[Reading]:
        reply = self._cmd("Status 8")
        sns = reply.get("StatusSNS")
        energy = sns.get("ENERGY") if isinstance(sns, dict) else None
        if not isinstance(energy, dict):
            raise HttpFailure("reply", f"{self.label} reports no ENERGY data: not a metering Tasmota device")
        src = self.label
        return [
            reading(*POWER, self._channel(energy, "Power"), src, what="Power", caveat=AC_CAVEAT),
            reading(*VOLTAGE, self._channel(energy, "Voltage"), src, what="Voltage", caveat=AC_CAVEAT),
            reading(*CURRENT, self._channel(energy, "Current"), src, what="Current", caveat=AC_CAVEAT),
        ]

    def _is_on(self) -> bool:
        n = self.cfg.outlet
        reply = self._cmd(f"Power{n}")
        state = reply.get(f"POWER{n}", reply.get("POWER") if n == 1 else None)
        if state not in ("ON", "OFF"):
            raise HttpFailure("reply", f"{self.label}: Power{n} did not answer ON or OFF")
        return state == "ON"

    def _schedule_cycle(self, off_s: float) -> None:
        n = self.cfg.outlet
        tenths = max(2, round(off_s * 10))
        self._cmd(f"Backlog Power{n} Off; Delay {tenths}; Power{n} On")


class Netio(SwitchedPlug):
    kind = "netio"
    scheme = "basic"

    def _state(self) -> tuple[dict[str, Any], dict[str, Any]]:
        reply = self.http.get("/netio.json")
        outputs = reply.get("Outputs") if isinstance(reply, dict) else None
        if not isinstance(outputs, list):
            raise HttpFailure("reply", f"{self.label}: /netio.json has no Outputs list")
        ids = [o.get("ID") for o in outputs if isinstance(o, dict)]
        out = next((o for o in outputs if isinstance(o, dict) and o.get("ID") == self.cfg.outlet), None)
        if out is None:
            have = ", ".join(str(i) for i in ids) or "none"
            raise HttpFailure("reply", f"{self.label}: the PDU has no output ID {self.cfg.outlet} "
                                       f"(outputs: {have}); check power.outlet in boards.toml")
        glob = reply.get("GlobalMeasure")
        return out, glob if isinstance(glob, dict) else {}

    def _measure(self) -> list[Reading]:
        out, glob = self._state()
        caveat = _caveat(out.get("State") == 0)
        milliamps = number(out, "Current")
        return [
            reading(*POWER, number(out, "Load"), self.label, what="Load", caveat=caveat),
            reading(*VOLTAGE, number(glob, "Voltage"), self.label, what="Voltage", caveat=caveat),
            reading(*CURRENT, None if milliamps is None else milliamps / 1000.0, self.label,
                    what="Current", caveat=caveat),
        ]

    def _is_on(self) -> bool:
        state = self._state()[0].get("State")
        if state not in (0, 1):
            raise HttpFailure("reply", f"{self.label}: output State is {state!r}, not 0 or 1")
        return state == 1

    def _schedule_cycle(self, off_s: float) -> None:
        self.http.post("/netio.json", {"Outputs": [
            {"ID": self.cfg.outlet, "Action": 2, "Delay": int(round(off_s * 1000))}]})
