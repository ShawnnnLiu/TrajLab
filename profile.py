"""TrajLab capture server: one bare-metal Ubuntu 24.04 node with Docker, uv, and /srv/trajlab/jobs.

Mirrors the AWS server the repair experiment ran on (Ubuntu 24.04, Docker with the compose plugin,
job dirs in a group-writable `/srv/trajlab/jobs`). A local blockstore is mounted at `/srv` and holds
both the job dirs and Docker's data root, since task images and checkpoint images outgrow a node's
system disk (round 1 kept 41.8 GB of checkpoint images after pruning).

Instructions:
After the node boots, setup runs once as root and logs to `/var/log/trajlab-setup.log`; it is done
when `/srv/.trajlab-setup-done` exists. Every project member on the node is in the `docker` and
`trajlab` groups (log in again after setup to pick them up). Then, as yourself:

    git clone https://github.com/ShawnnnLiu/TrajLab && cd TrajLab
    ln -s /srv/trajlab/jobs corpus/jobs
    uv sync && cp .env.example .env    # add CLAUDE_CODE_OAUTH_TOKEN
    docker info && make test

Pass `--storage /srv/trajlab/jobs` to `trajlab run` and `trajlab repair` (`corpus/README.md`).
"""

import shlex

import geni.portal as portal
import geni.rspec.pg as pg

IMAGES = [
    ("urn:publicid:IDN+emulab.net+image+emulab-ops//UBUNTU24-64-STD", "Ubuntu 24.04"),
    ("urn:publicid:IDN+emulab.net+image+emulab-ops//UBUNTU22-64-STD", "Ubuntu 22.04"),
]

# Runs as root on every boot; the marker file makes it a no-op after the first.
SETUP = r"""
[ -e /srv/.trajlab-setup-done ] && exit 0
exec >>/var/log/trajlab-setup.log 2>&1
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

mkdir -p /etc/docker /srv/docker
cat > /etc/docker/daemon.json <<'JSON'
{"data-root": "/srv/docker"}
JSON
apt-get update
apt-get install -y docker.io docker-compose-v2 git gh curl jq
systemctl enable --now docker

curl -LsSf https://astral.sh/uv/install.sh \
  | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh

groupadd -f trajlab
mkdir -p /srv/trajlab/jobs
chgrp -R trajlab /srv/trajlab
chmod 2775 /srv/trajlab /srv/trajlab/jobs
for home in /users/*; do
  user=$(basename "$home")
  if id "$user" >/dev/null 2>&1; then usermod -aG docker,trajlab "$user"; fi
done

touch /srv/.trajlab-setup-done
"""

pc = portal.Context()
pc.defineParameter(
    "hardware_type",
    "Hardware type",
    portal.ParameterType.NODETYPE,
    "c220g5",
    longDescription="Node type, e.g. c220g5 (Wisconsin: 40 cores, 192 GB RAM) or c6525-25g "
    "(Utah: 16 cores, 128 GB RAM). Leave empty for any type with enough local disk.",
)
pc.defineParameter(
    "image",
    "Disk image",
    portal.ParameterType.IMAGE,
    IMAGES[0][0],
    IMAGES,
)
pc.defineParameter(
    "disk_gb",
    "Local disk for /srv (GB)",
    portal.ParameterType.INTEGER,
    400,
    longDescription="Ephemeral blockstore holding /srv/trajlab/jobs and Docker's data root. "
    "Its contents are lost when the experiment ends; copy results off before it expires.",
)
params = pc.bindParameters()
if params.disk_gb < 100:
    pc.reportError(portal.ParameterError("Use at least 100 GB for /srv.", ["disk_gb"]))
pc.verifyParameters()

request = pc.makeRequestRSpec()
node = request.RawPC("trajlab")
node.disk_image = params.image
if params.hardware_type:
    node.hardware_type = params.hardware_type

srv = node.Blockstore("srv", "/srv")
srv.size = f"{params.disk_gb}GB"

node.addService(
    pg.Execute(
        shell="bash",
        command=f"sudo bash -c {shlex.quote(SETUP)}",
    )
)

pc.printRequestRSpec(request)
