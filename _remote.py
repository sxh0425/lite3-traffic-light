"""Deployment protocol. OpenSSH runs this helper on the selected Jetson."""

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import uuid


ROOT_NAME = "yidong"
METADATA = ".yidong-vision-deploy"
STATE_FILE = "state.json"
MAX_SOURCE = 1024 * 1024
SOURCE_FILES = (
    "configs/traffic_light.yaml",
    "src/__init__.py", "src/camera_reader.py", "src/light_state.py", "src/preview.py",
    "scripts/__init__.py", "scripts/generate_simulated_data.py",
    "tests/test_camera_reader.py", "tests/test_light_state.py",
)
TEST_FILES = ("tests/test_camera_reader.py", "tests/test_light_state.py")
RESULT_SUFFIXES = {".csv", ".json", ".txt", ".md", ".png", ".jpg", ".jpeg", ".mp4", ".avi", ".mkv"}
REVISION = re.compile(r"[0-9a-f]{32}")
HEX = re.compile(r"[0-9a-f]{64}")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("ascii")


def file_hash(path):
    with path.open("rb") as stream:
        h = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
        return h.hexdigest()


def checked(path, kind, missing=False):
    """Reject symlinks at every path component, including dangling links."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if missing:
            return False
        raise
    test = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    require(test(mode), f"Unsafe {kind}: {path}")
    require(path.resolve() == path, f"Symlink in path: {path}")
    return True


def target():
    import pwd

    model = Path("/proc/device-tree/model").read_bytes().rstrip(b"\0").decode()
    require(os.uname().machine == "aarch64" and "Jetson Xavier NX" in model,
            f"Unexpected board: {model}")
    account = pwd.getpwuid(os.getuid())
    home = Path(account.pw_dir)
    checked(home, "directory")
    require(os.getuid() != 0 and Path.home() == home, "Unexpected remote account/home")
    root = home / ROOT_NAME
    checked(root, "directory")
    require(root.stat().st_uid == os.getuid(), f"Target owner mismatch: {root}")
    identity = {"host": socket.gethostname(), "model": model, "user": account.pw_name,
                "root": str(root),
                "id": digest(Path("/etc/machine-id").read_bytes())}
    return root, identity


def source_manifest(data):
    require(isinstance(data, dict) and set(data) <= set(SOURCE_FILES)
            and all(isinstance(h, str) and HEX.fullmatch(h) for h in data.values()),
            "Invalid source manifest")
    return data


def source_hash(root, name):
    path = root / name
    if not checked(path.parent, "directory", missing=True) or not checked(path, "file", missing=True):
        return None
    require(path.stat().st_size <= MAX_SOURCE, f"Remote source exceeds 1 MiB: {name}")
    return file_hash(path)


def verify_sources(root, manifest):
    for name, expected in manifest.items():
        require(source_hash(root, name) == expected, f"Remote file changed or missing: {name}")


def load_state(root):
    checked(root, "directory")
    meta = root / METADATA
    empty = {"protocol": 2, "revision": None, "manifest": {}}
    if not checked(meta, "directory", missing=True):
        return empty
    state_file = meta / STATE_FILE
    if not checked(state_file, "file", missing=True):
        require(not any(meta.iterdir()), f"Missing deployment state: {meta}")
        return empty
    state = json.loads(state_file.read_bytes())
    revision = state.get("revision")
    require(state.get("protocol") == 2 and (revision is None or REVISION.fullmatch(revision)),
            "Invalid deployment state")
    source_manifest(state["manifest"])
    return state


def plan(root, identity, request):
    state = load_state(root)
    require("pending" not in state, "Incomplete deployment: re-run the same approved --apply command")
    tracked = state["manifest"]
    verify_sources(root, tracked)
    new = source_manifest(request["local"])
    require(type(request["test"]) is bool, "Invalid test flag")
    old = dict(tracked)
    changes = {}
    for name, expected in new.items():
        existing = old.get(name) if name in tracked else source_hash(root, name)
        if existing is None:
            changes[name] = "ADD"
            continue
        require(name in tracked or existing == expected,
                f"Existing untracked file differs: {name}")
        old[name] = existing
        if existing != expected:
            changes[name] = "MODIFY"
    if request["test"]:
        require(set(TEST_FILES) <= (old.keys() | new.keys()), "Visual tests missing")
    return {"protocol": 2, "target": identity, "revision": state["revision"],
            "remote": old, "local": new, "changes": changes,
            "retained": sorted(old.keys() - new.keys()), "test": request["test"]}


def visual_tests(release):
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(key, None)
    run = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "--tb=short",
         "-p", "no:cacheprovider", "-o", "addopts=", "--basetemp",
         str(release.parent / "test-tmp"), *TEST_FILES],
        cwd=release, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", timeout=120,
    )
    lines = [line for line in run.stdout.splitlines() if line.strip()]
    require(run.returncode == 0, "Visual tests failed:\n" + "\n".join(lines[-35:]))
    return lines[-1] if lines else "Visual tests passed"


@contextmanager
def deployment_lock(meta):
    import fcntl

    fd = os.open(meta, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def save_state(meta, state):
    file = meta / (".state-" + uuid.uuid4().hex)
    with file.open("xb") as stream:
        stream.write(canonical(state))
    os.replace(file, meta / STATE_FILE)


def stage_sources(root, release, info, blobs):
    manifest = {**info["remote"], **info["local"]}
    after = release / "after"
    after.mkdir(parents=True, mode=0o700)
    for name in manifest:
        path = after / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name in blobs:
            path.write_bytes(blobs[name])
            if name in info["remote"]:
                backup = release / "before" / name
                backup.parent.mkdir(parents=True, exist_ok=True)
                require(source_hash(root, name) == info["remote"][name], f"Remote file changed: {name}")
                shutil.copy2(root / name, backup)
                require(file_hash(backup) == info["remote"][name], f"Backup verification failed: {name}")
        else:
            require(source_hash(root, name) == manifest[name], f"Remote file changed: {name}")
            shutil.copyfile(root / name, path)
    verify_sources(after, manifest)
    summary = visual_tests(after) if info["test"] else None
    verify_sources(after, manifest)
    return summary


def finish(root, meta, pending):
    info = pending["plan"]
    revision = pending["revision"]
    require(REVISION.fullmatch(revision), "Invalid pending revision")
    release = meta / revision
    after = release / "after"
    manifest = {**info["remote"], **info["local"]}
    verify_sources(after, manifest)
    for name, expected in manifest.items():
        actual = source_hash(root, name)
        allowed = (info["remote"].get(name), expected) if name in info["changes"] else (expected,)
        require(actual in allowed, f"Remote file changed during deployment: {name}")
    # Publish files individually. A recorded plan can resume if the connection drops.
    for name, action in info["changes"].items():
        old = info["remote"].get(name)
        expected = manifest[name]
        if action == "MODIFY":
            require(source_hash(release / "before", name) == old, f"Backup changed: {name}")
        actual = source_hash(root, name)
        if actual == expected:
            continue
        require(actual == old, f"Remote file changed during deployment: {name}")
        dest = root / name
        dest.parent.mkdir(exist_ok=True)
        checked(dest.parent, "directory")
        temp = release / (".publish-" + uuid.uuid4().hex)
        shutil.copyfile(after / name, temp)
        if action == "ADD":
            os.link(temp, dest)
            temp.unlink()  # Only remove this run's temporary name.
        else:
            shutil.copymode(dest, temp)
            os.replace(temp, dest)
    verify_sources(root, manifest)
    save_state(meta, {"protocol": 2, "revision": revision, "manifest": manifest})
    return {"uploaded": len(info["changes"]), "revision": revision,
            "previous": info["revision"], "tests": pending["tests"]}


def apply(root, identity, request):
    state = load_state(root)
    pending = state.get("pending")
    info = pending["plan"] if pending else plan(root, identity, request)
    require(request["approval"] == digest(canonical(info))
            and info["target"] == identity and info["local"] == request["local"]
            and info["test"] == request["test"],
            ("Incomplete deployment: re-run the original approved --apply command"
             if pending else "Preview changed: run dry-run again"))
    uploads = request["uploads"]
    require(uploads.keys() == info["changes"].keys(), "Upload set differs from preview")
    blobs = {name: base64.b64decode(data, validate=True) for name, data in uploads.items()}
    for name, data in blobs.items():
        require(len(data) <= MAX_SOURCE and digest(data) == info["local"][name],
                f"Upload hash or size mismatch: {name}")

    if not pending and not blobs and not info["test"] and state["manifest"] == info["remote"]:
        return {"uploaded": 0, "revision": info["revision"], "previous": info["revision"], "tests": None}
    meta = root / METADATA
    meta.mkdir(mode=0o700, exist_ok=True)
    checked(meta, "directory")
    with deployment_lock(meta):
        require(load_state(root) == state, "Remote changed while preparing deployment")
        if not pending:
            require(plan(root, identity, request) == info, "Remote changed while preparing deployment")
            # Record the baseline before staging so a failed test can be retried.
            if not (meta / STATE_FILE).exists():
                save_state(meta, state)
            revision = uuid.uuid4().hex
            summary = stage_sources(root, meta / revision, info, blobs)
            require(plan(root, identity, request) == info, "Remote changed before deployment")
            pending = {"revision": revision, "plan": info, "tests": summary}
            save_state(meta, {**state, "pending": pending})
        return finish(root, meta, pending)


def result_name(name):
    parts = name.split("/")
    require(len(name) <= 240 and all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", p)
                                    and not p.endswith(".") for p in parts)
            and Path(parts[-1]).suffix.lower() in RESULT_SUFFIXES,
            f"Invalid result path: {name}")
    return name


def fetch(root, request):
    checked(root, "directory")
    results = root / "results"
    checked(results, "directory")
    names = request["files"]
    require(0 < len(names) <= 100 and len(set(names)) == len(names), "Invalid file list")
    entries = []
    for name in names:
        path = results / result_name(name)
        checked(path, "file")
        before = path.stat()
        entries.append((name, path, before.st_size, file_hash(path), before))
    out = sys.stdout.buffer
    out.write(canonical([{"name": n, "size": s, "sha256": h} for n, _, s, h, _ in entries]) + b"\n")
    out.flush()
    for name, path, size, expected, before in entries:
        checked(path, "file")
        with path.open("rb") as stream:
            require(os.path.samestat(os.fstat(stream.fileno()), before), f"Result replaced: {name}")
            remaining = size
            h = hashlib.sha256()
            while remaining:
                block = stream.read(min(1024 * 1024, remaining))
                require(block, f"Result truncated: {name}")
                out.write(block)
                h.update(block)
                remaining -= len(block)
            require(h.hexdigest() == expected and not stream.read(1), f"Result changed: {name}")


def main():
    root, identity = target()
    request = json.load(sys.stdin)
    action = request["action"]
    if action == "fetch":
        fetch(root, request)
        return
    if action == "plan":
        info = plan(root, identity, request)
        result = {"plan": info, "approval": digest(canonical(info))}
    elif action == "apply":
        result = apply(root, identity, request)
    else:
        raise ValueError("Unknown action")
    print(canonical(result).decode("ascii"))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        sys.exit(str(error))
