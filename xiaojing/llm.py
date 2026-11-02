"""调 DeepSeek（OpenAI 兼容的 /chat/completions，带工具调用）。用标准库发请求，放进线程里，不卡主循环。"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


class DeepSeek:
    def __init__(self, cfg):
        self.cfg = cfg

    async def chat(self, messages: list[dict], tools: list[dict]) -> tuple[dict, dict]:
        """返回 (assistant 消息, 用量)。用量里是 hit / miss / out 三种 token 数和实际用的模型。"""
        return await asyncio.to_thread(self._chat, messages, tools)

    def _chat(self, messages, tools):
        if not self.cfg.api_key:
            raise LLMError("没有设置环境变量 DEEPSEEK_API_KEY")
        body = json.dumps({"model": self.cfg.model, "messages": messages, "tools": tools},
                          ensure_ascii=False).encode()
        req = urllib.request.Request(
            self.cfg.base_url.rstrip("/") + "/chat/completions", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.cfg.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as res:
                data = json.loads(res.read())
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace")
            raise LLMError(f"HTTP {e.code}：{detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LLMError(f"连不上 DeepSeek：{getattr(e, 'reason', e)}") from None
        except json.JSONDecodeError:
            raise LLMError("DeepSeek 返回的不是 JSON") from None

        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"DeepSeek 返回格式不对：{str(data)[:200]}") from None
        u = data.get("usage") or {}
        hit = u.get("prompt_cache_hit_tokens", 0)
        miss = u.get("prompt_cache_miss_tokens", max(u.get("prompt_tokens", 0) - hit, 0))
        usage = {"hit": hit, "miss": miss, "out": u.get("completion_tokens", 0),
                 "model": data.get("model", self.cfg.model)}
        return msg, usage
