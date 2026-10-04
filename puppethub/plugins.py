"""插件体系：统一 `PluginAPI` + 三个槽位 + 目录扫描发现。

三条不可动摇的边界：

1. **插件只产出值**。`PluginAPI` 不提供任何引擎句柄——没有任何口子能"往程序里写"。
   于是"仅 LLM 能写"从约定升级为**机制保证**。
2. **策略在宿主、机制在插件**。storage 只提供读写（`write` 有**原子性义务**）；
   JSON 序列化 / jsonl 组装 / 快照淘汰 / `reset` 分界 / `DESIGN.md` 双写者串行化
   全在宿主 → 那些已定的不变量对**任何** storage 插件都成立。
3. **失败必须可见**。加载失败、槽位名不认识、插件重名、路径越界，一律产生诊断；
   单个插件坏掉**不阻止**其余插件与宿主启动（隔离但不静默）。

**渲染器不是插件**（绑定 flet，是产品的一部分）；**能力不是插件**（方向相反：能力是 app
给 LLM 提供工具，插件是宿主向外扩展 puppethub）；**命令流改写钩子不存在**。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

from puppet import Diagnostic, ERROR, INFO, WARNING

SLOTS = ("llm_provider", "storage", "prompt")

# 单选槽位的内置默认（零配置能跑）；显式指定不存在的名字 → 可见报错并列出可用值。
DEFAULT_SLOT = {"llm_provider": "openai-compat", "storage": "file", "prompt": "default"}

# 用户插件目录：**放文件即生效**（不必 pip install）。
USER_PLUGIN_DIR = os.path.join(os.path.expanduser("~"), ".puppethub", "plugins")
PLUGIN_DIR_ENV = "PUPPETHUB_PLUGINS"


class PluginError(RuntimeError):
    """插件侧的可见失败。刻意是异常：不静默降级。"""


class StorageDenied(PluginError):
    """越界路径。要说清理由，而不是只说"不允许"。"""


class StorageFailed(PluginError):
    """写入失败。宿主据此**标脏并持续报警**，不吞不回退。"""


class FileConflict(PluginError):
    """撞上"人放的资产 LLM 不得覆盖"或已有文件。"""


# ------------------------------------------------------------------ API

class PluginAPI:
    """插件能拿到的一切。

    刻意只有三样：**名字、自己的配置、环境变量里的凭据**。
    没有引擎、没有程序路径、没有事件总线——最坏只能让 LLM 表现变差，而这始终可见。
    """

    def __init__(self, name: str, config: Optional[dict] = None,
                 log: Optional[Callable[[str, str, str], None]] = None):
        self.name = name
        self.config = dict(config or {})
        self._log = log

    def option(self, key: str, default=None):
        return self.config.get(key, default)

    def secret(self, env_name: str) -> Optional[str]:
        """凭据走**环境变量**，toml 只存变量名——toml 会被 git 跟踪、进 dist、被融合拷贝。"""
        return os.environ.get(env_name) if env_name else None

    def log(self, level: str, message: str) -> None:
        if self._log is not None:
            self._log(self.name, level, message)


# ------------------------------------------------------------------ 加载与发现

@dataclass
class LoadedPlugin:
    name: str
    source: str                      # 来源文件；内置显示为 "内置"
    provides: List[str] = field(default_factory=list)
    factories: Dict[str, Callable] = field(default_factory=dict)
    builtin: bool = False


def plugin_dirs() -> List[str]:
    """待扫描的插件目录：环境变量可覆盖（测试与 `verify` 的干净环境用它）。"""
    override = os.environ.get(PLUGIN_DIR_ENV)
    if override:
        return [part for part in override.split(os.pathsep) if part]
    return [USER_PLUGIN_DIR]


def _import_file(path: str):
    stem = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location("puppethub_plugin_" + stem, path)
    if spec is None or spec.loader is None:
        raise PluginError("无法加载插件文件：%s" % path)
    module = importlib.util.module_from_spec(spec)
    # 必须先注册进 sys.modules 再 exec：不注册，模块内的 dataclass / 自引用 import
    # 会拿到一份**副本**模块——测试夹具也因此拿不到模块级状态。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_plugin_file(path: str) -> tuple[Optional[LoadedPlugin], List[Diagnostic]]:
    """从一个 `.py` 文件加载插件。失败**必须可见**，且不阻止其余插件。"""
    diags: List[Diagnostic] = []
    name = os.path.splitext(os.path.basename(path))[0]
    try:
        module = _import_file(path)
    except Exception as ex:  # noqa: BLE001 - 一律转为可见失败
        diags.append(Diagnostic("PLUGIN_IMPORT", ERROR,
                                "插件导入失败（%s）：%s: %s" % (name, type(ex).__name__, ex)))
        return None, diags

    declared = getattr(module, "PROVIDES", None)
    if not isinstance(declared, (list, tuple)) or not declared:
        diags.append(Diagnostic("PLUGIN_PROVIDES", ERROR,
                                "插件 %s 没有声明 PROVIDES（形如 PROVIDES = [\"prompt\"]），不予加载"
                                % name))
        return None, diags

    plugin_name = str(getattr(module, "NAME", "") or name)
    factories: Dict[str, Callable] = {}
    provides: List[str] = []
    for slot in declared:
        if slot not in SLOTS:
            diags.append(Diagnostic("PLUGIN_SLOT", ERROR,
                                    "插件 %s 声明了未知槽位 %r（可用：%s）"
                                    % (plugin_name, slot, "、".join(SLOTS))))
            continue
        factory = getattr(module, "create_" + slot, None)
        if not callable(factory):
            diags.append(Diagnostic("PLUGIN_FACTORY", ERROR,
                                    "插件 %s 声明提供 %s，但没有 create_%s(api) 工厂"
                                    % (plugin_name, slot, slot)))
            continue
        provides.append(slot)
        factories[slot] = factory
    if not provides:
        return None, diags
    return LoadedPlugin(name=plugin_name, source=path, provides=provides,
                        factories=factories, builtin=False), diags


class PluginRegistry:
    """已发现的插件表。**插件名全局唯一**，重名可见报错并列出冲突文件。"""

    def __init__(self):
        self.plugins: Dict[str, LoadedPlugin] = {}
        self.diagnostics: List[Diagnostic] = []

    def add(self, plugin: LoadedPlugin) -> None:
        existing = self.plugins.get(plugin.name)
        if existing is not None:
            self.diagnostics.append(Diagnostic(
                "PLUGIN_CONFLICT", ERROR,
                "插件名 %s 冲突：%s 与 %s。插件名即身份，必须全局唯一"
                % (plugin.name, existing.source, plugin.source)))
            return
        self.plugins[plugin.name] = plugin
        self.diagnostics.append(Diagnostic(
            "PLUGIN_LOADED", INFO, "已加载插件 %s（%s）→ 提供 %s"
            % (plugin.name, plugin.source, "、".join(plugin.provides))))

    def load_builtins(self) -> None:
        from .builtin_plugins import default_prompt, file_storage, openai_compat
        for module in (file_storage, openai_compat, default_prompt):
            plugin = LoadedPlugin(
                name=module.NAME, source="内置",
                provides=list(module.PROVIDES),
                factories={slot: getattr(module, "create_" + slot)
                           for slot in module.PROVIDES},
                builtin=True)
            self.add(plugin)

    def discover(self, dirs: Optional[Iterable[str]] = None) -> None:
        for directory in (dirs if dirs is not None else plugin_dirs()):
            if not os.path.isdir(directory):
                continue
            for filename in sorted(os.listdir(directory)):
                if not filename.endswith(".py") or filename.startswith("_"):
                    continue
                plugin, diags = load_plugin_file(os.path.join(directory, filename))
                self.diagnostics.extend(diags)
                if plugin is not None:
                    self.add(plugin)

    def available(self, slot: str) -> List[str]:
        return sorted(name for name, plugin in self.plugins.items()
                      if slot in plugin.provides)


def build_registry(include_user: bool = True) -> PluginRegistry:
    """内置插件 + 用户插件。

    `include_user=False` 是 `verify` 的**干净环境**：只加载内置插件，
    保证自证可复现——用户插件的 bug 不该让 conformance 变红。
    """
    registry = PluginRegistry()
    registry.load_builtins()
    if include_user:
        registry.discover()
    return registry


def select_slots(registry: PluginRegistry, config: dict,
                 log: Optional[Callable[[str, str, str], None]] = None):
    """按配置解析三个槽位，返回 `(instances, names, diagnostics)`。

    单选槽位指定了不存在的名字 → **可见报错并列出可用值**，然后退回内置默认
    （报错而非静默：用户看得见自己写错了什么）。
    """
    diags: List[Diagnostic] = []
    instances: Dict[str, object] = {}
    names: Dict[str, object] = {}

    single = {}
    for slot in ("llm_provider", "storage"):
        requested = config.get(slot)
        if requested is None:
            requested = DEFAULT_SLOT[slot]
        plugin = registry.plugins.get(str(requested))
        if plugin is None or slot not in plugin.provides:
            diags.append(Diagnostic(
                "PLUGIN_SLOT_MISSING", ERROR,
                "%s 指定了 %r，但它不存在或未提供该槽位；可用：%s。已退回内置默认 %s"
                % (slot, requested, "、".join(registry.available(slot)) or "（无）",
                   DEFAULT_SLOT[slot])))
            plugin = registry.plugins.get(DEFAULT_SLOT[slot])
        if plugin is not None and slot in plugin.provides:
            single[slot] = plugin
            names[slot] = plugin.name

    requested_prompts = config.get("prompt")
    if requested_prompts is None:
        requested_prompts = [DEFAULT_SLOT["prompt"]]
    if isinstance(requested_prompts, str):
        requested_prompts = [requested_prompts]
    chain: List[LoadedPlugin] = []
    for name in requested_prompts:
        plugin = registry.plugins.get(str(name))
        if plugin is None or "prompt" not in plugin.provides:
            diags.append(Diagnostic(
                "PLUGIN_SLOT_MISSING", ERROR,
                "prompt 链里的 %r 不存在或未提供该槽位；可用：%s。已跳过它"
                % (name, "、".join(registry.available("prompt")) or "（无）")))
            continue
        chain.append(plugin)
    if not chain:
        fallback = registry.plugins.get(DEFAULT_SLOT["prompt"])
        if fallback is not None and "prompt" in fallback.provides:
            chain = [fallback]
            diags.append(Diagnostic("PLUGIN_PROMPT_FALLBACK", WARNING,
                                    "没有可用的 prompt 插件，已回退内置 default"))

    for slot, plugin in single.items():
        instances[slot] = _instantiate(plugin, slot, config, log, diags)

    prompts = []
    for plugin in chain:
        instance = _instantiate(plugin, "prompt", config, log, diags)
        if instance is not None:
            prompts.append((plugin.name, instance))
    instances["prompt"] = prompts
    names["prompt"] = [name for name, _ in prompts]
    return instances, names, diags


def _instantiate(plugin: LoadedPlugin, slot: str, config: dict,
                 log, diags: List[Diagnostic]):
    options = (config.get("plugins") or {}).get(plugin.name) or {}
    api = PluginAPI(plugin.name, options, log)
    try:
        return plugin.factories[slot](api)
    except Exception as ex:  # noqa: BLE001 - 单插件失败被隔离，但必须可见
        diags.append(Diagnostic("PLUGIN_INIT", ERROR,
                                "插件 %s 的 %s 初始化失败：%s: %s"
                                % (plugin.name, slot, type(ex).__name__, ex)))
        return None


# ------------------------------------------------------------------ 存储：宿主策略

ALLOWED_ROOTS = (".puppet", ".puppethub")


class HostStorage:
    """storage 槽位的**宿主侧策略**：路径白名单 + 序列化 + 脏标记。

    - 可写范围只有 `.puppet/` 与 `.puppethub/`；写 `app.puppet` / `capabilities.py`
      → 可见报错并说明理由（那是程序资产，只能走命令批或受限文件写入）。
    - 写入失败**不吞不回退**：标脏并持续报警，由宿主拦住依赖该写入的后续动作。
      这是唯一涉及**数据正确性**的槽位——内存已变、盘上未变，而用户以为存上了。
    """

    def __init__(self, plugin: object, app_root: str,
                 log: Optional[Callable[[str, str, str], None]] = None):
        self.plugin = plugin
        self.app_root = os.path.abspath(app_root)
        self.dirty: List[str] = []
        self.failures: List[dict] = []      # 未落盘的写入（供"重试一次"）
        self._log = log

    # -------------------------------------------------- 路径

    def resolve(self, rel: str, *, for_write: bool = True) -> str:
        if os.path.isabs(rel) or rel.startswith("~"):
            raise StorageDenied("只接受 app 目录内的相对路径：%r" % rel)
        target = os.path.abspath(os.path.join(self.app_root, rel))
        for root in ALLOWED_ROOTS:
            base = os.path.join(self.app_root, root)
            if target == base or target.startswith(base + os.sep):
                break
        else:
            raise StorageDenied(
                "路径越界：%s（可写范围只有 %s）。程序资产（app.puppet / capabilities.py）"
                "只能走命令批或受限文件写入，插件拿不到那个口子"
                % (rel, " 与 ".join(ALLOWED_ROOTS)))
        if for_write:
            os.makedirs(os.path.dirname(target), exist_ok=True)
        return target

    # -------------------------------------------------- 机制（转发给插件）

    def _call(self, method: str, *args, **kwargs):
        func = getattr(self.plugin, method, None)
        if not callable(func):
            raise PluginError("storage 插件缺少方法 %s" % method)
        try:
            return func(*args, **kwargs)
        except StorageDenied:
            raise
        except Exception as ex:  # noqa: BLE001 - 写失败必须标脏并可见
            path = args[0] if args else ""
            self.failures.append({"method": method, "args": list(args),
                                  "message": "%s: %s" % (type(ex).__name__, ex)})
            self._fail("%s 失败（%s）：%s: %s" % (method, path, type(ex).__name__, ex))
            raise StorageFailed(str(ex))

    def _fail(self, message: str) -> None:
        if message not in self.dirty:
            self.dirty.append(message)
        if self._log is not None:
            self._log("storage", "error", message)

    def retry(self) -> List[str]:
        """重试所有未落盘的写入。返回**仍然失败**的说明。

        "先自动重试一次；仍失败则拦窗确认"——重试要真重试，所以失败项带着参数留着，
        而不是只留一句"写入失败了"。
        """
        pending, self.failures = self.failures, []
        self.dirty = []
        for item in pending:
            try:
                func = getattr(self.plugin, item["method"])
                func(*item["args"])
            except Exception:  # noqa: BLE001 - `_call` 已重新登记并标脏
                pass
        return list(self.dirty)

    def read_text(self, rel: str, default: str = "") -> str:
        try:
            path = self.resolve(rel, for_write=False)
        except StorageDenied:
            raise
        if not os.path.isfile(path):
            return default
        return self._call("read", path)

    def write_text(self, rel: str, text: str) -> str:
        path = self.resolve(rel)
        self._call("write", path, text)
        return path

    def append_text(self, rel: str, text: str) -> str:
        path = self.resolve(rel)
        self._call("append", path, text)
        return path

    def exists(self, rel: str) -> bool:
        try:
            path = self.resolve(rel, for_write=False)
        except StorageDenied:
            return False
        return bool(self._call("exists", path))

    def remove(self, rel: str) -> None:
        path = self.resolve(rel)
        self._call("remove", path)

    def list_dir(self, rel: str) -> List[str]:
        path = self.resolve(rel, for_write=False)
        if not os.path.isdir(path):
            return []
        return list(self._call("list", path))

    # -------------------------------------------------- 序列化（策略在宿主）

    def read_json(self, rel: str, default=None):
        text = self.read_text(rel, "")
        if not text.strip():
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError as ex:
            self._fail("状态文件 %s 不是合法 JSON（%s），已按缺失处理但**不覆盖它**"
                       % (rel, ex))
            return default

    def write_json(self, rel: str, payload) -> None:
        self.write_text(rel, json.dumps(payload, ensure_ascii=False, indent=2))

    def read_jsonl(self, rel: str) -> List[dict]:
        out = []
        for line in self.read_text(rel, "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                self._fail("历史文件 %s 有不可解析的行，已跳过该行（其余照读）" % rel)
        return out

    def append_jsonl(self, rel: str, payload) -> None:
        self.append_text(rel, json.dumps(payload, ensure_ascii=False) + "\n")
