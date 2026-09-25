"""The media link over USB, presented as if it were the socket.

The device can take the same FAV1 stream down a cable instead of over WiFi, and
it is several times faster: the network path carries about 175 kB/s measured,
while the reference implementation on this same panel reaches 20 frames a second
at 320x180 over this same USB peripheral. The picture is the same picture; only
the pipe is different.

What this module is, and what it deliberately is not. It is an adapter that makes
a serial port answer to `send`, `recv`, `setblocking`, `fileno` and `close`, so
the sender above it -- which was written against a socket and is full of
carefully-reasoned timeouts -- does not learn that anything changed. It is not a
second implementation of the pacing, the framing or the protocol: those are the
parts that took months to get right on the WiFi path, and a copy of them would
be a copy that drifts.

Why an adapter rather than a socket. The device is on the other end of a USB
cable, which appears to this machine as a serial port and offers no IP address
at all. A socket cannot be opened to it. But the difference between the two on
the sending side is narrow: both are ordered byte streams with back-pressure,
both can be waited on with `select`, and both report "not ready" rather than
blocking when they are not ready. Those three properties are the whole of what
the sender uses.

One thing does not carry over, and it is stated plainly rather than papered
over: a socket tells you when the peer has gone, and a serial port does not.
Writing to a USB serial port whose device has unplugged does not fail; the bytes
are accepted by the driver and dropped. So `recv` cannot stand in for the
device's END packet, and the liveliness check has to come from somewhere else --
see `device_present`, which asks the operating system whether the port is still
there.
"""

from __future__ import annotations

import os
import time

# How long a single write may take before it is treated as the link refusing
# bytes. The sender applies its own frame-level deadlines as well; this is the
# backstop for the case where the driver itself will not complete a write.
WRITE_TIMEOUT_S = 3.0

# The USB serial port name differs by platform and by which USB socket the
# device is in, so the port is discovered rather than configured. These are the
# patterns macOS and Linux use for a USB CDC device.
PORT_PATTERNS = (
    "/dev/cu.usbmodem*",
    "/dev/tty.usbmodem*",
    "/dev/ttyACM*",
)


class SerialConnection:
    """A serial port wearing a socket's interface.

    Only the methods the sender actually calls are implemented, because a wider
    surface would be a wider thing to keep honest. Anything the sender starts
    using that is not here should fail loudly at the call rather than quietly
    do the wrong thing.
    """

    def __init__(self, port: str, baudrate: int = 921600, timeout: float = 0.0):
        import serial  # imported here so the module stays importable without it

        self.port = port
        # The baud rate is nominal on this peripheral: it is USB, not a UART,
        # so the device does not clock bytes against it and a wrong value
        # cannot corrupt the stream. It is set high anyway, because some
        # drivers on some platforms use it to size internal buffers and a low
        # value throttles the port for real.
        self._serial = serial.Serial(port, baudrate=baudrate, timeout=timeout,
                                     write_timeout=WRITE_TIMEOUT_S)
        self.closed = False

    # --- the socket surface the sender uses ---------------------------------

    def fileno(self) -> int:
        """The descriptor, so `select` can wait on this like any other stream.

        This is the load-bearing part of the whole adapter. The sender decides
        when it may write by selecting on the connection, and back-pressure --
        the device being slow, which is the normal state of affairs -- arrives
        as that select not reporting writable. Without a real descriptor there
        is no way to ask, and the sender would have to busy-wait instead.
        """
        return self._serial.fileno()

    def setblocking(self, blocking: bool) -> None:
        # The serial port is opened with a zero read timeout and a bounded
        # write timeout, which is the behaviour the sender expects from its
        # non-blocking socket: a read that has nothing returns nothing, a write
        # that cannot complete raises.
        self._serial.timeout = None if blocking else 0.0

    def recv(self, size: int) -> bytes:
        """Read what has arrived, up to `size`, without waiting.

        Empty means "nothing yet", which is what the sender's read loop already
        treats it as. It does NOT mean the peer has gone -- see the module note.
        """
        return self._serial.read(size)

    def send(self, data) -> int:
        """Write as much as the driver will take, and say how much that was."""
        written = self._serial.write(data)
        if written is None:
            # No timeout set on the port would make this possible; with one set
            # the driver raises instead, so this is a guard rather than a path.
            return 0
        return written

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self._serial.close()
            except Exception:
                # Closing a port whose device has already gone is not an error
                # worth reporting: there is nothing left to close.
                pass

    # --- what a serial port cannot do, said out loud ------------------------

    def shutdown(self, how) -> None:
        """Accepted and ignored.

        The sender shuts a socket down before closing it. There is no
        equivalent for a serial port and none is needed -- closing releases
        everything there is -- but the call has to exist, because the cleanup
        path is shared and a missing method there would turn an ordinary end of
        session into a crash.
        """

    def device_present(self) -> bool:
        """Whether the port the device was on still exists.

        This is the stand-in for the peer-closed signal a socket would give.
        Unplugging a USB device removes its special file, so the question "is
        the device still here" becomes "does this path still name something".
        It is a poll rather than an event, so it costs a stat and is meant to be
        called where the sender would otherwise have learned from a read.
        """
        return os.path.exists(self.port)


def find_port() -> str | None:
    """The first USB serial port that looks like the device.

    Discovery rather than configuration because the name moves: it depends on
    which socket the cable is in, and on macOS it can gain a suffix when a
    second device of the same kind is attached. Asking the filesystem is more
    reliable than asking the operator to keep a name up to date.
    """
    import glob

    for pattern in PORT_PATTERNS:
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


def wait_for_port(timeout: float = 30.0, poll: float = 0.5) -> str | None:
    """Wait for a device to appear, for the case where it boots after us.

    The usual order is that the device is already running and the computer is
    started afterwards, but the reverse happens -- plugging in a device that was
    off -- and a server that exited immediately would have to be started by hand
    for something it could simply have waited for.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        port = find_port()
        if port:
            return port
        time.sleep(poll)
    return None
