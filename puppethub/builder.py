"""设备打包（V3）：`puppethub build <app> --target android|web`。

产物 = **纯 app 实例的独立 flet 工程**（无驾驶舱、无 LLM、**写者 = 无**）——
`flet build apk` 把这份工程做成安装包。本模块负责**工程生成**（可本机验证）；
最终 `flet build` 需要 Android SDK / 构建环境，只在 `--run-flet-build` 时调用，
失败如实上报。

移动端的定位（`design-v3-mobile.md`）：搭建器留在桌面，上手机的是**搭建出来的
app**。所以生成的 main.py 只做三件事：装载真源、启动渲染器、投递交互。

三个诚实的抉择：

1. **真源以生成时快照内嵌**。手机上没有写者——改程序回桌面改 `app.puppet`
   重新 build。内嵌快照让"程序在这里是只读资产"成为结构事实，而不是靠约定。
2. **渲染器整体 vendor**（`flet_renderer.py`，与 puppethub 的 `render.py` 同源
   快照）。puppethub 尚未上 PyPI，生成工程不能依赖它；vendor 的代价是
   "升级 puppethub 后需重新 build"，换来的是生成工程自洽、可离线构建。
3. **触摸宿主自述**：生成的应用是触摸宿主——`RENDERING["pointer"] = "touch"`
   （规范 05 §10，T1–T4 生效：hover 永不激活等）。这不是装饰，是规范义务。
4. **凭据与依赖在生成期说清**（`manifest.json` + 构建期解析）：能力需要的
   环境变量静态扫描进 manifest（移动容器按它注入）；`REQUIRES` 的 pip 包
   当场核对（`importlib.metadata`），缺包生成期就停下——运行期才炸不可接受。
"""

from __future__ import annotations

import datetime as _dt
import importlib.metadata as _im
import json
import os
import re
import shutil
from pathlib import Path

from puppet import SPEC_VERSION
from puppet.ir import apply_stmt, new_program, validate
from puppet.lang import parse_program

from .appdir import AppDir
from .fusion import _requires

TARGETS = ("android", "web")

_MAIN_TEMPLATE = '''"""{title} —— 由 `puppethub build --target {target}` 生成（{stamp}）。

纯 app 实例：**写者 = 无**（手机上不改程序；要改回桌面打开同一 app 目录继续对话，
改完重新 build）。触摸宿主：本应用自述 `pointer=touch`（规范 05 §10——T1–T4 生效）。

依赖见 requirements.txt；构建：`flet build apk`（或 web）。
"""
import json
import os

import flet as ft
from puppet import Engine

from flet_renderer import FletRenderer, RENDERING

HERE = os.path.dirname(os.path.abspath(__file__))

# 真源打印件（`puppethub build` 生成时快照）。改程序请回桌面改 app.puppet 后重新 build。
_PROGRAM = json.loads(r"""{program_json}""")

# 触摸宿主自述（规范 05 §10）：pointer=touch → T1–T4 生效（hover 永不激活等）。
RENDERING = dict(RENDERING, pointer="touch")


def create_engine():
    """装载真源与能力。返回 (engine, diags)——装载问题**可见**，不静默。"""
    workdir = os.path.join(HERE, ".puppet", "state")
    caps = os.path.join(HERE, "capabilities.py")
    assets = os.path.join(HERE, "assets")
    engine = Engine(workdir=workdir, rendering=RENDERING)
    diags = engine.load(
        _PROGRAM,
        capability_modules=[caps] if os.path.isfile(caps) else [],
        assets_dir=assets if os.path.isdir(assets) else None)
    return engine, diags


def main(page: ft.Page):
    page.title = {title_json}
    page.padding = 0
    engine, load_diags = create_engine()
    banner = ft.Container(visible=False, bgcolor="#fff7ed", padding=6,
                          content=ft.Text("", size=11, color="#9a3412"))
    errors = [d for d in load_diags if d.level == "error"]
    if errors:
        banner.visible = True
        banner.content.value = "装载问题：" + "；".join(
            "%s %s" % (d.code, d.message) for d in errors)

    def repaint():
        # 渲染器上报的降级/异常一律可见：进横幅，绝不静默丢弃。
        notes = renderer.apply(engine.render_state())
        if notes:
            banner.visible = True
            banner.content.value = "；".join("%s %s" % (code, msg)
                                             for code, msg in notes[:3])
        page.update()
        renderer.restore_focus()

    def on_event(node_id, event, row, value):
        for d in engine.fire(node_id, event, row, value):
            if d.level == "error":
                banner.visible = True
                banner.content.value = "%s %s" % (d.code, d.message)
        repaint()

    renderer = FletRenderer(page, engine, on_event=on_event,
                            assets_dir=os.path.join(HERE, "assets"))
    page.add(ft.Column(expand=True, spacing=0,
                       controls=[banner,
                                 ft.Container(content=renderer.host, expand=True)]))
    repaint()


if __name__ == "__main__":
    ft.app(target=main)
'''

_README_TEMPLATE = """# {title}（{target} 构建）

由 `puppethub build --target {target}` 生成（{stamp}）。**纯 app 实例：写者 = 无**。

## 构建

```bash
pip install -r requirements.txt
flet build apk        # android；web 用 `flet build web`
```

`flet build` 需要 Android SDK / JDK（见 flet 文档）。缺包会在**构建期**报出来——
那比运行期才炸好。

## 诚实边界

- `main.py` 内嵌的是**生成时快照**的真源打印件：手机上没有写者。要改程序，
  回桌面打开同一 app 目录继续对话，改完重新 `puppethub build`。
- `requirements.txt` 里除 flet / openpuppet-language 外的包来自 `capabilities.py`
  的 `REQUIRES` 声明。**移动构建要求这些包可交叉编译（纯 Python 为佳）**——
  带二进制扩展的包可能构建失败，构建期就会看见。
- `flet_renderer.py` 是 puppethub 渲染器的生成时快照（vendor）：升级 puppethub
  后需重新 build 才会带上新渲染器。
- `manifest.json` 列出本应用需要的**凭据环境变量**（对 `capabilities.py` 的
  静态扫描）与第三方依赖，运行容器按 manifest 注入环境变量。**动态拼出的
  变量名扫描不到**——能力里若有 `os.environ[拼出来的名字]`，请改成字面量，
  否则装机后只会读到空。
- 触摸宿主语义（hover 永不激活等）见规范 05 §10；本应用自述 `pointer=touch`。
"""


_ENV_RE = re.compile(
    r"""os\.environ\[["']([A-Za-z_][A-Za-z0-9_]*)["']\]"""
    r"""|os\.environ\.get\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']"""
    r"""|os\.getenv\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']""")


def _credentials(capabilities_text: str) -> list:
    """静态扫描 capabilities.py 里直接读环境变量的位置——凭据 manifest 的来源。

    诚实边界：**静态**扫描。动态拼出来的变量名（`os.environ[name]`）扫不到——
    这条边界写进生成工程的 README，而不是假装清单完备。
    """
    found = []
    for m in _ENV_RE.finditer(capabilities_text):
        name = next(g for g in m.groups() if g)
        if name not in found:
            found.append(name)
    return found


def _missing_dists(names: list) -> list:
    """`importlib.metadata` 核对 pip 分发名——`REQUIRES` 的语义就是 pip 名。

    缺包在**生成期**报出来（design-v3-mobile 第 3 节："缺包 = 构建失败可见，
    运行期才炸不可接受"），而不是等到移动构建 / 装机才炸。
    """
    missing = []
    for name in names:
        try:
            _im.version(name)
        except _im.PackageNotFoundError:
            missing.append(name)
    return missing


def export_project(app: AppDir, target: str, out: Path | None = None) -> dict:
    """把 app 导出为独立 flet 工程（可交 `flet build`）。

    与融合同一条纪律：**先干跑**（parse → apply → validate），程序不合法
    就停下——导出一个装不出来的 app 就是假成功。
    """
    if target not in TARGETS:
        return {"ok": False,
                "errors": ["target %r 不认识（可用：%s）" % (target, "、".join(TARGETS))]}
    program_ir = new_program()
    diags: list = []
    stmts, parse_diags = parse_program(app.read_source())
    diags += parse_diags
    for stmt in stmts:
        apply_stmt(program_ir, stmt, diags)
    diags += validate(program_ir)
    hard = [d for d in diags if d.level == "error"]
    if hard:
        return {"ok": False,
                "errors": ["真源未通过静态校验："] + ["%s %s" % (d.code, d.message)
                                                    for d in hard]}

    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    out_dir = (Path(out) if out else app.root / "build" / target).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    program_json = json.dumps(app.read_source(), ensure_ascii=False)
    title_json = json.dumps(app.name)
    main_py = (_MAIN_TEMPLATE
               .replace("{program_json}", program_json)
               .replace("{title_json}", title_json)
               .replace("{title}", app.name)
               .replace("{target}", target)
               .replace("{stamp}", stamp))

    # 渲染器 vendor：与生成时同版快照（puppethub 未上 PyPI，生成工程不能依赖它）
    import puppethub.render as _render
    renderer_src = Path(_render.__file__).read_text(encoding="utf-8")
    renderer_src = ("# vendor 快照：puppethub/render.py（生成于 %s）。升级 puppethub"
                    "后请重新 build。\n" % stamp) + renderer_src

    capabilities_src = app.read_capabilities()
    requires = _requires(capabilities_src)
    extra = sorted(set(requires) - {"flet", "openpuppet-language"})
    req_lines = ["flet", "openpuppet-language==%s" % SPEC_VERSION] + extra

    # 构建期解析（design-v3-mobile 第 3 节）：缺包在这里停下并说清，
    # 运行期才炸不可接受。flet / 语言包必装（本模块 import 了它们），
    # 真正会被抓到的是 REQUIRES 里的第三方包。
    missing = _missing_dists(["flet", "openpuppet-language"] + extra)
    if missing:
        return {"ok": False,
                "errors": ["REQUIRES 的包未安装（构建期解析失败）：%s"
                           "——先 pip install 后重试；把缺口留到移动构建或装机"
                           "才炸是不可接受的" % "、".join(missing)]}

    credentials = _credentials(capabilities_src)
    notes = []
    if extra:
        notes.append("REQUIRES 声明 %s 已进 requirements.txt——移动构建要求这些包"
                     "可交叉编译（纯 Python 为佳），带二进制扩展的包可能构建失败"
                     % "、".join(extra))
    if credentials:
        notes.append("凭据变量 %s 已进 manifest.json（capabilities.py 静态扫描）"
                     "——由移动容器注入环境变量；动态拼出的变量名扫描不到，见 README"
                     % "、".join(credentials))
    assets_src = Path(app.assets_dir)
    if not (assets_src.is_dir() and any(assets_src.iterdir())):
        notes.append("assets/ 为空：引用了 src 的节点会按规范可见降级")

    files: list[str] = []

    def _write(rel: str, text: str) -> None:
        (out_dir / rel).write_text(text, encoding="utf-8", newline="\n")
        files.append(rel)

    _write("main.py", main_py)
    _write("flet_renderer.py", renderer_src)
    _write("capabilities.py", capabilities_src)
    _write("requirements.txt", "\n".join(req_lines) + "\n")
    _write("manifest.json", json.dumps(
        {"target": target, "stamp": stamp, "requires": extra,
         "credentials": credentials}, ensure_ascii=False, indent=2) + "\n")
    _write("README.md", _README_TEMPLATE
           .replace("{title}", app.name).replace("{target}", target)
           .replace("{stamp}", stamp))
    if assets_src.is_dir():
        for item in assets_src.rglob("*"):
            if item.is_file():
                rel = item.relative_to(assets_src)
                dest = out_dir / "assets" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, dest)
                files.append("assets/" + str(rel).replace(os.sep, "/"))
    return {"ok": True, "out_dir": str(out_dir), "files": files,
            "requires": extra, "credentials": credentials, "notes": notes}


def run_flet_build(project_dir: Path, target: str) -> dict:
    """调用 `flet build`（需要 flet CLI 与 Android SDK / 构建环境）。

    没有验证环境不等于不能尝试：跑了就把**完整输出**交还，失败如实上报——
    假装成功才是禁区。
    """
    import subprocess
    flet_exe = shutil.which("flet")
    if flet_exe is None:
        return {"ok": False,
                "error": "flet CLI 未安装（pip install flet 后重试）；"
                         "工程已生成，也可手动 `flet build %s`" % target}
    verb = "apk" if target == "android" else target
    proc = subprocess.run([flet_exe, "build", verb],
                          cwd=str(project_dir), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "output": (proc.stdout + proc.stderr)[-4000:],
            "error": None if proc.returncode == 0
            else "flet build 失败（需要 Android SDK / 构建环境，见输出）"}
