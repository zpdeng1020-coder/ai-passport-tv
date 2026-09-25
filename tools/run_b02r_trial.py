#!/usr/bin/env python3
"""Start a normal live session with the B02R trial profile. No flash writes.

Run --help for arguments. --dry-run validates configuration without connecting.
This is a trial entry point, not a hardware acceptance test or PASS evaluator.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


PROFILE = {
    'TV_GEOMETRY': '320x240', 'TV_PACKET_TARGET': '22528',
    'TV_FPS': '12', 'TV_MAX_FPS': '12', 'TV_START_FPS': '12',
    'TV_MIN_FPS': '3', 'TV_ADAPTIVE': '0',
    'TV_SYNC_TOLERANCE_S': '0.15', 'TV_PCM_QUEUE_CHUNKS': '400',
    'TV_VIDEO_QUEUE': '224', 'TV_PREBUFFER_S': '4',
    'TV_FRAME_DEADLINE_S': '2.5', 'TV_AUDIO_LOOKAHEAD_MS': '280',
    'TV_AUDIO_LEAD_MS': '320', 'TV_VIDEO_LEAD_MS': '320',
    'TV_AUDIO_FILL': '1',
}


class Tee:
    def __init__(self, terminal, file): self.terminal, self.file = terminal, file
    def write(self, text):
        self.terminal.write(text)
        self.file.write(text)
        self.file.flush()
        return len(text)
    def flush(self): self.terminal.flush(); self.file.flush()
    def __getattr__(self, name): return getattr(self.terminal, name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True, help='Existing project root')
    parser.add_argument('--channels', type=Path, required=True, help='Explicit existing channel file')
    parser.add_argument('--channel', required=True, help='A key from that file, e.g. baseline or ch000')
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8096)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--loop', action='store_true', help='Loop a finite test source; omit for live TV')
    parser.add_argument('--output-dir', type=Path, default=Path('b02r_trial_runs'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    repo, channels = args.repo.resolve(), args.channels.resolve()
    output_root = args.output_dir.resolve()
    if not (repo / 'server/tv_server.py').is_file(): parser.error('Project server/tv_server.py is missing')
    if not channels.is_file(): parser.error('Channel file does not exist; no file will be created or replaced')
    if shutil.which(args.ffmpeg) is None: parser.error('FFmpeg is not available')
    profile = {**PROFILE, 'TV_STREAM_LOOP': '1' if args.loop else '0'}
    # Freeze the named profile, including inherited experimental settings that
    # could otherwise change its behavior. Authentication outside TV_* is kept.
    inherited_keys = [key for key in os.environ if key.startswith('TV_')]
    for key in inherited_keys: os.environ.pop(key)
    os.environ.update(profile)
    os.environ['TV_CHANNELS_FILE'] = str(channels)
    os.chdir(repo)
    sys.path.insert(0, str(repo))
    from server import live, media, tv_server, rate, frames
    live.load_channels(channels)
    if args.channel not in live.CHANNELS:
        parser.error('Channel key is absent from the supplied file')
    effective = dict(geometry=f'{frames.WIDTH}x{frames.HEIGHT}',
        packet_target=frames.PACKET_TARGET_BYTES,
        fps=media.FPS, start_fps=rate.START_FPS, adaptive=rate.ADAPTIVE,
        pcm_chunks=live.PCM_QUEUE_CHUNKS, video_frames=live.VIDEO_QUEUE_FRAMES,
        sync_tolerance_s=live.VIDEO_SYNC_TOLERANCE_S,
        frame_deadline_s=tv_server.FRAME_WRITE_DEADLINE_S)
    expected = dict(geometry='320x240', packet_target=22528,
        fps=12, start_fps=12, adaptive=False, pcm_chunks=400,
        video_frames=224, sync_tolerance_s=0.15, frame_deadline_s=2.5)
    if effective != expected: parser.error('Loaded server does not implement the expected trial profile')
    manifest = dict(candidate='B02R reviewer trial; device acceptance NOT RUN',
        created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        channel_key=args.channel, channel_file_sha256=hashlib.sha256(channels.read_bytes()).hexdigest(),
        profile=profile, effective=effective, bind=args.bind, port=args.port,
        python=sys.version, removed_inherited_tv_keys=sorted(inherited_keys),
        source_sha256={str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in sorted((repo / 'server').glob('*.py'))})
    if args.dry_run:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        print('DRY RUN ONLY: no server started, no device contacted.')
        return 0
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix='trial-', dir=output_root))
    (run_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(f'Trial log directory: {run_dir}')
    stdout, stderr = sys.stdout, sys.stderr
    with (run_dir / 'server.log').open('w', buffering=1) as log:
        sys.stdout, sys.stderr = Tee(stdout, log), Tee(stderr, log)
        try:
            return tv_server.main(['live', '--channel', args.channel, '--bind', args.bind,
                '--port', str(args.port), '--ffmpeg', args.ffmpeg, '--media', 'both'])
        finally:
            sys.stdout, sys.stderr = stdout, stderr


if __name__ == '__main__':
    raise SystemExit(main())
