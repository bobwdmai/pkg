import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RunResult:
    return_code: int
    stdout: str
    stderr: str
    command: str
    file_path: str


RUNNER_BY_EXTENSION = {
    ".py": ["python3"],
    ".sh": ["bash"],
    ".bash": ["bash"],
    ".zsh": ["zsh"],
    ".js": ["node"],
    ".mjs": ["node"],
    ".cjs": ["node"],
    ".rb": ["ruby"],
    ".pl": ["perl"],
    ".php": ["php"],
}


def run_source_code(source_code: str, file_extension: str = ".py", timeout: int = 20) -> RunResult:
    run_dir = Path("/tmp/ai_os_runs")
    run_dir.mkdir(parents=True, exist_ok=True)
    normalized_ext = (file_extension or ".py").strip().lower()
    if not normalized_ext.startswith("."):
        normalized_ext = f".{normalized_ext}"
    file_name = f"snippet_{int(time.time() * 1000)}{normalized_ext}"
    file_path = run_dir / file_name
    file_path.write_text(source_code, encoding="utf-8")

    runner = RUNNER_BY_EXTENSION.get(normalized_ext, ["python3"])
    cmd = [*runner, str(file_path)]
    env = dict(os.environ)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return RunResult(
        return_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        command=" ".join(cmd),
        file_path=str(file_path),
    )
