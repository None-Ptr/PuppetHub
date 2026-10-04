"""设备打包冒烟：`puppethub build --target android` 生成的独立 flet 工程。

要验证的是生成工程的**真实性**，不是"文件存在"：
- 生成的 main.py 自述 `pointer=touch`（规范 05 §10——触摸宿主义务）；
- 生成的工程**能独立装载**：create_engine() 在子进程里跑，真源/能力/资产全进；
- REQUIRES 进了 requirements.txt，且**构建期解析**：已装的包导出成功、
  缺的包**生成期就拒**（运行期才炸不可接受）；
- 凭据 manifest：capabilities.py 里读的环境变量被静态扫进 manifest.json；
- 交互通路成立：fire 输入 → fire 点击 → 数据真的变了（不是"看起来有按钮"）；
- 不合法程序被拒（导出装不出来的 app = 假成功）；desktop target 被拒（那是 run）；
- flet CLI 不在时 --run-flet-build 如实上报（不假装构建成功）。

用法：python docs/smoke-build.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROGRAM = [
    'add #root window #win title="手机上的待办"',
    'add #win col #content pad=16 gap=12',
    'data #todos = [] of {text: str}',
    'add #content text #title text="待办"',
    'add #content input #inp placeholder="新待办"',
    'on #inp submit as e:',
    '    append #todos item={text: e.value} ; set #inp value=""',
]


def main() -> int:
    from puppethub.appdir import AppDir, create_app
    from puppethub.builder import export_project, run_flet_build

    work = Path(tempfile.mkdtemp(prefix="puppethub-build-"))
    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print("  [%s] %s%s" % ("ok" if ok else "失败", label,
                               ("  ← " + detail) if detail and not ok else ""))
        if not ok:
            failures.append(label)

    app = create_app(work, "todo", "手机上的待办")
    app.write_source(list(PROGRAM))
    # setuptools：任何 Python 环境（含 CI 的 venv）必装——REQUIRES 构建期解析
    # 的"已装"正例。凭据正例：os.environ.get 的字面量被扫进 manifest。
    app.write_capabilities(
        '"""能力。"""\nimport os\n\nfrom puppet import capability\n\n'
        'REQUIRES: list[str] = ["setuptools"]\n\n\n'
        '@capability(returns="str")\ndef shout(text: str) -> str:\n'
        '    """原样返回（带凭据时加前缀）。"""\n'
        '    token = os.environ.get("TODO_TOKEN")\n'
        '    return ("[%s] " % token + text) if token else text\n')
    assets = app.root / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "logo.txt").write_text("logo", encoding="utf-8")

    print("1) 生成独立 flet 工程（android）")
    result = export_project(app, "android")
    check("导出成功", result["ok"], str(result)[:240])
    out = Path(result["out_dir"])
    for rel in ("main.py", "flet_renderer.py", "capabilities.py",
                "requirements.txt", "README.md"):
        check("文件齐：%s" % rel, (out / rel).is_file())
    check("资产随包", (out / "assets" / "logo.txt").is_file())

    reqs = (out / "requirements.txt").read_text(encoding="utf-8")
    check("requirements：语言包钉版本", "openpuppet-language==2.1" in reqs, reqs)
    check("requirements：REQUIRES 进包（setuptools）", "setuptools" in reqs, reqs)
    check("requirements：flet 在列", "flet" in reqs)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    check("manifest：凭据静态扫描（TODO_TOKEN）",
          "TODO_TOKEN" in manifest.get("credentials", []), str(manifest))
    check("manifest：requires 与 requirements 一致",
          manifest.get("requires") == ["setuptools"], str(manifest))
    main_src = (out / "main.py").read_text(encoding="utf-8")
    check("触摸宿主自述 pointer=touch（规范 05 §10）",
          'pointer="touch"' in main_src or 'pointer=\u0027touch\u0027' in main_src)
    check("写者 = 无 写进了头部", "写者 = 无" in main_src)
    check("README 有诚实边界（快照内嵌 / manifest 边界）",
          "生成时快照" in (out / "README.md").read_text(encoding="utf-8")
          and "动态拼出" in (out / "README.md").read_text(encoding="utf-8"))
    check("notes 提到凭据注入",
          any("manifest" in note for note in result["notes"]), str(result["notes"]))

    print("\n2) 生成的工程能独立装载 + 交互通路成立（子进程，纯 headless）")
    probe = (
        "import json, sys\n"
        "sys.path.insert(0, '.')\n"
        "import main\n"
        "engine, diags = main.create_engine()\n"
        "errs = [d.to_dict() for d in diags if d.level == 'error']\n"
        "assert not errs, errs\n"
        "prog = '\\n'.join(engine.program_lines())\n"
        "assert '#inp' in prog, prog\n"
        "d1 = engine.fire('inp', 'submit', None, '买酱油')\n"
        "assert not [x for x in d1 if x.level == 'error'], d1\n"
        "state = json.dumps(engine.render_state(), ensure_ascii=False, default=str)\n"
        "assert '买酱油' in state, state[:300]\n"
        "print('OK')\n")
    proc = subprocess.run([sys.executable, "-c", probe], cwd=str(out),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    check("独立装载零错误", proc.returncode == 0 and "OK" in proc.stdout,
          (proc.stdout + proc.stderr)[-400:])
    check("交互通路成立（submit 载荷 → append → 数据变了，probe 的 OK 即证据）",
          proc.returncode == 0 and "OK" in proc.stdout, (proc.stdout + proc.stderr)[-400:])

    print("\n3) REQUIRES 缺包：生成期就拒（构建期解析，运行期才炸不可接受）")
    app3 = create_app(work, "needs", "缺包应用")
    app3.write_source(list(PROGRAM))
    app3.write_capabilities(
        '"""能力。"""\nfrom puppet import capability\n\n'
        'REQUIRES: list[str] = ["no-such-dist-xyz"]\n\n\n'
        '@capability(returns="str")\ndef shout(text: str) -> str:\n'
        '    """原样返回。"""\n    return text\n')
    result = export_project(AppDir(app3.root), "android")
    check("缺包导出被拒且点名缺的包", not result["ok"]
          and any("no-such-dist-xyz" in e for e in (result["errors"] or [])),
          str(result)[:240])

    print("\n4) 不合法程序 / 未知 target / flet CLI 缺失：全部如实拒绝")
    app2 = create_app(work, "broken", "坏程序")
    app2.write_source(list(PROGRAM) + ["this is not puppet"])
    result = export_project(AppDir(app2.root), "android")
    check("不合法程序导出被拒（干跑门）", not result["ok"]
          and any("静态校验" in e for e in result["errors"]), str(result)[:240])

    result = export_project(app, "desktop")
    check("desktop target 被拒（那是 puppethub run）", not result["ok"]
          and "desktop" in (result["errors"] or [""])[0], str(result)[:200])

    if shutil.which("flet") is None:
        outcome = run_flet_build(out, "android")
        check("flet CLI 缺失时如实上报（不假装构建成功）",
              not outcome["ok"] and "flet CLI" in (outcome.get("error") or ""),
              str(outcome)[:200])
    else:
        print("  [skip] 本机装了 flet CLI，跳过'缺失上报'断言")

    shutil.rmtree(work, ignore_errors=True)
    print("\n%s（%d 项失败）" % ("全部通过" if not failures else "有失败", len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
