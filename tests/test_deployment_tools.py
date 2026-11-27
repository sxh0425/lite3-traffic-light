import base64
from contextlib import nullcontext
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
    plan = remote.plan(root, identity, request)
    request["approval"] = remote.digest(remote.canonical(plan))
    request["uploads"] = {name: base64.b64encode(content[name]) for name in plan["changes"]}
    return identity, request, plan


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "yidong"
        self.root.mkdir()
        self.meta = self.root / remote.METADATA
        if remote.sys.platform == "win32":
            lock = patch.object(remote, "deployment_lock", return_value=nullcontext())
            lock.start()
            self.addCleanup(lock.stop)

    def test_first_incremental_unchanged_and_retained(self):
        (self.root / "README.md").write_bytes(b"old project")
        (self.root / "src").mkdir()
        (self.root / "src/__init__.py").write_bytes(b"keep")
        files = {"src/preview.py": b"first", "src/__init__.py": b"keep"}
        identity, request, plan = preview(self.root, files)
        self.assertEqual(plan["changes"], {"src/preview.py": "ADD"})
        self.assertFalse(self.meta.exists())
        first = remote.apply(self.root, identity, request)
        identity, request, plan = preview(self.root, {"src/preview.py": b"second"})
        self.assertEqual(plan["retained"], ["src/__init__.py"])
        self.assertEqual(plan["changes"], {"src/preview.py": "MODIFY"})
        second = remote.apply(self.root, identity, request)
        self.assertEqual(second["uploaded"], 1)
        self.assertEqual(second["previous"], first["revision"])
        self.assertEqual((self.meta / second["revision"] / "before/src/preview.py").read_bytes(), b"first")
        self.assertEqual((self.root / "src/preview.py").read_bytes(), b"second")
        self.assertEqual((self.root / "src/__init__.py").read_bytes(), b"keep")
        self.assertEqual((self.root / "README.md").read_bytes(), b"old project")
        identity, request, _ = preview(self.root, {"src/preview.py": b"second"})
        self.assertEqual(remote.apply(self.root, identity, request)["uploaded"], 0)

    def test_remote_edit_and_missing_state_stop_apply(self):
        identity, request, _ = preview(self.root, {"src/preview.py": b"first"})
        remote.apply(self.root, identity, request)
        identity, request, _ = preview(self.root, {"src/preview.py": b"second"})
        (self.root / "src/preview.py").write_bytes(b"someone edited")
        with self.assertRaisesRegex(ValueError, "Remote file changed"):
            remote.apply(self.root, identity, request)
        (self.meta / remote.STATE_FILE).unlink()
        with self.assertRaisesRegex(ValueError, "Missing deployment state"):
            preview(self.root, {"src/preview.py": b"second"})

    def test_bad_upload_and_failed_tests_keep_previous_release(self):
        content = {**dict.fromkeys(remote.TEST_FILES, b"# test"), "src/preview.py": b"first"}
        identity, request, _ = preview(self.root, content)
        remote.apply(self.root, identity, request)
        before = remote.load_state(self.root)
        content["src/preview.py"] = b"next"
        identity, request, _ = preview(self.root, content, test=True)
        bad_request = {**request, "uploads": {"src/preview.py": base64.b64encode(b"incorrect")}}
        with self.assertRaisesRegex(ValueError, "hash or size mismatch"):
            remote.apply(self.root, identity, bad_request)
        with patch.object(remote, "visual_tests", side_effect=ValueError("test failed")):
            with self.assertRaisesRegex(ValueError, "test failed"):
                remote.apply(self.root, identity, request)
        self.assertEqual(remote.load_state(self.root), before)
        self.assertEqual((self.root / "src/preview.py").read_bytes(), b"first")

    def test_requires_existing_target_and_rejects_untracked_collision(self):
        with self.assertRaises(FileNotFoundError):
            preview(self.root / "nonexistent", {"src/preview.py": b"new"})
        (self.root / "src").mkdir()
        (self.root / "src/preview.py").write_bytes(b"existing")
        with self.assertRaisesRegex(ValueError, "Existing untracked file differs"):
            preview(self.root, {"src/preview.py": b"new"})
        self.assertFalse(self.meta.exists())

    def test_same_approval_resumes_an_interrupted_write(self):
        content = {"src/preview.py": b"first", "scripts/generate_simulated_data.py": b"second"}
        identity, request, _ = preview(self.root, content)
        link = remote.os.link

        def fail_second_file(source, dest):
            if dest.name == "generate_simulated_data.py":
                raise OSError("simulated interruption")
            return link(source, dest)

        with patch.object(remote.os, "link", side_effect=fail_second_file):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                remote.apply(self.root, identity, request)
        self.assertEqual((self.root / "src/preview.py").read_bytes(), b"first")
        with self.assertRaisesRegex(ValueError, "Incomplete deployment"):
            preview(self.root, content)
        remote.apply(self.root, identity, request)
        self.assertEqual((self.root / "scripts/generate_simulated_data.py").read_bytes(), b"second")
        self.assertNotIn("pending", remote.load_state(self.root))

    def test_result_protocol_and_local_verification(self):
        results = self.root / "results"
        results.mkdir()
        (results / "test.csv").write_bytes(b"state\nRED\n")
        out = io.BytesIO()
        with patch.object(remote.sys, "stdout", SimpleNamespace(buffer=out)):
            remote.fetch(self.root, {"files": ["test.csv"]})
        with tempfile.TemporaryDirectory() as temp:
            receive(SimpleNamespace(stdout=io.BytesIO(out.getvalue())), Path(temp), ["test.csv"])
            self.assertEqual((Path(temp) / "0").read_bytes(), b"state\nRED\n")
        with self.assertRaisesRegex(ValueError, "Invalid result path"):
            remote.fetch(self.root, {"files": ["../README.md"]})


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
