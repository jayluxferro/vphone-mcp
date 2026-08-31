"""Low-level client for the vphone-cli host control Unix socket."""

import json
import socket
from pathlib import Path


def image_block(resp: dict) -> list[dict]:
    """Return an MCP image content block for the socket's inline screenshot.

    The socket's ``image`` field is a base64-encoded grayscale JPEG (1/3-scale,
    ~430x932) captured after the command settles. Returns an empty list when
    the response carries no image, so callers can concatenate unconditionally.
    """
    if resp.get("image"):
        return [{"type": "image", "data": resp["image"], "mimeType": "image/jpeg"}]
    return []


class VPhoneClient:
    """Sends JSON commands to the vphone.sock Unix domain socket.

    One request per connection: connect, send one JSON line, read one JSON
    line, disconnect. Every method returns the raw response dict — callers
    decide how to surface it. Missing/refused sockets produce an error dict,
    never an exception.
    """

    def __init__(self, socket_path: str | Path):
        self.socket_path = str(socket_path)

    def _send(self, msg: dict) -> dict:
        """Connect, send one JSON line, read one JSON line, disconnect."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self.socket_path)
            payload = json.dumps(msg) + "\n"
            sock.sendall(payload.encode())

            # Read response
            sock.settimeout(30.0)
            chunks: list[bytes] = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
                if b"\n" in data:
                    break

            raw = b"".join(chunks).strip()
            return json.loads(raw) if raw else {"ok": False, "error": "empty response"}
        except FileNotFoundError:
            return {"ok": False, "error": f"socket not found: {self.socket_path}"}
        except ConnectionRefusedError:
            return {"ok": False, "error": "connection refused — is vphone-cli running?"}
        finally:
            sock.close()

    def screenshot(self, path: str | None = None) -> dict:
        msg: dict = {"t": "screenshot"}
        if path:
            msg["path"] = path
        return self._send(msg)

    def tap(
        self,
        x: float,
        y: float,
        *,
        screen: bool = True,
        delay: int = 500,
    ) -> dict:
        msg: dict = {"t": "tap", "x": x, "y": y}
        if screen is not None:
            msg["screen"] = screen
        if delay is not None:
            msg["delay"] = delay
        return self._send(msg)

    def swipe(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        ms: int = 300,
        *,
        screen: bool = True,
        delay: int = 500,
    ) -> dict:
        msg: dict = {
            "t": "swipe",
            "x1": x1,
            "y1": y1,
            "x2": x2,
            "y2": y2,
            "ms": ms,
        }
        if screen is not None:
            msg["screen"] = screen
        if delay is not None:
            msg["delay"] = delay
        return self._send(msg)

    def key(
        self,
        name: str,
        *,
        screen: bool = True,
        delay: int = 500,
    ) -> dict:
        msg: dict = {"t": "key", "name": name}
        if screen is not None:
            msg["screen"] = screen
        if delay is not None:
            msg["delay"] = delay
        return self._send(msg)

    def type_text(
        self,
        text: str,
        *,
        screen: bool = True,
        delay: int = 500,
    ) -> dict:
        """Set the guest clipboard via the host control socket.

        NOT text input: the hostctl ``type`` command only sets the guest
        clipboard (which apps can then paste from). The socket has no channel
        for synthetic keystrokes.
        """
        msg: dict = {"t": "type", "text": text}
        if screen is not None:
            msg["screen"] = screen
        if delay is not None:
            msg["delay"] = delay
        return self._send(msg)
