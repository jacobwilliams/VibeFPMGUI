"""A status line's text getting longer doesn't make the window wider (a
window filling the screen grew past its edge as an AI review's findings
came in)."""

from __future__ import annotations

from conftest import open_editor

from vibefpmgui.widgets import StatusLabel


def test_long_status_text_doesnt_widen_the_window(qtbot, window, project):
    window.show()
    editor = open_editor(window, project / "src/foo.f90")
    qtbot.wait(50)
    narrowest = window.minimumSizeHint().width()
    long = " · ".join(["154 AI review notes"] * 15)
    labels = {"editor status": editor.status}
    for name in ("build_monitor", "test_monitor", "run_monitor", "review_monitor"):
        panel = getattr(window, name)
        labels[f"{name} progress"] = panel.progress_label
        labels[f"{name} subtitle"] = panel.subtitle
    labels["problems title"] = window.problems_panel.title
    labels["search status"] = window.search_panel.status
    for name, label in labels.items():
        assert isinstance(label, StatusLabel), name
        old = label.text()
        label.setText(long)
        qtbot.wait(20)
        assert window.minimumSizeHint().width() == narrowest, name
        assert label.sizeHint().width() > 1000  # (it still asks for its text's width, where there's room)
        label.setText(old)
