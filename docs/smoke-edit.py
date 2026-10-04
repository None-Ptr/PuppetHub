"""正式手写程序模式冒烟：编辑器整份替换 = 确认式 load_source。

要验证的：绕过对话，**不绕过纪律**——内容有变才动手；动手前有兜底快照；
整份替换走 load_source（与推倒重来同一通道）；决策流水留痕；无变化时零动作。

用法：python docs/smoke-edit.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from puppethub.appdir import create_app

    work = Path(tempfile.mkdtemp(prefix="puppethub-edit-"))
    app = create_app(work, "ed-app", "编辑冒烟")
    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "失败", label,
                               ("  ← " + detail) if detail and not ok else ""))
        if not ok:
            failures.append(label)

    # 假编辑器：往文件尾追加一行（Windows 下 EDITOR 以 .cmd 形式被 subprocess 调起）
    fake = work / "fake-edit.cmd"
    # 编辑器拿到的是文件路径 = %1（cmd 变量从 1 起算）
    fake.write_text('@echo add #content text #edited text="hand-written">>%1\n',
                    encoding="ascii")

    def run_edit() -> subprocess.CompletedProcess:
        env = dict(os.environ, EDITOR=str(fake), PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-m", "puppethub", "edit",
                               str(app.root), "--yes"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env)

    print("1) 编辑有变化：整份替换 + 兜底快照 + 决策流水")
    result = run_edit()
    check("退出码 0", result.returncode == 0, (result.stdout + result.stderr)[-200:])
    check("手写内容进了真源",
          any("hand-written" in line for line in app.read_source()))
    check("决策流水留痕", "手写程序模式" in app.read_design())

    print("\n2) 编辑无变化：零动作（连诊断都不该有）")
    noop = work / "noop-edit.cmd"
    noop.write_text("rem no-op\n", encoding="ascii")
    env = dict(os.environ, EDITOR=str(noop), PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-m", "puppethub", "edit",
                             str(app.root), "--yes"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", env=env)
    check("退出码 0 且明确说没做", result.returncode == 0 and "未变化" in result.stdout,
          (result.stdout + result.stderr)[-200:])

    print("\n3) 写进的是非法程序：拒绝采用 + 真源恢复原样 + 拒稿留底")
    fake.write_text('@echo this is not puppet>>%1\n', encoding="ascii")
    result = run_edit()
    check("非法程序被拒且可见", result.returncode == 1 and "静态校验" in (result.stdout + result.stderr),
          (result.stdout + result.stderr)[-300:])
    check("真源恢复原样（编辑器弄脏的部分被撤掉）",
          any("hand-written" in line for line in app.read_source())
          and not any("this is not puppet" in line for line in app.read_source()))
    check("拒稿留了底（人的工作不丢）",
          (app.root / "app.puppet.rejected").is_file()
          and "this is not puppet" in (app.root / "app.puppet.rejected").read_text(encoding="utf-8"))

    shutil.rmtree(work, ignore_errors=True)
    print("\n%s（%d 项失败）" % ("全部通过" if not failures else "有失败", len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
