<p align="right">
  <a href="device-display-baseline-20260916.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# The device's own display performance: isolating it layer by layer

This document answers one question. **How long does this device take to draw a
picture on its own, where is that ceiling, and why has it not been measured
yet.**

---

## 1. What is already settled

### One hard floor

The panel is 320×240, two bytes a pixel, over four-wire SPI at 40 MHz. A full
screen is 153600 bytes, which at 5 MB/s takes **30.7 ms**. No software beats
that. A 16-row stripe of it is 2.048 ms.

### A contradiction inside one function

`push_stripe()` (`main/av_player.c:1731`) waits for the DMA engine after every
submit:

- the `bsp_display_raw_wait(200)` at the end of the function accumulates into
  `panel_wait_us`
- but `bsp_display_raw_submit()` itself (`components/bsp/src/bsp_display.c:174`)
  already calls `bsp_display_raw_wait(timeout_ms)` as its first statement

Two waits are stacked. The one inside submit returns immediately, because the
previous transfer was waited out by the previous call, and the trailing wait is
where the blocking actually happens. **The net effect is strictly serial per
stripe: build a stripe, start its transfer, wait for it to finish, build the
next.** Fifteen stripes a frame, of which 30.7 ms is the SPI engine running with
the processor idle beside it.

This is not an inference. The code records a measurement
(`main/av_player.c:227-236`): with one stripe buffer, `bsp_display_raw_wait`
took **37 ms** out of a 63.5 ms frame. With two buffers it fell to **2.2 ms**.
That is consistent with the 30.7 ms SPI floor plus per-call overhead.

### But that two-buffer experiment proved nothing

With two buffers the frame rate did not move: 10.5 frames a second against 10.4.
The conclusion drawn at the time was that the processor was not idle during the
wait, because other tasks were running, so the wait was irrelevant. Two buffers
were reverted.

**That conclusion does not hold.** Whether the processor is idle during the wait
and why the frame rate sat at 10.5 are two different questions. Cutting a 37 ms
block to 2.2 ms without moving the frame rate admits only one explanation:
**whatever was setting the frame rate was not the drawing task.** That
measurement was taken while a server was feeding pictures in real time, so 10.5
fps was most likely the rate the link supplied, not a rate the device was
capable of.

Measure the turbine downstream of a pipe the upstream is already throttling, and
you measure the upstream every time. That is what this plan stops to fix first.

### The counters that exist, and what each one actually counts

The device prints one `CLOCK_ESTIMATED` line every ten seconds
(`main/av_player.c:3119`). The units are easy to misremember, so they are pinned
here:

| Field | Accumulated at | One accumulation is | Divide by | To get |
|---|---|---|---|---|
| `interval_frames` | `s.decoded` delta | a **complete frame** (all 15 stripes drawn) | itself | frames |
| `panel_ms` | around `push_stripe` | one **packet**, from before decoding to after the transfer wait | `panel_frames` for per packet, `interval_frames` for per frame | — |
| `panel_frames` | same | a **packet**, not a frame | — | — |
| `panel_wait_ms` | end of `push_stripe` | one stripe's DMA wait | — | — |
| `inflate_ms` | around `tinfl_decompress` | one stripe's decompression | — | — |
| `decode_max_ms` | same | the **worst** packet in the interval | — | — |
| `rx_pkts` / `rx_bps` | `receive_task` | literal | — | — |
| `rx_audio` | same | audio packets | — | — |
| `io_ms` / `wait_ms` / `iters` / `per_iter_us` | receive task | socket read time, audio flow-control wait, loop count | — | — |
| `starts` / `late` / `stray` | receive task | frames opened / frames dropped as late / packets that were not a frame's first | — | — |
| `nobuf` | receive task | packets dropped because both packet buffers were held | — | — |
| `in_bps` | `inflate_bytes` delta | compressed stripe bytes handed to the inflater, per second. Accumulated **before** the success tests, so not "successfully inflated". Not link throughput | — | — |

`panel_ms ÷ panel_frames` is per packet. `panel_ms ÷ interval_frames` is per
frame. An earlier note divided by the wrong one and was off by a factor of two.

---

## 1a. Results, measured (evening of 2026-09-16)

Running the first four layers below produced these. **Every figure was read off
the device; none is arithmetic.**

### What the panel costs on its own

From the `AV_DISPLAY_BENCH` build, before any socket exists, sixty frames:

| Method | Per frame | |
|---|---|---|
| One buffer, submit then wait for it | **46.13 ms** | the firmware path at the time; 2768 ms over 60 frames |
| Two buffers, one queued ahead | **33.00 ms** | 1980 ms over 60 frames, `drain_ms=1` — the wait is gone |
| Arithmetic floor | 30.7 ms | 153600 B ÷ 40 MHz |

**The 15.4 ms between 46.13 and 30.72 is entirely waiting.** The pipelined run
reaches 33.00 ms with 1 ms of wait left, so the transfer is fully overlapped and
about 33 ms is the real limit of this panel and this bus.

### What a real frame costs

One channel, one device, only the server's budget changed (`TV_VIDEO_BUDGET`):

| Server budget | Controller settles at | Device | Per frame | Of which waiting on DMA |
|---|---|---|---|---|
| 120000 (shipped) | RATE 4–5 | 4.4–5.5 fps | **70.5 ms** | 40.6 ms |
| 240000 | RATE 10, falling back to 7–8 | 9.2 fps | 39.6 ms | 20.0 ms |
| 480000 | RATE 12 (the ceiling) | 9.9 fps | 70.5 ms | 40.6 ms |

Before the change that 70.5 ms was inflation 19.4 + expansion 7.7 + submit 2.8 +
**DMA wait 40.6**. With a second stripe buffer overlapping the wait, the same
channel becomes **33.2 ms a frame with 0.04 ms of wait per stripe** — the
device's drawing capacity goes from 14 fps to about **30 fps**.

**Those six figures do not add up and must not be cross-referenced.** The 70.5 ms
line carries no `enlarge_ms` (enlargement is zero on this channel) and no
`overlay_ms`; its four measured parts sum to 70.5. The 33.2 ms line is a
different set entirely (inflation 15.3, expansion 6.8, submit 10.8, wait 0.1).
Pairing the first set's parts with the second set's total to claim "the six
parts agree within 1%" was two different runs spliced together, and it is not a
consistency check.

**The consistency check is `parts_ms` against `panel_ms` on the same line**,
which is what the device prints `parts_ms` for: it is the sum of inflation,
expansion, enlargement, overlay, submit and the DMA wait, computed on the way
out. Measured: `1013` against `1010` (−0.3%) and `2015` against `2014` (−0.0%).
Which number is being compared with which has to be the same line.

### What the link can carry

The same multi-channel run shows the device receiving **184–195 kB/s** at a
budget of 240000, close to double the 110 kB/s it gets today.

**What this supports is "195 kB/s was reached in a run", not "the link's ceiling
is 195 kB/s".** What it does establish is that **"the link is 160 kB/s" has no
basis as a sustained ceiling** — that figure is closer to where the controller
converges under a budget of 120000 than to a property of the link. The 195
figure is itself a measurement of the send loop as it stands, and three defects
in that loop are still unfixed (below).

### But the budget cannot simply be raised: one channel in four collapsed

| Channel | Budget 120000 | Budget 240000 |
|---|---|---|
| ch000 CCTV1 | 4.99 fps, 0 dropped, late 0 | 8.38 fps, **67 dropped**, late 40 |
| ch013 CGTN | 5.38, 0 dropped, late 0 | 8.76, **47 dropped**, late 11 |
| ch009 ottiptv | 4.88, 8 dropped, late 0 | **median 0 fps, 215 dropped, late 206, source fell to 30 kB/s** |
| ch030 metshop | 4.49, 9 dropped, late 0 | 8.78, **174 dropped**, late 112 |

Three channels genuinely double, at the cost of 47 to 174 dropped frames. The
fourth breaks outright. **Dropped frames are exactly the unevenness the viewer
complains about, so 240000 does not ship as it stands.** The headroom now
exists on the device side; what the sender needs is an adaptation that uses it
without overshooting, which is a sender-side question and outside this document.

### The conclusion

**The device is no longer the constraint under the shipped settings, and this is
not an inference: it draws a frame in 33.2 ms while the shipped configuration
delivers about 5 fps.** Both halves were measured across four channels.

**That 5 fps is the shipped configuration's result, not a property of the
link.** The same table above shows 8 to 10 fps at a higher budget, and about
195 kB/s was reached in a run. What is established is that the device can draw
faster than the shipped settings ask it to. **What limits the higher figures,
and whether the ceilings above them are stable, is not established** — the
budget that reached 8 to 10 fps also dropped frames on three channels and
collapsed a fourth, and three known send-loop defects are still unfixed. Until
those are fixed, a figure measured at a raised budget is a measurement of the
loop as much as of the link.

---

## 2. Why the world has to be stopped

A link is three stages in series: the server produces, the network carries, the
device receives and draws. Freeze any one of them and the true speed of the
others becomes visible.

On this device there are four places to freeze, from the outside in:

1. **Stop the server** — nothing new enters the link.
2. **Stop the receive task** — nobody reads the socket, the TCP window closes,
   and the sender stops on its own. Cleaner than the first, because it freezes
   the network and the device's internal queues together.
3. **Stop the drawing task** — the receive task keeps going and the packet
   buffers fill. `nobuf` and the `video_q` high-water mark say how deep the
   backlog is.
4. **Stop the SPI** — build stripes without submitting them, separating
   computing from transmitting.

Freezing each in turn gives four sets of numbers that do not contaminate each
other.

---

## 3. Three ceilings, not one

"Our device's display limit" currently means three different things, and they
have been used interchangeably:

- **Receive ceiling**: the fastest the device can pull bytes off the socket.
  This is WiFi, TCP and task scheduling combined.
- **Drawing ceiling**: the fastest it can turn packets that are already in
  memory into pixels on the panel. CPU plus SPI, **with no network involved**.
- **End-to-end ceiling**: both of the above running together while also feeding
  audio. This is what a viewer sees.

Every past claim that "the device is at its limit" conflated the second and the
third, and both decisive experiments ran below the link's supply rate.

---

## 4. The measurements, step by step

Each step states what is frozen, what to do, which fields to read, and what
range means what. **Change one thing at a time, and write the previous numbers
down before touching anything.**

### L0 — freeze the server, watch the drawing queue drain

**Why**: how much drawing work is already queued behind the link. This costs
nothing — no code change, no reflash.

**Do**

1. Play a channel until it settles: two consecutive `CLOCK_ESTIMATED` lines with
   a non-zero `interval_frames`.
2. Copy down every field in that line.
3. `kill -STOP <server pid>`. The process freezes; the connection stays open.
4. Record the device log every two seconds for twenty seconds.
5. `kill -CONT <pid>`.

**Read**

With nothing new arriving, the device still holds a backlog: packets in
`video_q`, PCM in the audio queue.

- `interval_frames` keeps being non-zero for a few more intervals. Those values
  are the **drawing rate with no link**. This is the first number in this
  project that is not contaminated by the sender.
- `nobuf` should be zero while frozen, because no new packet competes for a
  buffer. If it is not, the backlog was deeper than assumed before the freeze.
- `panel_ms ÷ interval_frames` is the full per-frame processing time. Compare it
  against the 30.7 ms floor.
- `panel_wait_ms ÷ panel_frames` is the real per-stripe DMA wait. Close to the
  2.05 ms stripe floor means the transfer is serial and unhidden; near zero means
  the processor is doing something else while it runs.

**Interpret**

- If `interval_frames` falls to zero while audio is still queued, the drawing
  task is blocked on something other than drawing. Go to L3.
- If the frozen drawing rate is clearly above the live `interval_frames`, the
  difference is what the link or the queue costs. L1 measures that.
- If the two are the same, the device's own drawing is the ceiling, and the
  conversation moves to memory buffering and to the encoding.

**Trap**: after `SIGSTOP` the server's TCP stack keeps pushing bytes already in
its kernel send buffer. The first second or two is mixed. **Start counting from
the third second.**

### L1 — one channel at a time, find the receive ceiling

**Why**: push the server far above what the device consumes and see how much it
swallows.

**Do**

Use a second channel as the control — the user's standing requirement is that a
single channel proves nothing. For each channel under test:

1. Point that channel at its own source. That is a table edit, not a rebuild.
2. Run the server with no rate limiting at all.
3. Watch `rx_bps`, `io_ms`, `iters` and `wait_ms` for three minutes.

**Read**

- `rx_bps` is the receive rate. Read it directly.
- `per_iter_us` is the mean cost of one read. Small and steady against the
  3000 ms read deadline means reads are not being starved.
- `wait_ms` is the audio flow-control wait. When it climbs, the audio queue is
  at its ceiling and the receive task is blocked by its own audio queue. **This
  is the likeliest real location of the receive ceiling**: before reading an
  audio packet, `receive_task` spins until
  `uxQueueMessagesWaiting(s.audio) > PCM_QUEUE-4` clears
  (`main/av_player.c:1047`). If audio does not drain, the whole receive line
  stops there.
- `nobuf` climbing means drawing cannot keep up with receiving.

**Interpret**

`rx_bps` flat while `io_ms` stays small means the limit is the TCP window or the
radio, not the software. `rx_bps` flat while `wait_ms` climbs means the limiter
is audio consumption, which is the I2S rate and has nothing to do with video.
That distinction matters a great deal: **if the receive ceiling is set by audio
drain, raising the video frame rate is impossible without changing the audio
queue policy.**

### L2 — pure drawing, the device's own speed

**Why**: remove the network entirely and measure the rate with the packets
already in memory.

**Do (no firmware change needed)**

Freezing the server leaves the receive task parked in `recv()`, because there is
nothing to read. **The receive task freezes itself**, so no test switch is
needed. L2 is therefore the same gesture as L0; only the interval that gets read
differs.

Two hard limits bound the window:

- Bytes already in the server's kernel buffer keep arriving. **Start at the
  third second.**
- Once media has started flowing, the device's own watchdog fails the session
  after five seconds without a packet (`idle_limit=5000`, around
  `main/av_player.c:2887`).

**So the usable window is from the third to the fifth second after `SIGSTOP`,
about two seconds.** At 10 fps that is twenty frames. Enough for a per-frame
cost, not enough for a tail, so repeat five times and take the median. Do not
conclude anything from a single run.

**Read**

`panel_ms ÷ interval_frames`. This is the per-frame cost of drawing with no link
in it. `nobuf` should be zero and `late` should stop climbing once the queue
drains; if not, the queue did not drain inside the window and the measurement is
void.

**Interpret**

- At or below 30.7 ms: drawing is against the SPI floor. No software helps;
  only fewer bytes (lower colour depth, lower resolution) or a faster bus.
  **Do not conclude this before L4**, because 30.7 ms has not itself been
  measured.
- Between 30.7 and 45 ms: serialisation is the dominant cost and a second
  buffer should pay. **Redo the two-buffer experiment**, this time with the
  world stopped.
- Well above 45 ms: CPU work dominates. Go to L3.

### L2b — a drawing ceiling with no link at all

L2's window is two seconds long and depends on the queue happening to hold
enough packets. For a number that runs as long as wanted, does not depend on
queue depth and has no network anywhere in it, use the standalone benchmark in
the firmware (see `AV_DISPLAY_BENCH` in step 3 of section 7).

It opens no socket and decodes nothing. It fills a stripe buffer and pushes it
at the panel sixty times a frame's worth, then prints the total with fill,
submit and wait separately. **This is the only measurement of what this panel
does at its rated 40 MHz**, and the only evidence the 30.7 ms floor will ever
have.

### L3 — split the cost of drawing

**Why**: once L2 shows CPU is dominant, find which part of the CPU's work it is.

**Do (firmware change)**

`push_stripe()` used to time two things: the whole thing (`panel_us`) and
decompression (`inflate_us`). Timers now sit between each stage:

```
tinfl_decompress   -> inflate_us   (existing)
av_expand_indexed  -> expand_us    (new)
enlarge_stripe     -> enlarge_us   (new)
overlay_stripe     -> overlay_us   (new)
submit             -> submit_us    (new)
wait               -> panel_wait_us (existing)
```

The six should sum to `panel_us`. That sum is its own instrument check: **a total
larger than the interval it sits inside means a timer was started twice or read
after a reset, and every mean derived from it is wrong.**

**Read**

What expansion and enlargement actually cost on real hardware. The code records
one measurement already (`main/av_player.c:1770-1782`): a single-pass version
that merged expansion and enlargement was slower than the two-pass version,
`decode_max_ms` going from 78 to 91, because the single-pass version does one
palette lookup per *panel* pixel while the two-pass does one per *source* pixel.
That says **palette lookups dominate on this chip**. L3 exists to confirm it:
if `expand_us` dominates, reducing lookups is where the frame rate is.

**Doable without the device**: count the per-pixel operations of
`av_expand_indexed`, `enlarge_stripe` and `tinfl_decompress` statically. That
can rank the candidates but cannot replace L3's numbers.

### L4 — the SPI on its own

**Why**: confirm the 30.7 ms floor is real on this board.

**Do**: skip all decoding and submit a constant-filled 153600-byte buffer 60
times, timing the whole run. This is the `AV_DISPLAY_BENCH` build described
above; it does not participate in normal playback.

**Interpret**: a measured figure well above 30.7 ms means the effective clock is
below 40 MHz or something else is in the way, and every "distance from the
floor" judgement in this project moves with it. **Do this before anything else
that depends on the floor**, because until now the floor has been arithmetic.

---

## 5. Fine-grained attribution: who blocks whom

The four layers above give each stage's capability. This section connects them
and answers what is holding what during an ordinary session.

There is one test: **see where the queue backs up.** Whichever stage has a full
queue in front of it is the one being held back, and the stage ahead of it is
the bottleneck.

| Symptom | Means it is stuck at | Next |
|---|---|---|
| `wait_ms` steadily non-zero, audio queue near full | audio consumption (I2S) cannot keep up with the sender | audio queue policy; unrelated to video |
| `nobuf` climbing, `video_q` high-water at its cap | the drawing task is holding both packet buffers | L2 |
| `nobuf` zero but `late` climbing | the device is fast enough; packets arrive too late | back to the link: `hdrgap_max`, `rx_bps` |
| `rx_bps` low with `io_ms` small | the sender is not feeding it | server side |
| `rx_bps` low with `io_ms` large | network or radio | RSSI, retries. Do not touch the main router |

`late` corresponds to `s.skipping` (`main/av_player.c:2085`), whose test is
`estimated_pts() - v.pts > 100`: this picture's moment is more than 100 ms behind
the playback clock. `late` climbing while the device is idle is a link problem.
`late` climbing while `nobuf` also climbs is a device problem.

**Discipline for this section**: watch one counter at a time. Watching two at
once produces two contradictory conclusions, which this project has already
done.

---

## 6. How to design the display memory buffering

This was tried once — one 10 KB stripe buffer against two — and reverted as
"no help, and it ate the channel list's memory". By section 1 that experiment's
conditions were invalid, so the result has to be retaken.

### Measure first, design after

The right form of the answer depends entirely on L2:

**If L2's per-frame cost is ≤ 32 ms**, serialisation is not dominant and a second
buffer is worthless. What is then worth doing is matching `AV_VIDEO_BUFFERS`'
depth to the drawing rate, not adding a stripe buffer.

**If it is 32 to 50 ms**, serialisation dominates and a second buffer pays. Then
the question is where its memory comes from. The heap at session start is about
65 KB; the video task stack (4096), audio task stack (4096), receive task stack
(5120) and the CONFIG buffer (7169) all come after that figure is printed. The
observed failure was `heap=13192 largest=7680` — **512 bytes short of
contiguous room, not short of room in total.** Before adding 10 KB, the channel
list's peak has to come down.

**If it is above 50 ms**, a buffer buys nothing; the problem is CPU.

### Three candidate paths, by cost

**One: shrink the channel table's peak.** Directly writing into the published
table removed the 8192-byte temporary allocation entirely, which is done. If
more room is still wanted, `channel_t` is `id[16] + name[48] = 64 bytes` and 128
entries need 8192 contiguous. `UI_MENU_NAME_MAX` is 48, but only about a dozen
characters fit on screen. Cutting the display name to 24 bytes takes an entry to
40 and 128 entries to 5120. The cost is truncated long names; check what the
menu needs first.

**Two: remove the overlap between the channel table and the session buffers.**
Packets are allocated before the channel list is parsed, so the two peaks
overlap. Parsing before the session buffers, or freeing the cJSON tree before
allocating packets, separates them. This costs no memory but reorders session
setup.

**Three: actually add the second stripe buffer.** Only after L2 shows
serialisation dominates and the first two have freed the room. The 10 KB has to
come from those, not from the packet buffers — that was tried once and cost 141
dropped frames.

### A no-memory alternative

`push_stripe()` currently waits for the transfer to finish before returning.
Deferring that wait until the buffer is about to be written again would need no
second buffer and would hide the wait under the next stripe's build time. The
code's own comments say this was the intent; the implementation waits anyway.

`s.stripe` is passed as the same pointer every time (`uint8_t *buf=s.stripe`,
`main/av_player.c:1737`), and the wait at the top of
`bsp_display_raw_submit()` is exactly the wait that would be owed before
overwriting it.

**So the smallest change is deleting the trailing wait in `push_stripe()`** and
letting submit's own wait cover it. One wait per stripe instead of two, and it
waits for the *previous* transfer rather than the current one. The catch is that
the next `av_expand_indexed` would then overwrite a buffer still being
transmitted, so **a second buffer is required for this to be safe**. Without it,
removing the wait tears the picture. This path and path three are two halves of
one change.

**Conclusion: run L2 before deciding anything about buffering.** Any specific
size or implementation given now would be a guess.

---

## 7. Execution order and acceptance

In order, each step producing a number that can be checked.

| # | Action | Code change | Output | Acceptance |
|---|---|---|---|---|
| 0 | Parse the channel table straight into the published table; no 8192-byte contiguous allocation | yes | sessions establish at all | a 127-channel CONFIG is accepted and `CONFIG carried no usable channel entries` does not appear |
| 1 | L1: two channels, three minutes each | no | `rx_bps` against `wait_ms` | the two channels differ; identical figures mean the readings do not track the channel |
| 2 | L0/L2: `SIGSTOP` the server, read the 3–5 s drain | no | drawing rate with no link | two consecutive non-zero `interval_frames` while frozen, `nobuf` zero |
| 3 | L2b + L4: `AV_DISPLAY_BENCH` | yes | measured ms per frame | how far it is from 30.7 ms |
| 4 | L3: read the split timers | yes | ms per stage | the six parts sum to `panel_us` |
| 5 | Choose the buffering scheme from L2 | — | the scheme | — |

Step 0 is the precondition, and it is already done: the 8192-byte temporary
table was the single thing blocking everything, and the table is now written in
place with no allocation at all. The timers for steps 3 and 4 are in the same
tree, so **all three ship in one reflash.**

---

## 8. What is still undecided

- **Whether the effective SPI clock is really 40 MHz.** Until L4, 30.7 ms is
  arithmetic.
- **Whether the 37 ms of `panel_wait` was really waiting for the SPI.** The
  37 → 2.2 ms reading is strong evidence, but it comes from an experiment whose
  conditions do not hold. It has to be retaken.
- **Whether the CPU is idle during the wait.** The code asserts other tasks are
  running, with no task-level evidence. L2 answers it directly.
- **How much memory a second stripe buffer really needs.** 10 KB is the buffer
  itself, plus fragmentation margin. Depends on what path one frees.
- **Whether the receive ceiling is set by audio drain.** This is L1's main
  question. If it is, raising the video frame rate has no route under the
  current audio queue policy.
- **How far the peak `decode_max_ms` sits above the mean.** The mean sets a
  sustained rate; the peak sets the margin. This project has used a mean to set a
  ceiling before and paid for it.

---

## 9. What this plan does not touch

- The main router is read-only.
- Only the `0x10000` application partition is written. No `flash-erase` on a
  configured device.
- No protocol, palette or resolution changes. Every measurement runs on the
  current format; encoding changes are a later conversation.
- No automated validation framework. Every step above is manual and moves one
  thing at a time.
