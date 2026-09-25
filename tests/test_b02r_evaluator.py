"""B02-R Negative Regression Test Suite.

Verifies that the evaluators, injectors, and harnesses reject invalid, synthetic,
or unrecovered conditions without false passes:
1. 10-second endurance runs CANNOT pass the 30-minute gate.
2. Dead display / no new video frames CANNOT pass pause recovery.
3. Stale / unchanging silence counters CANNOT claim 'no underflow'.
4. Session change immediately cancels pending injection requests.
5. Injections that fail to start or complete CANNOT claim recovery pass.
"""
import ast
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.fault import FaultInjector, InjectionStatus


class TestB02rEvaluator(unittest.TestCase):
    def test_10s_cannot_pass_30min_gate(self):
        """Verify that a short run (e.g. 10 seconds) fails the 30-minute endurance gate."""
        module = ast.parse(Path("tools/run_30min_stability.py").read_text())
        cls = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "StabilityTestRunner")
        run = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "run")
        selected = []
        active = False
        for n in run.body:
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "post_warmup" for t in n.targets):
                active = True
            if active:
                selected.append(n)
            if active and isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "summary" for t in n.targets):
                break

        compiled = compile(ast.Module(body=selected, type_ignores=[]), "delivered_stability_summary", "exec")
        short = SimpleNamespace(
            periodic_metrics=[{"fps": 12.0, "heap": 41000, "decode_max_ms": 34}],
            target_duration_s=10,
            channel_name="synthetic",
            session_resets=0
        )
        env = {"self": short, "total_elapsed": 10.0, "warmup_duration_s": 30.0}
        exec(compiled, env)
        synthetic_endurance = env["summary"]
        self.assertNotEqual(synthetic_endurance["verdict"], "PASS", f"10-second run must not pass 30-min gate! Got: {synthetic_endurance['verdict']}")

    def test_session_change_cancels_pending_injection(self):
        """Verify that when a session changes, pending injections for the previous session are cancelled."""
        injector = FaultInjector()
        req = injector.request_downstream_pause(1.0, target_session_id=1001, label="test_pause")
        self.assertEqual(req.status, InjectionStatus.REQUESTED)

        # Session 2002 connects, tearing down session 1001
        injector.cancel_pending(current_session=2002, reason="Session changed")
        self.assertEqual(req.status, InjectionStatus.CANCELLED)
        self.assertTrue(req.completed_event.is_set())
        self.assertIn("Session changed", str(req.error))

    def test_session_mismatch_in_check_pause_cancels_request(self):
        """Verify that check_pause rejects an injection requested for a different session."""
        injector = FaultInjector()
        req = injector.request_downstream_pause(1.0, target_session_id=1001, label="mismatch_pause")

        # A different session 1002 calls check_pause
        fake_conn = SimpleNamespace()
        fake_server = SimpleNamespace(logger=lambda msg: None, stop=SimpleNamespace(is_set=lambda: False))
        ok, pause_duration = injector.check_pause(fake_conn, session=1002, server=fake_server)

        self.assertTrue(ok)
        self.assertEqual(pause_duration, 0.0)
        self.assertEqual(req.status, InjectionStatus.CANCELLED)
        self.assertIn("Session mismatch", str(req.error))

    def test_uncompleted_injection_cannot_claim_recovery_pass(self):
        """Verify that if an injection is aborted (e.g. server stop during pause), status is CANCELLED and returns False."""
        import threading
        injector = FaultInjector()
        req = injector.request_downstream_pause(1.0, target_session_id=3003, label="abort_pause")

        stop_event = threading.Event()
        stop_event.set() # Server already stopped
        fake_server = SimpleNamespace(
            logger=lambda msg: None,
            stop=stop_event,
            phase="testing",
            _device_left=lambda conn, s: False
        )
        fake_conn = SimpleNamespace()

        ok, pause_duration = injector.check_pause(fake_conn, session=3003, server=fake_server)
        self.assertFalse(ok)
        self.assertEqual(pause_duration, 0.0)
        self.assertEqual(req.status, InjectionStatus.CANCELLED)
        self.assertIn("stopped", str(req.error).lower())

    def test_downstream_rejects_dead_display(self):
        """Verify that run_experiment_downstream_pauses rejects 9 synthetic samples with no video."""
        from typing import List, Dict, Any

        target_path = Path("tools/run_b02r_harness.py")
        if not target_path.exists():
            target_path = Path("tools/run_b02_device_experiments.py")
        module = ast.parse(target_path.read_text())
        needed = {"wait_for_healthy_baseline", "run_experiment_downstream_pauses"}
        selected = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name in needed]
        fn = next(n for n in selected if n.name == "run_experiment_downstream_pauses")

        class Clock:
            def __init__(self):
                self.value = 0.0
            def monotonic(self):
                return self.value
            def sleep(self, seconds):
                self.value += seconds

        clock = Clock()
        class FakeRunner:
            def __init__(self):
                self.server = SimpleNamespace(session_id=99, audio_sent=200, video_sent=0)
                self.injector = SimpleNamespace(request_downstream_pause=lambda *a, **k: None)
            def log_server(self, msg):
                pass
            def wait_for_active_streaming(self, **kwargs):
                return True
            def get_latest_device_metrics(self):
                return {"of_which_silence": 51200, "program_ms": 29800}
            def get_fresh_audio_empty_count(self, *args):
                return 0
            def get_fresh_window_metrics(self, *args):
                return []

        env = {
            "List": List, "Dict": Dict, "Any": Any,
            "DeviceExperimentRunner": FakeRunner, "time": clock, "print": lambda *a, **k: None
        }
        exec(compile(ast.Module(body=selected, type_ignores=[]), "delivered_downstream_function", "exec"), env)
        synthetic = env[fn.name](FakeRunner())
        passes = sum(r["verdict"] == "PASS" for r in synthetic)
        self.assertEqual(passes, 0, f"Delivered downstream verdict must reject synthetic dead display! Passed: {passes}")


if __name__ == "__main__":
    unittest.main()
