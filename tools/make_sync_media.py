#!/usr/bin/env python3
"""Generate a deterministic A/V sync calibration video.

Video: 320x240 @ 12 fps.
- Black background.
- Every 2.0 seconds (every 24 frames), exactly 1 frame (frame 0, 24, 48, ...) is pure white.
- All other frames are pure black.

Audio: 16000 Hz 16-bit mono PCM.
- Silent background.
- Exactly coinciding with each white frame (duration = 1/12 s = ~83.33 ms, 1333 samples),
  a 1000 Hz sine tone is emitted at 70% full scale.
- All other samples are exact digital zero.

Usage:
    python3 tools/make_sync_media.py [--duration 120] [--output /tmp/sync_media_120s.mkv]
"""
import argparse
import math
import struct
import subprocess
import sys
import numpy as np

def generate_sync_media(duration_s=120, output_path="/tmp/sync_media_120s.mkv"):
    fps = 12
    width, height = 320, 240
    sample_rate = 16000
    total_frames = duration_s * fps
    total_samples = duration_s * sample_rate

    # Generate raw audio
    print(f"Generating audio: {total_samples} samples ({duration_s}s @ {sample_rate}Hz)...")
    audio = np.zeros(total_samples, dtype=np.int16)
    cycle_samples = 2 * sample_rate # 32000 samples per 2 seconds
    flash_samples = int(round(sample_rate / fps)) # 1333 samples per frame

    for cycle_start in range(0, total_samples, cycle_samples):
        flash_end = min(cycle_start + flash_samples, total_samples)
        t = np.arange(flash_end - cycle_start) / sample_rate
        # 1000 Hz tone, windowed slightly with half-sine ramp to avoid pop
        ramp_len = min(64, len(t) // 2)
        window = np.ones(len(t))
        if ramp_len > 0:
            ramp = 0.5 * (1 - np.cos(np.pi * np.arange(ramp_len) / ramp_len))
            window[:ramp_len] = ramp
            window[-ramp_len:] = ramp[::-1]
        tone = (0.7 * 32767 * np.sin(2 * np.pi * 1000 * t) * window).astype(np.int16)
        audio[cycle_start:flash_end] = tone

    audio_bytes = audio.tobytes()

    # Generate raw video and pipe both to ffmpeg
    print(f"Generating video: {total_frames} frames ({duration_s}s @ {fps}fps)...")
    black_frame = np.zeros((height, width, 3), dtype=np.uint8).tobytes()
    white_frame = (np.ones((height, width, 3), dtype=np.uint8) * 255).tobytes()

    temp_wav = "/tmp/sync_temp_audio.wav"
    import wave
    with wave.open(temp_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_bytes)

    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps),
        "-i", "pipe:0",
        "-i", temp_wav,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-g", "12",
        "-c:a", "pcm_s16le",
        output_path
    ]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    with proc.stdin as f_vid:
        for f in range(total_frames):
            if f % (2 * fps) == 0:
                f_vid.write(white_frame)
            else:
                f_vid.write(black_frame)

    proc.wait()
    try:
        import os
        os.remove(temp_wav)
    except Exception:
        pass
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with {proc.returncode}")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited with {proc.returncode}")

    print(f"Successfully generated {output_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--output", default="/tmp/sync_media_120s.mkv")
    args = parser.parse_args()
    generate_sync_media(args.duration, args.output)
