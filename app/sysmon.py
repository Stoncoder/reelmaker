"""System usage for the usage bar: CPU, GPU, memory and network download speed — macOS and Windows.

Sampling runs in its own small process (python -I sysmon.py <file> <ask-file>) so heavy work inside Reelmaker
(numpy, OpenCV) can never freeze the readings. It writes the latest sample to a file about once a second and
exits by itself 60 s after the page stops asking (so gaps between reels in a batch don't restart it).
CPU, memory and network come from psutil; the GPU from the Apple GPU driver (Mac), nvidia-smi or Windows'
own GPU counters (PC).
"""
from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STATE = Path(tempfile.gettempdir()) / f"reelmaker-sysmon-{re.sub(r'[^A-Za-z0-9]', '_', getpass.getuser())}.json"
ASK = STATE.with_suffix(".ask")
_proc: subprocess.Popen | None = None
IS_MAC, IS_WIN = sys.platform == "darwin", os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0


def _gpu_mac():
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-r", "-d", "1", "-c", "IOAccelerator"], capture_output=True, text=True, timeout=3).stdout
        vals = [int(v) for v in re.findall(r'"Device Utilization %"=(\d+)', out)]
        return float(max(vals)) if vals else None
    except Exception:
        return None


_NVSMI = shutil.which("nvidia-smi")


def _gpu_win():
    try:
        if _NVSMI:   # NVIDIA: quick and exact
            out = subprocess.run([_NVSMI, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=3, creationflags=NO_WINDOW).stdout
            vals = [float(v) for v in out.split() if v.strip().replace(".", "", 1).isdigit()]
            if vals:
                return max(vals)
        # any GPU (Intel / AMD / NVIDIA): Windows' "GPU Engine" counters, busiest engine type
        ps = ("$s=(Get-Counter '\\GPU Engine(*)\\Utilization Percentage' -ErrorAction SilentlyContinue).CounterSamples;"
              "$g=$s | Group-Object {($_.InstanceName -split '_engtype_')[-1]} | ForEach-Object {($_.Group | Measure-Object CookedValue -Sum).Sum};"
              "[Console]::Out.Write(($g | Measure-Object -Maximum).Maximum)")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=6, creationflags=NO_WINDOW).stdout.strip()
        return min(100.0, round(float(out), 1)) if out else None
    except Exception:
        return None


def _gpu():
    return _gpu_mac() if IS_MAC else _gpu_win() if IS_WIN else None


def _worker(out_path: Path, ask_path: Path):
    import psutil
    psutil.cpu_percent(None)   # the first reading is "since boot": prime it
    prev_net = psutil.net_io_counters().bytes_recv
    prev_t = time.monotonic()
    while True:
        time.sleep(1.0)
        cpu = min(100.0, round(psutil.cpu_percent(None), 1))
        vm = psutil.virtual_memory()
        net, now = psutil.net_io_counters().bytes_recv, time.monotonic()
        down = max(0.0, (net - prev_net) / (now - prev_t)) if now > prev_t else None
        prev_net, prev_t = net, now
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"cpu": cpu, "gpu": _gpu(), "mem_used": vm.total - vm.available, "mem_total": vm.total,
                                   "net_down": down, "at": time.time()}))
        os.replace(tmp, out_path)
        try:
            if time.time() - ask_path.stat().st_mtime > 60:
                break
        except FileNotFoundError:
            break


def usage() -> dict:
    """Latest sample; starts the sampler process if it isn't running."""
    global _proc
    ASK.touch()
    if _proc is None or _proc.poll() is not None:
        _proc = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), str(STATE), str(ASK)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        d = json.loads(STATE.read_text())
        return d if time.time() - d.get("at", 0) < 10 else {}
    except Exception:
        return {}


def stop():
    if _proc is not None and _proc.poll() is None:
        _proc.kill()


if __name__ == "__main__":
    _worker(Path(sys.argv[1]), Path(sys.argv[2]))
