"""Synthetic negative cases for the capture summary; never device measurements."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

path = Path(__file__).resolve().parents[1]/'tools/summarize_live_trial.py'
spec = importlib.util.spec_from_file_location('live_trial_summary', path)
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


def window(t, frames=110, dropped=0, nobuf=0):
    return (f'[t={t} DEV] CLOCK_ESTIMATED interval_frames={frames} interval_ms=10000 '
            f'dropped={dropped} heap=41000 late=0 nobuf={nobuf}\n')


def finish(silence=0, decoded=10, dropped=0, ms=50000, samples=720000):
    return (f'CLOCK_ESTIMATED rendered_fps_x1000=200 session_ms={ms}\n'
            f'ESTIMATED clock: submitted_samples={samples} of_which_silence={silence} '
            f'program_samples={samples-silence} decoded={decoded} dropped={dropped} min_heap=5852\n')


class SummaryTests(unittest.TestCase):
    def test_empty_reconnect_does_not_erase_playback_silence(self):
        log = 'Allocated video=2\n'+window(10)+finish(17280)+\
              'Allocated video=2\n'+finish(0, decoded=0, samples=0, ms=50)
        result = summary.summarize(log, '')
        self.assertEqual(result['playback_sessions'], 1)
        self.assertEqual(result['silence_samples'], 17280)
        self.assertEqual(result['silence_seconds'], 1.08)

    def test_no_audio_empty_does_not_mean_no_silence(self):
        result = summary.summarize('Allocated video=2\n'+window(10)+finish(3840), '')
        self.assertEqual(result['audio_empty_events'], 0)
        self.assertEqual(result['silence_seconds'], .24)

    def test_cumulative_drops_are_not_added_per_window(self):
        log = 'Allocated video=2\n'+''.join(window(i*10, dropped=n) for i,n in enumerate([0,1,4,8],1))
        result = summary.summarize(log+finish(dropped=12), '')
        self.assertEqual(result['dropped_final'], 12)
        self.assertEqual(result['dropped_observed_max_sum'], 12)

    def test_nobuf_is_a_window_counter(self):
        log = 'Allocated video=2\n'+''.join(window(i*10,nobuf=n) for i,n in enumerate([23,33,41,34],1))
        self.assertEqual(summary.summarize(log+finish(), '')['nobuf_in_complete_windows'], 131)

    def test_missing_final_counter_is_unknown(self):
        result = summary.summarize('Allocated video=2\n'+window(10), '')
        self.assertIsNone(result['silence_samples'])
        self.assertFalse(result['terminal_counters_complete'])

    def test_empty_logs_do_not_report_zero_silence_or_fps(self):
        result = summary.summarize('', '')
        self.assertIsNone(result['silence_samples'])
        self.assertIsNone(result['all_windows']['fps'])
        self.assertEqual(result['acceptance'], 'NOT_EVALUATED')

    def test_heap_window_min_is_not_boot_low_watermark(self):
        result = summary.summarize('Allocated video=2\n'+window(10)+finish(), '')
        self.assertEqual(result['window_heap_min'], 41000)
        self.assertEqual(result['boot_heap_low_watermark'], 5852)

    def test_reset_classification_requires_stop_marker(self):
        log = '[t=12 DEV] Session reset: header-read\n[t=21 DEV] Session reset: connect\n'
        result = summary.summarize(log, '', [{'t':20, 'event':'stop_requested'}])
        self.assertEqual(result['resets_during_capture'], 1)
        self.assertEqual(result['resets_after_stop'], 1)
        self.assertIsNone(summary.summarize(log, '')['resets_during_capture'])

    def test_observation_excludes_warmup_boundary_and_post_stop(self):
        log = 'Allocated video=2\n'+window(10,1)+window(20,2)+window(30,110)+window(40,120)
        events = [{'t':15,'event':'observation_started'}, {'t':35,'event':'stop_requested'}]
        result = summary.summarize(log, '', events)
        self.assertEqual(result['all_windows']['frames'], 233)
        self.assertEqual(result['observation_windows']['frames'], 110)
        self.assertEqual(result['observation_windows']['ms'], 10000)
        self.assertEqual(result['acceptance'], 'NOT_EVALUATED')

    def test_final_session_and_window_rates_are_distinct(self):
        result = summary.summarize('Allocated video=2\n'+window(10,8)+finish(decoded=10), '')
        self.assertEqual(result['all_windows']['fps'], .8)
        self.assertEqual(result['sessions'][0]['final_session_fps'], .2)

    def test_server_gap_includes_startup_and_budget_is_observed(self):
        server = ('LIVE2 session=1 wall_s=5 video_budget_bps=48000 audio_gap_max_ms=388.0\n'
                  'LIVE2 session=1 wall_s=65 video_budget_bps=150645 audio_gap_max_ms=44.6\n')
        result = summary.summarize('', server)
        self.assertEqual(result['server']['observed_video_budget_max'], 150645)
        self.assertEqual(result['server']['observed_audio_gap_max_ms'], 388.0)


class CaptureProcessTests(unittest.TestCase):
    """A fake serial module and fake launcher check cleanup, not hardware."""
    def run_capture(self, serial_failure=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'tools').mkdir()
            (root/'channels.txt').write_text('synthetic fixture; never read by fake launcher')
            (root/'tools/run_live_v2.py').write_text(
                'import time\ntry:\n time.sleep(.2)\n print("LIVE2_START session=1",flush=True)\n'
                ' while True: time.sleep(.1)\nexcept KeyboardInterrupt: pass\n')
            serial_code = '''import time
class Serial:
 def __init__(self,*args,**kwargs): self.start=time.monotonic(); self.index=0
 def close(self): pass
 def readline(self):
  time.sleep(.02)
  elapsed=time.monotonic()-self.start
  if FAILURE and elapsed>.5: raise OSError('synthetic serial failure')
  rows=[(0.05,'Allocated video=2'),(1.2,'CLOCK_ESTIMATED interval_frames=11 interval_ms=1000 dropped=0 heap=41000 late=0 nobuf=0'),(2.0,'Session reset: header-read'),(2.05,'CLOCK_ESTIMATED rendered_fps_x1000=11000 session_ms=1000'),(2.1,'ESTIMATED clock: submitted_samples=16000 of_which_silence=0 program_samples=16000 decoded=11 dropped=0 min_heap=5852')]
  if self.index<len(rows) and elapsed>=rows[self.index][0]:
   text=rows[self.index][1]; self.index+=1; return (text+'\\n').encode()
  return b''
'''.replace('FAILURE', repr(serial_failure))
            (root/'serial.py').write_text(serial_code)
            env = dict(os.environ, PYTHONPATH=str(root))
            tool = path.parent/'run_live_v2_trial.py'
            result = subprocess.run([sys.executable,str(tool),'--repo',str(root),
                '--channels',str(root/'channels.txt'),'--channel','fixture','--serial-port','FAKE',
                '--seconds','1','--warmup-seconds','0','--output-dir',str(root/'runs')],
                env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=12)
            run = next((root/'runs').iterdir())
            return result, json.loads((run/'trial_summary.json').read_text()),\
                [json.loads(line) for line in (run/'capture_events.jsonl').read_text().splitlines()]

    def test_capture_has_phase_markers_and_never_calls_it_acceptance(self):
        process, result, events = self.run_capture()
        self.assertEqual(process.returncode, 0, process.stdout+process.stderr)
        self.assertEqual(result['capture_status'], 'CAPTURE_COMPLETED')
        self.assertEqual(result['acceptance'], 'NOT_EVALUATED')
        self.assertEqual(result['resets_after_stop'], 1)
        positions = {e['event']:e['t'] for e in events}
        self.assertGreaterEqual(positions['observation_completed']-positions['observation_started'], 1)
        self.assertGreaterEqual(positions['stop_requested'], positions['observation_completed'])

    def test_serial_failure_returns_nonzero_and_retains_unknown_counters(self):
        process, result, events = self.run_capture(serial_failure=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(result['capture_status'], 'CAPTURE_INCOMPLETE')
        self.assertIsNone(result['silence_samples'])
        self.assertTrue(any(e['event']=='serial_error' for e in events))


if __name__ == '__main__': unittest.main()
