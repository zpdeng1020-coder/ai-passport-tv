"""Where this computer can be reached from the device, worked out not asked for.

The device needs exactly one string: the address of this server. Until now the
person running it had to supply that themselves, as `--bind`, which assumes they
already know their own address. Most do not, and the number changes whenever the
router hands out a new lease -- on the machine this was written on it moved three
times in one day. Being asked to type something you would have to look up is a
poor first step, and getting it wrong produces a device that connects to nothing
and cannot say why.

So the address is discovered here. Two forms are reported, and the difference
between them matters:

* The **name** (`some-mac.local`) is what the device should be given. It follows
  the machine when the router renumbers it, so it keeps working without anyone
  touching either end.
* The **number** (`192.168.1.20`) is what the socket must bind to. Binding needs
  a literal address; there is no way to listen on a name.

Both are printed, the name first, with the instruction the reader actually needs.

Nothing here is platform-neutral, which is why the platform is detected rather
than assumed. The name lookup differs on every system and none of the methods is
portable; each is tried, and a failure is reported as "name unknown" rather than
guessed at, because a wrong name is worse than no name -- it looks authoritative
and does not work.
"""

from __future__ import annotations

import platform
import socket
import subprocess

# A LAN address is needed, not a public one, and the interfaces are not
# enumerable with the standard library alone. Connecting a datagram socket to a
# distant address is the usual trick: no packet is sent, but the kernel picks the
# interface it would use, and the local end of that socket is the address to
# advertise. A reserved documentation address is used so that a stray packet --
# which does not happen, the socket is never written to -- would go nowhere.
_PROBE_ADDRESSES = (
    ("192.0.2.1", 9),        # RFC 5737 TEST-NET-1
    ("198.51.100.1", 9),     # RFC 5737 TEST-NET-2
    ("8.8.8.8", 53),         # last resort: an address that is always routed
)


def lan_address() -> str | None:
    """The IPv4 address a device on the same network would reach this machine at.

    None when there is no route out of the machine at all, which is what an
    offline computer looks like. The caller reports that rather than substituting
    a loopback address: 127.0.0.1 is reachable only from this machine, so a
    device configured with it would fail in a way that looks like a server fault.
    """
    for address, port in _PROBE_ADDRESSES:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.5)
            sock.connect((address, port))
            found = sock.getsockname()[0]
        except OSError:
            continue
        finally:
            sock.close()
        # A loopback answer means the probe reached nothing usable; keep looking
        # rather than handing back an address only this machine can use.
        if found and not found.startswith("127."):
            return found
    return None


def _run(command: list[str]) -> str | None:
    """First line of a command's output, or None. Never raises: this is a
    convenience lookup, and a missing or failing tool is an ordinary outcome."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    line = done.stdout.strip().splitlines()
    return line[0].strip() if line else None


def local_name() -> str | None:
    """The `.local` name of this machine, or None if it cannot be established.

    Each system keeps this in a different place:

    * macOS: `scutil` reports the Bonjour name directly, which is the name other
      devices on the network will resolve.
    * Linux/BSD: `avahi-resolve` asks the running mDNS responder for the host's
      own name. Without avahi there is no `.local` name to advertise.
    * Windows: the computer name from the environment, which Windows publishes
      over mDNS as `<name>.local`.

    A name is returned only with a `.local` suffix attached, or empty. A bare
    hostname is not useful to the device: it would resolve only if the device's
    network had a DNS entry for it, which a home network does not.
    """
    system = platform.system()

    if system == "Darwin":
        name = _run(["scutil", "--get", "LocalHostName"])
        return f"{name}.local" if name else None

    if system == "Windows":
        import os
        name = os.environ.get("COMPUTERNAME")
        return f"{name}.local" if name else None

    # Linux, BSD and anything else that runs an mDNS responder.
    name = _run(["avahi-resolve", "--address", "-n", socket.gethostname()])
    if name:
        return name.rstrip(".") + ".local" if not name.endswith(".local") else name
    return None


def describe(bind: str | None = None, port: int = 8096) -> list[str]:
    """The lines to print at start-up so the reader knows what to type.

    In Chinese, because that is who reads it: these lines exist to be carried to
    the device's setup page, and the project's own instructions are in Chinese.
    A reader who has just been told in Chinese which page to open should not
    then be handed the address in a different language.

    `bind` is the address the socket is actually listening on, when the caller
    has already chosen one; it is preferred over a fresh probe because it is the
    fact, not an estimate.
    """
    address = bind or lan_address()
    name = local_name()

    # The address being listened on is not printed. It was the first line here,
    # and it is the machine's own IP -- useful to whoever is running the server
    # and needed by nobody else: the device is given the name, not the number,
    # and the number appears below as the fallback when the name fails. As a
    # leading line it pushed the thing to copy down the output.
    lines: list[str] = []
    if not address:
        lines.append("没有找到网络地址。这台电脑连上网络了吗？")
        return lines

    # Nothing outside this machine can reach a loopback address, so the advice
    # below would be wrong for it: the name would resolve to the real interface,
    # which nothing is listening on, and the device would fail to connect while
    # the instructions looked correct. Whoever bound to loopback did so on
    # purpose -- a test, or a deliberate local-only run -- and is told what that
    # means rather than handed an address that cannot work.
    if address.startswith("127."):
        lines.append("这是一个本机地址（127 开头），只有这台电脑自己能访问，")
        lines.append("局域网里的设备连接不上。")
        return lines

    # One value to copy, and at most one line of explanation under it.
    #
    # This block used to spend four lines on the address and three more
    # explaining when to use which, which reads as a decision to make rather
    # than an instruction to follow. The reader is holding a phone and about to
    # type one string into a form; the fallback matters only if the first one
    # fails, so it is stated as the fallback in a single line.
    if name:
        lines.append("设备上要填的地址：")
        lines.append(f"    {name}:{port}")
        lines.append(f"（连不上就换成 {address}:{port}）")
    else:
        lines.append("设备上要填的地址：")
        lines.append(f"    {address}:{port}")
        lines.append("（读不到这台电脑的名字；路由器换 IP 后要重新填一次）")
    # A blank line after, so whatever the server prints next -- the courtesy
    # note about the network -- is visibly a separate remark rather than another
    # line of the instruction.
    lines.append("")
    return lines
