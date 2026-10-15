"""Exercise review packaging/state/reporting with synthetic answers, without uploads."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review", ROOT / "skills/chatgpt-review/review.py")
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class ReviewFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        patcher = mock.patch.object(review, "HOME_STATE", str(self.base / "state"))
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.root / "app.py").write_text("def ratio(a, b):\n    return a / b\n")

    def call(self, fn, **values):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            fn(SimpleNamespace(root=str(self.root), **values))
        return output.getvalue()

    def init(self, **values):
        options = dict(name=None, force=False, context="test", focus=None, include=None,
                       source_zip=None, brief=None, diff=None)
        options.update(values)
        self.call(review.cmd_init, **options)

    def pack(self, **values):
        options = dict(chunk_tokens=0, include_locks=False, max_file_kb=512)
        options.update(values)
        return json.loads(self.call(review.cmd_pack, **options))

    def ingest(self, data, part=None):
        answer = self.base / "answer.md"
        answer.write_text("```json\n" + json.dumps(data) + "\n```\n")
        self.call(review.cmd_ingest, answer=str(answer), part=part)

    def test_pack_next_ingest_fix_recheck_report(self):
        self.init()
        first = self.pack()
        self.assertEqual(first["files"], 1)
        nxt = json.loads(self.call(review.cmd_next))
        with zipfile.ZipFile(nxt["zip"]) as archive:
            self.assertEqual(archive.read("project/app.py"), (self.root / "app.py").read_bytes())
            self.assertIsNone(archive.testzip())
        self.assertIn("1 个文件", Path(nxt["prompt"]).read_text())
        self.ingest({"summary": "division by zero", "findings": [
            {"id": "F1", "file": "app.py", "line": 2, "severity": "medium", "title": "zero divisor"}]})
        self.call(review.cmd_set, ids=["F1"], verdict="confirmed", status="fixed", note="handle zero")
        (self.root / "app.py").write_text("def ratio(a, b):\n    return a / b if b else None\n")
        self.assertEqual(self.pack()["round"], 2)
        self.ingest({"summary": "fixed", "fix_checks": [{"id": "F1", "status": "fixed"}], "findings": []})
        state = review.load(str(self.root))
        self.assertEqual(state["findings"][0]["status"], "verified")
        report = self.call(review.cmd_report, summary="verified").strip()
        self.assertIn("本地估算", Path(report).read_text())
        self.assertIn("非实际上下文用量", Path(report).read_text())
        self.assertIsNone(self.pack()["round"])

    def test_filters_are_distinct_from_upload_size(self):
        (self.root / "exact.py").write_bytes(b"a" * (512 * 1024))
        (self.root / "over.py").write_bytes(b"a" * (512 * 1024 + 1))
        (self.root / ".env").write_text("PASSWORD=synthetic\n")
        (self.root / "binary.bin").write_bytes(b"a\x00b")
        (self.root / "linked.py").symlink_to(self.root / "app.py")
        self.init()
        result = self.pack()
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["possible_secrets_excluded"], [".env"])
        self.assertEqual(result["skipped"]["too large (512 KB)"], 1)
        state = review.load(str(self.root))
        self.assertEqual(set(state["rounds"][0]["parts"][0]["files"]), {"app.py", "exact.py"})

    def test_initial_chunking_uses_estimated_tokens(self):
        (self.root / "b.py").write_text("b = 1\n" * 100)
        self.init()
        result = self.pack(chunk_tokens=100)
        self.assertEqual(result["total_parts"], 2)
        self.assertEqual(result["oversized_files"], ["b.py"])

    def test_invalid_answer_does_not_mark_part_completed(self):
        self.init()
        self.pack()
        answer = self.base / "answer.md"
        answer.write_text("incomplete reply without JSON")
        with self.assertRaises(SystemExit):
            self.call(review.cmd_ingest, answer=str(answer), part=None)
        part = review.load(str(self.root))["rounds"][0]["parts"][0]
        self.assertNotIn("answer", part)

    def test_source_zip_traversal_does_not_escape_source(self):
        source = self.base / "source.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("safe.py", "x = 1\n")
            archive.writestr("../outside.py", "bad\n")
        self.init(source_zip=str(source))
        state = review.load(str(self.root))
        extracted = Path(state["source"])
        self.assertTrue((extracted / "safe.py").is_file())
        self.assertFalse((extracted.parent / "outside.py").exists())
        self.assertEqual(self.pack()["files"], 1)

    # Confirmed review findings outside the upload-byte guard change. Keep desired
    # behavior here: removing expectedFailure after fixing each bug makes it a gate.
    @unittest.expectedFailure
    def test_existing_partition_respects_new_chunk_limit(self):
        (self.root / "app.py").write_text("x = 1\n" * 400)
        (self.root / "b.py").write_text("y = 2\n" * 400)
        self.init()
        self.pack()
        result = self.pack(chunk_tokens=1000)
        self.assertTrue(all(part["est_tokens"] <= 1000 for part in result["parts"]), result)

    @unittest.expectedFailure
    def test_unread_file_prevents_completion(self):
        self.init()
        self.pack()
        self.ingest({"summary": "not reviewed", "coverage": {"deep_read": 0},
                     "unread_files": ["app.py"], "findings": []})
        self.assertFalse(json.loads(self.call(review.cmd_next))["done"])

    @unittest.expectedFailure
    def test_secret_deleted_from_source_is_not_uploaded_in_diff(self):
        synthetic_secret = "sk-" + "A" * 24
        (self.root / "app.py").write_text("api_key = None\n")
        diff = self.base / "changes.diff"
        diff.write_text('diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n'
                        '@@ -1 +1 @@\n-api_key = "' + synthetic_secret + '"\n+api_key = None\n')
        self.init(diff=str(diff))
        self.pack()
        nxt = json.loads(self.call(review.cmd_next))
        with zipfile.ZipFile(nxt["zip"]) as archive:
            for name in archive.namelist():
                self.assertNotIn(synthetic_secret.encode(), archive.read(name), name)


if __name__ == "__main__":
    unittest.main()
