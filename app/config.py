"""App configuration: AI credentials + UI defaults. Never logged in clear text."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from typing import Any

from . import paths

DEFAULT_AI = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "max_workers": 4,
    "timeout": 60.0,
    "enabled": True,
    # DeepSeek 默认开启思考模式：思维链会吃光 max_tokens，正文就成空串。
    # 这里只想要一个 JSON，默认关闭思考（省 token 且快 20 倍）。
    "thinking": "disabled",
    "max_tokens": 512,
}
DEFAULT_EXPORT = {
    "pad_ratio": 0.10,
    "min_pad": 6,
    "order": "visual",
    "fmt": "png",
}


@dataclass
class AppConfig:
    ai: dict = field(default_factory=lambda: dict(DEFAULT_AI))
    export: dict = field(default_factory=lambda: dict(DEFAULT_EXPORT))
    last_project: str = ""

    # ---------------------------------------------------------------- load/save
    @classmethod
    def load(cls) -> "AppConfig":
        p = paths.config_path()
        cfg = cls()
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
            ai = dict(DEFAULT_AI)
            ai.update({k: v for k, v in (raw.get("ai") or {}).items() if k in DEFAULT_AI})
            exp = dict(DEFAULT_EXPORT)
            exp.update({k: v for k, v in (raw.get("export") or {}).items() if k in DEFAULT_EXPORT})
            cfg.ai, cfg.export = ai, exp
            cfg.last_project = raw.get("last_project", "")
        # environment overrides beat the file (handy for CI / no-UI runs)
        if os.environ.get("DEEPSEEK_API_KEY"):
            cfg.ai["api_key"] = os.environ["DEEPSEEK_API_KEY"]
        if os.environ.get("DEEPSEEK_BASE_URL"):
            cfg.ai["base_url"] = os.environ["DEEPSEEK_BASE_URL"]
        if os.environ.get("DEEPSEEK_MODEL"):
            cfg.ai["model"] = os.environ["DEEPSEEK_MODEL"]
        return cfg

    def save(self) -> None:
        p = paths.config_path()
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    # ------------------------------------------------------------------ helpers
    def public(self) -> dict:
        """Safe view for the UI: the API key is masked."""
        ai = dict(self.ai)
        key = ai.get("api_key") or ""
        ai["configured"] = bool(key)
        ai["api_key"] = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已设置" if key else "")
        return {"ai": ai, "export": dict(self.export), "last_project": self.last_project}

    def ai_config(self):
        """Import lazily so app.config has no heavy deps."""
        from .ai_refine import AIConfig
        keys = ("api_key", "base_url", "model", "max_workers", "timeout",
                "thinking", "max_tokens")
        return AIConfig(**{k: self.ai[k] for k in keys if k in self.ai})
