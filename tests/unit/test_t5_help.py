"""Team T5: the tool owns its help text; ``help --tabs`` is what the GUI renders."""

from __future__ import annotations

import json

import pytest

from harness_manager.cli.helptext import parse_tabs, render_tabs, tab_names, tabs
from harness_manager.cli.main import main, make_parser
from harness_manager.cli.output import TSV_COLUMNS
from harness_manager.core.errors import ExitCode


def run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_help_tabs_parses_into_the_named_sections(capsys):
    rc, out, _ = run(capsys, "help", "--tabs")
    sections = parse_tabs(out)
    assert rc == 0 and list(sections) == tab_names()
    assert all(text.strip() for text in sections.values())
    assert out.splitlines()[0] == "## Overview"


def test_text_without_section_markers_parses_to_nothing():
    assert parse_tabs("plain text\nno markers\n") == {}


def test_render_and_parse_round_trip():
    assert parse_tabs(render_tabs(tabs())) == {n: t.strip("\n") for n, t in tabs()}


def test_one_tab_by_name_any_case(capsys):
    rc, out, _ = run(capsys, "help", "--tabs", "board CONTROLLER")
    assert rc == 0 and list(parse_tabs(out)) == ["Board controller"]


def test_an_unknown_tab_is_usage_and_names_the_tabs(capsys):
    rc, out, err = run(capsys, "help", "--tabs", "Nonesuch")
    assert rc == ExitCode.USAGE and out == "" and "Quick start" in err


def test_the_output_tab_documents_every_tsv_layout(capsys):
    _, out, _ = run(capsys, "help", "--tabs", "Output")
    for layout, cols in TSV_COLUMNS.items():
        assert f"{layout}" in out and " ".join(cols) in out


def test_the_exit_code_tab_documents_every_code(capsys):
    _, out, _ = run(capsys, "help", "--tabs", "Exit codes")
    for code in ExitCode:
        assert code.name in out


def test_help_json_carries_the_same_text(capsys):
    rc, out, _ = run(capsys, "--json", "help", "--tabs")
    got = {t["name"]: t["text"] for t in json.loads(out)["tabs"]}
    assert rc == 0 and got == dict(tabs())


def test_help_tsv_rows_number_each_tab_from_one(capsys):
    rc, out, _ = run(capsys, "--tsv", "help", "--tabs", "Reset")
    rows = [line.split("\t") for line in out.splitlines()]
    assert rc == 0 and all(len(r) == 3 and r[0] == "Reset" for r in rows)
    assert [int(r[1]) for r in rows] == list(range(1, len(rows) + 1))


def _verbs() -> list[str]:
    sub = next(a for a in make_parser()._actions if a.dest == "cmd")
    return sorted(sub.choices)


@pytest.mark.parametrize("verb", _verbs())
def test_every_verb_has_help(capsys, verb):
    rc, out, _ = run(capsys, "help", verb)
    assert rc == 0 and f"harness-manager {verb}" in out


def test_help_for_an_unknown_verb_lists_the_verbs(capsys):
    rc, _, err = run(capsys, "help", "frobnicate")
    assert rc == ExitCode.USAGE and "program" in err


def test_verbs_with_a_tsv_layout_print_their_columns_in_help(capsys):
    _, out, _ = run(capsys, "help", "info")
    flat = " ".join(out.split())                       # argparse wraps the epilog
    assert "--tsv columns: " + " ".join(TSV_COLUMNS["info"]) in flat
    _, out, _ = run(capsys, "help", "lab")             # twin: a verb group has no one layout
    assert "--tsv columns" not in out
