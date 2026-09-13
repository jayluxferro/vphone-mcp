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

These templates reproduce that chain at login: a **user LaunchAgent** runs
`/usr/bin/sudo -n` (non-interactive), backed by a NOPASSWD sudoers entry
scoped to the exact interpreter + module invocation.

## Install

```bash
./install.sh
```

`install.sh` resolves your machine's interpreter and user, generates the
real plist + sudoers under `~/.vphone/amfidont-agent/`, validates them,
and prints the three commands you run once (two need sudo):

1. `sudo cp …/com.vphone.amfidont.plist ~/Library/LaunchAgents/`
2. `sudo cp …/vphone-amfidont.sudoers /etc/sudoers.d/ && sudo visudo -c`
3. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vphone.amfidont.plist`

## Verify

```
launchctl print gui/$(id -u)/com.vphone.amfidont | grep state   # state = running
tail -2 ~/Library/Logs/vphone-amfidont.log                       # "Attached to amfid"
```

The daemon survives reboots (RunAtLoad + KeepAlive). Only one amfidont may
be attached to amfid at a time — stop any foreground `vphone-amfidont`
before bootstrapping the agent.
