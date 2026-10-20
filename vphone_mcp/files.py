"""Hand tool-produced files to remote clients over the HTTP transport.

Tool results name paths on the *server* host — the screenshot PNG, a launch
log. That is all a local stdio client needs, but a remote agent cannot open any
of them. This module turns such a path into a fetchable, short-lived link::

    http://<expires>:<signature>@host:8765/files/screenshots/screen.png
           │         └ HMAC-SHA256 over (method, canonical path, expires)
           └ unix time the link stops working (default: minted + 1h)

Exactly one directory is served: ``$VPHONE_ROOT/vphone-mcp``, which is this
server's own — ``logs/``, ``screenshots/``, ``recordings/`` and ``uploads/``. The VM library and
the rest of the vphone root stay unreachable; anything worth fetching is written
here. ``uploads/`` is the one writable spot: a client PUTs an IPA there (through
a URL minted by ``create_upload_url``) and then installs it.

The links end up in tool results, so in model context and transcripts.
That is why they carry a capability, not the MCP token. Earlier versions put the
token itself in the Basic password, and every screenshot then leaked the key to
the whole server. A link now authorises one method on one file until it
expires, and nothing else. Its signing key is derived from the token (see
:func:`_link_key`), so the token is never in a link and cannot be recovered from
one. Rotating the token revokes every outstanding link.

Serving stays stateless: nothing remembers which links were handed out.
:func:`check_link` recomputes the signature from the request's own method, path
and expiry. It does that before the requested path is looked up on disk, so an
unauthenticated request never gets a ``stat`` of anything it named. The signed path is the
canonical spelling :func:`relative_path` mints, so a tampered path (``../``, an
absolute path, a different file) no longer matches its signature and gets a
401. After that the usual scope rules still apply: paths are resolved before
the scope check, so a symlink inside the root that points outside it is
rejected even when the link is genuine.
"""

import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

ROUTE_PREFIX = "/files"

#: Link lifetime when $VPHONE_MCP_LINK_TTL_SECONDS is unset, and its ceiling.
#: The ceiling exists so a typo cannot quietly turn links into long-lived
#: credentials again.
DEFAULT_LINK_TTL = 3600
MAX_LINK_TTL = 7 * 86400

_base_url = ""  # scheme://host[:port], set for network transports
_key = b""  # signs file links; derived from the MCP token, never the token itself
_ttl = DEFAULT_LINK_TTL


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


def library_roots() -> list[Path]:
    """VM library roots to search, most specific first.

    ``$VPHONE_LIBRARY_ROOT`` when set; otherwise vphone-cli's own default,
    ``~/.vphone/machines`` (``vm launch --help`` in 2.6.0), followed by the
    legacy ``~/.vphone/VMs`` that older releases used. Bundles are
    ``<root>/<name>/`` holding ``config.plist`` and, while running, ``vphone.sock``.
    """
    if env := os.environ.get("VPHONE_LIBRARY_ROOT"):
        return [Path(env).expanduser()]
    base = Path.home() / ".vphone"
    return [base / "machines", base / "VMs"]


def recording_dir() -> Path:
    """Where finished screen recordings land."""
    return root() / "recordings"


def recording_ttl_seconds() -> int:
    """How long a recording is kept (``$VPHONE_MCP_RECORDING_TTL_HOURS``).

    Default 0 — keep them until something deletes them: a recording is usually
    evidence, so sweeping it is opt-in rather than a surprise a day later.
    """
    return _ttl_env("VPHONE_MCP_RECORDING_TTL_HOURS", 0)


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
# Signed links
# ---------------------------------------------------------------------------

class LinkError(Exception):
    """The request's credentials are not a live link for it (answered 401)."""


_EXPIRES = re.compile(r"[0-9]{1,12}")


def _link_key(token: str) -> bytes:
    """The key that signs file links.

    It is an HMAC of the token under a fixed label, not the token itself, so a
    signature is useless anywhere except this endpoint. The derivation is
    deterministic, which means links survive a restart under the same token.
    With no token at all (``--allow-anonymous``) there is nothing to derive
    from, so the key is random per process: links are still unforgeable, and
    they last only until the server restarts.
    """
    if not token:
        return secrets.token_bytes(32)
    return hmac.new(
        token.encode("utf-8"), b"vphone-mcp file link v1", hashlib.sha256
    ).digest()


def _utf8(text: str) -> bytes:
    # compare_digest() raises TypeError on a non-ASCII str (a 500 from any
    # client sending "é" as credentials), so everything is compared as bytes.
    # surrogatepass encodes every str, so the encoding cannot raise either.
    return text.encode("utf-8", "surrogatepass")


def _signature(method: str, relative: str, expires: int) -> str:
    """Hex HMAC over (method, canonical absolute path, expiry).

    The path is built from the relative spelling, root + "/" + relative, the
    same way at minting and at checking. A non-canonical spelling of the same
    file therefore signs differently. NUL separates the fields because it
    cannot occur in a path, a method or a decimal number, so no two
    (method, path, expiry) triples produce the same message.
    """
    message = b"\0".join(
        (_utf8(method), _utf8(f"{root()}/{relative}"), str(expires).encode())
    )
    return hmac.new(_key, message, hashlib.sha256).hexdigest()


def check_link(method: str, relative: str, expires: str, signature: str) -> None:
    """Raise :class:`LinkError` unless the credentials are a live link minted
    for exactly this method and relative path.

    Nothing here looks at the requested path on disk (the only filesystem
    access is resolving the fixed server root), so it runs first. HEAD counts
    as GET. The signature is checked before the expiry, so
    "expired" is only ever said about a link this server really minted.
    """
    if not _key:
        raise LinkError("file links are not enabled on this server")
    method = "GET" if method == "HEAD" else method
    if not _EXPIRES.fullmatch(expires) or "\x00" in relative:
        raise LinkError("not a file link minted by this server")
    expected = _signature(method, relative, int(expires))
    if not hmac.compare_digest(_utf8(expected), _utf8(signature)):
        raise LinkError(
            f"this link is not valid for {method} on this path: links are "
            "minted per file and method by the tools that report the file"
        )
    if int(expires) < time.time():
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(expires)))
        raise LinkError(
            f"this link expired at {stamp}: ask for a fresh one "
            "(get_file_url for downloads, create_upload_url for uploads)"
        )


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def relative_path(path: str | Path) -> str | None:
    """Path relative to the root, or None when it is out of scope."""
    if not in_scope(path):
        return None
    return _resolved(path).relative_to(root()).as_posix()


def resolve(relative: str, expires: str, signature: str) -> Path | None:
    """The file a GET/HEAD is for, or None (404).

    Raises :class:`LinkError` (401) when the credentials are not a live GET
    link for this exact path. Once they are, the file is served only if the
    path, joined onto the root and normalised, still stays inside it and is
    still the canonical spelling (a component may have become a symlink since
    the link was minted), and if it is a regular file.
    """
    check_link("GET", relative, expires, signature)
    # An absolute path would swallow the root in the join below.
    relative = relative.lstrip("/")
    try:
        candidate = _resolved(root() / relative)
    except (OSError, RuntimeError):
        return None
    if relative_path(candidate) != relative:
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


def upload_target(relative: str, expires: str, signature: str) -> Path | None:
    """The path a PUT may write, or None (404).

    The same rules as :func:`resolve`, but checked against a PUT link (a GET
    link for the same file cannot overwrite it). The existence check is
    dropped, since the file is being created, and ``uploads/`` must be the
    parent: it is the only writable directory, whatever a link says.
    """
    check_link("PUT", relative, expires, signature)
    relative = relative.lstrip("/")
    try:
        candidate = _resolved(root() / relative)
    except (OSError, RuntimeError):
        return None
    if candidate.parent != upload_dir():
        return None
    if relative_path(candidate) != relative:
        return None
    if candidate.is_dir():
        return None
    return candidate


# ---------------------------------------------------------------------------
# URL emission
# ---------------------------------------------------------------------------

def configure(base_url: str, token: str, ttl: int = DEFAULT_LINK_TTL) -> None:
    """Enable link emission against a base like ``https://host:8765``.

    Only the derived signing key is kept; the token is not stored here.
    """
    global _base_url, _key, _ttl
    _base_url = base_url.rstrip("/")
    _key = _link_key(token)
    _ttl = ttl


def enabled() -> bool:
    return bool(_base_url)


def link_ttl() -> int:
    """Seconds a newly minted link stays valid."""
    return _ttl


def upload_url(filename: str) -> str | None:
    """PUT link for ``uploads/<filename>``, or None when not serving remotely."""
    return _mint("PUT", upload_dir() / safe_filename(filename))


def url_for(path: str | Path) -> str | None:
    """GET link for a servable path, or None when it is not servable."""
    return _mint("GET", path)


def _mint(method: str, path: str | Path) -> str | None:
    if not _base_url:
        return None
    relative = relative_path(path)
    if relative is None:
        return None
    expires = int(time.time()) + _ttl
    scheme, sep, host = _base_url.partition("://")
    # Basic credentials ride in the URL, so `curl -O '<url>'` and
    # `curl -T file '<url>'` need nothing else. Both halves are plain ASCII
    # ([0-9] and [0-9a-f]), so there is nothing to percent-encode.
    userinfo = f"{expires}:{_signature(method, relative, expires)}@"
    segments = "/".join(quote(part, safe="") for part in relative.split("/"))
    return f"{scheme}{sep}{userinfo}{host}{ROUTE_PREFIX}/{segments}"
