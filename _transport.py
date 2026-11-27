"""Local SSH transport shared by deployment and results collection."""

import base64
import json
from pathlib import Path
import shlex
import subprocess
import zlib

from _remote import canonical, require


ROOT = Path(__file__).resolve().parent
ALIAS = "Lite3-Perception"


def command(config):
    config_args = ["-F", str(config.resolve(strict=True))] if config else []
    options = ["ssh", *config_args, "-o", "StrictHostKeyChecking=yes",
               "-o", "UpdateHostKeys=no", "-o", "ConnectTimeout=20",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
    resolved = subprocess.run([*options, "-G", ALIAS], capture_output=True, check=True,
                              text=True, encoding="utf-8", errors="replace")
    settings = dict(line.split(maxsplit=1) for line in resolved.stdout.splitlines() if " " in line)
    require(settings.get("hostname", "").lower() != ALIAS.lower(),
            "OpenSSH did not resolve Lite3-Perception. Check the SSH config or use --ssh-config.")
    helper = zlib.compress((ROOT / "_remote.py").read_bytes())
    payload = base64.b64encode(helper).decode("ascii")
    code = f"import base64,zlib;exec(zlib.decompress(base64.b64decode('{payload}')))"
    return [*options, "-T", ALIAS, "/usr/bin/python3 -I -B -c " + shlex.quote(code)]


def exchange(config, request):
    proc = subprocess.run(command(config), input=canonical(request), stdout=subprocess.PIPE,
                          check=True, timeout=300)
    return json.loads(proc.stdout)
