"""CLI 功能的**图形入口**：每个动作是一个**可直接调用的方法**，浮层只是它的薄壳。

为什么这样分层：GUI 里的动作必须能被冒烟驱动（`docs/smoke-gui.py` 直接调
`save_settings` / `apply_human_edit` / `export_now` …），否则"界面能用"就只剩
肉眼证据。同时——与 `cli.py` 开头那条防漂移约束同源——语义一律在 service 层
（`hub` / `fusion` / `builder` / `keys` / `humanedit` / `create_app`），这里只做
"收参数 + 排版输出"。两套入口各写一遍语义，迟早漂移。

覆盖的 CLI 面：
    new    → 新建 app          keys   → 模型设置（base_url / 模型 / 凭据 / 探活）
    edit   → 人的写入          build  → 打包（生成工程 / 连跑 flet build）
    hub    → 编排（list/up/down/status/bus）   remote → 服务化（无头服务进程）
    fuse   → 融合（先体检干跑）                  repl   → 命令批（origin=driver）

两个 UI 原语：**表单浮层**（收参数）与**输出浮层**（长任务放后台线程，行流出来）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import flet as ft

from . import keys as keymod
from . import theme
from .theme import (DIM, RED, RULE, SIZE_DATA, SIZE_MICRO, TEXT, area, btn, field,
                    labeled, mono, primary, rule)


class GuiTools:
    """驾驶舱「工具」抽屉背后的一组动作（同时也是可被冒烟直接调用的 API）。"""

    def __init__(self, session, page: ft.Page, repaint):
        self.session = session
        self.page = page
        self.repaint = repaint
        self._out: ft.Column | None = None
        self._service: dict = {}          # remote 子进程句柄

    # ============================================================ 动作（可测）

    def save_settings(self, *, scope: str, profile_name: str = "", base_url: str = "",
                      model: str = "", key_env: str = "", api_key: str = "",
                      key_target: str = "keystore") -> dict:
        """写 provider 设置（`scope="app"` 写 app 配置；`"profile"` 写机器级 profile）。

        字段值里 **`-` 表示删除该项**（空 = 不改）——「能写不能清」是缺口：
        端点写错要能撤回去，否则只能去手改文件。
        `key_target`：凭据写**钥匙串**（缺省，更安全）还是**环境变量**（持久、别的
        程序也能用，但同用户所有进程可见）。
        写完**热重载插件**：配置即时生效，不必重启窗口（凭据值不回显）。
        """
        fields, clear = split_clear({"profile": profile_name, "base_url": base_url,
                                     "model": model, "key_env": key_env})
        result = keymod.apply_provider_settings(
            self.session.app.config_path, scope=scope,
            profile_name=fields.get("profile", ""),
            base_url=fields.get("base_url", ""), model=fields.get("model", ""),
            key_env=fields.get("key_env", ""), api_key=api_key or None,
            clear=clear, key_target=key_target)
        if result["ok"]:
            self.session.reload_plugins()
        return result

    def clear_env_key(self, name: str) -> dict:
        """把某个凭据从**环境变量**撤掉（本进程 + 持久层）——env 是第一优先，
        它挡在前面时往钥匙串写新值不会生效，所以撤销这条路必须存在且可达。"""
        return keymod.clear_secret(name, target="env")

    def credential_layers(self) -> dict:
        return keymod.credential_targets()

    def provider_view(self) -> dict:
        return keymod.provider_view(self.session.app.config_path)

    def probe_now(self) -> dict:
        return keymod.probe(self.session.llm_settings())

    def create_app_now(self, parent: str, name: str, title: str = "") -> dict:
        """CLI `new`：生成骨架 + **静态校验**（骨架零诊断是它的契约）。"""
        from .appdir import create_app
        from .fusion import _dry_run
        app = create_app(parent, name, title or None)
        errors = _dry_run(app.read_source())
        return {"root": str(app.root),
                "errors": [{"level": d.level, "code": d.code, "message": d.message}
                           for d in errors]}

    def export_now(self, target: str, out: str = "", also_run: bool = False,
                   log: Callable[[str], None] = print) -> bool:
        """CLI `build`：生成独立 flet 工程（可选连跑 flet build）。"""
        from .builder import export_project, run_flet_build
        app = self.session.app
        result = export_project(app, target, Path(out) if out else None)
        if not result["ok"]:
            for error in result.get("errors") or []:
                log("拒绝：%s" % error)
            return False
        log("已生成独立 flet 工程：%s" % result["out_dir"])
        for rel in result["files"]:
            log("  · %s" % rel)
        for note in result.get("notes") or []:
            log("· %s" % note)
        log("手机上写者=无（改程序回桌面改 app.puppet 再 build）")
        if not also_run:
            log("构建：cd \"%s\" && flet build %s" % (result["out_dir"], target))
            return True
        outcome = run_flet_build(Path(result["out_dir"]), target)
        for line in (outcome.get("output") or "").splitlines()[-40:]:
            log(line)
        if not outcome["ok"]:
            log("失败：%s" % (outcome.get("error") or "flet build 失败"))
            return False
        log("构建完成（产物见 flet build 输出）")
        return True

    def hub_now(self, action: str, base_port: int = 8800,
                log: Callable[[str], None] = print) -> None:
        """CLI `hub`：编排动作（list / up / down / status / bus）。"""
        from . import hub as hubmod
        parent = self.session.app.root.parent
        log("目录：%s（base-port %d）" % (parent, base_port))
        if action == "list":
            for app in hubmod.discover(parent):
                log("  · %s" % app.name)
        elif action == "up":
            for item in hubmod.up(parent, base_port=base_port):
                log("  %s → 端口 %s（握手 %s）"
                    % (item["name"], item["port"],
                       "ok" if item["ready"] else "未就绪"))
            log("协作总线端口：%s" % hubmod._load_state(parent).get("bus_port"))
        elif action == "down":
            hubmod.down(parent)
            log("已停止（账本保留：再次 up 幂等拉起）")
        elif action == "status":
            for row in hubmod.status(parent):
                log("  %s 端口 %s 存活 %s"
                    % (row.get("name"), row.get("port"), row.get("alive")))
        elif action == "bus":
            entries = hubmod.bus_entries(parent, limit=30)
            if not entries:
                log("（总线还没有审计记录：app 之间还没说过话）")
            for entry in entries:
                if "delivery" in entry:
                    log("  %s 投递回执 %s.%s → ok=%s failed=%s"
                        % (entry.get("time"), entry.get("from"), entry.get("topic"),
                           entry["delivery"].get("ok"), entry["delivery"].get("failed")))
                else:
                    log("  %s %s [%s] %s：%s"
                        % (entry.get("time"), entry.get("from"), entry.get("topic"),
                           entry.get("title") or "-", (entry.get("text") or "")[:60]))
        else:
            log("拒绝：不认识的编排动作 %r" % action)

    def fuse_dry_now(self, b_path: str, log: Callable[[str], None] = print) -> bool:
        """CLI `fuse` 的干跑：打印 plan 摘要 + 体检结论（**不写任何东西**）。"""
        from .appdir import AppDir
        from .fusion import audit_plan, default_plan
        b = AppDir(Path(b_path))
        if not b.exists():
            log("拒绝：%s 不是 app 目录" % b_path)
            return False
        a = self.session.app
        plan = default_plan(a, b)
        plan["b"] = str(b.root)
        report = audit_plan(a, b, plan)
        log("plan：renames=%s" % (plan.get("renames") or {}))
        log("      caps=%s · window_title=%s"
            % (plan.get("caps") or {}, plan.get("window_title")))
        log("      intent=%s" % plan.get("intent"))
        log("依赖对照：A〔%s〕← B〔%s〕"
            % (report["deps"]["a"] or "未填", report["deps"]["b"] or "未填"))
        for error in report["errors"]:
            log("拒绝：%s" % error)
        log("体检：%s" % ("通过——确认后可执行" if report["ok"] else "未通过"))
        return report["ok"]

    def fuse_now(self, b_path: str, log: Callable[[str], None] = print) -> bool:
        """CLI `fuse --yes`：体检通过才执行（执行前有兜底快照，B 归档不删）。"""
        from .appdir import AppDir
        from .fusion import audit_plan, default_plan, fuse
        b = AppDir(Path(b_path))
        if not b.exists():
            log("拒绝：%s 不是 app 目录" % b_path)
            return False
        a = self.session.app
        plan = default_plan(a, b)
        plan["b"] = str(b.root)
        report = audit_plan(a, b, plan)
        if not report["ok"]:
            for error in report["errors"]:
                log("拒绝：%s" % error)
            log("体检未通过：不执行（一个字节都没写）")
            return False
        result = fuse(a, b, plan)
        if not result["ok"]:
            log("失败：停在 %s 阶段——%s"
                % (result["stage"], (result.get("errors") or ["见上"])[0]))
            return False
        log("融合完成：合并后 %d 行；B 已归档为 %s"
            % (result["merged_lines"], result["archive"]))
        log("撤销路径：操作 » 回滚 选 rebuild 那一份（执行前有兜底快照）")
        return True

    def human_edit_check(self, text: str) -> dict:
        """干跑校验 + 差异摘要（`apply` 之前给人看的东西）。"""
        from .humanedit import diff_summary, validate_text
        errors = validate_text(text)
        summary = diff_summary(self.session.app.read_source(), str(text).splitlines())
        return {"ok": not errors,
                "errors": [{"level": d.level, "code": d.code, "message": d.message}
                           for d in errors],
                "summary": summary}

    def apply_human_edit(self, text: str) -> dict:
        """CLI `edit`：整份替换（干跑 → 兜底快照 → 替换 → 重载；失败拒稿留底）。"""
        from .humanedit import apply_text
        return apply_text(self.session.app, self.session, text)

    def send_batch(self, lines: list) -> list:
        """CLI `repl`：人作驱动者的命令批（走同一条写入路径，origin=driver）。"""
        return self.session.send([line for line in lines if str(line).strip()],
                                 origin="driver")

    def service_start(self, port: int = 8765) -> dict:
        """CLI `remote`：起一个**独立的无头服务进程**（写者=驱动者）。

        用子进程而不是线程：服务与窗口是两个生命周期，一个崩不该拖死另一个；
        这一点与 CLI 完全一致。
        """
        proc = self._service.get("proc")
        if proc is not None and proc.poll() is None:
            return {"ok": False, "error": "已经在运行（先停止）", "pid": proc.pid}
        cmd = ([sys.executable, "remote", str(self.session.app.root), "--port", str(port)]
               if getattr(sys, "frozen", False) else
               [sys.executable, "-m", "puppethub", "remote", str(self.session.app.root),
                "--port", str(port)])
        child = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self._service.update({"proc": child, "port": int(port)})
        return {"ok": True, "pid": child.pid, "port": int(port)}

    def service_stop(self) -> dict:
        proc = self._service.get("proc")
        if proc is None or proc.poll() is not None:
            return {"ok": False, "error": "未运行"}
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001 - 停不下来要可见
            proc.kill()
        return {"ok": True}

    def service_state(self) -> dict:
        proc = self._service.get("proc")
        alive = bool(proc is not None and proc.poll() is None)
        return {"alive": alive, "pid": proc.pid if alive else None,
                "port": self._service.get("port")}

    # ============================================================ UI 原语

    def _log(self, line: str) -> None:
        if self._out is None:
            return
        tone = RED if line.startswith(("失败", "拒绝", "错误")) else TEXT
        self._out.controls.append(mono(line, size=SIZE_MICRO, color=tone,
                                       selectable=True))
        try:
            self.page.update()
        except Exception:  # noqa: BLE001 - 浮层被关掉而已
            pass

    def form(self, title: str, body_controls: list, actions: list,
             width: int = 720) -> None:
        theme.overlay_scrim(self.page, title,
                            ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO,
                                      controls=body_controls),
                            actions=actions, width=width)

    def run(self, title: str, body_controls: list, fn, actions=None,
            width: int = 760) -> None:
        """**后台线程**跑 `fn(log)`，输出逐行流进浮层（长任务不占事件循环）。"""
        self._out = ft.Column(spacing=1, scroll=ft.ScrollMode.AUTO, controls=[])
        content = ft.Column(expand=True, spacing=8, controls=[
            ft.Column(spacing=8, controls=body_controls),
            rule(),
            ft.Container(content=self._out, expand=True),
        ])
        theme.overlay_scrim(self.page, title, content,
                            actions=actions or [btn("关闭", lambda _e: self._close())],
                            width=width)

        def worker() -> None:
            try:
                fn(self._log)
            except Exception as ex:  # noqa: BLE001 - 失败必须可见
                self._log("失败：%s: %s" % (type(ex).__name__, ex))
            else:
                self._log("—— 完成 ——")
            self.repaint()
        self.page.run_thread(worker)

    def _close(self) -> None:
        self._out = None
        theme.close_overlay(self.page)

    # ============================================================ 浮层

    def settings(self) -> None:
        """模型设置：base_url / 模型 / 凭据名 / 凭据值（落点可选）+ 探活。"""
        view = self.provider_view()
        eff = view["effective"]
        scope = {"value": "profile" if view["profile"] else "app"}
        key_target = {"value": "keystore"}
        profile_field = field(view["profile"], hint="profile 名（例如 deepseek）")
        base_field = field(eff.get("base_url") or "", hint="https://api.deepseek.com/v1")
        model_field = field(eff.get("model") or "", hint="deepseek-chat")
        env_field = field(eff.get("key_env") or "OPENAI_API_KEY", hint="DEEPSEEK_API_KEY")
        key_field = field("", hint="粘贴凭据（不回显）；留空 = 不改", password=True)
        status = mono("", size=SIZE_MICRO, color=DIM, selectable=False)

        def refresh_status() -> None:
            """现状一律**现读**（保存后立刻反映，不留旧副本）。"""
            live = self.provider_view()
            current = live["effective"]
            layers = self.credential_layers()
            lines = []
            for name in self.session.credential_names():
                info = layers.get(name) or {}
                marks = []
                if info.get("keystore"):
                    marks.append("钥匙串 ✓")
                if info.get("env"):
                    marks.append("环境变量 ✓（第一优先）")
                if not marks:
                    marks.append("未设置")
                lines.append("%-24s %s%s" % (name, " · ".join(marks),
                                             "  长度 %d" % info["length"]
                                             if info.get("length") else ""))
            lines.append("生效：%s · %s · %s"
                         % (current.get("base_url") or "（未设）",
                            current.get("model") or "（未设）", current.get("key_env")))
            if live["profile"]:
                lines.append("profile：%s（%s）"
                             % (live["profile"],
                                "已定义" if live["profile_found"] else "**未定义**"))
            lines.append("本机 profile 表：%s"
                         % ("、".join(live["profiles"]) or "（空）"))
            if any(info.get("env") for info in layers.values()):
                lines.append("⚠ 环境变量里已有凭据：它优先级更高，会盖过钥匙串里的同名值"
                             "（「清掉环境变量那把」可撤）")
            status.value = "\n".join(lines)
            self.page.update()

        def choose(target: str):
            def handler(_event=None) -> None:
                scope["value"] = target
                for key, button in scope_buttons.items():
                    active = key == target
                    button.content.value = "[%s]" % _SCOPE_LABEL[key] if active \
                        else _SCOPE_LABEL[key]
                    button.content.color = TEXT if active else DIM
                profile_row.visible = target == "profile"   # 整行藏，不留空标签
                self.page.update()
            return handler

        def choose_key_target(target: str):
            def handler(_event=None) -> None:
                key_target["value"] = target
                for key, button in key_buttons.items():
                    active = key == target
                    button.content.value = "[%s]" % _KEY_TARGET_LABEL[key] if active \
                        else _KEY_TARGET_LABEL[key]
                    button.content.color = TEXT if active else DIM
                key_hint.value = _KEY_TARGET_HINT[target]
                self.page.update()
            return handler

        scope_buttons = {
            "app": btn(_SCOPE_LABEL["app"], choose("app"),
                       tone=TEXT if scope["value"] == "app" else DIM),
            "profile": btn(_SCOPE_LABEL["profile"], choose("profile"),
                           tone=TEXT if scope["value"] == "profile" else DIM),
        }
        for key, button in scope_buttons.items():
            if key == scope["value"]:
                button.content.value = "[%s]" % _SCOPE_LABEL[key]
        key_buttons = {
            name: btn("[%s]" % _KEY_TARGET_LABEL[name] if name == "keystore"
                      else _KEY_TARGET_LABEL[name], choose_key_target(name),
                      tone=TEXT if name == "keystore" else DIM)
            for name in ("keystore", "env")
        }
        key_hint = mono(_KEY_TARGET_HINT["keystore"], size=SIZE_MICRO, color=DIM,
                        selectable=False, md=True)
        profile_row = labeled("profile", profile_field)
        profile_row.visible = scope["value"] == "profile"
        refresh_status()

        def save(_event=None) -> None:
            result = self.save_settings(
                scope=scope["value"], profile_name=(profile_field.value or "").strip(),
                base_url=(base_field.value or "").strip(),
                model=(model_field.value or "").strip(),
                key_env=(env_field.value or "").strip(),
                api_key=(key_field.value or "").strip(),
                key_target=key_target["value"])
            if not result["ok"]:
                self._log("拒绝：%s" % result.get("error"))
                return
            key_field.value = ""
            for path in result["wrote"]:
                self._log("已写：%s" % path)
            for note in result["notes"]:
                for line in str(note).splitlines():
                    self._log("· " + line)
            self._log("插件已热重载：配置即时生效（凭据值不回显）")
            refresh_status()
            self.repaint()

        def clear_env(_event=None) -> None:
            names = [name for name, info in self.credential_layers().items()
                     if info.get("env")]
            if not names:
                self._log("环境变量里没有本 app 的凭据，无需清理")
                return
            for name in names:
                result = self.clear_env_key(name)
                self._log("%s：%s" % (name, result["note"]))
            refresh_status()
            self.repaint()

        def probe(_event=None) -> None:
            result = self.probe_now()
            self.run("探活", [], lambda log: log(result["message"]), width=640)

        self.form("模型设置（base_url / 模型 / 凭据）", [
            # 文案一律**短行**：一屏里挤成一段话就没人读（实测反馈："文字有点密集"）。
            mono("凭据值永不写进配置文件——只写端点与变量名，值落进选定的一层。",
                 size=SIZE_MICRO, color=DIM, selectable=False),
            ft.Row(spacing=10, controls=[
                mono("端点作用域", size=SIZE_DATA, color=DIM, selectable=False, width=84),
                scope_buttons["app"], scope_buttons["profile"]]),
            mono("· 本机 profile：端点留本机，app 只留名字 —— **app 可安全分享**",
                 size=SIZE_MICRO, color=DIM, selectable=False, md=True),
            mono("· 仅本 app：端点写进 app 配置 —— 分享会带上端点",
                 size=SIZE_MICRO, color=DIM, selectable=False),
            profile_row,
            mono("字段：留空 = 不改 · 填 `-` = 清空（回到上一层或默认值）",
                 size=SIZE_MICRO, color=DIM, selectable=False, md=True),
            labeled("base_url", base_field),
            labeled("model", model_field),
            labeled("key_env", env_field),
            ft.Row(spacing=10, controls=[mono("凭据落点", size=SIZE_DATA, color=DIM,
                                             selectable=False, width=84),
                                         key_buttons["keystore"], key_buttons["env"]]),
            key_hint,
            labeled("凭据", key_field),
            rule(),
            status,
        ], actions=[primary("保存", save), btn("探活", probe),
                     btn("清掉环境变量那把", clear_env),
                     btn("关闭", lambda _e: self._close())])

    def new_app(self) -> None:
        parent_field = field(str(self.session.app.root.parent), hint="父目录")
        name_field = field("", hint="app 目录名（英文/短横线）")
        title_field = field("", hint="窗口标题（缺省同目录名）")

        def create(_event=None) -> None:
            parent = (parent_field.value or "").strip()
            name = (name_field.value or "").strip()
            if not parent or not name:
                self._log("拒绝：父目录与名字都要填")
                return

            def job(log):
                result = self.create_app_now(parent, name,
                                             (title_field.value or "").strip())
                log("已创建：%s" % result["root"])
                log("骨架静态校验：%s" % ("零诊断" if not result["errors"]
                                        else "%d 项错误" % len(result["errors"])))
                for err in result["errors"][:6]:
                    log("  %s %s: %s" % (err["level"], err["code"], err["message"]))
                log("下一步：puppethub run \"%s\"（新 app 要新开一个窗口进程）"
                    % result["root"])
            self.run("新建 app", [
                labeled("父目录", parent_field),
                labeled("名字", name_field),
                labeled("标题", title_field),
            ], job)
        self.form("新建 app（CLI `new`）", [
            mono("骨架 = window + navbar + 内容容器，不含业务——意图写进 DESIGN.md。",
                 size=SIZE_MICRO, color=DIM, selectable=False),
            labeled("父目录", parent_field),
            labeled("名字", name_field),
            labeled("标题", title_field),
        ], actions=[primary("创建", create), btn("关闭", lambda _e: self._close())])

    def human_edit(self) -> None:
        """人的写入：整份替换真源（干跑 → 确认 → 兜底快照 → 替换 → 重载）。"""
        from .humanedit import rejected_path
        app = self.session.app
        text_area = area("\n".join(app.read_source()), lines=16)
        info = mono("", size=SIZE_MICRO, color=DIM, selectable=False)
        rejected = rejected_path(app)
        if rejected:
            info.value = ("上一份被拒的编辑还在 %s（没通过静态校验；真源已恢复原样）"
                          % rejected.name)

        def check(_event=None) -> None:
            result = self.human_edit_check(text_area.value or "")
            if not result["ok"]:
                info.value = "干跑拒绝（%d 项）——应用会被拦下并把拒稿留底：" % len(result["errors"])
                for err in result["errors"][:6]:
                    info.value += "\n  %s %s: %s" % (err["level"], err["code"],
                                                     err["message"])
            else:
                summary = result["summary"]
                info.value = ("干跑通过：%d 行 → %d 行；新增 %d 行 / 删除 %d 行"
                              % (summary["before_lines"], summary["after_lines"],
                                 len(summary["added"]), len(summary["removed"])))
            self.page.update()

        def apply(_event=None) -> None:
            result = self.apply_human_edit(text_area.value or "")
            if not result["ok"]:
                self._log("拒绝：干跑未通过（真源已恢复原样）")
                for err in result["errors"][:6]:
                    self._log("  %s %s: %s" % (err["level"], err["code"], err["message"]))
                self._log("拒稿已留底：%s" % Path(result["rejected"]).name)
            else:
                self._log("已整份替换并重载（写前有兜底快照 rebuild，可回滚）")
            self.repaint()

        self.form("人的写入（CLI `edit`）", [
            mono("写者=你（origin=driver）：干跑 → 兜底快照 → 整份替换 → 重载。",
                 size=SIZE_MICRO, color=DIM, selectable=False),
            ft.Container(content=text_area, border=ft.Border.all(1, RULE),
                         padding=6, expand=True),
            info,
        ], actions=[primary("应用", apply), btn("校验（干跑）", check),
                     btn("关闭", lambda _e: self._close())])

    def build(self) -> None:
        target = {"value": "android"}
        out_field = field("", hint="输出目录（留空 = <app>/build/<target>）")
        buttons = {}

        def choose(name: str):
            def handler(_event=None) -> None:
                target["value"] = name
                for key, button in buttons.items():
                    button.content.value = "[%s]" % key if key == name else key
                    button.content.color = TEXT if key == name else DIM
                self.page.update()
            return handler

        for name in ("android", "web"):
            buttons[name] = btn("[%s]" % name if name == "android" else name,
                                choose(name), tone=TEXT if name == "android" else DIM)

        def do(also_run: bool):
            def handler(_event=None) -> None:
                self.run("打包 · %s" % target["value"], [
                    ft.Row(spacing=10, controls=[buttons["android"], buttons["web"]]),
                    labeled("输出", out_field),
                ], lambda log: self.export_now(target["value"],
                                               (out_field.value or "").strip(),
                                               also_run, log))
            return handler

        self.form("设备打包（CLI `build`）", [
            mono("生成独立 flet 工程（纯 app 实例、写者=无、pointer=touch）。"
                 "「生成」可本机验证；「生成并构建」需要 flet CLI + Android SDK。",
                 size=SIZE_MICRO, color=DIM, selectable=False),
            ft.Row(spacing=10, controls=[buttons["android"], buttons["web"]]),
            labeled("输出", out_field),
        ], actions=[primary("生成", do(False)), btn("生成并构建", do(True)),
                    btn("关闭", lambda _e: self._close())])

    def hub_tools(self) -> None:
        parent = self.session.app.root.parent
        base_field = field("8800", hint="起始端口")

        def action(name: str):
            def handler(_event=None) -> None:
                try:
                    base = int((base_field.value or "8800").strip())
                except ValueError:
                    self._log("拒绝：端口要是整数")
                    return
                self.run("编排 · %s" % name, [labeled("起始端口", base_field)],
                         lambda log: self.hub_now(name, base, log))
            return handler

        self.form("多 app 编排（CLI `hub`）", [
            mono("目录：%s（本 app 的父目录）" % parent, size=SIZE_MICRO,
                 color=DIM, selectable=False),
            labeled("起始端口", base_field),
        ], actions=[btn("list", action("list")), primary("up", action("up")),
                    btn("down", action("down")), btn("status", action("status")),
                    btn("bus", action("bus")), btn("关闭", lambda _e: self._close())])

    def service(self) -> None:
        port_field = field("8765", hint="监听端口")
        state = mono("", size=SIZE_MICRO, color=DIM, selectable=False)

        def refresh() -> None:
            info = self.service_state()
            state.value = ("运行中：127.0.0.1:%s（pid %s）"
                           % (info["port"], info["pid"]) if info["alive"] else "未运行")
            self.page.update()

        def start(_event=None) -> None:
            try:
                port = int((port_field.value or "8765").strip())
            except ValueError:
                self._log("拒绝：端口要是整数")
                return
            result = self.service_start(port)
            if not result["ok"]:
                self._log("拒绝：%s" % result["error"])
            else:
                self._log("已启动无头服务进程 pid=%d → 127.0.0.1:%d"
                          % (result["pid"], result["port"]))
                self._log("协议与窗口内一致；写者=驱动者（外部程序即写者）")
            refresh()

        def stop(_event=None) -> None:
            result = self.service_stop()
            self._log("已停止" if result["ok"] else "未运行，无需停止")
            refresh()

        refresh()
        self.form("服务化（CLI `remote`）", [
            mono("把本 app 暴露成可编程接口：外部程序/agent 用同一套协议操作驱动它"
                 "（写者=驱动者，单写者仍成立）。", size=SIZE_MICRO, color=DIM,
                 selectable=False),
            labeled("端口", port_field),
            state,
        ], actions=[primary("启动", start), btn("停止", stop),
                     btn("刷新", lambda _e: refresh()), btn("关闭", lambda _e: self._close())])

    def fuse(self) -> None:
        b_field = field("", hint="被并方 app 目录（B）")
        self.form("融合（CLI `fuse`）", [
            mono("**推倒重来级**动作：执行前有兜底快照（rebuild，不参与淘汰）",
                 size=SIZE_MICRO, color=DIM, selectable=False, md=True),
            mono("B 目录归档改名、不删除 · 先跑「体检（干跑）」再执行",
                 size=SIZE_MICRO, color=DIM, selectable=False),
            labeled("B 目录", b_field),
        ], actions=[
            btn("体检（干跑）", lambda _e: self.run(
                "融合体检（干跑）", [labeled("B 目录", b_field)],
                lambda log: self.fuse_dry_now((b_field.value or "").strip(), log))),
            primary("执行融合", lambda _e: self.run(
                "执行融合（B 并入 A）", [labeled("B 目录", b_field)],
                lambda log: self.fuse_now((b_field.value or "").strip(), log))),
            btn("关闭", lambda _e: self._close()),
        ])

    def command_batch(self) -> None:
        """人作驱动者的命令批（CLI `repl` 的图形形态）。"""
        input_area = area("", lines=6, hint="一行一条命令；整块作为一批提交")

        def send(_event=None) -> None:
            lines = [line for line in (input_area.value or "").splitlines()
                     if line.strip()]
            if not lines:
                self._log("拒绝：没有内容")
                return
            diags = self.send_batch(lines)
            errors = 0
            for diag in diags:
                level = getattr(diag, "level", "信息")
                self._log("%s %s: %s" % (level, getattr(diag, "code", "?"),
                                         getattr(diag, "message", diag)))
                errors += 1 if level == "错误" else 0
            if not diags:
                self._log("零诊断（命令批已写入真源，批末自动快照）")
            elif not errors:
                self._log("（有提示但无错误；真源已写回）")
            input_area.value = ""
            self.repaint()

        self.form("命令批（CLI `repl`）", [
            mono("写者=你（origin=driver）：引擎校验 → 批末写回真源 → 写前自动快照。",
                 size=SIZE_MICRO, color=DIM, selectable=False),
            ft.Container(content=input_area, border=ft.Border.all(1, RULE), padding=6),
        ], actions=[primary("发送", send), btn("关闭", lambda _e: self._close())])


_SCOPE_LABEL = {"app": "仅本 app", "profile": "本机 profile"}
_KEY_TARGET_LABEL = {"keystore": "钥匙串", "env": "环境变量"}
_KEY_TARGET_HINT = {
    "keystore": "钥匙串：0600、app 目录之外，只本用户可读 —— **更安全（推荐）**",
    "env": "环境变量：持久（新进程也能用）· **同用户每个进程可见** · "
           "第一优先，会盖过钥匙串同名值",
}
CLEAR = "-"                  # 字段填 `-` = 删除该项（空 = 不改）


def split_clear(values: dict) -> tuple:
    """把表单值分成「要写的」与「要删的」：`-` 是删除哨兵，空串是不改。

    哨兵只能是一个孤零零的 `-`——`-` 开头的正常值（比如负号）不会被误判，
    因为它们不是"恰好等于"。
    """
    fields, clear = {}, []
    for key, raw in values.items():
        text = str(raw or "").strip()
        if text == CLEAR:
            clear.append(key)
        elif text:
            fields[key] = text
    return fields, clear
