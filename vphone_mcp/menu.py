"""Drive vphone-cli's own AppKit menu through macOS Accessibility.

vphone-cli records the screen far more cheaply than this server can: its
``VPhoneScreenRecorder`` feeds ``AVAssetWriterInputPixelBufferAdaptor``
straight from the VM view in-process — no PNG per frame, no IPC, no ffmpeg.
It is not on the host control socket though (``{"t":"record"}`` answers
``unknown command``), so the only handle on it is the menu item its
``VPhoneMenuRecord.swift`` installs, which means System Events and the
Accessibility permission.

The item's title is not discoverable from the binary (Swift stores strings of
15 bytes or less inline), so nothing here hardcodes it: the menus are read at
runtime and matched by keyword. :func:`dump_menus` prints what is actually
there when the match fails.
"""

import os
import subprocess

PROCESS_ENV = "VPHONE_MCP_APP_PROCESS"
KEYWORDS_ENV = "VPHONE_MCP_RECORD_MENU_KEYWORDS"
DEFAULT_PROCESS = "vphone-cli"
DEFAULT_KEYWORDS = ("record",)

#: -1719/-1743 are "not allowed assistive access" / "not authorised".
_PERMISSION_CODES = ("-1719", "-1743", "-25211")


class MenuError(RuntimeError):
    """A menu action could not be performed (never a traceback for the agent)."""


def app_process() -> str:
    return os.environ.get(PROCESS_ENV, "").strip() or DEFAULT_PROCESS


def record_keywords() -> tuple[str, ...]:
    raw = os.environ.get(KEYWORDS_ENV, "").strip()
    if not raw:
        return DEFAULT_KEYWORDS
    return tuple(k.strip().lower() for k in raw.split(",") if k.strip())


def _run(script: str, timeout: float = 15.0) -> str:
    """Run one AppleScript, or raise MenuError with an actionable message."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise MenuError("osascript not found — this needs macOS")
    except subprocess.TimeoutExpired:
        raise MenuError(
            "the menu did not respond within "
            f"{timeout:g}s — is a dialog open in the VM window?"
        )
    if proc.returncode != 0:
        message = (proc.stderr or "").strip()
        if any(code in message for code in _PERMISSION_CODES):
            raise MenuError(
                "macOS denied Accessibility control. Grant it to the program "
                "that runs this server (System Settings → Privacy & Security → "
                "Accessibility): the terminal or launchd agent it runs under, "
                "and /usr/bin/osascript. Driving vphone-cli's own recorder is "
                "not possible without it — start_recording(mode='poll') works "
                "without any permission.\n"
                f"osascript said: {message}"
            )
        raise MenuError(message or f"osascript exited {proc.returncode}")
    return proc.stdout.strip()


def _quote(value: str) -> str:
    """Escape a string for embedding in an AppleScript literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def accessibility_status() -> dict:
    """Whether this process may drive the UI. Never raises.

    The answer depends on which process asks: macOS attributes the permission
    to the responsible process, so a launchd agent needs its own executable
    listed, not the terminal a developer happens to test from.
    """
    # `UI elements enabled` answers for the calling process without touching
    # anything. Plain process queries are NOT a probe: they answer even when
    # assistive access is denied — only UI element access is gated.
    try:
        answer = _run('tell application "System Events" to return UI elements enabled')
    except MenuError as exc:
        detail = str(exc)
        return {
            "granted": False,
            "denied": "Accessibility" in detail,
            "detail": detail.splitlines()[-1][:200],
        }
    granted = answer.strip().lower() == "true"
    return {
        "granted": granted,
        "denied": not granted,
        "detail": "" if granted else "assistive access is off for this process",
    }


def app_running() -> bool:
    script = (
        'tell application "System Events" to return (exists process '
        f'"{_quote(app_process())}")'
    )
    return _run(script).lower() == "true"


def dump_menus() -> list[tuple[str, str]]:
    """Every (menu, item) pair in the VM app's menu bar."""
    process = _quote(app_process())
    script = f'''
tell application "System Events"
    if not (exists process "{process}") then return "ERR:no-process"
    tell process "{process}"
        set out to ""
        repeat with mb in menu bar items of menu bar 1
            set mbName to ""
            try
                set mbName to name of mb
            end try
            try
                repeat with mi in menu items of menu 1 of mb
                    try
                        set out to out & mbName & tab & (name of mi) & linefeed
                    end try
                end repeat
            end try
        end repeat
        return out
    end tell
end tell'''
    out = _run(script)
    if out == "ERR:no-process":
        raise MenuError(
            f"no running {app_process()} process — launch a VM first (vm_launch)"
        )
    pairs: list[tuple[str, str]] = []
    for line in out.splitlines():
        menu, _, item = line.partition("\t")
        if item.strip():
            pairs.append((menu.strip(), item.strip()))
    return pairs


def find_item(keywords: tuple[str, ...]) -> tuple[str, str]:
    """The first (menu, item) whose item title contains one of the keywords."""
    pairs = dump_menus()
    for menu, item in pairs:
        lowered = item.lower()
        if any(keyword in lowered for keyword in keywords):
            return menu, item
    listing = "\n  ".join(f"{m} → {i}" for m, i in pairs) or "(no menu items)"
    raise MenuError(
        "no menu item matches "
        + "/".join(keywords)
        + f" in {app_process()}. Set {KEYWORDS_ENV} to the right words. "
        f"Menu items are:\n  {listing}"
    )


def click_item(menu: str, item: str) -> None:
    process = _quote(app_process())
    script = (
        f'tell application "System Events" to tell process "{process}" to '
        f'click menu item "{_quote(item)}" of menu 1 of menu bar item '
        f'"{_quote(menu)}" of menu bar 1'
    )
    _run(script)


def toggle_recording() -> str:
    """Click vphone-cli's recording item; returns the title that was clicked."""
    menu, item = find_item(record_keywords())
    click_item(menu, item)
    return item


def read_dialog() -> dict:
    """Texts and buttons of the app's front window, when it is a dialog."""
    process = _quote(app_process())
    script = f'''
tell application "System Events"
    if not (exists process "{process}") then return ""
    tell process "{process}"
        if not (exists window 1) then return ""
        set out to ""
        try
            repeat with t in static texts of window 1
                try
                    set out to out & "T" & tab & (value of t as text) & linefeed
                end try
            end repeat
        end try
        try
            repeat with b in buttons of window 1
                try
                    set out to out & "B" & tab & (name of b) & linefeed
                end try
            end repeat
        end try
        return out
    end tell
end tell'''
    texts: list[str] = []
    buttons: list[str] = []
    for line in _run(script).splitlines():
        kind, _, value = line.partition("\t")
        if kind == "T" and value.strip():
            texts.append(value.strip())
        elif kind == "B" and value.strip():
            buttons.append(value.strip())
    return {"texts": texts, "buttons": buttons}


def dismiss_dialog(avoid: tuple[str, ...] = ("reveal",)) -> str:
    """Click the front dialog's dismiss button; returns its name, or ""."""
    dialog = read_dialog()
    for name in dialog["buttons"]:
        if any(word in name.lower() for word in avoid):
            continue
        process = _quote(app_process())
        _run(
            f'tell application "System Events" to tell process "{process}" to '
            f'click button "{_quote(name)}" of window 1'
        )
        return name
    return ""
