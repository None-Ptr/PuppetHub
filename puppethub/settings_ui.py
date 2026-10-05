"""首页的设置浮层：**模型 profile / 凭据 / 调度官**三件事（`docs/design-home.md` §5.14）。

它只组装既有 service 层（`keys` / `secrets` / `config_edit` / `Orchestrator`），
**不写第二份写入语义**——CLI `puppethub keys …` 与这里共用同一份实现，是为不漂移。

四条纪律，逐条对应这里的做法：

1. **凭据永不回显**（连掩码都不给）：界面只显示"来自 env / 钥匙串 │ 长度 N"。
   `providers.toml` 里存的只是**环境变量名**（`key_env`），值从不落进任何配置文件。
2. **写配置一律 `config_edit.set_options`**（外科式）：注释与行序原样保留——
   配置是给人读的，注释是它的一半。
3. **改模型立刻生效**：走 `Orchestrator.reconfigure()` 热重建。首页是社会层宿主，
   为一个模型重启首页 = 把总线和所有已接线的小孩一起关掉，不值。
4. **自主白名单只能人给**（Q18）：界面上那排动词开关就是"人的显式动作"落点，
   调度官自己改不了自己的 `allow`。
"""

from __future__ import annotations

import re
import threading

import flet as ft

from . import keys, secrets, theme
from .config_edit import set_options
from .orchestrator import config_path, read_config, resolve_model

TITLE = "设置"
WIDTH = 920

PROFILE_FIELDS = ("base_url", "model", "key_env")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")     # TOML 小节名——点击概念式名字会写坏文件
_NEW = "\x00new"                                # 编辑表单的"这是新建"标记（不可能重名）


def _gated_verbs(ops) -> tuple:
    """需要**人手动授权**的动词 = 全部动词 − 自主默认放行 − 黑名单。

    从 `SocietyOps` 派生而不是手抄一遍列表：抄下来的单子会和真边界漂移，而这条边界
    正是"人手＝唯一的授权源"的凭据。
    """
    cls = type(ops) if ops is not None else None
    verbs = getattr(cls, "VERBS", None) or ()
    free = getattr(cls, "FREE_WHEN_AUTONOMOUS", None) or ()
    return tuple(v for v in verbs if v not in free)


# ---------------------------------------------------------------- service 层
#
# 这四个函数是**这一屏的真相**，浮层只是它们的呈现。抽出来不是为好看：界面驱动不了
# （没有 page 就渲染不出来），而"留空不改""删除要连指针一起清"这类语义必须能被
# 测试直接打（`docs/smoke-home.py` 第七组）。将来 CLI 若要 `puppethub keys profile …`，
# 也用这一份——两处各写一遍语义，迟早漂移。

def save_profile(name: str, *, base_url: str = "", model: str = "",
                 key_env: str = "", api_key: str = "",
                 require_endpoint: bool = False) -> dict:
    """写一个本机 profile。返回 `{ok, name, wrote, notes, error}`。

    **留空 = 不改**（表单里的空白不该悄悄抹掉配置）。给了 `api_key` 时：
    没声明 `key_env` 就落到 `<NAME>_API_KEY` 上并把名字回写进 profile——不丢这把钥匙。
    凭据**永远走钥匙串**（0600、在 app 目录之外），不进任何配置文件。
    """
    name = str(name or "").strip()
    if not _NAME_RE.match(name):
        return {"ok": False, "name": name, "wrote": [], "notes": [],
                "error": "名字只能是字母/数字/下划线/短横线：%r（TOML 小节名的限制）" % name}
    if require_endpoint and not (base_url or "").strip() \
            and not (secrets.profile(name) or {}).get("base_url"):
        return {"ok": False, "name": name, "wrote": [], "notes": [],
                "error": "缺 base_url：没有端点，模型无从对话"}
    values = {}
    for key, value in (("base_url", base_url), ("model", model), ("key_env", key_env)):
        if str(value or "").strip():
            values[key] = str(value).strip()
    notes: list = []
    try:
        set_options(secrets.providers_path(), name, values)
        wrote = [str(secrets.providers_path())]
        secret = str(api_key or "").strip()
        if secret:
            target = values.get("key_env") \
                or str((secrets.profile(name) or {}).get("key_env") or "") \
                or ("%s_API_KEY" % name.upper().replace("-", "_"))
            outcome = keys.set_secret(target, secret, target="keystore")
            if not values.get("key_env"):
                set_options(secrets.providers_path(), name, {"key_env": target})
            notes.append(outcome.get("note") or "凭据已录入")
    except Exception as ex:  # noqa: BLE001 - 写不进去必须当场说，不能"看起来存了"
        return {"ok": False, "name": name, "wrote": [], "notes": [],
                "error": "写入 %s 失败：%s: %s" % (secrets.providers_path(),
                                                 type(ex).__name__, ex)}
    notes.append("已保存 profile `%s` → %s" % (name, secrets.providers_path()))
    return {"ok": True, "name": name, "wrote": wrote, "notes": notes, "error": ""}


def delete_profile(name: str) -> dict:
    """删掉一个 profile，**并连指向它的 `[llm] profile` 一起清掉**。

    为什么一定要清指针：留下它会指向一个不存在的 profile——启动时的报错是"该 profile
    没定义"，而不是"你刚删掉了它"，这就是另一种静默失败。**凭据不动**（它在钥匙串里）。
    """
    name = str(name or "").strip()
    table = secrets.profile(name)
    if table is None:
        return {"ok": False, "notes": [], "wrote": [],
                "error": "%s 里没有 profile `%s`" % (secrets.providers_path().name, name)}
    notes = []
    try:
        # 逐键置 None = 删除该键；键删空后 `set_options` 会连小节一起清掉
        set_options(secrets.providers_path(), name, {key: None for key in table})
        pointed = str((read_config().get("llm") or {}).get("profile") or "").strip()
        if pointed == name:
            set_options(config_path(), "llm", {"profile": None})
            notes.append("调度官原来指着 `%s`，已一并清除指向" % name)
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "notes": [], "wrote": [],
                "error": "删除失败：%s: %s" % (type(ex).__name__, ex)}
    notes.append("已删除 profile `%s`" % name)
    return {"ok": True, "notes": notes, "wrote": [str(secrets.providers_path())],
            "error": ""}


def pick_profile(name: str) -> dict:
    """给调度官选一个 profile（空串 = 清除指向，回到"唯一则自动用"）。"""
    name = str(name or "").strip()
    try:
        set_options(config_path(), "llm", {"profile": name or None})
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": "写入失败：%s: %s" % (type(ex).__name__, ex)}
    return {"ok": True, "error": "",
            "note": ("已写 %s：[llm] profile = \"%s\"" % (config_path().name, name))
                    if name else "已清除 profile 指向"}


def set_allow(verbs) -> dict:
    """重写自主白名单（`[autonomous] allow`）。**只能人调用**——调度官没有这条路。"""
    listing = sorted({str(v).strip() for v in (verbs or []) if str(v).strip()})
    try:
        set_options(config_path(), "autonomous", {"allow": listing})
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": "写入失败：%s: %s" % (type(ex).__name__, ex)}
    return {"ok": True, "allow": listing, "error": ""}


class SettingsPanel:
    def __init__(self, win):
        self.win = win
        self.editing: str | None = None
        self.status = theme.mono("", size=theme.SIZE_DATA, color=theme.DIM,
                                 selectable=False)

    # ---------------------------------------------------------------- 入口

    def open(self, edit=None) -> None:
        """`edit=None` = 列表；给了 profile 名 = 该 profile 的编辑表单。"""
        self.editing = edit
        theme.overlay_scrim(
            self.win.page, TITLE, self._body(),
            actions=[theme.btn("关闭", self._close)], width=WIDTH)

    def _close(self, _event=None) -> None:
        theme.close_overlay(self.win.page)
        self.win._render_status()

    def _refresh(self, edit=None) -> None:
        self.open(self.editing if edit is None else edit)

    # ---------------------------------------------------------------- 说话

    def _say(self, level: str, text: str) -> None:
        """一句话两处留痕：浮层里的状态行（看得见）+ 转录（关掉浮层后仍可查）。"""
        color = {"error": theme.RED, "warning": theme.AMBER,
                 "info": theme.GREEN}.get(level, theme.DIM)

        def job():
            self.status.color = color
            self.status.value = theme.plain(text)

        # UI 改动一律丢给首页的事件泵执行：`page.update()` 只该发生在 UI 线程上，
        # 而探活/保存都在别的线程里跑。
        self.win._q("call", job)
        self.win._note_transcript(level, text)

    @property
    def _orch(self):
        return self.win.orchestrator

    # ---------------------------------------------------------------- 主体

    def _body(self) -> ft.Column:
        controls = []
        if self.editing is not None:
            controls += self._edit_form()
        else:
            controls += self._profiles_section()
            controls += [theme.rule(), self._orch_section()]
            controls += [theme.rule(), self._credential_section()]
        controls += [theme.rule(), self.status]
        return ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, controls=controls)

    def _path_note(self, label: str, path) -> ft.Row:
        return ft.Row(spacing=6, controls=[
            theme.micro(label),
            theme.micro(str(path), color=theme.RULE)])

    # ---------------------------------------------------------------- 模型 profile

    def _profiles_section(self) -> list:
        table = secrets.profiles()
        controls = [theme.section("模型 profile"),
                    self._path_note("文件", secrets.providers_path())]
        if not table:
            controls.append(theme.mono("（还没有 profile）",
                                       size=theme.SIZE_DATA, color=theme.DIM,
                                       selectable=False))
        for name in sorted(table):
            controls.append(self._profile_row(name, table[name]))
        controls.append(ft.Row(spacing=8, controls=[
            theme.primary("新建 profile", lambda _e: self._refresh(_NEW))]))
        return controls

    def _profile_row(self, name: str, table: dict) -> ft.Control:
        current = str((read_config().get("llm") or {}).get("profile") or "").strip()
        marked = current == name or (not current and len(secrets.profiles()) == 1)
        info = theme.micro("调度官在用", color=theme.GREEN) if marked else None
        controls = [
            theme.mono(name, size=theme.SIZE_DATA,
                       color=theme.AMBER if marked else theme.TEXT,
                       weight=ft.FontWeight.BOLD if marked else None,
                       selectable=False, width=110, no_wrap=True),
            theme.mono(str(table.get("model") or "—"), size=theme.SIZE_MICRO,
                       color=theme.DIM, selectable=False, width=160, no_wrap=True,
                       overflow=ft.TextOverflow.ELLIPSIS),
            theme.mono(str(table.get("base_url") or "—"), size=theme.SIZE_MICRO,
                       color=theme.DIM, selectable=False, expand=True, no_wrap=True,
                       overflow=ft.TextOverflow.ELLIPSIS),
            self._key_badge(str(table.get("key_env") or "")),
        ]
        if info is not None:
            controls.append(info)
        for label, handler in (("探活", lambda _e, n=name: self._probe(n)),
                               ("编辑", lambda _e, n=name: self._refresh(n)),
                               ("删除", lambda _e, n=name: self._delete(n))):
            controls.append(theme.btn(label, handler))
        return ft.Row(spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                      controls=controls)

    def _key_badge(self, name: str) -> ft.Control:
        """凭据层徽章：**只报来源，绝不回显**。"""
        if not name:
            return theme.micro("未声明 key_env", color=theme.AMBER, width=120)
        value, source = secrets.resolve(name)
        if not value:
            return theme.micro("凭据未设置", color=theme.AMBER, width=120)
        return theme.micro("来自 %s（长度 %d）" % (source, len(value)),
                           color=theme.DIM, width=120)

    def _edit_form(self) -> list:
        creating = self.editing == _NEW
        name = "" if creating else self.editing
        table = {} if creating else (secrets.profile(name) or {})
        name_field = theme.field(value=name, hint="名字（例如 deepseek）", width=180)
        if not creating:
            name_field.read_only = True     # 改名会让 [llm] profile 指向落空；要改名请删了重建
        fields = {}
        for key in PROFILE_FIELDS:
            fields[key] = theme.field(value=str(table.get(key) or ""),
                                      hint={"base_url": "https://…/v1",
                                            "model": "模型名",
                                            "key_env": "环境变量名（凭据不落地在这里）"}[key])
        key_field = theme.field(hint="新密钥（留空不改）", password=True)
        for control in [name_field, *fields.values(), key_field]:
            # 输入框聚焦时**不能被首页快捷键截走**：否则填表单时按 n 会弹出新建 app
            control.on_focus = self.win._focus_on
            control.on_blur = self.win._focus_off
        controls = [
            theme.section("新建 profile" if creating else "编辑 profile `%s`" % name),
            self._path_note("文件", secrets.providers_path()),
            *[theme.labeled(key, fields[key], label_width=110) for key in PROFILE_FIELDS],
        ]
        if creating:
            controls.insert(2, theme.labeled("名字", name_field, label_width=110))
        controls.append(theme.labeled("密钥", key_field, label_width=110))
        controls.append(ft.Row(spacing=8, controls=[
            theme.primary("保存", lambda _e: self._save(name, name_field, fields, key_field)),
            theme.btn("取消", lambda _e: self._refresh(None))]))
        return controls

    def _save(self, original, name_field, fields, key_field) -> None:
        creating = original == _NEW or not original
        name = (name_field.value or "").strip() if creating else original
        outcome = save_profile(name,
                               base_url=fields["base_url"].value or "",
                               model=fields["model"].value or "",
                               key_env=fields["key_env"].value or "",
                               api_key=key_field.value or "",
                               require_endpoint=creating)
        if not outcome.get("ok"):
            self._say("warning", outcome.get("error") or "保存失败")
            return
        for note in outcome.get("notes") or []:
            self._say("info", note)
        self._reconfigure()

    def _delete(self, name: str) -> None:
        body = ft.Column(spacing=8, controls=[
            theme.markup("删除本机 profile **`%s`**？" % name, size=theme.SIZE_DATA),
            theme.micro("凭据不动（在钥匙串里）。", color=theme.DIM)])

        def do_it(_event=None) -> None:
            outcome = delete_profile(name)
            if not outcome.get("ok"):
                self._say("error", outcome.get("error") or "删除失败")
                return
            for note in outcome.get("notes") or []:
                # 清掉指针是**附带后果**，必须看得见：否则人以为只删了一段配置
                self._say("warning" if "清除指向" in note else "info", note)
            self._reconfigure()

        theme.overlay_scrim(self.win.page, "删除 profile？", body,
                            actions=[theme.btn("留下", lambda _e: self._refresh(None)),
                                     theme.primary("删除", do_it)], width=560)

    def _probe(self, name: str) -> None:
        """探活：六件事（401/403/404/429/超时/连不上）修法完全不同，先分清再动手。"""
        table = secrets.profile(name) or {}

        def work():
            try:
                result = keys.probe({"base_url": table.get("base_url"),
                                     "model": table.get("model"),
                                     "key_env": table.get("key_env")})
            except Exception as ex:  # noqa: BLE001 - 探活自己炸了也得说
                self._say("error", "探活异常：%s: %s" % (type(ex).__name__, ex))
                return
            self._say("info" if result.get("ok") else "warning",
                      "探活 `%s`：%s" % (name, result.get("message")))

        self._say("info", "正在探活 `%s` …" % name)
        threading.Thread(target=work, daemon=True, name="home-probe-model").start()

    # ---------------------------------------------------------------- 调度官

    def _orch_section(self) -> ft.Column:
        raw = read_config()
        model = resolve_model(raw)
        explicit = str((raw.get("llm") or {}).get("profile") or "").strip()
        names = sorted(secrets.profiles())
        if self._orch is None:
            head = "调度官：不可用（%s）" % (model.get("error") or "没有 provider") \
                if model.get("error") else "调度官：本次是 `--no-llm`"
        else:
            head = "调度官：%s" % ("已就绪（profile `%s`）" % model["profile"]
                                  if model.get("profile") and not model.get("error")
                                  else "（未就绪）")
        controls = [
            theme.section("调度官"),
            self._path_note("文件", config_path()),
            theme.mono(head, size=theme.SIZE_DATA,
                       color=theme.RED if model.get("error") else theme.TEXT,
                       selectable=False),
        ]
        if model.get("error"):
            # 报错文案本身是**给人照着做的**（去哪里写、现有哪几个），原样显示
            controls.append(theme.mono(theme.plain(model["error"]),
                                       size=theme.SIZE_MICRO, color=theme.AMBER,
                                       selectable=False))
        row = [theme.micro("用哪个 profile：", color=theme.DIM)]
        for name in names:
            active = name == explicit
            row.append(theme.btn(name, lambda _e, n=name: self._use(n),
                                 tone=theme.AMBER if active else theme.DIM,
                                 bold=active))
        if explicit:
            row.append(theme.btn("清除指向", lambda _e: self._use("")))
        if names:
            controls.append(ft.Row(spacing=6, wrap=True, run_spacing=4, controls=row))
        controls += self._allow_section()
        return ft.Column(spacing=6, controls=controls)

    def _allow_section(self) -> list:
        verbs = _gated_verbs(self.win.ops)
        orch = self._orch
        allowed = set()
        if orch is not None:
            allowed = set(str(v) for v in (orch.config.get("allow") or []))
        controls = [theme.rule(), theme.section("自主白名单")]
        if not verbs:
            controls.append(theme.micro("（没有可授权的动词）", color=theme.DIM))
        row = []
        for verb in verbs:
            on = verb in allowed
            row.append(theme.btn("[%s] %s" % ("x" if on else " ", verb),
                                 lambda _e, v=verb: self._toggle_allow(v),
                                 tone=theme.AMBER if on else theme.DIM, bold=on))
        controls.append(ft.Row(spacing=6, wrap=True, run_spacing=4, controls=row))
        return controls

    def _toggle_allow(self, verb: str) -> None:
        orch = self._orch
        if orch is None:
            self._say("warning", "没有调度官：白名单改了也没有对象")
            return
        allowed = {str(v) for v in (orch.config.get("allow") or [])}
        if verb in allowed:
            allowed.discard(verb)
        else:
            allowed.add(verb)
        outcome = set_allow(allowed)
        if not outcome.get("ok"):
            self._say("error", outcome.get("error") or "写入失败")
            return
        listing = outcome.get("allow") or []
        self._reconfigure()          # 让清单**当场**生效（不必重启首页）
        self._say("info", "自主白名单 %s：%s"
                  % (("+" + verb) if verb in allowed else ("-" + verb),
                     "、".join(listing) or "空"))

    def _use(self, name: str) -> None:
        outcome = pick_profile(name)
        if not outcome.get("ok"):
            self._say("error", outcome.get("error") or "写入失败")
            return
        self._say("info", outcome.get("note") or "已更新 profile 指向")
        self._reconfigure()

    def _reconfigure(self) -> None:
        orch = self._orch
        if orch is None:
            self._refresh(None)
            return
        try:
            outcome = orch.reconfigure()
        except Exception as ex:  # noqa: BLE001 - 热重建失败要可见，不能"看起来换了"
            self._say("error", "重建失败：%s: %s" % (type(ex).__name__, ex))
            return
        if outcome.get("error"):
            self._say("warning", outcome["error"])
        if outcome.get("note"):
            self._say("info", outcome["note"])
        self.win._render_status()
        self._refresh(None)

    # ---------------------------------------------------------------- 凭据

    def _credential_section(self) -> ft.Column:
        controls = [theme.section("凭据"),
                    self._path_note("钥匙串", secrets.secrets_path())]
        targets = keys.credential_targets()
        # 先看 profile 们声明的那些（顺序稳定），再看别的
        declared = []
        for name in sorted(secrets.profiles()):
            env_name = str((secrets.profile(name) or {}).get("key_env") or "")
            if env_name and env_name not in declared:
                declared.append(env_name)
        ordered = declared + [n for n in sorted(targets) if n not in declared]
        if not ordered:
            controls.append(theme.mono("（还没有凭据）", size=theme.SIZE_DATA,
                                       color=theme.DIM, selectable=False))
        for name in ordered:
            info = targets.get(name) or {}
            if not info:
                info = {"source": None, "keystore": False, "env": False, "length": 0}
            state = ("未设置" if not info.get("length")
                     else "来自 %s │ 长度 %d" % (info.get("source"), info["length"]))
            row_controls = [
                theme.mono(name, size=theme.SIZE_DATA, selectable=False, width=200,
                           no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                theme.micro(state, color=theme.DIM if info.get("length") else theme.AMBER,
                            width=150),
            ]
            if info.get("keystore"):
                row_controls.append(theme.btn("撤掉钥匙串里的值",
                                              lambda _e, n=name: self._revoke(n)))
            controls.append(ft.Row(spacing=6,
                                   vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                   controls=row_controls))
        name_field = theme.field(hint="例如 DEEPSEEK_API_KEY", width=200)
        value_field = theme.field(hint="密钥", password=True)
        for control in (name_field, value_field):
            control.on_focus = self.win._focus_on
            control.on_blur = self.win._focus_off
        controls += [
            theme.labeled("新凭据", ft.Row(spacing=8, controls=[
                ft.Container(content=name_field, width=200),
                ft.Container(content=value_field, expand=True)]), label_width=110),
            ft.Row(spacing=8, controls=[
                theme.primary("录入钥匙串",
                              lambda _e: self._set_key(name_field, value_field))])]
        return ft.Column(spacing=6, controls=controls)

    def _set_key(self, name_field, value_field) -> None:
        name = (name_field.value or "").strip()
        value = (value_field.value or "").strip()
        if not name:
            self._say("warning", "凭据要有个名字")
            return
        if not value:
            self._say("warning", "值为空：删请用「撤掉」")
            return
        try:
            outcome = keys.set_secret(name, value, target="keystore")
        except Exception as ex:  # noqa: BLE001 - 名字不合规/写不进去，都得当场说
            self._say("error", "录入失败：%s" % ex)
            return
        self._say("info", outcome.get("note") or "已录入 %s" % name)
        value_field.value = ""
        self._reconfigure()

    def _revoke(self, name: str) -> None:
        body = ft.Column(spacing=8, controls=[
            theme.markup("撤掉钥匙串里的 **`%s`**？" % name, size=theme.SIZE_DATA),
            theme.micro("环境变量里还有同名的值时它继续生效。", color=theme.DIM)])

        def do_it(_event=None) -> None:
            try:
                keys.clear_secret(name, target="keystore")
            except Exception as ex:  # noqa: BLE001
                self._say("error", "撤销失败：%s: %s" % (type(ex).__name__, ex))
                return
            self._say("info", "已从钥匙串撤掉 %s" % name)
            self._reconfigure()

        theme.overlay_scrim(self.win.page, "撤掉凭据？", body,
                            actions=[theme.btn("留下", lambda _e: self._refresh(None)),
                                     theme.primary("撤掉", do_it)], width=560)


def open_settings(win, edit=None) -> None:
    """首页顶栏「设置」与 `s` 快捷键的入口。"""
    SettingsPanel(win).open(edit)
