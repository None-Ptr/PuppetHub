"""凭据：**三层解析 + 机器级 profile**（V5，重设计）。

## 为什么重设计

旧系统只有一层：环境变量。它有一个要保住的性质——**明文密钥不进 app 目录**
（app 目录会被 git 跟踪、被融合拷贝、被打包分发）——但代价是：录入要改系统
环境变量、轮换麻烦、没有清单、没有探活、没有泄漏自检。

## 三层解析（顺序写死，且**可见**）

1. **进程环境变量**（第一优先）：CI、服务器、hub 拉起的子进程都靠它继承
2. **本机钥匙串** `$PUPPETHUB_HOME/secrets.toml`（缺省 `~/.puppethub/`）——
   **在 app 目录之外**，所以分享/融合/打包 app 时天然不带它；文件权限 0600
3. 都没有 → 报错说清"试过哪两层"，并给一条修复命令

**永远不回显任何密钥字符**——诊断与清单只报：名字、来源层次、值长度。
连掩码都不给：掩码是"少泄漏一点"，不是"不泄漏"。

## 机器级 profile（端点表）

`$PUPPETHUB_HOME/providers.toml` 定义命名的供应商：

```toml
[deepseek]
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
key_env = "DEEPSEEK_API_KEY"
```

app 的 `puppethub.toml` 只写 `[llm] profile = "deepseek"`——端点与凭据名都留在
机器上，app 因此**可以安全分享**。旧的 `[plugins.openai-compat]` 写法继续有效
（profile 只补缺省，app 里的显式键优先）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

HOME_ENV = "PUPPETHUB_HOME"
HOME_DIR = ".puppethub"
SECRETS_FILE = "secrets.toml"
PROVIDERS_FILE = "providers.toml"
ENV_FILE_ENV = "PUPPETHUB_ENV_FILE"      # 持久化落点覆盖（测试/自定义用）

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 受管块标记：只动我们写的这几行，别碰用户自己写的 rc
BLOCK_BEGIN = "# >>> puppethub >>>"
BLOCK_END = "# <<< puppethub <<<"


class SecretNameError(ValueError):
    """名字不像环境变量名——写下去也没人能读出来，所以当场拒绝。"""


def home_dir() -> Path:
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / HOME_DIR


def secrets_path() -> Path:
    return home_dir() / SECRETS_FILE


def providers_path() -> Path:
    return home_dir() / PROVIDERS_FILE


# ------------------------------------------------------------------ TOML 读写
#
# 只写"一层字符串表"——正是 secrets.toml / providers.toml 的形状。stdlib 的
# tomllib 只能读，为了写而引第三方依赖不值得（本项目零依赖是硬约束）。

def _toml_escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r"))


# TOML 的裸键只允许 A-Za-z0-9_-：凡不满足的键（例如**路径**）必须加引号，
# 否则写出来的文件自己读不回来（`C:\... = "x"` 是非法 TOML）。
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _render_key(key) -> str:
    text = str(key)
    return text if _BARE_KEY_RE.match(text) else '"%s"' % _toml_escape(text)


def _read_table(path: Path, on_error=None) -> dict:
    """读一层表。`on_error` 给了就把解析失败**交出去**——静默吞掉等于假装文件不存在。"""
    if not path.is_file():
        return {}
    import tomllib
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as ex:
        if on_error is not None:
            on_error(ex)
        return {}
    out: dict = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out[str(key)] = {str(k): v for k, v in value.items()}
    return out


def _write_table(path: Path, table: dict, header: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [header] if header else []
    for section, values in sorted(table.items()):
        if section:
            lines.append("[%s]" % section)
        for key, value in sorted(values.items()):
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (int, float)):
                rendered = str(value)
            else:
                rendered = '"%s"' % _toml_escape(str(value))
            lines.append("%s = %s" % (_render_key(key), rendered))
        lines.append("")
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    _restrict(path)


def _restrict(path: Path) -> None:
    """0600：本机用户独占。Windows 上 chmod 语义有限——退化为"只在用户目录"，
    这条边界如实写进手册，不假装做到了。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ------------------------------------------------------------------ 钥匙串

def store() -> dict:
    """钥匙串内容：`{名字: 值}`（`_read_table` 会丢掉非表结构，所以用平铺表）。"""
    data = _read_table(secrets_path())
    flat = data.get("secrets") or {}
    return {k: str(v) for k, v in flat.items()}


def store_set(name: str, value: str) -> Path:
    """写入钥匙串。名字当场校验（不像环境变量名的名字写下去也没人读得出来）。"""
    if not _NAME_RE.match(name or ""):
        raise SecretNameError("名字必须是环境变量式的（字母/下划线开头）：%r" % (name,))
    if not value:
        raise SecretNameError("值为空——要删除请用 store_delete")
    data = store()
    data[name] = value
    _write_table(secrets_path(), {"secrets": data},
                 header="# PuppetHub 钥匙串（0600）。环境变量优先级更高；\n"
                        "# 这个文件在 app 目录之外，所以不会被分享/融合/打包带上。")
    return secrets_path()


def store_delete(name: str) -> bool:
    data = store()
    if name not in data:
        return False
    data.pop(name)
    if data:
        _write_table(secrets_path(), {"secrets": data})
    else:
        try:
            secrets_path().unlink()
        except OSError:
            pass
    return True


# ------------------------------------------------------------------ 环境变量层
#
# 请求很直白："设置 api-key 时自动写进环境变量"。但这一步的代价必须写在前面：
#
# - 环境变量对**所有以你身份运行的进程**可见，还会被每个子进程继承（可能进日志、
#   进崩溃转储）；钥匙串是 0600、只有本用户可读。**安全性：钥匙串 > 环境变量。**
# - 它又是**第一优先**：env 里有值就会盖过钥匙串（`resolve` 的顺序写死）。所以
#   "往钥匙串写新钥匙、env 里躺着旧钥匙"是最容易踩的坑——写钥匙串时若发现 env
#   已有一把，必须**当面提醒**（`keys.apply_provider_settings` 会带这条 note）。
#
# 之所以还是提供它：CI、其他语言写的脚本、别的工具常只认环境变量；轮换时把新值
# 同步过去比让人手改系统设置靠谱。

def check_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise SecretNameError("名字必须是环境变量式的（字母/下划线开头）：%r" % (name,))
    return name


def env_rc_path() -> Path:
    """POSIX 上持久化环境变量的落地文件（受管块写这里）。

    Windows 不写文件（用用户级环境变量，见 `_persist_env`）；这个路径只服务
    POSIX 与文档说明。按登录 shell 选文件：zsh 读 `~/.zprofile`，bash 读
    `~/.bash_profile`/`~/.profile`——写错文件等于没写，所以按 shell 判。
    """
    override = os.environ.get(ENV_FILE_ENV)
    if override:
        return Path(override).expanduser()
    home = Path.home()
    shell = Path(os.environ.get("SHELL") or "").name
    if shell == "zsh":
        return home / ".zprofile"
    if shell == "bash":
        for candidate in (".bash_profile", ".bashrc", ".profile"):
            if (home / candidate).is_file():
                return home / candidate
        return home / ".bash_profile"
    return home / ".profile"


def render_env_block(text: str, name: str, value: Optional[str]) -> str:
    """在 rc 文本里更新/插入/删除受管块（**纯函数**，跨平台可测）。

    `value=None` = 删掉这个名字；块空了就把块一起去掉（不留空壳，与 config_edit 同一纪律）。
    """
    lines = (text or "").splitlines()
    kept: list[str] = []
    inside = False
    entries: list[tuple[str, str]] = []
    for line in lines:
        if line.strip() == BLOCK_BEGIN:
            inside = True
            continue
        if line.strip() == BLOCK_END:
            inside = False
            continue
        if inside:
            stripped = line.strip()
            if stripped.startswith("export ") and "=" in stripped:
                key = stripped[len("export "):].split("=", 1)[0].strip()
                raw = stripped.split("=", 1)[1]
                entries.append((key, raw))
            continue
        kept.append(line)
    if value is None:
        entries = [(key, raw) for key, raw in entries if key != name]
    else:
        entries = [(key, raw) for key, raw in entries if key != name]
        entries.append((name, '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')))
    while kept and not kept[-1].strip():
        kept.pop()
    if entries:
        kept.extend([BLOCK_BEGIN]
                    + ["export %s=%s" % (key, raw) for key, raw in entries]
                    + [BLOCK_END])
    return "\n".join(kept).rstrip("\n") + "\n"


def _persist_env(name: str, value: Optional[str]) -> str:
    """把环境变量**持久化给新进程**。返回值是"写到哪里、何时生效"的人话说明。

    Windows：用户级环境变量（`setx` 写 / `reg delete` 删）。**它不影响已经开着的
    进程**——包括我们自己和当前终端；所以 `set_env` 同时改本进程的 `os.environ`。
    POSIX：写登录 shell 的 rc 受管块；重开终端或 `source` 后生效。
    """
    if os.name == "nt":
        if value is None:
            subprocess.run(["reg", "delete", "HKCU\\Environment", "/v", name, "/f"],
                           capture_output=True, check=False)
            return "已删除用户环境变量 %s（新开的进程里消失）" % name
        result = subprocess.run(["setx", name, value], capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                check=False)
        if result.returncode != 0:
            raise OSError((result.stderr or result.stdout or "setx 失败").strip()[:200])
        return "已写入用户环境变量 %s（注册表，**新开的进程**可见；当前窗口已即时生效）" % name
    path = env_rc_path()
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_env_block(text, name, value), encoding="utf-8")
    _restrict(path)
    return ("已%s %s 于 %s（重开终端或 source 后对新进程生效；当前窗口已即时生效）"
            % ("删除" if value is None else "写入", name, path))


def set_env(name: str, value: str, persist: bool = True) -> dict:
    """把凭据写进**环境变量**：当前进程立即生效，并按平台持久化给新进程。

    `persist=False` 只改本进程（冒烟与临时会话用）。持久化失败时**如实上报**，
    但本进程的生效不撤回——两件事分开说。
    """
    check_name(name)
    if not value:
        raise SecretNameError("值为空——要删除请用 clear_env")
    os.environ[name] = value
    if not persist:
        return {"ok": True, "note": "只写进了当前进程的环境（未持久化）"}
    try:
        return {"ok": True, "note": _persist_env(name, value)}
    except Exception as ex:  # noqa: BLE001 - 本进程已生效，持久化失败要说清
        return {"ok": False, "note": "当前窗口已即时生效，但持久化失败：%s" % ex,
                "error": str(ex)}


def clear_env(name: str, persist: bool = True) -> dict:
    """从环境变量里撤掉一个凭据（本进程 + 持久层）。"""
    had = os.environ.pop(name, None) is not None
    if not persist:
        return {"ok": True, "had": had, "note": "只从当前进程的环境里移除"}
    try:
        return {"ok": True, "had": had, "note": _persist_env(name, None)}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "had": had, "note": "持久层删除失败：%s" % ex,
                "error": str(ex)}


def env_shadow_note(name: str, source: Optional[str]) -> str:
    """写钥匙串时的提醒：env 里已经有一把 → 它会盖过你刚写的新值。"""
    if source != "env":
        return ""
    return ("注意：%s 在**环境变量**里已有一把（env 优先级更高，会盖过你刚写进"
            "钥匙串的新值）——要新值生效，先清掉环境变量那一把。" % name)


# ------------------------------------------------------------------ 解析与清单

def resolve(name: str) -> Tuple[Optional[str], Optional[str]]:
    """返回 `(值, 来源)`。来源 ∈ `"env" / "keystore" / None`——顺序写死在这里。"""
    if not name:
        return None, None
    env_value = os.environ.get(name)
    if env_value:
        return env_value, "env"
    stored = store().get(name)
    if stored:
        return stored, "keystore"
    return None, None


def secret_info(name: str) -> dict:
    """**只报名字/来源/长度**——这个函数是本项目"永不回显密钥"的唯一出口。"""
    value, source = resolve(name)
    return {"name": name, "source": source,
            "length": len(value) if value else 0, "present": bool(value)}


def describe_line(name: str) -> str:
    info = secret_info(name)
    if not info["present"]:
        return "%-24s 未设置（env 与钥匙串都没有）" % name
    return "%-24s 来自 %-9s 长度 %d（不回显）" % (name, info["source"], info["length"])


# ------------------------------------------------------------------ 供应商 profile

def profiles() -> dict:
    return _read_table(providers_path())


def profile(name: str) -> Optional[dict]:
    table = profiles().get(str(name))
    return dict(table) if table else None


def profile_error(name: str) -> str:
    """profile 找不到时的**可执行**报错：说清去哪里写、现有哪些。"""
    known = sorted(profiles())
    return ("app.的 [llm] profile = %r 在本机 %s 里没有定义。\n"
            "  已定义：%s\n"
            "  加一段即可：\n"
            "    [%s]\n"
            "    base_url = \"https://…/v1\"\n"
            "    model = \"…\"\n"
            "    key_env = \"…_API_KEY\""
            % (name, providers_path(), "、".join(known) or "（无）", name))
