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
# Control payload ceiling, matching main/av_protocol.h AV_CONTROL_MAX. The
# channel list travels in one CONFIG packet: a few hundred channels is about
# 12 KB of JSON, which the previous 1024-byte ceiling rejected outright.
CONTROL_MAX = 24 * 1024
VIDEO_MAX = 24 * 1024
AUDIO_BYTES = 640
UINT32_MAX = 0xFFFFFFFF
IO_TIMEOUT = 0.25


class Kind(IntEnum):
    HELLO = 1
    CONFIG = 2
    PCM = 3
    JPEG = 4
    END = 5
    ERROR = 6


class ProtocolError(ValueError):
    """Invalid peer input (messages must never contain peer payloads)."""


@dataclass(frozen=True)
class Packet:
    kind: Kind
    session: int
    seq: int
    pts_ms: int
    payload: bytes = b""

    def encode(self) -> bytes:
        validate_length(self.kind, len(self.payload))
        for value in (self.session, self.seq, self.pts_ms):
            if type(value) is not int or not 0 <= value <= UINT32_MAX:
                raise ProtocolError("integer out of range")
        return HEADER.pack(MAGIC, VERSION, self.kind, 0, self.session,
                           self.seq, self.pts_ms, len(self.payload)) + self.payload


def validate_length(kind: Kind, size: int) -> None:
    if kind in (Kind.HELLO, Kind.CONFIG, Kind.ERROR):
        valid = 0 < size <= CONTROL_MAX
    elif kind == Kind.PCM:
        valid = size == AUDIO_BYTES
    elif kind == Kind.JPEG:
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
    if magic != MAGIC or version != VERSION or flags != 0:
        raise ProtocolError("unsupported packet header")
    try:
        kind = Kind(kind)
    except ValueError:
        raise ProtocolError("unknown packet type") from None
    validate_length(kind, length)
    if expected_session is not None and session != expected_session:
        raise ProtocolError("session mismatch")
    return Packet(kind, session, seq, pts, _read_exact(sock, length, deadline))


def send_packet(sock: socket.socket, packet: Packet,
                timeout: float = IO_TIMEOUT) -> None:
    """No application send queue; at most one bounded packet, one total deadline.

    The server owns a nonblocking socket. A partial write followed by timeout is
    fatal: do not append an ERROR into the unfinished packet.
    """
    data = memoryview(packet.encode())
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
