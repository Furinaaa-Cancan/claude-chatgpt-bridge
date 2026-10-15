"""Safety regressions using synthetic UI states; never drives the desktop."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock

from test_upload_limits import bridge as b, ROOT


class ComputerUse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for attr, value in (("STATE_DIR", str(self.root)), ("LEDGER", str(self.root / "ledger.json"))):
            patch = mock.patch.object(b, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        # A missed mock must fail the test instead of operating an application.
        for attr in ("pk", "raise_chatgpt", "observe"):
            patch = mock.patch.object(b, attr, side_effect=AssertionError("Unexpected desktop access: " + attr))
            patch.start()
            self.addCleanup(patch.stop)

    def empty_chat(self):
        return NS(input={"id": "input"}, input_text="", snapshot="s", title=b.NEW_CHAT_TITLE,
                  has_messages=False, busy=False,
                  all=lambda **_: [{"label": "Switch mode, current mode: ChatGPT"}])

    def test_idle_query_failure_stops(self):
        for response in (NS(stdout="", returncode=0), NS(stdout='"HIDIdleTime" = 123', returncode=1)):
            with self.subTest(response=response), mock.patch.object(b.subprocess, "run", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "Cannot determine user idle"):
                    b.user_idle_seconds()

    def test_idle_query_timeout_stops(self):
        with mock.patch.object(b.subprocess, "run", side_effect=subprocess.TimeoutExpired("ioreg", 5)):
            with self.assertRaises(RuntimeError):
                b.user_idle_seconds()

    def test_valid_idle_query(self):
        with mock.patch.object(b.subprocess, "run", return_value=NS(stdout='"HIDIdleTime" = 31000000000', returncode=0)):
            self.assertEqual(b.user_idle_seconds(), 31)

    def test_recent_human_input_always_waits(self):
        with mock.patch.dict(os.environ, {"CHATGPT_BRIDGE_USER_IDLE": "30"}), \
                mock.patch.object(b, "check_stop"), \
                mock.patch.object(b, "user_idle_seconds", side_effect=[0.1, 31]), \
                mock.patch.object(b.time, "sleep") as sleep, mock.patch.object(b, "log"):
            b.wait_until_user_away()
            sleep.assert_called_once_with(5)

    def test_prompt_preserves_significant_whitespace(self):
        prompt = "if ready:\n    run()\n"
        for actual in ("if ready:\nrun()\n", "if ready:\n\trun()\n", prompt.rstrip()):
            with self.subTest(actual=actual), mock.patch.object(b, "raise_chatgpt"), \
                    mock.patch.object(b, "pk"), mock.patch.object(b.time, "sleep"), \
                    mock.patch.object(b, "observe", side_effect=[
                        self.empty_chat(), NS(input_text=actual)]):
                with self.assertRaisesRegex(RuntimeError, "full prompt"):
                    b.set_prompt(1, prompt)

    def test_only_line_endings_are_normalized(self):
        prompt = "if ready:\n    run()\n"
        with mock.patch.object(b, "raise_chatgpt"), mock.patch.object(b, "pk"), \
                mock.patch.object(b.time, "sleep"), mock.patch.object(b, "observe", side_effect=[
                    self.empty_chat(),
                    NS(input_text=prompt.replace("\n", "\r\n"))]):
            b.set_prompt(1, prompt)

    def test_ui_does_not_strip_draft_whitespace(self):
        ui = b.UI({"snapshot_id": "s", "ui_elements": [
            {"ax_role": "AXTextArea", "label": "Message", "value": "  code\n"}]})
        self.assertEqual(ui.input_text, "  code\n")

    def test_missing_and_wrong_mode_rejected(self):
        for switches in ([], [{"label": "Switch mode, current mode: Work"}],
                         [{"label": "Switch mode, current mode: ChatGPT"}] * 2):
            with self.subTest(switches=switches):
                with self.assertRaises(RuntimeError):
                    b.require_chat_ui(NS(all=lambda **_: switches))
        b.require_chat_ui(NS(all=lambda **_: [{"label": "Switch mode, current mode: ChatGPT"}]))

    def args(self):
        return NS(prompt="review", attach=None, conversation=None, new_chat=True, title=None,
                  budget=90_000, out=None, wait_idle=False, timeout=10, poll=1)

    def ask_patches(self, stack):
        for attr, value in (("load_ledger", {}), ("main_window", 1),
                            ("observe", NS(input_text="", busy=False)), ("previous_app", None),
                            ("open_new_chat", None), ("restore", None), ("set_prompt", None),
                            ("require_chat_mode", None), ("pin_title", "Review"), ("log", None)):
            stack.enter_context(mock.patch.object(b, attr, return_value=value))
        stack.enter_context(mock.patch.object(b, "ui_step", side_effect=lambda *_: contextlib.nullcontext()))

    def test_mode_failure_precedes_upload(self):
        args = self.args()
        source = self.root / "code.txt"
        source.write_text("code")
        args.attach = str(source)
        with contextlib.ExitStack() as stack:
            self.ask_patches(stack)
            stack.enter_context(mock.patch.object(b, "require_chat_mode", side_effect=RuntimeError("unknown mode")))
            upload = stack.enter_context(mock.patch.object(b, "attach"))
            with self.assertRaisesRegex(RuntimeError, "unknown mode"):
                b.cmd_ask(args)
            upload.assert_not_called()

    def test_uncertain_send_persists_identity_and_never_retries(self):
        args = self.args()
        with contextlib.ExitStack() as stack:
            self.ask_patches(stack)
            send = stack.enter_context(mock.patch.object(b, "send", side_effect=RuntimeError("missing acknowledgment")))
            collect = stack.enter_context(mock.patch.object(b, "collect_answer"))
            with self.assertRaisesRegex(RuntimeError, "do not resend"):
                b.cmd_ask(args)
            send.assert_called_once()
            collect.assert_not_called()
        receipt = json.loads(next((self.root / "requests").glob("*.json")).read_text())
        self.assertEqual(receipt["phase"], "delivery_unknown")
        self.assertEqual(receipt["marker"], args.marker)
        self.assertIn(args.marker, send.call_args.args[1])

    def test_every_send_gets_unique_identity_and_completed_receipt(self):
        identities = []
        for _ in range(2):
            args = self.args()
            with contextlib.ExitStack() as stack:
                self.ask_patches(stack)
                stack.enter_context(mock.patch.object(b, "send"))
                stack.enter_context(mock.patch.object(b, "collect_answer", side_effect=lambda *a: (args.marker, "Review")))
                stack.enter_context(mock.patch.object(b, "record"))
                b.cmd_ask(args)
            identities.append(args.marker)
            receipt = json.loads((self.root / "requests" / (args.marker + ".json")).read_text())
            self.assertEqual(receipt["phase"], "completed")
        self.assertEqual(len(set(identities)), 2)

    def test_same_title_requires_identity_before_copy(self):
        with mock.patch.object(b, "wait_for_answer", return_value="Review"), \
                mock.patch.object(b, "ui_step", side_effect=lambda *_: contextlib.nullcontext()), \
                mock.patch.object(b, "observe", return_value=NS(title="Review", busy=False)), \
                mock.patch.object(b, "marker_in_chat", return_value=None), \
                mock.patch.object(b, "open_conversation", side_effect=RuntimeError("mismatch")), \
                mock.patch.object(b, "find_by_marker", return_value=None), \
                mock.patch.object(b, "copy_answer") as copy, mock.patch.object(b, "log"):
            with self.assertRaises(RuntimeError):
                b.collect_answer(1, self.args(), None, "Review", "our-run")
            copy.assert_not_called()

    def test_wrong_answer_marker_is_never_returned(self):
        with mock.patch.object(b, "wait_for_answer", return_value="Review"), \
                mock.patch.object(b, "ui_step", side_effect=lambda *_: contextlib.nullcontext()), \
                mock.patch.object(b, "observe", return_value=NS(title="Review", busy=False)), \
                mock.patch.object(b, "marker_in_chat", return_value=True), \
                mock.patch.object(b, "find_by_marker", return_value="Review"), \
                mock.patch.object(b, "copy_answer", return_value="wrong answer"), mock.patch.object(b, "log"):
            with self.assertRaisesRegex(RuntimeError, "Could not copy"):
                b.collect_answer(1, self.args(), None, "Review", "our-run")

    def test_matching_answer_marker_is_returned(self):
        with mock.patch.object(b, "wait_for_answer", return_value="Review"), \
                mock.patch.object(b, "ui_step", side_effect=lambda *_: contextlib.nullcontext()), \
                mock.patch.object(b, "observe", return_value=NS(title="Review", busy=False)), \
                mock.patch.object(b, "marker_in_chat", return_value=True), \
                mock.patch.object(b, "copy_answer", return_value="answer: our-run"), mock.patch.object(b, "log"):
            self.assertEqual(b.collect_answer(1, self.args(), None, "Review", "our-run"),
                             ("answer: our-run", "Review"))

    def test_same_title_cannot_bypass_open_identity_check(self):
        with mock.patch.object(b, "observe", return_value=NS(title="Review")), \
                mock.patch.object(b, "marker_in_chat", return_value=None), \
                mock.patch.object(b, "conversation_rows", return_value=[]):
            with self.assertRaises(RuntimeError):
                b.open_conversation(1, "Review", "our-run")

    def test_click_resolves_new_bounds_after_wait(self):
        old = {"_window": 1, "ax_role": "AXButton", "label": "Send",
               "bounds": {"x": 0, "y": 0, "width": 10, "height": 10}}
        new = {**old, "bounds": {"x": 100, "y": 200, "width": 10, "height": 10}}
        with mock.patch.object(b, "raise_chatgpt"), \
                mock.patch.object(b, "observe", return_value=NS(all=lambda **_: [new])), \
                mock.patch.object(b, "pk") as pk:
            b.click(old)
            self.assertIn("105,205", pk.call_args.args)
            self.assertTrue(pk.call_args.kwargs["prepared"])

    def test_ambiguous_click_target_stops(self):
        old = {"_window": 1, "ax_role": "AXButton", "label": "Review"}
        with mock.patch.object(b, "raise_chatgpt"), \
                mock.patch.object(b, "observe", return_value=NS(all=lambda **_: [old, old])), \
                mock.patch.object(b, "pk") as pk:
            with self.assertRaises(RuntimeError):
                b.click(old)
            pk.assert_not_called()

    def test_new_chat_target_rejects_existing_messages(self):
        ui = self.empty_chat()
        ui.has_messages = True
        with self.assertRaisesRegex(RuntimeError, "empty new chat"):
            b.require_target(ui)

    def test_existing_chat_target_requires_marker(self):
        with mock.patch.object(b, "shows_marker", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "identity"):
                b.require_target(self.empty_chat(), "previous-run")

    def test_blank_composer_alone_does_not_confirm_send(self):
        def wait(win, predicate, timeout, description):
            if description == "Send to become available":
                return {"label": "Send"}
            # Simulate navigation to another blank chat after clicking Send.
            self.assertFalse(predicate(NS(busy=False, input_text="")))
            raise RuntimeError("delivery is unknown")

        with mock.patch.object(b, "wait_for", side_effect=wait), \
                mock.patch.object(b, "click"), mock.patch.object(b, "log"), \
                mock.patch.object(b, "shows_marker", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "unknown"):
                b.send(1, "prompt", "our-run")

    def test_changed_prompt_is_rejected_at_final_send_target(self):
        def click(button, resolve):
            ui = self.empty_chat()
            ui.input_text = "changed by user"
            resolve(ui)

        with mock.patch.object(b, "wait_for", return_value={"label": "Send"}), \
                mock.patch.object(b, "click", side_effect=click), mock.patch.object(b, "log"):
            with self.assertRaisesRegex(RuntimeError, "Prompt changed"):
                b.send(1, "original", "our-run")

    def test_distinct_chats_with_same_title_block_future_continuation(self):
        with mock.patch.object(b, "emit"), mock.patch.object(b, "log"):
            for marker in ("run-one", "run-two"):
                b.record("Review", 10, "answer", NS(marker=marker, out=None, budget=90_000))
        self.assertTrue(b.load_ledger()["Review"]["ambiguous"])
        args = self.args()
        args.conversation = "Review"
        args.new_chat = False
        with self.assertRaisesRegex(RuntimeError, "multiple conversations"):
            b.cmd_ask(args)

    def test_verified_followup_and_recovery_do_not_make_title_ambiguous(self):
        with mock.patch.object(b, "emit"), mock.patch.object(b, "log"):
            b.record("Review", 10, "answer", NS(marker="first", out=None, budget=90_000))
            args = NS(marker="second", previous_marker="first", out=None, budget=90_000)
            b.record("Review", 10, "answer", args)
            b.record("Review", 10, "answer", args)
        entry = b.load_ledger()["Review"]
        self.assertEqual(entry["turns"], 2)
        self.assertFalse(entry.get("ambiguous"))

    def test_wait_uses_request_receipt(self):
        args = self.args()
        args.marker = "bridge-run-" + "a" * 32
        args.previous_marker = "previous"
        args.conversation = "Review"
        args.prompt_file = None
        b.write_request(args, "delivery_unknown")
        args.previous_marker = None
        with mock.patch.object(b, "previous_app", return_value=None), \
                mock.patch.object(b, "main_window", return_value=1), \
                mock.patch.object(b, "restore"), \
                mock.patch.object(b, "collect_answer", return_value=(args.marker, "Review")), \
                mock.patch.object(b, "record") as record:
            b.cmd_wait(args)
            record.assert_called_once()
        self.assertEqual(args.previous_marker, "previous")
        receipt = json.loads((self.root / "requests" / (args.marker + ".json")).read_text())
        self.assertEqual(receipt["phase"], "completed")


class LedgerConcurrency(unittest.TestCase):
    def test_parallel_processes_preserve_all_results(self):
        # Slow each read to make a missing read/modify/write lock lose updates.
        code = '''
import importlib.util,sys,time
from types import SimpleNamespace
s=importlib.util.spec_from_file_location('bridge',sys.argv[1]);b=importlib.util.module_from_spec(s);s.loader.exec_module(b)
b.STATE_DIR=sys.argv[2];b.LEDGER=b.STATE_DIR+'/ledger.json'
load=b.load_ledger
def slow_load():
 value=load();time.sleep(0.05);return value
b.load_ledger=slow_load
b.emit=lambda *a:None;b.log=lambda *a:None
b.record(sys.argv[3],10,'answer',SimpleNamespace(marker=sys.argv[3],budget=90000,out=None))
'''
        with tempfile.TemporaryDirectory() as tmp:
            processes = [subprocess.Popen([sys.executable, "-c", code,
                         str(ROOT / "skills/ask-chatgpt/chatgpt_bridge.py"), tmp, "chat-" + str(i)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(6)]
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stdout + stderr)
            data = json.loads((Path(tmp) / "ledger.json").read_text())
            self.assertEqual(len(data), 6)
            self.assertTrue(all(entry["turns"] == 1 for entry in data.values()))


if __name__ == "__main__":
    unittest.main()
