# vaultsync

Rotating backups of a ZFS pool to removable USB hard drives, with a small web dashboard. It tracks which drive is where, who has it, and how long it has been since each one was updated.

Built for a Proxmox host with a ZFS pool (`tank`) that holds a Piwigo library and the container backups. Nothing in it is Piwigo-specific.

```
┌──────────────── Proxmox host ────────────────┐        ┌──── Dashboard LXC (Docker Compose) ────┐
│  tank ──snapshot──► zfs send ─► USB drive    │        │  web UI ─ FastAPI ─ SQLite              │
│                     (vs-<id> pool)           │◄──────►│  drives, custody, history, settings     │
│  /usr/local/sbin/vaultsync  (CLI + agent)    │  HTTP  │                                         │
│  vaultsync-agent.service  :8710              │ token  └─────────────────────────────────────────┘
└──────────────────────────────────────────────┘
```

- **Host side** (`host/vaultsync`) is a single Python 3 script with no dependencies. It does all the ZFS work and runs fine on its own: `vaultsync sync` copies the pool to the plugged-in drive and prints `COMPLETE`.
- **Dashboard** (`dashboard/`) runs anywhere Docker runs. It never touches ZFS or USB itself; it asks the host agent. The agent only accepts a fixed set of actions, a token, and (optionally) a single client IP.

## How backups work

- Each backup drive is a single-disk ZFS pool named `vs-<8 hex id>` with `cachefile=none`, so the host never auto-imports it at boot.
- A sync snapshots the source recursively, then sends each dataset to `vs-<id>/data/...`.
  - **Full** on a new drive.
  - **Incremental** after that.
- After a successful sync the source keeps only a **bookmark** per dataset for that drive, not a snapshot. A drive that sits in a closet for a year pins no space on `tank`, and the next sync is still incremental.
- The drive keeps **only the newest copy**. Older snapshots on the drive are pruned after each sync.
- **Interrupted syncs resume.** Receives use `zfs recv -s`. If a sync is stopped, unplugged, or the power drops:
  - The next sync to that drive finishes the interrupted copy.
  - Then it copies the newer changes.
  - If the drive doesn't come back within `stale_snapshot_days` (default 14), the leftover source snapshot is removed.
- Datasets added to the source are picked up automatically. Datasets removed from the source are removed from the drive.
- If a sync won't fit on the drive, it stops before copying anything and says so.
- Backup datasets are `readonly=on`, so browsing a drive can't change it.

## Install

### 1. Host (Proxmox node)

```bash
git clone https://github.com/longblonde/vaultsync.git /opt/vaultsync
cd /opt/vaultsync
./host/install.sh --source tank --dashboard-ip <dashboard LXC IP>
```

This installs `/usr/local/sbin/vaultsync`, writes `/etc/vaultsync/config.json` with a random token, and starts `vaultsync-agent.service` on port 8710. It prints the `AGENT_URL` and `AGENT_TOKEN` for the dashboard. You can leave `--dashboard-ip` off for now and re-run the script once the LXC has its DHCP reservation.

Check it:

```bash
vaultsync status
vaultsync ports
vaultsync drives
```

### 2. Dashboard LXC

On the Proxmox host, create an unprivileged Debian container with nesting (needed for Docker). Change the storage names to yours (`pvesm status` lists them):

```bash
pveam update
pveam download local debian-13-standard_13.1-2_amd64.tar.zst   # see `pveam available --section system` for the current name
pct create 120 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst \
  --hostname vaultsync --cores 1 --memory 512 --swap 512 \
  --rootfs local-lvm:4 --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 --features nesting=1,keyctl=1 --onboot 1
pct start 120
pct exec 120 -- ip -4 addr show eth0        # note the IP and make a DHCP reservation for it
```

Inside the container (`pct enter 120`):

```bash
apt update && apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sh
git clone https://github.com/longblonde/vaultsync.git /opt/vaultsync
cd /opt/vaultsync/dashboard
cp .env.example .env && nano .env            # AGENT_URL and AGENT_TOKEN from step 1
docker compose up -d --build
```

Open `http://<lxc-ip>:8080`.

Then, back on the host, lock the agent to the dashboard's address:

```bash
/opt/vaultsync/host/install.sh --dashboard-ip <lxc-ip>
```

Keep the dashboard on the LAN. Don't publish it through the Cloudflare tunnel: anyone who can reach it can erase a plugged-in drive.

### Updating

```bash
# host
cd /opt/vaultsync && git pull && ./host/install.sh
# LXC
cd /opt/vaultsync && git pull && cd dashboard && docker compose up -d --build
```

## Using it

### First time

1. **Settings → USB ports.** Plug a drive into each port you'll use and watch which row fills in. Name the port (e.g. "Rear USB 3 left") and set its use:
   - **Active**: drives here can be backed up.
   - **Active, slow**: allowed, but warns before a long copy. Use this for USB 2.
   - **Ignore**: drives here never appear, for example a printer or the Bluetooth dongle's port.
2. **Plug in a blank drive.** A "Set up a new backup drive" window opens. Give the drive a name and record who will keep it. Type the name again to confirm it can be erased. The first full backup starts right after setup.

### Rotating drives

1. Plug the returning drive in. Its row turns blue and shows **Back up now**, **Verify drive**, **Hand off**, and **Eject**.
2. **Back up now** first checks what will be copied, then shows whether it's a full or incremental copy, the size, and an estimated time. You can close the page while it runs; the job runs on the host.
3. When it's done, **Eject** the drive (each backup also exports the drive at the end, so it's already safe to unplug).
4. **Hand off** to record who takes it next.

The number in each row is days since that drive's last successful backup. It turns amber at the warning threshold and red when stale; both are set in **Settings → Staleness**.

### Command line

Everything the dashboard does works from the host shell too:

```
vaultsync sync                 # the only attached backup drive, or name one: vaultsync sync Alpha
vaultsync plan Alpha           # what a sync would copy, without copying
vaultsync verify Alpha         # scrub: read every block and check checksums
vaultsync eject Alpha
vaultsync init /dev/sdX --label Charlie   # ERASES the disk
vaultsync jobs | vaultsync log <job id> | vaultsync cancel <job id>
vaultsync reconnect            # reattach a drive that was unplugged while in use
vaultsync forget vs-xxxxxxxx   # delete the host's bookmarks and job history for a retired drive
vaultsync cleanup              # remove leftover snapshots from interrupted syncs
```

Jobs started from the shell show up in the dashboard's history too.

### Two backups at once

Up to `max_jobs` (default 2) jobs run at once, one per drive. On snowowl only the two add-on USB 3 controllers run at 5 Gb/s:

- `0000:04:00.0`
- the second one, on buses 5 and 6

Two drives on the same controller share its bandwidth. For two full-speed backups at once, plug the drives into ports on different controllers. The ports page groups ports by controller for this reason.

### Capacity

A drive has to hold the whole source. The dashboard warns when the data reaches 80% of a drive's capacity, and a sync that won't fit is refused before anything is copied. As `tank` grows past about 5.5 TB, the 6 TB drive will stop fitting. Retire it then, via **Edit → Retired**, and use larger drives. To leave a dataset out, add it to `"exclude"` in `/etc/vaultsync/config.json`.

### Retiring and deleting drives

Removing a drive takes two steps, so its history can't disappear by accident:

1. **Retire it.** On the drive's page, choose **Edit → Retired**. A retired drive keeps all its history and moves to the bottom of the list.
2. **Delete it.** A retired drive's page gets a **Delete drive** button. Type the drive's name to confirm.

Deleting removes:
- the drive's custody records and job history from the dashboard
- its bookmarks and leftover snapshots on `tank`
- its job logs on the host

The drive has to be unplugged first. Deleting never touches the data on the drive itself.

This is the cleanup to use after reformatting a drive. A reformatted disk gets a new pool id, so it shows up as a new drive, and the old entry can then be retired and deleted. From the shell, `vaultsync forget vs-xxxxxxxx` does the host-side part of the cleanup.

## If a drive is unplugged mid-backup

When the only disk of a ZFS pool disappears, ZFS suspends that pool and anything writing to it waits.

- **Drives created with v0.2 or later** use `failmode=continue`. Older drives are switched to it the next time they're imported. With this setting, writes fail with an error instead of blocking forever.
- **The sync stops itself.** It watches for a stalled drive: after 45 seconds with no progress it checks the drive. If the drive's pool isn't healthy, the sync stops with "the drive stopped responding".
- **What the dashboard shows:** the drive row turns red with a **Reconnect** button. From the shell, use `vaultsync reconnect`.

To recover:

1. Plug the drive back in, preferably into the same port.
2. Choose **Reconnect**, or run `vaultsync reconnect`. This runs `zpool clear` on the drive's pool.
3. Back up again. The interrupted copy resumes.

If reconnecting keeps failing while the drive is plugged in, the host has to be rebooted to release the pool. This is a ZFS limitation. `tank` is never affected.

## Restoring from a backup drive

Any Linux machine with ZFS (another Proxmox box, Ubuntu with `zfsutils-linux`, etc.) can read a drive.

```bash
zpool import                                   # lists vs-xxxxxxxx
zpool import -o readonly=on -R /mnt/restore vs-xxxxxxxx
zfs mount -a
ls /mnt/restore/mnt/vaultsync/xxxxxxxx/data/piwigo/upload
# ... copy files out ...
zpool export vs-xxxxxxxx
```

To put a whole dataset back on the server, send it from the drive:

```bash
zpool import -N vs-xxxxxxxx
zfs list -t snapshot -r vs-xxxxxxxx/data       # find the snapshot name
zfs send -R vs-xxxxxxxx/data/piwigo@vaultsync-... | zfs recv -u tank/piwigo-restored
zpool export vs-xxxxxxxx
```

The container backups (`tank/backups`, the vzdump files) come along too, so CT110 can be restored with `pct restore` from the copied archive.

## Configuration (`/etc/vaultsync/config.json`)

| key | default | meaning |
|---|---|---|
| `source` | `tank` | dataset to back up, recursively |
| `exclude` | `[]` | datasets (and their children) to skip |
| `listen`, `port` | `0.0.0.0`, `8710` | agent address |
| `token` | random | shared secret for the dashboard |
| `allowed_clients` | `[]` | if set, only these IPs may call the agent |
| `max_jobs` | `2` | jobs at once (dashboard: Settings → Jobs) |
| `verify_after_sync` | `false` | scrub the drive after every backup |
| `stale_snapshot_days` | `14` | drop leftover snapshots of interrupted syncs after this |
| `pre_snapshot_hook` | `""` | shell command run before each snapshot; a failure stops the sync |
| `ports` | `{}` | port names and modes (dashboard: Settings → USB ports) |

The hook can only be set in the file, not from the dashboard, because it runs as root. Example, to dump Piwigo's database into the backed-up dataset first (adjust the path to wherever the dataset is mounted inside CT110):

```json
"pre_snapshot_hook": "pct exec 110 -- sh -c 'mysqldump --single-transaction --all-databases > /path/inside/ct/to/tank/piwigo/db.sql'"
```

The dashboard's own settings (staleness thresholds) and its records (drives, custody, history) live in `dashboard/data/vaultsync.db`. Back that folder up if you care about the custody history. Drives themselves carry their id and label, so a lost database can be rebuilt: plug each drive in and choose **Add to list**.

## Development

```bash
python3 tests/test_engine.py     # sync engine against a fake zfs/zpool (no ZFS needed)
python3 tests/mock_agent.py &    # fake host agent on 127.0.0.1:8710, token "dev"
cd dashboard/app && AGENT_URL=http://127.0.0.1:8710 AGENT_TOKEN=dev DB_PATH=/tmp/vs.db uvicorn main:app --port 8080
```

The engine tests cover:
- full, incremental and new-dataset syncs
- removed datasets
- two drives rotating independently
- interrupted and resumed syncs
- a drive unplugged mid-copy (stall detected, reconnect, resume)
- forgetting a retired drive
- too-small drives
- a diverged drive being rebuilt
- plan
- stale snapshot cleanup
