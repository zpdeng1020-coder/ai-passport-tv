#!/usr/bin/env python3
"""Audit raw trial logs without treating missing counters as zero or declaring PASS."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re


def numbers(line):
    return {key: float(value) if '.' in value else int(value)
            for key, value in re.findall(r'(\w+)=(-?\d+(?:\.\d+)?)', line)}


def captured_at(line):
    match = re.match(r'\[t=(\d+(?:\.\d+)?) ', line)
    return float(match[1]) if match else None


def summarize(serial_text, server_text, events=None):
    events = events or []
    stop = next((e['t'] for e in events if e['event'] == 'stop_requested'), None)
    observe = next((e['t'] for e in events if e['event'] == 'observation_started'), None)
    sessions, resets, current = [], [], None

    def new_session(anchored=False):
        entry = dict(index=len(sessions), start_observed=anchored,
                     windows=[], clock=None, final=None, audio_empty_events=0)
        sessions.append(entry)
        return entry

    for line_no, line in enumerate(serial_text.splitlines(), 1):
        values, t = numbers(line), captured_at(line)
        if 'Allocated video=' in line:
            current = new_session(True)
        if 'interval_frames=' in line and 'interval_ms=' in line:
            if current is None or current['clock'] is not None:
                current = new_session()
            record = dict(values, line=line_no, captured_t=t)
            record['phase'] = ('unclassified' if t is None or observe is None else
                               'post_stop' if stop is not None and t > stop else
                               'observation' if t-values['interval_ms']/1000 >= observe else
                               'warmup_or_boundary')
            current['windows'].append(record)
        if 'AUDIO_EMPTY' in line:
            if current is None:
                current = new_session()
            current['audio_empty_events'] += 1
        if 'rendered_fps_x1000=' in line and 'session_ms=' in line:
            if current is None or current['clock'] is not None:
                current = new_session()
            current['final'] = dict(values, line=line_no)
        if 'ESTIMATED clock:' in line and 'of_which_silence=' in line:
            if current is None:
                current = new_session()
            current['clock'] = dict(values, line=line_no)
        if 'Session reset:' in line:
            phase = ('unclassified' if t is None or stop is None else
                     'after_stop_requested' if t >= stop else
                     'during_capture')
            resets.append(dict(line=line_no, captured_t=t, phase=phase))

    # Empty reconnect attempts must never replace the preceding playback totals.
    active = [s for s in sessions if s['windows'] or
              (s['clock'] and (s['clock'].get('submitted_samples', 0) or
                               s['clock'].get('decoded', 0)))]
    for s in active:
        windows = s['windows']
        clock = s['clock']
        s['window_frames'] = sum(w['interval_frames'] for w in windows)
        s['window_ms'] = sum(w['interval_ms'] for w in windows)
        s['window_fps'] = (1000*s['window_frames']/s['window_ms'] if s['window_ms'] else None)
        s['silence_samples'] = clock.get('of_which_silence') if clock else None
        s['dropped_observed_max'] = max([w.get('dropped', 0) for w in windows] +
                                      ([clock['dropped']] if clock and 'dropped' in clock else [0]))
        s['dropped_final'] = clock.get('dropped') if clock else None
        s['late_observed_max'] = max([w.get('late', 0) for w in windows], default=0)
        s['nobuf_in_windows'] = sum(w.get('nobuf', 0) for w in windows)
        s['final_session_fps'] = (clock['decoded']*1000/s['final']['session_ms']
                                 if clock and 'decoded' in clock and s['final'] and s['final']['session_ms'] else None)
    windows = [w for s in active for w in s['windows']]
    def window_totals(selected):
        frames = sum(w['interval_frames'] for w in selected)
        ms = sum(w['interval_ms'] for w in selected)
        return dict(count=len(selected), frames=frames, ms=ms,
                    fps=frames*1000/ms if ms else None,
                    min_fps=min((w['interval_frames']*1000/w['interval_ms'] for w in selected), default=None),
                    max_fps=max((w['interval_frames']*1000/w['interval_ms'] for w in selected), default=None))

    complete_clocks = bool(active) and all(s['clock'] is not None for s in active)
    total_silence = sum(s['silence_samples'] for s in active) if complete_clocks else None
    reports, previous, frame_costs = [], {}, []
    for line in server_text.splitlines():
        if 'LIVE2 session=' not in line:
            continue
        prefix, _, source = line.partition(' source=')
        report = numbers(prefix)
        reports.append(report)
        try:
            flow = ast.literal_eval(source.split(' device_fps=')[0])
            sid = report['session']
            if sid in previous:
                old = previous[sid]
                n = flow['video_produced']-old['video_produced']
                if n > 0:
                    frame_costs.append((flow['video_encoded_bytes']-old['video_encoded_bytes'])/n)
            previous[sid] = flow
        except (ValueError, SyntaxError, KeyError, TypeError):
            pass
    errors = [e for e in events if e['event'].endswith('_error')]
    result = dict(status='ANALYZED', acceptance='NOT_EVALUATED',
        firmware_identity='UNVERIFIED', acoustic_sync='UNMEASURED',
        playback_sessions=len(active), sessions=active,
        all_windows=window_totals(windows),
        observation_windows=window_totals([w for w in windows if w['phase']=='observation']),
        silence_samples=total_silence,
        silence_seconds=total_silence/16000 if total_silence is not None else None,
        silence_scope='final software counters of captured playback sessions; not acoustic or phase-local',
        terminal_counters_complete=complete_clocks,
        session_starts_complete=bool(active) and all(s['start_observed'] for s in active),
        dropped_final=sum(s['dropped_final'] for s in active)
                      if active and all(s['dropped_final'] is not None for s in active) else None,
        dropped_observed_max_sum=sum(s['dropped_observed_max'] for s in active),
        late_observed_max_sum=sum(s['late_observed_max'] for s in active),
        nobuf_in_complete_windows=sum(s['nobuf_in_windows'] for s in active),
        audio_empty_events=sum(s['audio_empty_events'] for s in active),
        window_heap_min=min((w['heap'] for w in windows if 'heap' in w), default=None),
        boot_heap_low_watermark=min((s['clock']['min_heap'] for s in active
                                    if s['clock'] and 'min_heap' in s['clock']), default=None),
        resets=resets, resets_raw=len(resets),
        resets_during_capture=sum(r['phase']=='during_capture' for r in resets) if stop is not None else None,
        resets_after_stop=sum(r['phase']=='after_stop_requested' for r in resets) if stop is not None else None,
        capture_errors=errors,
        server=dict(report_count=len(reports),
                    observed_video_budget_max=max((v.get('video_budget_bps', 0) for v in reports),default=None),
                    observed_audio_gap_max_ms=max((v.get('audio_gap_max_ms', 0) for v in reports),default=None),
                    consecutive_source_frame_cost_min=min(frame_costs,default=None),
                    consecutive_source_frame_cost_max=max(frame_costs,default=None)))
    result['warnings'] = []
    if not complete_clocks: result['warnings'].append('Missing final playback counters; silence is unknown, not zero.')
    if stop is None: result['warnings'].append('No stop marker; raw resets cannot be labelled unexpected automatically.')
    if not result['session_starts_complete']: result['warnings'].append('At least one playback session start was not captured.')
    if not windows: result['warnings'].append('No complete device windows captured.')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    path = args.run_dir/'capture_events.jsonl'
    events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    result = summarize((args.run_dir/'serial.log').read_text(),
                       (args.run_dir/'server.log').read_text(), events)
    with args.output.open('x', encoding='utf-8') as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
    print(json.dumps({k:result[k] for k in ('status','acceptance','all_windows','silence_seconds','server','warnings')},ensure_ascii=False,indent=2))
    return 0


if __name__ == '__main__': raise SystemExit(main())
