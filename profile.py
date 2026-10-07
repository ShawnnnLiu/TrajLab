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

# Cluster, cores, RAM, and the disk /srv lands on (the blockstore avoids the system disk).
HARDWARE = [
    ("r650", "r650 (Clemson): 72 cores, 256 GB, 1.6 TB NVMe"),
    ("r6525", "r6525 (Clemson): 64 cores, 256 GB, 1.6 TB NVMe"),
    ("r6615", "r6615 (Clemson): 32 cores, 192 GB, 800 GB NVMe"),
    ("c6525-100g", "c6525-100g (Utah): 24 cores, 128 GB, 1.6 TB NVMe"),
    ("c6420", "c6420 (Clemson): 32 cores, 384 GB, 1 TB HDD"),
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
    HARDWARE[0][0],
    HARDWARE,
    longDescription="If the cluster has no free node of this type, pick another from the list.",
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
    600,
    longDescription="Ephemeral blockstore holding /srv/trajlab/jobs and Docker's data root. "
    "Its contents are lost when the experiment ends; copy results off before it expires.",
)
params = pc.bindParameters()
if not 100 <= params.disk_gb <= 700:
    pc.reportError(
        portal.ParameterError("Use 100 to 700 GB for /srv (r6615's NVMe is 800 GB).", ["disk_gb"])
    )
pc.verifyParameters()

request = pc.makeRequestRSpec()
node = request.RawPC("trajlab")
node.disk_image = params.image
node.hardware_type = params.hardware_type

srv = node.Blockstore("srv", "/srv")
srv.size = f"{params.disk_gb}GB"
srv.placement = "nonsysvol"

node.addService(
    pg.Execute(
        shell="bash",
        command=f"sudo bash -c {shlex.quote(SETUP)}",
    )
)

pc.printRequestRSpec(request)
