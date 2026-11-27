"""Preview and apply incremental releases on the Lite3 perception host."""

import argparse
import base64
import json
from pathlib import Path
import re
import subprocess
import sys

from _remote import MAX_SOURCE, METADATA, ROOT_NAME, SOURCE_FILES, canonical, digest, require
from _transport import ROOT, exchange


PLAN_FILE = ROOT / ".deploy-plan.json"


def local_sources():
    sources = {}
    for name in SOURCE_FILES:
        path = ROOT / name
        require(path.resolve() == path, f"Symlink not allowed in source path: {name}")
        if not path.exists():
            continue
        require(path.is_file() and path.stat().st_size <= MAX_SOURCE,
                f"Source is not a regular file within the 1 MiB limit: {name}")
        raw = path.read_bytes()
        require(len(raw) <= MAX_SOURCE and b"\0" not in raw, f"Invalid source: {name}")
        # Git checkouts can convert repository text to CRLF on Windows.
        sources[name] = raw.decode("utf-8").replace("\r\n", "\n").encode("utf-8")
    require(sources, "No deployable files found")
    return sources


def deploy(args):
    blobs = local_sources()
    manifest = {name: digest(data) for name, data in blobs.items()}
    request = {"action": "plan" if args.dry_run else "apply", "local": manifest, "test": args.test}
    if args.dry_run:
        receipt = exchange(args.ssh_config, request)
        plan = receipt["plan"]
        target = plan["target"]
        require(plan["protocol"] == 2 and plan["local"] == manifest
                and receipt["approval"] == digest(canonical(plan)),
                "Invalid preview response")
        PLAN_FILE.write_bytes(canonical(receipt) + b"\n")
        print(f"Target {target['user']}@{target['host']} | {target['model']} | {target['root']}")
        print(f"Current release {plan['revision'] or '(first deployment)'}")
        for name, action in sorted(plan["changes"].items()):
            print(f"{action:6} {name}")
        for name in plan["retained"]:
            print(f"KEEP   {name} (remote only)")
        if plan["revision"] is None:
            print(f"Deployment state and backups: {target['root']}/{METADATA}/")
        print(f"{len(plan['changes'])} upload(s), {len(manifest) - len(plan['changes'])} unchanged, "
              f"0 deletions. Visual tests {'enabled' if plan['test'] else 'disabled'}.")
        print(f"Approval digest {receipt['approval']}")
        return

    require(re.fullmatch(r"[0-9a-f]{64}", args.apply), "Invalid approval digest")
    require(PLAN_FILE.is_file(), "Run --dry-run first")
    receipt = json.loads(PLAN_FILE.read_bytes())
    plan = receipt["plan"]
    require(plan.get("protocol") == 2 and Path(plan["target"]["root"]).name == ROOT_NAME,
            "Deployment target changed: run dry-run again")
    require(receipt["approval"] == args.apply == digest(canonical(plan)),
            "Approval digest does not match the local preview")
    require(plan["local"] == manifest and plan["test"] == args.test,
            "Local files or test option changed: run dry-run again")
    request.update(approval=args.apply, uploads={
        name: base64.b64encode(blobs[name]).decode("ascii") for name in plan["changes"]
    })
    result = exchange(args.ssh_config, request)
    print(f"{result['uploaded']} uploaded | release {result['revision'] or '(none)'} | "
          f"previous {result['previous'] or '(none)'}")
    if result["tests"]:
        print(result["tests"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read-only preview (default)")
    mode.add_argument("--apply", metavar="APPROVAL_SHA256", help="Apply the locally approved preview")
    parser.add_argument("--test", action="store_true", help="Run the two fixed visual test modules")
    parser.add_argument("--ssh-config", type=Path)
    args = parser.parse_args()
    args.dry_run = args.apply is None
    try:
        deploy(args)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        sys.exit(f"Deployment stopped: {error}")


if __name__ == "__main__":
    main()
