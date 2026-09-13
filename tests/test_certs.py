"""Finding certificate authorities on a machine that is not the build machine.

This was a real failure, and one the tests here would have had to be written
against: the macOS build from CI answered

    unable to get local issuer certificate

for an address `curl` fetched without complaint, because the certificate path
recorded by the build machine's Python does not exist on the reader's. It was
found by a user, in the first thing they tried.

What can be checked without reproducing that machine: that a healthy system is
left alone, that a deliberate setting is not overruled, that a file which exists
but loads nothing is rejected, and that the environment is restored when nothing
works. The last two matter most -- the first fix that comes to mind is to check
whether the file exists, and that is precisely the check that would have passed
on the broken machine if the path had been the right one but the file empty.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import certs


def _clear_variables():
    """A patch removing the certificate variables from the environment.

    Tests run on machines where these may be set -- a corporate bundle, or a CI
    runner -- and `use_system_ca` treats them as deliberate, so leaving them in
    would make every test below measure the wrong thing.
    """
    patched = {name: os.environ.pop(name, None) for name in certs.ENVIRONMENT_VARIABLES}
    return patched


def _restore(patched):
    os.environ.pop("SSL_CERT_FILE", None)
    os.environ.pop("SSL_CERT_DIR", None)
    for name, value in patched.items():
        if value is not None:
            os.environ[name] = value


class HealthySystemTests(unittest.TestCase):
    def test_a_machine_that_already_verifies_is_not_touched(self):
        """The ordinary case, and the one that must change nothing.

        A Python running where it was installed finds its own bundle. Anything
        this function did here would be an unforced change to a working setup.
        """
        saved = _clear_variables()
        try:
            with mock.patch.object(certs, "loaded_authorities", return_value=137):
                self.assertIsNone(certs.use_system_ca())
            self.assertNotIn("SSL_CERT_FILE", os.environ)
        finally:
            _restore(saved)

    def test_a_setting_that_works_is_respected(self):
        """Whether a setting is deliberate is answered by whether it verifies.

        A company bundle loads authorities, and the variable naming it is
        someone's arrangement. A path left over from the machine this program
        was built on loads nothing -- and looks identical in the environment.
        The count is what tells them apart; the presence of the variable does
        not, which is why it is not what decides.
        """
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": "/company/bundle.pem"}):
            with mock.patch.object(certs, "loaded_authorities", return_value=137):
                self.assertIsNone(certs.use_system_ca())
            self.assertEqual(os.environ["SSL_CERT_FILE"], "/company/bundle.pem")


class RepairTests(unittest.TestCase):
    def test_a_bundle_that_loads_authorities_is_adopted(self):
        """Rule: set the variable, then ask what actually got loaded."""
        saved = _clear_variables()
        try:
            with tempfile.TemporaryDirectory() as directory:
                bundle = Path(directory) / "ca.pem"
                bundle.write_text("placeholder\n")

                def authorities():
                    # Zero until the variable points at our bundle, then 137
                    # -- the shape of the real failure, where nothing loads
                    # until the path is right.
                    return 137 if os.environ.get("SSL_CERT_FILE") == str(bundle) else 0

                with mock.patch.object(certs, "SYSTEM_CA_FILES", (str(bundle),)), \
                        mock.patch.object(certs, "loaded_authorities", authorities):
                    self.assertEqual(certs.use_system_ca(), str(bundle))
                self.assertEqual(os.environ.get("SSL_CERT_FILE"), str(bundle))
        finally:
            _restore(saved)

    def test_a_bundle_that_exists_but_loads_nothing_is_rejected(self):
        """The check that the first version of this got wrong.

        A file being present says nothing about whether it is a usable bundle:
        it may be empty, truncated, or a directory that has moved. Only the
        number of authorities loaded answers the question the caller has.
        """
        saved = _clear_variables()
        try:
            with tempfile.TemporaryDirectory() as directory:
                bundle = Path(directory) / "empty.pem"
                bundle.write_text("")
                with mock.patch.object(certs, "SYSTEM_CA_FILES", (str(bundle),)), \
                        mock.patch.object(certs, "loaded_authorities", return_value=0):
                    self.assertIsNone(certs.use_system_ca())
        finally:
            _restore(saved)

    def test_the_environment_is_restored_when_nothing_works(self):
        """No candidate may be left behind as a half-applied setting.

        A variable pointing at a bundle that does not load would break HTTPS
        for this process and, if anything later re-reads the environment, for
        whatever it starts. Leaving nothing is the only honest outcome.
        """
        saved = _clear_variables()
        try:
            with mock.patch.object(certs, "SYSTEM_CA_FILES", ("/nonexistent/ca.pem",)), \
                    mock.patch.object(certs, "loaded_authorities", return_value=0):
                self.assertIsNone(certs.use_system_ca())
            self.assertNotIn("SSL_CERT_FILE", os.environ)
        finally:
            _restore(saved)

    def test_what_was_there_before_is_put_back(self):
        """The tests above clear the environment; a real machine may not have it
        clear. Whatever was there and did not work is still what was there, and
        a program that overwrote it and gave up would leave a second mystery
        behind the first.
        """
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": "/stale/path.pem"}):
            with mock.patch.object(certs, "SYSTEM_CA_FILES", ("/nonexistent/ca.pem",)), \
                    mock.patch.object(certs, "loaded_authorities", return_value=0):
                self.assertIsNone(certs.use_system_ca())
            self.assertEqual(os.environ["SSL_CERT_FILE"], "/stale/path.pem")

    def test_the_first_candidate_that_works_wins(self):
        """Order is a preference between equivalents, not a decision to weigh."""
        saved = _clear_variables()
        try:
            with tempfile.TemporaryDirectory() as directory:
                first = Path(directory) / "first.pem"
                second = Path(directory) / "second.pem"
                first.write_text("")
                second.write_text("")

                def authorities():
                    return 137 if os.environ.get("SSL_CERT_FILE") == str(second) else 0

                with mock.patch.object(certs, "SYSTEM_CA_FILES",
                                       (str(first), str(second))), \
                        mock.patch.object(certs, "loaded_authorities", authorities):
                    self.assertEqual(certs.use_system_ca(), str(second))
        finally:
            _restore(saved)


class EntryPointTests(unittest.TestCase):
    """Every way this program starts has to prepare the certificates.

    The repair above is worth nothing if a program that reaches the network
    never calls it, and that is not a hypothetical either: the helper is called
    at import time by each entry point, there are five of them, and the one
    that was forgotten would be the one that failed. Scanned rather than
    imported, because importing these runs them.
    """

    # Every file that can start the program: the three the launcher spawns, the
    # bundled dispatcher, and the two conveniences meant to be run directly.
    ENTRY_POINTS = (
        "tools/launch.py",
        "tools/channel_config.py",
        "tools/packaged_entry.py",
        "tools/ffmpeg_fetch.py",
    )

    def test_every_entry_point_prepares_the_certificates(self):
        root = Path(__file__).resolve().parents[1]
        missing = [name for name in self.ENTRY_POINTS
                   if "use_system_ca" not in (root / name).read_text(encoding="utf-8")]
        self.assertEqual(
            missing, [],
            "these can open an HTTPS connection but never call use_system_ca(); "
            "in a downloaded build the certificate path is the build machine's "
            "and every fetch fails with 'unable to get local issuer certificate'")

    def test_the_list_of_entry_points_is_not_empty_and_the_files_exist(self):
        """A scan over a list of paths that are all wrong would pass silently."""
        root = Path(__file__).resolve().parents[1]
        self.assertGreater(len(self.ENTRY_POINTS), 2)
        for name in self.ENTRY_POINTS:
            with self.subTest(name=name):
                self.assertTrue((root / name).is_file(), name)

    def test_the_scan_would_notice_a_new_offender(self):
        """The scan has to be able to fail, or it proves nothing."""
        root = Path(__file__).resolve().parents[1]
        probe = root / "tools" / "_probe_entry.py"
        probe.write_text("import urllib.request\n", encoding="utf-8")
        try:
            missing = ["tools/_probe_entry.py"] if "use_system_ca" not in probe.read_text(
                encoding="utf-8") else []
        finally:
            probe.unlink()
        self.assertEqual(missing, ["tools/_probe_entry.py"])


class RealSystemTests(unittest.TestCase):
    def test_the_known_paths_are_absolute_and_do_not_include_windows(self):
        """A relative path would resolve against the working directory.

        That is the data directory when this program runs, which is the user's
        own folder -- so a relative entry would look for a bundle there. Windows
        is absent because Python there reaches the system store through
        `load_default_certs` and there is no file to name.
        """
        for candidate in certs.SYSTEM_CA_FILES:
            self.assertTrue(candidate.startswith("/"), candidate)
            self.assertNotIn("windows", candidate.lower())

    def test_asking_for_the_authority_count_does_not_raise(self):
        """Whatever the platform, this answers. -1 for "cannot tell".

        Called on the start-up path of every entry point, so a platform where
        this raises would take the whole program down before it printed
        anything -- the failure this module exists to prevent.
        """
        self.assertGreaterEqual(certs.loaded_authorities(), -1)


if __name__ == "__main__":
    unittest.main()
