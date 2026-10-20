"""Data-driven wrapper around the installed vphone-cli binary.

Every vphone-cli subcommand is described by a dict entry in :data:`COMMANDS`
(no if/elif chains over commands). ``run`` executes one entry synchronously,
``run_background`` detaches blocking commands (vm launch / boot) into a log
file, and ``format_result`` turns a result dict into a human-readable summary.

The CLI surface here was verified against vphone-cli 2.6.0 (`vphone-cli help`,
live runs, and the upstream Swift source): subcommand names, flags, and the
``vm list --json`` smoke test all match. Patch selection moved from the
per-command ``--variant``/``--force-exc-guard`` flags to ``--preset`` (see
``fw patches``; ``patch-component`` still takes its own ``--frida``), and
privileged commands (``cfw install``, ``vm create``) are now run under
``sudo`` themselves — the CLI's ``--sudo-password`` / ``--root-popup``
self-elevation flags are gone.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import files


# ---------------------------------------------------------------------------
# Binary resolution (fail loudly)
# ---------------------------------------------------------------------------

BUNDLES_ROOT = Path("/Library/Application Support/vphone-launchpad/Bundles")

#: The vphone-cli version this registry was verified against. ``vphone-mcp
#: doctor`` diffs it against the resolved binary and flags drift.
REGISTRY_VERSION = "2.6.0"

_VERSION_RE = re.compile(r"(?:^|/)Bundles/(\d+\.\d+\.\d+)/")


def _config_path() -> Path:
    return Path(os.environ.get("VPHONE_MCP_CONFIG") or files.vphone_root() / "config.json")


def _read_config() -> dict:
    try:
        return json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_config(cfg: dict) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def bundle_bin(version: str, name: str = "vphone-cli") -> Path:
    """Path to a companion binary inside a launchpad bundle of ``version``."""
    return BUNDLES_ROOT / version / "VPhone.bundle/Contents/MacOS" / name


def installed_versions() -> list[str]:
    """Installed vphone-launchpad bundle versions, newest first."""
    try:
        versions = [
            p.name for p in BUNDLES_ROOT.iterdir() if (p / "VPhone.bundle").is_dir()
        ]
    except OSError:
        return []

    def key(v: str):
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0,)

    return sorted(versions, key=key, reverse=True)


def detect_version(bin_path: str | Path) -> str | None:
    """The bundle version a vphone-cli binary belongs to, or None."""
    m = _VERSION_RE.search(str(bin_path))
    return m.group(1) if m else None


def selected_version() -> str | None:
    """Explicitly selected version: $VPHONE_CLI_VERSION env, else config file."""
    if env := os.environ.get("VPHONE_CLI_VERSION", "").strip():
        return env
    return _read_config().get("vphone_cli_version")


def select_version(version: str) -> None:
    """Persist the active version selection (``vphone-mcp select``)."""
    cfg = _read_config()
    cfg["vphone_cli_version"] = version
    _write_config(cfg)


def resolve_cli_bin() -> str:
    """Resolve the vphone-cli binary path, or raise RuntimeError.

    Priority:
    1. ``$VPHONE_CLI_BIN`` — an explicit binary path.
    2. ``$VPHONE_CLI_VERSION`` env, then the persisted selection — a bundle
       version under the vphone-launchpad Bundles dir.
    3. ``vphone-cli`` on PATH.
    4. The newest installed launchpad bundle (auto-detected).
    5. /Applications/vphone-cli.app/Contents/MacOS/vphone-cli (legacy).

    Raises loudly instead of silently falling back to a broken command.
    """
    env_bin = os.environ.get("VPHONE_CLI_BIN")
    if env_bin:
        if os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
            return env_bin
        raise RuntimeError(f"VPHONE_CLI_BIN is set but not an executable file: {env_bin}")
    version = selected_version()
    if version:
        if not re.fullmatch(r"\d+(?:\.\d+)+", version):
            raise RuntimeError(f"invalid vphone-cli version {version!r}")
        candidate = bundle_bin(version)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise RuntimeError(
            f"selected vphone-cli version {version!r} is not installed "
            f"(no {candidate}) — run `vphone-mcp versions` to see what is available"
        )
    found = shutil.which("vphone-cli")
    if found:
        return found
    for version in installed_versions():
        candidate = bundle_bin(version)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    app_bin = Path("/Applications/vphone-cli.app/Contents/MacOS/vphone-cli")
    if app_bin.is_file() and os.access(app_bin, os.X_OK):
        return str(app_bin)
    raise RuntimeError(
        "vphone-cli binary not found: set VPHONE_CLI_BIN, add vphone-cli to "
        "PATH, or install a launchpad bundle under "
        f"{BUNDLES_ROOT}/<version>/VPhone.bundle"
    )


# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------
# Option spec kinds (one key selects the kind, the rest are metadata):
#   {"flag": "--json", "type": bool, "default": False, "help": ...}
#       bare boolean flag; appended when the value is truthy. An extra
#       "invert": True flips that (flag appended when the value is FALSY).
#   {"option": "--cpu", "type": int, "help": ...}
#       "--flag <value>"; default None means omitted. "required": True
#       makes a missing value raise ValueError.
#   {"option_multi": "-v", "type": int, "default": 0, "help": ...}
#       repeatable flag; the value repeats it N times.
#   {"positional": "name", "type": str, "help": ...}
#       positional argument, appended in registry order.
#       "required": True raises ValueError when missing.
# Short-flag spellings from the CLI (e.g. -c/--config, -d/--dfu, -p/--preset)
# are all emitted in their canonical long form, as used elsewhere in the repo.

COMMANDS: dict[str, dict] = {
    "vm_list": {
        "argv": ["vm", "list"],
        "help": (
            "List all VM bundles in the library. Use this to discover what "
            "VMs exist before targeting one; json=True gives machine-readable "
            "output (prints [] when the library is empty)."
        ),
        "options": {
            "json": {"flag": "--json", "type": bool, "default": False,
                     "help": "Emit machine-readable JSON"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
        },
        "timeout_s": 30,
    },
    "vm_info": {
        "argv": ["vm", "info"],
        "help": (
            "Show configuration details for a VM bundle (name, CPU, memory, "
            "disk, network mode). json=True returns the machine-readable "
            "manifest data."
        ),
        "options": {
            "json": {"flag": "--json", "type": bool, "default": False,
                     "help": "Emit machine-readable JSON"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
            "name": {"positional": "name", "type": str,
                     "help": "VM name (defaults to the only VM in the library)"},
        },
        "timeout_s": 30,
    },
    "vm_new": {
        "argv": ["vm", "new"],
        "help": (
            "Create a new empty VM bundle with the given name and optional "
            "CPU/memory/disk-size/rom/seprom settings. The bundle must still "
            "be built with vm_create before it can boot."
        ),
        "options": {
            "name": {"positional": "name", "type": str,
                     "help": "Name for the new VM bundle"},
            "cpu": {"option": "--cpu", "type": int,
                    "help": "Number of vCPUs (e.g. 4)"},
            "memory": {"option": "--memory", "type": int,
                       "help": "Memory size in MB (e.g. 4096)"},
            "disk_size": {"option": "--disk-size", "type": int,
                          "help": "Disk size in GB (e.g. 64)"},
            "rom": {"option": "--rom", "type": str,
                    "help": "Path to a custom ROM image"},
            "seprom": {"option": "--seprom", "type": str,
                       "help": "Path to a custom secure element PROM image"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
        },
        "timeout_s": 30,
    },
    "vm_config": {
        "argv": ["vm", "config"],
        "help": (
            "Adjust an existing VM's configuration: CPU count, memory, "
            "network mode (nat/bridged/none), and bridge interface. Changes "
            "apply the next time the VM boots."
        ),
        "options": {
            "name": {"positional": "name", "type": str,
                     "help": "VM name (defaults to the only VM in the library)"},
            "cpu": {"option": "--cpu", "type": int,
                    "help": "Number of vCPUs (e.g. 4)"},
            "memory": {"option": "--memory", "type": int,
                       "help": "Memory size in MB (e.g. 4096)"},
            "network": {"option": "--network", "type": str,
                        "help": "Network mode: nat, bridged, or none"},
            "bridge_interface": {"option": "--bridge-interface", "type": str,
                                 "help": "Network interface to bridge (e.g. en0)"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
        },
        "timeout_s": 30,
    },
    "vm_rename": {
        "argv": ["vm", "rename"],
        "help": "Rename a VM bundle in the library (the VM must be stopped).",
        "options": {
            "name": {"positional": "name", "type": str,
                     "help": "Current VM name"},
            "new_name": {"positional": "new_name", "type": str,
                         "help": "New VM name"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
        },
        "timeout_s": 30,
    },
    "vm_delete": {
        "argv": ["vm", "delete"],
        "help": (
            "Delete a VM bundle from the library. Destructive: requires "
            "confirm=True unless force=True (force mirrors the CLI's -f flag, "
            "which skips its y/N prompt)."
        ),
        "options": {
            "name": {"positional": "name", "type": str,
                     "help": "VM name to delete"},
            "force": {"flag": "--force", "type": bool, "default": False,
                      "help": "Skip the confirmation prompt"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
        },
        "timeout_s": 30,
        "confirm": True,
    },
    "vm_clone": {
        "argv": ["vm", "clone"],
        "help": (
            "Clone an existing VM bundle to a new name. The clone gets a "
            "fresh device identity — useful for per-testcase VMs that must "
            "not share state or serial numbers."
        ),
        "options": {
            "name": {"positional": "name", "type": str,
                     "help": "Source VM name"},
            "new_name": {"positional": "new_name", "type": str,
                         "help": "Name for the cloned VM"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
        },
        "timeout_s": 30,
    },
    "vm_export": {
        "argv": ["vm", "export"],
        "help": (
            "Export a VM bundle to a .tzst archive (zstd-fast) at the --out "
            "path, or .txz with max_compression=True. Useful for archiving a "
            "known-good test baseline before mutating it; skips the restore "
            "dir and staging files."
        ),
        "options": {
            "name": {"positional": "name", "type": str,
                     "help": "VM name to export"},
            "out": {"option": "--out", "type": str, "required": True,
                    "help": "Output .vphone archive path"},
            "max_compression": {"flag": "--max", "type": bool, "default": False,
                     "help": "Densest compression (xz -9) instead of default fast (zstd)"},
            "include_ipsw": {"flag": "--include-ipsw", "type": bool, "default": False,
                             "help": "Include the IPSW in the archive"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
        },
        "timeout_s": 3600,
        "allow_background": True,
    },
    "vm_import": {
        "argv": ["vm", "import"],
        "help": (
            "Import a previously exported archive (.tzst/.txz) as a new VM "
            "bundle, optionally under a different name."
        ),
        "options": {
            "input": {"positional": "input", "type": str, "required": True,
                      "help": "Path to the .vphone archive"},
            "name": {"option": "--name", "type": str,
                     "help": "Name for the imported VM (defaults to the archive's)"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
        },
        "timeout_s": 3600,
        "allow_background": True,
    },
    "vm_launch": {
        "argv": ["vm", "launch"],
        "help": (
            "Launch a VM bundle. This is a blocking VM process (streams the "
            "guest serial console), so it runs in the background with a log "
            "file; check vphone_status or the log to verify boot."
        ),
        "options": {
            "name": {"positional": "name", "type": str,
                     "help": "VM name to launch (defaults to the only VM in the library)"},
            "dfu": {"flag": "--dfu", "type": bool, "default": False,
                    "help": "Boot into DFU mode"},
            "headless": {"flag": "--headless", "type": bool, "default": False,
                         "help": "Boot without a VM window or menu bar"},
            "api_listen": {"option": "--api-listen", "type": str,
                           "help": "Expose the guest HTTP/WebSocket API on host:port (e.g. 127.0.0.1:8765)"},
            "kernel_debug_port": {"option": "--kernel-debug-port", "type": int,
                                  "help": "Kernel GDB debug stub port (6000...65535)"},
            "project_root": {"option": "--project-root", "type": str,
                             "help": "Resource base override (default: inferred from the binary path)"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
            "verbose": {"option_multi": "-v", "type": int, "default": 0,
                        "help": "Verbosity level; repeat -v up to N times"},
        },
        "timeout_s": 0,  # n/a — background entry
        "background": True,
    },
    "vm_stop": {
        "argv": ["vm", "stop"],
        "help": (
            "Stop a running VM by locating its processes (SIGINT then SIGKILL "
            "after --timeout). Requires confirm=True."
        ),
        "options": {
            "name": {"positional": "name", "type": str,
                     "help": "VM name to stop (defaults to the only VM in the library)"},
            "timeout": {"option": "--timeout", "type": int,
                        "help": "Grace period in seconds before SIGKILL"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
        },
        "timeout_s": 30,
        "confirm": True,
    },
    "vm_create": {
        "argv": ["vm", "create"],
        "help": (
            "Run the end-to-end VM creation pipeline for a new bundle: "
            "prepare, patch, restore, CFW install, first boot. Needs internet "
            "for firmware downloads and runs under sudo for the CFW stage "
            "(set VPHONE_SUDO_PASSWORD for non-interactive elevation); "
            "long-running (may take an hour). Requires confirm=True."
        ),
        "options": {
            "name": {"positional": "name", "type": str, "required": True,
                     "help": "Name of the new VM bundle"},
            "iphone_source": {"option": "--iphone-source", "type": str,
                              "help": "iPhone IPSW URL or local path"},
            "cloudos_source": {"option": "--cloudos-source", "type": str,
                               "help": "cloudOS IPSW URL or local path"},
            "gpu_driver_bundle": {"option": "--gpu-driver-bundle", "type": str,
                                  "help": "GPU driver bundle from the same cloudOS build (offline AEA recovery)"},
            "ipsw_cache": {"option": "--ipsw-cache", "type": str,
                           "help": "Directory for downloaded IPSWs (default ~/.vphone/ipsws)"},
            "device": {"option": "--device", "type": str,
                       "help": "Guest device when the IPSW covers several models (e.g. iPad15,5)"},
            "disk_size": {"option": "--disk-size", "type": int,
                          "help": "Disk size in GB (default 64)"},
            "preset": {"option": "--preset", "type": str,
                       "help": "Patch preset (default standard; see `fw patches`)"},
            "keep_artifacts": {"flag": "--keep-artifacts", "type": bool, "default": False,
                               "help": "Keep the prepared restore tree after installation"},
            "verbose": {"option_multi": "-v", "type": int, "default": 0,
                        "help": "Verbosity level; repeat -v up to N times"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
        },
        "timeout_s": 7200,
        "confirm": True,
        "allow_background": True,
        "sudo": True,
    },
    "fw_catalog": {
        "argv": ["fw", "catalog"],
        "help": (
            "Show the known iOS ↔ cloudOS firmware pairings (recommended "
            "cloudOS per iOS build). json=True gives machine-readable output."
        ),
        "options": {
            "json": {"flag": "--json", "type": bool, "default": False,
                     "help": "Emit machine-readable JSON"},
        },
        "timeout_s": 30,
    },
    "fw_prepare": {
        "argv": ["fw", "prepare"],
        "help": (
            "Download + merge iPhone and cloudOS IPSWs into the named VM "
            "bundle (runs inside the bundle dir). Long download — give it "
            "a generous timeout."
        ),
        "options": {
            "name": {"positional": "name", "type": str,
                     "help": "Name for the prepared firmware set"},
            "iphone_source": {"option": "--iphone-source", "type": str,
                              "help": "Local path or URL for the iPhone IPSW"},
            "cloudos_source": {"option": "--cloudos-source", "type": str,
                               "help": "Local path or URL for the cloudOS restore bundle"},
            "iphone_version": {"option": "--iphone-version", "type": str,
                               "help": "iPhone firmware version to fetch (e.g. 26.0)"},
            "iphone_build": {"option": "--iphone-build", "type": str,
                             "help": "iPhone firmware build number to fetch"},
            "list_only": {"flag": "--list", "type": bool, "default": False,
                      "help": "List downloadable IPSWs and exit (does not prepare)"},
            "project_root": {"option": "--project-root", "type": str,
                             "help": "vphone project root (default ~/.vphone)"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
            "verbose": {"option_multi": "-v", "type": int, "default": 0,
                        "help": "Verbosity level; repeat -v up to N times"},
        },
        "timeout_s": 7200,
        "allow_background": True,
    },
    "fw_patch": {
        "argv": ["fw", "patch"],
        "help": (
            "Patch the boot chain for a VM with a patch preset (see "
            "`fw patches` for what each preset applies). Long-running."
        ),
        "options": {
            "name": {"positional": "name", "type": str,
                     "help": "Name of the firmware set to patch"},
            "preset": {"option": "--preset", "type": str,
                       "help": "Patch preset (default: the VM's recorded choice, else standard)"},
            "quiet": {"flag": "--quiet", "type": bool, "default": False,
                      "help": "Suppress per-component progress"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
        },
        "timeout_s": 3600,
        "allow_background": True,
    },
    "restore": {
        "argv": ["restore"],
        "help": (
            "DFU-restore firmware into a VM bundle — requires a running DFU "
            "boot of that VM. get_shsh=True fetches only the SHSH blob "
            "(no restore). Long-running and privileged. Requires confirm=True."
        ),
        "options": {
            "name": {"positional": "name", "type": str,
                     "help": "VM name to restore (defaults to the only VM in the library)"},
            "get_shsh": {"flag": "--get-shsh", "type": bool, "default": False,
                         "help": "Fetch SHSH blobs before restoring"},
            "offline": {"flag": "--offline", "type": bool, "default": False,
                        "help": "Restore offline (no network)"},
            "udid": {"option": "--udid", "type": str,
                     "help": "Override the device UDID"},
            "ecid": {"option": "--ecid", "type": str,
                     "help": "Device ECID (default: from the bundle's udid-prediction.txt)"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
            "verbose": {"option_multi": "-v", "type": int, "default": 0,
                        "help": "Verbosity level; repeat -v up to N times"},
        },
        "timeout_s": 7200,
        "confirm": True,
        "allow_background": True,
    },
    "cfw_install": {
        "argv": ["cfw", "install"],
        "help": (
            "Install the custom firmware (CFW) into a VM bundle via host "
            "mount — the VM must be off. The boot chain follows `fw patch`'s "
            "plan and the guest half follows the VM's current patch selection. "
            "Runs under sudo (set VPHONE_SUDO_PASSWORD for non-interactive "
            "elevation). Long-running. Requires confirm=True."
        ),
        "options": {
            "name": {"positional": "name", "type": str,
                     "help": "VM name to install CFW into (defaults to the only VM in the library)"},
            "keep_artifacts": {"flag": "--keep-artifacts", "type": bool, "default": False,
                               "help": "Keep the extracted firmware after install (default: removed to save space)"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/machines or $VPHONE_LIBRARY_ROOT)"},
        },
        "timeout_s": 7200,
        "confirm": True,
        "allow_background": True,
        "sudo": True,
    },
    "patch_firmware": {
        "argv": ["patch-firmware"],
        "help": (
            "Patch boot-chain firmware in a VM directory with the Swift "
            "pipeline (records-out, quiet, preset options). Long-running."
        ),
        "options": {
            "vm_directory": {"option": "--vm-directory", "type": str, "required": True,
                             "help": "Path to the VM directory containing the *Restore* folder"},
            "records_out": {"option": "--records-out", "type": str,
                            "help": "Write emitted PatchRecord JSON to this path"},
            "quiet": {"flag": "--quiet", "type": bool, "default": False,
                      "help": "Suppress per-component progress"},
            "preset": {"option": "--preset", "type": str,
                       "help": "Patch preset (default standard; individual patches are selected per VM by `fw patch`)"},
        },
        "timeout_s": 3600,
        "allow_background": True,
    },
    "patch_component": {
        "argv": ["patch-component"],
        "help": (
            "Patch a single firmware component file (txm, kernel-base, or "
            "kernel-jb): reads --input, applies the component patch, writes "
            "--output."
        ),
        "options": {
            "component": {"option": "--component", "type": str, "required": True,
                          "help": "Component to patch: txm|kernel-base|kernel-jb"},
            "input": {"option": "--input", "type": str, "required": True,
                      "help": "Input component file"},
            "output": {"option": "--output", "type": str, "required": True,
                       "help": "Output patched component file"},
            "quiet": {"flag": "--quiet", "type": bool, "default": False,
                      "help": "Suppress progress output"},
            "records_out": {"option": "--records-out", "type": str,
                            "help": "Write patch records to this JSON file"},
            "target_os": {"option": "--target-os", "type": str,
                          "help": "Target OS version for the patch"},
            "frida": {"flag": "--frida", "type": bool, "default": False,
                      "help": "kernel-jb only: opt in to the Frida Stalker kernel relaxations"},
        },
        "timeout_s": 1800,
        "allow_background": True,
    },
    "boot": {
        "argv": ["boot"],
        "help": (
            "Boot a virtual iPhone directly from a raw config.plist manifest "
            "path (no library bundle; PV=3). Blocking VM process — runs in "
            "background with a log file."
        ),
        "options": {
            "config": {"option": "--config", "type": str, "required": True,
                       "help": "Path to VM manifest plist (config.plist)"},
            "dfu": {"flag": "--dfu", "type": bool, "default": False,
                    "help": "Boot into DFU mode"},
            "headless": {"flag": "--headless", "type": bool, "default": False,
                         "help": "Boot without a VM window or menu bar"},
            "api_listen": {"option": "--api-listen", "type": str,
                           "help": "Expose the guest HTTP/WebSocket API on host:port (e.g. 127.0.0.1:8765)"},
            "kernel_debug_port": {"option": "--kernel-debug-port", "type": int,
                                  "help": "Kernel GDB debug stub port (6000...65535)"},
            "vphoned_bin": {"option": "--vphoned-bin", "type": str,
                            "help": "Path to signed vphoned binary (default .vphoned.signed)"},
            "install_ipa": {"option": "--install-ipa", "type": str,
                            "help": "Install IPA/TIPA after the guest control channel connects (unavailable with dfu)"},
        },
        "timeout_s": 0,  # n/a — background entry
        "background": True,
    },
}

_DEFAULT_TIMEOUT_S = 30


# ---------------------------------------------------------------------------
# Argument building / validation
# ---------------------------------------------------------------------------

def _entry(cmd_key: str) -> dict:
    try:
        return COMMANDS[cmd_key]
    except KeyError:
        raise ValueError(
            f"unknown vphone-cli command {cmd_key!r} "
            f"(known: {', '.join(sorted(COMMANDS))})"
        ) from None


def _spec_kind(spec: dict) -> str:
    for kind in ("flag", "option", "option_multi", "positional"):
        if kind in spec:
            return kind
    raise ValueError(f"option spec has no recognized kind: {spec!r}")


def _coerce(spec: dict, name: str, value) -> str:
    """Validate ``value`` against the spec's declared type and stringify it."""
    t = spec.get("type")
    if t is int:
        if isinstance(value, bool):
            raise ValueError(f"param {name!r} expects an integer, got bool")
        try:
            return str(int(value))
        except (TypeError, ValueError):
            raise ValueError(
                f"param {name!r} expects an integer, got {value!r}"
            ) from None
    if t is bool:
        return str(bool(value))
    return str(value)


def _build_argv(entry: dict, params: dict) -> list[str]:
    """Build the argv (binary + subcommand + flags + positionals) for an entry.

    Unknown params and type-invalid values raise ValueError; missing required
    options/positionals raise ValueError too (fail loudly, never silently
    mis-invoke the CLI). Raises RuntimeError if the binary cannot be resolved.
    """
    argv: list[str] = [resolve_cli_bin(), *entry["argv"]]
    unknown = set(params) - set(entry["options"])
    if unknown:
        raise ValueError(
            f"unknown param(s) {sorted(unknown)!r} for {entry['argv']!r}"
        )
    positionals: list[str] = []
    for pname, spec in entry["options"].items():
        kind = _spec_kind(spec)
        if pname not in params:
            value = spec.get("default")
        else:
            value = params[pname]
        if kind == "positional":
            if value is None:
                if spec.get("required"):
                    raise ValueError(
                        f"missing required positional {pname!r} for "
                        f"{entry['argv']!r}"
                    )
                continue
            positionals.append(_coerce(spec, pname, value))
        elif kind == "flag":
            if value is None:
                value = spec.get("default", False)
            if spec.get("invert"):
                value = not bool(value)
            if bool(value):
                argv.append(spec["flag"])
        elif kind == "option":
            if value is None:
                if spec.get("required"):
                    raise ValueError(
                        f"missing required option {pname!r} for {entry['argv']!r}"
                    )
                continue
            argv.append(spec["option"])
            argv.append(_coerce(spec, pname, value))
        elif kind == "option_multi":
            if value is None:
                value = spec.get("default", 0)
            n = int(_coerce(spec, pname, value))
            if n < 0:
                raise ValueError(f"param {pname!r} must be >= 0")
            if n > 10:
                raise ValueError(f"param {pname!r} must be <= 10 (got {n})")
            # The repeatable flag string is the value of the option_multi key.
            argv.extend([spec["option_multi"]] * n)
    # Positionals go last; Swift ArgumentParser consumes flags and positionals
    # in any order, and positionals keep their registry order (e.g. old new).
    argv.extend(positionals)
    return argv


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _sudo_prefix() -> tuple[list[str], dict, str | None]:
    """argv prefix + env + askpass-path for commands declared ``sudo: True``.

    vphone-cli 2.6.0 dropped --sudo-password/--root-popup; privileged commands
    (``cfw install``, ``vm create``) must be run under sudo themselves. With
    VPHONE_SUDO_PASSWORD set, ``sudo -A`` drives a 0700 askpass helper in /tmp
    that deletes itself once sudo reads it (its path is also returned so the
    caller can remove it as a fallback); otherwise ``sudo -n`` fails loudly
    rather than hanging on a TTY prompt.
    """
    pw = os.environ.get("VPHONE_SUDO_PASSWORD")
    if not pw:
        return ["sudo", "-n"], {}, None
    fd, path = tempfile.mkstemp(prefix="vphone-sudo-askpass.")
    safe = pw.replace("'", "'\\''")
    os.write(fd, f"#!/bin/sh\necho '{safe}'\nrm -f \"$0\"\n".encode())
    os.close(fd)
    os.chmod(path, 0o700)
    return ["sudo", "-A"], {"SUDO_ASKPASS": path}, path


def _unlink_askpass(path: str | None) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def run(cmd_key: str, **params) -> dict:
    """Run a registry command synchronously and return its result dict.

    Returns ``{"exit_code", "stdout", "stderr", "argv"}`` — a nonzero exit
    code still returns normally (the MCP tool formats it so the LLM sees the
    error). Raises RuntimeError on subprocess timeout (including partial
    output), ValueError on bad arguments, and RuntimeError if the binary is
    missing. Entries declared ``sudo: True`` run under sudo.
    """
    entry = _entry(cmd_key)
    argv = _build_argv(entry, params)
    timeout = entry.get("timeout_s", _DEFAULT_TIMEOUT_S)
    timeout = None if timeout <= 0 else timeout
    env = os.environ.copy()
    askpass_path = None
    if entry.get("sudo"):
        prefix, senv, askpass_path = _sudo_prefix()
        argv = prefix + argv
        env.update(senv)
    try:
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, env=env
            )
        except subprocess.TimeoutExpired as exc:
            partial = ""
            if exc.stdout:
                partial += f"\npartial stdout:\n{exc.stdout}"
            if exc.stderr:
                partial += f"\npartial stderr:\n{exc.stderr}"
            raise RuntimeError(
                f"vphone-cli {cmd_key} timed out after {timeout}s{partial}"
            ) from exc
    finally:
        _unlink_askpass(askpass_path)
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "argv": argv,
    }


def run_background(cmd_key: str, **params) -> dict:
    """Detach a background command (vm launch / boot) and log it to disk.

    Only entries declared with ``background: True`` are accepted. The child is
    started in its own session (survives the MCP server) with stdout+stderr
    streamed to ``$VPHONE_ROOT/vphone-mcp/logs/<name-or-cmd>_launch_<epoch>.log``
    (a file-endpoint root, so the log is fetchable remotely). Entries declared
    ``sudo: True`` run under sudo; the askpass helper deletes itself once sudo
    reads it.
    Returns ``{"pid", "log_path", "argv"}``.
    """
    entry = _entry(cmd_key)
    if not (entry.get("background") or entry.get("allow_background")):
        raise ValueError(f"command {cmd_key!r} does not support background execution")
    argv = _build_argv(entry, params)
    name = params.get("name")
    stem = str(name) if name else cmd_key
    # A VM name is a single path component; keep it that way so a client-
    # supplied name cannot escape the log directory via ../ or a separator.
    stem = stem.replace("/", "_").replace("\\", "_").replace("..", "_")
    if not stem or stem.startswith("."):
        stem = cmd_key
    log_dir = files.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{stem}_launch_{int(time.time())}.log"
    env = os.environ.copy()
    if entry.get("sudo"):
        prefix, senv, _ = _sudo_prefix()
        argv = prefix + argv
        env.update(senv)
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(
            argv,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    return {"pid": proc.pid, "log_path": str(log_path), "argv": argv}


# ---------------------------------------------------------------------------
# vphone-escalator (AMFI code-requirement allowlist)
# ---------------------------------------------------------------------------

def escalator_bin() -> str:
    """The vphone-escalator binary next to the resolved vphone-cli."""
    candidate = Path(resolve_cli_bin()).with_name("vphone-escalator")
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    raise RuntimeError(f"vphone-escalator not found next to {resolve_cli_bin()}")


def vm_bin() -> str:
    """The vphone-vm companion binary next to the resolved vphone-cli."""
    candidate = Path(resolve_cli_bin()).with_name("vphone-vm")
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    raise RuntimeError(f"vphone-vm not found next to {resolve_cli_bin()}")


def escalator_status() -> dict:
    """Run ``vphone-escalator status`` (needs root) and return the result."""
    esc = escalator_bin()  # resolve first — it can raise before any askpass is created
    prefix, senv, askpass = _sudo_prefix()
    argv = prefix + [esc, "status"]
    env = os.environ.copy()
    env.update(senv)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)
    finally:
        _unlink_askpass(askpass)
    return {"exit_code": proc.returncode, "stdout": proc.stdout or "", "stderr": proc.stderr or "", "argv": argv}


def escalator_allow(binaries: list[str] | None = None) -> dict:
    """Run ``sudo vphone-escalator allow <vphone-vm>`` for the resolved VM binary.

    This writes the vphone-vm cdhash into the AMFI code-requirement allowlist
    so the virtualization-entitled companion binary can launch. Idempotent —
    running it again refreshes the same entries.
    """
    esc = escalator_bin()  # resolve first — it can raise before any askpass is created
    targets = binaries or [vm_bin()]
    prefix, senv, askpass = _sudo_prefix()
    argv = prefix + [esc, "allow", *targets]
    env = os.environ.copy()
    env.update(senv)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
    finally:
        _unlink_askpass(askpass)
    return {"exit_code": proc.returncode, "stdout": proc.stdout or "", "stderr": proc.stderr or "", "argv": argv}


# ---------------------------------------------------------------------------
# Registry validation (vphone-cli version drift)
# ---------------------------------------------------------------------------

def validate_registry(bin_path: str | None = None) -> list[dict]:
    """Flag registry entries whose subcommand or flags differ from the binary.

    Runs ``<bin> <argv> --help`` for every COMMANDS entry and reports (a) a
    subcommand that no longer exists, and (b) any flag/option string absent
    from the binary's help. Empty list means the registry matches. This is the
    backstop for the next vphone-cli update: ``vphone-mcp doctor`` surfaces
    drift before a tool call fails at runtime.
    """
    bin_path = bin_path or resolve_cli_bin()
    findings: list[dict] = []
    for key, entry in COMMANDS.items():
        argv = [bin_path, *entry["argv"], "--help"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            findings.append({"command": key, "kind": "subcommand", "detail": str(exc)})
            continue
        help_text = (proc.stdout or "") + (proc.stderr or "")
        lowered = help_text.lower()
        if (
            "unknown subcommand" in lowered
            or "unexpected argument" in lowered
            or "missing expected argument" in lowered
        ):
            first = help_text.strip().splitlines()[0] if help_text.strip() else "?"
            findings.append({"command": key, "kind": "subcommand", "detail": first})
            continue
        for pname, spec in entry["options"].items():
            for kind in ("flag", "option", "option_multi"):
                if kind in spec and spec[kind] not in help_text:
                    findings.append({
                        "command": key,
                        "kind": "flag",
                        "detail": f"{spec[kind]} ({pname})",
                    })
    return findings


def format_result(res: dict) -> str:
    """Human-readable summary of a ``run()`` result dict."""
    lines = [
        f"exit code: {res['exit_code']}",
        f"command: {' '.join(res['argv'])}",
    ]
    stdout = (res.get("stdout") or "").rstrip()
    stderr = (res.get("stderr") or "").rstrip()
    if stdout:
        lines.append(f"stdout:\n{stdout}")
    if stderr:
        lines.append(f"stderr:\n{stderr}")
    return "\n".join(lines)
