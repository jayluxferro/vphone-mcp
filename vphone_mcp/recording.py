"""Screen recording, two ways.

**native** — vphone-cli's own recorder, driven through its menu (see
:mod:`vphone_mcp.menu`). It feeds ``AVAssetWriterInputPixelBufferAdaptor``
straight from the VM view in-process: no PNG per frame, no IPC, no ffmpeg, so
it is by far the cheaper of the two. It needs the Accessibility permission, and
its frame rate, file name and destination are vphone-cli's to choose.

**poll** — this server's own fallback, for when that permission is not granted.
The host control socket has no video channel, so a background thread asks for a
screenshot every ``1/fps`` seconds and pipes each full-resolution PNG into
ffmpeg (``image2pipe``): one temp frame on disk instead of gigabytes of PNGs. A
frame is ~3 MB and a capture round-trip ~90 ms, which is what caps the rate.
Without ffmpeg the frames are kept and zipped at the end, so either way there
is exactly one artifact.

One recording runs at a time (there is one VM), guarded by a module lock.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import files, menu

#: The socket answers a screenshot in ~90 ms, so anything above this just
#: drops frames and burns CPU.
MAX_FPS = 5
DEFAULT_FPS = 2
DEFAULT_MAX_SECONDS = 120

#: vphone-cli names its own recordings with this prefix.
NATIVE_PREFIX = "vphone-recording-"

_lock = threading.Lock()
_current: "Recorder | None" = None
_native: dict | None = None


def ffmpeg_bin() -> str | None:
    """The ffmpeg binary, or None (then recordings are zipped PNG frames)."""
    override = os.environ.get("VPHONE_MCP_FFMPEG_BIN", "").strip()
    if override:
        return override if Path(override).is_file() else None
    return shutil.which("ffmpeg")


class Recorder:
    """A running capture loop writing one artifact."""

    def __init__(self, client_factory, out_path: Path, fps: float, max_seconds: int):
        self.client_factory = client_factory
        self.out_path = out_path
        self.fps = fps
        self.max_seconds = max_seconds
        self.interval = 1.0 / fps
        self.frames = 0
        self.errors: list[str] = []
        self.stopped_reason = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self._proc: subprocess.Popen | None = None
        self._frames_dir: Path | None = None
        self._tmp_frame = out_path.with_suffix(".frame.tmp.png")
        self._log_path = out_path.with_suffix(out_path.suffix + ".ffmpeg.log")

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        binary = ffmpeg_bin()
        if binary:
            self._proc = subprocess.Popen(
                [
                    binary, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "image2pipe", "-framerate", f"{self.fps:g}", "-i", "-",
                    "-c:v", "libx264", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p",
                    # yuv420p needs even dimensions; the default 1290x2796
                    # already is, but a resized guest may not be.
                    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                    str(self.out_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                # A file, not a pipe: ffmpeg must never block on a full stderr
                # buffer while we are feeding it frames.
                stderr=open(self._log_path, "wb"),
            )
        else:
            self._frames_dir = self.out_path.with_suffix(".frames")
            self._frames_dir.mkdir(parents=True, exist_ok=True)
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="vphone-rec")
        self._thread.start()

    def _loop(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            if self.max_seconds and time.monotonic() - self._started >= self.max_seconds:
                self.stopped_reason = f"max_seconds ({self.max_seconds}s) reached"
                break
            self._capture()
            if self.errors and self.frames == 0 and len(self.errors) >= 3:
                self.stopped_reason = "capture kept failing — is the VM running?"
                break
            next_tick += self.interval
            remaining = next_tick - time.monotonic()
            if remaining > 0:
                self._stop.wait(remaining)
            else:  # behind schedule (slow socket): resync instead of drifting
                next_tick = time.monotonic()

    def _capture(self) -> None:
        resp = self.client_factory().screenshot(str(self._tmp_frame))
        if not resp.get("ok"):
            self.errors.append(str(resp.get("error", "unknown error"))[:200])
            return
        try:
            data = self._tmp_frame.read_bytes()
        except OSError as exc:
            self.errors.append(str(exc)[:200])
            return
        if not data:
            return
        try:
            if self._proc is not None and self._proc.stdin is not None:
                self._proc.stdin.write(data)
            elif self._frames_dir is not None:
                (self._frames_dir / f"frame_{self.frames:05d}.png").write_bytes(data)
        except (BrokenPipeError, OSError) as exc:
            self.errors.append(f"encoder write failed: {exc}"[:200])
            self._stop.set()
            return
        self.frames += 1

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, self.interval * 3))
        elapsed = time.monotonic() - self._started
        artifact = self._finish()
        self._tmp_frame.unlink(missing_ok=True)
        return {
            "path": str(artifact) if artifact else "",
            "frames": self.frames,
            "seconds": round(elapsed, 1),
            "fps_requested": self.fps,
            "fps_actual": round(self.frames / elapsed, 2) if elapsed > 0 else 0,
            "reason": self.stopped_reason or "stopped by request",
            "errors": self.errors[-3:],
        }

    def _finish(self) -> Path | None:
        """Close the encoder (or zip the frames) and return the artifact."""
        if self._proc is not None:
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
                self._proc.wait(timeout=120)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            # A killed (or still unreaped, None) encoder leaves a truncated
            # file behind; only a clean exit 0 is a finished artifact.
            if self._proc.returncode != 0 or not self.out_path.is_file():
                tail = ""
                try:
                    tail = self._log_path.read_text(errors="ignore").strip()[-300:]
                except OSError:
                    pass
                self.errors.append(f"ffmpeg failed: {tail or self._proc.returncode}")
                return None
            self._log_path.unlink(missing_ok=True)
            return self.out_path
        if self._frames_dir is not None:
            if not self.frames:
                shutil.rmtree(self._frames_dir, ignore_errors=True)
                return None
            archive = shutil.make_archive(
                str(self.out_path.with_suffix("")), "zip", self._frames_dir
            )
            shutil.rmtree(self._frames_dir, ignore_errors=True)
            return Path(archive)
        return None

    # -- introspection -----------------------------------------------------

    def status(self) -> dict:
        elapsed = time.monotonic() - self._started
        capturing = bool(self._thread is not None and self._thread.is_alive())
        return {
            "recording": True,
            # The loop stops on its own at max_seconds; the artifact is only
            # finalised by stop_recording(), so say which state this is in.
            "capturing": capturing,
            "reason": "" if capturing else (self.stopped_reason or "capture ended"),
            "path": str(self.out_path),
            "frames": self.frames,
            "seconds": round(elapsed, 1),
            "fps_requested": self.fps,
            "fps_actual": round(self.frames / elapsed, 2) if elapsed > 0 else 0,
            "encoder": "ffmpeg" if self._proc is not None else "png frames (no ffmpeg)",
            "stops_after": self.max_seconds,
            "errors": self.errors[-3:],
        }


# ---------------------------------------------------------------------------
# Module-level single recording
# ---------------------------------------------------------------------------

def search_dirs() -> list[Path]:
    """Where a native recording might land (``$VPHONE_MCP_RECORDING_SEARCH_DIRS``).

    vphone-cli picks the destination, not us; its saved-file dialog usually
    names it, and this is the fallback when the dialog cannot be read.
    """
    raw = os.environ.get("VPHONE_MCP_RECORDING_SEARCH_DIRS", "").strip()
    if raw:
        entries = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        home = Path.home()
        entries = [
            str(home / "Movies"),
            str(home / "Desktop"),
            str(home / "Downloads"),
            str(home / "Pictures"),
            str(files.vphone_root()),
            *(str(lib) for lib in files.library_roots()),
            tempfile.gettempdir(),
        ]
    found: list[Path] = []
    for entry in entries:
        try:
            resolved = Path(entry).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.is_dir() and resolved not in found:
            found.append(resolved)
    return found


def _path_from_dialog(texts: list[str]) -> Path | None:
    """An existing recording path mentioned by vphone-cli's saved dialog."""
    for text in texts:
        for candidate in re.findall(r"(?:file://)?(/[^\s\"\']+)", text):
            path = Path(candidate.replace("file://", ""))
            if path.is_file():
                return path
        # Some dialogs name the file only; look for it in the usual places.
        for token in re.findall(rf"{NATIVE_PREFIX}[\w.\-]+", text):
            for directory in search_dirs():
                candidate_path = directory / token
                if candidate_path.is_file():
                    return candidate_path
    return None


def _newest_native_recording(after: float) -> Path | None:
    """The newest vphone-recording-* written since the recording started."""
    newest: Path | None = None
    for directory in search_dirs():
        try:
            entries = list(directory.glob(f"{NATIVE_PREFIX}*"))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_file() or entry.stat().st_mtime < after - 5:
                    continue
                if newest is None or entry.stat().st_mtime > newest.stat().st_mtime:
                    newest = entry
            except OSError:
                continue
    return newest


def _publish(path: Path) -> Path:
    """Make a native recording fetchable without copying it twice.

    The file stays where vphone-cli put it (its "Reveal in Finder" keeps
    working); a hard link in the served directory costs no extra space, and a
    copy is the fallback across filesystems.
    """
    if not files.enabled() or files.in_scope(path):
        return path
    target = files.recording_dir() / path.name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return target
        os.link(path, target)
    except OSError:
        try:
            shutil.copy2(path, target)
        except OSError:
            return path
    return target


def _start_native() -> dict:
    """Toggle vphone-cli's own recorder on. Raises MenuError when unreachable."""
    global _native
    item = menu.toggle_recording()
    _native = {"item": item, "started": time.time()}
    return {
        "recording": True,
        "capturing": True,
        "mode": "native",
        "menu_item": item,
        "note": "vphone-cli is recording with its own encoder — it chooses the "
        "frame rate, file name and destination; stop_recording() finds the file",
    }


def _stop_native() -> dict:
    """Toggle it off, then locate, unblock and publish the artifact."""
    global _native
    state = _native or {}
    _native = None
    item = menu.toggle_recording()
    # The app puts up a "saved" dialog; it names the file and blocks the UI
    # until something dismisses it.
    dialog = {"texts": [], "buttons": []}
    for _ in range(10):
        time.sleep(0.5)
        dialog = menu.read_dialog()
        if dialog["buttons"]:
            break
    path = _path_from_dialog(dialog["texts"])
    dismissed = menu.dismiss_dialog() if dialog["buttons"] else ""
    if path is None:
        path = _newest_native_recording(state.get("started", 0))
    result = {
        "mode": "native",
        "menu_item": item,
        "seconds": round(time.time() - state.get("started", time.time()), 1),
        "dialog": dialog["texts"][:3],
        "dismissed": dismissed,
    }
    if path is None:
        result["path"] = ""
        result["error"] = (
            "recording stopped, but its file was not found. Set "
            "VPHONE_MCP_RECORDING_SEARCH_DIRS to the directory vphone-cli "
            f"saves to (looked in: {', '.join(str(d) for d in search_dirs())})"
        )
        return result
    published = _publish(path)
    result["path"] = str(published)
    if published != path:
        result["original_path"] = str(path)
    return result


def start(
    client_factory, name: str, fps: float, max_seconds: int, mode: str = "auto"
) -> dict:
    """Start a recording, or raise RuntimeError if one is already running."""
    global _current
    if mode not in ("auto", "native", "poll"):
        raise ValueError("mode must be 'auto', 'native' or 'poll'")
    if not 0 < fps <= MAX_FPS:
        raise ValueError(f"fps must be > 0 and <= {MAX_FPS} (the socket answers ~11/s)")
    fallback = ""
    with _lock:
        if _current is not None or _native is not None:
            raise RuntimeError("a recording is already running — stop_recording() first")
        if mode in ("auto", "native"):
            try:
                return _start_native()
            except menu.MenuError as exc:
                if mode == "native":
                    raise RuntimeError(str(exc)) from None
                fallback = str(exc).splitlines()[0]

        files.prune_recordings()
        suffix = ".mp4" if ffmpeg_bin() else ".zip"
        out = files.recording_dir() / f"{files.safe_filename(name)}{suffix}"
        recorder = Recorder(client_factory, out, fps, max_seconds)
        recorder.start()
        _current = recorder
    state = recorder.status()
    state["mode"] = "poll"
    if fallback:
        state["native_unavailable"] = fallback
    return state


def stop() -> dict:
    """Finish the running recording, or raise RuntimeError if there is none."""
    global _current
    with _lock:
        if _native is not None:
            try:
                return _stop_native()
            except menu.MenuError as exc:
                raise RuntimeError(str(exc)) from None
        recorder = _current
        _current = None
    if recorder is None:
        raise RuntimeError("no recording is running")
    result = recorder.stop()
    result["mode"] = "poll"
    return result


def status() -> dict:
    with _lock:
        if _native is not None:
            return {
                "recording": True,
                "capturing": True,
                "mode": "native",
                "menu_item": _native["item"],
                "seconds": round(time.time() - _native["started"], 1),
            }
        recorder = _current
    if recorder is None:
        return {"recording": False}
    state = recorder.status()
    state["mode"] = "poll"
    return state
