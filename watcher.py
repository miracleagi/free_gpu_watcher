#!/usr/bin/env python3
"""GPU 空闲监控 - 轮询多台远程机器，有空闲 GPU 时推送 macOS 通知。"""

import argparse
import asyncio
import datetime
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import asyncssh
import yaml
from rich import box
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

console = Console()

NVIDIA_SMI_CMD = (
    "nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu "
    "--format=csv,noheader,nounits 2>/dev/null || echo '__NO_GPU__'"
)


# ─── Data classes ────────────────────────────────────────────────────────────

@dataclass
class GPUInfo:
    index: int
    name: str
    memory_used_mb: int
    memory_total_mb: int
    utilization_pct: int

    @property
    def memory_pct(self) -> float:
        if self.memory_total_mb == 0:
            return 0.0
        return self.memory_used_mb / self.memory_total_mb * 100


@dataclass
class HostStatus:
    name: str
    host: str
    gpus: list[GPUInfo] = field(default_factory=list)
    error: Optional[str] = None
    reachable: bool = True
    last_updated: Optional[datetime.datetime] = None


@dataclass
class Config:
    hosts: list[dict]
    poll_interval: int = 60
    notify_cooldown: int = 300
    idle_util_threshold: int = 10
    idle_memory_threshold: int = 20
    ssh_timeout: int = 10
    ssh_connect_timeout: int = 5


# ─── Config loading ───────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    thresholds = raw.get("idle_threshold", {})
    return Config(
        hosts=raw["hosts"],
        poll_interval=raw.get("poll_interval", 60),
        notify_cooldown=raw.get("notify_cooldown", 300),
        idle_util_threshold=thresholds.get("utilization_pct", 10),
        idle_memory_threshold=thresholds.get("memory_pct", 20),
        ssh_timeout=raw.get("ssh_timeout", 10),
        ssh_connect_timeout=raw.get("ssh_connect_timeout", 5),
    )


# ─── SSH query ────────────────────────────────────────────────────────────────

async def query_host(host_cfg: dict, config: Config) -> HostStatus:
    name = host_cfg.get("name", host_cfg["host"])
    status = HostStatus(name=name, host=host_cfg["host"])

    ssh_kwargs: dict = {
        "host": host_cfg["host"],
        "username": host_cfg.get("user"),
        "port": host_cfg.get("port", 22),
        "connect_timeout": config.ssh_connect_timeout,
        # 开发机共享场景 host key 经常变，跳过验证
        "known_hosts": None,
    }
    if "key" in host_cfg:
        ssh_kwargs["client_keys"] = [str(Path(host_cfg["key"]).expanduser())]

    try:
        async with asyncssh.connect(**ssh_kwargs) as conn:
            result = await asyncio.wait_for(
                conn.run(NVIDIA_SMI_CMD, check=False),
                timeout=config.ssh_timeout,
            )
            output = result.stdout.strip()

            if "__NO_GPU__" in output or not output:
                status.error = "nvidia-smi 不可用"
                return status

            for line in output.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 5:
                    continue
                try:
                    status.gpus.append(GPUInfo(
                        index=int(parts[0]),
                        name=parts[1],
                        memory_used_mb=int(parts[2]),
                        memory_total_mb=int(parts[3]),
                        utilization_pct=int(parts[4]),
                    ))
                except ValueError:
                    continue

            status.last_updated = datetime.datetime.now()

    except asyncio.TimeoutError:
        status.error = "连接超时"
        status.reachable = False
    except (asyncssh.DisconnectError, asyncssh.ConnectionLost) as e:
        status.error = f"SSH 断开: {e}"
        status.reachable = False
    except (OSError, asyncssh.Error) as e:
        status.error = str(e)[:70]
        status.reachable = False

    return status


async def poll_all(host_cfgs: list[dict], config: Config) -> list[HostStatus]:
    return await asyncio.gather(*[query_host(h, config) for h in host_cfgs])


# ─── Idle detection ───────────────────────────────────────────────────────────

def is_idle(gpu: GPUInfo, config: Config) -> bool:
    return (
        gpu.utilization_pct <= config.idle_util_threshold
        and gpu.memory_pct <= config.idle_memory_threshold
    )


# ─── macOS notification ───────────────────────────────────────────────────────

def send_notification(title: str, subtitle: str, body: str) -> None:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        [
            "osascript", "-e",
            f'display notification "{esc(body)}" with title "{esc(title)}" '
            f'subtitle "{esc(subtitle)}" sound name "Ping"',
        ],
        capture_output=True,
    )


# ─── Rich table ───────────────────────────────────────────────────────────────

def _color_pct(val: float, warn: float = 30, crit: float = 80) -> str:
    if val < warn:
        return "green"
    if val < crit:
        return "yellow"
    return "red"


def build_table(statuses: list[HostStatus], config: Config) -> Table:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    table = Table(
        title=f"[bold]GPU Watcher[/bold]  [dim]{ts}[/dim]",
        box=box.ROUNDED,
        show_lines=True,
        expand=False,
    )
    table.add_column("Host", style="cyan bold", no_wrap=True)
    table.add_column("GPU", no_wrap=True)
    table.add_column("Util %", justify="right")
    table.add_column("VRAM Used / Total", justify="right")
    table.add_column("Status", justify="center")

    for status in statuses:
        if not status.reachable or status.error:
            table.add_row(
                status.name,
                "—",
                "—",
                "—",
                f"[red]✗ {status.error or '不可达'}[/red]",
            )
            continue

        if not status.gpus:
            table.add_row(status.name, "[dim]无 GPU[/dim]", "—", "—", "[yellow]?[/yellow]")
            continue

        for i, gpu in enumerate(status.gpus):
            idle = is_idle(gpu, config)
            uc = _color_pct(gpu.utilization_pct)
            mc = _color_pct(gpu.memory_pct)
            mem_str = f"{gpu.memory_used_mb/1024:.1f} / {gpu.memory_total_mb/1024:.1f} G"
            table.add_row(
                status.name if i == 0 else "",
                f"[dim]#{gpu.index}[/dim] {gpu.name}",
                f"[{uc}]{gpu.utilization_pct}%[/{uc}]",
                f"[{mc}]{mem_str}[/{mc}]",
                "[bold green]FREE ✓[/bold green]" if idle else "[dim]busy[/dim]",
            )

    return table


# ─── Notification state ───────────────────────────────────────────────────────

class NotifyState:
    """检测 GPU 从忙变闲（或长时间仍空闲）时触发通知，避免轰炸。"""

    def __init__(self, cooldown: int) -> None:
        self.cooldown = cooldown
        # (host_name, gpu_index) -> last notify timestamp
        self._last_notified: dict[tuple[str, int], float] = {}
        # (host_name, gpu_index) -> was idle in last round
        self._prev_idle: dict[tuple[str, int], bool] = {}

    def check(
        self, statuses: list[HostStatus], config: Config
    ) -> list[tuple[str, GPUInfo]]:
        """返回本轮需要通知的 (host_name, gpu) 列表。"""
        now = time.monotonic()
        events: list[tuple[str, GPUInfo]] = []

        for status in statuses:
            for gpu in status.gpus:
                key = (status.name, gpu.index)
                current_idle = is_idle(gpu, config)
                was_idle = self._prev_idle.get(key, False)
                last_t = self._last_notified.get(key, 0.0)

                became_free = current_idle and not was_idle
                still_free_timeout = current_idle and (now - last_t >= self.cooldown)

                if became_free or still_free_timeout:
                    events.append((status.name, gpu))
                    self._last_notified[key] = now

                self._prev_idle[key] = current_idle

        return events


# ─── Entry point ──────────────────────────────────────────────────────────────

async def run(config: Config, once: bool = False) -> None:
    notify_state = NotifyState(cooldown=config.notify_cooldown)

    if once:
        statuses = await poll_all(config.hosts, config)
        console.print(build_table(statuses, config))
        return

    console.print(
        f"[dim]监控 {len(config.hosts)} 台机器，每 {config.poll_interval}s 轮询一次。"
        f"Ctrl+C 退出。[/dim]\n"
    )

    with Live(console=console, refresh_per_second=0.5) as live:
        while True:
            statuses = await poll_all(config.hosts, config)

            events = notify_state.check(statuses, config)
            if events:
                # 按 host 分组，一条通知，body 每行一台机器
                from collections import defaultdict
                by_host: dict[str, list[GPUInfo]] = defaultdict(list)
                for h, g in events:
                    by_host[h].append(g)

                n = len(events)
                title = "GPU 空闲" if n == 1 else f"GPU 空闲 · {n} 块"
                subtitle = "  ·  ".join(
                    f"{h} " + " ".join(f"#{g.index}" for g in gpus)
                    for h, gpus in by_host.items()
                )
                body = "\n".join(
                    f"{h}:  " + "   ".join(
                        f"#{g.index} ({g.utilization_pct}%  {g.memory_used_mb/1024:.0f}/{g.memory_total_mb/1024:.0f}G)"
                        for g in gpus
                    )
                    for h, gpus in by_host.items()
                )
                send_notification(title, subtitle, body)
                live.console.log(f"[bold green][通知][/bold green] {title}  {subtitle}")

            live.update(build_table(statuses, config))
            await asyncio.sleep(config.poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="监控远程机器 GPU 空闲状态")
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="配置文件路径（默认 config.yaml）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只查询一次并打印结果，不持续监控",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="覆盖配置文件中的轮询间隔（秒）",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    if args.interval is not None:
        config.poll_interval = args.interval

    try:
        asyncio.run(run(config, once=args.once))
    except KeyboardInterrupt:
        console.print("\n[dim]已停止。[/dim]")


if __name__ == "__main__":
    main()
