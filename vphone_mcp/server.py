"""vphone-mcp: MCP server for programmatic iOS VM control."""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .actions import (
    APP_SWITCHER,
    BACK_BUTTON,
    CONTROL_CENTER,
    NEXT_PAGE,
    NOTIFICATION_CENTER,
    PREV_PAGE,
    SCROLL_DOWN,
    SCROLL_UP,
    SEARCH_BAR,
    app_position,
)
from .cli import (
    COMMANDS,
    format_result,
    resolve_cli_bin,
    run,
    run_background,
)
from .client import VPhoneClient, image_block

mcp = FastMCP("vphone")

# ---------------------------------------------------------------------------
# Socket discovery
# ---------------------------------------------------------------------------

def _glob_vphone_socks(base: str) -> list[str]:
    """All <base>/<name>/vphone.sock paths, sorted, never raising."""
    try:
        return sorted(str(p) for p in Path(base).glob("*/vphone.sock"))
    except OSError:
        return []


def _socket_path() -> str:
    """Resolve the vphone.sock hostctl socket path.

    Priority: $VPHONE_SOCK env var > first existing <vmlib>/<name>/vphone.sock
    where vmlib = $VPHONE_LIBRARY_ROOT or ~/.vphone/VMs (glob */vphone.sock,
    sorted) > $VPHONE_ROOT/VMs/*/vphone.sock (when $VPHONE_ROOT is set) >
    legacy development paths. Falls back to a default path that produces the
    existing clean "socket not found" error when nothing exists.
    """
    if env := os.environ.get("VPHONE_SOCK"):
        return env
    vmlib = os.environ.get("VPHONE_LIBRARY_ROOT") or str(
        Path.home() / ".vphone" / "VMs"
    )
    candidates = _glob_vphone_socks(vmlib)
    if root := os.environ.get("VPHONE_ROOT"):
        candidates += _glob_vphone_socks(str(Path(root) / "VMs"))
    candidates += [
        str(Path.home() / "localdev" / "experiments" / "vphone-cli" / "vm" / "vphone.sock"),
        str(Path.cwd() / "vm" / "vphone.sock"),
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return str(Path.home() / ".vphone" / "VMs" / "vphone.sock")


def _client() -> VPhoneClient:
    return VPhoneClient(_socket_path())


def _require_ok(resp: dict) -> str:
    """Return success message or raise with error detail."""
    if resp.get("ok"):
        return resp.get("path") or "ok"
    raise RuntimeError(resp.get("error", "unknown error"))


def _text_result(status: str, resp: dict) -> str | list:
    """Status string alone, or status + inline compact screenshot if the
    socket returned one (every hostctl command does when screen=True)."""
    blocks = image_block(resp)
    if blocks:
        return [{"type": "text", "text": status}] + blocks
    return status


# ---------------------------------------------------------------------------
# Layer 1: Hardware keys
# ---------------------------------------------------------------------------

@mcp.tool()
def go_home(*, screen: bool = True, delay: int = 500) -> str | list:
    """Press the home button to return to the home screen.

    Returns the socket's inline screenshot of the result when available.
    """
    resp = _client().key("home", screen=screen, delay=delay)
    return _text_result(_require_ok(resp), resp)


@mcp.tool()
def press_power(*, screen: bool = True, delay: int = 500) -> str | list:
    """Press the power button (lock/wake).

    Returns the socket's inline screenshot of the result when available.
    """
    resp = _client().key("power", screen=screen, delay=delay)
    return _text_result(_require_ok(resp), resp)


@mcp.tool()
def volume_up(*, screen: bool = True, delay: int = 500) -> str | list:
    """Press volume up.

    Returns the socket's inline screenshot of the result when available.
    """
    resp = _client().key("volup", screen=screen, delay=delay)
    return _text_result(_require_ok(resp), resp)


@mcp.tool()
def volume_down(*, screen: bool = True, delay: int = 500) -> str | list:
    """Press volume down.

    Returns the socket's inline screenshot of the result when available.
    """
    resp = _client().key("voldown", screen=screen, delay=delay)
    return _text_result(_require_ok(resp), resp)


# ---------------------------------------------------------------------------
# Layer 2: Screenshots / clipboard
# ---------------------------------------------------------------------------

@mcp.tool()
def screenshot(
    save_path: str | None = None, include_full_png: bool = False
) -> list:
    """Take a screenshot of the VM display.

    Saves the full-resolution PNG to save_path when given (any directory
    the agent chooses, e.g. an evidence folder), otherwise to
    /tmp/vphone-mcp-screen.png. Returns the socket's compact grayscale
    preview inline. Set include_full_png=True to also embed the full PNG
    inline (large — prefer reading the saved file directly instead).
    """
    path = save_path or os.path.join(tempfile.gettempdir(), "vphone-mcp-screen.png")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    resp = _client().screenshot(path)
    _require_ok(resp)

    blocks: list[dict] = []
    if include_full_png:
        image_data = Path(path).read_bytes()
        blocks.append(
            {
                "type": "image",
                "data": base64.b64encode(image_data).decode(),
                "mimeType": "image/png",
            }
        )
    return blocks + image_block(resp)


@mcp.tool()
def set_clipboard(
    text: str, *, screen: bool = True, delay: int = 500
) -> str | list:
    """Set the guest clipboard to the given text.

    IMPORTANT: this sets the guest clipboard via the hostctl 'type' command —
    it does NOT type into the keyboard. The host control socket has no channel
    for synthetic keystrokes; apps read the value via paste.
    """
    resp = _client().type_text(text, screen=screen, delay=delay)
    return _text_result(_require_ok(resp), resp)


# ---------------------------------------------------------------------------
# Layer 3: Pre-mapped navigation
# ---------------------------------------------------------------------------

@mcp.tool()
def open_app(name: str) -> str | list:
    """Open an app from the home screen by name.

    First presses home to ensure we're on the home screen, then taps the app.

    Supported apps: FaceTime, Calendar, Photos, Mail, Notes, Reminders,
    Clock, TV, Games, App Store, Maps, Health, Wallet, Settings,
    Phone, Safari, Messages, Music.
    """
    pos = app_position(name)
    if pos is None:
        raise ValueError(
            f"Unknown app '{name}'. Use tap() for apps not on the default home screen, "
            f"or use open_url() to launch by URL scheme."
        )
    # Go home first to ensure we're on page 1
    _require_ok(_client().key("home"))
    time.sleep(0.5)
    resp = _client().tap(pos[0], pos[1])
    return _text_result(_require_ok(resp), resp)


@mcp.tool()
def tap_back() -> str | list:
    """Tap the iOS navigation back button (top-left corner)."""
    resp = _client().tap(BACK_BUTTON[0], BACK_BUTTON[1])
    return _text_result(_require_ok(resp), resp)


@mcp.tool()
def open_search() -> str | list:
    """Tap the Search bar on the home screen."""
    resp = _client().tap(SEARCH_BAR[0], SEARCH_BAR[1])
    return _text_result(_require_ok(resp), resp)


@mcp.tool()
def scroll_down() -> str | list:
    """Scroll down on the current screen."""
    resp = _client().swipe(**SCROLL_DOWN)
    return _text_result(_require_ok(resp), resp)


@mcp.tool()
def scroll_up() -> str | list:
    """Scroll up on the current screen."""
    resp = _client().swipe(**SCROLL_UP)
    return _text_result(_require_ok(resp), resp)


@mcp.tool()
def open_notification_center() -> str | list:
    """Swipe down from the top-left to open Notification Center."""
    resp = _client().swipe(**NOTIFICATION_CENTER)
    return _text_result(_require_ok(resp), resp)


@mcp.tool()
def open_control_center() -> str | list:
    """Swipe down from the top-right to open Control Center."""
    resp = _client().swipe(**CONTROL_CENTER)
    return _text_result(_require_ok(resp), resp)


@mcp.tool()
def open_app_switcher() -> str | list:
    """Slow swipe up from bottom to open the App Switcher."""
    resp = _client().swipe(**APP_SWITCHER)
    return _text_result(_require_ok(resp), resp)


@mcp.tool()
def swipe_to_next_page() -> str | list:
    """Swipe left to go to the next home screen page."""
    resp = _client().swipe(**NEXT_PAGE)
    return _text_result(_require_ok(resp), resp)


@mcp.tool()
def swipe_to_previous_page() -> str | list:
    """Swipe right to go to the previous home screen page."""
    resp = _client().swipe(**PREV_PAGE)
    return _text_result(_require_ok(resp), resp)


# ---------------------------------------------------------------------------
# Layer 4: Raw interaction (for app-specific UI)
# ---------------------------------------------------------------------------

@mcp.tool()
def tap(
    x: int, y: int, *, screen: bool = True, delay: int = 500
) -> str | list:
    """Tap at specific pixel coordinates on the screen.

    Coordinates are in pixels matching the screenshot dimensions (1290x2796).
    Use screenshot() first to identify the target position.

    Args:
        x: Horizontal pixel coordinate (0=left, 1290=right)
        y: Vertical pixel coordinate (0=top, 2796=bottom)
        screen: Include the socket's inline screenshot of the result
        delay: Settle time in ms before the screenshot is captured
    """
    resp = _client().tap(x, y, screen=screen, delay=delay)
    return _text_result(_require_ok(resp), resp)


@mcp.tool()
def swipe(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration_ms: int = 300,
    *,
    screen: bool = True,
    delay: int = 500,
) -> str | list:
    """Swipe from one point to another.

    Coordinates are in pixels matching the screenshot dimensions (1290x2796).

    Args:
        x1: Start X coordinate
        y1: Start Y coordinate
        x2: End X coordinate
        y2: End Y coordinate
        duration_ms: Swipe duration in milliseconds (default 300)
        screen: Include the socket's inline screenshot of the result
        delay: Settle time in ms before the screenshot is captured
    """
    resp = _client().swipe(
        x1, y1, x2, y2, ms=duration_ms, screen=screen, delay=delay
    )
    return _text_result(_require_ok(resp), resp)


# ---------------------------------------------------------------------------
# Environment status
# ---------------------------------------------------------------------------

def _running_launch_pids() -> list[int]:
    """PIDs of running vphone-cli processes (vm launch / boot are blocking)."""
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "vphone-cli"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(p) for p in proc.stdout.split() if p.strip().isdigit()]


def _guest_ip() -> str:
    """Best-effort guest IP: launch-log serial scan + arp on the NAT bridge.

    vm launch streams the guest serial console into its log file, but iOS
    stays quiet about networking there; the reliable signal is the arp table
    on the Virtualization.framework NAT bridge (bridge100, 192.168.x.y).
    Returns a comma-joined list of candidates or "(none found)".
    """
    import re

    log_dir = Path.home() / ".vphone" / "vphone-mcp" / "logs"
    pat = re.compile(
        r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"
    )
    found: list[str] = []
    try:
        logs = sorted(
            log_dir.glob("*_launch_*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:3]
        for logf in logs:
            try:
                text = logf.read_text(errors="ignore")
            except OSError:
                continue
            for m in pat.finditer(text):
                if m.group() not in found:
                    found.append(m.group())
    except OSError:
        pass
    # arp fallback: the NAT bridge's table lists the guest next to the
    # host-side .1 (filtered out).
    try:
        out = subprocess.run(
            ["arp", "-an"], capture_output=True, text=True, timeout=5
        ).stdout
        for line in out.splitlines():
            if " bridge" not in line:
                continue
            for m in pat.finditer(line):
                ip = m.group()
                if ip.endswith(".1") or ip in found:
                    continue
                found.append(ip)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ", ".join(found) or "(none found)"


@mcp.tool()
def vphone_status() -> str:
    """Report the vphone environment: CLI binary, socket, VMs, running launches.

    Never raises: every probe is guarded and reported inline, so the LLM can
    diagnose an unready environment (missing binary, no VM running) instead
    of getting a traceback.
    """
    lines: list[str] = []

    # vphone-cli binary
    try:
        cli_bin = resolve_cli_bin()
        lines.append(f"vphone-cli binary: {cli_bin}")
        lines.append(
            "vphone-cli in PATH: " + ("yes" if shutil.which("vphone-cli") else "no")
        )
    except RuntimeError as exc:
        lines.append(f"vphone-cli binary: NOT FOUND — {exc}")

    # socket
    sock = _socket_path()
    lines.append(f"socket: {sock}")
    lines.append(f"socket exists: {'yes' if Path(sock).exists() else 'no'}")

    # vm list --json
    try:
        res = run("vm_list", json=True)
        lines.append(f"`vm list --json` exit code: {res['exit_code']}")
        if res["exit_code"] == 0:
            try:
                vms = json.loads(res["stdout"] or "[]")
                if isinstance(vms, list):
                    names = ", ".join(
                        str(v.get("name")) if isinstance(v, dict) else str(v)
                        for v in vms
                    ) or "(none)"
                    lines.append(f"  {len(vms)} VM(s): {names}")
                else:
                    lines.append(f"  raw output: {res['stdout'].strip()}")
            except (TypeError, ValueError):
                lines.append(f"  raw output: {res['stdout'].strip()}")
        else:
            lines.append(f"  stderr: {res['stderr'].strip()}")
    except RuntimeError as exc:
        lines.append(f"`vm list --json`: failed — {exc}")

    # guest IP (parsed from launch-log serial output)
    lines.append(f"guest IP (from launch logs): {_guest_ip()}")

    # running launch processes
    try:
        pids = _running_launch_pids()
    except Exception:
        pids = []
    lines.append(
        "running vphone-cli processes (pgrep -f vphone-cli): "
        + (", ".join(str(p) for p in pids) if pids else "none")
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI tools (one per COMMANDS registry entry, generated at import time)
# ---------------------------------------------------------------------------
# Tools are generated with real signatures (no **kwargs) because FastMCP
# introspects the function signature to build its schema. exec-based function
# generation is standard for this pattern and keeps the registry data-driven.

def _spec_kind(spec: dict) -> str:
    for kind in ("flag", "option", "option_multi", "positional"):
        if kind in spec:
            return kind
    raise ValueError(f"option spec has no recognized kind: {spec!r}")


def _py_type_name(t) -> str:
    return {int: "int", str: "str", bool: "bool"}.get(t, "str")


def _cli_tool_docstring(cmd_key: str, entry: dict) -> str:
    doc = entry["help"]
    if entry.get("allow_background"):
        doc += (
            "\n\nLong-running: set background=True to detach the command "
            "(returns {\"started\": pid, \"log\": path} immediately) — the MCP "
            "transport caps upstream calls at ~60s, so background mode is "
            "required for this command."
        )
    if entry.get("confirm"):
        if cmd_key == "vm_delete":
            doc += (
                "\n\nRequires confirm=True, unless force=True — mirrors the "
                "CLI's -f flag, which skips its y/N prompt."
            )
        else:
            doc += "\n\nRequires confirm=True — destructive or privileged operation."
    return doc


def _register_cli_tools() -> None:
    """Create one FastMCP tool per COMMANDS registry entry."""
    def _emit_spec(pname: str, spec: dict) -> None:
        kind = _spec_kind(spec)
        tname = _py_type_name(spec.get("type"))
        if kind == "flag":
            sig_parts.append(
                f"{pname}: bool = {bool(spec.get('default', False))!r}"
            )
        elif kind == "option_multi":
            sig_parts.append(
                f"{pname}: int = {int(spec.get('default', 0))!r}"
            )
        else:  # option / positional
            if spec.get("required"):
                sig_parts.append(f"{pname}: {tname}")
            elif spec.get("default") is not None:
                sig_parts.append(f"{pname}: {tname} = {spec['default']!r}")
            else:
                sig_parts.append(f"{pname}: {tname} | None = None")
        call_parts.append(f"{pname}={pname}")
        if spec.get("env_fallback"):
            # Secrets (e.g. sudo_password) fall back to an env var so they
            # never have to appear in agent context.
            env_fallback_lines.append(
                f"    if {pname} is None:\n"
                f"        {pname} = os.environ.get({spec['env_fallback']!r})"
            )

    for cmd_key, entry in COMMANDS.items():
        sig_parts: list[str] = []
        call_parts: list[str] = []
        env_fallback_lines: list[str] = []
        # Python signatures cannot have defaulted params before required ones,
        # so emit required params first (call site uses keyword args, so the
        # resulting order is irrelevant to execution).
        for pname, spec in entry["options"].items():
            if spec.get("required"):
                _emit_spec(pname, spec)
        for pname, spec in entry["options"].items():
            if not spec.get("required"):
                _emit_spec(pname, spec)

        body: list[str] = []
        if entry.get("confirm"):
            if cmd_key == "vm_delete":
                body.append("    if not (confirm or force):")
                body.append(
                    '        raise ValueError("vm_delete requires confirm=True '
                    '(or force=True) to run")'
                )
                # The MCP confirm gate IS the human approval; translate it to
                # the CLI's -f so its own y/N prompt (which has no TTY here)
                # never blocks.
                body.append("    if confirm:")
                body.append("        force = True")
            else:
                body.append("    if not confirm:")
                body.append(
                    f"        raise ValueError({cmd_key!r} + ' requires "
                    "confirm=True (destructive or privileged operation)')"
                )
            sig_parts.append("confirm: bool = False")

        body.extend(env_fallback_lines)

        if entry.get("allow_background"):
            body.append("    if background:")
            body.append(
                f"        _res = run_background({cmd_key!r}, {', '.join(call_parts)})"
            )
            body.append(
                '        return json.dumps({"started": _res["pid"], '
                '"log": _res["log_path"], "argv": _res["argv"]})'
            )
            body.append(
                f"    return format_result(run({cmd_key!r}, {', '.join(call_parts)}))"
            )
            sig_parts.append("background: bool = False")
        elif entry.get("background"):
            body.append(
                f"    _res = run_background({cmd_key!r}, {', '.join(call_parts)})"
            )
            body.append(
                '    return json.dumps({"started": _res["pid"], '
                '"log": _res["log_path"], "argv": _res["argv"]})'
            )
        else:
            body.append(
                f"    return format_result(run({cmd_key!r}, {', '.join(call_parts)}))"
            )

        src = f"def {cmd_key}({', '.join(sig_parts)}):\n" + "\n".join(body)
        ns: dict = {
            "run": run,
            "run_background": run_background,
            "format_result": format_result,
            "json": json,
            "os": os,
        }
        exec(compile(src, f"<generated-tool-{cmd_key}>", "exec"), ns)
        func = ns[cmd_key]
        func.__name__ = cmd_key
        func.__doc__ = _cli_tool_docstring(cmd_key, entry)
        mcp.tool()(func)


_register_cli_tools()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
