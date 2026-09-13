# amfidont login-agent for vphone-cli

When the AMFI boot-arg is not set (e.g. after a macOS update clears it),
`vphone-cli.app` is ad-hoc signed and `amfid` SIGKILLs it at exec (exit 137).
[amfidont](https://github.com/zqxwce/amfidont) allows the app through by
attaching to `amfid` via LLDB and spoofing the Apple anchor for one path.

The catch: **LLDB can only attach from a process in the user's login
session** — launchd daemons (and even root agents in the gui domain) get
denied with `Failed to attach to process, should probably run as root`
regardless of uid. The chain that works is exactly the one a terminal uses:

```
login session → sudo → python -m amfidont --spoof-apple --path <app>
```

These files reproduce that chain at login: a **user LaunchAgent** runs
`/usr/bin/sudo -n` (non-interactive), backed by a NOPASSWD sudoers entry
scoped to the exact interpreter + module invocation.

## Install

One command does everything — generate machine-specific files, install the
sudoers entry (validated with `visudo` *before* installing so a broken file
can't brick sudo), install the LaunchAgent, reload it, and verify:

```bash
./install.sh
```

You'll be asked for your password once (sudoers step). The script is
idempotent — safe to re-run after upgrades or when paths change.

## Verify

```bash
launchctl print gui/$(id -u)/com.vphone.amfidont | grep state   # state = running
tail -3 ~/Library/Logs/vphone-amfidont.log                       # "Attached to amfid"
```

The agent survives reboots (RunAtLoad + KeepAlive). Only one amfidont may
be attached to amfid at a time — `install.sh` boots out any stale
same-label job before loading.

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.vphone.amfidont
sudo rm /etc/sudoers.d/vphone-amfidont
rm ~/Library/LaunchAgents/com.vphone.amfidont.plist
```
