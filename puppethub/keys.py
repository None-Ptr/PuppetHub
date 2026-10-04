"""凭据操作：清单 / 录入 / **探活** / **泄漏自检**。

CLI（`puppethub keys …`）与驾驶舱（「操作」抽屉里的 `密钥` / `探活`）共用这一份
实现——两处各写一遍语义，迟早漂移（这条纪律在 CLI 那一层就立过）。

**探活为什么值钱**：401（密钥错）、403（无权限）、404（端点错，常见于 base_url
少了 `/v1`）、429（额度/频率）、超时、连不上——这六件事的修法完全不同。没有探活，
它们全都只在一次真实对话里撞出来，代价是一整轮上下文 + 一次误导诊断。

**泄漏自检为什么值钱**："密钥不进 app 目录"是设计承诺，但承诺需要机制去验证：
这里真的去 app 内的文本文件里找明文。**命中只报文件名与次数，绝不回显密钥**。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from . import secrets

# 全量扫的小文件（人读的、体量可控）
SMALL_FILES = ("app.puppet", "DESIGN.md", "capabilities.py",
               ".puppethub/goal.json", ".puppethub/approvals.json")
# 只扫尾部的过程账（可能几十 MB）
JSONL_FILES = (".puppethub/chat.jsonl", ".puppethub/autonomous.jsonl")
TAIL_LINES = 200
SNAPSHOT_TAIL = 5          # 最近几份快照也扫（快照里有 app.puppet 的副本）
MIN_SECRET_LEN = 8         # 太短的"值"匹配起来全是噪声，不扫


# ------------------------------------------------------------------ 清单

def list_lines(names=None) -> list:
    """清单：名字 / 来源 / 长度。**不回显密钥**，连掩码都不给。"""
    if names is None:
        names = sorted(set(secrets.store()) | set(_profile_key_envs()))
    return [secrets.describe_line(name) for name in names]


def _profile_key_envs() -> list:
    out = []
    for table in secrets.profiles().values():
        name = table.get("key_env")
        if name:
            out.append(str(name))
    return out


def set_secret(name: str, value: str, target: str = "keystore") -> dict:
    """录入凭据。`target="keystore"`（缺省，0600、app 目录之外）或 `"env"`（环境变量）。

    **缺省仍是钥匙串**：环境变量对同用户的每个进程可见、还会被所有子进程继承，
    严格更差；要写它得**明确选**。写进去之后 env 又是第一优先，所以往钥匙串写新值
    时会检查 env 里是否已有一把并当面提醒（`env_shadow_note`）。
    返回值：`{"target", "path"?, "note"}`——两处入口（CLI / 驾驶舱）共用这一份。
    """
    if target == "env":
        result = secrets.set_env(name, value)
        note = result.get("note") or ""
        if result.get("ok"):
            note += "（%s）" % _env_shadow_hint(name)
        return {"target": "env", "note": note, "ok": bool(result.get("ok")),
                "error": result.get("error")}
    path = secrets.store_set(name, value)
    note = "已写入钥匙串 %s（0600；app 目录之外——分享 app 不会带上它）" % path.name
    shadow = secrets.env_shadow_note(name, secrets.resolve(name)[1])
    if shadow:
        note += "\n" + shadow
    return {"target": "keystore", "path": path, "note": note, "ok": True, "error": None}


def _env_shadow_hint(name: str) -> str:
    return ("环境变量对以你身份运行的**每个进程**可见，也会被所有子进程继承"
            "（比钥匙串差）；它是第一优先，会盖过钥匙串里的同名值")


def clear_secret(name: str, target: str = "env") -> dict:
    """撤掉凭据：`target="env"` 清环境变量（本进程 + 持久层），`"keystore"` 删钥匙串。"""
    if target == "keystore":
        return {"target": "keystore", "ok": secrets.store_delete(name),
                "note": "已从钥匙串删除 %s" % name}
    result = secrets.clear_env(name)
    return {"target": "env", "ok": bool(result.get("ok")),
            "note": result.get("note") or "", "error": result.get("error")}


def delete_secret(name: str) -> bool:
    """删钥匙串里的凭据（`clear_secret(target="keystore")` 的布尔旧名）。

    保留而不是改名删掉：它是既有调用面（冒烟与脚本按布尔语义用它），
    **改名删掉别人的调用点**是"静默断链"的一种——正是本项目要防的事。
    """
    return secrets.store_delete(name)


def credential_targets(names=None) -> dict:
    """每个凭据名字在**哪一层**存在（GUI 设置面板用来显示"钥匙串/环境变量"状态）。"""
    out = {}
    for name in (names if names is not None
                 else sorted(set(secrets.store()) | set(_profile_key_envs())
                             | {k for k in os.environ if k.endswith("_API_KEY")})):
        value, source = secrets.resolve(name)
        out[name] = {"source": source, "keystore": name in secrets.store(),
                     "env": bool(os.environ.get(name)),
                     "length": len(value) if value else 0}
    return out


# ------------------------------------------------------------------ 探活

def _request(url: str, key: Optional[str], timeout: float,
             method: str = "GET", payload: Optional[dict] = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4000).decode("utf-8", "replace")
            return {"status": response.status, "body": body}
    except urllib.error.HTTPError as ex:
        try:
            body = ex.read(4000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return {"status": ex.code, "body": body, "reason": str(ex.reason)}
    except urllib.error.URLError as ex:
        return {"status": None, "error": "network", "reason": str(ex.reason)}
    except TimeoutError:
        return {"status": None, "error": "timeout"}
    except Exception as ex:  # noqa: BLE001 - 失败必须可见
        return {"status": None, "error": "network",
                "reason": "%s: %s" % (type(ex).__name__, ex)}


def probe(settings: dict, timeout: float = 20.0) -> dict:
    """一次最小请求，把六种失败分开说清。

    `settings` = `{base_url, model, key_env, key?}`（app 的 llm_settings() 直接给）。
    先试 `GET /models`（多数兼容服务免费提供）；端点不支持再退化为 1-token 对话。
    """
    base = str(settings.get("base_url") or "").rstrip("/")
    model = str(settings.get("model") or "")
    key = settings.get("key")
    key_env = str(settings.get("key_env") or "")
    if not base:
        return {"ok": False, "kind": "config", "message": "没有 base_url：在 provider 配置里给一个端点"}
    if key is None:
        key, source = secrets.resolve(key_env)
    else:
        source = "settings"
    if not key:
        return {"ok": False, "kind": "no_key",
                "message": ("凭据 %s 没找到：env 与钥匙串（%s）两层都试过了。\n"
                            "  录入：puppethub keys set %s\n"
                            "  或设置环境变量后重启（env 优先级更高）"
                            % (key_env or "（未命名）", secrets.secrets_path(),
                               key_env or "<名字>"))}

    first = _request(base + "/models", key, timeout)
    kind = _classify(first, model)
    if kind == "endpoint":
        # /models 不是所有兼容实现都有：退化为最小对话
        if not model:
            return {"ok": False, "kind": "config",
                    "message": "端点无 /models 且没有配置 model，无法探活"}
        second = _request(base + "/chat/completions", key, timeout, method="POST",
                          payload={"model": model, "stream": False, "max_tokens": 1,
                                   "messages": [{"role": "user", "content": "ping"}]})
        kind = _classify(second, model)
        first = second
    return _verdict(kind, first, base, model, key_env, key)


def _classify(result: dict, model: str) -> str:
    status = result.get("status")
    if result.get("error") == "timeout":
        return "timeout"
    if result.get("error") == "network":
        return "network"
    if status == 200:
        return "ok"
    if status == 401:
        return "auth"
    if status == 403:
        return "forbidden"
    if status in (404, 405):
        return "endpoint"
    if status == 429:
        return "rate_limit"
    if status == 400 and model and "model" in (result.get("body") or "").lower():
        return "model"
    return "http"


def _verdict(kind: str, result: dict, base: str, model: str,
             key_env: str, key: str) -> dict:
    body = (result.get("body") or "").strip().replace("\n", " ")[:160]
    wd = "（%s，长度 %d，来源 %s）" % (key_env or "凭据", len(key),
                                     "env" if os.environ.get(key_env or "") else "keystore")
    messages = {
        "ok": "端点与密钥都可用：%s（模型 %s）" % (base, model or "（未声明）"),
        "auth": "**密钥被拒（401）**%s——换一把或检查是否贴错行了" % wd,
        "forbidden": "密钥被接受但**无权限（403）**%s——账号没开通这个模型/端点" % wd,
        "endpoint": ("**端点不存在（404）**：%s——常见的 base_url 少了 `/v1`，"
                     "或路径不对" % base),
        "rate_limit": "**额度或频率限制（429）**%s——不是密钥错" % wd,
        "model": "模型名 %r 不被接受（400）：%s" % (model, body or "（无响应体）"),
        "timeout": "**超时**：%s 在限时内没有响应（网络或服务端慢）" % base,
        "network": "**连不上**：%s —— %s" % (base, result.get("reason") or ""),
        "http": "服务端返回 %s：%s" % (result.get("status"), body or "（无响应体）"),
        "config": "配置不完整",
    }
    return {"ok": kind == "ok", "kind": kind,
            "message": messages.get(kind, "未知结果：%s" % result),
            "status": result.get("status")}


# ------------------------------------------------------------------ 泄漏自检

def leak_scan(app_dir, names=None, tail_lines: int = TAIL_LINES) -> dict:
    """在 app 内的文本文件里找**密钥明文**。命中只报文件名与次数。

    诚实边界：过程账只扫最近 `tail_lines` 行（可能几十 MB）；快照只扫最近
    `SNAPSHOT_TAIL` 份。报告里如实写清扫了什么、没扫什么。
    """
    root = Path(app_dir)
    if names is None:
        names = sorted(set(secrets.store()) | set(_profile_key_envs()))
    wanted = []
    for name in names:
        value, _ = secrets.resolve(name)
        if value and len(value) >= MIN_SECRET_LEN:
            wanted.append((name, value))
    hits, scanned, skipped = [], [], []
    if not wanted:
        return {"hits": hits, "scanned": scanned, "skipped": skipped,
                "note": "没有可扫的密钥值（未设置或都太短）"}

    def scan_text(rel: str, text: str, tail_only: bool) -> None:
        if tail_only:
            text = "\n".join(text.splitlines()[-tail_lines:])
        for name, value in wanted:
            count = text.count(value)
            if count:
                hits.append({"file": rel, "name": name, "count": count})

    for rel in SMALL_FILES:
        path = root / rel
        if path.is_file():
            try:
                scan_text(rel, path.read_text(encoding="utf-8", errors="replace"), False)
                scanned.append(rel)
            except OSError:
                skipped.append(rel)
    for rel in JSONL_FILES:
        path = root / rel
        if path.is_file():
            try:
                scan_text(rel + "（尾 %d 行）" % tail_lines,
                          path.read_text(encoding="utf-8", errors="replace"), True)
                scanned.append(rel)
            except OSError:
                skipped.append(rel)
    snaps = sorted((root / ".puppet" / "snapshots").glob("*"), reverse=True)[:SNAPSHOT_TAIL]
    for snap in snaps:
        for name in ("app.puppet", "capabilities.py", "meta.json"):
            path = snap / name
            if path.is_file():
                rel = ".puppet/snapshots/%s/%s" % (snap.name, name)
                try:
                    scan_text(rel, path.read_text(encoding="utf-8", errors="replace"), False)
                    scanned.append(rel)
                except OSError:
                    skipped.append(rel)
    return {"hits": hits, "scanned": scanned, "skipped": skipped,
            "note": "小文件全量扫；过程账只扫最近 %d 行；快照最近 %d 份"
                    % (tail_lines, SNAPSHOT_TAIL)}


def format_leak(result: dict) -> str:
    lines = ["泄漏自检：%s" % result.get("note", "")]
    if result.get("hits"):
        lines.append("  !! 命中 %d 处（只报位置，不回显内容）：" % len(result["hits"]))
        for hit in result["hits"]:
            lines.append("     %s ← %s（%d 次）" % (hit["file"], hit["name"], hit["count"]))
        lines.append("  处置：换一把密钥（旧的在文件里已经泄漏），再清掉那几行内容。")
    else:
        lines.append("  ok 未在 app 内的文本文件里发现密钥明文")
    if result.get("skipped"):
        lines.append("  （跳过：%s）" % "、".join(result["skipped"][:5]))
    return "\n".join(lines)


# ------------------------------------------------------------------ 组合

def provider_view(app_dir, app_config_path=None) -> dict:
    """当前**生效**的 provider 设置与来源（GUI 设置面板显示用，全部派生）。

    两个写入目标在界面上必须说清区别：`app` = 端点随 app 走；`profile` = 端点
    留在本机、app 只留名字（推荐，app 因此可安全分享）。
    """
    from .config_edit import read_options
    options = read_options(app_config_path, "plugins.openai-compat") if app_config_path else {}
    llm = read_options(app_config_path, "llm") if app_config_path else {}
    name = str(llm.get("profile") or "")
    table = secrets.profile(name) if name else None
    effective = {"base_url": options.get("base_url") or (table or {}).get("base_url"),
                 "model": options.get("model") or (table or {}).get("model"),
                 "key_env": options.get("key_env") or (table or {}).get("key_env")
                 or "OPENAI_API_KEY"}
    return {"profile": name, "profile_found": bool(table) if name else None,
            "app_options": options, "profile_options": dict(table or {}),
            "effective": effective,
            "profiles": sorted(secrets.profiles()), "home": str(secrets.home_dir())}


def apply_provider_settings(app_config_path, scope: str = "profile",
                            profile_name: str = "", base_url: str = "",
                            model: str = "", key_env: str = "",
                            api_key: Optional[str] = None,
                            clear: Optional[list] = None,
                            key_target: str = "keystore") -> dict:
    """写 provider 设置（GUI 设置面板 / CLI 共用**同一份实现**）。

    `scope="app"` → 写 app 的 `[plugins.openai-compat]`（端点随 app 走，分享会带上）；
    `scope="profile"` → 写机器级 `providers.toml` 的 `[名字]`，并把 app 里
    `[llm] profile` 指过去（端点留在本机）。

    三条写入语义，都说清楚：
    - **非空 = 写入**（空值一律视为"不改"——表单里留空不该悄悄抹掉配置）；
    - **`clear=["base_url", …]` = 删除该项**（GUI 里是字段填 `-`）；
    - `api_key` 给值时按 `key_target` 落盘：`"keystore"`（缺省）或 `"env"`
      （环境变量；**永不写进任何配置文件**）。

    返回值里带 `wrote` / `notes`，一步都不静默。
    """
    from .config_edit import set_options
    wrote, notes = [], []
    fields = {"base_url": base_url, "model": model, "key_env": key_env}
    fields = {key: value for key, value in fields.items() if value}
    for key in list(clear or []):
        fields[key] = None                       # 显式删除：回到上一层/默认值
    if scope == "app":
        set_options(Path(app_config_path), "plugins.openai-compat", fields)
        wrote.append(str(app_config_path))
        # 留着 `[llm] profile` 会让人以为端点在 profile 里：选"仅本 app"就把它撤掉。
        set_options(Path(app_config_path), "llm", {"profile": None})
        notes.append("端点写进了 app 配置：这份 app 分享给别人时会带上端点"
                     "（凭据仍是变量名，不会带上）")
    else:
        if not profile_name:
            return {"ok": False, "wrote": [], "notes": [],
                    "error": "profile 模式需要一个名字（例如 deepseek）"}
        set_options(secrets.providers_path(), profile_name, fields)
        wrote.append(str(secrets.providers_path()))
        set_options(Path(app_config_path), "llm", {"profile": profile_name})
        wrote.append(str(app_config_path))
        notes.append("app 里只留了 profile 名（%s）：端点与凭据名留在本机 %s"
                     % (profile_name, secrets.providers_path()))
    for key in (clear or []):
        notes.append("已删除 %s（回到上一层或默认值）" % key)
    if api_key:
        name = key_env or str((provider_view(app_config_path)["effective"] or {}).get("key_env")
                              or "OPENAI_API_KEY")
        outcome = set_secret(name, api_key, target=key_target)
        wrote.append(str(outcome.get("path") or ("env:%s" % name)))
        notes.append("凭据 %s：%s" % (name, outcome["note"]))
    return {"ok": True, "wrote": wrote, "notes": notes, "error": None}


def check(app_dir, names=None, settings=None, timeout: float = 20.0) -> dict:
    """探活 + 泄漏自检（`puppethub keys check` 与驾驶舱 `探活` 的一体化动作）。"""
    result = {"probe": None, "leak": leak_scan(app_dir, names=names),
              "text": ""}
    if settings:
        result["probe"] = probe(settings, timeout=timeout)
    lines = []
    if result["probe"]:
        lines.append("探活：%s" % ("ok " if result["probe"]["ok"] else "失败 ")
                     + result["probe"]["message"])
    lines.append(format_leak(result["leak"]))
    result["text"] = "\n".join(lines)
    result["ok"] = (result["probe"] or {}).get("ok", True) and not result["leak"]["hits"]
    return result
