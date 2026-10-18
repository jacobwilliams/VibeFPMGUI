"""The framing the Language Server Protocol and the Debug Adapter Protocol
share: a ``Content-Length`` header, a blank line, then a JSON message (see
`vibefpmgui.lsp` and `vibefpmgui.dap`)."""

from __future__ import annotations

import json
import re

_LENGTH = re.compile(rb"Content-Length:\s*(\d+)", re.IGNORECASE)


def frame(message: dict) -> bytes:
    """A message, ready to write."""
    body = json.dumps(message).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


class FrameReader:
    """The messages in a stream, read as it arrives (a message may come in
    pieces, or several at once). Something that isn't a header is skipped,
    and a message that isn't JSON dropped."""

    def __init__(self):
        self.buffer = b""

    def clear(self):
        self.buffer = b""

    def feed(self, data: bytes) -> list[dict]:
        self.buffer += data
        messages = []
        while True:
            end = self.buffer.find(b"\r\n\r\n")
            if end < 0:
                return messages
            match = _LENGTH.search(self.buffer[:end])
            if not match:  # not a header: skip it
                self.buffer = self.buffer[end + 4:]
                continue
            length = int(match.group(1))
            if len(self.buffer) < end + 4 + length:
                return messages
            body, self.buffer = self.buffer[end + 4:end + 4 + length], self.buffer[end + 4 + length:]
            try:
                message = json.loads(body)
            except ValueError:
                continue
            if isinstance(message, dict):
                messages.append(message)
