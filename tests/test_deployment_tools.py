import base64
import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _remote as remote
from fetch_results import destination, receive


def preview(root, content, test=False):
    identity = {"root": str(root), "id": "local-test"}
    request = {"local": {name: remote.digest(data) for name, data in content.items()}, "test": test}
    plan, _ = remote.plan(root, identity, request)
    request["approval"] = remote.digest(remote.canonical(plan))
    request["uploads"] = {name: base64.b64encode(content[name]) for name in plan["changes"]}
    return identity, request, plan


@unittest.skipUnless(importlib.util.find_spec("fcntl"), "Linux release activation test")
class ReleaseTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "yidong-vision"

    def test_first_incremental_unchanged_and_retained(self):
        files = {"README.md": b"first", "src/__init__.py": b"keep"}
        identity, request, plan = preview(self.root, files)
        self.assertEqual(plan["changes"], dict.fromkeys(files, "ADD"))
        self.assertFalse(self.root.exists())
        first = remote.apply(self.root, identity, request)
        old = self.root / ".releases" / first["revision"]
        identity, request, plan = preview(self.root, {"README.md": b"second"})
        self.assertEqual(plan["retained"], ["src/__init__.py"])
        self.assertEqual(plan["changes"], {"README.md": "MODIFY"})
        second = remote.apply(self.root, identity, request)
        self.assertEqual(second["uploaded"], 1)
        self.assertEqual(second["previous"], first["revision"])
        self.assertEqual((old / "README.md").read_bytes(), b"first")
        self.assertEqual((self.root / "current/src/__init__.py").read_bytes(), b"keep")
        identity, request, _ = preview(self.root, {"README.md": b"second"})
        self.assertEqual(remote.apply(self.root, identity, request)["uploaded"], 0)

    def test_remote_edit_and_missing_pointer_stop_apply(self):
        identity, request, _ = preview(self.root, {"README.md": b"first"})
        remote.apply(self.root, identity, request)
        identity, request, _ = preview(self.root, {"README.md": b"second"})
        (self.root / "current/README.md").write_bytes(b"someone edited")
        with self.assertRaisesRegex(ValueError, "Remote file changed"):
            remote.apply(self.root, identity, request)
        (self.root / "current").unlink()
        with self.assertRaisesRegex(ValueError, "invalid current"):
            preview(self.root, {"README.md": b"second"})

    def test_bad_upload_and_failed_tests_keep_previous_release(self):
        content = {**dict.fromkeys(remote.TEST_FILES, b"# test"), "README.md": b"first"}
        identity, request, _ = preview(self.root, content)
        remote.apply(self.root, identity, request)
        before = remote.current(self.root)[0]
        content["README.md"] = b"next"
        identity, request, _ = preview(self.root, content, test=True)
        bad_request = {**request, "uploads": {"README.md": base64.b64encode(b"incorrect")}}
        with self.assertRaisesRegex(ValueError, "hash or size mismatch"):
            remote.apply(self.root, identity, bad_request)
        with patch.object(remote, "visual_tests", side_effect=ValueError("test failed")):
            with self.assertRaisesRegex(ValueError, "test failed"):
                remote.apply(self.root, identity, request)
        self.assertEqual(remote.current(self.root)[0], before)

    def test_unmanaged_directory_rejected(self):
        self.root.mkdir()
        (self.root / "README.md").write_bytes(b"existing")
        with self.assertRaises(FileNotFoundError):
            preview(self.root, {"README.md": b"new"})

    def test_result_protocol_and_local_verification(self):
        identity, request, _ = preview(self.root, {"README.md": b"first"})
        remote.apply(self.root, identity, request)
        results = self.root / "results"
        results.mkdir()
        (results / "test.csv").write_bytes(b"state\nRED\n")
        out = io.BytesIO()
        with patch.object(remote.sys, "stdout", SimpleNamespace(buffer=out)):
            remote.fetch(self.root, {"files": ["test.csv"]})
        with tempfile.TemporaryDirectory() as temp:
            receive(SimpleNamespace(stdout=io.BytesIO(out.getvalue())), Path(temp), ["test.csv"])
            self.assertEqual((Path(temp) / "0").read_bytes(), b"state\nRED\n")
        (results / "leak.csv").symlink_to(self.root / "current/README.md")
        with self.assertRaisesRegex(ValueError, "Unsafe file"):
            remote.fetch(self.root, {"files": ["leak.csv"]})


class ResultTest(unittest.TestCase):
    def test_rejects_invalid_paths_and_local_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("../outside.csv", "/tmp/absolute.csv", "folder/*.jpg", "SSH.key"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    destination(root, name)
            (root / "sample.csv").touch()
            with self.assertRaisesRegex(ValueError, "exists"):
                destination(root, "sample.csv")

    def test_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            header = [{"name": "sample.csv", "size": 3, "sha256": remote.digest(b"yes")}]
            proc = SimpleNamespace(stdout=io.BytesIO(remote.canonical(header) + b"\nno!"))
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                receive(proc, Path(directory), ["sample.csv"])
