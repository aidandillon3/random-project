"""Minimal OSC 1.0 message encoder over UDP (no extra dependency, no allocations to speak of)."""

from __future__ import annotations

import socket
import struct


def _pad(b: bytes) -> bytes:
    return b + b"\0" * (4 - len(b) % 4)


def _string(s: str) -> bytes:
    # OSC strings are null-terminated and padded to a multiple of 4 bytes
    return _pad(s.encode("utf-8"))


def encode_message(address: str, *args) -> bytes:
    tags = ","
    payload = b""
    for a in args:
        if isinstance(a, bool):
            tags += "T" if a else "F"
        elif isinstance(a, int):
            tags += "i"
            payload += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"
            payload += struct.pack(">f", a)
        elif isinstance(a, str):
            tags += "s"
            payload += _string(a)
        else:
            # numpy scalars and the like
            tags += "f"
            payload += struct.pack(">f", float(a))
    return _string(address) + _string(tags) + payload


def encode_bundle(messages: list[bytes]) -> bytes:
    out = _string("#bundle") + struct.pack(">Q", 1)  # timetag 1 = "immediately"
    for m in messages:
        out += struct.pack(">i", len(m)) + m
    return out


class OscSender:
    def __init__(self, host: str, port: int):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, address: str, *args) -> None:
        self.sock.sendto(encode_message(address, *args), self.addr)

    def send_raw(self, packet: bytes) -> None:
        self.sock.sendto(packet, self.addr)

    def close(self) -> None:
        self.sock.close()
