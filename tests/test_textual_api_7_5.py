"""Guard the Textual 7.5.0 API surface and keyboard-only policy."""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
UI = APP / "raidforge" / "ui"
ROOT = APP.parent


def _ui_sources():
    for path in sorted(UI.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def test_requirements_pin_textual_7_5():
    text = (APP / "requirements.txt").read_text(encoding="utf-8")
    assert "textual==7.5.0" in text
    assert "rich>=14.2.0" in text


def test_no_removed_textual_api():
    forbidden = {
        r"\.renderable\b": "Static.renderable was replaced by Static.content",
        r"get_cell_value": "DataTable.get_cell_value is gone",
        r"get_value_at": "not present in Textual 7.5.0",
        r"post_message_no_wait": "use post_message",
        r"Select\.NULL": "not present in Textual 7.5.0; use Select.BLANK",
        r"app\.action_": "use App.run_action / Widget.run_action",
        r"on_click\b": "mouse activation is disabled",
        r"on_mouse_": "mouse handlers are disabled",
        r"MouseDown|MouseUp": "mouse events are disabled",
        r"pointer\s*:": "pointer styles are disabled",
    }
    hits = []
    for path in _ui_sources():
        text = path.read_text(encoding="utf-8")
        for pattern, reason in forbidden.items():
            if re.search(pattern, text):
                hits.append(f"{path.relative_to(ROOT)}: {reason}")
    assert not hits, "\n".join(hits)


def test_keyboard_and_action_contracts():
    assert "run_action" in (UI / "screens" / "command_palette.py").read_text(encoding="utf-8")
    assert "Select.BLANK" in (UI / "widgets" / "filter_bar.py").read_text(encoding="utf-8")
    assert "mouse=False" in (APP / "raidforge" / "main.py").read_text(encoding="utf-8")
    tcss = (UI / "styles" / "app.tcss").read_text(encoding="utf-8")
    assert "border: tab" in tcss
    assert "pointer:" not in tcss


def test_navbar_is_focusable_and_has_tab_border():
    text = (UI / "widgets" / "nav_bar.py").read_text(encoding="utf-8")
    assert "can_focus = True" in text
    assert "action_activate" in text
    assert "on_click" not in text


def test_hotkey_bar_is_not_focusable():
    text = (UI / "widgets" / "hotkey_bar.py").read_text(encoding="utf-8")
    assert "can_focus = False" in text
