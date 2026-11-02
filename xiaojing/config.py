"""读取 config.toml。密钥只从环境变量拿，不写进任何文件。"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _root() -> Path:
    """项目文件夹：config.toml、人设、data/ 都在这。打包成 exe 后就是 exe 所在的文件夹。"""
    if override := os.environ.get("XIAOJING_ROOT"):
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _root()


@dataclass
class OneBotConfig:
    api_url: str = "http://127.0.0.1:3020"  # 测试号 SnowLuma 的 HTTP 服务器
    listen_host: str = "127.0.0.1"           # SnowLuma 的 HTTP 上报发到这里
    listen_port: int = 5311
    token: str = ""   # 环境变量 ONEBOT_TOKEN：调 SnowLuma 时带上
    secret: str = ""  # 环境变量 ONEBOT_SECRET：校验上报的签名


@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key: str = ""  # 环境变量 DEEPSEEK_API_KEY
    timeout: float = 90
    max_steps: int = 8  # 一圈里最多连着调几次模型
    # 每百万 token 的价格（元），闲时价；高峰乘 peak_multiplier
    price_hit: float = 0.02
    price_miss: float = 1.0
    price_out: float = 4.0
    peak_multiplier: float = 2.0


@dataclass
class GateConfig:
    owner_qq: str = ""  # 只有这个号私聊发「暂停」「继续」才算数
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
    env = os.environ if env is None else env
    path = path or ROOT / "config.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    onebot = OneBotConfig(**raw.get("onebot", {}))
    onebot.token = env.get("ONEBOT_TOKEN", "")
    onebot.secret = env.get("ONEBOT_SECRET", "")

    llm = LLMConfig(**raw.get("llm", {}))
    llm.api_key = env.get("DEEPSEEK_API_KEY", "")

    gate = GateConfig(**raw.get("gate", {}))
    gate.owner_qq = str(gate.owner_qq)
    gate.allow_groups = [str(x) for x in gate.allow_groups]
    gate.allow_users = [str(x) for x in gate.allow_users]

    data_dir = Path(raw.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
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
