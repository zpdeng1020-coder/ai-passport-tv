"""V2 contracts and real TCP with a deliberately bandwidth-limited reader.

The reader is a host transport fixture, not ESP32 firmware or acoustic proof.
"""
import json
import random
import socket
import threading
import time
import unittest
import zlib
from unittest.mock import patch

from server import frames, live, tv_server
from server.live_sender import AudioSchedule, EndReader, PendingWrite, repack
from server.protocol import HEADER, Kind, Packet, VIDEO_CONTINUES, json_bytes, send_packet


def unpack(parts):
    result = bytearray()
    expected = 0
    for part in parts:
        first, count = part[:2]
        assert first == expected
        pos = 2 + 2 * count
        for i in range(count):
            size = int.from_bytes(part[2+2*i:4+2*i], 'big')
            result.extend(zlib.decompress(part[pos:pos+size]))
            pos += size
        assert pos == len(part)
        expected += count
    assert expected == frames.STRIPES
    return bytes(result)


class SenderContracts(unittest.TestCase):
    def test_fragmented_end_keeps_prefix_without_blocking_media(self):
        class Socket:
            data = bytearray(Packet(Kind.END, 7, 0, 0).encode())
            def recv(self, count):
                block = bytes(self.data[:min(3, count)])
                del self.data[:len(block)]
                return block
        reader, sock = EndReader(), Socket()
        for _ in range(7): self.assertFalse(reader.step(sock, 7, 0.1))
        self.assertTrue(reader.step(sock, 7, .2))

    def test_incomplete_end_times_out_without_waiting_for_more_bytes(self):
        class Socket:
            def recv(self, count): return b'F'
        reader = EndReader()
        self.assertFalse(reader.step(Socket(), 7, 2))
        with self.assertRaises(TimeoutError): reader.check_deadline(3.1)

    def test_repacking_preserves_every_pixel_and_bounds_packets(self):
        for data in (bytes(frames.FRAME_PIXELS), random.Random(7).randbytes(frames.FRAME_PIXELS)):
            result = repack(frames.frame_packets(data), 6144)
            self.assertTrue(all(len(p) <= 6144 for p in result))
            self.assertEqual(unpack(result), data)

    def test_repacking_rejects_missing_stripe(self):
        stripe = zlib.compress(bytes(frames.STRIPE_PIXELS))
        with self.assertRaises(ValueError):
            repack([frames.packet(1, [stripe])], 6144)

    def test_partial_packet_is_not_restarted_or_interleaved(self):
        class ShortSocket:
            data = bytearray()
            calls = 0
            def send(self, data):
                self.calls += 1
                if self.calls % 3 == 0:
                    raise BlockingIOError()
                n = min(71, len(data))
                self.data.extend(data[:n])
                return n
        sock = ShortSocket()
        packet = Packet(Kind.PCM, 7, 9, 40, bytes(1280))
        pending = PendingWrite(packet, 0, 1)
        while not pending.step(sock, 0.5):
            pass
        self.assertEqual(bytes(sock.data), packet.encode())

    def test_partial_timeout_emits_no_replacement_packet(self):
        class Socket:
            data = bytearray()
            def send(self, data): self.data.extend(data[:10]); return 10
        sock = Socket()
        pending = PendingWrite(Packet(Kind.PCM, 1, 1, 0, bytes(1280)), 0, 1)
        self.assertFalse(pending.step(sock, .1))
        with self.assertRaises(TimeoutError): pending.step(sock, 1.01)
        self.assertEqual(len(sock.data), 10)

    def test_long_pause_cannot_create_an_unbounded_audio_burst(self):
        pacing = AudioSchedule(0)
        self.assertTrue(pacing.recover(30))
        sent = 0
        while pacing.due(30):
            pacing.sent(); sent += 1
        self.assertLessEqual(sent, 8)
        self.assertGreaterEqual(sent, 7)

    def test_joint_trim_preserves_wire_pcm_continuity(self):
        url = 'http://127.0.0.1/no-network-fixture'
        with patch.dict(live.CHANNELS, {'test': url}):
            channel = live.LiveChannel(url)
        channel.calibrate_from_source()
        for i in range(300): channel._push_audio(bytes(1280), i * 40)
        for i in range(144): channel._push_video(bytes(frames.FRAME_PIXELS), i * 1000 / 12)
        for _ in range(10): channel.pop_audio()
        removed = channel.trim_backlog()
        self.assertEqual(removed, 190)
        self.assertEqual(len(channel.audio), 100)
        self.assertGreaterEqual(channel.video_content[0], channel.audio_content[0])
        self.assertEqual(channel.pop_audio()[1], 400)
        self.assertEqual(channel.pop_audio()[1], 440)


class TcpIntegration(unittest.TestCase):
    def test_heavy_video_preserves_audio_and_frame_protocol_on_slow_reader(self):
        raw = random.Random(17).randbytes(frames.FRAME_PIXELS)
        class FixtureChannel(live.LiveChannel):
            def start(self):
                self.calibrate_from_source()
                for i in range(200): self._push_audio(bytes(1280), i * 40)
                for i in range(96): self._push_video(raw, i * 1000 / 12)

        env = {'TV_LIVE_ENGINE': 'v2', 'TV_LIVE_PACKET_BYTES': '6144', 'TV_STRIPE_PACE_MS': '5'}
        url = 'http://127.0.0.1/host-tcp-fixture'
        logs = []
        with patch.dict('os.environ', env), patch.dict(live.CHANNELS, {'test_v2': url}):
            server = tv_server.AVServer(None, None, '127.0.0.1', 0, logger=logs.append)
            server.live_enabled = True
            server.channel_name = 'test_v2'
            server.channel_factory = lambda source, *a: FixtureChannel(source)
            thread = threading.Thread(target=server.serve)
            thread.start()
            self.assertTrue(server.ready.wait(3))
            client = socket.create_connection(('127.0.0.1', server.port), timeout=3)
            client.setblocking(False)
            send_packet(client, Packet(Kind.HELLO, 0, 0, 0, json_bytes({'version': 1})))
            buffer, packets, audio_times, complete_frames = bytearray(), [], [], []
            started, first_audio = time.monotonic(), None
            current_parts, current_pts = [], None
            try:
                while time.monotonic() - started < 8:
                    try: block = client.recv(256)
                    except BlockingIOError:
                        time.sleep(.001); continue
                    if not block: break
                    buffer.extend(block)
                    # Deliberate transport ceiling, including PCM and headers.
                    time.sleep(len(block) / 96000)
                    while len(buffer) >= HEADER.size:
                        magic, version, kind, flags, session, seq, pts, size = HEADER.unpack(buffer[:HEADER.size])
                        if len(buffer) < HEADER.size + size: break
                        payload = bytes(buffer[HEADER.size:HEADER.size+size])
                        del buffer[:HEADER.size+size]
                        self.assertEqual(magic, b'FAV1')
                        self.assertEqual(version, 1)
                        self.assertEqual(seq, len(packets))
                        packets.append((kind, flags, pts))
                        if kind == Kind.PCM:
                            self.assertEqual(pts, len(audio_times)*40)
                            audio_times.append(time.monotonic())
                            if first_audio is None: first_audio = time.monotonic()
                        elif kind == Kind.JPEG:
                            self.assertLessEqual(size, 6144)
                            if flags == 0:
                                self.assertFalse(current_parts, 'previous frame was abandoned')
                                current_pts = pts
                            else:
                                self.assertEqual(flags, VIDEO_CONTINUES)
                                self.assertEqual(pts, current_pts)
                            current_parts.append(payload)
                            if payload[0] + payload[1] == frames.STRIPES:
                                self.assertEqual(unpack(current_parts), raw)
                                complete_frames.append(pts)
                                current_parts = []
                    if first_audio and (time.monotonic() - first_audio >= 4.2 or len(audio_times) >= 85): break
            finally:
                server.stop.set()
                client.close()
                thread.join(3)
            self.assertFalse(thread.is_alive(), logs)
            self.assertGreaterEqual(len(complete_frames), 1, logs)
            self.assertGreaterEqual(len(audio_times), 85, logs)
            max_gap = max(b-a for a,b in zip(audio_times, audio_times[1:]))
            self.assertLess(max_gap, .4, logs)
            self.assertEqual(complete_frames, sorted(set(complete_frames)))
            self.assertGreater(server.video_sent, server.frames_sent)
            print('LIVE2_HOST_TCP', json.dumps(dict(audio_packets=len(audio_times),
                complete_frames=len(complete_frames), video_packets=server.video_sent,
                max_audio_gap_ms=round(max_gap*1000, 2), device_test=False)))


if __name__ == '__main__': unittest.main()
