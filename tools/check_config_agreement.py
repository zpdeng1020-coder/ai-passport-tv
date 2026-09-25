"""Do the server and the firmware agree on CONFIG, without a device in the loop.

The device validates CONFIG field by field and ends the session on any mismatch.
What that looks like from outside is not a rejected configuration: it is a device
that connects, authenticates, and drops within a second having sent nothing --
which reads as a broken link, an unstable network, or a firmware fault, and is
none of them. It cost a full evening once, chasing the wrong thing.

The mismatch that prompted this was `stripe_rows`. The name meant the panel's
row count on one side and the picture's stripe height on the other, and nothing
forced the two to move together: changing the picture geometry updated the server
and left the device checking a constant that no longer described what was sent.

So this compares what the server will actually put in CONFIG against what the
firmware will actually accept -- both read from the real sources, a header parse
and the module that builds the packet, rather than a copy of either. A check that
rebuilds the values by hand agrees with itself and proves nothing.

    python3 tools/check_config_agreement.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import frames                      # noqa: E402
from server.media import AUDIO_CHUNK_MS        # noqa: E402
from server.tv_server import CONFIG            # noqa: E402

HEADER = ROOT / "main" / "av_protocol.h"


def firmware_constant(name: str) -> int:
    """A #define from the protocol header, as an integer."""
    match = re.search(rf"^#define {name}\s+(\d+)u?", HEADER.read_text(), re.M)
    if not match:
        raise SystemExit(f"{name} is not defined in {HEADER}")
    return int(match.group(1))


# Every field the device checks against a firmware constant. The names on the
# left are the JSON keys; the names on the right are what the firmware compares
# them to, and the two are deliberately written out rather than derived, because
# a wrong pairing here is exactly the fault this exists to catch.
CHECKED = [
    ("width", "AV_VIDEO_WIDTH"),
    ("height", "AV_VIDEO_HEIGHT"),
    ("stripe_rows", "AV_VIDEO_STRIPE_ROWS"),
    ("video_max_bytes", "AV_VIDEO_MAX"),
]

# Fields the device checks against a literal rather than a constant. Listed so
# that a change to one of them is at least visible here, since nothing else
# would connect the two files.
LITERAL = [
    ("fps", None, "checked as a range, 1..30"),
    ("sample_rate", 16000, ""),
    ("channels", 1, "audio channel count, not the channel list"),
    ("sample_bits", 16, ""),
    ("audio_chunk_ms", None, "checked against AV_AUDIO_MS"),
    ("start_delay_ms", 200, ""),
]


def main() -> int:
    # The firmware's geometry and the server's are chosen independently -- the
    # device compiles its own in, the server reads TV_GEOMETRY -- so the first
    # thing worth printing is whether they name the same pane.
    fw_w, fw_h = firmware_constant("AV_VIDEO_WIDTH"), firmware_constant("AV_VIDEO_HEIGHT")
    built = f"{fw_w}x{fw_h}"
    print(f"firmware is built for {built}; server will send "
          f"{frames.WIDTH}x{frames.HEIGHT}")
    if built != f"{frames.WIDTH}x{frames.HEIGHT}":
        print(f"  set TV_GEOMETRY={built} to match, or flash a firmware built for "
              f"{frames.WIDTH}x{frames.HEIGHT}")
    print()
    print(f"{'field':<18}{'server sends':>14}{'device accepts':>16}   verdict")
    failures = []
    for key, constant in CHECKED:
        sent, accepted = CONFIG[key], firmware_constant(constant)
        good = sent == accepted
        if not good:
            failures.append(f"{key}: server sends {sent}, firmware accepts {accepted}")
        print(f"{key:<18}{sent:>14}{accepted:>16}   {'ok' if good else 'MISMATCH'}")

    # These are checked, not merely printed.
    #
    # They used to be printed only -- `sent` displayed beside a note and never
    # compared -- so this script answered "Both ends agree; CONFIG will be
    # accepted" for a server sending fps=31, sample_rate=8000 or
    # audio_chunk_ms=20, every one of which the device refuses. An external
    # review found it by setting those three and reading the exit code, which
    # was 0 each time. **A check whose pass message is broader than what it
    # checks is worse than no check**: it is quoted as evidence.
    #
    # The device's own conditions are in `config_valid()` (main/av_player.c):
    # `json_between(j,"fps",1,30)`, `json_number(j,"sample_rate",16000)`,
    # `json_number(j,"channels",1)`, `json_number(j,"sample_bits",16)`,
    # `json_number(j,"audio_chunk_ms",AV_AUDIO_MS)`.
    for key, value, note in LITERAL:
        sent = CONFIG.get(key)
        if key == "audio_chunk_ms":
            # The device compares this against AV_AUDIO_MS, not a literal, and
            # the server derives it from media.AUDIO_CHUNK_MS. All three have to
            # be the same number, which is one more relation than a literal check.
            accepted = firmware_constant("AV_AUDIO_MS")
            good = sent == AUDIO_CHUNK_MS == accepted
            shown = f"AV_AUDIO_MS={accepted}"
            if not good:
                failures.append(
                    f"audio_chunk_ms: server sends {sent}, media.py uses "
                    f"{AUDIO_CHUNK_MS}, firmware accepts {accepted}")
        elif key == "fps":
            accepted = "1..30"
            good = isinstance(sent, int) and 1 <= sent <= 30
            shown = accepted
            if not good:
                failures.append(f"fps: server sends {sent}, firmware accepts {accepted}")
        else:
            accepted = value
            good = sent == accepted
            shown = str(accepted)
            if not good:
                failures.append(f"{key}: server sends {sent}, firmware accepts {accepted}")
        print(f"{key:<18}{str(sent):>14}{shown:>16}   {'ok' if good else 'MISMATCH'}")

    # The channel list travels under its own key, and the two keys colliding is
    # a fault that has been introduced twice. Checked here because it is the
    # same class of mistake as the one above.
    if CONFIG.get("channels") != 1:
        failures.append("channels is the audio channel count and must be 1")
    if "channel_list" in CONFIG:
        failures.append("channel_list must stay a separate key from channels")

    print()
    if failures:
        print("CONFIG will be rejected; every session will drop on connect:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("Both ends agree; CONFIG will be accepted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
