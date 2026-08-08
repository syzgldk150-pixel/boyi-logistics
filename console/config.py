import json
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8-sig")

MODULE_DIR = Path(__file__).resolve().parent


def _discover_project_root(module_dir: Path) -> Path:
    explicit = os.getenv("DOCFLOW_AGENT_PROJECT_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()

    parent_dir = module_dir.parent
    direct_candidates = (
        parent_dir / "agent",
        parent_dir,
    )
    for candidate in direct_candidates:
        if (
            candidate.exists()
            and (candidate / "main.py").exists()
            and (candidate / "agent").is_dir()
            and (candidate / "tools").is_dir()
        ):
            return candidate.resolve()

    for candidate in parent_dir.iterdir():
        if (
            candidate.is_dir()
            and (candidate / "main.py").exists()
            and (candidate / "agent").is_dir()
            and (candidate / "tools").is_dir()
        ):
            return candidate.resolve()

    return parent_dir.resolve()


PROJECT_ROOT = _discover_project_root(MODULE_DIR)


def _discover_ocr_module_dir(project_root: Path) -> Path | None:
    for candidate in project_root.iterdir():
        if not candidate.is_dir():
            continue
        if (candidate / "train_data").exists() and (candidate / "check_images.py").exists():
            return candidate
    return None


OCR_MODULE_DIR = _discover_ocr_module_dir(PROJECT_ROOT)

def _wsl_gateway_ip() -> str:
    """在 WSL 环境下自动获取 Windows 宿主机网关 IP。"""
    try:
        with open("/proc/net/route", "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    hex_ip = parts[2]
                    return ".".join(
                        str(int(hex_ip[i:i+2], 16))
                        for i in range(6, -1, -2)
                    )
    except (FileNotFoundError, ValueError, IndexError):
        pass
    return ""


def _running_in_wsl() -> bool:
    if os.getenv("WSL_DISTRO_NAME"):
        return True
    return "microsoft" in platform.release().lower()


def _resolve_mysql_host() -> str:
    raw_host = os.getenv("DOCFLOW_MYSQL_HOST", os.getenv("AGENT_DB_HOST", "")).strip()
    if raw_host != "wsl-gateway":
        return raw_host
    if _running_in_wsl():
        return _wsl_gateway_ip()
    return "127.0.0.1"


def _resolve_mysql_port() -> int:
    raw_host = os.getenv("DOCFLOW_MYSQL_HOST", os.getenv("AGENT_DB_HOST", "")).strip()
    if raw_host == "wsl-gateway" and not _running_in_wsl():
        return _env_int("AGENT_DB_PORT", 3306)
    return _env_int("DOCFLOW_MYSQL_PORT", _env_int("AGENT_DB_PORT", 3306))


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_path(raw_path: str, default_path: Path) -> Path:
    if not raw_path:
        return default_path
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def _load_json_env(name: str) -> dict[str, Any]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    app_title: str
    runtime_dir: Path
    state_dir: Path
    originals_dir: Path
    artifacts_dir: Path
    processed_dir: Path
    temp_dir: Path
    mysql_host: str
    mysql_port: int
    mysql_connect_timeout_seconds: int
    mysql_user: str
    mysql_password: str
    mysql_database: str
    agent_base_url: str
    agent_internal_api_token: str
    agent_timeout_seconds: int
    basic_auth_user: str
    basic_auth_password: str
    admin_seed_username: str
    admin_seed_password: str
    session_secret: str
    session_cookie_secure: bool
    session_ttl_hours: int
    template_path: Path
    templates_dir: Path
    template_state_path: Path
    confidence_threshold: float
    ocr_worker_count: int
    http_timeout_seconds: int
    qwen_provider_mode: str
    qwen_endpoint: str
    qwen_api_key: str
    qwen_model: str
    qwen_extra_headers: dict[str, Any]
    amap_api_key: str
    amap_security_code: str
    training_crops_dir: Path
    paddle_model_dir: Path
    training_sample_threshold: int
    paddle_enabled: bool
    paddle_confidence_threshold: float


def load_settings() -> Settings:
    runtime_dir = MODULE_DIR / "runtime"
    state_dir = runtime_dir / "state"
    template_default = MODULE_DIR / "config" / "waybill_template.json"
    templates_dir = MODULE_DIR / "config" / "templates"
    agent_port = _env_int("AGENT_PORT", 9000)
    return Settings(
        host=os.getenv("DOCFLOW_HOST", "127.0.0.1"),
        port=_env_int("DOCFLOW_PORT", 8765),
        app_title=os.getenv("DOCFLOW_APP_TITLE", "物流 Agent 本地控制台"),
        runtime_dir=runtime_dir,
        state_dir=state_dir,
        originals_dir=runtime_dir / "originals",
        artifacts_dir=runtime_dir / "artifacts",
        processed_dir=runtime_dir / "artifacts" / "processed",
        temp_dir=runtime_dir / "artifacts" / "temp",
        mysql_host=_resolve_mysql_host(),
        mysql_port=_resolve_mysql_port(),
        mysql_connect_timeout_seconds=max(1, _env_int("DOCFLOW_MYSQL_CONNECT_TIMEOUT_SECONDS", 5)),
        mysql_user=os.getenv("DOCFLOW_MYSQL_USER", os.getenv("AGENT_DB_USER", "")),
        mysql_password=os.getenv("DOCFLOW_MYSQL_PASSWORD", os.getenv("AGENT_DB_PASS", "")),
        mysql_database=os.getenv("DOCFLOW_MYSQL_DATABASE", os.getenv("AGENT_DB_NAME", "")),
        agent_base_url=(
            os.getenv("DOCFLOW_AGENT_BASE_URL", "").strip()
            or f"http://127.0.0.1:{agent_port}"
        ),
        agent_internal_api_token=os.getenv("AGENT_INTERNAL_API_TOKEN", "").strip(),
        agent_timeout_seconds=max(5, _env_int("DOCFLOW_AGENT_TIMEOUT_SECONDS", 30)),
        basic_auth_user=os.getenv("DOCFLOW_BASIC_AUTH_USER", "").strip(),
        basic_auth_password=os.getenv("DOCFLOW_BASIC_AUTH_PASS", "").strip(),
        admin_seed_username=os.getenv("DOCFLOW_ADMIN_USERNAME", "").strip(),
        admin_seed_password=os.getenv("DOCFLOW_ADMIN_PASSWORD", ""),
        session_secret=os.getenv("DOCFLOW_SESSION_SECRET", "").strip(),
        session_cookie_secure=_env_bool("DOCFLOW_COOKIE_SECURE", False),
        session_ttl_hours=max(1, _env_int("DOCFLOW_SESSION_TTL_HOURS", 12)),
        template_path=_resolve_path(os.getenv("DOCFLOW_TEMPLATE_PATH", ""), template_default),
        templates_dir=templates_dir,
        template_state_path=state_dir / "active_template.txt",
        confidence_threshold=_env_float("DOCFLOW_CONFIDENCE_THRESHOLD", 0.85),
        ocr_worker_count=max(1, min(_env_int("DOCFLOW_OCR_WORKER_COUNT", 5), 10)),
        http_timeout_seconds=_env_int("DOCFLOW_HTTP_TIMEOUT_SECONDS", 30),
        qwen_provider_mode=os.getenv(
            "DOCFLOW_QWEN_PROVIDER_MODE",
            "http_json" if (os.getenv("DOCFLOW_QWEN_API_KEY", "").strip() or os.getenv("QWEN_VL_API_KEY", "").strip()) else "placeholder",
        ).lower(),
        qwen_endpoint=(
            os.getenv("DOCFLOW_QWEN_ENDPOINT", "").strip()
            or os.getenv("QWEN_VL_ENDPOINT", "").strip()
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        qwen_api_key=(
            os.getenv("DOCFLOW_QWEN_API_KEY", "").strip()
            or os.getenv("QWEN_VL_API_KEY", "").strip()
            or os.getenv("Qwen VL API", "").strip()
        ),
        qwen_model=(
            os.getenv("DOCFLOW_QWEN_MODEL", "").strip()
            or os.getenv("QWEN_VL_MODEL", "").strip()
            or "qwen-vl-ocr"
        ),
        qwen_extra_headers=_load_json_env("DOCFLOW_QWEN_EXTRA_HEADERS"),
        amap_api_key=os.getenv("AMAP_API_KEY", "").strip(),
        amap_security_code=(
            os.getenv("AMAP_API_secret", "").strip()
            or os.getenv("AMAP_SECURITY_CODE", "").strip()
        ),
        training_crops_dir=runtime_dir / "training_crops",
        paddle_model_dir=runtime_dir / "models" / "paddle",
        training_sample_threshold=_env_int("DOCFLOW_TRAINING_THRESHOLD", 50),
        paddle_enabled=os.getenv("DOCFLOW_PADDLE_ENABLED", "false").lower() in {"true", "1", "yes"},
        paddle_confidence_threshold=_env_float("DOCFLOW_PADDLE_CONFIDENCE", 0.8),
    )


def load_template_spec(template_path: Path) -> dict[str, Any]:
    with template_path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)
