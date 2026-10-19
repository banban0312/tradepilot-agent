"""运行悬浮窗与全屏共享状态的前端行为回归。"""

from __future__ import annotations

import subprocess
from pathlib import Path
import shutil


CODE_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_ROOT = CODE_ROOT / "frontend"


def test_shared_task_workspace_frontend_suite() -> None:
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    assert npm is not None, "未找到 npm；请先安装 Node.js 与 npm。"
    result = subprocess.run(
        f'"{npm}" run test:unit -- --reporter=dot',
        cwd=FRONTEND_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        shell=True,
    )
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, output
    assert "5 passed" in output, output
