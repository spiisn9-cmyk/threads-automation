"""Contract test: every `service.X` called in dashboard/app.py must exist.

Guards against the AttributeError class of bugs (e.g. app.py calling
service.read_draft_groups / service.list_references before they're implemented).
Parses app.py with ast (no Streamlit import needed) and checks each attribute
against the actually-importable dashboard.service module.
"""
from __future__ import annotations

import ast
import pathlib

from dashboard import service

_APP = pathlib.Path(__file__).resolve().parents[1] / "dashboard" / "app.py"


def _service_attrs_used_in_app() -> set[str]:
    tree = ast.parse(_APP.read_text(encoding="utf-8"))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "service"
    }


def test_all_service_calls_in_app_exist():
    used = _service_attrs_used_in_app()
    assert used, "expected app.py to reference service.* helpers"
    missing = sorted(name for name in used if not hasattr(service, name))
    assert not missing, f"dashboard/app.py calls undefined service functions: {missing}"


def test_key_helpers_present_and_callable():
    # the two that triggered the reported AttributeError, plus the writers
    for name in (
        "read_draft_groups",
        "list_references",
        "add_reference",
        "set_reference_active",
        "save_rows",
    ):
        assert callable(getattr(service, name)), f"service.{name} missing/not callable"
