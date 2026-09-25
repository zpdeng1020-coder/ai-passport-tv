#!/usr/bin/env python3
"""Measure the real decode/pack pipeline without any device or sender.

Runs the configured paced input. This establishes realtime supply, not peak
unpaced CPU capacity. Output contains no channel URL or credentials.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channels', type=Path, required=True)
    parser.add_argument('--channel', required=True)
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.seconds < 5: parser.error('Measure at least 5 seconds after warm-up')
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    os.environ['TV_CHANNELS_FILE'] = str(args.channels.resolve())
    from server import live, media, frames
    live.load_channels(args.channels)
    if args.channel not in live.CHANNELS: parser.error('Unknown channel key')
    channel = live.LiveChannel(live.CHANNELS[args.channel], args.ffmpeg,
                               live.CHANNEL_AGENTS.get(args.channel, ''))
    result = dict(test='source_without_sender', channel=args.channel, machine=platform.platform(),
                  device_connected=False, source_is_paced=True, target_video_fps=media.FPS,
                  geometry=f'{frames.WIDTH}x{frames.HEIGHT}', packet_target=frames.PACKET_TARGET_BYTES)

    def drain():
        # Both pipe readers keep running. We discard their finished output
        # here, preventing any network/device backpressure on the decoder.
        with channel.lock:
            for queue in (channel.audio, channel.audio_at, channel.audio_content,
                          channel.video, channel.video_at, channel.video_content):
                queue.clear()

    try:
        channel.build_palette()
        channel.start()
        deadline = time.monotonic() + 30
        while not channel.prebuffered():
            if channel.failure(): raise RuntimeError('decoder failed during warm-up')
            if time.monotonic() > deadline: raise TimeoutError('source warm-up exceeded 30 seconds')
            time.sleep(.01)
        initial = channel.flow_snapshot()
        started = time.monotonic()
        while time.monotonic() - started < args.seconds:
            if channel.failure(): raise RuntimeError('decoder failed during measurement')
            drain()
            time.sleep(.005)
        elapsed = time.monotonic() - started
        final = channel.flow_snapshot()
        n_audio = final['audio_produced'] - initial['audio_produced']
        n_video = final['video_produced'] - initial['video_produced']
        n_bytes = final['video_encoded_bytes'] - initial['video_encoded_bytes']
        result.update(status='MEASURED', duration_s=round(elapsed, 3),
            audio_chunks=n_audio, video_frames=n_video,
            audio_chunks_s=round(n_audio/elapsed, 3), video_frames_s=round(n_video/elapsed, 3),
            average_video_frame_bytes=round(n_bytes/n_video, 2) if n_video else None,
            encoded_video_bytes_s=round(n_bytes/elapsed, 2),
            pack_wall_s=round(final['video_encode_wall_s']-initial['video_encode_wall_s'], 4))
    except Exception as error:
        result.update(status='FAILED', error_type=type(error).__name__)
    finally:
        channel.close()
    result['server_sha256'] = {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted((repo/'server').glob('*.py'))}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # New output only: measurements must never silently replace an old run.
    with args.output.open('x') as handle: json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'MEASURED' else 1


if __name__ == '__main__': raise SystemExit(main())
