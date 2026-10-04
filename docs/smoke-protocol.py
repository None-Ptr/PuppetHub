"""控制面外壳冒烟：直接用协议驱动 `python -m puppethub.protocol`。

为什么单独测它：`自证`按钮与将来的 puppetOS 都走这条路，而它最容易坏的**不是语义**
（语义由 `puppet` 仓的用例保证），而是**桥接**——隐藏窗口、stdin 线程、截图重试这三处。
语义问题会在 conformance 里暴露，桥接问题只会让人看到一个"没反应"的子进程。

用法：python docs/smoke-protocol.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def start() -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen([sys.executable, "-m", "puppethub.protocol"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            env=env, cwd=str(ROOT))


def request(proc: subprocess.Popen, payload: dict) -> dict:
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("实现进程未响应即退出（stderr: %s）" % _stderr(proc))
    return json.loads(line)


def _stderr(proc: subprocess.Popen) -> str:
    try:
        proc.kill()
        _, err = proc.communicate(timeout=3)
        return (err or "").strip()[:600]
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "失败", label,
                               ("  ← " + detail) if detail and not ok else ""))
        if not ok:
            failures.append(label)

    proc = start()
    try:
        hello = request(proc, {"op": "hello"})
        rendering = hello.get("rendering") or {}
        print("握手：protocol=%s · controls=%d · geometry=%s snapshot=%s interaction=%s"
              % (hello.get("protocol"), len(rendering.get("controls") or []),
                 rendering.get("geometry"), rendering.get("snapshot"),
                 rendering.get("interaction")))
        check("hello 返回渲染自述", bool(rendering.get("controls")))
        check("geometry 诚实降级为 false", rendering.get("geometry") is False)

        program = ["add #root window #win w=200 h=120",
                   "add #win text #t text=\"未点\"",
                   "add #win button #b text=\"点我\"",
                   "on #b click: set #t text=\"已点\""]
        resp = request(proc, {"op": "load", "program": program})
        check("load 无错误诊断",
              not [d for d in resp.get("diagnostics", []) if d.get("level") == "error"],
              str(resp.get("diagnostics")))

        snap = request(proc, {"op": "observe"})
        check("observe 带节点", "#b" in (snap.get("nodes") or []), str(snap.get("nodes")))

        resp = request(proc, {"op": "interact", "target": "#b", "action": "click"})
        check("动作经真部件投递", resp.get("delivered") is True, str(resp))
        attrs = request(proc, {"op": "observe"}).get("attrs", {})
        check("处理器确实跑了", attrs.get("#t.text") == "已点", str(attrs.get("#t.text")))

        resp = request(proc, {"op": "snapshot"})
        image = resp.get("image")
        check("截图真的产出图像", bool(image), str(resp)[:200])
        if image:
            print("      PNG base64 长度 = %d" % len(image))

        resp = request(proc, {"op": "dump"})
        check("dump 返回源文本", bool(resp.get("program")), str(resp)[:120])

        resp = request(proc, {"op": "send", "batch": ["add #win text #t2 text=\"x\""]})
        check("命令批可用", "diagnostics" in resp, str(resp)[:120])

        resp = request(proc, {"op": "quit"})
        check("quit 收尾", resp.get("ok") is True, str(resp))

        if "--full" in sys.argv:
            # 走**驾驶舱"自证"按钮同一条内部接口**跑全量（分钟级）。
            from puppethub.verify import run_conformance
            print("\n自证全量（与按钮同一条路径）：")
            streamed: list[str] = []
            result = run_conformance(on_line=streamed.append)
            for line in streamed[:10]:
                print("   " + line)
            check("自证全绿", result["ok"], result.get("error") or result.get("summary"))
            for line in result["failed"][:10]:
                print("   " + line)
    finally:
        if proc.poll() is None:
            proc.kill()
        err = proc.stderr.read() or ""
        interesting = [line for line in err.splitlines()
                       if line.strip() and "[render]" in line]
        if interesting:
            print("渲染器上报（stderr，仅供人看）：")
            for line in interesting[:6]:
                print("   " + line)
        elif err.strip():
            print("子进程 stderr（前 6 行）：")
            for line in err.strip().splitlines()[:6]:
                print("   " + line)

    print("\n%s（%d 项失败）" % ("全部通过" if not failures else "有失败", len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
