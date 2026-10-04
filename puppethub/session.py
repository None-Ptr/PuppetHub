"""service 层：CLI 与驾驶舱**唯一共用**的实现（防漂移）。

两套入口各写一遍语义，迟早漂移——所以 CLI 只做参数解析与输出格式化，语义都在这里。

这里也是"程序变更只走命令批、批末一次性写回"落地的地方：
命令批 → 引擎校验并应用 → 若真源文本确有变化，则**先自动快照**再整份写回。
真源是程序 IR 的打印件，绕过命令批直接编辑文件是无效的——文件是 IR 的投影，
改它不改 IR，下次写回就被覆盖。所以这条路径是**必需机制，不是风格偏好**。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from puppet import SPEC_VERSION, Engine

from . import catalog as _catalog
from . import secrets
from .appdir import AppDir, ConfigError, HUB_DIR, Snapshot
from .chat import Chat
from .compat import check_data_files, check_spec_version
from .memory import Memory
from .plugins import HostStorage, PluginError, build_registry, plugin_dirs, select_slots
from .render import RENDERING

ORIGINS = ("llm", "autonomous", "driver", "system", "user")

# 写者状态机：一个 app 实例同一时刻只有一个写者。切换必须显式、留痕、可审计。
WRITERS = ("llm", "autonomous", "none")

_LEVELS = {"info": "信息", "warning": "警告", "error": "错误"}


class _EngineStore:
    """引擎状态文件 → storage 槽位的适配器。

    引擎只要求 duck interface `read() -> dict` / `write(payload)`；**路径决策留在这里**，
    引擎保持"不懂宿主"。不接这个适配器，storage 插件（尤其沙箱 / 远端实现）只接管
    一半数据——对话历史与记忆走槽位、引擎状态却直接写本机盘，那就是
    "派生数据第二份真相"：同一份状态，两个通道，谁也不认识谁。
    """

    STATE_REL = ".puppet/state/state.json"

    def __init__(self, storage: HostStorage):
        self._storage = storage

    def read(self) -> dict:
        data = self._storage.read_json(self.STATE_REL, None)
        return data if isinstance(data, dict) else {}

    def write(self, payload) -> None:
        try:
            self._storage.write_json(self.STATE_REL, payload)
        except Exception:  # noqa: BLE001 - 与 chat.jsonl 同一策略
            # HostStorage 已标脏、已通过 _plugin_log 记为 error（STORAGE_DIRTY 可见、
            # failures 留给重试）。这里不再抛：炸掉命令批只会让人连其余诊断都看不到。
            # 内存里的状态照常成立——"内存已变、盘上未变"必须**可见**，但不必致命。
            pass


class Session:
    """一个 app 的运行期。

    `origin` 表示"谁改的"：`llm`（正常实例的唯一写者）/ `driver`（无 LLM 实例）/
    `system`（快照回滚、重置等预定义动作）/ `user`（人在界面里的交互，不是改程序）。
    """

    def __init__(self, app: AppDir, on_change: Optional[Callable[[], None]] = None):
        self.spec_version = check_spec_version()
        self.spec_dir = check_data_files()     # 分发定稿：数据文件缺失在启动时可见，而非晚炸
        self.app = app
        self.app.ensure_layout()
        self.engine = Engine(workdir=str(app.state_dir), rendering=RENDERING)
        self.renderer = None                       # 由宿主挂上（窗口 / 控制面）
        self.on_change = on_change                 # 程序或状态变化后的宿主回调
        self.on_delta = None                       # 流式回调（窗口用它逐字上屏）
        self._lock = threading.RLock()
        self._catalog: list[dict] = []
        self.log: deque[dict] = deque(maxlen=800)
        self.diag_log: deque[dict] = deque(maxlen=300)
        self.config: dict = {}
        self.registry = None
        self.slots: dict = {}
        self.slot_names: dict = {}
        self.storage: HostStorage | None = None
        self.memory: Memory | None = None
        self.chat: Chat | None = None
        self.autonomous = None                      # AutonomousRunner（装配后才有）
        self.writer = "none"                        # 写者状态机（装配后置为 llm）
        self.fusion_target: str | None = None       # 融合对象路径（grilling 共识：只存路径，其余现算）
        self._cap_stamp = 0.0                       # capabilities.py 的 mtime（热重载检查点）
        self.plugin_error = ""
        self._started = False                       # start() 的幂等闸（见 start 的注释）
        # V4 服务化与协作：`lend` 是**借出**的能力白名单（缺省空 = 默认拒绝）；
        # `bus` 是协作总线客户端（hub 没起就 None，tell 可见失败）。
        # lend 在 _build_plugins 里随 config 装载（配置在那里才读得到）。
        self.service_lend: list = []
        self.bus = None                             # BusClient（attach_hub 后才有）

    # ------------------------------------------------------------ 生命周期

    def start(self) -> list:
        """装载插件、真源与能力模块。返回装载期诊断。**幂等**。

        两个入口都会调它：`cmd_run` 先 start（要打印装载诊断），窗口打开
        （`HubWindow._main`）再 start（保证任何入口进来的会话都已就绪）——
        第二次必须是无操作，否则观察流里"已装载"出现两遍（实测）。
        显式重载请走 `reload()`，别重复 start。
        """
        if self._started:
            return []
        self._started = True
        self._build_plugins()
        modules = [str(self.app.capabilities_path)] if self.app.capabilities_path.is_file() else []
        if not modules:
            self._note("CAP_IMPORT", "警告", "没有 capabilities.py：本 app 不提供任何能力")
        diags = self.engine.load(self.app.read_source(),
                                 capability_modules=modules,
                                 assets_dir=str(self.app.assets_dir))
        self._rebuild_catalog()
        self._absorb_diags(diags, "装载")
        self._note("SESSION", "信息",
                   "已装载 %s（语言 %s）· %d 个节点 · %d 项能力"
                   % (self.app.name, self.spec_version,
                      len(self.engine.program.nodes), len(self._catalog)))
        return diags

    def reload(self) -> list:
        """显式重载：重新读真源与能力，**丢瞬态、保持久**（持久数据从状态文件恢复）。

        持 `_lock`：重载整体换掉引擎对象，绝不能与另一线程的 apply_batch 交错
        （RLock 可重入——check_capabilities 持锁调进来不会自锁）。
        """
        with self._lock:
            diags = self.engine.load(self.app.read_source(),
                                     capability_modules=([str(self.app.capabilities_path)]
                                                         if self.app.capabilities_path.is_file() else []),
                                     assets_dir=str(self.app.assets_dir))
            self._rebuild_catalog()
        self._absorb_diags(diags, "重载")
        self._note("SESSION", "信息", "已重载（瞬时状态丢弃，持久数据保留）")
        return diags

    # ------------------------------------------------------------ 插件与对话

    def _build_plugins(self) -> None:
        """发现插件、解析槽位、装配 storage 与对话回路。

        插件**改动需重启**（热重载要正确处理"流式中不能重载""storage 未落盘数据"，
        而 V1 只有个位数插件）：所以加载一次、启动时非静默列出、驾驶舱常驻显示生效链。
        """
        try:
            self.config = self.app.read_config()
        except ConfigError as ex:
            self._note("CONFIG", "错误", str(ex))
            self.plugin_error = str(ex)
            self.config = {}
        self._apply_llm_profile()
        self.registry = build_registry(include_user=False)
        # 插件目录两处：app 自带（.puppethub/plugins/，随 app 走）+ 用户全局。
        # app 级目录让"这个 app 需要的插件"和 app 一起搬——融合/拷贝时不会掉件。
        self.registry.discover([str(self.app.root / HUB_DIR / "plugins")] + plugin_dirs())
        # **借出清单（V4）**：`[service] lend = ["能力名"]`——对外服务与跨 app 调用
        # 只放行清单内的能力。缺省空 = 一个都不借：默认拒绝原则在服务化里同样成立。
        self.service_lend = list((self.config.get("service") or {}).get("lend") or [])
        slots, names, diags = select_slots(self.registry, self.config, log=self._plugin_log)
        for diag in list(self.registry.diagnostics) + list(diags):
            self.note(diag.level, diag.code, diag.message)
        storage_plugin = slots.get("storage")
        if storage_plugin is None:
            self.plugin_error = "没有可用的 storage 插件：对话历史与运行期记忆无处落盘"
            self.note("error", "PLUGIN_SLOT_MISSING", self.plugin_error)
            return
        self.storage = HostStorage(storage_plugin, str(self.app.root), self._plugin_log)
        self.slots = slots
        self.slot_names = names
        # **沙箱（V2 阶段 3）**：storage 插件跑进子进程。机制/策略分离让这一步便宜——
        # 宿主只把"白名单内已解析的绝对路径"递给子进程，插件拿不到更多。
        # 配置布局：`storage = "名字"` 选槽位；沙箱开关在 `[sandbox]` 表——
        # TOML 不允许 `storage = "file"` 与 `[storage]` 表共存，别把它们放一起。
        sandbox_cfg = self.config.get("sandbox")
        if isinstance(sandbox_cfg, dict) and sandbox_cfg.get("enabled"):
            from .sandbox import sandbox_wrap
            wrapped, warn = sandbox_wrap(self.slot_names.get("storage"), self.registry)
            if warn:
                self.note("warning", "SANDBOX", warn)
            if wrapped is not None:
                self.storage = HostStorage(wrapped, str(self.app.root), self._plugin_log)
                self.note("info", "SANDBOX", "storage 插件已装入子进程沙箱（文件边界 = 白名单）")
        # **落盘走同一通道**：快照与引擎状态也接到 storage 槽位上（白名单、原子写、
        # 写失败标脏对它们同样成立）。程序资产（app.puppet / capabilities.py）不在此列，
        # 它们走命令批与受限文件写入——那是刻意 exclude 的另一条有守卫的通道。
        self.app.storage = self.storage
        self.engine.store = _EngineStore(self.storage)
        # 运行期记忆走**同一个 storage 槽位**：白名单、原子写、写失败标脏，一条都不少。
        self.memory = Memory(self.storage, self.note, self.config.get("memory"))
        self.chat = Chat(self, provider=slots.get("llm_provider"),
                         prompts=slots.get("prompt") or [], storage=self.storage,
                         memory=self.memory, log=self.note,
                         on_delta=lambda piece: self._delta(piece))
        self.chat.mode = "执行"
        # **自主回路（V2 核心）**：与共作者共用 provider（加锁串行）、共用同一条写入路径；
        # 但过程账分文件、预算更紧、危险动作默认拒绝。它是否当值由写者状态机决定。
        if slots.get("llm_provider") is not None:
            from .autonomous import AutonomousRunner
            self.autonomous = AutonomousRunner(
                self, provider=slots.get("llm_provider"),
                prompts=slots.get("prompt") or [], storage=self.storage,
                memory=self.memory, log=self.note,
                on_delta=lambda piece: self._delta(piece))
        self.writer = "llm" if self.chat is not None else "none"
        self._cap_stamp = self._cap_mtime()

    def _plugin_log(self, name: str, level: str, message: str) -> None:
        self.note(level, "PLUGIN:" + name, message)

    def _delta(self, piece: str) -> None:
        if self.on_delta is not None:
            self.on_delta(piece)

    def plugin_chain(self) -> str:
        """当前生效的 provider / storage / prompt 链（驾驶舱常驻显示，与报错配套）。"""
        if not self.slots:
            return "（未装配）"
        provider = self.slots.get("llm_provider")
        describe = getattr(provider, "describe", None)
        prompts = "、".join(self.slot_names.get("prompt") or []) or "（无）"
        return "provider=%s · storage=%s · prompt=%s" % (
            describe() if callable(describe) else (self.slot_names.get("llm_provider") or "无"),
            self.slot_names.get("storage") or "无", prompts)

    def chat_turn(self, text: str):
        """与 LLM 的一轮。**这是唯一能让程序改变的入口**（人只能通过对话表达写入意图）。"""
        if self.chat is None:
            self.note("error", "LLM_UNAVAILABLE",
                      "对话回路不可用：%s" % (self.plugin_error or "插件未装配"))
            return None
        self.check_capabilities()
        result = self.chat.turn(text)
        self._changed()
        return result

    # ------------------------------------------------------------ 写者状态机（V2）

    def set_writer(self, next_writer: str, origin: str = "user",
                   reason: str = "") -> bool:
        """切换写者（llm / autonomous / none）。**互斥、显式、留痕**——这是"单写者"
        在自主模式下的形态：自主当值时共作者退化为只读提问（chat 端动态判定），
        人随时可以暂停或交回。切换本身写决策流水：谁、何时、从谁切到谁。"""
        if next_writer not in WRITERS:
            self.note("error", "WRITER", "未知写者 %r（可用：%s）" % (next_writer, "、".join(WRITERS)))
            return False
        if next_writer == "llm" and self.chat is None:
            self.note("error", "WRITER", "没有共作者回路，无从交回（插件未装配 LLM）")
            return False
        if next_writer == "autonomous" and self.autonomous is None:
            self.note("error", "WRITER", "自主回路不可用（没有装配 LLM provider）")
            return False
        previous = self.writer
        if previous == next_writer:
            return True
        self.writer = next_writer
        if next_writer == "autonomous" and self.autonomous.chat.halted:
            self.autonomous.chat.resume(origin=origin)   # 显式交回 = 重新开始计数
        self.note("info", "WRITER", "写者切换：%s → %s（origin=%s）%s"
                  % (previous, next_writer, origin, ("；%s" % reason) if reason else ""))
        self.app.append_decision("写者切换 %s → %s" % (previous, next_writer),
                                 reason or ("origin=%s" % origin), "运行期")
        self._changed()
        return True

    def autonomous_step(self, trigger: str):
        """自主回路跑一步（事件触发或人手动点）。写者不是它时 runner 自己会拒绝。"""
        if self.autonomous is None:
            self.note("warning", "AUTONOMOUS", "自主回路不可用")
            return None
        entry = self.autonomous.step(trigger)
        self._changed()
        return entry

    # ------------------------------------------------------------ 融合（V3）

    def fusion_brief_text(self) -> str:
        """B 的结构摘要（LLM 上下文固定层）。**只记路径、其余现算**——没有第二个真相。"""
        if not self.fusion_target:
            return ""
        from .appdir import AppDir
        from .fusion import brief_text
        return brief_text(AppDir(self.fusion_target))

    def fuse(self, plan: dict, origin: str = "llm") -> dict:
        """执行融合（确认之后才会被调到）。成功即清融合意图；失败如实上报，
        意图保留（下一轮 LLM 可修正方案重发）。"""
        from .appdir import AppDir
        from .fusion import fuse as _fuse
        b_path = str((plan or {}).get("b") or self.fusion_target or "")
        # 相对基准 = **A 的父目录**（与 chat._resolve_b、hub 的发现语义一致——
        # 融合对象天然是同级 app）。基准不一致曾让 alpha/beta 解析进 A 自己肚子里。
        base = self.app.root.parent
        target = Path(b_path)
        b = AppDir(target if target.is_absolute() else base / target)
        result = _fuse(self.app, b, plan or {}, origin=origin)
        if result["ok"]:
            # _fuse 走的是 AppDir 分支（只写真源文件）；**运行中的实例必须立即
            # 重载**——文件已经是新的，引擎还停在旧程序上，是最经典的静默分叉。
            self.reload()
            self.fusion_target = None
            self.note("info", "FUSION_APPLIED",
                      "融合完成：%s 已并入（%d 行）；B 归档于 %s"
                      % (b.name, result.get("merged_lines", 0), result.get("archive")))
        else:
            for error in result.get("errors") or []:
                self.note("error", "FUSION_FAILED", error)
        self._changed()
        return result

    # ------------------------------------------------------------ 热重载（V2 阶段 3）

    def _cap_mtime(self) -> float:
        try:
            return self.app.capabilities_path.stat().st_mtime
        except OSError:
            return 0.0

    def check_capabilities(self) -> bool:
        """**能力热重载**：`capabilities.py` 在盘上变了就自动重载。

        语言 2.1 的能力装载是幂等的，重载丢瞬态、保持久（与显式重载同一语义）；
        不自动重载的后果是"文件加了能力、引擎不认识"——LLM 以为加了能力，一调用就
        CALL_UNKNOWN，还要它自己发现原因。自动，但**可见**。

        两条竞态守卫：**写入进行中（锁被占）或流式响应中就跳过本轮检查**——
        重载会整体换掉引擎对象，绝不能发生在另一个线程的 apply_batch 中途。
        文件不会跑掉，下一轮检查点再重载，同样是可见的。
        """
        stamp = self._cap_mtime()
        if not stamp or stamp == self._cap_stamp:
            return False
        if (self.chat is not None and self.chat.streaming) \
                or not self._lock.acquire(blocking=False):
            return False
        try:
            self._cap_stamp = stamp
            self._note("CAPABILITY_RELOAD", "信息",
                       "capabilities.py 已在盘上变化，自动重载能力模块")
            self.reload()
            return True
        finally:
            self._lock.release()

    def reload_plugins(self) -> bool:
        """**插件热重载**：重新发现并装配三槽位（配置与白名单即时生效）。

        两条护栏：**流式中拒绝**（半截响应没有归属）与**有待确认动作时拒绝**
        （pending 是内存态，重装回路会把它弄丢——丢了就像"确认过了"，不可接受）。
        对话历史 / approvals / 资产清单都在 storage 里，重装不丢。
        """
        if self.chat is not None and getattr(self.chat, "streaming", False):
            self.note("warning", "PLUGIN_RELOAD", "正在流式响应中，热重载被拒绝（稍后再试）")
            return False
        if self.chat is not None and self.chat.pending is not None:
            self.note("warning", "PLUGIN_RELOAD", "有待确认的动作，热重载被拒绝"
                      "（先批准或拒绝那个动作——重装回路会把它弄丢）")
            return False
        previous_writer = self.writer
        self.writer = "none"
        self.autonomous = None
        self.chat = None
        self.memory = None
        self.storage = None
        self.plugin_error = ""
        self.slots = {}
        self.slot_names = {}
        self._build_plugins()
        if previous_writer == "llm" and self.chat is not None:
            self.writer = "llm"
        elif previous_writer == "autonomous" and self.autonomous is not None:
            self.writer = "autonomous"
            self.autonomous.chat.resume(origin="reload")
        self.note("info", "PLUGIN_RELOAD", "插件已重载；生效链：%s" % self.plugin_chain())
        self._changed()
        return True

    def approve_pending(self) -> None:
        """人确认后执行挂起的那一批。**不重载**：能力模块没变，重载会丢瞬态状态。"""
        if self.chat is not None:
            self.chat.approve_pending()
            self._changed()

    def reject_pending(self) -> None:
        if self.chat is not None:
            self.chat.reject_pending()

    def resume_chat(self, origin: str = "user") -> None:
        """人工接管：解除"停止自动重试"。**这是运行期入口**——`--no-llm` 是另一条实例级通道，
        两者不互替：人接管的是这一个实例里的对话，而不是换一种实例。"""
        if self.chat is None:
            self.note("warning", "LLM_RESUME", "对话回路不可用，无从接管")
            return
        self.chat.resume(origin)
        self._changed()

    @property
    def mode(self) -> str:
        return self.chat.mode if self.chat is not None else "执行"

    def set_mode(self, mode: str) -> None:
        if self.chat is not None:
            self.chat.mode = "讨论" if mode == "讨论" else "执行"
        self.note("info", "MODE", "对话模式：%s" % self.mode)

    @property
    def dirty(self) -> list:
        return list(self.storage.dirty) if self.storage else []

    def retry_storage(self) -> list:
        """重试未落盘的写入。关窗前先调它一次（§6.4：先自动重试，仍失败才拦人）。"""
        if self.storage is None:
            return []
        remaining = self.storage.retry()
        if remaining:
            self.note("error", "STORAGE_DIRTY",
                      "仍有 %d 项改动未写入磁盘：%s" % (len(remaining), remaining[0]))
        return remaining

    def recent_diagnostics(self, limit: int = 40) -> list:
        """喂回 LLM 的诊断摘要：**诊断是搭建回路的核心**，不是附属品。"""
        return list(self.diag_log)[-limit:]

    def hello(self) -> dict:
        """能力声明握手：协议 + 渲染自述 + 能力目录 + 服务清单。

        一份声明、内外兼得：LLM 的词汇边界、诚实的降级、驱动者的能力发现、
        外部 agent 的服务发现（V4 B：app 即服务，manifest 全部派生）。
        """
        return {"protocol": "1", "rendering": dict(RENDERING),
                "catalog": list(self._catalog),
                "service": self.service_manifest()}

    def catalog(self) -> list[dict]:
        return list(self._catalog)

    def _rebuild_catalog(self) -> None:
        self._catalog = _catalog.compact(self.engine.capabilities.values())

    # ------------------------------------------------------------ 凭据（V5）

    def _apply_llm_profile(self) -> None:
        """`[llm] profile = "名字"` → 把机器级 `providers.toml` 的端点/模型/凭据名
        **补进** `[plugins.openai-compat]`（app 里的显式键优先）。

        为什么在宿主做而不在插件里做：机器级文件的位置与解析是宿主的职责，
        插件只该看见"自己的配置"（铁律：插件不拿引擎句柄、只产出值）。这样
        端点与凭据名留在机器上，**app 因此可以安全分享**。
        """
        block = self.config.get("llm")
        if not isinstance(block, dict) or not block.get("profile"):
            return
        name = str(block["profile"])
        table = secrets.profile(name)
        if not table:
            self.note("error", "LLM_PROFILE", secrets.profile_error(name))
            return
        plugins = self.config.setdefault("plugins", {})
        options = plugins.setdefault("openai-compat", {})
        if not isinstance(options, dict):
            return
        added = []
        for key in ("base_url", "model", "key_env", "temperature",
                    "context_limit", "timeout"):
            if key in table and key not in options:
                options[key] = table[key]
                added.append(key)
        self.note("info", "LLM_PROFILE",
                  "profile %s → %s（来自 %s；app 里的显式键优先）"
                  % (name, "、".join(added) or "（无需补）", secrets.providers_path()))

    def credential_names(self) -> list:
        """本 app 需要的凭据变量名（当前 llm provider 的 `key_env`）。"""
        options = (self.config.get("plugins") or {}).get("openai-compat") or {}
        return [str(options.get("key_env") or "OPENAI_API_KEY")]

    def llm_settings(self) -> dict:
        """探活要用的三样（**不含密钥**——由 `keys.probe` 自己去解析）。"""
        options = (self.config.get("plugins") or {}).get("openai-compat") or {}
        return {"base_url": str(options.get("base_url")
                                or "https://api.openai.com/v1"),
                "model": str(options.get("model") or ""),
                "key_env": str(options.get("key_env") or "OPENAI_API_KEY")}

    def check_credentials(self) -> dict:
        """探活 + 泄漏自检（驾驶舱与 CLI 共用同一实现）。"""
        from . import keys as _keys
        return _keys.check(self.app.root, names=self.credential_names(),
                           settings=self.llm_settings())

    # ------------------------------------------------------------ 服务化与协作（V4）

    def service_manifest(self) -> dict:
        """服务清单（B：app 即服务）。**全部派生，不手写**——声明即实现。

        `interactions` 从程序的处理器派生（可编程驱动的合法入口就这些，
        没列的交互不该被外部 agent 猜）；`data` 是可读状态；`lend` 是借出的
        能力（缺省空）。挂在 hello 上：服务发现与能力发现同一握手。
        """
        program = self.engine.program
        return {
            "interactions": sorted({"#%s.%s" % (h.target, h.event)
                                    for h in program.handlers}),
            "data": ["#" + name for name in program.data],
            "lend": list(self.service_lend),
        }

    def call_capability(self, name: str, args: Optional[dict] = None,
                        origin: str = "peer") -> dict:
        """对外服务 + 跨 app 能力借出（A/B）：**同步**执行一个能力。

        能力契约 = 纯函数（值进值出，不碰引擎状态）——所以这条通道不写任何
        真源，单写者铁律成立。但"纯"是声明者说的，**调用权在被调方**：
        不在 `[service] lend` 清单里一律拒绝（默认拒绝，与自主白名单同源）。
        参数校验与引擎 `_exec_call` 同一契约；超时与序列化校验同规范。
        """
        cap = self.engine.capabilities.get(name)
        if cap is None:
            self._note("CALL_UNKNOWN", "错误", "能力 %s 不存在（origin=%s）" % (name, origin))
            return {"ok": False, "error": "能力 %s 不存在" % name}
        if name not in self.service_lend:
            self._note("SERVICE_DENIED", "警告",
                       "能力 %s 未列入借出清单（[service] lend），默认拒绝（origin=%s）"
                       % (name, origin))
            return {"ok": False, "error": "能力 %s 未借出（被调方清单决定调用权）" % name}
        args = dict(args or {})
        params = cap.get("params") or []
        missing = [p.get("name") for p in params
                   if p.get("required") and p.get("name") not in args]
        if missing:
            return {"ok": False, "error": "缺少必需参数：%s" % ", ".join(map(str, missing))}
        known = {p.get("name") for p in params}
        extra = [k for k in args if k not in known]
        if extra:
            return {"ok": False, "error": "多余参数：%s" % ", ".join(sorted(extra))}
        from puppet.capabilities import type_matches
        bad = ["%s 期望 %s" % (p["name"], p.get("type")) for p in params
               if p.get("name") in args and not type_matches(p.get("type", "any"),
                                                             args[p["name"]])]
        if bad:
            return {"ok": False, "error": "参数类型不符：%s" % "；".join(bad)}
        fn = cap.get("callable")
        if fn is None:
            return {"ok": False, "error": "能力 %s 没有可执行体" % name}
        timeout = float((getattr(self.engine, "limits", None) or {})
                        .get("callTimeoutMs", 5000)) / 1000.0
        import inspect
        # **为什么不用 ThreadPoolExecutor**：① 它的上下文管理器退出时 `shutdown(wait=True)`
        # 会等那个**已判定超时**的任务跑完，超时就只写在诊断里、调用方照样被卡死；
        # ② 它的工作线程是**非守护**线程，挂死的能力会在解释器退出时被 atexit join，
        # 把"关不掉"从这一步一路传染到进程结束。
        # 所以自己起一条 **daemon** 线程 + Event 等待：超时能真脱身，挂死线程随进程一起消失。
        box: dict = {}
        done = threading.Event()

        def _invoke() -> None:
            try:
                box["value"] = (asyncio.run(fn(**args))
                                if inspect.iscoroutinefunction(fn) else fn(**args))
            except BaseException as ex:  # noqa: BLE001 - 能力炸了/挂了都要过线
                box["error"] = ex
            finally:
                done.set()

        worker = threading.Thread(target=_invoke, daemon=True,
                                  name="capability-%s" % name)
        worker.start()
        if not done.wait(timeout):
            self._note("SLOT_TIMEOUT", "错误",
                       "能力 %s 执行超时（%s）——本步放弃等待，挂死的线程随进程退出"
                       % (name, timeout))
            return {"ok": False, "error": "能力 %s 执行超时" % name}
        if "error" in box:
            ex = box["error"]
            self._note("CALL_RESULT", "错误",
                       "能力 %s 调用失败：%s: %s" % (name, type(ex).__name__, ex))
            return {"ok": False, "error": "%s: %s" % (type(ex).__name__, ex)}
        value = box.get("value")
        try:
            json.dumps(value)
        except (TypeError, ValueError) as ex:
            self._note("CALL_RESULT", "错误", "能力 %s 返回值不可序列化：%s" % (name, ex))
            return {"ok": False, "error": "返回值不可序列化"}
        self._note("SERVICE_CALL", "信息",
                   "能力 %s 经借出通道被调用（origin=%s）" % (name, origin))
        return {"ok": True, "value": value}

    def attach_hub(self, hub_port: int) -> bool:
        """接入协作总线（hub 编排时把端口传进来）。订阅也在这里注册：
        app 重启 = 重新注册，总线端不需要持久化订阅表。"""
        from .bus import BusClient
        self.bus = BusClient(self.app.name, hub_port)
        topics = list((self.config.get("collab") or {}).get("subscribe") or [])
        if topics:
            reply = self.bus.subscribe(topics)
            if not reply.get("ok"):
                self._note("BUS", "警告", "订阅注册失败：%s" % reply.get("error"))
                return False
        self._note("BUS", "信息", "已接入协作总线 127.0.0.1:%d（订阅：%s）"
                   % (hub_port, "、".join(topics) if topics else "无"))
        return True

    def send_peer_message(self, peer: str, topic: str, text: str,
                          title: str = "") -> bool:
        """`tell` 指令块的执行体：给 peer 发协作消息。**不写对方**——
        投递是刺激（对方的观察流 + 自主回路），不是写入。"""
        if self.bus is None:
            self._note("BUS", "警告",
                       "未接入协作总线（需要 hub 启动并在配置/启动参数里给 hub_port），"
                       "消息没有发送——不装作发了")
            return False
        reply = self.bus.post(topic=topic, text=text, title=title)
        if not reply.get("ok"):
            self._note("BUS", "错误", "消息发送失败：%s" % reply.get("error"))
            return False
        self._note("BUS", "信息", "消息已投递 topic=%s → %s（未送达：%s）"
                   % (topic, "、".join(reply.get("delivered") or []) or "（无订阅者）",
                      "、".join(reply.get("failed") or []) or "无"))
        return True

    def deliver_peer_event(self, from_app: str, topic: str, text: str,
                           title: str = "") -> dict:
        """总线的投递端点：**不写真源**——进观察流，写者当值时触发自主回路。"""
        self._note("PEER_MESSAGE", "信息", "来自 %s（%s）%s：%s"
                   % (from_app, topic, ("「%s」" % title) if title else "",
                      text[:200]))
        self._changed()
        if self.autonomous is not None and self.writer == "autonomous":
            self.autonomous.on_peer_message(from_app, topic, text)
        return {"ok": True}

    # ------------------------------------------------------------ 目标（C）

    def set_goal(self, text: str, origin: str = "llm") -> bool:
        """设定自主回路的目标（C：从被动响应到有方向）。存 storage——跨会话
        可见；决策流水留痕（谁在何时把方向盘转到哪）。空串 = 清除。"""
        text = (text or "").strip()
        payload = {"text": text[:400], "origin": origin,
                   "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            self.storage.write_json(".puppethub/goal.json", payload)
        except Exception as ex:  # noqa: BLE001 - 已标脏，这里可见即可
            self._note("GOAL", "错误", "目标写入失败：%s" % ex)
            return False
        self.app.append_decision("设定目标" if text else "清除目标",
                                 text[:120] or "（清空）", origin)
        self._note("GOAL", "信息", ("目标已更新：%s" % text[:80]) if text else "目标已清除")
        self._changed()
        return True

    def current_goal(self) -> str:
        try:
            data = self.storage.read_json(".puppethub/goal.json", None) or {}
        except Exception:  # noqa: BLE001 - 读不出当没有
            return ""
        return str(data.get("text") or "")

    # ------------------------------------------------------------ 写入

    def send(self, lines: list, origin: str = "llm") -> list:
        """应用一个命令批，并在批末一次性写回真源。

        写回**之前**先自动存档：LLM 每轮对话都在改真源，只在显式重载前存远远不够。
        """
        assert origin in ORIGINS, origin
        # **机制层的写者核对**（不只靠 chat.turn 的入口检查）：写者状态机切换后，
        # **在途**的那一轮（流式慢、步进线程还在跑）会迟到这里——"暂停必须真的
        # 停得住"，否则切换只是仪式。拒绝本身是可见诊断：在途调用方拿到它，
        # 卡住/熔断的计数也就看到了真实的失败，而不是一个假成功。
        if origin in ("llm", "autonomous") and origin != self.writer:
            from puppet import ERROR, Diagnostic
            diag = Diagnostic("WRITER_DENIED", ERROR,
                              "当前写者是 %s，%s 的写入被拒绝（写者状态机在写入路径上核对，"
                              "在途调用同样拦下）" % (self.writer, origin))
            self._absorb_diags([diag], "写入拒绝")
            return [diag]
        with self._lock:
            before = self.app.read_source()
            diags = self.engine.apply_batch(list(lines))
            after = self.engine.program_lines()
            changed = after != before
            if changed:
                self.app.push_snapshot("auto", "写回前自动存档", origin, source_lines=before)
                self.app.write_source(after, origin)
            self._rebuild_catalog()
        self._absorb_diags(diags, "命令批（%s）" % origin)
        if changed:
            self._note("WRITEBACK", "信息",
                       "%s 改写真源：%d 行（批末整份重排，已自动存档）" % (origin, len(after)))
        else:
            self._note("WRITEBACK", "信息", "%s 的命令批未改变真源文本" % origin)
        self._changed()
        return diags

    def load_source(self, lines: list, origin: str = "system") -> list:
        """整体替换真源（"推倒重来"的落点）。**只由确认过的系统动作调用**。"""
        assert origin in ORIGINS, origin
        self.app.write_source(list(lines), origin)
        self.note("info", "WRITEBACK",
                  "%s 整体替换真源：%d 行（替换前已存兜底快照）" % (origin, len(lines)))
        diags = self.reload()
        self._changed()
        return diags

    def fire(self, target: str, event: str, row: Optional[int] = None,
             value=None) -> list:
        """交互事件：用户动作 → 引擎。渲染器不自己实现业务反应。"""
        diags = self.engine.fire(target, event, row, value)
        self._absorb_diags(diags, "交互 %s #%s" % (event, target))
        self._changed()
        return diags

    # ------------------------------------------------------------ 观察

    def refresh(self):
        """驱动渲染，并取走观察流。返回观察快照（诊断/事件/探针已取走）。

        渲染读的是 `render_state()`（**只读**），观察流由本方法独占取走——
        两条路分开，渲染永远不可能"吃掉"驱动者的事件与诊断。
        """
        if self.renderer is not None:
            for code, message in self.renderer.apply(self.engine.render_state()):
                # 渲染器上报的降级/异常一律是警告：它们代表"你说的这件事没做到"。
                self._note(code, "警告", message)
        snap = self.engine.observe()
        self._absorb(snap)
        return snap

    def _absorb(self, snap: dict) -> None:
        for diag in snap.get("diagnostics", []):
            self._note(diag.get("code", "?"), _level_name(diag.get("level")),
                       diag.get("message", ""), diag.get("line"))
            self._remember_diag(diag)
        for event in snap.get("events", []):
            self._note("EVENT", "事件", "%s %s%s" % (event.get("target"), event.get("event"),
                                                    _payload(event)))
        for probe in snap.get("probes", []):
            self._note("PROBE", "探针", "%s %s → %s" % (probe.get("verb"), probe.get("target"),
                                                       _short(probe.get("result"))))

    def _absorb_diags(self, diags, stage: str) -> None:
        for diag in diags or []:
            self._note(diag.code, _level_name(diag.level), "%s：%s" % (stage, diag.message),
                       diag.line)
            self._remember_diag(diag.to_dict() if hasattr(diag, "to_dict") else diag)

    def _remember_diag(self, diag) -> None:
        """进 `diag_log` 的才是**喂回 LLM** 的那一份（搭建回路靠它闭合）。"""
        payload = diag.to_dict() if hasattr(diag, "to_dict") else dict(diag)
        if not payload.get("code"):
            return
        self.diag_log.append(payload)

    def note(self, level: str, code: str, message: str, line=None) -> None:
        self._note(code, _LEVELS.get(level, str(level)), message, line)

    # ------------------------------------------------------------ 系统动作

    def snapshot_program(self, source_lines: list[str] | None = None) -> Snapshot:
        return self.app.push_snapshot("auto", "写前自动存档", "system", source_lines)

    def named_snapshot(self, label: str) -> Snapshot:
        snap = self.app.push_snapshot("named", label or "里程碑", "user")
        self._note("SNAPSHOT", "信息", "已命名快照 %s：%s" % (snap.id, snap.reason))
        return snap

    def restore(self, snapshot_id: str) -> Snapshot:
        snap = self.app.restore(snapshot_id, "system")
        self._note("RESTORE", "信息", "已回滚到 %s（回滚前已自动存档）" % snapshot_id)
        self.reload()
        self._changed()
        return snap

    def reset(self) -> None:
        """重置**只清状态**：记忆与快照不受影响。"""
        self.app.reset_state()
        self.app.ensure_layout()      # 状态目录刚被删掉：马上重建，免得随后写入落到空处
        self.reload()
        self._note("RESET", "信息", "已重置引擎状态（记忆与快照保留）")
        self._changed()

    def wipe_memory(self, origin: str = "user") -> None:
        """清空运行期记忆。**必须显式**（驾驶舱按钮 / `run --wipe-memory`）——重置不碰它。

        与"超出上限自动裁剪"在观察流里用**不同的码**（`MEMORY_WIPED` vs `MEMORY_TRIMMED`）：
        一个是人的指令，一个是系统自保；混在一起就分不清"我的记忆怎么没了"。
        """
        if self.memory is not None:
            # Memory.wipe 自带一条 MEMORY_WIPED（含清掉的条数）；这里**不再重记**，
            # 否则同一次清空在观察流里出现两条同样的码，反而看不清清了几条。
            self.memory.wipe(origin)
        else:
            # 记忆槽位没装配时也要留痕：整区照样被清掉了，不能毫无记录。
            self.note("info", "MEMORY_WIPED",
                      "运行期记忆整区已清空（origin=%s；记忆未装配，无法计数）" % origin)
        self.app.wipe_memory()          # 整区清掉：记忆是独立成区的，清就清干净

    def memory_entries(self) -> list:
        return self.memory.entries() if self.memory is not None else []

    def memory_text(self) -> str:
        return self.memory.export_text() if self.memory is not None else "（记忆不可用）"

    def verify(self, filter_text: str = "", on_line=None) -> str:
        """自证（断言①的入口）：让**产品渲染器**跑一遍 conformance。

        它 spawn 一个子进程（`puppethub.protocol`），结果以行流进观察流——
        "自证"不是一句保证，是一条能看的输出。找不到运行器、跑不完、有 FAIL，都会明说。
        """
        from .verify import run_conformance
        self._note("VERIFY", "信息", "开始自证：用产品渲染器跑 conformance")

        def sink(line: str) -> None:
            self.note("info", "VERIFY", line)
            if on_line is not None:
                on_line()

        result = run_conformance(on_line=sink, filter_text=filter_text)
        if not result["ok"]:
            head = result.get("error") or result["summary"] or "自证未通过"
            self._note("VERIFY", "错误", "自证未通过：%s" % head)
        else:
            self._note("VERIFY", "信息", "自证通过：%s" % result["summary"])
        return "自证%s（%s）\n%s" % ("通过" if result["ok"] else "未通过",
                                   result["summary"] or "无汇总",
                                   "\n".join(result["failed"][:8]))

    # ------------------------------------------------------------ 文本视图

    def program_text(self) -> str:
        return "\n".join(self.engine.program_lines())

    def program_assets_text(self) -> str:
        return self.app.read_capabilities()

    # ------------------------------------------------------------ 日志

    def _note(self, code: str, level: str, message: str, line=None) -> None:
        where = ("第 %d 行" % line) if line else ""
        self.log.append({"time": time.strftime("%H:%M:%S"), "code": code,
                         "level": level, "where": where, "text": message})
        # 自主回路的事件源（V2 M2）：引擎报错 → 触发自我修复。runner 自己会挡掉
        # "自己动作的回声"（busy / 拒绝类诊断），这里只负责把声音传过去。
        if self.autonomous is not None and level == "错误":
            self.autonomous.on_diagnostic(level, code, message)

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()


def _level_name(level: str) -> str:
    return {"error": "错误", "warning": "警告", "info": "信息"}.get(level, str(level))


def _payload(event: dict) -> str:
    extra = {k: v for k, v in event.items() if k not in ("target", "event")}
    return (" " + _short(extra)) if extra else ""


def _short(value, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"
