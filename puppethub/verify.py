"""自证：跑 conformance，被测实现是**产品渲染器**（`puppethub.protocol`）。

- **干净环境**：只跑内置实现，不加载用户插件——用户插件的 bug 不该让自证变红，
  否则"自证"就变成一个不稳定的信号，人就不看了。
- **一份实现两个入口**：驾驶舱的"自证"按钮与将来 CI 走的都是这里；
  结果以**行**流回调用方，由它转进观察流（不在这里 print，那是调用方的事）。
- **不吞失败**：退出码、失败清单、汇总行一起返回；找不到运行器也要说清为什么。
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Callable, Optional

from puppet import assets_dir, conformance_dir

# 汇总行与失败行之外的 "ok …" 不往观察流里灌：那是绝大多数，灌进去面板就没人看了。
_INTERESTING = ("FAIL", "通过", "跳过", "实现进程", "用例文件", "Traceback", "Error")


def run_conformance(*, on_line: Optional[Callable[[str], None]] = None,
                    filter_text: str = "") -> dict:
    """跑一遍自证。返回 `{ok, returncode, summary, failed, lines, total}`。"""
    directory = conformance_dir()
    runner = os.path.join(directory, "runner.py")
    if not os.path.isfile(runner):
        message = ("找不到 conformance 运行器：%s（语言包安装不完整？"
                   "或 PUPPET_ASSETS 指错了地方）" % runner)
        if on_line is not None:
            on_line(message)
        return {"ok": False, "error": message, "summary": "", "failed": [],
                "lines": [], "total": 0, "returncode": -1}

    if getattr(sys, "frozen", False):
        # **冻结态：runner 进程内执行**。实测教训：verify-ci → runner → 被测实现
        # 是三层 onefile 嵌套，三份解包目录的清理互相踩踏，跑到中段子进程就静默
        # 死掉（套件只跑到一半、没有汇总行）。进程内执行少一层嵌套；被测实现
        # 仍由 runner 以 `exe -m 模块` 拉子进程（cli 的 -m 垫片负责）。
        import contextlib
        import io
        import runpy
        old_argv = sys.argv
        buffer = io.StringIO()
        code = 0
        sys.argv = [runner, "--impl-module", "puppethub.protocol"] + \
            (["--filter", filter_text] if filter_text else [])
        try:
            with contextlib.redirect_stdout(buffer):
                runpy.run_path(runner, run_name="__main__")
        except SystemExit as ex:
            code = int(ex.code or 0)
        finally:
            sys.argv = old_argv
        lines = [line.rstrip() for line in buffer.getvalue().splitlines()]
    else:
        code, lines = _spawn_runner(runner, directory, filter_text, on_line)

    summary = next((line.strip() for line in reversed(lines) if "通过" in line), "")
    failed = [line.strip() for line in lines if line.strip().startswith("FAIL")]
    ok = code == 0 and not failed
    if on_line is not None:
        on_line("自证结束：%s（退出码 %d）" % (summary or "没有汇总行", code))
    return {"ok": ok, "returncode": code, "summary": summary,
            "failed": failed[:20], "lines": lines[-60:], "total": len(lines)}


def _spawn_runner(runner: str, directory: str, filter_text: str,
                  on_line: Optional[Callable[[str], None]]) -> tuple:
    """开发态：子进程跑 runner，逐行回流。"""
    env = dict(os.environ)
    # 子进程要能 import 到本包——开发态（未 pip install）时这一条是必需的。
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PUPPET_ASSETS"] = assets_dir()

    command = [sys.executable, runner, "--impl-module", "puppethub.protocol"]
    if filter_text:
        command += ["--filter", filter_text]
    if on_line is not None:
        on_line("自证开始：%s" % " ".join(command[1:]))
    proc = subprocess.Popen(command, cwd=os.path.dirname(directory), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    lines: list[str] = []
    for raw in proc.stdout:
        line = raw.rstrip()
        lines.append(line)
        if on_line is not None and any(token in line for token in _INTERESTING):
            on_line(line)
    return proc.wait(), lines
