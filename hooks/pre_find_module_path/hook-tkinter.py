"""Keep tkinter discoverable when PyInstaller's Tcl/Tk probe is unavailable."""


def pre_find_module_path(_hook_api):
    """The spec file provides the Windows Tcl/Tk binaries and data fallback."""
