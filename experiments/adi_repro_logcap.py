#!/usr/bin/env python3
"""Cap Docker's json-file log size in /etc/docker/daemon.json (merging, not replacing).

GitHub-hosted runner only: refuses to run anywhere else. Calibration found the uncapped container log grows
about 8.4 KiB per block (~11 GiB by the chain tip), which was the previously unexplained part of the disk usage.
"""

import json
import os
import subprocess
import sys

if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
    sys.exit("refusing to run: not a GitHub-hosted runner")

path = "/etc/docker/daemon.json"
config = json.load(open(path)) if os.path.exists(path) else {}
config["log-driver"] = "json-file"
options = config.setdefault("log-opts", {})
options["max-size"] = os.environ.get("DOCKER_LOG_MAX_SIZE", "100m")
options["max-file"] = os.environ.get("DOCKER_LOG_MAX_FILE", "3")
staging = "/tmp/daemon.json"
with open(staging, "w") as handle:
    json.dump(config, handle, indent=2)
subprocess.run(["sudo", "cp", staging, path], check=True)
print(json.dumps(config))
