<p align="right">
  <a href="config-snapshot-20260915.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Configuration snapshot, 2026-09-15, before adaptive frame rate

Taken so the adaptive-frame-rate work can be undone without guessing. Every
value here is what was in force when the snapshot was made, and each has the
file and line it lives in.

## How to go back

The working tree is dirty -- the whole picture-path rewrite is uncommitted --
so `git checkout` would not take you back to this state. It would take you back
to the last commit, which is a different program.

To restore, either of these works:

```bash
# The whole tree, including the picture-path rewrite:
cp -a ~/Desktop/LLMtopic/ai-passport-tv-server-snapshot-20260915/. \
      ~/Desktop/LLMtopic/ai-passport-tv/

# Or just the two files the adaptive work will touch:
cp ~/Desktop/LLMtopic/ai-passport-tv-server-snapshot-20260915/server/{media,live,tv_server,frames}.py \
   ~/Desktop/LLMtopic/ai-passport-tv/server/
```

A copy of the four server files at this state is at
`~/Desktop/LLMtopic/ai-passport-tv-server-snapshot-20260915/`.

## The settings that matter

| Setting | Value | Where | What it does |
| --- | --- | --- | --- |
| `TV_FPS` | **10** (the default in the file) | `server/media.py:38` | How many picture frames a second the server aims for |
| `TV_PREBUFFER_S` | **8** | `server/live.py:154` | Seconds of playback held ready before the first packet |
| `TV_STALL_S` | not set | -- | Removed; the stall timeout is a constant again |
| `AUDIO_LEAD_MS` | 200 | `server/media.py:47` | How far the sound leads the wall clock |
| `AUDIO_MAX_LOOKAHEAD_MS` | 160 | `server/live.py:109` | Cap on how much further it may run ahead |
| `PCM_QUEUE_CHUNKS` | 3000 (60 s) | `server/live.py:118` | Audio queue capacity |
| `VIDEO_QUEUE_FRAMES` | 180 (15 s) | `server/live.py:124` | Picture queue capacity |
| `PREBUFFER_CHUNKS` | derived: 8 s | `server/live.py:155` | Sound half of the reserve |
| `PREBUFFER_FRAMES` | derived: 8 s | `server/live.py:156` | Picture half of the reserve |
| `PREBUFFER_TIMEOUT_S` | 60 | `server/live.py:161` | Backstop for a source that never produces |
| `PACKET_TARGET_BYTES` | 12288 | `server/frames.py:87` | Byte budget per picture packet |
| `VIDEO_MAX` | 12288 | `server/frames.py:82` | Hard packet ceiling; must equal `AV_VIDEO_MAX` in `main/av_protocol.h` |
| `MIN_LINK_BYTES_PER_SEC` | 32768 | `server/tv_server.py:71` | Pessimistic rate used to size a frame's deadline |
| `VIDEO_SLICE_BYTES` | 4096 | `server/tv_server.py:85` | How much of a packet is written before the sound gets a turn |

## The device

Firmware is the `sdkconfig.av-prototype` build flashed on 2026-09-15, with the
receiver task at priority 6 and the video task at 5. It is at 192.168.0.117.

The firmware does **not** need reflashing for any of this work. Frame rate,
reserve depth and packet sizes are all server-side; the device accepts whatever
`TV_FPS` announces as long as it is between 1 and 30.

## Why 10 was wrong

The link carries about 227 kB/s, measured. The sound is fixed by the protocol
at 50 packets of 640 bytes, so 32 kB/s. That leaves about 195 kB/s for the
picture, and a live 320x240 frame measures 21 kB at the median and 28 kB at its
worst. Ten frames a second is 210 kB/s, which is over before the worst frame is
counted. Eight fits with 14% to spare; nine fits the median with 3%, and those
three percent are gone the moment a channel shows something busy.
