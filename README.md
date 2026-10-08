# vaultsync

vaultsync backs up a ZFS pool to a rotating set of removable USB hard drives. A small web dashboard shows:
- which drives you have
- who has each one and where it is kept
- how long it has been since each one was updated

It was built for a Proxmox host, `snowowl`, whose ZFS pool `tank` holds a Piwigo library and the container backups. Nothing in it is specific to Piwigo.

```
┌──────────────── Proxmox host ────────────────┐        ┌──── Dashboard LXC (Docker Compose) ────┐
│  tank ──snapshot──► zfs send ─► USB drive    │        │  web UI ─ FastAPI ─ SQLite              │
│                     (vs-<id> pool)           │◄──────►│  drives, custody, history, settings     │
│  /usr/local/sbin/vaultsync  (CLI + agent)    │  HTTP  │                                         │
│  vaultsync-agent.service  :8710              │ token  └─────────────────────────────────────────┘
└──────────────────────────────────────────────┘
```

The project has two parts.

**Host side (`host/vaultsync`).** A single Python 3 script with no dependencies.
- It does all the ZFS and USB work.
- It works on its own: `vaultsync sync` copies the pool to the plugged-in drive and prints `COMPLETE`.
- It also runs as a small HTTP agent for the dashboard.

**Dashboard (`dashboard/`).** A Docker Compose app that runs anywhere Docker runs.
- It never touches ZFS or USB itself. Instead it asks the host agent.
- The agent only accepts a fixed set of actions, a token, and optionally a single client IP.

## Contents

- [How backups work](#how-backups-work)
- [Install](#install)
- [Updating](#updating)
- [Using the dashboard](#using-the-dashboard)
- [Command line](#command-line)
- [If a drive is unplugged mid-backup](#if-a-drive-is-unplugged-mid-backup)
- [Two backups at once](#two-backups-at-once)
- [Capacity](#capacity)
- [Restoring from a backup drive](#restoring-from-a-backup-drive)
- [Configuration](#configuration)
- [Where things are stored](#where-things-are-stored)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## How backups work

**Each drive is its own pool.** Every backup drive is a single-disk ZFS pool named `vs-<8 hex id>`. It is created with:
- `cachefile=none`, so the host never auto-imports it at boot
- `failmode=continue`, so a disconnected drive makes writes fail instead of hang

**Each sync copies everything that changed.** A sync snapshots the source recursively and sends each dataset to `vs-<id>/data/...`.
- The first sync to a drive is a **full** copy.
- After that, syncs are **incremental**.

**The source keeps bookmarks, not snapshots.** After a successful sync the source keeps only a ZFS **bookmark** per dataset for that drive.
- A drive that spends a year in a closet pins no space on `tank`.
- The next sync to that drive is still incremental.

**The drive keeps only its newest copy.** Older snapshots on the drive are pruned after each sync. Safety against ransomware or corruption comes from the drives being out of sync with each other.

**Interrupted syncs resume.** Receives use `zfs recv -s`. If a sync is stopped, the drive is unplugged, or the power drops:
- The next sync to that drive finishes the interrupted copy first, then copies the newer changes.
- The interrupted sync leaves a snapshot behind on the source. It is removed after `stale_snapshot_days` (default 14) if the drive doesn't come back.

**The drive mirrors the source's datasets.**
- Datasets added to the source are picked up automatically.
- Datasets removed from the source are removed from the drive.
- Backup datasets are `readonly=on`, so browsing a drive can't change it.

**A sync that won't fit stops early.** It stops before copying anything and says so.

## Install

The repo is public, so it can be downloaded with `curl`. Git isn't needed.

```bash
mkdir -p /opt/vaultsync
curl -fsSL https://github.com/longblonde/vaultsync/archive/refs/heads/main.tar.gz \
  | tar xz -C /opt/vaultsync --strip-components=1
```

If you prefer git: `apt install -y git && git clone https://github.com/longblonde/vaultsync.git /opt/vaultsync`.

### 1. Host (the Proxmox node)

```bash
# download as above, then:
/opt/vaultsync/host/install.sh --source tank
```

This:
- installs `/usr/local/sbin/vaultsync`
- writes `/etc/vaultsync/config.json` with a random token
- starts `vaultsync-agent.service` on port 8710
- prints the `AGENT_URL` and `AGENT_TOKEN` lines for the dashboard

Re-running it updates the program, keeps the existing config and token, and prints those two lines again.

Check that it works:

```bash
vaultsync status    # source pool, attached drives, running jobs
vaultsync ports     # USB ports, which controller they're on, what's plugged in
vaultsync drives    # attached USB disks
```

### 2. Dashboard LXC

On the Proxmox host, create an unprivileged Debian container with nesting enabled, which Docker needs. Change the storage names to yours; `pvesm status` lists them.

```bash
pveam update
pveam available --section system | grep debian-13      # find the current template name
pveam download local debian-13-standard_13.1-2_amd64.tar.zst
pct create 120 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst \
  --hostname vaultsync --cores 1 --memory 512 --swap 512 \
  --rootfs local-lvm:4 --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 --features nesting=1,keyctl=1 --onboot 1
pct start 120
pct exec 120 -- ip -4 addr show eth0     # note the IP and make a DHCP reservation for it
```

Inside the container (`pct enter 120`):

```bash
apt update && apt install -y ca-certificates curl
curl -fsSL https://get.docker.com | sh
mkdir -p /opt/vaultsync
curl -fsSL https://github.com/longblonde/vaultsync/archive/refs/heads/main.tar.gz \
  | tar xz -C /opt/vaultsync --strip-components=1
cd /opt/vaultsync/dashboard
cp .env.example .env && nano .env       # paste AGENT_URL and AGENT_TOKEN from step 1
docker compose up -d --build
```

Open `http://<lxc-ip>:8080`.

### 3. Lock the agent to the dashboard

Back on the host:

```bash
/opt/vaultsync/host/install.sh --dashboard-ip <lxc-ip>
```

After this the agent accepts requests only from that address, and only with the token.

**Keep the dashboard on the LAN.** Don't publish it through the Cloudflare tunnel. It has no login, and anyone who can reach it can erase a plugged-in drive.

## Updating

Re-download over the existing copy. This replaces program files and leaves your `.env`, `/etc/vaultsync/config.json`, and `dashboard/data/` alone.

```bash
# on the host
curl -fsSL https://github.com/longblonde/vaultsync/archive/refs/heads/main.tar.gz \
  | tar xz -C /opt/vaultsync --strip-components=1
/opt/vaultsync/host/install.sh

# in the dashboard LXC
curl -fsSL https://github.com/longblonde/vaultsync/archive/refs/heads/main.tar.gz \
  | tar xz -C /opt/vaultsync --strip-components=1
cd /opt/vaultsync/dashboard && docker compose up -d --build
```

Running jobs keep going while the agent restarts. They are separate processes.

## Using the dashboard

The dashboard checks with the host every few seconds. Plugging in or unplugging a drive shows up on its own, with no refresh needed.

### First time: name your ports

Go to **Settings → USB ports**.
- Ports are grouped by USB controller.
- To find a physical port, plug a drive into it and watch which row fills in.
- Give each port a name, such as "Rear USB 3 left", and choose how it's used:

| Use | Meaning |
|---|---|
| **Active** | Drives here can be backed up. |
| **Active, slow** | Allowed, but warns before a long copy. Use this for USB 2 ports. |
| **Ignore** | Drives here never appear. Use this for ports with a printer, a Bluetooth dongle, and so on. |
| **Not set** | Works, but the drive row reminds you to configure the port. |

### Setting up a new drive

1. Plug a drive the dashboard hasn't seen before into an active port. A **Set up a new backup drive** window opens.
2. Fill in:
   - **Drive name.** It must be unique; see [Drive names](#drive-names).
   - **Who will keep it**, **where it will be kept**, and optional **notes**.
   - **Start the first backup right after setup.** This is on by default.
3. Type the drive name again to confirm the drive can be erased, then choose **Erase and set up**.

The window shows what's currently on the disk before you erase it. It also warns if the disk:
- is on a USB 2 link
- is smaller than the data to back up
- belongs to another ZFS pool

If you close the window, a blue strip at the top of the drives page keeps a **Set up as backup drive** button.

### Drives already set up elsewhere

You might plug in a drive that has a vaultsync pool but isn't in the dashboard's list. This happens if it was set up from the shell or the dashboard database was lost. The dashboard then offers **Add an existing backup drive**, which keeps the drive's data.

### Rotating drives

The drives page lists every drive. For each one it shows:
- its custodian and location
- days since its last successful backup, on a bar marked at the warning and stale thresholds
- the last result
- its capacity

Plug the returning drive in. Its row turns blue and shows the port, the link speed, and these actions:

| Action | What it does |
|---|---|
| **Back up now** | Checks what needs copying first, then shows full or incremental, the size, and an estimated time. You can close the page; the backup runs on the host. |
| **Verify drive** | Scrubs the drive: reads every block and checks it against its checksum. This can take hours on a full drive. |
| **Hand off** | Records who has the drive now and where it is. The previous custody record is closed with today's date. |
| **Eject** | Exports the drive's pool and detaches the disk so it can be unplugged. |

Every backup also exports the drive when it finishes, so the drive is safe to unplug once the job says **Safe to unplug**.

While a job runs, its row shows a progress bar, the speed, the time left, **View log**, and **Stop**. A stopped backup resumes on the next backup to that drive.

### Drive pages

Click a drive to see:
- its custody timeline: who had it and when
- every backup, verify, setup and eject, with logs
- its pool id, disk model, serial and capacity

From there you can **Hand off**, **Edit** (rename, notes, retire), and, for retired drives, **Delete drive**.

### Drive names

Drive names must be unique.
- Names are compared ignoring capitalization and extra spaces.
- Retired drives count, so a name can only be reused after the old drive is deleted.
- The setup, add, and rename windows warn as you type, and the save button stays disabled while the name clashes.
- `vaultsync init` on the host enforces the same rule.

### Retiring and deleting drives

Removing a drive is two steps, so its history can't disappear by accident.

1. **Retire it.** On the drive page, choose **Edit → Retired**. A retired drive keeps all its history and moves to the bottom of the list.
2. **Delete it.** A retired drive's page has a **Delete drive** button. Type the drive's name to confirm.

Deleting removes:
- the drive's custody records and job history from the dashboard
- its bookmarks and any leftover snapshots on `tank`
- its job logs on the host

The rules:
- The drive has to be unplugged first.
- Deleting never touches the data on the drive itself.
- Deleted history doesn't come back the next time the dashboard checks with the host.

**After reformatting a drive:** a reformatted disk gets a new pool id, so it shows up as a new drive. Set it up under a new name, then retire and delete the old entry.

### History

The **History** tab lists every job across all drives, newest first. Click a row to read its log.

### Settings

| Section | What's there |
|---|---|
| **Staleness** | Days until a drive turns amber (default 30) and red (default 60). |
| **USB ports** | Port names and uses; see [above](#first-time-name-your-ports). |
| **Jobs** | How many jobs can run at once (default 2). How many days before an unfinished backup's snapshot is discarded (default 14). Whether to verify the whole drive after every backup (off by default; slow). **Remove old snapshots now**. |
| **Host** | The agent address, the host name, and the source datasets with their sizes. |

## Command line

Everything the dashboard does also works from the host shell, and jobs started there appear in the dashboard's history. Commands that take a drive accept its pool name (`vs-…`), label or serial number. If you leave the drive out, the command uses the only attached backup drive.

| Command | What it does |
|---|---|
| `vaultsync status` | Source pool and its datasets, attached drives, running jobs |
| `vaultsync ports` | USB ports with controller, max speed, configured use, and what's plugged in |
| `vaultsync drives` | Attached USB disks and whether each is a backup drive |
| `vaultsync init /dev/sdX --label NAME` | **Erases** the disk and makes it a backup drive. Asks you to type the label to confirm, unless you pass `--yes`. |
| `vaultsync plan [DRIVE]` | What a sync would copy (full or incremental, size, per dataset) without copying anything |
| `vaultsync sync [DRIVE]` | Backs up the source to the drive with a progress bar, then prints **COMPLETE** |
| `vaultsync verify [DRIVE]` | Scrubs the drive and reports errors |
| `vaultsync eject [DRIVE]` | Exports the drive's pool and detaches the disk |
| `vaultsync reconnect [DRIVE]` | Reattaches a drive that was unplugged while in use; see [below](#if-a-drive-is-unplugged-mid-backup) |
| `vaultsync jobs [--limit N]` | Recent jobs |
| `vaultsync log JOB_ID` | A job's log |
| `vaultsync cancel JOB_ID` | Stops a running job. A stopped sync resumes next time. |
| `vaultsync cleanup` | Removes leftover snapshots of interrupted syncs older than `stale_snapshot_days` |
| `vaultsync forget vs-xxxxxxxx` | Deletes the host's bookmarks, leftover snapshots and job records for a drive. The dashboard's **Delete drive** runs this for you. |
| `vaultsync agent` | Runs the HTTP agent. `vaultsync-agent.service` does this for you. |

A typical rotation from the shell:

```bash
vaultsync sync          # plug in the drive, then this; wait for COMPLETE
vaultsync eject         # optional, the sync already exported the drive
```

## If a drive is unplugged mid-backup

When the only disk of a ZFS pool disappears, ZFS **suspends** the pool. vaultsync handles this in three ways:
- Drive pools use `failmode=continue`, so writes fail instead of blocking forever. Drives created before v0.2 are switched to this the next time they're imported.
- During a copy, if no data moves for 45 seconds, vaultsync checks the drive. If the drive's pool isn't healthy, the backup stops with "the drive stopped responding". It doesn't hang.
- Every `zfs` and `zpool` command has a time limit, so the dashboard stays responsive while a drive is missing.

On the dashboard the drive's row turns red. While the drive is unplugged it says to plug it back in. Once it's back, the row shows a **Reconnect** button.

**To recover:**
1. Plug the drive back in, preferably into the same port.
2. Choose **Reconnect** on the dashboard, or run `vaultsync reconnect`. This runs `zpool clear` on the drive's pool.
3. Back up again. The interrupted copy resumes.

If reconnecting keeps failing while the drive is plugged in, the host has to be rebooted to release the pool. This is a ZFS limitation. `tank` and Piwigo are never affected.

**If a job is still stuck** (for example, one started by a version before 0.2):
- Run `vaultsync jobs` to find its ID.
- Run `systemctl kill -s KILL vaultsync-<job id>`. That covers jobs started from the dashboard. For jobs started from the shell, use Ctrl+C or `kill -9`.

The dashboard's **Stop** button force-kills a job that doesn't exit within 20 seconds.

## Two backups at once

Up to `max_jobs` jobs run at once (default 2, set in **Settings → Jobs**), at most one per drive.

Two drives on the same USB controller share its bandwidth. On snowowl:
- Only the two add-on USB 3 controllers run at 5 Gb/s: `0000:04:00.0`, and the one on buses 5 and 6.
- The other ports are USB 2, at about 35 MB/s.
- For two full-speed backups at once, plug the drives into ports on **different** USB 3 controllers.

The ports page groups ports by controller for this reason.

## Capacity

A drive has to hold the whole source.
- The dashboard warns when the data to back up reaches 80% of a drive's capacity.
- A sync that won't fit is refused before anything is copied.

As `tank` grows past about 5.5 TB, the 6 TB drive will stop fitting. Retire it then and use larger drives. To leave a dataset out, add it to `"exclude"` in the config.

Rough copy times:
- **USB 3 HDD:** about 150 MB/s, or roughly 10 hours for a full 6 TB copy.
- **USB 2:** about 35 MB/s, or roughly 2 days.

Incremental backups only copy what changed.

## Restoring from a backup drive

Any Linux machine with ZFS can read a drive: another Proxmox box, or Ubuntu with `zfsutils-linux`.

**Browse or copy files:**

```bash
zpool import                                         # lists vs-xxxxxxxx
zpool import -o readonly=on -R /mnt/restore vs-xxxxxxxx
zfs mount -a
ls /mnt/restore/mnt/vaultsync/xxxxxxxx/data/piwigo/upload
# ... copy files out ...
zpool export vs-xxxxxxxx
```

**Put a whole dataset back on the server:**

```bash
zpool import -N vs-xxxxxxxx
zfs list -t snapshot -r vs-xxxxxxxx/data             # find the snapshot name
zfs send -R vs-xxxxxxxx/data/piwigo@vaultsync-... | zfs recv -u tank/piwigo-restored
zpool export vs-xxxxxxxx
```

The container backups in `tank/backups` (the vzdump files) come along too. CT110 can be restored with `pct restore` from the copied archive.

## Configuration

The host config is `/etc/vaultsync/config.json`. The dashboard edits `ports`, `max_jobs`, `verify_after_sync` and `stale_snapshot_days`. Edit the rest by hand, then run `systemctl restart vaultsync-agent`.

| Key | Default | Meaning |
|---|---|---|
| `source` | `tank` | Dataset to back up, recursively |
| `exclude` | `[]` | Datasets (and their children) to skip |
| `listen`, `port` | `0.0.0.0`, `8710` | Agent address |
| `token` | random | Shared secret for the dashboard |
| `allowed_clients` | `[]` | If set, only these IPs may call the agent |
| `max_jobs` | `2` | Jobs at once |
| `verify_after_sync` | `false` | Scrub the drive after every backup |
| `stale_snapshot_days` | `14` | Drop leftover snapshots of interrupted syncs after this many days |
| `pre_snapshot_hook` | `""` | Shell command run before each snapshot. If it fails, the sync stops. |
| `ports` | `{}` | Port names and uses |

The hook can only be set in the file, never from the dashboard, because it runs as root. For example, to dump Piwigo's database into a backed-up dataset first (adjust the path to where the dataset is mounted inside CT110):

```json
"pre_snapshot_hook": "pct exec 110 -- sh -c 'mysqldump --single-transaction --all-databases > /path/inside/ct/to/tank/piwigo/db.sql'"
```

**Print the dashboard token again:**

```bash
python3 -c 'import json;print(json.load(open("/etc/vaultsync/config.json"))["token"])'
# or re-run /opt/vaultsync/host/install.sh, which prints AGENT_URL and AGENT_TOKEN
```

The dashboard's `.env` lives in `/opt/vaultsync/dashboard/.env`. After changing it, run `docker compose up -d` to apply.

| Variable | Meaning |
|---|---|
| `AGENT_URL` | `http://<host-ip>:8710` |
| `AGENT_TOKEN` | The host's token |
| `DASHBOARD_PORT` | Port the dashboard listens on (default 8080) |
| `TZ` | Time zone for the container |

## Where things are stored

| What | Where |
|---|---|
| Program | `/usr/local/sbin/vaultsync` on the host |
| Host config and token | `/etc/vaultsync/config.json` |
| Job records and logs | `/var/lib/vaultsync/jobs/`, `/var/lib/vaultsync/logs/` |
| Known drive labels | `/var/lib/vaultsync/known_drives.json` |
| Agent service | `vaultsync-agent.service` (`journalctl -u vaultsync-agent`) |
| Running jobs | systemd units named `vaultsync-<job id>` |
| Dashboard records (drives, custody, history, staleness settings) | `/opt/vaultsync/dashboard/data/vaultsync.db` in the LXC |
| On each drive | Pool `vs-<id>`, data under `vs-<id>/data/`, with `vaultsync:id`, `vaultsync:label`, `vaultsync:last_sync` and related properties on the pool |

Back up `dashboard/data/` if you care about custody history. Each drive carries its own id and label, so a lost dashboard database can be rebuilt: plug each drive in and choose **Add to list**.

## Troubleshooting

**The red banner says the host agent can't be reached.**
- Check that `systemctl status vaultsync-agent` is running on the host.
- Check that `AGENT_URL` in `.env` points at the host's LAN IP and port 8710.

**The banner says "bad or missing token".** `AGENT_TOKEN` doesn't match the host's token. Print the token again (see [Configuration](#configuration)), update `.env`, and run `docker compose up -d`.

**The banner says "client … not allowed".** The dashboard LXC's IP isn't in `allowed_clients`. Run `install.sh --dashboard-ip <lxc-ip>` on the host, or make a DHCP reservation so the IP stops changing.

**A plugged-in drive doesn't appear.**
- Its port may be set to **Ignore** in **Settings → USB ports**.
- Check `vaultsync drives` on the host.
- `dmesg | tail` shows whether the kernel saw the disk.

**A backup was refused because it won't fit.** The drive is too small for the source. Use a larger drive, or exclude datasets.

**A drive shows red after being unplugged.** See [If a drive is unplugged mid-backup](#if-a-drive-is-unplugged-mid-backup).

## Development

```bash
python3 tests/test_engine.py     # sync engine against a fake zfs/zpool; no ZFS needed
python3 tests/mock_agent.py &    # fake host agent on 127.0.0.1:8710, token "dev"
cd dashboard/app && AGENT_URL=http://127.0.0.1:8710 AGENT_TOKEN=dev DB_PATH=/tmp/vs.db \
  uvicorn main:app --port 8080
```

The engine tests cover:
- full, incremental and new-dataset syncs
- datasets removed from the source
- two drives rotating independently
- interrupted and resumed syncs
- a drive unplugged mid-copy: stall detected, reconnect, resume
- a too-small drive
- a diverged drive being rebuilt
- plan
- stale snapshot cleanup
- forgetting a retired drive

The mock agent simulates:
- USB 3 and USB 2 ports
- a registered drive and a blank drive
- jobs that progress over time

You can also drive it directly:
- `POST /mock/plug {"disk": "alpha"|"blank"|"bravo", "attached": true|false}` plugs and unplugs disks.
- `POST /mock/problem {"pool": "vs-…"}` simulates a disconnected drive.
