"""DeepSeek 计价：闲时价 × 高峰倍数。高峰 = 北京时间工作日 9–12 点、14–18 点（节假日暂时没扣除）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
PEAK_WINDOWS = ((9, 12), (14, 18))


def is_peak(ts: float) -> bool:
    t = datetime.fromtimestamp(ts, BJ)
    return t.isoweekday() <= 5 and any(a <= t.hour < b for a, b in PEAK_WINDOWS)


def cost(usage: dict, llm_cfg, ts: float) -> tuple[float, bool]:
    """返回 (这次花了多少元, 是不是高峰)。usage 里是 hit / miss / out 三种 token 数。"""
    peak = is_peak(ts)
    factor = llm_cfg.peak_multiplier if peak else 1.0
    yuan = (
        usage["hit"] * llm_cfg.price_hit
        + usage["miss"] * llm_cfg.price_miss
        + usage["out"] * llm_cfg.price_out
    ) / 1e6 * factor
    return yuan, peak


def bj_day_start(ts: float) -> float:
    """ts 所在的北京时间那一天的零点。"""
    return (ts + 8 * 3600) // 86400 * 86400 - 8 * 3600


def bj_time(ts: float, fmt: str = "%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts, BJ).strftime(fmt)
