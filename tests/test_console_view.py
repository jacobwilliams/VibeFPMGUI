"""The Output panel: fpm's build progress ("[ 25%]") drawn as badges, blue
at 0% to green at 100%, over the text (which stays as fpm wrote it)."""

from __future__ import annotations

from vibefpmgui.console_view import ConsoleView, progress_color

OUTPUT = ("$ fpm build\n[  0%]               demo_kinds.f90\n[ 26%]     sqpopt_spectral_module.f90  done.\n"
          "not progress [ 50%]\n[100%] Project compiled successfully.\n[fpm exited with code 0]")


def test_blue_to_green():
    blue, green = progress_color(0), progress_color(100)
    assert blue.blue() > blue.green() and green.green() > green.blue()
    hues = [progress_color(p).hsvHueF() for p in (0, 25, 50, 75, 100)]
    assert hues == sorted(hues, reverse=True)  # (blue's hue down to green's, evenly)
    assert progress_color(-5) == blue and progress_color(140) == green


def test_the_badges_over_the_progress(qtbot, window):
    view = window.console
    assert isinstance(view, ConsoleView)
    view.setPlainText(OUTPUT)
    view.resize(600, 300)
    view.show()
    qtbot.waitExposed(view)
    badges = view.progress_badges()
    assert [percent for percent, _rect in badges] == [0, 26, 100]  # (only at a line's start)
    first, second = badges[0][1], badges[1][1]
    assert first.left() == second.left() and first.top() < second.top() and first.width() > 0
    assert "[ 26%]     sqpopt_spectral_module.f90  done." in view.toPlainText()  # (copied as fpm wrote it)
    image = view.viewport().grab(first.toRect().adjusted(2, 2, -2, -2)).toImage()
    color = image.pixelColor(2, image.height() // 2)  # (the badge's fill, at its left edge)
    assert abs(color.hsvHueF() - progress_color(0).hsvHueF()) < 0.05
