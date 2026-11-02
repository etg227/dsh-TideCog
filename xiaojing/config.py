"""读取 config.toml。密钥只从环境变量拿，不写进任何文件。"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
class Config:
    data_dir: Path
    onebot: OneBotConfig
    llm: LLMConfig
    gate: GateConfig
    console: ConsoleConfig
    max_turns: int = 30  # 会话里最多留几圈，超了把最旧的三分之一整块移进存档


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
        max_turns=int(raw.get("max_turns", 30)),
    )
