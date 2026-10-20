"""Data-driven wrapper around the installed vphone-cli binary.

Every vphone-cli subcommand is described by a dict entry in :data:`COMMANDS`
(no if/elif chains over commands). ``run`` executes one entry synchronously,
``run_background`` detaches blocking commands (vm launch / boot) into a log
file, and ``format_result`` turns a result dict into a human-readable summary.

The CLI surface here was verified against vphone-cli 1.0.12 (`vphone-cli help`,
live runs, and the upstream Swift source): subcommand names, flags, and the
``vm list --json`` smoke test all match.
"""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import files


# ---------------------------------------------------------------------------
# Binary resolution (fail loudly)
# ---------------------------------------------------------------------------

def resolve_cli_bin() -> str:
    """Resolve the vphone-cli binary path, or raise RuntimeError.

    Priority: $VPHONE_CLI_BIN env var > ``vphone-cli`` on PATH >
    /Applications/vphone-cli.app/Contents/MacOS/vphone-cli (the homebrew
    symlink target). Raises loudly instead of silently falling back to a
    broken command.
    """
    env_bin = os.environ.get("VPHONE_CLI_BIN")
    if env_bin:
        if os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
            return env_bin
        raise RuntimeError(f"VPHONE_CLI_BIN is set but not an executable file: {env_bin}")
    found = shutil.which("vphone-cli")
    if found:
        return found
    app_bin = Path("/Applications/vphone-cli.app/Contents/MacOS/vphone-cli")
    if app_bin.is_file() and os.access(app_bin, os.X_OK):
        return str(app_bin)
    raise RuntimeError(
        "vphone-cli binary not found: set VPHONE_CLI_BIN, add vphone-cli to "
        "PATH, or install the app bundle at /Applications/vphone-cli.app"
    )


# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------
# Option spec kinds (one key selects the kind, the rest are metadata):
#   {"flag": "--json", "type": bool, "default": False, "help": ...}
#       bare boolean flag; appended when the value is truthy. An extra
#       "invert": True flips that (flag appended when the value is FALSY —
#       used by cfw_install's use_sudo).
#   {"option": "--cpu", "type": int, "help": ...}
#       "--flag <value>"; default None means omitted. "required": True
#       makes a missing value raise ValueError.
#   {"option_multi": "-v", "type": int, "default": 0, "help": ...}
#       repeatable flag; the value repeats it N times.
#   {"positional": "name", "type": str, "help": ...}
#       positional argument, appended in registry order.
#       "required": True raises ValueError when missing.
# Short-flag spellings from the CLI (e.g. -c/--config, -d/--dfu, -V/--variant)
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
                             "help": "VM library root (default ~/.vphone/VMs)"},
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
                             "help": "VM library root (default ~/.vphone/VMs)"},
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
                             "help": "VM library root (default ~/.vphone/VMs)"},
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
                             "help": "VM library root (default ~/.vphone/VMs)"},
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
                             "help": "VM library root (default ~/.vphone/VMs)"},
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
                             "help": "VM library root (default ~/.vphone/VMs)"},
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
                             "help": "VM library root (default ~/.vphone/VMs)"},
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
                             "help": "VM library root (default ~/.vphone/VMs)"},
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
                             "help": "VM library root (default ~/.vphone/VMs)"},
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
            "variant": {"option": "--variant", "type": str,
                        "help": "Firmware variant: less|regular|dev|jb|exp"},
            "no_vphoned": {"flag": "--no-vphoned", "type": bool, "default": False,
                           "help": "Exclude vphoned usage (patchless-only)"},
            "kernel_debug_port": {"option": "--kernel-debug-port", "type": int,
                                  "help": "Kernel GDB debug stub port (6000...65535)"},
            "project_root": {"option": "--project-root", "type": str,
                             "help": "vphone project root (default ~/.vphone)"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/VMs)"},
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
                             "help": "VM library root (default ~/.vphone/VMs)"},
        },
        "timeout_s": 30,
        "confirm": True,
    },
    "vm_create": {
        "argv": ["vm", "create"],
        "help": (
            "Run the end-to-end VM creation pipeline for a new bundle: "
            "prepare, patch, restore, CFW install, first boot. Needs internet "
            "for firmware downloads and sudo for the CFW stage; long-running "
            "(may take an hour). Requires confirm=True."
        ),
        "options": {
            "name": {"positional": "name", "type": str, "required": True,
                     "help": "Name of the new VM bundle"},
            "variant": {"option": "--variant", "type": str,
                        "help": "Firmware variant: regular|dev|jb|exp|less (default regular)"},
            "iphone_source": {"option": "--iphone-source", "type": str,
                              "help": "iPhone IPSW URL or local path"},
            "cloudos_source": {"option": "--cloudos-source", "type": str,
                               "help": "cloudOS IPSW URL or local path"},
            "disk_size": {"option": "--disk-size", "type": int,
                          "help": "Disk size in GB (default 64)"},
            "sudo_password": {
                "option": "--sudo-password", "type": str,
                "env_fallback": "VPHONE_SUDO_PASSWORD",
                "help": (
                    "sudo password for the CFW host-mount (passed via askpass, "
                    "never logged by the CLI). WARNING: passing this as an MCP "
                    "argument puts it in agent transcripts — prefer setting the "
                    "VPHONE_SUDO_PASSWORD env var instead; the tool falls back "
                    "to that env value when the param is omitted."
                ),
            },
            "spoof_build": {"option": "--spoof-build", "type": str,
                            "help": "(exp only) rewrite ProductBuildVersion to this build id"},
            "force_dsc_maxslide": {"flag": "--force-dsc-maxslide", "type": bool, "default": False,
                                   "help": "Force a fixed dsc maxslide value"},
            "frida": {"flag": "--frida", "type": bool, "default": False,
                      "help": "Install re.frida.server (latest GitHub release) + jb/exp kernel relaxations"},
            "root_popup": {"flag": "--root-popup", "type": bool, "default": False,
                           "help": "Elevate CFW via macOS auth dialog (osascript) instead of sudo prompt"},
            "interactive": {"flag": "--interactive", "type": bool, "default": False,
                            "help": "Prompt at first-boot stages instead of non-interactive"},
            "keep_artifacts": {"flag": "--keep-artifacts", "type": bool, "default": False,
                               "help": "Keep build artifacts after installing"},
            "project_root": {"option": "--project-root", "type": str,
                             "help": "vphone project root (default ~/.vphone)"},
            "verbose": {"option_multi": "-v", "type": int, "default": 0,
                        "help": "Verbosity level; repeat -v up to N times"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/VMs)"},
        },
        "timeout_s": 7200,
        "confirm": True,
        "allow_background": True,
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
                             "help": "VM library root (default ~/.vphone/VMs)"},
            "verbose": {"option_multi": "-v", "type": int, "default": 0,
                        "help": "Verbosity level; repeat -v up to N times"},
        },
        "timeout_s": 7200,
        "allow_background": True,
    },
    "fw_patch": {
        "argv": ["fw", "patch"],
        "help": (
            "Patch a prepared firmware set for a VM (variant-specific "
            "patches, optional force-exc-guard, frida, quiet). Long-running."
        ),
        "options": {
            "name": {"positional": "name", "type": str,
                     "help": "Name of the firmware set to patch"},
            "variant": {"option": "--variant", "type": str,
                        "help": "Firmware variant: less|regular|dev|jb|exp"},
            "force_exc_guard": {"flag": "--force-exc-guard", "type": bool, "default": False,
                                "help": "Force-enable the EXC_GUARD (Mach port guard) disable patch"},
            "frida": {"flag": "--frida", "type": bool, "default": False,
                      "help": "Opt in to Frida Stalker kernel relaxations (jb/exp only)"},
            "quiet": {"flag": "--quiet", "type": bool, "default": False,
                      "help": "Suppress progress output"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/VMs)"},
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
                     "help": "Override the device ECID"},
            "project_root": {"option": "--project-root", "type": str,
                             "help": "vphone project root (default ~/.vphone)"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/VMs)"},
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
            "mount — the VM must be off. Long-running and privileged. "
            "Requires confirm=True. By default passes --root-popup (osascript "
            "auth dialog); set use_sudo=True to use vphone-cli's plain sudo "
            "re-exec instead."
        ),
        "options": {
            "name": {"positional": "name", "type": str,
                     "help": "VM name to install CFW into (defaults to the only VM in the library)"},
            "variant": {"option": "--variant", "type": str, "default": "exp",
                        "help": "Firmware variant: less|regular|dev|jb|exp (default exp)"},
            "spoof_build": {"option": "--spoof-build", "type": str,
                            "help": "Build number to spoof to the guest"},
            "force_dsc_maxslide": {"flag": "--force-dsc-maxslide", "type": bool, "default": False,
                                   "help": "Force a fixed dsc maxslide value"},
            "use_sudo": {"flag": "--root-popup", "type": bool, "default": False,
                         "invert": True,
                         "help": "Use the default sudo re-exec instead of --root-popup (needs a TTY, which the MCP server lacks)"},
            "keep_artifacts": {"flag": "--keep-artifacts", "type": bool, "default": False,
                               "help": "Keep build artifacts after installing"},
            "project_root": {"option": "--project-root", "type": str,
                             "help": "vphone project root (default ~/.vphone)"},
            "library_root": {"option": "--library-root", "type": str,
                             "help": "VM library root (default ~/.vphone/VMs)"},
            "verbose": {"option_multi": "-v", "type": int, "default": 0,
                        "help": "Verbosity level; repeat -v up to N times"},
        },
        "timeout_s": 7200,
        "confirm": True,
        "allow_background": True,
        "sudo_env": True,
    },
    "patch_firmware": {
        "argv": ["patch-firmware"],
        "help": (
            "Patch an existing VM directory's firmware in place with "
            "variant-specific patches (records-out, no-binpack, no-vphoned, "
            "force-exc-guard, frida options). Long-running."
        ),
        "options": {
            "vm_directory": {"option": "--vm-directory", "type": str, "required": True,
                             "help": "Path to the VM directory to patch"},
            "variant": {"option": "--variant", "type": str,
                        "help": "Firmware variant: less|regular|dev|jb|exp"},
            "records_out": {"option": "--records-out", "type": str,
                            "help": "Write patch records to this JSON file"},
            "quiet": {"flag": "--quiet", "type": bool, "default": False,
                      "help": "Suppress progress output"},
            "no_binpack": {"flag": "--no-binpack", "type": bool, "default": False,
                           "help": "Exclude the SSH, VNC, ... binaries (patchless-only)"},
            "no_vphoned": {"flag": "--no-vphoned", "type": bool, "default": False,
                           "help": "Exclude vphoned usage (patchless-only)"},
            "force_exc_guard": {"flag": "--force-exc-guard", "type": bool, "default": False,
                                "help": "Force-enable the EXC_GUARD (Mach port guard) disable patch"},
            "frida": {"flag": "--frida", "type": bool, "default": False,
                      "help": "Opt in to Frida Stalker kernel relaxations (jb/exp only)"},
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
    "setup_env": {
        "argv": ["setup"],
        "help": (
            "Provision the vphone Python environment (~/.vphone/venv) and "
            "prerequisites. force=True re-runs even if already set up."
        ),
        "options": {
            "force": {"flag": "--force", "type": bool, "default": False,
                      "help": "Re-run setup even if already provisioned"},
            "project_root": {"option": "--project-root", "type": str,
                             "help": "vphone project root (default ~/.vphone)"},
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
            "kernel_debug_port": {"option": "--kernel-debug-port", "type": int,
                                  "help": "Kernel GDB debug stub port (6000...65535)"},
            "vphoned_bin": {"option": "--vphoned-bin", "type": str,
                            "help": "Path to signed vphoned binary"},
            "variant": {"option": "--variant", "type": str,
                        "help": "Firmware variant: less|regular|dev|jb|exp"},
            "install_ipa": {"option": "--install-ipa", "type": str,
                            "help": "Install IPA/TIPA after the guest control channel connects (unavailable with dfu)"},
            "no_vphoned": {"flag": "--no-vphoned", "type": bool, "default": False,
                           "help": "Exclude vphoned usage (patchless-only)"},
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
            n = _coerce(spec, pname, value)
            if int(n) < 0:
                raise ValueError(f"param {pname!r} must be >= 0")
            # The repeatable flag string is the value of the option_multi key.
            argv.extend([spec["option_multi"]] * int(n))
    # Positionals go last; Swift ArgumentParser consumes flags and positionals
    # in any order, and positionals keep their registry order (e.g. old new).
    argv.extend(positionals)
    return argv


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _sudo_askpass_env() -> dict:
    """Askpass env for commands that self-elevate via sudo -A.

    When VPHONE_SUDO_PASSWORD is set, writes a 0700 askpass helper in /tmp and
    exports SUDO_ASKPASS + SUDO_PASSWORD so the CLI's scripts (which honor
    ``sudo ${SUDO_ASKPASS:+-A}``) elevate without a TTY. The helper is removed
    after a synchronous run; for background runs it stays until the command
    exits (the elevation happens early). Same mechanism vphone-cli's own
    vm-create orchestrator uses for --sudo-password.
    """
    pw = os.environ.get("VPHONE_SUDO_PASSWORD")
    if not pw:
        return {}
    fd, path = tempfile.mkstemp(prefix="vphone-sudo-askpass.")
    safe = pw.replace("'", "'\\''")
    os.write(fd, f"#!/bin/sh\necho '{safe}'\n".encode())
    os.close(fd)
    os.chmod(path, 0o700)
    return {"SUDO_ASKPASS": path, "SUDO_PASSWORD": pw}


def run(cmd_key: str, **params) -> dict:
    """Run a registry command synchronously and return its result dict.

    Returns ``{"exit_code", "stdout", "stderr", "argv"}`` — a nonzero exit
    code still returns normally (the MCP tool formats it so the LLM sees the
    error). Raises RuntimeError on subprocess timeout (including partial
    output), ValueError on bad arguments, and RuntimeError if the binary is
    missing.
    """
    entry = _entry(cmd_key)
    argv = _build_argv(entry, params)
    timeout = entry.get("timeout_s", _DEFAULT_TIMEOUT_S)
    timeout = None if timeout <= 0 else timeout
    env = os.environ.copy()
    if entry.get("sudo_env"):
        env.update(_sudo_askpass_env())
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
    (a file-endpoint root, so the log is fetchable remotely).
    Returns ``{"pid", "log_path", "argv"}``.
    """
    entry = _entry(cmd_key)
    if not (entry.get("background") or entry.get("allow_background")):
        raise ValueError(f"command {cmd_key!r} does not support background execution")
    argv = _build_argv(entry, params)
    name = params.get("name")
    stem = str(name) if name else cmd_key
    log_dir = files.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{stem}_launch_{int(time.time())}.log"
    env = os.environ.copy()
    if entry.get("sudo_env"):
        env.update(_sudo_askpass_env())
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(
            argv,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    return {"pid": proc.pid, "log_path": str(log_path), "argv": argv}


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
