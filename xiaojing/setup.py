"""第一次用 launcher 时的准备：放好默认文件、检查缺什么、把设置页填的内容存下来。"""
from __future__ import annotations

import re
import shutil
import sys
from copy import deepcopy
from pathlib import Path

from . import secrets
from .config import Config, dump

# 打包进 exe 的默认文件 -> 放到项目文件夹里的位置
DEFAULT_FILES = (("config.example.toml", "config.toml"),
                 ("persona.default.md", "persona.md"),
                 ("prompts/system.md", "prompts/system.md"))


def bundled(rel: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / rel


def ensure_defaults(root: Path) -> list[str]:
    """缺哪个默认文件就放哪个，已有的一个都不碰。返回新放的文件。"""
    created = []
    for src, dst in DEFAULT_FILES:
        target = root / dst
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(bundled(src), target)
            created.append(dst)
    return created


def missing(cfg: Config) -> list[str]:
    """跑起来至少要有的东西，缺了就先去设置页。"""
    out = []
    if not cfg.gate.owner_qq:
        out.append("主人的 QQ 号")
    if not cfg.llm.api_key:
        out.append("DeepSeek API key")
    if not cfg.onebot.token:
        out.append("OneBot 的 HTTP token")
    return out


def parse_ids(text: str) -> list[str]:
    """从"123, 456 789"这种写法里挑出 QQ 号 / 群号，去重保序。"""
    seen = []
    for x in re.findall(r"\d{5,12}", str(text or "")):
        if x not in seen:
            seen.append(x)
    return seen


def state(cfg: Config, root: Path) -> dict:
    """设置页要显示的东西。密钥只说有没有设，不给值。"""
    return {
        "owner_qq": cfg.gate.owner_qq,
        "owner_name": cfg.gate.owner_name,
        "allow_groups": " ".join(cfg.gate.allow_groups),
        "allow_users": " ".join(x for x in cfg.gate.allow_users if x != cfg.gate.owner_qq),
        "api_url": cfg.onebot.api_url,
        "model": cfg.llm.model,
        "has_key": bool(cfg.llm.api_key),
        "has_token": bool(cfg.onebot.token),
        "has_secret": bool(cfg.onebot.secret),
        "report_url": f"http://{cfg.onebot.listen_host}:{cfg.onebot.listen_port}/",
        "root": str(root),
        "missing": missing(cfg),
        "can_save_secrets": secrets.available(),
    }


def apply(cfg: Config, values: dict) -> tuple[Config, dict, dict]:
    """检查设置页填的内容。返回 (新配置, 要存的密钥, 每个字段的错误)；有错误时前两个不要用。"""
    errors = {}
    new = deepcopy(cfg)
    owner = parse_ids(values.get("owner_qq", ""))
    if len(owner) != 1:
        errors["owner_qq"] = "填一个 QQ 号（5 到 12 位数字）"
    else:
        new.gate.owner_qq = owner[0]
    new.gate.owner_name = str(values.get("owner_name", "")).strip()[:20] or "主人"
    new.gate.allow_groups = parse_ids(values.get("allow_groups", ""))
    users = parse_ids(values.get("allow_users", ""))
    new.gate.allow_users = ([new.gate.owner_qq] if owner else []) + [u for u in users if u not in owner]
    api_url = str(values.get("api_url", "")).strip().rstrip("/")
    if not re.fullmatch(r"https?://[\w.\-]+(:\d{1,5})?(/[\w.\-/]*)?", api_url):
        errors["api_url"] = "填 OneBot HTTP 服务器的地址，比如 http://127.0.0.1:3020"
    else:
        new.onebot.api_url = api_url
    new.llm.model = str(values.get("model", "")).strip() or "deepseek-chat"

    saved = {"DEEPSEEK_API_KEY": str(values.get("api_key", "")).strip(),
             "ONEBOT_TOKEN": str(values.get("token", "")).strip(),
             "ONEBOT_SECRET": str(values.get("secret", "")).strip()}
    if not saved["DEEPSEEK_API_KEY"] and not cfg.llm.api_key:
        errors["api_key"] = "还没有 DeepSeek API key"
    if not saved["ONEBOT_TOKEN"] and not cfg.onebot.token:
        errors["token"] = "还没有 OneBot HTTP 服务器的 token"
    return new, saved, errors


def save(cfg: Config, saved: dict, root: Path):
    """写 config.toml（旧的留一份 .bak）和加密的密钥。"""
    path = root / "config.toml"
    if path.exists():
        shutil.copyfile(path, root / "config.toml.bak")
    path.write_text(dump(cfg), encoding="utf-8")
    if any(saved.values()):
        secrets.save(cfg.data_dir, saved)
