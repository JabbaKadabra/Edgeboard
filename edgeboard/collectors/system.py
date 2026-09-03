"""System metrics: CPU, memory, disks, network and GPU (NVIDIA or AMD)."""

from __future__ import annotations

import shutil
import subprocess
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil

HISTORY = 120
CPU_SENSOR_PREFERENCE = [
    ("k10temp", "Tctl"),
    ("k10temp", "Tdie"),
    ("zenpower", "Tdie"),
    ("zenpower", "Tctl"),
    ("coretemp", "Package id 0"),
    ("cpu_thermal", ""),
    ("acpitz", ""),
]


@dataclass
class GpuState:
    name: str = ""
    percent: float | None = None
    temp: float | None = None
    mem_used: int | None = None
    mem_total: int | None = None
    vendor: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def pick_cpu_temp(temps: dict[str, list]) -> float | None:
    """Pick the most meaningful package temperature from psutil's structure."""
    def current(entry) -> float | None:
        value = getattr(entry, "current", None)
        if value is None and isinstance(entry, dict):
            value = entry.get("current")
        return float(value) if value is not None else None

    def label(entry) -> str:
        value = getattr(entry, "label", None)
        if value is None and isinstance(entry, dict):
            value = entry.get("label")
        return value or ""

    for chip, wanted in CPU_SENSOR_PREFERENCE:
        for entry in temps.get(chip, []):
            if not wanted or label(entry) == wanted:
                value = current(entry)
                if value is not None and value > 0:
                    return value
    for entries in temps.values():
        for entry in entries:
            value = current(entry)
            if value is not None and value > 0:
                return value
    return None


def parse_nvidia_smi(line: str) -> GpuState:
    parts = [p.strip() for p in line.strip().split(",")]
    if len(parts) < 5:
        raise ValueError(f"unexpected nvidia-smi output: {line!r}")

    def num(value: str) -> float | None:
        try:
            return float(value)
        except ValueError:
            return None

    mem_used = num(parts[3])
    mem_total = num(parts[4])
    return GpuState(
        name=parts[0],
        percent=num(parts[1]),
        temp=num(parts[2]),
        mem_used=int(mem_used * 1024 * 1024) if mem_used is not None else None,
        mem_total=int(mem_total * 1024 * 1024) if mem_total is not None else None,
        vendor="nvidia",
    )


def read_nvidia_gpu() -> GpuState | None:
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return parse_nvidia_smi(proc.stdout.splitlines()[0])
    except ValueError:
        return None


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def read_amd_gpu(sysfs_root: Path = Path("/sys/class/drm")) -> GpuState | None:
    if not sysfs_root.is_dir():
        return None
    for card in sorted(sysfs_root.glob("card[0-9]*")):
        if "-" in card.name:
            continue  # connectors like card0-DP-1
        device = card / "device"
        busy = _read_int(device / "gpu_busy_percent")
        if busy is None:
            continue
        temp = None
        for hwmon in sorted((device / "hwmon").glob("hwmon*")) if (device / "hwmon").is_dir() else []:
            raw = _read_int(hwmon / "temp1_input")
            if raw is not None:
                temp = raw / 1000
                break
        name = "AMD GPU"
        try:
            name = (device / "product_name").read_text().strip() or name
        except OSError:
            pass
        return GpuState(
            name=name,
            percent=float(busy),
            temp=temp,
            mem_used=_read_int(device / "mem_info_vram_used"),
            mem_total=_read_int(device / "mem_info_vram_total"),
            vendor="amd",
        )
    return None


def read_gpu() -> GpuState | None:
    return read_nvidia_gpu() or read_amd_gpu()


class SystemSampler:
    """Stateful sampler; call ``sample()`` about once per second."""

    def __init__(self, gpu_reader=read_gpu, mounts: tuple[str, ...] = ("/", "/home")):
        self.gpu_reader = gpu_reader
        self.mounts = mounts
        self.cpu_hist: deque[float] = deque(maxlen=HISTORY)
        self.gpu_hist: deque[float] = deque(maxlen=HISTORY)
        self.rx_hist: deque[float] = deque(maxlen=HISTORY)
        self.tx_hist: deque[float] = deque(maxlen=HISTORY)
        self._last_net = None
        self._last_time = None
        self._gpu_every = 2
        self._tick = 0
        self._gpu: GpuState | None = None
        psutil.cpu_percent(percpu=True)  # prime the counter

    def _disks(self) -> list[dict]:
        seen = set()
        result = []
        for mount in self.mounts:
            try:
                usage = psutil.disk_usage(mount)
            except OSError:
                continue
            key = (usage.total, usage.used)
            if key in seen:
                continue  # /home on the same filesystem as /
            seen.add(key)
            result.append({"mount": mount, "percent": usage.percent, "used": usage.used, "total": usage.total})
        return result

    def _net(self) -> dict:
        now = time.monotonic()
        counters = psutil.net_io_counters()
        rx_bps = tx_bps = 0.0
        if self._last_net is not None and self._last_time is not None:
            dt = max(now - self._last_time, 1e-3)
            rx_bps = max(0.0, (counters.bytes_recv - self._last_net.bytes_recv) / dt)
            tx_bps = max(0.0, (counters.bytes_sent - self._last_net.bytes_sent) / dt)
        self._last_net, self._last_time = counters, now
        self.rx_hist.append(rx_bps)
        self.tx_hist.append(tx_bps)
        return {"rx_bps": rx_bps, "tx_bps": tx_bps}

    def sample(self) -> dict:
        per_core = psutil.cpu_percent(percpu=True)
        cpu_percent = sum(per_core) / len(per_core) if per_core else 0.0
        self.cpu_hist.append(cpu_percent)
        try:
            temps = psutil.sensors_temperatures()
        except (AttributeError, OSError):
            temps = {}
        freq = None
        try:
            f = psutil.cpu_freq()
            freq = f.current if f else None
        except (OSError, RuntimeError):
            pass
        mem = psutil.virtual_memory()
        if self._tick % self._gpu_every == 0:
            try:
                self._gpu = self.gpu_reader()
            except Exception:  # noqa: BLE001 - a GPU reader must never kill the sampler
                self._gpu = None
        self._tick += 1
        if self._gpu and self._gpu.percent is not None:
            self.gpu_hist.append(self._gpu.percent)
        load1, load5, load15 = psutil.getloadavg()
        return {
            "cpu": {
                "percent": round(cpu_percent, 1),
                "per_core": [round(c, 1) for c in per_core],
                "temp": pick_cpu_temp(temps),
                "freq_mhz": round(freq) if freq else None,
            },
            "mem": {"percent": mem.percent, "used": mem.used, "total": mem.total},
            "gpu": self._gpu.to_dict() if self._gpu else None,
            "disks": self._disks(),
            "net": self._net(),
            "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
            "uptime_s": int(time.time() - psutil.boot_time()),
            "history": {
                "cpu": list(self.cpu_hist),
                "gpu": list(self.gpu_hist),
                "rx": list(self.rx_hist),
                "tx": list(self.tx_hist),
            },
        }
