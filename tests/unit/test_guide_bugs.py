"""GUIDE-BUGS: three rough edges found by a walkthrough of the user guide.

- ``kit fetch``'s hint names no lab path;
- ``xdc rm-kit``: a placeholder ``rm_id`` is a usage error naming the field (not a crash);
  a ``wrapper`` that does not exist says how to get a starting file.
"""

from __future__ import annotations

import json

import pytest

from harness_manager.core.errors import AbsentError, UsageError
from harness_manager.services.kit.service import HubSource
from harness_manager.services.xdc import design


def doc(**kw):
    return {"kind": "rm", "name": "my_soc", **kw}


def test_the_kit_fetch_hint_names_no_lab_path():
    reason = HubSource(None).reason
    assert "kits.hub_dir" in reason and "/home/" not in reason and "david" not in reason
    # twin: a real folder gives no reason at all
    assert HubSource(__import__("pathlib").Path("/")).reason == ""


@pytest.mark.parametrize("bad", ["0x0100XXXX", "TBD", "0x", "1.5", "0x1FFFFFFFF", -1])
def test_a_placeholder_rm_id_is_a_usage_error_naming_the_field(bad):
    with pytest.raises(UsageError) as ei:
        design.from_doc(doc(rm_id=bad))
    msg = f"{ei.value} {getattr(ei.value, 'hint', '')}"
    assert "rm_id" in msg and "0x0100XXXX" in msg


@pytest.mark.parametrize("good", ["0x01000001", "0x8000", 16777217, "", None])
def test_a_real_or_absent_rm_id_still_loads(good):
    design.from_doc(doc(rm_id=good))
    design.from_doc(doc())


def test_a_missing_wrapper_says_how_to_get_one(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps(doc(wrapper="rtl/none.sv")))
    with pytest.raises(AbsentError) as ei:
        design.from_doc(json.loads(p.read_text()), base_dir=tmp_path)
    assert "without the" in ei.value.hint and "skeleton" in ei.value.hint
    # the wrapper is the user's own file: it is never created by loading the design
    assert not (tmp_path / "rtl").exists()
