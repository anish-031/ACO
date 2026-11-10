import unittest
from types import SimpleNamespace
from unittest.mock import patch

from acobot import deps


class DepsTests(unittest.TestCase):
    def test_ensure_does_nothing_when_imports_work(self):
        with patch.object(deps, "missing", return_value=[]):
            self.assertEqual(deps.ensure(), [])

    def test_ensure_installs_cryptography_when_it_is_missing(self):
        calls = []

        def runner(specs, extra=()):
            calls.append((tuple(specs), tuple(extra)))
            return SimpleNamespace(returncode=0, stderr="")

        with patch.object(deps, "missing", side_effect=[["cryptography>=41"], []]):
            installed = deps.ensure(runner=runner)
        self.assertEqual(installed, ["cryptography>=41"])
        self.assertEqual(calls, [(("cryptography>=41",), ())])

    def test_ensure_retries_when_python_blocks_pip(self):
        calls = []

        def runner(specs, extra=()):
            calls.append(tuple(extra))
            if extra:
                return SimpleNamespace(returncode=0, stderr="")
            return SimpleNamespace(returncode=1, stderr="externally-managed-environment")

        with patch.object(deps, "missing", side_effect=[["cryptography>=41"], []]):
            deps.ensure(runner=runner)
        self.assertEqual(calls, [(), ("--break-system-packages",)])

    def test_ensure_stops_when_pip_cannot_install(self):
        def runner(specs, extra=()):
            return SimpleNamespace(returncode=1, stderr="no network")

        with patch.object(deps, "missing", return_value=["cryptography>=41"]):
            with self.assertRaises(SystemExit) as raised:
                deps.ensure(runner=runner)
        self.assertIn("cryptography", str(raised.exception))
        self.assertIn("requirements.txt", str(raised.exception))
