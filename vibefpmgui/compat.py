"""Small helpers that differ between macOS, Linux and Windows."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

WINDOWS = os.name == "nt"


def split_args(text: str) -> list[str]:
    """Split a command-line string typed by the user into arguments.

    On Windows backslashes are path separators, not escapes, so POSIX
    ``shlex.split`` would turn ``C:\\data\\in.txt`` into ``C:datain.txt``;
    use non-POSIX splitting there and drop the surrounding quotes.
    """
    if not WINDOWS:
        return shlex.split(text)
    parts = shlex.split(text, posix=False)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]


def join_args(args: list[str]) -> str:
    """A command line to show the user, quoted the way their shell expects."""
    return subprocess.list2cmdline(args) if WINDOWS else shlex.join(args)


def quote_arg(arg: str) -> str:
    return subprocess.list2cmdline([arg]) if WINDOWS else shlex.quote(arg)


def file_manager_name() -> str:
    """What the platform's file manager is called (for menu items)."""
    return {"darwin": "Finder", "win32": "Explorer"}.get(sys.platform, "the file manager")


def reveal_label() -> str:
    return {"darwin": "Show in Finder", "win32": "Show in Explorer"}.get(
        sys.platform, "Open Containing Folder")


def set_windows_app_id(app_id: str = "VibeFPMGUI.VibeFPMGUI"):
    """On Windows, give the process its own taskbar identity, so the taskbar
    shows the app's icon instead of grouping it under python.exe."""
    if not WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except (AttributeError, OSError):
        pass


# ------------------------------------------------------------ macOS app name
# Started as "python -m vibefpmgui" (not from an .app bundle), macOS would call
# the app "python": in the menu bar, the Dock, and Qt's application menu
# ("About __main__.py", "Quit __main__.py", from argv[0]). These give it its
# own name; each does nothing elsewhere or if macOS doesn't allow it.
def _objc():
    """(objc_msgSend caller, class lookup, selector lookup), or None."""
    import ctypes
    import ctypes.util

    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    ctypes.cdll.LoadLibrary(ctypes.util.find_library("Foundation"))
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.restype = ctypes.c_void_p

    def send(restype, *argtypes):
        return ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes))

    return send, lambda name: objc.objc_getClass(name.encode()), lambda name: objc.sel_registerName(name.encode())


def set_macos_bundle_name(name: str) -> bool:
    """Before Qt starts: the name in the menu bar (the bundle's CFBundleName,
    which a python executable has none of). True if it was set."""
    if sys.platform != "darwin":
        return False
    try:
        import ctypes

        send, cls, sel = _objc()

        def nsstring(text):
            return send(ctypes.c_void_p, ctypes.c_char_p)(cls("NSString"), sel("stringWithUTF8String:"),
                                                         text.encode())

        bundle = send(ctypes.c_void_p)(cls("NSBundle"), sel("mainBundle"))
        info = send(ctypes.c_void_p)(bundle, sel("infoDictionary"))
        if not info:
            return False
        send(None, ctypes.c_void_p, ctypes.c_void_p)(info, sel("setObject:forKey:"), nsstring(name),
                                                    nsstring("CFBundleName"))
        return True
    except (OSError, AttributeError, TypeError):
        return False


def set_macos_display_name(name: str) -> bool:
    """After Qt starts: the name the Dock shows for the running app. A private
    LaunchServices call (as Chromium uses for its helper processes): if a
    macOS version lacks it, nothing happens. True if it was set."""
    if sys.platform != "darwin":
        return False
    try:
        import ctypes

        services = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        core = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        core.CFStringCreateWithCString.restype = ctypes.c_void_p
        core.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        current = services._LSGetCurrentApplicationASN
        current.restype = ctypes.c_void_p
        set_item = services._LSSetApplicationInformationItem
        set_item.restype = ctypes.c_int
        set_item.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        key = ctypes.c_void_p.in_dll(services, "_kLSDisplayNameKey")
        value = core.CFStringCreateWithCString(None, name.encode(), 0x08000100)  # (UTF-8)
        asn = current()
        return bool(asn) and set_item(-2, asn, key, value, None) == 0
    except (OSError, AttributeError, ValueError, TypeError):
        return False
