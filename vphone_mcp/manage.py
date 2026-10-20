"""Management CLI for vphone-mcp.

``vphone-mcp`` with no arguments (or ``serve``) runs the MCP server, exactly
as before — MCP clients launch it bare, so the default must stay serve. The
management subcommands make the host easy to reconfigure when vphone-cli
updates, and to keep the AMFI allowlist in shape:

    vphone-mcp versions            list installed vphone-cli versions
    vphone-mcp select <version>    pick which one the server drives
    vphone-mcp doctor              probe the env + validate the registry
    vphone-mcp escalator status    AMFI code-requirement allowlist state
    vphone-mcp escalator allow     allow vphone-vm (the companion VM binary)
"""

import json
import os
import sys

from . import cli


def _usage(stream=None) -> None:
    out = stream or sys.stdout
    print(
        "usage: vphone-mcp [serve] [--transport ...] | <command>\n"
        "\n"
        "commands:\n"
        "  versions            list installed vphone-cli versions\n"
        "  select <version>    pick which version the server drives\n"
        "  doctor              probe env + validate the registry against the binary\n"
        "  escalator status    AMFI code-requirement allowlist state (needs root)\n"
        "  escalator allow     allow vphone-vm, the companion VM binary (needs root)\n"
        "\n"
        "no command (or 'serve') runs the MCP server on stdio/HTTP.",
        file=out,
    )


def _print_versions() -> int:
    installed = cli.installed_versions()
    if not installed:
        print("no vphone-launchpad bundles found under", cli.BUNDLES_ROOT)
        return 1
    try:
        active = cli.detect_version(cli.resolve_cli_bin())
    except RuntimeError:
        active = None
    selected = cli.selected_version()
    print("installed vphone-cli versions:")
    for version in installed:
        marks = []
        if version == active:
            marks.append("active")
        if version == selected and version != active:
            marks.append("selected")
        suffix = f"  <-- {'/'.join(marks)}" if marks else ""
        print(f"  {version}{suffix}")
    if selected and selected not in installed:
        print(f"warning: selected version {selected!r} is not installed")
    return 0


def _select(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: vphone-mcp select <version>", file=sys.stderr)
        return 2
    version = argv[0]
    if version not in cli.installed_versions():
        print(f"error: version {version!r} is not installed", file=sys.stderr)
        _print_versions()
        return 1
    candidate = cli.bundle_bin(version)
    if not (candidate.is_file() and os.access(candidate, os.X_OK)):
        print(f"error: version {version!r} has no executable at {candidate}", file=sys.stderr)
        return 1
    cli.select_version(version)
    print(f"selected vphone-cli {version}")
    print("binary:", candidate)
    if os.environ.get("VPHONE_CLI_BIN") or os.environ.get("VPHONE_CLI_VERSION"):
        print("note: $VPHONE_CLI_BIN / $VPHONE_CLI_VERSION override the selection")
    print("(the MCP server picks this up on its next start)")
    return 0


def _doctor() -> int:
    print("vphone-mcp doctor")
    print("=" * 62)
    try:
        binary = cli.resolve_cli_bin()
        print(f"vphone-cli binary:  {binary}")
    except RuntimeError as exc:
        print(f"vphone-cli binary:  NOT FOUND — {exc}")
        return 1
    version = cli.detect_version(binary)
    print(f"detected version:   {version or 'unknown'}")
    print(f"registry version:   {cli.REGISTRY_VERSION}")
    if version and version != cli.REGISTRY_VERSION:
        print(
            f"  !! registry was verified against {cli.REGISTRY_VERSION}; "
            "expect drift (reported below)"
        )
    print()
    print("registry validation (every subcommand + flag, against the binary):")
    findings = cli.validate_registry(binary)
    if not findings:
        print("  ✓ all registry entries match the binary")
    else:
        print(f"  {len(findings)} mismatch(es) — update the COMMANDS registry:")
        for finding in findings:
            print(f"    - {finding['command']}: [{finding['kind']}] {finding['detail']}")
    print()
    print("vphone-vm (VM binary):   ", end="")
    try:
        print(cli.vm_bin())
    except RuntimeError as exc:
        print(f"NOT FOUND — {exc}")
    print("vphone-escalator:         ", end="")
    try:
        print(cli.escalator_bin())
    except RuntimeError as exc:
        print(f"NOT FOUND — {exc}")
    try:
        res = cli.run("vm_list", json=True)
        if res["exit_code"] == 0:
            vms = json.loads(res["stdout"] or "[]")
            names = ", ".join(v.get("name", "?") for v in vms) or "(none)"
            print(f"VMs in library:           {names}")
        else:
            print(f"vm list:                  exit {res['exit_code']} — {res['stderr'].strip()}")
    except Exception as exc:  # doctor must never traceback
        print(f"vm list:                  failed — {exc}")
    print()
    print("run `vphone-mcp escalator status` to see the AMFI allowlist (needs root).")
    return 0


def _escalator(argv: list[str]) -> int:
    if not argv or argv[0] not in ("status", "allow"):
        print("usage: vphone-mcp escalator <status|allow> [binary...]", file=sys.stderr)
        return 2
    sub, rest = argv[0], argv[1:]
    if sub == "status":
        res = cli.escalator_status()
    else:
        res = cli.escalator_allow(rest or None)
    print(f"$ {' '.join(res['argv'])}")
    if res["stdout"]:
        print(res["stdout"].rstrip())
    if res["stderr"]:
        print(res["stderr"].rstrip(), file=sys.stderr)
    return res["exit_code"]


def _serve(argv: list[str]) -> int:
    from .server import main as server_main

    server_main(argv)  # blocks while serving
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return _serve([])
    cmd, rest = argv[0], argv[1:]
    if cmd == "serve":
        return _serve(rest)
    if cmd == "versions":
        return _print_versions()
    if cmd == "select":
        return _select(rest)
    if cmd == "doctor":
        return _doctor()
    if cmd == "escalator":
        return _escalator(rest)
    if cmd in ("-h", "--help", "help"):
        _usage()
        return 0
    if cmd.startswith("-"):
        # Server transport flags (--transport, --host, ...) route to serve.
        return _serve(argv)
    print(f"unknown command {cmd!r}", file=sys.stderr)
    _usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
