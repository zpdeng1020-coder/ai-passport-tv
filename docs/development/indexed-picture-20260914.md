<p align="right">
  <a href="indexed-picture-20260914.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Rebuilding the picture path (2026-09-14)

This is the working log for replacing "a 160x120 JPEG magnified twice on the
device" with "a full 320x240 palettised frame". The conclusions are in the
CHANGELOG; what follows is the part that belongs here instead -- what was not
known before starting and only became clear by measuring. Anyone touching this
path next needs exactly these things.

## Why

The panel is 240x320. The released picture was a 160x120 JPEG scaled up by
nearest-neighbour on the device, so three quarters of the pixels on screen were
copies of their neighbours. The user's request was one sentence: the picture is
neither smooth nor clear; rebuild it borrowing from the community, and improve
both.

The community project L33Z22L11/pocket-tv takes the other road: a whole
palettised frame, compressed with zlib. JPEG decoding has to build Huffman
tables and run an inverse transform; inflate does table lookups and copies. The
cost per pixel is far lower, so clarity and frame rate can improve together
rather than trading off. This project now takes that road.

## Measurements taken before writing code

These numbers decided the shape of the design. All measured, none estimated:

| Item | Value | How |
| --- | --- | --- |
| One indexed frame | 76800 bytes | 320x240, one byte a pixel |
| Sixteen-row stripes vs one stream | only 2.2% larger | four channels, 10 s each |
| Worst frame over 30 s | 29275 bytes | 10 s of sampling only saw 23167 |
| Adaptive palette RMSE | 10.1 (fixed 3-3-2: 28.2) | same frames, compared |
| `-sws_dither none` | worst frame 31 KB to 23 KB | dithering is noise; it will not compress |
| `tinfl_decompress` | in the C3 ROM at `0x400000f4` | `esp32c3.rom.ld:81` |
| SPI floor per frame | 153600 / 5 MB/s, about 30.7 ms | arithmetic |

The 29275-byte worst case is what ruled out one packet per frame, and produced
the format instead: three stripes a packet, five packets a frame, and a flag
bit meaning "this packet continues the previous frame".

## Four things found only after starting

The first two are ffmpeg's behaviour. The last two were our own mistakes.

### 1. `-pix_fmt rgb8` destroys the adaptive palette

`rgb8` reads like "8-bit colour" and is not. It is ffmpeg's own fixed 3-3-2
grid, so asking for it re-quantises every pixel onto that grid and **throws the
chosen palette away entirely**. The device then looks up indices in a table they
were never chosen from.

This one is dangerous because it does not fail: the picture still appears, the
adaptive palette simply does nothing -- the 3-3-2 grid with extra steps. It was
caught by measuring the bars on a white test card: `rgb8` emitted three shades
of near-black into the first row past the picture, where `pal8` on the same
signal was correctly black.

The fix is `pal8`, which passes indices through untouched. That was then nailed
down rather than assumed: both formats were emitted from one ffmpeg run, the
indices were replayed through the palette into colours, and the result compared
against the rgb24 output pixel by pixel -- 307200 pixels, zero mismatches.

### 2. `pal8` carries 1024 bytes of palette per frame, *after* the pixels

The rawvideo muxer writes its palette once per frame. Measured stride is
77824 = 76800 indices + 1024 palette: **pixels first, palette second**, not the
other way round. Those 1024 bytes have to be read and dropped -- a pipe cannot be
seeked past them. Get the stride wrong and the next frame starts 1024 bytes
early, shearing the whole stream, which shows up as slow tearing rather than as
an error.

### 3. The palette sampler and the picture mapper used different geometry

`palette_command()` sampled with `fps,FIT`, while `import_video()` mapped the
picture with `scale=iw*sar:ih,setsar=1,FIT`. For an **anamorphic source** that
meant a palette sampled full-frame applied to a letterboxed picture: the palette
contained no black at all. The bars were then mapped to the nearest colour
available, which was white, and the whole panel looked lit.

Both now derive from one string, so the two cannot disagree. This is the one
place in the change where the same mistake could return, which is why it is
prevented by construction rather than by a comment.

### 4. Whether a departing device counted as a fault depended on which code path noticed

`_device_left()` treated "connection closed without END" as a normal departure
and returned True. That raced the exception path in `_wait_until()`: a device
that had authenticated and then vanished could be counted as a completed session
or as a fault, **depending on which path saw the FIN first**.

Measured, that was wrong about one run in ten. That number is the only statistic
the operator is shown at exit, so being right nine times out of ten is being
wrong. The intended boundary is the **handshake**, not the FIN: a break before
authentication is a viewer who changed their mind, and a break after it is a real
fault. The fix lets the exception through to the `session_id != 0` branch.

## Two smaller faults fixed along the way

- `tools/install-actionlint.sh` tested for a checksum tool with
  `command -v sha256sum`. macOS ships a `/sbin/sha256sum` that exists, is found,
  and rejects every option it is given -- `--check` prints its usage and stops --
  so the script failed on every macOS machine. `shasum` is now preferred.
- `import_video()` closed only stdout and left the stderr pipe open: one
  descriptor per import, which on a long-running server is one per clip.

## What has not been verified

**Decode time and memory use on the real device have not been measured.** The
code compiles, the host tests pass and the firmware builds, but tinfl's actual
throughput on a 160 MHz RISC-V is inferred from miniz's usual figures rather than
observed. The only hard evidence of whether the picture is smoother is the
measured value in the serial log after flashing. Until that number exists, any
claim about smoothness is an intention, not a result.

The same goes for how the palette looks. RMSE 10.1 is a host-side number about
quantisation; whether it looks good on a 240x320 panel needs eyes on it.

## Where this stands

```
Build:         PASS (static gate green; firmware builds, app 1527312 / 3145728 bytes)
Host tests:    PASS (11 Python suites, 8 C host tests)
Device tests:  NOT RUN (decode time and memory need a flashed device)
Unverified:    on-device decode time, memory use, how the palette actually looks
```
