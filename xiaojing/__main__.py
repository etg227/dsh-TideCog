"""启动：python -m xiaojing"""
import asyncio
import logging
import sys

from .app import App, PortInUse
from .config import load


def main():
    cfg = load()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s：%(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(cfg.data_dir / "xiaojing.log", encoding="utf-8")],
    )
    missing = [k for k, v in (("DEEPSEEK_API_KEY", cfg.llm.api_key),) if not v]
    if missing:
        logging.warning("没有设置环境变量：%s。她会被叫醒，但调不了模型。", "、".join(missing))
    if not cfg.gate.allow_groups and not cfg.gate.allow_users:
        logging.warning("config.toml 里允许名单是空的：她谁也不会回。")
    try:
        asyncio.run(App(cfg).run_forever())
    except PortInUse as e:
        logging.error("%s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        print("已停止")


main()
