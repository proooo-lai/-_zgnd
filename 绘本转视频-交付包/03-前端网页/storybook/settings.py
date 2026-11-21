from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
PROVIDER_NAMES = ("vision", "chat", "asr", "tts", "video")
PUBLIC_PROVIDER_FIELDS = ("kind", "base_url", "model", "voice")
RUNTIME_FIELDS = ("concurrency", "max_pages", "target_duration", "width", "height", "fps")


def validate_service_url(value: str) -> str:
    """Validate without putting credentials or a signed URL into errors."""
    if not isinstance(value, str):
        raise ValueError("模型服务地址必须是字符串")
    value = value.strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if not parsed.hostname or any(character.isspace() for character in value):
            raise ValueError
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError
        if parsed.scheme == "https":
            pass
        elif parsed.scheme == "http" and parsed.hostname.casefold() in ("localhost", "127.0.0.1", "::1"):
            pass
        else:
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("模型服务地址必须是 HTTPS 或严格本机 HTTP 地址，不能包含用户名、密码、查询参数或片段；密钥请填独立密钥字段") from None
    return value.rstrip("/")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    config_file: Path = field(default_factory=lambda: Path(os.environ.get("BOOK_CONFIG_FILE", ROOT / "config.local.json")))
    ffmpeg: str = ""
    ffprobe: str = ""
    max_pages: int = 100
    concurrency: int = 4
    target_duration: float = 120.0
    width: int = 1280
    height: int = 720
    fps: int = 24
    max_upload_mb: int = 100
    providers: dict = field(default_factory=dict)
    _loaded_config: dict = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)
        self.config_file = Path(self.config_file)
        if self.config_file.is_file():
            config = json.loads(self.config_file.read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict) or not isinstance(config.get("runtime", {}), dict):
                raise ValueError("配置文件必须包含合法的对象结构")
            self._loaded_config = config
            self.providers = config.get("providers", config)
            for key in RUNTIME_FIELDS:
                if key in config.get("runtime", {}):
                    setattr(self, key, config["runtime"][key])
        if not isinstance(self.providers, dict):
            raise ValueError("providers 配置必须是对象")
        for name in PROVIDER_NAMES:
            provider = self.providers.get(name, {})
            if not isinstance(provider, dict):
                raise ValueError(f"{name} 服务配置必须是对象")
            if provider.get("base_url"):
                validate_service_url(provider["base_url"])
            for key in PUBLIC_PROVIDER_FIELDS + ("api_key",):
                if key in provider and not isinstance(provider[key], str):
                    raise ValueError(f"{name} 服务的 {key} 必须是字符串")
        for name in ("ffmpeg", "ffprobe"):
            if getattr(self, name):
                continue
            local = ROOT / "tools" / "bin" / (name + (".exe" if os.name == "nt" else ""))
            setattr(self, name, str(local) if local.is_file() else shutil.which(name) or "")
        if not self.ffmpeg:
            try:
                import imageio_ffmpeg
                self.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except ImportError:
                pass
        self.concurrency = max(1, min(20, int(self.concurrency)))
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def public(self):
        result = {}
        for name in PROVIDER_NAMES:
            private = self.providers.get(name, {})
            config = {key: private[key] for key in PUBLIC_PROVIDER_FIELDS if key in private}
            # Revalidate because callers may update the in-memory object directly.
            try:
                config["base_url"] = validate_service_url(config.get("base_url", ""))
            except ValueError:
                config["base_url"] = ""
            key = private.get("api_key", "")
            if isinstance(key, str) and key.startswith("env:"):
                key = os.environ.get(key[4:], "")
            config["has_key"] = bool(key)
            result[name] = config
        return result

    def save_providers(self, updates: dict):
        if not isinstance(updates, dict):
            raise ValueError("模型配置更新必须是对象")
        staged = {name: dict(value) for name, value in self.providers.items() if name in PROVIDER_NAMES}
        for name, update in updates.items():
            if name not in PROVIDER_NAMES:
                continue
            if not isinstance(update, dict):
                raise ValueError(f"{name} 服务配置必须是对象")
            current = dict(staged.get(name, {}))
            for key in ("kind", "base_url", "model", "voice", "api_key"):
                value = update.get(key)
                if value is not None and (key != "api_key" or str(value).strip()):
                    current[key] = str(value).strip()
            current["base_url"] = validate_service_url(current.get("base_url", ""))
            staged[name] = current
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        saved = {key: value for key, value in self._loaded_config.items() if key not in PROVIDER_NAMES}
        saved["providers"] = staged
        saved["runtime"] = {**saved.get("runtime", {}), **{key: getattr(self, key) for key in RUNTIME_FIELDS}}
        temp = self.config_file.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            temp.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
            if os.name != "nt":
                temp.chmod(0o600)
            temp.replace(self.config_file)
        finally:
            temp.unlink(missing_ok=True)
        self.providers = staged
        self._loaded_config = saved
