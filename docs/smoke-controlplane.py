"""控制面冒烟：`run --no-llm`（无 LLM 实例）——驱动者即写者。

它验证的不是"协议能收发"（那由 `smoke-protocol.py` 覆盖），而是**绑到 app 目录之后**的三件事：

1. `send` 走的是与 LLM **完全相同**的写入路径：引擎校验 → 批末写回真源 → **写前自动快照**。
2. `hello` 的能力目录**从 capabilities.py 派生**（不手写、不漂移）。
3. 整份替换（`load`）**被拒绝并说明理由**——那是不可回滚的确认式系统动作，
   而控制面协议没有"人确认"这个通道。

用法：python docs/smoke-controlplane.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CAPABILITIES = '''from puppet import capability

REQUIRES: list[str] = []


@capability(returns="dict")
def ping() -> dict:
    """冒烟用：回一个 pong。"""
    return {"pong": True}


@capability(returns="str")
def echo(text: str) -> str:
    """冒烟用：原样返回（不列入借出清单 → 服务面必须拒绝）。"""
    return text
'''

BATCH = [
    'add #content text #t text="未点"',
    'add #content button #b text="点我"',
    'data #todos = [] of {text: str}',
    'on #b click:',
    '    append #todos item={text: "一条"}',
    '    set #t text="已点"',
]


def start(app_dir: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen([sys.executable, "-m", "puppethub.controlplane", str(app_dir)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            env=env, cwd=str(ROOT))


def request(proc: subprocess.Popen, payload: dict) -> dict:
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("控制面未响应即退出（stderr: %s）" % _stderr(proc))
    return json.loads(line)


def _stderr(proc: subprocess.Popen) -> str:
    try:
        proc.kill()
        _, err = proc.communicate(timeout=3)
        return (err or "").strip()[:600]
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    from puppethub.appdir import create_app

    work = Path(tempfile.mkdtemp(prefix="puppethub-cp-"))
    app = create_app(work / "app", "cp-smoke", "控制面冒烟")
    app.capabilities_path.write_text(CAPABILITIES, encoding="utf-8")
    # 借出清单：只借 ping（echo 不在列 → 服务面必须默认拒绝）
    with open(app.config_path, "a", encoding="utf-8") as fh:
        fh.write('\n[service]\nlend = ["ping"]\n')

    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "失败", label,
                               ("  ← " + detail) if detail and not ok else ""))
        if not ok:
            failures.append(label)

    proc = start(app.root)
    try:
        hello = request(proc, {"op": "hello"})
        names = [item["name"] for item in hello.get("catalog") or []]
        print("握手：protocol=%s · 渲染自述 %d 项能力 · catalog=%s"
              % (hello.get("protocol"), len(hello.get("rendering", {}).get("controls") or []),
                 names or "（空）"))
        check("hello 带渲染自述", bool(hello.get("rendering", {}).get("controls")))
        check("能力目录从 capabilities.py 派生", names == ["echo", "ping"], str(names))

        source_before = "\n".join(app.read_source())
        resp = request(proc, {"op": "send", "batch": BATCH})
        errors = [d for d in resp.get("diagnostics", []) if d.get("level") == "error"]
        check("命令批无错误", not errors, str(errors))

        source_after = "\n".join(app.read_source())
        check("真源被写回（不是只改了内存）",
              source_after != source_before and "已点" in source_after,
              source_after[:200])
        snapshots = app.list_snapshots()
        check("写前自动快照已产生", bool(snapshots), str(snapshots))
        if snapshots:
            print("     快照：%s · %s" % (snapshots[0].id, snapshots[0].reason))

        resp = request(proc, {"op": "dump"})
        check("dump 与磁盘上的真源一致",
              "\n".join(resp.get("program") or []) == source_after,
              str(resp.get("program"))[:160])

        resp = request(proc, {"op": "interact", "target": "#b", "action": "click"})
        check("经真部件投递动作", resp.get("delivered") is True, str(resp))
        attrs = request(proc, {"op": "observe"}).get("attrs", {})
        check("处理器跑了（界面真的会变）", attrs.get("#t.text") == "已点",
              str(attrs.get("#t.text")))
        data = request(proc, {"op": "observe"}).get("data", {})
        check("状态（不是程序）也变了", len(data.get("#todos") or []) == 1,
              str(data.get("#todos")))

        resp = request(proc, {"op": "snapshot"})
        check("控制面也能截图", bool(resp.get("image")), str(resp)[:160])

        resp = request(proc, {"op": "load", "program": ["add #root window #win"]})
        check("整份替换被拒绝且说明理由",
              "error" in resp and "命令批" in resp.get("error", ""), str(resp)[:200])

        print("\n服务面（V4 B）：stdio 外壳与 TCP 外壳一致")
        resp = request(proc, {"op": "call", "name": "ping", "args": {}})
        check("借出能力可同步调用", resp.get("ok") is True
              and resp.get("value") == {"pong": True}, str(resp))
        resp = request(proc, {"op": "call", "name": "echo", "args": {"text": "嗨"}})
        check("未列入借出清单一律拒绝（默认拒绝）",
              resp.get("ok") is False and "未借出" in (resp.get("error") or ""),
              str(resp))
        resp = request(proc, {"op": "deliver_peer_event", "from_app": "x",
                              "topic": "t", "text": "y"})
        check("stdio 不是总线端点：如实拒绝并说明（不装作收到）",
              "error" in resp and "远程实例" in resp.get("error", ""), str(resp))

        resp = request(proc, {"op": "quit"})
        check("quit 收尾", resp.get("ok") is True, str(resp))
    finally:
        if proc.poll() is None:
            proc.kill()
        err = proc.stderr.read() or ""
        render_notes = [line for line in err.splitlines() if "[render]" in line]
        if render_notes:
            print("渲染器上报（stderr，仅供人看）：")
            for line in render_notes[:5]:
                print("   " + line)

    print("\n%s（%d 项失败）" % ("全部通过" if not failures else "有失败", len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
