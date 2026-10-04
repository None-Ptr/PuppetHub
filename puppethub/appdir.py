"""app 目录契约：布局、真源读写、快照、重置。

```
<app>/
├── app.puppet            真源 = **程序 IR 的打印件**（程序变更只走命令批，批末整份重排写回）
├── capabilities.py       能力实现（`@puppet.capability`）
├── assets/               静态资源——**人可以直接放**（它不算改程序）
├── DESIGN.md             意图档：头部契约（稳定）+ append-only 决策流水
├── .gitignore            忽略 `.puppet/`
├── .puppethub/chat.jsonl 过程记忆：搭建对话历史
└── .puppet/
    ├── state/            引擎状态（可整体删除重置）
    ├── memory/           运行期自主记忆（重置**保留**，清空需显式 wipe）
    └── snapshots/        真源快照（重置**保留**）
```

两本账物理分离：程序（`app.puppet`）与状态（`.puppet/state/`）不混存，
引擎只写状态、绝不改程序。

**快照的单位是"程序资产"** = 真源 + `capabilities.py`（+ 将来的其他程序文件），
不是只有 `.puppet` 一个文件：`capabilities.py` 同样不可再生、同样只有一份，而且是
"仅 LLM 能写"之下人最无力独立修复的东西。`assets/` **不在其中**——回滚去动它就会
删掉用户后加的照片。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

APP_FILE = "app.puppet"
CAP_FILE = "capabilities.py"
DESIGN_FILE = "DESIGN.md"
CONFIG_FILE = "puppethub.toml"
GITIGNORE_FILE = ".gitignore"
PUPPET_DIR = ".puppet"
HUB_DIR = ".puppethub"
CHAT_FILE = "chat.jsonl"
AUTO_KEEP = 20

SKELETON_GITIGNORE = """# 引擎状态 / 记忆 / 快照：都是本机运行产物，不进版本库。
.puppet/
"""

SKELETON_CONFIG = '''# PuppetHub 的 app 配置。**零配置也能跑**（全用内置默认）；下面两块按需选一。

# 方式一（推荐）：用本机命名 profile——端点与凭据名留在
# ~/.puppethub/providers.toml（机器级），app 里只留名字，
# 所以这个文件可以安全分享/提交。
# [llm]
# profile = "deepseek"

# 方式二：直接写本 app 的 provider 选项（端点会随 app 一起被分享）
# [plugins.openai-compat]
# base_url = "https://api.openai.com/v1"
# model = "gpt-4o-mini"
# key_env = "OPENAI_API_KEY"      # 只写变量名；值走环境变量或 `puppethub keys set`
'''


SKELETON_CAPABILITIES = '''"""{title} 的能力（工具）。

能力（`@puppet.capability`）是 **app 提供给 LLM 使用的工具**：LLM 通过 `call` 调用它，
所以每个函数都必须有说明文本——那是 LLM 发现它的唯一说明书（没有说明的能力不予注册）。

两条硬性义务：
- **依赖显式声明**：本模块顶层 import 的第三方包，逐个写进 `REQUIRES`；
  声明与实际 import 不一致会被报出来（部署机上才发现缺包是踩过的坑）。
- **返回值必须可序列化**：否则由引擎转为可见失败。

本文件走「受限文件写入」，是普通 Python，**注释会保留**；
而 `app.puppet` 走引擎 IR，注释会在写回时丢失——所以能写的说明就写在这里。
"""

from puppet import capability

# 本模块顶层 import 的第三方包名。无第三方依赖时留空列表（留空即跳过核对）。
REQUIRES: list[str] = []


# 取消注释即可得到一个可用能力（骨架刻意不含业务，见设计"骨架给结构、DESIGN.md 给意图"）：
#
# @capability(returns="dict")
# def hello(name: str = "world") -> dict:
#     """打个招呼，返回 {"message": <问候语>}。"""
#     return {"message": "hello " + name}
'''

SKELETON_DESIGN = """# {title}

> 本文件是**意图的唯一载体**。真源是 `app.puppet`，它是程序 IR 的打印件——
> 注释与排版会在每次写回时被重排，故**程序里表达不了意图**。

## 头部契约

供一眼定位，也供应用融合做依赖检查。约束性字段（非目标 / 依赖 / 已知限制）
**放松需要显式确认**，加严不需要。

- **目标**：（一句话：这个 app 是做什么的）
- **非目标**：
- **提供的能力**：见 `capabilities.py`（**由代码派生，勿手写**）
- **依赖**：
- **已知限制**：

## 决策流水

append-only。关键节点由 PuppetHub 自动追加一条，格式与渲染契约一致。

| 日期 | 决策 | 理由 | 影响范围 |
|---|---|---|---|
| {today} | 由 `puppethub new` 生成骨架 | 起点：`window` + `navbar` + 内容容器，不含业务 | 全程序 |
"""


class ConfigError(RuntimeError):
    """app 配置坏了。必须可见——装作"没有配置"会让人以为默认值生效了。"""


@dataclass
class Snapshot:
    id: str
    kind: str          # auto | named | rebuild
    reason: str
    origin: str
    time: str


def skeleton_source(title: str) -> list[str]:
    """`new` 的骨架：**只给布局惯例 + 能跑，不含任何业务**。

    给完整范例会束缚 LLM——它倾向于在示例上小修小补，而不是把用户要的东西做对；
    而用户的需求是"做个 X"，不是"改这个 Y"。
    """
    return [
        "add #root window #win title=%s w=900 h=640" % _quote(title),
        "add #win navbar #bar title=%s" % _quote(title),
        "add #win col #content pad=16 gap=12 flex=1",
    ]


def _quote(text: str) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


class AppDir:
    """一个 app 目录的读写入口。只碰文件，不懂语言语义（那是 Session 的事）。

    `storage`（宿主在装配好 storage 槽位后注入）决定**派生数据**（快照 / 引擎状态文件 /
    记忆区）从哪里落盘：注入后全部走 storage 槽位（白名单、原子写、写失败标脏），
    没注入才直接落本机盘。**程序资产不在此列**——`app.puppet` / `capabilities.py`
    是白名单刻意 excludes 的东西，走命令批与受限文件写入，这是另一条有守卫的通道。
    """

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root).resolve()
        self.storage = None                       # type: ignore[assignment]

    # ------------------------------------------------------------ 路径

    @property
    def name(self) -> str:
        return self.root.name

    @property
    def source_path(self) -> Path:
        return self.root / APP_FILE

    @property
    def capabilities_path(self) -> Path:
        return self.root / CAP_FILE

    @property
    def design_path(self) -> Path:
        return self.root / DESIGN_FILE

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"

    @property
    def state_dir(self) -> Path:
        return self.root / PUPPET_DIR / "state"

    @property
    def memory_dir(self) -> Path:
        return self.root / PUPPET_DIR / "memory"

    @property
    def snapshots_dir(self) -> Path:
        return self.root / PUPPET_DIR / "snapshots"

    @property
    def chat_path(self) -> Path:
        return self.root / HUB_DIR / CHAT_FILE

    def exists(self) -> bool:
        return self.source_path.is_file()

    # ------------------------------------------------------------ 真源 / 程序资产

    def read_source(self) -> list[str]:
        if not self.source_path.is_file():
            return []
        with open(self.source_path, "r", encoding="utf-8") as fh:
            return fh.read().splitlines()

    def write_source(self, lines: list[str], origin: str = "system") -> None:
        """**原子**写回真源：整份重排打印的结果一次落盘，不出现半份文件。"""
        self._atomic_write(self.source_path, "\n".join(lines) + "\n")

    def read_capabilities(self) -> str:
        if not self.capabilities_path.is_file():
            return ""
        with open(self.capabilities_path, "r", encoding="utf-8") as fh:
            return fh.read()

    def write_capabilities(self, text: str) -> None:
        self._atomic_write(self.capabilities_path, text)

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILE

    def read_config(self) -> dict:
        """`puppethub.toml`：槽位选择 + 插件选项。**零配置能跑**（全用内置默认）。"""
        if not self.config_path.is_file():
            return {}
        try:
            import tomllib
            with open(self.config_path, "rb") as fh:
                return tomllib.load(fh)
        except Exception as ex:  # noqa: BLE001 - 配置坏掉必须可见
            raise ConfigError("puppethub.toml 解析失败（%s）：%s: %s"
                              % (self.config_path, type(ex).__name__, ex)) from ex

    def read_design(self) -> str:
        if not self.design_path.is_file():
            return ""
        with open(self.design_path, "r", encoding="utf-8") as fh:
            return fh.read()

    def append_decision(self, decision: str, reason: str, scope: str,
                        origin: str = "system") -> None:
        """`DESIGN.md` 的决策流水是 **append-only**：只增不减，因此不需要串行化的双写者。"""
        row = "| %s | %s | %s | %s |\n" % (_dt.date.today().isoformat(), decision,
                                          reason, scope)
        if self.design_path.is_file():
            with open(self.design_path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(row)
        else:
            self._atomic_write(self.design_path, "# %s\n\n" % self.name + row)

    def program_assets(self) -> list[tuple[Path, Path]]:
        """快照 / 回滚的单位：真源 + `capabilities.py`（相对路径 → 绝对路径）。"""
        out = [(Path(APP_FILE), self.source_path)]
        if self.capabilities_path.is_file():
            out.append((Path(CAP_FILE), self.capabilities_path))
        return out

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)

    # ------------------------------------------------------------ 快照

    def push_snapshot(self, kind: str, reason: str, origin: str,
                      source_lines: list[str] | None = None) -> Snapshot:
        """存一份快照（程序资产全部）。

        `kind`：`auto`（写前自动，环形淘汰）/ `named`（里程碑，**不淘汰**）/
        `rebuild`（推倒重来前的兜底，**不淘汰**——兜底存在但会消失比没有兜底更糟，
        因为人**以为**有）。

        落盘走 storage 槽位（注入时）：快照是**派生数据**，绕过槽位直接写盘，
        就会出现"storage 插件只接管一半数据"的分裂事实。
        """
        sid = _stamp()
        rel = ".puppet/snapshots/" + sid
        text = ("\n".join(source_lines) + "\n") if source_lines is not None \
            else ("\n".join(self.read_source()) + "\n" if self.source_path.is_file() else "")
        meta = {"id": sid, "kind": kind, "reason": reason, "origin": origin,
                "time": _now()}
        if self.storage is not None:
            self.storage.write_text(rel + "/" + APP_FILE, text)
            if self.capabilities_path.is_file():
                self.storage.write_text(rel + "/" + CAP_FILE, self.read_capabilities())
            self.storage.write_json(rel + "/meta.json", meta)
        else:
            dest = self.snapshots_dir / sid
            dest.mkdir(parents=True, exist_ok=True)
            with open(dest / APP_FILE, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            if self.capabilities_path.is_file():
                shutil.copyfile(self.capabilities_path, dest / CAP_FILE)
            with open(dest / "meta.json", "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False, indent=2)
        if kind == "auto":
            self._prune_auto()
        return Snapshot(**meta)

    def _prune_auto(self, keep: int = AUTO_KEEP) -> None:
        autos = [s for s in self.list_snapshots() if s.kind == "auto"]
        for snap in autos[keep:]:
            if self.storage is not None:
                self.storage.remove(".puppet/snapshots/" + snap.id)
            else:
                shutil.rmtree(self.snapshots_dir / snap.id, ignore_errors=True)

    def list_snapshots(self) -> list[Snapshot]:
        out: list[Snapshot] = []
        if self.storage is not None:
            ids = self.storage.list_dir(".puppet/snapshots")
        elif not self.snapshots_dir.is_dir():
            return out
        else:
            ids = [child.name for child in self.snapshots_dir.iterdir()]
        for sid in sorted(ids, reverse=True):          # 新的在前
            if self.storage is not None:
                meta = self.storage.read_json(".puppet/snapshots/%s/meta.json" % sid,
                                              None)
            else:
                meta_path = self.snapshots_dir / sid / "meta.json"
                if not meta_path.is_file():
                    continue
                try:
                    with open(meta_path, "r", encoding="utf-8") as fh:
                        meta = json.load(fh)
                except Exception:  # noqa: BLE001 - 与下方同口径：坏元数据列成"损坏"
                    meta = None
            if not isinstance(meta, dict):
                out.append(Snapshot(id=sid, kind="broken",
                                    reason="元数据损坏或缺失", origin="?", time="?"))
                continue
            try:
                out.append(Snapshot(**meta))
            except Exception:  # noqa: BLE001 - 坏元数据不静默：列成"损坏"条目
                out.append(Snapshot(id=sid, kind="broken",
                                    reason="元数据损坏或缺失", origin="?", time="?"))
        return out

    def restore(self, snapshot_id: str, origin: str) -> Snapshot:
        """回滚。回滚**之前**先给当前状态存一份自动快照——否则回滚本身就是不可逆的。"""
        rel = ".puppet/snapshots/" + snapshot_id
        if self.storage is not None:
            if not self.storage.exists(rel + "/" + APP_FILE):
                raise FileNotFoundError("快照不存在或缺少 %s：%s" % (APP_FILE, snapshot_id))
            text = self.storage.read_text(rel + "/" + APP_FILE)
            cap_text = (self.storage.read_text(rel + "/" + CAP_FILE)
                        if self.storage.exists(rel + "/" + CAP_FILE) else None)
        else:
            src = self.snapshots_dir / snapshot_id
            if not (src / APP_FILE).is_file():
                raise FileNotFoundError("快照不存在或缺少 %s：%s" % (APP_FILE, snapshot_id))
            text = (src / APP_FILE).read_text(encoding="utf-8")
            cap_path = src / CAP_FILE
            cap_text = (cap_path.read_text(encoding="utf-8")
                        if cap_path.is_file() else None)
        self.push_snapshot("auto", "回滚前自动存档（回到 %s）" % snapshot_id, origin)
        self._atomic_write(self.source_path, text)
        if cap_text is not None:
            self._atomic_write(self.capabilities_path, cap_text)
        return Snapshot(id=snapshot_id, kind="restore", reason="已回滚", origin=origin,
                        time=_now())

    # ------------------------------------------------------------ 重置 / 记忆

    def reset_state(self) -> None:
        """重置**只清 `state/`**：记忆与快照是救场工具，被重置顺手删掉是荒谬的。"""
        if self.storage is not None:
            if self.storage.exists(".puppet/state"):
                self.storage.remove(".puppet/state")
            return
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def wipe_memory(self) -> None:
        if self.storage is not None:
            if self.storage.exists(".puppet/memory"):
                self.storage.remove(".puppet/memory")
            return
        shutil.rmtree(self.memory_dir, ignore_errors=True)

    def ensure_layout(self) -> None:
        for path in (self.assets_dir, self.state_dir, self.memory_dir,
                     self.snapshots_dir, self.chat_path.parent):
            path.mkdir(parents=True, exist_ok=True)


def create_app(parent: str | os.PathLike, name: str,
               title: str | None = None) -> AppDir:
    """`new <name>`：生成最小可用骨架。"""
    root = Path(parent).resolve() / name
    if root.exists() and any(root.iterdir()):
        raise FileExistsError("目录非空，拒绝覆盖：%s" % root)
    app = AppDir(root)
    app.ensure_layout()
    shown = title or name
    app.write_source(skeleton_source(shown), origin="system")
    # 用 replace 而不是 format/`%`：样板里含 `{}` 与 `%`，不能被当成占位符。
    app.write_capabilities(SKELETON_CAPABILITIES.replace("{title}", shown))
    AppDir._atomic_write(app.config_path, SKELETON_CONFIG)
    AppDir._atomic_write(app.design_path,
                         SKELETON_DESIGN.replace("{title}", shown)
                         .replace("{today}", _dt.date.today().isoformat()))
    AppDir._atomic_write(app.root / GITIGNORE_FILE, SKELETON_GITIGNORE)
    return app
