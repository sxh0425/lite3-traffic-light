"""Deployment protocol. OpenSSH runs this helper on the selected Jetson."""

import base64
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


ROOT_NAME = "yidong-vision"
MAX_SOURCE = 1024 * 1024
SOURCE_FILES = (
    "README.md", "configs/traffic_light.yaml",
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
    if checked(root, "directory", missing=True):
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


def check_release(release, manifest):
    checked(release, "directory")
    found = set()
    for entry in release.rglob("*"):
        name = entry.relative_to(release).as_posix()
        if stat.S_ISDIR(entry.lstat().st_mode):
            checked(entry, "directory")
            continue
        if name == ".manifest.json":
            continue
        checked(entry, "file")
        require(name in manifest and file_hash(entry) == manifest[name],
                f"Remote file changed or unmanaged: {name}")
        found.add(name)
    require(found == set(manifest),
            f"Remote files missing: {', '.join(sorted(set(manifest) - found))}")


def managed_root(root):
    if not checked(root, "directory", missing=True):
        return False
    releases = root / ".releases"
    checked(releases, "directory")
    marker = root / ".project"
    checked(marker, "file")
    require(marker.read_bytes() == b"lite3-traffic-light-v1\n",
            "Target is not managed by this project")
    return True


def current(root):
    if not managed_root(root):
        return None, {}
    pointer = root / "current"
    require(pointer.is_symlink(), "Missing or invalid current release pointer")
    link = Path(os.readlink(pointer))
    if link == Path(".releases/empty"):
        return None, {}
    require(len(link.parts) == 2 and link.parts[0] == ".releases"
            and REVISION.fullmatch(link.name), "Invalid current release pointer")
    release = root / link
    manifest_file = release / ".manifest.json"
    checked(manifest_file, "file")
    manifest = source_manifest(json.loads(manifest_file.read_bytes()))
    check_release(release, manifest)
    return release, manifest


def plan(root, identity, request):
    release, old = current(root)
    new = source_manifest(request["local"])
    require(type(request["test"]) is bool, "Invalid test flag")
    changes = {p: ("MODIFY" if p in old else "ADD") for p, h in new.items()
               if old.get(p) != h}
    if request["test"]:
        require(set(TEST_FILES) <= (old.keys() | new.keys()), "Visual tests missing")
    info = {"target": identity, "revision": release.name if release else None,
            "remote": old, "local": new, "changes": changes,
            "retained": sorted(old.keys() - new.keys()), "test": request["test"]}
    return info, release


def visual_tests(release):
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(key, None)
    run = subprocess.run(
        [sys.executable, "-B", "-s", "-m", "pytest", "-q", "--tb=short",
         "-p", "no:cacheprovider", "-o", "addopts=", *TEST_FILES],
        cwd=release, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", timeout=120,
    )
    lines = [line for line in run.stdout.splitlines() if line.strip()]
    require(run.returncode == 0, "Visual tests failed:\n" + "\n".join(lines[-35:]))
    return lines[-1] if lines else "Visual tests passed"


def apply(root, identity, request):
    import fcntl

    info, _ = plan(root, identity, request)
    require(request["approval"] == digest(canonical(info)), "Preview changed: run dry-run again")
    uploads = request["uploads"]
    require(uploads.keys() == info["changes"].keys(), "Upload set differs from preview")
    blobs = {name: base64.b64decode(data, validate=True) for name, data in uploads.items()}
    for name, data in blobs.items():
        require(len(data) <= MAX_SOURCE and digest(data) == info["local"][name],
                f"Upload hash or size mismatch: {name}")

    if not blobs and not info["test"]:
        return {"uploaded": 0, "revision": info["revision"], "previous": info["revision"], "tests": None}
    if not root.exists():
        root.mkdir(mode=0o700)
        (root / ".releases").mkdir(mode=0o700)
        (root / "current").symlink_to(".releases/empty")
        with (root / ".project").open("xb") as stream:
            stream.write(b"lite3-traffic-light-v1\n")

    # Lock the managed directory without creating or removing a lock file.
    fd = os.open(root / ".releases", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked, previous = plan(root, identity, request)
        require(locked == info, "Remote changed while preparing deployment")
        if not blobs:
            return {"uploaded": 0, "revision": info["revision"], "previous": info["revision"],
                    "tests": visual_tests(previous)}
        revision = uuid.uuid4().hex
        release = root / ".releases" / revision
        release.mkdir(mode=0o700)
        manifest = {**info["remote"], **info["local"]}
        for name in manifest:
            path = release / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if name in blobs:
                path.write_bytes(blobs[name])
            else:
                shutil.copyfile(previous / name, path)
        check_release(release, manifest)
        summary = visual_tests(release) if info["test"] else None
        check_release(release, manifest)
        (release / ".manifest.json").write_bytes(canonical(manifest))
        require(plan(root, identity, request)[0] == info, "Remote changed before activation")
        pointer = root / (".next-" + revision)
        pointer.symlink_to(Path(".releases") / revision)
        os.replace(pointer, root / "current")
        return {"uploaded": len(blobs), "revision": revision, "previous": info["revision"], "tests": summary}
    finally:
        os.close(fd)


def result_name(name):
    parts = name.split("/")
    require(len(name) <= 240 and all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", p)
                                    and not p.endswith(".") for p in parts)
            and Path(parts[-1]).suffix.lower() in RESULT_SUFFIXES,
            f"Invalid result path: {name}")
    return name


def fetch(root, request):
    require(managed_root(root), "Deploy the project before collecting results")
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
        info, _ = plan(root, identity, request)
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
