"""Bounded FAV1 framing; a bad header terminates the connection, never resyncs."""

from __future__ import annotations

import json
import select
import socket
import struct
import time
from dataclasses import dataclass
from enum import IntEnum

HEADER = struct.Struct("!4sBBHIIII")
MAGIC = b"FAV1"
VERSION = 1
# Control payload ceiling, matching main/av_protocol.h TV_CONTROL_MAX. The
# channel list travels in one CONFIG packet: a few hundred channels is about
# 12 KB of JSON, which the previous 1024-byte ceiling rejected outright.
CONTROL_MAX = 24 * 1024
# Must equal AV_VIDEO_MAX in main/av_protocol.h and VIDEO_MAX in frames.py. A
# video packet carries whole stripes, so the worst it can be is three stripes
# of incompressible data -- 15399 bytes -- and the picture format module is
# where that number is derived. Kept as an import rather than a second literal
# so the two cannot drift apart.
from .frames import VIDEO_MAX
AUDIO_BYTES = 1280
UINT32_MAX = 0xFFFFFFFF
IO_TIMEOUT = 0.25


class Kind(IntEnum):
    HELLO = 1
    CONFIG = 2
    PCM = 3
    # The picture, one indexed frame cut into stripes. Still called JPEG here
    # because the device's own name for the slot is AV_VIDEO and the numbers
    # have to agree; the payload is no longer a JPEG.
    JPEG = 4
    END = 5
    ERROR = 6
    # The 256 colours the indices refer to, sent once before the first frame of
    # a channel because the palette is chosen per channel.
    PALETTE = 7

# The only flag bit in use, and only on video: this packet carries more stripes
# of the frame the previous packet started. A frame cut across several packets
# sends them all under one timestamp, and the timestamp must otherwise advance,
# so a continuation has to say so or it is indistinguishable from a repeat.
VIDEO_CONTINUES = 0x01


class ProtocolError(ValueError):
    """Invalid peer input (messages must never contain peer payloads)."""


@dataclass(frozen=True)
class Packet:
    kind: Kind
    session: int
    seq: int
    pts_ms: int
    payload: bytes = b""
    # Only video uses this, and only VIDEO_CONTINUES. Kept a plain integer so a
    # caller passing a stray bit is caught by the range check rather than
    # silently reaching the wire.
    flags: int = 0

    def encode(self) -> bytes:
        validate_length(self.kind, len(self.payload))
        for value in (self.session, self.seq, self.pts_ms):
            if type(value) is not int or not 0 <= value <= UINT32_MAX:
                raise ProtocolError("integer out of range")
        if type(self.flags) is not int or not 0 <= self.flags <= 0xFF:
            raise ProtocolError("flags out of range")
        if self.flags and not (self.kind == Kind.JPEG and self.flags == VIDEO_CONTINUES):
            raise ProtocolError("flags are only for a continued video frame")
        return HEADER.pack(MAGIC, VERSION, self.kind, self.flags, self.session,
                           self.seq, self.pts_ms, len(self.payload)) + self.payload


def validate_length(kind: Kind, size: int) -> None:
    if kind in (Kind.HELLO, Kind.CONFIG, Kind.ERROR):
        valid = 0 < size <= CONTROL_MAX
    elif kind == Kind.PCM:
        valid = size == AUDIO_BYTES
    elif kind in (Kind.JPEG, Kind.PALETTE):
        valid = 0 < size <= VIDEO_MAX
    elif kind == Kind.END:
        valid = size == 0
    else:
        valid = False
    if not valid:
        raise ProtocolError("invalid payload length or type")


def json_bytes(value: dict) -> bytes:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    validate_length(Kind.CONFIG, len(raw))
    return raw


def json_object(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_):
        raise ProtocolError("nonstandard JSON constant")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ProtocolError("invalid control JSON") from None
    if not isinstance(value, dict):
        raise ProtocolError("control JSON must be an object")
    return value


def _wait(sock: socket.socket, deadline: float, writable: bool = False) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("packet deadline expired")
    ready = select.select([] if writable else [sock], [sock] if writable else [],
                          [], remaining)
    if not (ready[1] if writable else ready[0]):
        raise TimeoutError("packet deadline expired")


def _read_exact(sock: socket.socket, size: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < size:
        _wait(sock, deadline)
        try:
            chunk = sock.recv(size - len(data))
        except BlockingIOError:
            continue
        if not chunk:
            raise EOFError("peer disconnected")
        data.extend(chunk)
    return bytes(data)


def receive_packet(sock: socket.socket, timeout: float = IO_TIMEOUT,
                   expected_session: int | None = None) -> Packet:
    """One total deadline includes header and payload; validate before allocation."""
    deadline = time.monotonic() + timeout
    raw = _read_exact(sock, HEADER.size, deadline)
    magic, version, kind, flags, session, seq, pts, length = HEADER.unpack(raw)
    if magic != MAGIC or version != VERSION:
        raise ProtocolError("unsupported packet header")
    try:
        kind = Kind(kind)
    except ValueError:
        raise ProtocolError("unknown packet type") from None
    # Only the one flag exists, and only on video, exactly as the device
    # enforces it. Anything else reaching the wire is a bug on the sending
    # side, and it is better caught here than drawn as a torn picture.
    if flags and not (kind == Kind.JPEG and flags == VIDEO_CONTINUES):
        raise ProtocolError("unsupported packet flags")
    validate_length(kind, length)
    if expected_session is not None and session != expected_session:
        raise ProtocolError("session mismatch")
    return Packet(kind, session, seq, pts, _read_exact(sock, length, deadline),
                  flags)


def send_packet(sock: socket.socket, packet: Packet,
                timeout: float = IO_TIMEOUT, deadline: float | None = None) -> None:
    """No application send queue; at most one bounded packet, one total deadline.

    The server owns a nonblocking socket. A partial write followed by timeout is
    fatal: do not append an ERROR into the unfinished packet.

    `deadline` is for a packet that is one of several making up a single thing.
    A frame is five packets, and giving each of them its own full timeout would
    let a frame hold the sender for five times as long -- during which no audio
    goes out and the device's 400 ms buffer runs dry. A shared deadline bounds
    the whole frame instead of each packet in it.
    """
    data = memoryview(packet.encode())
    if deadline is None:
        deadline = time.monotonic() + timeout
    while data:
        _wait(sock, deadline, writable=True)
        try:
            sent = sock.send(data)
        except BlockingIOError:
            continue
        if not sent:
            raise EOFError("peer disconnected")
        data = data[sent:]


def _write_slice(sock: socket.socket, data: memoryview, deadline: float) -> bool:
    """Write one already-encoded slice of a packet, or report the deadline passed.

    The caller encodes the packet once and hands over successive pieces of it,
    which is the only way for a single loop carrying both media to serve the
    sound while a picture packet is still going out. `send_packet` cannot be
    used for that: it takes the whole packet and returns only when all of it is
    written, which for a picture packet is a quarter of a second during which
    nothing else in the process happens.

    Returns False rather than raising when the deadline has passed, because a
    slice is a fraction of a packet and the caller is the one that knows what
    the whole of it was worth -- the same division send_packet makes with its
    `deadline` argument.
    """
    remaining = memoryview(data)
    while remaining:
        try:
            _wait(sock, deadline, writable=True)
        except TimeoutError:
            return False
        try:
            sent = sock.send(remaining)
        except BlockingIOError:
            continue
        if not sent:
            raise EOFError("peer disconnected")
        remaining = remaining[sent:]
    return True
