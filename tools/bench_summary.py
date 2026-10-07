#!/usr/bin/env python3
"""Tabulate the hardware benchmark's output.

The firmware prints one `BENCH name=... key=value` line per item
(main/av_player.c, hw_bench()), and one `CLOCK_ESTIMATED` line per ten seconds
during a session. This reads a captured console log and turns both into tables,
because the interesting comparisons are between variants, not within one line,
and doing that by eye across a terminal scrollback is how a wrong number gets
written down as a right one.

It also checks the arithmetic the firmware already offers. `CLOCK_ESTIMATED`
carries a `parts_ms` field that is the sum of its own parts, and the code beside
it says a sum larger than the interval containing it means a broken timer. That
check is repeated here rather than trusted: a counter that carries milliseconds
into a field divided by a thousand again has already happened once in this
project, and the failure looks like a plausible number.

    python3 tools/bench_summary.py bench_boot.txt
    python3 tools/bench_summary.py --json out.json bench_boot.txt

Offline by design: it takes a file, never a port, so it cannot itself disturb a
measurement in progress.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import OrderedDict

# name="..." then any number of key=value pairs. Values are ints or bare words
# (skipped=no_memory), so the split is on the first '=' of each pair.
BENCH_RE = re.compile(r"BENCH\s+name=(\S+)((?:\s+[A-Za-z_][A-Za-z0-9_]*=\S+)*)")
KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(\S+)")
# The session counter line, printed every ten seconds. Only the fields this
# script reasons about are captured; the rest are counted but not interpreted.
CLOCK_RE = re.compile(r"CLOCK_ESTIMATED\s+(.*)")
AUDIO_EMPTY_RE = re.compile(r"AUDIO_EMPTY\s+gap_ms=(\d+)")


def parse_values(text: str) -> "OrderedDict[str, object]":
    out: "OrderedDict[str, object]" = OrderedDict()
    for key, raw in KV_RE.findall(text):
        try:
            out[key] = int(raw)
        except ValueError:
            out[key] = raw
    return out


def parse_bench(lines) -> list[dict]:
    items = []
    for line in lines:
        m = BENCH_RE.search(line)
        if not m:
            continue
        record = {"name": m.group(1)}
        record.update(parse_values(m.group(2)))
        items.append(record)
    return items


def parse_clock(lines) -> list[dict]:
    return [parse_values(m.group(1)) for m in (CLOCK_RE.search(l) for l in lines) if m]


def check_parts_ms(clocks: list[dict]) -> list[str]:
    """Re-derive parts_ms from its components and report disagreement.

    The firmware sums inflate + expand + enlarge + overlay + submit + panel_wait
    and calls that parts_ms. Recomputing it here catches a counter that has been
    scaled twice, which has happened in this project and reads as a normal
    number.
    """
    parts = ("inflate_ms", "expand_ms", "enlarge_ms", "overlay_ms", "submit_ms")
    problems = []
    for i, c in enumerate(clocks):
        have = c.get("parts_ms")
        if have is None:
            continue
        # panel_wait_ms is part of the firmware's sum but is reported separately
        # from the five above, so it is added here explicitly.
        want = sum(c.get(k, 0) for k in parts) + c.get("panel_wait_ms", 0)
        # A millisecond of slack: each addend is integer-truncated before it is
        # summed, so the reconstructed value can legitimately differ by the
        # number of addends. More than that is a real disagreement.
        if abs(int(have) - want) > len(parts) + 1:
            problems.append(
                f"clock line {i + 1}: parts_ms={have} but the parts sum to {want}"
            )
    return problems


def print_bench_table(items: list[dict]) -> None:
    if not items:
        print("no BENCH lines found")
        return
    print("== hardware benchmark ==")
    width = max(len(str(i["name"])) for i in items)
    for item in items:
        name = item["name"]
        rest = "  ".join(f"{k}={v}" for k, v in item.items() if k != "name")
        print(f"  {name:<{width}}  {rest}")


def print_clock_table(clocks: list[dict]) -> None:
    if not clocks:
        print("\n== session counters == none found")
        return
    print(f"\n== session counters == {len(clocks)} interval(s)")
    fields = (
        "interval_frames",
        "interval_ms",
        "in_bps",
        "rx_pkts",
        "rx_bps",
        "late",
        "nobuf",
        "io_ms",
        "iters",
        "per_iter_us",
    )
    header = "  " + "  ".join(f"{f:>15}" for f in fields)
    print(header)
    for c in clocks:
        row = "  " + "  ".join(f"{c.get(f, '-'):>15}" for f in fields)
        print(row)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", help="captured console log (text)")
    parser.add_argument("--json", metavar="PATH", help="also write JSON here")
    args = parser.parse_args()

    try:
        with open(args.log, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        print(f"cannot read {args.log}: {exc}", file=sys.stderr)
        return 1

    bench = parse_bench(lines)
    clocks = parse_clock(lines)
    audio_empty = [int(m.group(1)) for m in (AUDIO_EMPTY_RE.search(l) for l in lines) if m]

    print_bench_table(bench)
    print_clock_table(clocks)

    if audio_empty:
        print(
            f"\n== audio underruns == {len(audio_empty)}, "
            f"max gap {max(audio_empty)} ms"
        )

    problems = check_parts_ms(clocks)
    if problems:
        print("\n== arithmetic check FAILED ==")
        for p in problems:
            print(f"  {p}")
    elif clocks:
        print("\n== arithmetic check == parts_ms agrees with its parts")

    if args.json:
        payload = {
            "bench": bench,
            "clock": clocks,
            "audio_empty_gaps_ms": audio_empty,
            "arithmetic_problems": problems,
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print(f"\nwrote {args.json}")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
