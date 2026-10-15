"""Offline boundary tests. No test starts Peekaboo or operates ChatGPT."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "bridge", ROOT / "skills/ask-chatgpt/chatgpt_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
LIMIT = 536_870_912


class UploadLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / "审查包.zip"
        # Sparse files exercise real stat() boundaries without writing 512 MiB.
        self.resize(1)

    def resize(self, size):
        with self.file.open("wb") as stream:
            stream.truncate(size)

    def main(self, extra=()):
        argv = ["bridge", "ask", "review", "--new-chat", "--attach", str(self.file), *extra]
        with mock.patch.object(bridge.sys, "argv", argv), \
                mock.patch.object(bridge.signal, "signal"), \
                mock.patch.object(bridge, "check_stop"), \
                mock.patch.object(bridge, "registered", side_effect=AssertionError("UI reached")) as ui:
            with self.assertRaises(SystemExit) as result:
                bridge.main()
            ui.assert_not_called()
        return str(result.exception.code)

    def test_real_byte_boundaries(self):
        self.assertEqual(bridge.MAX_CHATGPT_UPLOAD_SIZE_BYTES, LIMIT)
        for size in (LIMIT - 1, LIMIT):
            with self.subTest(size=size):
                self.resize(size)
                self.assertEqual(bridge.validate_attachment_size(self.file), size)
        self.resize(LIMIT + 1)
        with self.assertRaisesRegex(RuntimeError, "536,870,913 bytes.*536,870,912 bytes"):
            bridge.validate_attachment_size(self.file)

    def test_over_limit_cli_rejected_before_ui_registration(self):
        self.resize(LIMIT + 1)
        with mock.patch.object(bridge, "attachment_tokens") as estimate:
            error = self.main()
            estimate.assert_not_called()
        self.assertIn("Attachment upload size", error)
        self.assertIn("审查包.zip", error)
        self.assertIn("compressed file size", error)
        self.assertIn("Nothing was uploaded", error)
        self.assertIn("Reduce or split", error)

    def test_large_token_budget_cannot_bypass_upload_limit(self):
        self.resize(LIMIT + 1)
        self.assertIn("Attachment upload size", self.main(("--budget", "999999999")))

    def test_small_token_budget_does_not_change_size_error(self):
        self.resize(LIMIT + 1)
        self.assertIn("Attachment upload size", self.main(("--budget", "1")))

    def test_missing_attachment_fails_before_ui(self):
        self.file.unlink()
        self.assertIn("Cannot inspect attachment", self.main())

    def test_directory_fails_before_ui(self):
        self.file.unlink()
        self.file.mkdir()
        self.assertIn("not a regular file", self.main())

    def test_stat_error_is_actionable(self):
        with mock.patch.object(bridge.os, "stat", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(RuntimeError, "Cannot inspect attachment.*denied"):
                bridge.validate_attachment_size(self.file)

    def test_compressed_size_check_does_not_read_archive(self):
        with zipfile.ZipFile(self.file, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("code.txt", "a" * 1_000_000)
        disk_bytes = self.file.stat().st_size
        self.assertLess(disk_bytes, 2000)
        with mock.patch.object(bridge.zipfile, "ZipFile", side_effect=AssertionError("decompressed")):
            self.assertEqual(bridge.validate_attachment_size(self.file), disk_bytes)

    def test_non_zip_attachment_has_the_same_byte_guard(self):
        self.file = self.root / "code.txt"
        self.resize(LIMIT + 1)
        self.assertIn("Attachment upload size", self.main())

    def test_direct_ask_rejects_before_estimation_and_window_lookup(self):
        self.resize(LIMIT + 1)
        with mock.patch.object(bridge, "attachment_tokens") as estimate, \
                mock.patch.object(bridge, "main_window") as window:
            with self.assertRaisesRegex(RuntimeError, "Attachment upload size"):
                bridge.cmd_ask(SimpleNamespace(prompt="review", attach=str(self.file)))
            estimate.assert_not_called()
            window.assert_not_called()

    def test_attach_rejects_before_copy_or_ui(self):
        self.resize(LIMIT + 1)
        with mock.patch.object(bridge.shutil, "copy2") as copy, \
                mock.patch.object(bridge, "observe") as observe:
            with self.assertRaisesRegex(RuntimeError, "Attachment upload size"):
                bridge.attach(1, str(self.file))
            copy.assert_not_called()
            observe.assert_not_called()

    def test_staged_copy_is_rechecked_before_upload_menu(self):
        def growing_copy(src, dest):
            with open(dest, "wb") as stream:
                stream.truncate(LIMIT + 1)

        with mock.patch.object(bridge, "STATE_DIR", str(self.root / "state")), \
                mock.patch.object(bridge.shutil, "copy2", side_effect=growing_copy), \
                mock.patch.object(bridge, "observe") as observe, \
                mock.patch.object(bridge, "click") as click:
            with self.assertRaisesRegex(RuntimeError, "Attachment upload size"):
                bridge.attach(1, str(self.file))
            observe.assert_not_called()
            click.assert_not_called()

    def test_under_limit_cli_continues_to_execution(self):
        argv = ["bridge", "ask", "review", "--new-chat", "--attach", str(self.file)]
        with mock.patch.object(bridge.sys, "argv", argv), \
                mock.patch.object(bridge.signal, "signal"), \
                mock.patch.object(bridge, "check_stop"), \
                mock.patch.object(bridge, "registered", return_value=contextlib.nullcontext()) as register, \
                mock.patch.object(bridge, "cmd_ask") as ask:
            bridge.main()
            register.assert_called_once()
            ask.assert_called_once()

    def test_stop_switch_keeps_precedence(self):
        self.resize(LIMIT + 1)
        argv = ["bridge", "ask", "review", "--new-chat", "--attach", str(self.file)]
        with mock.patch.object(bridge.sys, "argv", argv), \
                mock.patch.object(bridge.signal, "signal"), \
                mock.patch.object(bridge, "check_stop", side_effect=bridge.Stopped("switched off")), \
                mock.patch.object(bridge, "validate_attachment_size") as validate, \
                mock.patch.object(bridge, "registered") as register, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                bridge.main()
            self.assertEqual(result.exception.code, 5)
            validate.assert_not_called()
            register.assert_not_called()

    def test_under_byte_limit_can_exceed_separate_token_budget(self):
        args = SimpleNamespace(prompt="review", attach=str(self.file),
                               conversation="test chat", budget=90_000)
        with mock.patch.object(bridge, "attachment_tokens", return_value=500_000), \
                mock.patch.object(bridge, "ATTACH_CONTEXT", 30_000), \
                mock.patch.object(bridge, "load_ledger", return_value={"test chat": {"tokens": 85_000, "marker": "previous-run"}}), \
                mock.patch.object(bridge, "main_window") as window:
            with self.assertRaisesRegex(bridge.BudgetExceeded, "local.*token budget"):
                bridge.cmd_ask(args)
            window.assert_not_called()

    def test_context_budget_keeps_exit_code_three(self):
        argv = ["bridge", "ask", "review", "--new-chat", "--attach", str(self.file)]
        with mock.patch.object(bridge.sys, "argv", argv), \
                mock.patch.object(bridge.signal, "signal"), \
                mock.patch.object(bridge, "check_stop"), \
                mock.patch.object(bridge, "registered", return_value=contextlib.nullcontext()), \
                mock.patch.object(bridge, "cmd_ask", side_effect=bridge.BudgetExceeded("local token budget")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit) as result:
                bridge.main()
            self.assertEqual(result.exception.code, 3)
            self.assertIn("budget:", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
