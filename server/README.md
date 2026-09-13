<p align="right">
  <a href="README.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Local audio/video prototype server

This is an opt-in, single-device **LAN player for live channels, video files and
synthetic media**. It is not an Internet proxy or a production authentication
service. Python 3.11+ and its standard library serve the media; ffmpeg is needed
to prepare files and to transcode the `live` subcommand, so that host needs it
installed. No pip dependencies, device access or global configuration changes
are required. Start commands below from the repository root.

Channel addresses live in a fixed allowlist in `server/live.py`. They come from a
community IPTV playlist, so their availability and rights status are not verified;
they are intended for a private LAN test only. The device picks a channel by name
in its handshake, and the server falls back to `DEFAULT_CHANNEL` for anything it
does not recognise.

## Import a video file

```sh
python3 -m server.tv_server import-video --input /path/to/video.mp4 \
  --media-dir server/.local/my-video --seconds 60 --start 0
```

This accepts a local file, not a remote URL. Choose 1..600 seconds; the output keeps
display aspect ratio (including sample aspect ratio), fitting 16:12 (4:3) with
centered black borders at 160x120/12 FPS, plus 16 kHz mono PCM. No crop or stretch;
even YUV420 rounding may make opposite borders differ by up to two pixels. Silent
sources receive a silence track. JPEG payloads remain bounded at 24 KiB. Imported
media is read from disk per packet, never loaded as a complete movie into RAM.
Use the resulting directory with the same `run --media-dir` command below.

## Play a live channel

The shortest path, which starts everything and prints the address to type on the
device:

```sh
./run.sh                 # macOS and Linux
run.bat                  # Windows
```

It starts the media server and the channel page together, and restarts the media
server when the channel list is saved. Needs Python 3.9+ and ffmpeg; if either is
missing it says so and how to install it, rather than failing obscurely.

> **Windows is untested.** `run.bat` and the Windows branches in
> `tools/launch.py` are written from documented command behaviour and have never
> been run on a Windows machine -- the project has only been developed on macOS.
> Treat them as untried.

The individual commands, when you want to run one without the other:

```sh
python3 -m server.tv_server live --channel ch000      # media server, port 8096
python3 tools/channel_config.py                       # channel page, port 8097
```

`--bind` may be omitted, and usually should be: the server works out the
network address itself and prints both it and this computer's `.local` name. The
name is the better one to enter on the device, because it survives the router
handing out a different address. Supplying `--bind` by hand is still supported
for a machine with several interfaces.

```sh
python3 -m server.tv_server live --channel ch000 \
  --bind 192.168.1.20 --port 8096 --token-file <owner-only file>
```

Each device connection starts its own ffmpeg process and its own time origin, so
switching channels is just the device reconnecting with a different name. A
failed transcode ends that one session and the service keeps listening.

The channel table is read once, at start-up. `run.sh` and `run.bat` watch
`channels.txt` and restart the server when it changes; running the command above
directly means restarting it yourself to pick up an edit.

Old 320x240 JPEGs/manifests must be regenerated in a new directory. The device
requires 160x120 input and uses x2 nearest-neighbor output to 320x240; this does
not establish any device FPS improvement. Protocol timing and PCM are unchanged.

## Prepare once, copy, then run

```sh
python3 -m server.tv_server prepare --media-dir server/.local/media
# Optional: --ffmpeg /absolute/path/to/ffmpeg
```

The destination must not already exist. It contains a portable `manifest.json`,
`audio.s16le` (320,000 bytes) and `frame-000.jpg` through `frame-119.jpg`. Move/copy
the whole directory unchanged to the runtime host. Generated media and local
configuration under `server/.local/` are ignored by Git. A preparation failure
must be investigated; use a new destination for a retry, not a partly generated
set. `run` validates every frame before binding a socket.

The ten-second source has a three-digit frame counter (000–119), moving box,
one-frame white flash at each integer second, and a matching 50 ms, 1 kHz beep.
The beep amplitude is 0.08 (-22 dBFS); start with low device volume. PCM and video
both start at t=0. RGB frames are generated using the standard library (no font
package), then ffmpeg encodes baseline 160×120 JPEG 4:2:0 and 16 kHz mono s16le.
Oversized JPEGs retry at lower quality (q=10,18,25,31 after q=5); still-oversized
frames fail preparation. Frames are **never truncated**. This simple pattern
is not a worst-case JPEG decode/throughput benchmark.

Create the pairing file without placing its value in an argument or shell log:

```sh
python3 - <<'PY'
import os
import secrets
from pathlib import Path
folder = Path('server/.local')
folder.mkdir(mode=0o700, parents=True, exist_ok=True)
fd = os.open(folder / 'token', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(secrets.token_hex(32) + '\n')
PY
python3 -m server.tv_server run --media-dir server/.local/media \
  --bind 127.0.0.1 --port 8096 --token-file server/.local/token \
  --duration-seconds 1800
```

Provision the same token to the device through the separately managed private
configuration. Do not print or commit it. Alternatively, supply `TV_PAIRING_TOKEN`
through a secret-aware environment launcher; do not also use `--token-file`.
Tokens are 16–128 printable ASCII characters; use random tokens, not passwords.
Token files must be regular, owned by the process user, exactly mode 0400 or 0600,
and not symlinks. Run under an unprivileged account. Token comparison uses
`hmac.compare_digest`; authentication failures disclose no token or input JSON.

For the device, replace `127.0.0.1` with an **explicit local RFC1918 IPv4 address**
assigned to the server. Wildcard/public binds, hostnames, IPv6 and public peers
are rejected; port defaults to 8096. There is no TLS: use a trusted isolated LAN,
restrict access with the host's existing firewall policy, and never port-forward
this service. RFC1918 filtering alone does not protect against forwarded traffic.
The server accepts one authenticated stream and immediately closes extra clients
while streaming. A slow first HELLO has a 250 ms total deadline.

## Wire contract

TCP packets use the 24-byte network-order `!4sBBHIIII` header:

| Field | Value |
| --- | --- |
| magic, version, flags | `FAV1`, 1, 0 |
| type | HELLO=1, CONFIG=2, PCM=3, JPEG=4, END=5, ERROR=6 |
| session | uint32; initial HELLO=0, server chooses a fresh nonzero value |
| seq | uint32; HELLO=0, CONFIG=0, then one global increasing server sequence |
| pts_ms | uint32 presentation timestamp on the shared media timeline |
| payload_length | uint32, validated before reading a payload |

The client first sends HELLO (session=seq=pts=0):

```json
{"version":1,"token":"<private pairing token>","channel":"cctv1"}
```

`channel` is optional and names the channel to play; omitting it, or naming an
unknown channel, selects the server default.

CONFIG contains `width=160`, `height=120`, `fps=12`, `sample_rate=16000`,
`channels=1`, `sample_bits=16`, `audio_chunk_ms=20`, `video_max_bytes=24576`,
`session`, and advisory `duration_ms=10000`, `start_delay_ms=200`,
`audio_lead_ms=200`, `video_lead_ms=50`. The live path also sends `channel` and a
`channel_list` array of `{id,name}` entries for the device to step through.
Control JSON is at most 1024 bytes.

`channels` is the audio channel count and must remain a number; `channel_list` is
a separate key. Do not merge them: sending the list as `channels` fails the
device's CONFIG validation and ends every session immediately.
PCM is exactly 640 bytes (320 samples, little endian) every 20 ms. JPEG payloads
are at most 24 KiB. END has no payload. The client may send END with the active
session or close the connection to stop; no inbound media/control commands are
allowed after HELLO. Invalid versions, flags, types, lengths, sessions or JSON
terminate the connection without searching for another magic. ERROR is reserved
and framing-supported; this server closes on errors rather than risking an ERROR
appended after a partially sent packet.

## Scheduling, bounds and shutdown

- Server time origin is CONFIG write completion plus 200 ms. Audio is scheduled
  200 ms before its PTS; video 50 ms before its PTS. Network arrival and actual
  device playback timing are not guaranteed by send scheduling.
- **Wire PTS is not globally sorted across types.** Events merge by send deadline,
  preserving strictly increasing PTS within PCM and within JPEG, and globally
  increasing sequence numbers. Sorting both types by PTS would put video in front
  of audio that needs earlier prefetch, causing head-of-line delay. For example,
  audio PTS=140 precedes video PTS=0. The receiver must track PTS per stream.
- At the ten-second loop boundary indices wrap, but PTS continues to 10000 and
  beyond in the same session. Integer timestamps avoid cumulative rounding drift:
  video PTS=`frame_index*1000//12`. Default run duration is 1800 seconds (180 loops),
  configurable from 1 to 86400 seconds. Reconnect creates a new session and origin.
- There is no application send queue and no real-time transcoding. Memory holds
  a fixed ten-second set (at most about 3.3 MiB), plus one bounded outgoing packet.
  Requested kernel send buffer is 32768 bytes; OS rounding/doubling is possible.
- Nonblocking socket reads/writes use **250 ms total packet deadlines**, not a
  fresh timeout for every fragment. Busy sockets and cancellation are polled at
  most every 50 ms between operations. A timed-out partial send closes immediately.
- JPEG more than 83 ms past presentation is dropped before sending. Audio more
  than 100 ms past presentation closes the session instead of catching up an
  unbounded stale backlog. Reconnect/rebuffer is the receiver's responsibility.
- SIGINT/SIGTERM stops the local server without background workers; otherwise a
  finite stream emits END at the end of the timeline and the listener remains
  ready for another client. User-space cancellation is normally within 300 ms;
  scheduler/OS suspension is not a hard real-time guarantee.
- Logs include authenticated session IDs, sent PCM/JPEG counts every ten seconds,
  dropped video totals and close/failure summaries. They never include credentials
  or received payloads. These are **socket submission counts**, not DAC/DMA,
  acoustic playback, or audiovisual synchronization acceptance measurements.

## Host validation

```sh
python3 tests/test_tv_server.py -v
python3 tests/test_video_import.py -v
```

Tests cover exact header bytes, fragmentation/coalescing, EOF, no magic resync,
unknown fields, payload and uint32 limits, slow receive/send deadlines, token
sources/permissions/authentication, session mismatch, busy-client rejection,
stream termination, JPEG metadata, and the full 30-minute **simulated** schedule
(90,000 PCM packets and 21,600 JPEG frames). When ffmpeg is on PATH, the test also
prepares and reloads ten seconds and checks all ten audio marker windows; without
ffmpeg that preparation test is explicitly skipped. A metadata fixture used by
framing tests is not an image-decoder test.

A 30-minute wall-clock device run, DMA/audio timing, RF performance and JPEG ROM
decoding are separate acceptance work. No host test result implies those passed.
See [repository test guidance](../docs/development/engineering/build-and-test.md).

## Scope notes

The required unequal prefetch leads are retained; only the interpretation of
cross-type PTS ordering is made explicit. Closing rather than replying ERROR on
invalid input avoids framing ambiguity. Extra CONFIG fields are advisory.
Documentation is kept beside this server because this implementation's edit
scope is `server/` and `tests/test_tv_server.py`; documentation-index/changelog
integration belongs to the coordinating change. No firmware or deployment
configuration is changed by this module.
