"""AIClient —— 通用 OpenAI 兼容异步客户端（带熔断降级）

供 Personae / Memoir 插件共用：AI 分析对话、提取记忆、生成性格画像。
- 支持手动配置 base_url / api_key / model（OpenAI 兼容即可）
- 连续失败达到阈值后进入冷却期，冷却期内直接跳过（长期不可达就不总结）
- 提供 chat / chat_json 两个接口，均不抛异常，失败返回 None
"""
import json
import time

try:
    from aiohttp import ClientSession, ClientTimeout
    HAS_AIOHTTP = True
except Exception:
    HAS_AIOHTTP = False

DEFAULT_TIMEOUT = 60        # 单次请求超时（秒）
DEFAULT_COOLDOWN = 1800     # 熔断冷却（秒）
DEFAULT_MAX_FAILS = 3       # 连续失败多少次进入冷却


class AIClient:
    def __init__(self, base_url="", api_key="", model="",
                 timeout=DEFAULT_TIMEOUT, cooldown=DEFAULT_COOLDOWN,
                 max_fails=DEFAULT_MAX_FAILS):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = int(timeout or DEFAULT_TIMEOUT)
        self.cooldown = int(cooldown or DEFAULT_COOLDOWN)
        self.max_fails = int(max_fails or DEFAULT_MAX_FAILS)
        self.fail_streak = 0
        self.cooldown_until = 0.0
        self.total_calls = 0
        self.total_fails = 0

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @property
    def in_cooldown(self) -> bool:
        return time.time() < self.cooldown_until

    @property
    def healthy(self) -> bool:
        """是否可用：配置完整且不在冷却期。"""
        return self.configured and not self.in_cooldown

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    async def chat(self, system: str, user: str, max_tokens: int = 800,
                   temperature: float = 0.3, json_mode: bool = False) -> str | None:
        """调用 LLM，返回 content 文本；失败/熔断/未配置返回 None。"""
        if not HAS_AIOHTTP or not self.healthy:
            return None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with ClientSession(timeout=ClientTimeout(total=self.timeout)) as sess:
                async with sess.post(self._endpoint(), json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = await resp.json()
            self.total_calls += 1
            self.fail_streak = 0
            try:
                return data["choices"][0]["message"]["content"]
            except Exception:
                return None
        except Exception:
            self.total_calls += 1
            self.total_fails += 1
            self.fail_streak += 1
            if self.fail_streak >= self.max_fails:
                self.cooldown_until = time.time() + self.cooldown
                self.fail_streak = 0
            return None

    async def chat_json(self, system: str, user: str, max_tokens: int = 800) -> dict | list | None:
        """调用 LLM 并要求 JSON 输出，解析后返回 dict/list；失败返回 None。"""
        text = await self.chat(system, user, max_tokens=max_tokens, json_mode=True)
        if not text:
            return None
        return self._parse_json(text)

    @staticmethod
    def _parse_json(text: str):
        text = (text or "").strip()
        # 去掉 ```json 围栏
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.startswith("json"):
                text = text[4:].strip()
        try:
            return json.loads(text)
        except Exception:
            # 兜底：截取最外层 {..} 或 [..]
            for s, e in (("{", "}"), ("[", "]")):
                i, j = text.find(s), text.rfind(e)
                if i >= 0 and j > i:
                    try:
                        return json.loads(text[i:j + 1])
                    except Exception:
                        continue
            return None

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "in_cooldown": self.in_cooldown,
            "healthy": self.healthy,
            "base_url": self.base_url,
            "model": self.model,
            "total_calls": self.total_calls,
            "total_fails": self.total_fails,
            "cooldown_until": self.cooldown_until,
        }
