import os
import shutil
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
    ".lua": ["lua"],
    ".r": ["Rscript"],
    ".swift": ["swift"],
    ".ps1": ["pwsh", "-File"],
    ".ts": ["node", "--loader", "ts-node/esm"],
}


def _safe_run(cmd: list[str], timeout: int, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def _compiled_run_cmd(file_path: Path) -> list[str] | None:
    ext = file_path.suffix.lower()
    stem = file_path.stem
    run_dir = file_path.parent
    bin_path = run_dir / f"{stem}_bin"

    if ext == ".c":
        if shutil.which("gcc") is None:
            return None
        return ["bash", "-lc", f"gcc '{file_path}' -o '{bin_path}' && '{bin_path}'"]
    if ext in {".cc", ".cpp", ".cxx"}:
        compiler = "g++" if shutil.which("g++") else ("clang++" if shutil.which("clang++") else "")
        if not compiler:
            return None
        return ["bash", "-lc", f"{compiler} '{file_path}' -o '{bin_path}' && '{bin_path}'"]
    if ext == ".go":
        if shutil.which("go") is None:
            return None
        return ["go", "run", str(file_path)]
    if ext == ".rs":
        if shutil.which("rustc") is None:
            return None
        return ["bash", "-lc", f"rustc '{file_path}' -o '{bin_path}' && '{bin_path}'"]
    if ext == ".java":
        if shutil.which("javac") is None or shutil.which("java") is None:
            return None
        class_name = file_path.stem
        return ["bash", "-lc", f"cd '{run_dir}' && javac '{file_path.name}' && java {class_name}"]
    if ext == ".kt":
        if shutil.which("kotlinc") is None:
            return None
        jar_path = run_dir / f"{stem}.jar"
        return ["bash", "-lc", f"kotlinc '{file_path}' -include-runtime -d '{jar_path}' && java -jar '{jar_path}'"]
    return None


def run_source_code(source_code: str, file_extension: str = ".py", timeout: int = 20) -> RunResult:
    run_dir = Path("/tmp/ai_os_runs")
    run_dir.mkdir(parents=True, exist_ok=True)
    normalized_ext = (file_extension or ".py").strip().lower()
    if not normalized_ext.startswith("."):
        normalized_ext = f".{normalized_ext}"
    file_name = f"snippet_{int(time.time() * 1000)}{normalized_ext}"
    file_path = run_dir / file_name
    file_path.write_text(source_code, encoding="utf-8")

    env = dict(os.environ)
    cmd: list[str]

    compiled_cmd = _compiled_run_cmd(file_path)
    if compiled_cmd is not None:
        cmd = compiled_cmd
    else:
        runner = RUNNER_BY_EXTENSION.get(normalized_ext, ["python3"])
        if runner and shutil.which(runner[0]) is None:
            return RunResult(
                return_code=127,
                stdout="",
                stderr=f"Runtime '{runner[0]}' not found for extension {normalized_ext}.",
                command=" ".join(runner + [str(file_path)]),
                file_path=str(file_path),
            )
        cmd = [*runner, str(file_path)]

    proc = _safe_run(cmd=cmd, timeout=timeout, env=env)
    return RunResult(
        return_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        command=" ".join(cmd),
        file_path=str(file_path),
    )
