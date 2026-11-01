"""App configuration: export defaults + last project. (AI 功能已移除)"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field

from . import paths

DEFAULT_EXPORT = {
    "pad_ratio": 0.10,
    "prefix": "",
    "start": 1,
    "digits": 0,
    "sheet": True,
    "quality": 95,
    "min_pad": 6,
    "order": "visual",
    "fmt": "png",
}


@dataclass
class AppConfig:
    export: dict = field(default_factory=lambda: dict(DEFAULT_EXPORT))
    last_project: str = ""

    @classmethod
    def load(cls) -> "AppConfig":
        p = paths.config_path()
        cfg = cls()
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
            exp = dict(DEFAULT_EXPORT)
            exp.update({k: v for k, v in (raw.get("export") or {}).items() if k in DEFAULT_EXPORT})
            cfg.export = exp
            cfg.last_project = raw.get("last_project", "")
        return cfg

    def save(self) -> None:
        p = paths.config_path()
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    def public(self) -> dict:
        return {"export": dict(self.export), "last_project": self.last_project}
