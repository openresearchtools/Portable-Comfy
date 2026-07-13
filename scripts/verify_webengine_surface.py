#!/usr/bin/env python3
"""Capture and validate a Qt WebEngine viewport over loopback Chromium DevTools."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import secrets
import socket
import struct
import sys
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any


class SurfaceError(RuntimeError):
    """The WebEngine target or its rendered output was not usable."""


def _read_exact(stream: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise SurfaceError("DevTools WebSocket closed unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class _WebSocket:
    def __init__(self, url: str, timeout: float) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise SurfaceError("DevTools WebSocket must use loopback ws://")
        if parsed.port is None:
            raise SurfaceError("DevTools WebSocket URL has no port")
        self._host = parsed.hostname
        self._port = parsed.port
        self._path = parsed.path or "/"
        if parsed.query:
            self._path += "?" + parsed.query
        self._timeout = timeout
        self._socket: socket.socket | None = None

    def __enter__(self) -> "_WebSocket":
        stream = socket.create_connection((self._host, self._port), self._timeout)
        stream.settimeout(self._timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: {self._host}:{self._port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        stream.sendall(request.encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(_read_exact(stream, 1))
            if len(response) > 64 * 1024:
                raise SurfaceError("oversized DevTools WebSocket handshake")
        header = bytes(response).decode("iso-8859-1")
        if not header.startswith("HTTP/1.1 101"):
            raise SurfaceError(
                f"DevTools WebSocket handshake failed: {header.splitlines()[0]}"
            )
        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        headers = {
            name.strip().lower(): value.strip()
            for line in header.split("\r\n")[1:]
            if ":" in line
            for name, value in (line.split(":", 1),)
        }
        if headers.get("sec-websocket-accept") != expected:
            raise SurfaceError("DevTools WebSocket returned an invalid accept key")
        self._socket = stream
        return self

    def __exit__(self, *_args: object) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._socket is None:
            raise SurfaceError("DevTools WebSocket is not connected")
        mask = secrets.token_bytes(4)
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((0x80 | opcode, 0xFE)) + struct.pack("!H", length)
        else:
            header = bytes((0x80 | opcode, 0xFF)) + struct.pack("!Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._socket.sendall(header + mask + masked)

    def send_json(self, value: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(value, separators=(",", ":")).encode())

    def receive_json(self) -> dict[str, Any]:
        if self._socket is None:
            raise SurfaceError("DevTools WebSocket is not connected")
        message = bytearray()
        message_opcode: int | None = None
        while True:
            first, second = _read_exact(self._socket, 2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", _read_exact(self._socket, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", _read_exact(self._socket, 8))[0]
            if length > 64 * 1024 * 1024:
                raise SurfaceError("oversized DevTools WebSocket frame")
            mask = _read_exact(self._socket, 4) if masked else b""
            payload = _read_exact(self._socket, length)
            if masked:
                payload = bytes(
                    value ^ mask[index % 4] for index, value in enumerate(payload)
                )
            if opcode == 0x8:
                raise SurfaceError("DevTools WebSocket closed before replying")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode in {0x1, 0x2}:
                message_opcode = opcode
                message = bytearray(payload)
            elif opcode == 0x0 and message_opcode is not None:
                message.extend(payload)
            else:
                continue
            if not final:
                continue
            if message_opcode != 0x1:
                raise SurfaceError("DevTools returned a non-text response")
            try:
                value = json.loads(message)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise SurfaceError("DevTools returned invalid JSON") from error
            if not isinstance(value, dict):
                raise SurfaceError("DevTools returned a non-object response")
            return value


def _devtools_targets(endpoint: str, timeout: float) -> list[dict[str, Any]]:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise SurfaceError("DevTools endpoint must use loopback HTTP")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with opener.open(
                endpoint.rstrip("/") + "/json/list", timeout=2
            ) as response:
                value = json.load(response)
            if isinstance(value, list):
                targets = [item for item in value if isinstance(item, dict)]
                if any(item.get("type") == "page" for item in targets):
                    return targets
        except (OSError, ValueError, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(0.2)
    raise SurfaceError(
        f"Qt WebEngine DevTools did not expose a page target: {last_error}"
    )


def _command(
    socket_: _WebSocket, identifier: int, method: str, **params: Any
) -> dict[str, Any]:
    socket_.send_json({"id": identifier, "method": method, "params": params})
    while True:
        response = socket_.receive_json()
        if response.get("id") != identifier:
            continue
        if "error" in response:
            raise SurfaceError(f"DevTools {method} failed: {response['error']}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise SurfaceError(f"DevTools {method} returned no result")
        return result


def capture_surface(
    endpoint: str,
    output: Path,
    timeout: float,
    *,
    url_prefix: str | None = "http://127.0.0.1:",
    title_contains: str | None = "comfy",
    body_contains: str | None = None,
) -> dict[str, Any]:
    targets = _devtools_targets(endpoint, timeout)
    pages = [item for item in targets if item.get("type") == "page"]
    target = next(
        (
            item
            for item in pages
            if str(item.get("url", "")).startswith("http://127.0.0.1:")
        ),
        pages[0],
    )
    websocket_url = target.get("webSocketDebuggerUrl")
    if not isinstance(websocket_url, str):
        raise SurfaceError("Qt WebEngine page target has no WebSocket URL")

    with _WebSocket(websocket_url, timeout) as devtools:
        evaluated = _command(
            devtools,
            1,
            "Runtime.evaluate",
            expression=(
                "({readyState:document.readyState,title:document.title,"
                "url:location.href,width:document.documentElement.clientWidth,"
                "height:document.documentElement.clientHeight,"
                "bodyChildren:document.body?document.body.children.length:0,"
                "bodyText:document.body?(document.body.innerText||'').slice(0,4096):''})"
            ),
            returnByValue=True,
        )
        remote = evaluated.get("result")
        document = remote.get("value") if isinstance(remote, dict) else None
        if not isinstance(document, dict):
            raise SurfaceError("DevTools could not inspect the rendered document")
        if document.get("readyState") != "complete":
            raise SurfaceError(f"WebEngine document is not complete: {document}")
        if url_prefix is not None and not str(document.get("url", "")).startswith(
            url_prefix
        ):
            raise SurfaceError(f"WebEngine loaded an unexpected URL: {document}")
        if (
            title_contains is not None
            and title_contains.lower() not in str(document.get("title", "")).lower()
        ):
            raise SurfaceError(f"WebEngine loaded an unexpected document: {document}")
        if body_contains is not None and body_contains not in str(
            document.get("bodyText", "")
        ):
            raise SurfaceError(
                f"WebEngine document does not contain {body_contains!r}: {document}"
            )
        if int(document.get("width", 0)) < 800 or int(document.get("height", 0)) < 600:
            raise SurfaceError(f"WebEngine document viewport is too small: {document}")
        if int(document.get("bodyChildren", 0)) < 1:
            raise SurfaceError("WebEngine document body is empty")

        captured = _command(
            devtools,
            2,
            "Page.captureScreenshot",
            format="png",
            fromSurface=True,
            captureBeyondViewport=False,
        )
    encoded = captured.get("data")
    if not isinstance(encoded, str):
        raise SurfaceError("DevTools screenshot response contained no image")
    try:
        png = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise SurfaceError("DevTools screenshot was not valid base64") from error
    output.write_bytes(png)
    return document


def png_luminance(path: Path) -> dict[str, float | int]:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SurfaceError("DevTools screenshot is not a PNG")
    position = 8
    width = height = bit_depth = color_type = interlace = -1
    compressed = bytearray()
    while position < len(data):
        if position + 12 > len(data):
            raise SurfaceError("truncated PNG chunk")
        length = struct.unpack("!I", data[position : position + 4])[0]
        chunk_type = data[position + 4 : position + 8]
        chunk = data[position + 8 : position + 8 + length]
        crc = data[position + 8 + length : position + 12 + length]
        if len(chunk) != length or len(crc) != 4:
            raise SurfaceError("truncated PNG data")
        expected_crc = zlib.crc32(chunk_type + chunk) & 0xFFFFFFFF
        if struct.unpack("!I", crc)[0] != expected_crc:
            raise SurfaceError("PNG checksum mismatch")
        position += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                "!IIBBBBB", chunk
            )
        elif chunk_type == b"IDAT":
            compressed.extend(chunk)
        elif chunk_type == b"IEND":
            break
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if width < 1 or height < 1 or bit_depth != 8 or channels is None or interlace != 0:
        raise SurfaceError(
            f"unsupported PNG layout: {width}x{height}, depth={bit_depth}, type={color_type}"
        )
    stride = width * channels
    try:
        filtered = zlib.decompress(compressed)
    except zlib.error as error:
        raise SurfaceError("invalid compressed PNG pixels") from error
    if len(filtered) != height * (stride + 1):
        raise SurfaceError("PNG pixel payload has the wrong size")

    previous = bytearray(stride)
    luminance_sum = 0
    luminance_squared_sum = 0
    nonblack = 0
    offset = 0

    def paeth(left: int, above: int, upper_left: int) -> int:
        prediction = left + above - upper_left
        left_distance = abs(prediction - left)
        above_distance = abs(prediction - above)
        upper_left_distance = abs(prediction - upper_left)
        if left_distance <= above_distance and left_distance <= upper_left_distance:
            return left
        return above if above_distance <= upper_left_distance else upper_left

    for _ in range(height):
        filter_type = filtered[offset]
        offset += 1
        raw = filtered[offset : offset + stride]
        offset += stride
        row = bytearray(stride)
        for index, value in enumerate(raw):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                reconstructed = value
            elif filter_type == 1:
                reconstructed = value + left
            elif filter_type == 2:
                reconstructed = value + above
            elif filter_type == 3:
                reconstructed = value + ((left + above) // 2)
            elif filter_type == 4:
                reconstructed = value + paeth(left, above, upper_left)
            else:
                raise SurfaceError(f"unsupported PNG filter {filter_type}")
            row[index] = reconstructed & 0xFF
        for index in range(0, stride, channels):
            if color_type in {0, 4}:
                red = green = blue = row[index]
                alpha = row[index + 1] if color_type == 4 else 255
            else:
                red, green, blue = row[index : index + 3]
                alpha = row[index + 3] if color_type == 6 else 255
            luminance = ((54 * red + 183 * green + 19 * blue) >> 8) * alpha // 255
            luminance_sum += luminance
            luminance_squared_sum += luminance * luminance
            if luminance >= 4:
                nonblack += 1
        previous = row

    pixels = width * height
    mean = luminance_sum / (pixels * 255)
    variance = max(0.0, luminance_squared_sum / pixels - (luminance_sum / pixels) ** 2)
    return {
        "width": width,
        "height": height,
        "mean_luminance": mean,
        "luminance_stddev": math.sqrt(variance) / 255,
        "nonblack_fraction": nonblack / pixels,
    }


def validate_surface(stats: dict[str, float | int]) -> None:
    width = int(stats["width"])
    height = int(stats["height"])
    mean = float(stats["mean_luminance"])
    deviation = float(stats["luminance_stddev"])
    nonblack = float(stats["nonblack_fraction"])
    if width < 800 or height < 600:
        raise SurfaceError(
            f"WebEngine viewport is unexpectedly small: {width}x{height}"
        )
    if mean <= 0.005 or deviation <= 0.005 or nonblack <= 0.01:
        raise SurfaceError(f"WebEngine viewport is blank or uniform: {stats}")


def capture_validated_surface(
    endpoint: str,
    output: Path,
    timeout: float,
    *,
    retry_interval: float = 0.2,
    url_prefix: str | None = "http://127.0.0.1:",
    title_contains: str | None = "comfy",
    body_contains: str | None = None,
) -> tuple[dict[str, Any], dict[str, float | int]]:
    """Wait for asynchronous frontend startup to produce a usable viewport."""

    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SurfaceError(
                f"WebEngine viewport did not become usable within {timeout:g}s: "
                f"{last_error}"
            ) from last_error
        try:
            document = capture_surface(
                endpoint,
                output,
                remaining,
                url_prefix=url_prefix,
                title_contains=title_contains,
                body_contains=body_contains,
            )
            stats = png_luminance(output)
            validate_surface(stats)
            return document, stats
        except (OSError, SurfaceError, ValueError) as error:
            last_error = error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        time.sleep(min(retry_interval, remaining))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("endpoint", help="loopback Qt WebEngine DevTools HTTP endpoint")
    parser.add_argument("output", type=Path, help="destination PNG evidence")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--allow-local-page",
        action="store_true",
        help="allow the launcher's internal HTML page instead of requiring localhost",
    )
    parser.add_argument(
        "--body-contains",
        help="require this exact text in the rendered document body",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        document, stats = capture_validated_surface(
            args.endpoint,
            args.output,
            args.timeout,
            url_prefix=None if args.allow_local_page else "http://127.0.0.1:",
            title_contains=None if args.allow_local_page else "comfy",
            body_contains=args.body_contains,
        )
    except (OSError, SurfaceError, ValueError) as error:
        print(f"webengine surface verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"document": document, "surface": stats}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
