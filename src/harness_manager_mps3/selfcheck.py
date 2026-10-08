"""Startup self-check: the pyverify this process loaded has what the MPS3 pack needs.

Harness Manager ships with a vendored pyverify (``vendor/mps3_pyverify-*.whl``). A venv
can end up with a different one (an editable install of a platform checkout on another
branch). Without this check that shows up much later as ``cannot import name 'slot'`` and
a generic "internal error". Here the service names the pyverify it found and the fix.

The list of what the pack needs is read from the pack's own source: every module-level
``from pyverify... import ...`` in this package (so it cannot go stale).
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from harness_manager.core.errors import HarnessError

FIX = ("run `make venv` in the harness-manager checkout (it installs the vendored pyverify "
       "wheel), or reinstall: pip install --force-reinstall vendor/mps3_pyverify-*.whl")


def needed(package_dir: Path | None = None) -> dict[str, set[str]]:
    """``{pyverify module: {names}}`` imported at module level by this package."""
    root = package_dir or Path(__file__).resolve().parent
    out: dict[str, set[str]] = {}
    for path in sorted(root.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and (
                    node.module == "pyverify" or node.module.startswith("pyverify.")):
                out.setdefault(node.module, set()).update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "pyverify" or a.name.startswith("pyverify."):
                        out.setdefault(a.name, set())
    return out


def missing(package_dir: Path | None = None) -> list[str]:
    """What is absent from the loaded pyverify, as ``module`` or ``module.name`` strings."""
    gone: list[str] = []
    for module, names in sorted(needed(package_dir).items()):
        try:
            mod = importlib.import_module(module)
        except ImportError:
            gone.append(module)
            continue
        for name in sorted(names):
            if name == "*" or hasattr(mod, name):
                continue
            try:
                importlib.import_module(f"{module}.{name}")      # a submodule: `from pyverify import slot`
            except ImportError:
                gone.append(f"{module}.{name}")
    return gone


def pyverify_where() -> str:
    try:
        import pyverify
        return str(Path(pyverify.__file__).resolve().parent)
    except Exception:  # noqa: BLE001
        return "(pyverify is not importable)"


def check(package_dir: Path | None = None) -> None:
    """Raise one clear error when the loaded pyverify lacks something the pack imports."""
    gone = missing(package_dir)
    if gone:
        shown = ", ".join(gone[:5]) + (f" and {len(gone) - 5} more" if len(gone) > 5 else "")
        raise HarnessError(
            f"the pyverify this install loaded is not the one Harness Manager ships with: it "
            f"lacks {shown}. It is loaded from {pyverify_where()}",
            hint=FIX)
