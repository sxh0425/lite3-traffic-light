"""Fetch named, completed vision results from the perception host."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from _remote import HEX, canonical, require, result_name
from _transport import ROOT, command


def destination(root, name):
    dest = root / result_name(name)
    require(dest.resolve() == dest, f"Symlink in local destination: {dest}")
    require(not os.path.lexists(dest), f"Local result already exists: {dest}")
    require(not any(part.is_reserved() for part in Path(name).parents if part != Path("."))
            and not Path(name).is_reserved(), f"Reserved local path: {name}")
    return dest


def receive(proc, stage, files):
    header = proc.stdout.readline(65536)
    require(header.endswith(b"\n"), "Missing or oversized result manifest")
    entries = json.loads(header)
    require(isinstance(entries, list) and [entry["name"] for entry in entries] == files,
            "Result manifest does not match the requested files")
    for i, entry in enumerate(entries):
        remaining = entry["size"]
        expected = entry["sha256"]
        require(type(remaining) is int and remaining >= 0 and HEX.fullmatch(expected),
                "Invalid result manifest")
        h = hashlib.sha256()
        with (stage / str(i)).open("xb") as stream:
            while remaining:
                block = proc.stdout.read(min(1024 * 1024, remaining))
                require(block, f"Incomplete download: {entry['name']}")
                stream.write(block)
                h.update(block)
                remaining -= len(block)
        require(h.hexdigest() == expected, f"SHA-256 mismatch: {entry['name']}")
    require(not proc.stdout.read(1), "Unexpected bytes from remote host")


def fetch(args):
    require(len(args.files) <= 100 and len({f.casefold() for f in args.files}) == len(args.files),
            "Specify up to 100 distinct result paths")
    root = ROOT / "results"
    for name in args.files:
        destination(root, name)
    root.mkdir(exist_ok=True)
    require(root.is_dir() and root.resolve() == root, "Unsafe local results directory")
    with tempfile.TemporaryDirectory(prefix=".fetch-", dir=root) as stage:
        stage = Path(stage)
        with subprocess.Popen(command(args.ssh_config), stdin=subprocess.PIPE, stdout=subprocess.PIPE) as proc:
            try:
                proc.stdin.write(canonical({"action": "fetch", "files": args.files}))
                proc.stdin.close()
                receive(proc, stage, args.files)
                require(proc.wait(timeout=15) == 0, "Remote download failed")
            finally:
                if proc.poll() is None:
                    proc.kill()

        destinations = [destination(root, name) for name in args.files]
        published = []
        try:
            for i, dest in enumerate(destinations):
                dest.parent.mkdir(parents=True, exist_ok=True)
                destination(root, args.files[i])
                os.link(stage / str(i), dest)
                published.append((stage / str(i), dest))
        except (OSError, ValueError):
            for source, dest in published:
                if dest.exists() and dest.samefile(source):
                    dest.unlink()
            raise
    print(f"{len(args.files)} verified download(s) in {root}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", metavar="RELATIVE_RESULT_PATH")
    parser.add_argument("--ssh-config", type=Path)
    args = parser.parse_args()
    try:
        fetch(args)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        sys.exit(f"Download stopped: {error}")


if __name__ == "__main__":
    main()
