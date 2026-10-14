import json
from pathlib import Path
import subprocess
import sys
import time


def run_process_workers(db, job_id, tmp_path):
    root = Path(__file__).resolve().parents[1]
    url = db["engine"].url.render_as_string(hide_password=False)
    gate = tmp_path / "worker-gate"
    ready = [tmp_path / f"worker-ready-{i}" for i in range(2)]
    processes = [subprocess.Popen(
        [sys.executable, "-m", "tests.stage1_process_runner", url, job_id, str(marker), str(gate)],
        cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) for marker in ready]
    try:
        deadline = time.monotonic() + 15
        while not all(marker.exists() for marker in ready):
            if any(process.poll() is not None for process in processes) or time.monotonic() >= deadline:
                raise AssertionError("Worker processes failed to start")
            time.sleep(0.01)
        gate.touch()
        output = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            output.append(json.loads(stdout))
        return output
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
