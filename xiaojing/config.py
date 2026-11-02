"""读取和写回 config.toml。密钥不写进这里：环境变量优先，其次是 data/secrets.bin 里加密存的。"""
from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import secrets


def _root() -> Path:
    """项目文件夹：config.toml、人设、data/ 都在这。

    打包成 exe 后：exe 旁边有 config.toml 就用 exe 所在的文件夹（自己打包、放在项目里的情况）；
    没有就用 %APPDATA%\\xiaojing（从 Release 下载、随便放哪的情况），不往桌面上乱撒文件。
    """
    if override := os.environ.get("XIAOJING_ROOT"):
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "config.toml").exists():
            return exe_dir
        return Path(os.environ.get("APPDATA", exe_dir)) / "xiaojing"
    return Path(__file__).resolve().parent.parent


ROOT = _root()


@dataclass
class OneBotConfig:
    api_url: str = "http://127.0.0.1:3020"  # OneBot（NapCat / SnowLuma）的 HTTP 服务器
    listen_host: str = "127.0.0.1"           # OneBot 的 HTTP 上报发到这里
    listen_port: int = 5311
    token: str = ""   # ONEBOT_TOKEN：调 OneBot 时带上
    secret: str = ""  # ONEBOT_SECRET：校验上报的签名


@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key: str = ""  # DEEPSEEK_API_KEY
    timeout: float = 90
    max_steps: int = 8  # 一圈里最多连着调几次模型
    # 每百万 token 的价格（元），闲时价；高峰乘 peak_multiplier
    price_hit: float = 0.02
    price_miss: float = 1.0
    price_out: float = 4.0
    peak_multiplier: float = 2.0


@dataclass
class GateConfig:
    owner_qq: str = ""       # 主人的 QQ 号：只有这个号私聊发「暂停」「继续」才算数，夜里也只有这个号叫得醒她
    owner_name: str = "主人"  # 她怎么称呼主人，会写进提示词
    allow_groups: list[str] = field(default_factory=list)
    allow_users: list[str] = field(default_factory=list)
    per_conv_per_minute: int = 4
    global_per_minute: int = 10
    max_text_len: int = 500


@dataclass
class ConsoleConfig:
    host: str = "127.0.0.1"
    port: int = 5310


@dataclass
class InboxConfig:
    group_batch_seconds: float = 30   # 群里没 @ 她的消息，攒多久再叫醒她
    group_min_interval: float = 180   # 同一个群，普通消息最快多久叫醒她一次


@dataclass
class RhythmConfig:
    heartbeat_default: float = 30  # 她没说下次几分钟醒，就按这个（分钟）
    heartbeat_min: float = 5
    heartbeat_max: float = 120
    sleep_start: str = "00:30"     # 北京时间；睡着时只有主人私聊能叫醒她
    wake_time: str = "08:30"


@dataclass
class Config:
    data_dir: Path
    onebot: OneBotConfig
    llm: LLMConfig
    gate: GateConfig
    console: ConsoleConfig
    inbox: InboxConfig = field(default_factory=InboxConfig)
    rhythm: RhythmConfig = field(default_factory=RhythmConfig)
    # 上下文上限（token，按上一次调用的实际输入算）。超了就把最旧的三分之一整块移进存档
    context_budget: int = 64000


def load(path: Path | None = None, env: dict | None = None) -> Config:
    """读配置。env 不传时用真实环境变量，并且读 data/secrets.bin 补上没设的密钥；传了 env（测试用）就只认它。"""
    use_saved = env is None
    env = os.environ if env is None else env
    path = path or ROOT / "config.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    data_dir = Path(raw.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    saved = secrets.load(data_dir) if use_saved else {}

    def secret(name):
        return env.get(name) or saved.get(name, "")

    onebot = OneBotConfig(**raw.get("onebot", {}))
    onebot.token, onebot.secret = secret("ONEBOT_TOKEN"), secret("ONEBOT_SECRET")

    llm = LLMConfig(**raw.get("llm", {}))
    llm.api_key = secret("DEEPSEEK_API_KEY")

    gate = GateConfig(**raw.get("gate", {}))
    gate.owner_qq = str(gate.owner_qq)
    gate.allow_groups = [str(x) for x in gate.allow_groups]
    gate.allow_users = [str(x) for x in gate.allow_users]

    return Config(
        data_dir=data_dir,
        onebot=onebot,
        llm=llm,
        gate=gate,
        console=ConsoleConfig(**raw.get("console", {})),
        inbox=InboxConfig(**raw.get("inbox", {})),
        rhythm=RhythmConfig(**raw.get("rhythm", {})),
        context_budget=int(raw.get("context_budget", 64000)),
    )  # 第 1 步的 max_turns 已经不用了，旧配置里留着也没关系


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)  # 字符串和字符串列表：JSON 的写法 TOML 也认


def dump(cfg: Config) -> str:
    """把配置写回 TOML（设置页保存时用）。密钥不写。"""
    data_dir = "data" if cfg.data_dir == ROOT / "data" else str(cfg.data_dir)
    sections = {
        "onebot": (cfg.onebot, ("api_url", "listen_host", "listen_port")),
        "llm": (cfg.llm, ("base_url", "model", "timeout", "max_steps", "price_hit", "price_miss", "price_out",
                          "peak_multiplier")),
        "gate": (cfg.gate, ("owner_qq", "owner_name", "allow_groups", "allow_users", "per_conv_per_minute",
                            "global_per_minute", "max_text_len")),
        "console": (cfg.console, ("host", "port")),
        "inbox": (cfg.inbox, ("group_batch_seconds", "group_min_interval")),
        "rhythm": (cfg.rhythm, ("heartbeat_default", "heartbeat_min", "heartbeat_max", "sleep_start", "wake_time")),
    }
    lines = ["# 由 launcher 的设置页保存。可以手改，改完重启生效；密钥不在这里。", "",
             f"data_dir = {_toml(data_dir)}", f"context_budget = {_toml(cfg.context_budget)}"]
    for name, (obj, keys) in sections.items():
        lines += ["", f"[{name}]"] + [f"{k} = {_toml(getattr(obj, k))}" for k in keys]
    return "\n".join(lines) + "\n"
