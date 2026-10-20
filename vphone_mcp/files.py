"""Hand tool-produced files to remote clients over the HTTP transport.

Tool results name paths on the *server* host — the screenshot PNG, a launch
log. That is all a local stdio client needs, but a remote agent cannot open any
of them. This module turns such a path into a fetchable URL::

    http://<hash>:<mcp-token>@host:8765/files/screenshots/screen.png
           └ sha256 of the resolved absolute path
                   └ the same bearer token the MCP endpoint uses

Exactly one directory is served: ``$VPHONE_ROOT/vphone-mcp``, which is this
server's own — ``logs/``, ``screenshots/``, ``recordings/`` and ``uploads/``. The VM library and
the rest of the vphone root stay unreachable; anything worth fetching is written
here. ``uploads/`` is the one writable spot: a client PUTs an IPA there (through
a URL minted by ``create_upload_url``) and then installs it.

The URL exposes a path relative to that root, and carries the hash of the full
path it was minted for as the Basic-auth *user*, with the MCP token as the
*password*. Serving therefore needs no state at all: join the relative path onto
the root, normalise it, and serve only when the result stays inside the root, is
the spelling this module would have minted for it, and hashes to what the
request presented. A tampered path — ``../``, an absolute path, a different
file — resolves elsewhere, so its hash no longer matches and it never reaches
the filesystem. Paths are resolved before the scope check, so a symlink inside
the root pointing outside it is rejected too.
"""

import hashlib
import hmac
import os
import time
from pathlib import Path
from urllib.parse import quote

ROUTE_PREFIX = "/files"

_base_url = ""  # scheme://host[:port], set for network transports
_token = ""  # MCP bearer token, used as the Basic-auth password


# ---------------------------------------------------------------------------
# The served root
# ---------------------------------------------------------------------------

def vphone_root() -> Path:
    return _resolved(os.environ.get("VPHONE_ROOT") or Path.home() / ".vphone")


def root() -> Path:
    """The only directory whose files may be served."""
    return vphone_root() / "vphone-mcp"


def screenshot_dir() -> Path:
    """Default destination for ``screenshot()`` — inside the root, so the
    capture is fetchable remotely without the agent having to pick a path."""
    return root() / "screenshots"


def log_dir() -> Path:
    """Where detached ``vm launch`` / ``boot`` processes write their logs."""
    return root() / "logs"


def recording_dir() -> Path:
    """Where finished screen recordings land."""
    return root() / "recordings"


def recording_ttl_seconds() -> int:
    """How long a recording is kept (``$VPHONE_MCP_RECORDING_TTL_HOURS``,
    default 24); 0 keeps them until something deletes them."""
    return _ttl_env("VPHONE_MCP_RECORDING_TTL_HOURS", 24)


def upload_dir() -> Path:
    """The only writable directory — where clients PUT IPAs and other inputs."""
    return root() / "uploads"


PART_SUFFIX = ".part"

#: A half-written upload is never a deliverable, so it goes sooner than a
#: complete one, and even when uploads are kept forever.
_STALE_PART_SECONDS = 3600


def _ttl_env(name: str, default_hours: int) -> int:
    """Hours from an env var as seconds; 0 means "keep"."""
    raw = os.environ.get(name, "").strip()
    try:
        hours = int(raw) if raw else default_hours
    except ValueError:
        hours = default_hours
    return max(hours, 0) * 3600


def upload_ttl_seconds() -> int:
    """How long a staged upload is kept (``$VPHONE_MCP_UPLOAD_TTL_HOURS``,
    default 3); 0 keeps them until something deletes them.

    An upload is staging for one install, not storage: it is read minutes
    later and then only takes up space.
    """
    return _ttl_env("VPHONE_MCP_UPLOAD_TTL_HOURS", 3)


def max_upload_bytes() -> int:
    """Upload size cap, 0 for none (``$VPHONE_MCP_MAX_UPLOAD_MB``, default 4096)."""
    raw = os.environ.get("VPHONE_MCP_MAX_UPLOAD_MB", "").strip()
    try:
        megabytes = int(raw) if raw else 4096
    except ValueError:
        megabytes = 4096
    return max(megabytes, 0) * 1024 * 1024


def _resolved(path: str | Path) -> Path:
    """Absolute path with symlinks followed — the identity used everywhere."""
    return Path(path).expanduser().resolve()


def in_scope(path: str | Path) -> bool:
    """True when path resolves inside :func:`root`."""
    try:
        target = _resolved(path)
    except (OSError, RuntimeError):
        return False
    base = root()
    return target == base or target.is_relative_to(base)


def scope_error(path: str | Path) -> str:
    """Message explaining why path is not servable."""
    return (
        f"{path} is outside {root()}, the only directory the file endpoint "
        "serves. Write it there (or under logs/ or screenshots/) if the client "
        "has to fetch it."
    )


# ---------------------------------------------------------------------------
# Hash / lookup
# ---------------------------------------------------------------------------

def hash_for(path: str | Path) -> str:
    """SHA-256 of the resolved absolute path — the Basic-auth user."""
    return hashlib.sha256(str(_resolved(path)).encode()).hexdigest()


def relative_path(path: str | Path) -> str | None:
    """Path relative to the root, or None when it is out of scope."""
    if not in_scope(path):
        return None
    return _resolved(path).relative_to(root()).as_posix()


def resolve(relative: str, presented_hash: str) -> Path | None:
    """The file a request is for, or None.

    Served only when the relative path, joined onto the root and normalised,
    stays inside it, is the canonical spelling of the result (the one
    :func:`relative_path` would mint, so each file has exactly one URL), hashes
    to what the request presented, and is a regular file.
    """
    if not presented_hash or "\x00" in relative:
        return None
    # An absolute path would swallow the root in the join below.
    relative = relative.lstrip("/")
    try:
        candidate = _resolved(root() / relative)
    except (OSError, RuntimeError):
        return None
    if relative_path(candidate) != relative:
        return None
    if not hmac.compare_digest(hash_for(candidate), presented_hash):
        return None
    if not candidate.is_file():
        return None
    return candidate


def list_uploads() -> list[Path]:
    """Files currently staged in :func:`upload_dir`, oldest name first."""
    try:
        return sorted(p for p in upload_dir().glob("*") if p.is_file())
    except OSError:
        return []


def _prune(directory: Path, ttl: int, short_lived: dict[str, int] | None = None) -> list[str]:
    """Delete files in directory past their TTL, by mtime. Never raises.

    Age comes from the filesystem, so nothing has to be remembered between
    requests and a restart changes nothing. ``short_lived`` maps a filename
    suffix to its own, shorter TTL.
    """
    now = time.time()
    removed: list[str] = []
    try:
        entries = sorted(p for p in directory.glob("*") if p.is_file())
    except OSError:
        return removed
    for entry in entries:
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        limit = ttl
        for suffix, suffix_ttl in (short_lived or {}).items():
            if entry.name.endswith(suffix):
                limit = suffix_ttl
                break
        if not limit or age <= limit:
            continue
        try:
            entry.unlink()
        except OSError:
            continue
        removed.append(entry.name)
    return removed


def prune_uploads() -> list[str]:
    """Delete staged uploads past their TTL (and stale ``.part`` files).

    Called before each new upload and on each listing, so the directory cannot
    grow without bound: IPAs are big and nothing else ever removes them.
    """
    return _prune(
        upload_dir(),
        upload_ttl_seconds(),
        {PART_SUFFIX: _STALE_PART_SECONDS},
    )


def prune_recordings() -> list[str]:
    """Delete recordings past their TTL — called when a new one starts."""
    return _prune(recording_dir(), recording_ttl_seconds())


def delete_upload(filename: str) -> Path:
    """Delete one staged upload, or raise FileNotFoundError."""
    target = upload_dir() / safe_filename(filename)
    if not target.is_file():
        raise FileNotFoundError(f"{target} is not a staged upload")
    target.unlink()
    return target


def safe_filename(filename: str) -> str:
    """A bare filename for :func:`upload_dir`, or raise ValueError.

    Uploads are flat by design: no directories, no traversal, nothing hidden.
    """
    name = filename.strip()
    if not name or name in (".", "..") or Path(name).name != name:
        raise ValueError(
            f"{filename!r} is not a plain filename — uploads take a bare name "
            "like 'MyApp.ipa', with no directory part"
        )
    if name.startswith(".") or "\x00" in name:
        raise ValueError(f"{filename!r} is not an allowed upload filename")
    return name


def upload_target(relative: str, presented_hash: str) -> Path | None:
    """The path an upload may be written to, or None.

    Same rules as :func:`resolve` — canonical spelling, hash match — minus the
    existence check (the file is being created), plus the restriction that
    ``uploads/`` is the only writable directory.
    """
    if not presented_hash or "\x00" in relative:
        return None
    relative = relative.lstrip("/")
    try:
        candidate = _resolved(root() / relative)
    except (OSError, RuntimeError):
        return None
    if candidate.parent != upload_dir():
        return None
    if relative_path(candidate) != relative:
        return None
    if not hmac.compare_digest(hash_for(candidate), presented_hash):
        return None
    if candidate.is_dir():
        return None
    return candidate


# ---------------------------------------------------------------------------
# URL emission
# ---------------------------------------------------------------------------

def configure(base_url: str, token: str) -> None:
    """Enable URL emission against a base like ``https://host:8765``."""
    global _base_url, _token
    _base_url = base_url.rstrip("/")
    _token = token


def enabled() -> bool:
    return bool(_base_url)


def upload_url(filename: str) -> str | None:
    """URL a client can PUT ``filename`` to, or None when not serving remotely."""
    return url_for(upload_dir() / safe_filename(filename))


def url_for(path: str | Path) -> str | None:
    """Return the URL for a servable path, or None when it is not servable."""
    if not _base_url:
        return None
    relative = relative_path(path)
    if relative is None:
        return None
    scheme, sep, host = _base_url.partition("://")
    userinfo = f"{hash_for(path)}:{quote(_token, safe='')}@"
    segments = "/".join(quote(part, safe="") for part in relative.split("/"))
    return f"{scheme}{sep}{userinfo}{host}{ROUTE_PREFIX}/{segments}"
