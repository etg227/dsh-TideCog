"""作息：几点睡、几点醒，她自己说"去睡了"也算。时间一律按北京时间。"""
from __future__ import annotations

from datetime import datetime, timedelta

from .prices import BJ


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


class Rhythm:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sleep_until = 0.0  # 她调 sleep() 提前去睡时设上

    def in_sleep_window(self, ts: float) -> bool:
        t = datetime.fromtimestamp(ts, BJ)
        now = t.hour * 60 + t.minute
        start, wake = _minutes(self.cfg.sleep_start), _minutes(self.cfg.wake_time)
        if start == wake:
            return False
        if start < wake:
            return start <= now < wake
        return now >= start or now < wake  # 跨零点，比如 00:30 睡 08:30 醒算不跨，23:30 睡就跨

    def sleeping(self, ts: float) -> bool:
        return ts < self.sleep_until or self.in_sleep_window(ts)

    def next_wake(self, ts: float) -> float:
        """ts 之后最近的一个起床时间点。"""
        t = datetime.fromtimestamp(ts, BJ)
        wake = _minutes(self.cfg.wake_time)
        target = t.replace(hour=wake // 60, minute=wake % 60, second=0, microsecond=0)
        if target <= t:
            target += timedelta(days=1)
        return target.timestamp()
