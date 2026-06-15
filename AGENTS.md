# GPU Watcher — Agent Guide

Monitor GPU availability across multiple remote servers via SSH and send macOS notifications when GPUs become idle.

## Project layout

```
watcher.py       # main script (async SSH polling + rich TUI)
config.yaml      # host list, thresholds, intervals
requirements.txt # asyncssh, rich, pyyaml
```

## Running

```bash
pip install -r requirements.txt

python3 watcher.py              # continuous monitoring with live TUI
python3 watcher.py --once       # one-shot query, print table, exit
python3 watcher.py --interval 30  # override poll interval (seconds)
python3 watcher.py -c /path/to/other.yaml  # use a different config file
```

## Key behaviors

- **Idle detection**: GPU util ≤ `idle_threshold.utilization_pct` **and** VRAM usage ≤ `idle_threshold.memory_pct` (both thresholds from `config.yaml`)
- **Notification**: fires on idle transition (busy → free); repeats after `notify_cooldown` seconds if still idle
- **SSH**: uses per-host `key` if specified, otherwise falls back to ssh-agent; `known_hosts` verification is disabled for shared-dev-machine convenience
- **Concurrency**: all hosts queried in parallel via `asyncio.gather`

## Config schema

```yaml
poll_interval: 60          # seconds between polls
notify_cooldown: 300       # re-notify interval for a persistently idle GPU
ssh_timeout: 10
ssh_connect_timeout: 5
idle_threshold:
  utilization_pct: 10
  memory_pct: 20
hosts:
  - name: "label"          # display name
    host: "ip-or-hostname"
    user: "username"
    port: 22               # optional, default 22
    key: "~/.ssh/id_rsa"   # optional
```

## Adding/removing hosts

Edit `hosts` list in `config.yaml`. No code changes needed.
