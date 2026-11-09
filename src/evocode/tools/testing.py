import subprocess
import sys
from pathlib import Path


def run_tests(repo_dir: Path) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "pytest", "-q"]

    return subprocess.run(
        command,
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
