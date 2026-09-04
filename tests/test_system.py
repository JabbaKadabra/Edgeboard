from collections import namedtuple
from pathlib import Path

import pytest

from edgeboard.collectors.system import GpuState, SystemSampler, parse_nvidia_smi, pick_cpu_temp, read_amd_gpu

T = namedtuple("shwtemp", "label current high critical")


def test_pick_cpu_temp_prefers_package_sensors():
    temps = {
        "nvme": [T("Composite", 41.0, None, None)],
        "coretemp": [T("Core 0", 55.0, None, None), T("Package id 0", 61.0, None, None)],
        "k10temp": [T("Tctl", 64.5, None, None)],
    }
    assert pick_cpu_temp(temps) == 64.5
    del temps["k10temp"]
    assert pick_cpu_temp(temps) == 61.0
    assert pick_cpu_temp({"nvme": [T("Composite", 41.0, None, None)]}) == 41.0
    assert pick_cpu_temp({"k10temp": [T("Tctl", 0.0, None, None)], "nvme": [T("Composite", 41.0, None, None)]}) == 41.0
    assert pick_cpu_temp({}) is None


def test_parse_nvidia_smi():
    gpu = parse_nvidia_smi("NVIDIA GeForce RTX 4080, 12, 55, 2048, 16376\n")
    assert gpu.name == "NVIDIA GeForce RTX 4080"
    assert gpu.percent == 12.0 and gpu.temp == 55.0
    assert gpu.mem_used == 2048 * 1024 * 1024 and gpu.mem_total == 16376 * 1024 * 1024
    with pytest.raises(ValueError):
        parse_nvidia_smi("garbage")


def test_read_amd_gpu(tmp_path: Path):
    assert read_amd_gpu(tmp_path) is None
    device = tmp_path / "card1" / "device"
    (device / "hwmon" / "hwmon3").mkdir(parents=True)
    (tmp_path / "card1-DP-1").mkdir()
    (device / "gpu_busy_percent").write_text("37\n")
    (device / "hwmon" / "hwmon3" / "temp1_input").write_text("58000\n")
    (device / "mem_info_vram_used").write_text("1000\n")
    (device / "mem_info_vram_total").write_text("4000\n")
    gpu = read_amd_gpu(tmp_path)
    assert gpu.vendor == "amd" and gpu.percent == 37.0 and gpu.temp == 58.0
    assert gpu.mem_used == 1000 and gpu.mem_total == 4000


def test_sampler_shape():
    sampler = SystemSampler(gpu_reader=lambda: GpuState(name="fake", percent=5.0, temp=40.0, vendor="test"))
    first = sampler.sample()
    second = sampler.sample()
    for key in ("cpu", "mem", "gpu", "disks", "net", "load", "uptime_s", "history"):
        assert key in second
    assert second["net"]["rx_bps"] >= 0
    assert second["gpu"]["name"] == "fake"
    assert len(second["history"]["cpu"]) == 2
    assert first["mem"]["total"] > 0
    assert second["disks"][0]["mount"] == "/"


def test_sampler_survives_gpu_reader_crash():
    def boom():
        raise RuntimeError("no gpu")

    assert SystemSampler(gpu_reader=boom).sample()["gpu"] is None


def test_history_carries_only_what_the_page_draws():
    # The page draws a CPU and a GPU trace; net rates are in the one-line summary
    # only, so their history would be 240 floats per snapshot for nothing.
    sampler = SystemSampler(gpu_reader=lambda: GpuState(name="fake", percent=5.0))
    sampler.sample()
    assert set(sampler.sample()["history"]) == {"cpu", "gpu"}
