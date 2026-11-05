from __future__ import annotations

import asyncio
import base64
import importlib
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
    from astrbot.api.message_components import At, Image, Reply
except Exception:  # pragma: no cover - compatibility with older AstrBot
    At = Image = Reply = object

try:
    from astrbot.api.web import PluginUploadFile, error_response, file_response, json_response, request
except Exception:  # pragma: no cover
    PluginUploadFile = None
    error_response = file_response = json_response = request = None

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except Exception:  # pragma: no cover
    get_astrbot_plugin_data_path = None

# ---------------------------------------------------------------------------
# Optional local visual metric (dghs-imgutils / CCIP), available to all libraries.
#
# requirements.txt is intentionally EMPTY: the PyPI distribution that provides
# the `imgutils` python package is named `dghs-imgutils`, and its metadata
# pins numpy<2, which conflicts with the numpy version pinned by AstrBot core.
# Declaring it in requirements.txt therefore makes the whole plugin fail to
# load (pip exits with code 1 / AstrBot dependency protection kicks in).
#
# Instead we install the wheel here with --no-deps (never touching numpy) and
# bring in its companion dependencies separately. Every installation command
# tries the configured/default PyPI index first, then two Chinese mirrors.
# Any failure leaves the plugin functional and falls back to AstrBot Vision.
# ---------------------------------------------------------------------------
PIP_MIRROR_URLS = (
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple/",
)

# dghs-imgutils 0.19 imports more than the CCIP module itself when
# imgutils.metrics is loaded. Install its declared runtime dependencies except
# numpy (AstrBot owns that version) and OpenCV (handled with a headless fallback).
CCIP_COMPANION_PACKAGES = (
    "hbutils>=0.9.0",
    "pillow",
    "scikit-learn",
    "huggingface-hub",
    "tqdm",
    "pandas",
    "scipy",
    "emoji>=2.5.0,<2.12",
    "pilmoji>=1.3.0",
    "shapely!=2.0.7",
    "pyclipper",
    "deprecation>=2.0.0",
    "hfutils>=0.9.0",
    "filelock",
    "bchlib>=1.0.0,!=2.0.0,!=2.0.1,!=2.1.0,!=2.1.1,!=2.1.2",
    "piexif",
    "tokenizers>=0.20.0",
    "pyrfc6266>=1",
    "urlobject>=2",
    "requests",
    "packaging",
    "onnxruntime",
)


def _pip_install(*packages: str, no_deps: bool = False, uninstall_first: tuple = ()) -> None:
    def _run(args: list) -> None:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "--disable-pip-version-check", *args],
            stdout=subprocess.DEVNULL,
        )

    last_exc: Exception | None = None
    for pkg in uninstall_first:
        try:
            _run(["uninstall", "-y", pkg])
        except Exception:
            pass
    for index_url in (None, *PIP_MIRROR_URLS):
        source_name = index_url or "当前 pip 默认源"
        try:
            logger.info("[VisionMemory] 正在通过%s安装依赖：%s", source_name, ", ".join(packages))
            args = ["install", "--no-input", "--retries", "2", "--timeout", "30"]
            if no_deps:
                args.append("--no-deps")
            if index_url:
                args.extend(["--index-url", index_url])
            _run([*args, *packages])
            logger.info("[VisionMemory] 依赖安装成功，来源：%s", source_name)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("[VisionMemory] 通过%s安装失败，将尝试下一来源：%s", source_name, str(exc)[:300])
    raise last_exc if last_exc else RuntimeError("pip install failed")


def _log_ccip_import_error(stage: str, exc: Exception) -> None:
    logger.warning(
        "[VisionMemory] CCIP 导入失败（%s）：%s: %s",
        stage,
        type(exc).__name__,
        _clean(exc, 500),
    )
    logger.debug("[VisionMemory] CCIP import traceback", exc_info=True)


def _ccip_backend_available() -> bool:
    try:
        importlib.import_module("imgutils.metrics")
        return True
    except Exception as exc:
        _log_ccip_import_error("安装前检查", exc)
    try:
        logger.info("[VisionMemory] CCIP component missing; installing dghs-imgutils with --no-deps "
                    "(bypassing its numpy<2 pin to stay compatible with AstrBot core) ...")
        _pip_install("dghs-imgutils>=0.19.0,<0.20", no_deps=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[VisionMemory] dghs-imgutils installation failed: %s", exc)
        return False
    companion_sets = (
        (*CCIP_COMPANION_PACKAGES, "opencv-python"),
        (*CCIP_COMPANION_PACKAGES, "opencv-python-headless"),
    )
    for companions in companion_sets:
        headless = "opencv-python-headless" in companions
        try:
            _pip_install(*companions, uninstall_first=("opencv-python",) if headless else ())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[VisionMemory] companion dependency install failed: %s", exc)
            continue
        try:
            importlib.invalidate_caches()
            importlib.import_module("imgutils.metrics")
            logger.info("[VisionMemory] dghs-imgutils is ready; local CCIP verification enabled.")
            return True
        except Exception as exc:
            _log_ccip_import_error("无界面 OpenCV 回退" if headless else "常规 OpenCV", exc)
    logger.warning("[VisionMemory] dghs-imgutils 已安装但无法导入；请根据上方的具体异常检查依赖或系统运行库。"
                   "本地 CCIP 核验已停用，将回退到视觉模型。")
    return False


PLUGIN_ID = "astrbot_plugin_vision_memory"
AUTHOR = "UptonKipling"
VERSION = "1.10.6"
DATA_VERSION = 9

CHARACTER_NAMES = [
    "二阶堂希罗", "月代雪", "樱羽艾玛", "夏目安安", "城崎诺亚", "莲见蕾雅",
    "佐伯米莉亚", "宝生玛格", "黑部奈叶香", "紫藤亚里沙", "橘雪莉", "远野汉娜",
    "泽渡可可", "冰上梅露露", "典狱长", "看守",
]
LIBRARIES = {
    "magic_trial_characters": "预设库（魔裁）",
    "custom_objects": "人工视觉添加库",
    "bot_memory": "Bot图片库",
}
LIBRARY_ORDER = list(LIBRARIES)


def _now() -> float:
    return time.time()


def _clean(value: Any, limit: int = 4000) -> str:
    text = str(value or "").replace("\x00", " ").strip()
    return text[:limit]


# Run optional CCIP setup only after every helper used by its diagnostics has
# been defined. A missing dependency must never prevent the plugin itself loading.
if _ccip_backend_available():
    try:
        from huggingface_hub import hf_hub_download
        from imgutils.metrics import ccip_difference, ccip_extract_feature, ccip_default_threshold, ccip_merge
        CCIP_AVAILABLE = True
    except Exception as exc:  # pragma: no cover
        _log_ccip_import_error("函数导入", exc)
        hf_hub_download = None
        ccip_difference = ccip_extract_feature = ccip_default_threshold = ccip_merge = None
        CCIP_AVAILABLE = False
else:
    hf_hub_download = None
    ccip_difference = ccip_extract_feature = ccip_default_threshold = ccip_merge = None
    CCIP_AVAILABLE = False


# CCIP Python dependencies and their ONNX model files come from different
# services. Pre-download every file used by extraction/comparison/thresholds so
# imgutils can subsequently load them from the same Hugging Face local cache.
CCIP_MODEL_REPOSITORY = "deepghs/ccip_onnx"
CCIP_MODEL_FILES = ("model_feat.onnx", "model_metrics.onnx", "metrics.json")
CCIP_HF_ENDPOINTS = ("https://hf-mirror.com", "https://huggingface.co")
CCIP_LOCAL_DIR_ENV = "VISION_MEMORY_CCIP_MODEL_DIR"
_CCIP_MODEL_LOCK = threading.Lock()
_CCIP_READY_MODELS: set[str] = set()
_CCIP_VERIFIED_MODELS: set[str] = set()


def _ccip_endpoints() -> list[str]:
    """Prefer an explicit user endpoint, then the China mirror, then the Hub."""
    endpoints = []
    custom = os.environ.get("HF_ENDPOINT", "").strip().rstrip("/")
    for endpoint in (custom, *CCIP_HF_ENDPOINTS):
        if endpoint and endpoint not in endpoints:
            endpoints.append(endpoint)
    return endpoints


def _ccip_exception_detail(exc: BaseException | None) -> str:
    """Expose the wrapped network error that LocalEntryNotFoundError hides."""
    if exc is None:
        return "无详细异常"
    parts = []
    current: BaseException | None = exc
    seen = set()
    while current is not None and id(current) not in seen and len(parts) < 5:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {_clean(current, 350)}")
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def _ccip_log_network_environment() -> None:
    """Log offline/proxy state without exposing proxy URLs or credentials."""
    try:
        constants = importlib.import_module("huggingface_hub.constants")
        hub_offline = bool(getattr(constants, "HF_HUB_OFFLINE", False))
    except Exception:
        hub_offline = os.environ.get("HF_HUB_OFFLINE", "").lower() in {"1", "true", "yes", "on"}
    transformers_offline = os.environ.get("TRANSFORMERS_OFFLINE", "")
    proxy_vars = [key for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy") if os.environ.get(key)]
    logger.info(
        "[VisionMemory][CCIP] 网络环境：HF_HUB_OFFLINE=%s，TRANSFORMERS_OFFLINE=%s，代理变量=%s",
        hub_offline,
        transformers_offline or "未设置",
        "、".join(sorted(set(proxy_vars))) if proxy_vars else "未设置",
    )


def _ccip_model_root() -> Path:
    custom = os.environ.get(CCIP_LOCAL_DIR_ENV, "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    try:
        if get_astrbot_plugin_data_path:
            return Path(get_astrbot_plugin_data_path()) / PLUGIN_ID / "ccip_models"
    except Exception:
        pass
    return Path.cwd() / "data" / "plugin_data" / PLUGIN_ID / "ccip_models"


def _ccip_local_model_dir(model: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model or ""):
        raise ValueError("CCIP 模型名称只能包含字母、数字、点、下划线和连字符。")
    return _ccip_model_root() / model


def _ccip_local_model_complete(model: str) -> bool:
    try:
        root = _ccip_local_model_dir(model)
        return all((root / filename).is_file() and (root / filename).stat().st_size > 0 for filename in CCIP_MODEL_FILES)
    except Exception:
        return False


def _ccip_local_file_for_repo_filename(filename: str) -> Path | None:
    parts = filename.split("/")
    if len(parts) != 2 or parts[1] not in CCIP_MODEL_FILES:
        return None
    try:
        path = _ccip_local_model_dir(parts[0]) / parts[1]
        return path if path.is_file() and path.stat().st_size > 0 else None
    except Exception:
        return None


def _install_ccip_local_loader() -> None:
    """Let imgutils resolve plugin-managed model files before using the Hub."""
    ccip_module = importlib.import_module("imgutils.metrics.ccip")
    original = getattr(ccip_module, "hf_hub_download", None)
    if original is None or getattr(original, "_vision_memory_local_loader", False):
        return

    def local_first_download(repo_id, filename, *args, **kwargs):
        if repo_id == CCIP_MODEL_REPOSITORY:
            local_path = _ccip_local_file_for_repo_filename(filename)
            if local_path is not None:
                return str(local_path)
        return original(repo_id, filename, *args, **kwargs)

    local_first_download._vision_memory_local_loader = True
    ccip_module.hf_hub_download = local_first_download


def _activate_hf_endpoint(endpoint: str) -> None:
    """Make later imgutils downloads use the endpoint that actually worked."""
    os.environ["HF_ENDPOINT"] = endpoint
    try:
        constants = importlib.import_module("huggingface_hub.constants")
        if hasattr(constants, "ENDPOINT"):
            constants.ENDPOINT = endpoint
    except Exception as exc:  # pragma: no cover - old hub compatibility
        logger.debug("[VisionMemory][CCIP] 无法更新 Hugging Face 运行端点：%s", _clean(exc, 200))


def _download_ccip_file_direct(session: Any, endpoint: str, model: str, filename: str) -> None:
    """Download one model file without the Hub HEAD/metadata request."""
    target = _ccip_local_model_dir(model) / filename
    if target.is_file() and target.stat().st_size > 0:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"{endpoint}/{CCIP_MODEL_REPOSITORY}/resolve/main/{model}/{filename}"
    temporary = target.with_name(target.name + f".{uuid.uuid4().hex[:8]}.part")
    try:
        with session.get(
            url,
            stream=True,
            allow_redirects=True,
            timeout=(15, 60),
            headers={"User-Agent": "astrbot-plugin-vision-memory/1.10", "Accept-Encoding": "identity"},
        ) as response:
            response.raise_for_status()
            expected = int(response.headers.get("Content-Length", "0") or 0)
            written = 0
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
                        written += len(chunk)
            if written <= 0 or (expected and written != expected):
                raise IOError(f"下载文件不完整：预期 {expected} 字节，实际 {written} 字节。")
        os.replace(temporary, target)
        logger.info("[VisionMemory][CCIP] 直接下载完成：%s（%.1f MB）", filename, target.stat().st_size / 1024 / 1024)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass


def _direct_download_ccip_model(model: str) -> None:
    """Use plain HTTPS, with and without proxy variables, as a final fallback."""
    import requests

    last_exc: Exception | None = None
    for endpoint in _ccip_endpoints():
        for trust_env in (True, False):
            mode = "继承系统代理" if trust_env else "绕过代理变量"
            try:
                logger.info("[VisionMemory][CCIP] 正在直接下载模型，来源=%s，模式=%s", endpoint, mode)
                with requests.Session() as session:
                    session.trust_env = trust_env
                    for filename in CCIP_MODEL_FILES:
                        _download_ccip_file_direct(session, endpoint, model, filename)
                _install_ccip_local_loader()
                _CCIP_READY_MODELS.add(model)
                logger.info("[VisionMemory][CCIP] 本地模型目录已就绪：%s", _ccip_local_model_dir(model))
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "[VisionMemory][CCIP] 直接下载失败（%s，%s）：%s；将尝试下一方式。",
                    endpoint,
                    mode,
                    _ccip_exception_detail(exc),
                )
    raise RuntimeError("直接 HTTP 下载也未能准备完整 CCIP 模型。") from last_exc


def _ensure_ccip_model(model: str) -> None:
    """Prepare a model from local files, the HF cache, Hub, or direct HTTPS."""
    if model in _CCIP_READY_MODELS:
        return
    with _CCIP_MODEL_LOCK:
        if model in _CCIP_READY_MODELS:
            return
        if _ccip_local_model_complete(model):
            _install_ccip_local_loader()
            _CCIP_READY_MODELS.add(model)
            logger.info("[VisionMemory][CCIP] 使用本地模型目录：%s", _ccip_local_model_dir(model))
            return

        last_exc: Exception | None = None
        if hf_hub_download is not None:
            try:
                for filename in CCIP_MODEL_FILES:
                    hf_hub_download(
                        repo_id=CCIP_MODEL_REPOSITORY,
                        filename=f"{model}/{filename}",
                        local_files_only=True,
                    )
                _CCIP_READY_MODELS.add(model)
                logger.info("[VisionMemory][CCIP] Hugging Face 缓存完整，直接使用：%s", model)
                return
            except Exception:
                pass  # Some files are absent or stale; obtain a complete snapshot below.

            for endpoint in _ccip_endpoints():
                try:
                    logger.info("[VisionMemory][CCIP] 正在通过 %s 检查/下载模型 %s（3 个必要文件）", endpoint, model)
                    for filename in CCIP_MODEL_FILES:
                        hf_hub_download(
                            repo_id=CCIP_MODEL_REPOSITORY,
                            filename=f"{model}/{filename}",
                            endpoint=endpoint,
                        )
                    _activate_hf_endpoint(endpoint)
                    _CCIP_READY_MODELS.add(model)
                    logger.info("[VisionMemory][CCIP] 模型文件已就绪，来源：%s", endpoint)
                    return
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    logger.warning(
                        "[VisionMemory][CCIP] Hub 来源 %s 失败：%s；将尝试下一来源。",
                        endpoint,
                        _ccip_exception_detail(exc),
                    )

        try:
            _direct_download_ccip_model(model)
            return
        except Exception as exc:
            last_exc = exc
        detail = _ccip_exception_detail(last_exc)
        raise RuntimeError(
            "CCIP 模型准备失败。已检查本地目录和 Hugging Face 缓存，并尝试 Hub 与直接 HTTP 下载；"
            f"可通过 {CCIP_LOCAL_DIR_ENV} 指定离线模型目录。最后错误：" + detail
        ) from last_exc


def _ccip_extract_with_model_support(path: str, size: int, model: str) -> Any:
    _ensure_ccip_model(model)
    feature = ccip_extract_feature(path, size, model)
    _CCIP_VERIFIED_MODELS.add(model)
    return feature


_ccip_log_network_environment()


def _safe_id(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "_", _clean(value, 120))
    return text[:120] or uuid.uuid4().hex[:12]


def _file_hash(path: str) -> str:
    try:
        h=hashlib.sha256()
        with open(path,"rb") as fp:
            for chunk in iter(lambda: fp.read(1024*1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _pct(value: Any) -> float:
    try:
        x = float(value)
    except Exception:
        return 0.0
    if x > 1:
        x /= 100
    return max(0.0, min(1.0, x))


def _component_type(component: Any) -> str:
    value = getattr(component, "type", None)
    if value is not None:
        return str(getattr(value, "value", value)).lower()
    return component.__class__.__name__.lower()


def _is_image_component(component: Any) -> bool:
    if isinstance(component, Image):
        return True
    kind = _component_type(component)
    return "image" in kind or hasattr(component, "convert_to_file_path")


def _is_reply_component(component: Any) -> bool:
    if Reply is not object and isinstance(component, Reply):
        return True
    return "reply" in _component_type(component)


def _iter_children(value: Any, depth: int = 0):
    if depth > 5 or value is None:
        return
    if isinstance(value, dict):
        for v in value.values():
            yield v
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield v
    else:
        for attr in ("chain", "message", "message_chain", "message_obj", "quote", "content", "data"):
            try:
                v = getattr(value, attr, None)
            except Exception:
                v = None
            if v is not None and v is not value:
                yield v


class VisionMemoryPlugin(Star):
    """UptonKipling - three-tier image recognition memory for AstrBot."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or AstrBotConfig({})
        self.root = self._resolve_data_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.images_root = self.root / "images"
        self.images_root.mkdir(parents=True, exist_ok=True)
        self.db_file = self.root / "vision_memory.json"
        self.logs_file = self.root / "recognition_log.jsonl"
        self._lock = asyncio.Lock()
        self.data = self._load_data()
        self._ensure_defaults()
        self._repair_legacy_memory_names()
        self._pending_recognitions: dict[str, dict[str, Any]] = {}
        self._rebuild_jobs: dict[str, dict[str, Any]] = {}
        # AstrBot _conf_schema is the bootstrap source; the JSON store is the
        # runtime source so changes made in the custom Page take effect immediately.
        try:
            if isinstance(self.config, dict):
                for key in self.data["settings"]:
                    if key in self.config:
                        self.data["settings"][key] = self.config[key]
        except Exception:
            pass
        # v1.9 hard policy after AstrBot config merge too: legacy v1.8 values of
        # 0 must never disable the pending queue retention/cap.
        try:
            if int(self.data["settings"].get("pending_memory_retention_hours", 168) or 0) <= 0:
                self.data["settings"]["pending_memory_retention_hours"] = 168
        except Exception:
            self.data["settings"]["pending_memory_retention_hours"] = 168
        try:
            if int(self.data["settings"].get("context_learning_max_pending", 20) or 0) <= 0:
                self.data["settings"]["context_learning_max_pending"] = 20
        except Exception:
            self.data["settings"]["context_learning_max_pending"] = 20
        self.data["settings"]["auto_memory_enabled"] = False
        self._save_sync()
        self._register_web_routes()
        logger.info("[VisionMemory] 多模态视觉识图与图片记忆管理 %s loaded", VERSION)

    # ------------------------------------------------------------------
    # Persistent storage
    # ------------------------------------------------------------------
    def _resolve_data_dir(self) -> Path:
        try:
            if get_astrbot_plugin_data_path:
                path = get_astrbot_plugin_data_path()
                return Path(path) / PLUGIN_ID
        except Exception:
            pass
        try:
            from astrbot.api.star import StarTools
            return Path(StarTools.get_data_dir()) / PLUGIN_ID
        except Exception:
            return Path("data") / "plugin_data" / PLUGIN_ID

    def _blank_data(self) -> dict[str, Any]:
        return {
            "data_version": DATA_VERSION,
            "settings": {
                "enabled": True,
                "vision_provider_id": "",
                "fallback_to_current_provider": True,
                "reference_visual_verification_enabled": True,
                "ccip_enabled": True,
                "ccip_model": "ccip-caformer-24-randaug-pruned",
                "ccip_threshold": 0.17847511429108218,
                "ccip_top_candidates": 8,
                "ccip_reference_images_per_entry": 3,
                "ccip_strong_match_distance": 0.17847511429108218,
                "ccip_reject_distance": 0.30,
                "ccip_reference_feature_size": 384,
                "ccip_use_merged_entry_feature": True,
                "character_threshold": 0.42,
                "general_threshold": 0.45,
                "auto_memory_enabled": False,
                "pending_memory_retention_hours": 168,
                "context_learning_window_minutes": 360,
                "context_learning_max_pending": 20,
                "auto_memory_retention_days": 7,
                "max_images_per_entry": 24,
                "recognition_timeout_seconds": 45,
                "feature_extraction_timeout_seconds": 45,
                "max_candidates_per_library": 80,
                "log_recognition": True,
                "only_group_at_or_private": True,
                "self_character_name": "夏目安安",
            },
            "libraries": {key: [] for key in LIBRARIES},
            "recognition_log": [],
            "pending_confirmations": [],
            "context_ledger": {},
            "correction_log": [],
        }

    def _load_data(self) -> dict[str, Any]:
        if not self.db_file.exists():
            return self._blank_data()
        try:
            data = json.loads(self.db_file.read_text("utf-8"))
            if not isinstance(data, dict):
                return self._blank_data()
            base = self._blank_data()
            base.update(data)
            base.setdefault("settings", {}).update({k: v for k, v in data.get("settings", {}).items() if k in base["settings"]})
            for key in LIBRARIES:
                if not isinstance(base.get("libraries", {}).get(key), list):
                    base["libraries"][key] = []
            if not isinstance(base.get("pending_confirmations"), list):
                base["pending_confirmations"] = []
            if not isinstance(base.get("context_ledger"), dict):
                base["context_ledger"] = {}
            if not isinstance(base.get("correction_log"), list):
                base["correction_log"] = []
            # Migrate installations from v1: the old 75% general threshold was
            # intentionally too strict for second-creation / fan-art recognition.
            # New installs and upgraded installs both use the same 45% tolerant policy.
            try:
                old_version = int(data.get("data_version", 1) or 1)
            except Exception:
                old_version = 1
            if old_version < 2:
                base["settings"]["general_threshold"] = 0.45
                base["settings"]["pending_memory_retention_hours"] = 168
                base["settings"]["context_learning_window_minutes"] = 360
                base["settings"]["context_learning_max_pending"] = 20
                base["settings"]["auto_memory_enabled"] = False
                base["data_version"] = DATA_VERSION
            elif old_version < DATA_VERSION:
                # v1.2/v1.2.0 could leave malformed Bot-memory labels behind and
                # could not reliably parse model feature output. Keep user images
                # only when the label is clearly a generated sentence, and move
                # such entries out of the active Bot memory automatically.
                # The old UI also shipped a 75% general threshold; migrate that
                # exact legacy default to the current 45% fan-art-friendly default,
                # while preserving any other value the user deliberately chose.
                try:
                    if abs(float(base["settings"].get("general_threshold", 0.45)) - 0.75) < 1e-9:
                        base["settings"]["general_threshold"] = 0.45
                except Exception:
                    base["settings"]["general_threshold"] = 0.45
                # v1.9: unknown images remain administrator-managed, but the pending
                # queue keeps the original safety policy: 7-day retention and a
                # maximum of 20 pending images per conversation. This gives an
                # administrator time to name them while preventing unbounded growth.
                base["settings"]["pending_memory_retention_hours"] = 168
                base["settings"]["context_learning_window_minutes"] = max(30, int(base["settings"].get("context_learning_window_minutes", 360) or 360))
                base["settings"]["context_learning_max_pending"] = 20
                base["settings"]["auto_memory_enabled"] = False
                base["data_version"] = DATA_VERSION
            # v1.6 stores authoritative features per exact reference image. Keep old
            # aggregate text for compatibility, but never use it when real image rows exist.
            for lib_entries in base.get("libraries", {}).values():
                if not isinstance(lib_entries, list):
                    continue
                for entry in lib_entries:
                    if not isinstance(entry, dict):
                        continue
                    for image in entry.get("images", []) or []:
                        if isinstance(image, dict):
                            image.setdefault("features", "")
                            image.setdefault("ccip_feature", [])
                            image.setdefault("ccip_model", "")
                            image.setdefault("ccip_size", 0)
                    entry.setdefault("features_rebuilt_at", 0)
            base["settings"]["auto_memory_enabled"] = False
            # v1.9 invariant: pending unknown images expire after 7 days and are
            # capped at 20 per conversation. Never allow legacy v1.8 zeros to
            # silently disable either safety limit.
            try:
                if int(base["settings"].get("pending_memory_retention_hours", 168) or 0) <= 0:
                    base["settings"]["pending_memory_retention_hours"] = 168
            except Exception:
                base["settings"]["pending_memory_retention_hours"] = 168
            try:
                if int(base["settings"].get("context_learning_max_pending", 20) or 0) <= 0:
                    base["settings"]["context_learning_max_pending"] = 20
            except Exception:
                base["settings"]["context_learning_max_pending"] = 20
            base["data_version"] = DATA_VERSION
            return base
        except Exception as exc:
            logger.error("[VisionMemory] data load failed: %s", exc, exc_info=True)
            return self._blank_data()

    def _ensure_defaults(self) -> None:
        existing = {str(x.get("name")) for x in self.data["libraries"]["magic_trial_characters"] if isinstance(x, dict)}
        for name in CHARACTER_NAMES:
            if name not in existing:
                self.data["libraries"]["magic_trial_characters"].append(self._new_entry(name, "", builtin=True))

    def _new_entry(self, name: str, features: str = "", *, builtin: bool = False) -> dict[str, Any]:
        now = _now()
        return {
            "id": uuid.uuid4().hex,
            "name": _clean(name, 160) or "未命名事物",
            "features": _clean(features, 12000),
            "images": [],
            "builtin": bool(builtin),
            "created_at": now,
            "updated_at": now,
            "last_seen_at": now,
        }

    def _save_sync(self) -> None:
        tmp = self.db_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.db_file)

    async def _save(self) -> None:
        async with self._lock:
            self._save_sync()

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    def _setting(self, key: str, default: Any = None) -> Any:
        return self.data.get("settings", {}).get(key, default)

    def _provider_id(self, event: AstrMessageEvent | None = None) -> str:
        configured = _clean(self._setting("vision_provider_id", ""), 200)
        if configured:
            return configured
        if bool(self._setting("fallback_to_current_provider", True)) and event is not None:
            try:
                return str(self.context.get_current_chat_provider_id(umo=event.unified_msg_origin))
            except Exception:
                pass
        try:
            providers = self.context.get_all_providers()
            if providers:
                return str(providers[0].meta().id)
        except Exception:
            pass
        return ""

    async def _provider_id_async(self, event: AstrMessageEvent | None = None) -> str:
        configured = _clean(self._setting("vision_provider_id", ""), 200)
        if configured:
            return configured
        if bool(self._setting("fallback_to_current_provider", True)) and event is not None:
            try:
                return str(await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin))
            except Exception:
                pass
        return self._provider_id(None)

    # ------------------------------------------------------------------
    # Provider / vision model
    # ------------------------------------------------------------------
    def _provider_views(self) -> list[dict[str, Any]]:
        result = []
        try:
            providers = self.context.get_all_providers()
        except Exception:
            providers = []
        for provider in providers or []:
            try:
                meta = provider.meta()
                result.append({
                    "id": _clean(getattr(meta, "id", ""), 200),
                    "model": _clean(getattr(meta, "model", "") or getattr(meta, "name", ""), 160),
                    "name": _clean(getattr(meta, "name", "") or getattr(meta, "id", ""), 160),
                    "type": _clean(getattr(meta, "type", "") or provider.__class__.__name__, 100),
                })
            except Exception:
                continue
        return result

    async def _vision_call(self, image_path: str, prompt: str, event: AstrMessageEvent | None = None, timeout: int | None = None) -> str:
        provider_id = await self._provider_id_async(event)
        if not provider_id:
            raise RuntimeError("没有可用的 AstrBot 视觉模型 Provider。请在插件配置中选择模型。")
        provider = None
        try:
            provider = self.context.get_provider_by_id(provider_id)
        except Exception:
            provider = None
        if provider is None:
            raise RuntimeError(f"找不到视觉 Provider：{provider_id}")
        limit = timeout or int(self._setting("recognition_timeout_seconds", 45))
        logger.info("[VisionMemory] 调用视觉模型 provider=%s image=%s", provider_id, Path(image_path).name)
        started = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    image_urls=[image_path],
                    temperature=0.0,
                    max_tokens=1800,
                ),
                timeout=max(5, limit),
            )
            text = _clean(getattr(resp, "completion_text", ""), 20000)
            logger.info("[VisionMemory] 视觉模型完成 provider=%s elapsed=%dms chars=%d", provider_id, int((time.perf_counter() - started) * 1000), len(text))
            return text
        except Exception as exc:
            logger.error("[VisionMemory] 视觉模型调用失败 provider=%s error=%s", provider_id, _clean(exc, 500), exc_info=True)
            raise

    async def _vision_call_multi(self, image_paths: list[str], prompt: str, event: AstrMessageEvent | None = None, timeout: int | None = None) -> str:
        """Vision call with multiple images; safely falls back to single-image."""
        image_paths = [str(Path(x).resolve()) for x in image_paths if x and Path(x).exists()]
        if not image_paths:
            return ""
        if len(image_paths) == 1:
            return await self._vision_call(image_paths[0], prompt, event, timeout)
        provider_id = await self._provider_id_async(event)
        if not provider_id:
            raise RuntimeError("找不到视觉 Provider")
        limit = timeout or int(self._setting("recognition_timeout_seconds", 45))
        try:
            resp = await asyncio.wait_for(self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt, image_urls=image_paths, temperature=0.0, max_tokens=1800), timeout=max(5, limit))
            return _clean(getattr(resp, "completion_text", ""), 20000)
        except Exception as exc:
            logger.warning("[VisionMemory] 多图视觉调用失败，回退单图：%s", _clean(exc, 500))
            return await self._vision_call(image_paths[0], prompt, event, timeout)

    def _parse_model_text(self, raw: Any) -> str:
        """Normalize vision-model output into a stable feature string.

        Vision providers do not all return the same format: some return plain
        text, others wrap the answer in Markdown fences or JSON. The old
        implementation called this method but did not define it, which caused
        every upload/recognition feature extraction to fail with
        ``object has no attribute _parse_model_text``. Never leak parser errors
        into the stored feature descriptor.
        """
        if hasattr(raw, "completion_text"):
            raw = getattr(raw, "completion_text", "")
        text = _clean(raw, 20000)
        if not text:
            return ""
        # Strip common Markdown code fences without assuming JSON.
        fenced = re.fullmatch(r"\s*```(?:json|JSON|text|markdown)?\s*(.*?)\s*```\s*", text, re.S)
        if fenced:
            text = fenced.group(1).strip()
        # Try JSON first.
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            preferred = (
                "features", "识别关键特征", "visual_identity", "visual_identity_fingerprint",
                "identity_fingerprint", "summary", "摘要", "description", "描述", "result"
            )
            for key in preferred:
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    return _clean(value, 12000)
                if isinstance(value, (dict, list)):
                    return _clean(json.dumps(value, ensure_ascii=False, indent=2), 12000)
            # Preserve useful structured JSON instead of failing.
            return _clean(json.dumps(obj, ensure_ascii=False, indent=2), 12000)
        if isinstance(obj, list):
            return _clean(json.dumps(obj, ensure_ascii=False, indent=2), 12000)
        # Remove accidental assistant prefixes but keep the actual description.
        text = re.sub(r"^\s*(?:识别关键特征|视觉特征|特征摘要|分析结果)\s*[：:]\s*", "", text, flags=re.I)
        return _clean(text, 12000)

    async def _extract_features(self, image_path: str, library: str, event: AstrMessageEvent | None = None) -> str:
        # All three libraries use the same tolerant visual-identity philosophy.
        if library == "magic_trial_characters":
            prompt = '''你是《魔法少女的魔女审判》人物视觉资料整理器。请分析当前图片并生成可长期用于“角色身份匹配”的识别关键特征。
不要把当前画风、服装、动作、背景、构图当成角色身份本身。重点记录跨二创仍可能稳定的视觉身份：脸型与轮廓、眼睛形状/颜色、眉眼关系、鼻口比例、发色、发型、刘海/发际线、脸部标志、稳定配饰、角色整体色彩印象。服装与背景只能低权重辅助。
如果图片是截图、漫画、Q版、同人图、不同画师、换装、表情变化或不同角度，也照常提取身份特征。看不清的部分写“不确定”，不要猜人物姓名。输出中文结构化摘要。'''
        else:
            prompt = '''你是一个鲁棒的图片身份资料整理器。请为当前图片生成可用于长期匹配的“视觉身份指纹”。
不要要求未来图片必须与当前图片像素级一致，也不要因为换画师、二创、截图、裁切、不同姿势、不同背景、不同服装而否定同一人物/事物。
对于人物，优先记录脸部、发型、发色、眼睛、稳定配饰、体态与整体视觉印象；对于物品，优先记录轮廓、结构、材质、颜色组合、文字、独特标志；场景只作辅助。明确区分“稳定特征”和“易变化特征”，不猜测名称。输出中文结构化摘要。'''
        raw = await self._vision_call(image_path, prompt, event, int(self._setting("feature_extraction_timeout_seconds", 45)))
        return self._parse_model_text(raw)

    @staticmethod
    def _ccip_feature_to_list(feature: Any) -> list[float]:
        try:
            return [float(x) for x in feature.tolist()]
        except Exception:
            try:
                return [float(x) for x in feature]
            except Exception:
                return []

    @staticmethod
    def _ccip_feature_from_list(values: Any) -> Any:
        if not isinstance(values, list) or not values:
            return None
        try:
            import numpy as np
            return np.asarray(values, dtype="float32")
        except Exception:
            return None

    async def _ccip_extract_for_image(self, image: dict[str, Any], model: str, size: int) -> Any:
        path = _clean(image.get("path"), 1000)
        if not path or not Path(path).exists() or not CCIP_AVAILABLE:
            return None
        cached_model = _clean(image.get("ccip_model"), 100)
        cached_size = int(image.get("ccip_size", 0) or 0)
        cached = self._ccip_feature_from_list(image.get("ccip_feature"))
        if cached is not None and cached_model == model and cached_size == size:
            return cached
        feature = await asyncio.to_thread(_ccip_extract_with_model_support, path, size, model)
        image["ccip_feature"] = self._ccip_feature_to_list(feature)
        image["ccip_model"] = model
        image["ccip_size"] = size
        image["ccip_feature_updated_at"] = _now()
        return feature

    async def _ccip_prepare_entry(self, entry: dict[str, Any], model: str, size: int) -> list[dict[str, Any]]:
        refs=[]
        for index, image in enumerate(entry.get("images", []) or [], 1):
            if not isinstance(image, dict):
                continue
            path=_clean(image.get("path"),1000)
            if not path or not Path(path).exists():
                continue
            try:
                feat=await self._ccip_extract_for_image(image, model, size)
                if feat is not None:
                    refs.append({"index":index,"image":image,"feature":feat})
            except Exception as exc:
                logger.warning("[VisionMemory][CCIP] reference feature failed name=%s image=%s: %s", entry.get("name"), image.get("filename"), _clean(exc,300))
        return refs

    async def _rebuild_entry_features(self, entry: dict[str, Any], event: AstrMessageEvent | None = None) -> dict[str, Any]:
        """Re-read the actual reference images and rebuild image-level fingerprints.

        The old aggregate entry.features is retained only as a compatibility summary;
        every reference image becomes the authoritative unit for both Vision and CCIP.
        """
        images=[x for x in entry.get("images", []) or [] if isinstance(x,dict)]
        feature_blocks=[]; vision_ok=0; ccip_ok=0
        for index, image in enumerate(images,1):
            path=_clean(image.get("path"),1000)
            if not path or not Path(path).exists():
                continue
            try:
                feat=await self._extract_features(path, "magic_trial_characters" if entry.get("builtin") else "custom_objects", event)
                image["features"]=feat
                image["feature_updated_at"]=_now()
                if feat: vision_ok += 1; feature_blocks.append(f"【参考图 {index} · {image.get('filename','图片')}】\n{feat}")
            except Exception as exc:
                image["features"]=""
                logger.warning("[VisionMemory] rebuild Vision feature failed name=%s image=%s: %s", entry.get("name"), image.get("filename"), _clean(exc,300))
        if CCIP_AVAILABLE and bool(self._setting("ccip_enabled", True)):
            model=_clean(self._setting("ccip_model","ccip-caformer-24-randaug-pruned"),100)
            size=max(128,int(self._setting("ccip_reference_feature_size",384) or 384))
            ccip_refs=await self._ccip_prepare_entry(entry,model,size)
            ccip_ok=len(ccip_refs)
            try:
                if ccip_refs and ccip_merge:
                    merged=await asyncio.to_thread(ccip_merge,[x["feature"] for x in ccip_refs],size,model)
                    entry["ccip_merged_feature"]=self._ccip_feature_to_list(merged)
                    entry["ccip_merged_model"]=model
                    entry["ccip_merged_size"]=size
                    entry["ccip_merged_updated_at"]=_now()
                else:
                    entry.pop("ccip_merged_feature",None)
            except Exception as exc:
                entry.pop("ccip_merged_feature",None)
                logger.warning("[VisionMemory][CCIP] merge failed name=%s: %s",entry.get("name"),_clean(exc,300))
        entry["features"]="\n\n".join(feature_blocks)
        entry["features_rebuilt_at"]=_now()
        entry["updated_at"]=_now()
        return {"vision_images":vision_ok,"ccip_images":ccip_ok,"total_images":len(images)}

    async def _score_against_library(self, image_path: str, library_key: str, entries: list[dict[str, Any]], event: AstrMessageEvent | None = None) -> list[dict[str, Any]]:
        """Rank candidates using image-level reference data, then verify with real images.

        A reference image is the atomic unit. Its own Vision fingerprint and CCIP
        feature stay attached to that exact file, preventing cross-image feature mixups.
        """
        if not entries:
            return []
        candidates=[]
        limit=int(self._setting("max_candidates_per_library",80))
        for entry in entries[:limit]:
            images=entry.get("images") if isinstance(entry.get("images"),list) else []
            ref_rows=[]
            for idx,img in enumerate(images[:24],1):
                if not isinstance(img,dict): continue
                path=_clean(img.get("path"),1000)
                if path and Path(path).exists():
                    ref_rows.append({"index":idx,"filename":img.get("filename",""),"path":path,"features":_clean(img.get("features"),5000),"id":img.get("id")})
            # Legacy fallback is used only until the user rebuilds image-level data.
            legacy=_clean(entry.get("features"),7000) if not ref_rows else ""
            if not ref_rows and not legacy:
                continue
            candidates.append({"id":entry.get("id"),"name":entry.get("name"),"ref_rows":ref_rows,"legacy_features":legacy,"entry":entry})
        if not candidates: return []
        threshold=_pct(self._setting("character_threshold",0.42) if library_key=="magic_trial_characters" else self._setting("general_threshold",0.45))
        extra=("允许二创、Q版、截图、不同画师、换装、不同姿势；稳定脸部/五官/发型/头部轮廓优先，不能只因发色或服装相似就判同一人。" if library_key=="magic_trial_characters" else "允许二创、截图、裁切、不同画风、不同姿势；稳定身份结构优先。")
        compact=[]
        for c in candidates:
            refs=[{"reference_index":r["index"],"filename":r["filename"],"features":r["features"]} for r in c["ref_rows"] if r["features"]]
            compact.append({"id":c["id"],"name":c["name"],"references":refs,"legacy_features":c["legacy_features"]})
        prompt=("你是鲁棒视觉身份匹配器。根据第1张查询图与候选的逐张参考资料判断是否同一人物/事物。"
                "每个候选的参考资料按 reference_index 与实际图片一一对应，不要把不同参考图的信息混在一起。"
                "不要凭候选名称常识猜测。%s 阈值参考%s。输出严格JSON："
                "{\"matches\":[{\"id\":\"...\",\"name\":\"...\",\"match_score\":0.0,\"reason\":\"...\",\"best_reference_index\":1}]}。候选资料：%s"
                % (extra,f"{threshold:.0%}",json.dumps(compact,ensure_ascii=False)))
        raw=await self._vision_call(image_path,prompt,event)
        try: obj=json.loads(raw)
        except Exception:
            m=re.search(r"\{.*\}",raw,re.S)
            try: obj=json.loads(m.group(0)) if m else {}
            except Exception: obj={}
        stage=[]
        for item in obj.get("matches",[]) if isinstance(obj,dict) else []:
            if isinstance(item,dict):
                stage.append({"id":_clean(item.get("id"),120),"name":_clean(item.get("name"),160),"score":_pct(item.get("match_score",item.get("score",0))),"reason":_clean(item.get("reason"),700),"best_reference_index":int(item.get("best_reference_index",0) or 0)})
        stage.sort(key=lambda x:x["score"],reverse=True)
        lookup={c["id"]:c for c in candidates}

        # True multimodal verification: send the query plus up to 3 reference images
        # per candidate, rather than always using refs[0].
        verify=[x for x in stage[:5] if lookup.get(x["id"],{}).get("ref_rows")]
        if bool(self._setting("reference_visual_verification_enabled",True)) and verify:
            imgs=[image_path]; meta=[]
            for item in verify:
                c=lookup[item["id"]]; per=max(1,int(self._setting("ccip_reference_images_per_entry",3) or 3))
                selected=c["ref_rows"][:per]
                indexes=[]
                for r in selected:
                    imgs.append(r["path"]); indexes.append({"reference_index":r["index"],"image_index":len(imgs)})
                meta.append({"id":item["id"],"name":item["name"],"references":indexes})
            vp=("做真实参考图核验。第1张是查询图，后续图片严格按候选元数据中的 image_index 对应。"
                "每个候选可能有多张参考图，请综合这些真实图片判断身份，不要凭名称猜测。允许二创/换装/换画师/姿势变化；"
                "如果只有发色或服装相似而脸部/结构不同，应明显降分。输出严格JSON："
                "{\"matches\":[{\"id\":\"...\",\"name\":\"...\",\"match_score\":0.0,\"reason\":\"...\",\"best_reference_index\":1}]}。元数据：%s" % json.dumps(meta,ensure_ascii=False))
            try:
                raw2=await self._vision_call_multi(imgs,vp,event,int(self._setting("recognition_timeout_seconds",45)))
                try: obj2=json.loads(raw2)
                except Exception:
                    m=re.search(r"\{.*\}",raw2,re.S); obj2=json.loads(m.group(0)) if m else {}
                vm={}
                for item in obj2.get("matches",[]) if isinstance(obj2,dict) else []:
                    if isinstance(item,dict): vm[_clean(item.get("id"),120)]={"score":_pct(item.get("match_score",item.get("score",0))),"reason":_clean(item.get("reason"),700),"best_reference_index":int(item.get("best_reference_index",0) or 0)}
                for x in stage:
                    if x["id"] in vm:
                        x.update(vm[x["id"]]); x["reason"]="真实参考图核验："+x["reason"]
                stage.sort(key=lambda x:x["score"],reverse=True)
            except Exception as exc:
                logger.warning("[VisionMemory] 真实参考图核验失败，保留第一阶段结果：%s",_clean(exc,500))

        # CCIP: compare against exact reference features in every library. The model
        # is optimized for anime characters; other image types still retain Vision.
        ccip_enabled=bool(self._setting("ccip_enabled",True))
        if ccip_enabled:
            if not CCIP_AVAILABLE:
                logger.warning("[VisionMemory] CCIP 已启用，但 imgutils 未安装；本次跳过 CCIP，保留视觉模型识别。")
            elif stage:
                model=_clean(self._setting("ccip_model","ccip-caformer-24-randaug-pruned"),100)
                size=max(128,int(self._setting("ccip_reference_feature_size",384) or 384))
                cthr=float(self._setting("ccip_threshold",0.17847511429108218) or 0.17847511429108218)
                strong=float(self._setting("ccip_strong_match_distance",cthr) or cthr)
                reject=float(self._setting("ccip_reject_distance",0.30) or 0.30)
                topn=max(1,int(self._setting("ccip_top_candidates",8) or 8))
                per_entry=max(1,int(self._setting("ccip_reference_images_per_entry",3) or 3))
                try:
                    qfeat=await asyncio.to_thread(_ccip_extract_with_model_support,image_path,size,model)
                    logger.info("[VisionMemory][CCIP] query feature extracted model=%s size=%s",model,size)
                    for item in stage[:topn]:
                        c=lookup.get(item["id"],{}); rows=c.get("ref_rows",[])[:per_entry]
                        distances=[]
                        for r in rows:
                            ref_record=next((x for x in c.get("entry",{}).get("images",[]) if isinstance(x,dict) and x.get("id")==r.get("id")),None)
                            try:
                                feat=None
                                if ref_record is not None:
                                    feat=await self._ccip_extract_for_image(ref_record,model,size)
                                if feat is None:
                                    feat=await asyncio.to_thread(_ccip_extract_with_model_support,r["path"],size,model)
                                d=float(await asyncio.to_thread(ccip_difference,qfeat,feat,size,model))
                                distances.append({"reference_index":r["index"],"filename":r["filename"],"distance":d})
                            except Exception as exc:
                                logger.warning("[VisionMemory][CCIP] reference compare failed name=%s image=%s: %s",item.get("name"),r.get("filename"),_clean(exc,250))
                        if not distances: continue
                        distances.sort(key=lambda x:x["distance"])
                        vals=[x["distance"] for x in distances]
                        best=vals[0]; second=vals[1] if len(vals)>1 else best
                        # Best evidence + consistency bonus. We do not let one lucky
                        # reference completely dominate a wildly inconsistent set.
                        consistency=max(0.0,min(1.0,1.0-(max(0.0,second-best)/max(reject,1e-6))))
                        item["ccip_distances"]=[{**d,"distance":round(d["distance"],6)} for d in distances]
                        item["ccip_distance"]=round(best,6)
                        item["ccip_threshold"]=cthr
                        item["ccip_same"]=bool(best<=cthr)
                        item["ccip_best_reference_index"]=distances[0]["reference_index"]
                        item["ccip_consistency"]=round(consistency,3)
                        old=float(item.get("score",0.0))
                        if best<=strong:
                            boost=min(0.22,0.12+0.10*consistency)
                            item["score"]=max(old,min(1.0,old+boost))
                            item["reason"]=(item.get("reason","")+f"；CCIP强匹配：最佳参考图#{distances[0]['reference_index']} distance={best:.4f}<=threshold={cthr:.4f}").strip("；")
                        elif best>=reject:
                            item["score"]=min(old,0.20)
                            item["reason"]=(item.get("reason","")+f"；CCIP明显不匹配：最佳参考图#{distances[0]['reference_index']} distance={best:.4f}>={reject:.4f}").strip("；")
                        else:
                            item["reason"]=(item.get("reason","")+f"；CCIP最佳参考图#{distances[0]['reference_index']} distance={best:.4f}；多图一致性={consistency:.2f}").strip("；")
                        logger.info("[VisionMemory][CCIP] name=%s distances=%s best=%.6f threshold=%.6f same=%s score=%.1f%%",item.get("name"),json.dumps(item["ccip_distances"],ensure_ascii=False),best,cthr,item.get("ccip_same"),float(item.get("score",0))*100)
                    stage.sort(key=lambda x:x["score"],reverse=True)
                except Exception as exc:
                    logger.warning("[VisionMemory][CCIP] query failed，回退视觉模型：%s",_clean(exc,500),exc_info=True)
        return stage

    # ------------------------------------------------------------------
    # Image extraction from AstrBot messages
    # ------------------------------------------------------------------
    async def _component_to_path(self, component: Any) -> str | None:
        for method_name in ("convert_to_file_path", "to_file_path"):
            method = getattr(component, method_name, None)
            if callable(method):
                try:
                    value = method()
                    if asyncio.iscoroutine(value):
                        value = await value
                    if value and Path(str(value)).exists():
                        return str(Path(str(value)).resolve())
                except Exception:
                    pass
        for attr in ("path", "file", "url", "src"):
            value = getattr(component, attr, None)
            if value and isinstance(value, str):
                if value.startswith("file://"):
                    value = value[7:]
                if Path(value).exists():
                    return str(Path(value).resolve())
        # A few adapters expose a raw file field in toDict().
        try:
            raw = component.toDict()
            if isinstance(raw, dict):
                data = raw.get("data", raw)
                for key in ("path", "file", "url"):
                    value = data.get(key) if isinstance(data, dict) else None
                    if isinstance(value, str) and Path(value).exists():
                        return str(Path(value).resolve())
        except Exception:
            pass
        return None

    async def _collect_images(self, event: AstrMessageEvent) -> list[tuple[str, str]]:
        roots = []
        for attr in ("message_obj", "message_chain", "message", "message_components"):
            try:
                value = getattr(event, attr, None)
            except Exception:
                value = None
            if value is not None:
                roots.append(value)
        seen = set()
        found: list[tuple[str, str]] = []

        async def walk(value: Any, depth: int = 0):
            if depth > 5 or value is None or id(value) in seen:
                return
            seen.add(id(value))
            if _is_image_component(value):
                path = await self._component_to_path(value)
                if path:
                    found.append((path, "current"))
                return
            if isinstance(value, dict):
                for child in value.values():
                    await walk(child, depth + 1)
            elif isinstance(value, (list, tuple, set)):
                for child in value:
                    await walk(child, depth + 1)
            else:
                for child in _iter_children(value, depth):
                    await walk(child, depth + 1)

        for root in roots:
            await walk(root)
        # Deduplicate by real path.
        unique = []
        paths = set()
        for path, source in found:
            if path not in paths:
                paths.add(path)
                unique.append((path, source))
        return unique

    def _event_has_at_bot(self, event: AstrMessageEvent) -> bool:
        """Strictly detect @Bot in the CURRENT message only.

        Never recurse through Reply/Forward/quoted/history objects: an @Bot inside
        a quoted message must not make the current image eligible. Never use
        message_str as an @ detector either, because adapters may include wake
        words or rendered history there.
        """
        try:
            bot_id = str(event.get_self_id() or "")
        except Exception:
            bot_id = ""
        if not bot_id:
            return False

        components = []
        # Prefer AstrBot's current-message component list if available.
        getter = getattr(event, "get_messages", None)
        if callable(getter):
            try:
                value = getter()
                if isinstance(value, (list, tuple)):
                    components.extend(value)
            except Exception:
                pass

        msg_obj = getattr(event, "message_obj", None)
        for attr in ("message", "message_chain", "message_chain_obj"):
            value = getattr(msg_obj, attr, None) if msg_obj is not None else None
            if isinstance(value, (list, tuple)):
                components.extend(value)

        chain = getattr(event, "message_chain", None)
        if isinstance(chain, (list, tuple)):
            components.extend(chain)

        seen = set()
        for comp in components:
            if comp is None or id(comp) in seen:
                continue
            seen.add(id(comp))
            kind = _component_type(comp)
            if "at" not in kind and "mention" not in kind:
                continue
            target = getattr(comp, "qq", None)
            if target is None:
                target = getattr(comp, "target", None)
            if target is None:
                target = getattr(comp, "user_id", None)
            if isinstance(comp, dict):
                data = comp.get("data", comp)
                if isinstance(data, dict):
                    target = data.get("qq") or data.get("target") or data.get("user_id") or target
                target = comp.get("qq") or comp.get("target") or comp.get("user_id") or target
            if target is not None and str(target) == bot_id:
                logger.debug("[VisionMemory] 检测到当前消息真实 @Bot：%s", bot_id)
                return True
        return False

    def _is_bot_self_event(self, event: AstrMessageEvent) -> bool:
        """Reliably reject outbound Bot messages before any image/context learning.

        AstrBot exposes both self-id and sender-id on AstrMessageEvent. Comparing
        those first is much safer than relying on adapter-specific boolean helpers.
        """
        # Canonical AstrBot API: sender id == bot self id means this event was sent by Bot.
        try:
            sender = str(event.get_sender_id() or "")
            self_id = str(event.get_self_id() or "")
            if sender and self_id and sender == self_id:
                return True
        except Exception:
            pass
        for method_name in ("is_self", "is_bot_message"):
            method = getattr(event, method_name, None)
            if callable(method):
                try:
                    if bool(method()):
                        return True
                except Exception:
                    pass
        for attr in ("is_self", "is_bot_message", "from_self"):
            if bool(getattr(event, attr, False)):
                return True
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            if raw.get("self_id") and raw.get("user_id") and str(raw.get("self_id")) == str(raw.get("user_id")):
                return True
            sender_obj=raw.get("sender")
            if isinstance(sender_obj,dict) and raw.get("self_id") and sender_obj.get("user_id") and str(raw.get("self_id"))==str(sender_obj.get("user_id")):
                return True
        return False

    def _is_private(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:
            return bool(getattr(event, "message_type", "") in {"private", "friend"})

    # ------------------------------------------------------------------
    # Recognition pipeline
    # ------------------------------------------------------------------
    async def recognize(self, event: AstrMessageEvent, image_path: str, *, force: bool = False) -> dict[str, Any]:
        if not bool(self._setting("enabled", True)):
            return {"status": "disabled"}
        await self._purge_expired_memory()
        self._purge_context_ledger()
        result: dict[str, Any] = {
            "status": "ok", "image": Path(image_path).name, "matches": [], "best_match": None,
            "is_self": False, "features": "", "created_memory": None,
        }
        logger.info("[VisionMemory] ===== 开始识图 ===== image=%s force=%s", Path(image_path).name, force)
        # First create a fresh visual description. This is also what gets stored for auto memory.
        try:
            result["features"] = await self._extract_features(image_path, "magic_trial_characters", event)
        except Exception as exc:
            result["features"] = ""
            logger.warning("[VisionMemory] feature extraction failed: %s", _clean(exc, 500), exc_info=True)

        for library_key in LIBRARY_ORDER:
            entries = [x for x in self.data["libraries"].get(library_key, []) if isinstance(x, dict)]
            if not entries:
                continue
            try:
                matches = await self._score_against_library(image_path, library_key, entries, event)
            except Exception as exc:
                logger.error("[VisionMemory] library compare failed library=%s error=%s", library_key, _clean(exc, 500), exc_info=True)
                matches = []
            threshold = _pct(self._setting("character_threshold", 0.42) if library_key == "magic_trial_characters" else self._setting("general_threshold", 0.45))
            for match in matches[:5]:
                match["library"] = library_key
                match["threshold"] = threshold
                match["accepted"] = bool(match["score"] >= threshold)
                result["matches"].append(match)
                logger.info(
                    "[VisionMemory] 比对 library=%s name=%s score=%.1f%% threshold=%.1f%% accepted=%s reason=%s",
                    LIBRARIES[library_key], match["name"], match["score"] * 100, threshold * 100, match["accepted"], match["reason"],
                )
        # An explicit user correction is authoritative over a prior visual guess.
        current_hash=_file_hash(image_path)
        correction=None
        for row in reversed(self.data.get("correction_log",[]) or []):
            if current_hash and row.get("image_hash")==current_hash and _now()-float(row.get("timestamp",0) or 0) <= max(5,int(self._setting("context_learning_window_minutes",360)))*60:
                correction=row; break
        if correction:
            cname=_clean(correction.get("name"),80)
            result["best_match"]={"id":"correction","name":cname,"score":1.0,"reason":"用户明确纠正覆盖此前识别","library":"bot_memory","threshold":_pct(self._setting("general_threshold",0.45)),"accepted":True}
            result["matches"]=[result["best_match"]]+[m for m in result["matches"] if m.get("name")!=cname]
            result["is_self"]=cname==_clean(self._setting("self_character_name","夏目安安"),80)
            logger.info("[VisionMemory] ★ 使用最近用户纠正覆盖本次识别：%s",cname)
            await self._save(); self._append_log(result,event)
            return result
        accepted = [m for m in result["matches"] if m.get("accepted")]
        accepted.sort(key=lambda x: x.get("score", 0), reverse=True)
        if accepted:
            result["best_match"] = accepted[0]
            if accepted[0]["name"] == _clean(self._setting("self_character_name", "夏目安安"), 80):
                result["is_self"] = True
                logger.info("[VisionMemory] ★ 图片人物判定为 Bot 自身：%s", accepted[0]["name"])
            # Mark matching auto-memory entries as seen.
            for match in accepted:
                if match["library"] == "bot_memory":
                    self._touch_entry("bot_memory", match["id"])
        else:
            # Unknown images are ALWAYS retained as unnamed pending items.
            # The bot is never allowed to infer a name from conversation history,
            # its own reply, or later user messages. Only an administrator using
            # the WebUI can assign a name and promote the image into Bot图片库.
            self._remember_pending_image(event, image_path, result)
            result["pending_memory"] = True
            logger.info("[VisionMemory] 未知图片已保留为【待管理员命名】；不会根据上下文自动命名或入 Bot 图片库")
        self._purge_pending()
        await self._save()
        self._append_log(result, event)
        logger.info("[VisionMemory] ===== 识图结束 ===== best=%s self=%s", (result.get("best_match") or {}).get("name"), result.get("is_self"))
        return result

    def _touch_entry(self, library: str, entry_id: str) -> None:
        for entry in self.data["libraries"].get(library, []):
            if entry.get("id") == entry_id:
                entry["last_seen_at"] = _now()
                entry["updated_at"] = _now()
                return

    def _copy_image(self, source: str, target_dir: Path, filename: str | None = None) -> dict[str, Any] | None:
        try:
            src = Path(source)
            if not src.exists():
                return None
            suffix = src.suffix.lower() or ".jpg"
            dest_name = _safe_id(filename or src.stem) + suffix
            dest = target_dir / f"{uuid.uuid4().hex[:8]}_{dest_name}"
            shutil.copy2(src, dest)
            return {"id": uuid.uuid4().hex, "filename": dest.name, "path": str(dest), "created_at": _now(), "features": "", "sha256": _file_hash(str(dest))}
        except Exception as exc:
            logger.warning("[VisionMemory] copy image failed: %s", _clean(exc, 300))
            return None

    def _append_log(self, result: dict[str, Any], event: AstrMessageEvent | None) -> None:
        if not bool(self._setting("log_recognition", True)):
            return
        item = {
            "timestamp": _now(),
            "image": result.get("image"),
            "best_match": result.get("best_match"),
            "is_self": result.get("is_self", False),
            "created_memory": result.get("created_memory"),
            "matches": result.get("matches", [])[:10],
        }
        try:
            with self.logs_file.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            pass
        logs = self.data.setdefault("recognition_log", [])
        logs.insert(0, item)
        del logs[100:]

    async def _purge_expired_memory(self) -> int:
        days = max(1, int(self._setting("auto_memory_retention_days", 7)))
        cutoff = _now() - days * 86400
        removed = 0
        entries = self.data["libraries"].get("bot_memory", [])
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if float(entry.get("last_seen_at", entry.get("updated_at", 0)) or 0) < cutoff:
                for image in entry.get("images", []) or []:
                    try:
                        Path(image.get("path", "")).unlink(missing_ok=True)
                    except Exception:
                        pass
                shutil.rmtree(self.images_root / "bot_memory" / str(entry.get("id")), ignore_errors=True)
                removed += 1
            else:
                kept.append(entry)
        if removed:
            self.data["libraries"]["bot_memory"] = kept
            logger.info("[VisionMemory] 遗忘清理：删除 Bot 图片库 %d 个超过 %d 天未使用的栏目", removed, days)
            self._save_sync()
        return removed

    # ------------------------------------------------------------------
    # Unknown-image pending memory / contextual naming
    # ------------------------------------------------------------------
    def _conversation_key(self, event: AstrMessageEvent | None) -> str:
        # Group conversations share one queue so another participant can answer
        # who/what an earlier image depicts. Private chats remain session-scoped.
        if event is None:
            return "unknown"
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = str(getattr(event, "group_id", "") or "")
        if gid:
            platform = _clean(getattr(event, "platform", "") or getattr(getattr(event, "message_obj", None), "platform", ""), 40)
            return f"group:{platform}:{gid}"
        return _clean(getattr(event, "unified_msg_origin", "") or "unknown", 300)

    def _session_key(self, event: AstrMessageEvent | None) -> str:
        return self._conversation_key(event)

    def _pending_items(self) -> list[dict[str, Any]]:
        items = self.data.setdefault("pending_confirmations", [])
        if not isinstance(items, list):
            items = []
            self.data["pending_confirmations"] = items
        return items

    def _remember_context_image(self, event: AstrMessageEvent, image_path: str, result: dict[str, Any]) -> dict[str, Any] | None:
        root=self.images_root/"context"; root.mkdir(parents=True,exist_ok=True)
        copied=self._copy_image(image_path,root,Path(image_path).stem+"_context")
        if not copied: return None
        copied.update({"timestamp":_now(),"source_image":Path(image_path).name,"best_match":result.get("best_match"),"features":_clean(result.get("features"),12000)})
        return copied

    def _attach_context_copy(self, event: AstrMessageEvent, context_copy: dict[str, Any]) -> None:
        """Attach the durable context image to the conversation ledger safely."""
        key=self._conversation_key(event)
        ledger=self.data.setdefault("context_ledger",{})
        rows=ledger.setdefault(key,[])
        # The current message row is normally already created by the ALL-event hook.
        # Update the newest row instead of creating duplicate image records.
        if rows:
            row=rows[-1]
            if isinstance(row,dict):
                imgs=row.setdefault("images",[])
                cid=context_copy.get("id")
                if not any(isinstance(x,dict) and x.get("id")==cid for x in imgs):
                    imgs.append(context_copy)
                return
        rows.append({"timestamp":_now(),"message_id":"","sender_id":"","text":"","images":[context_copy]})

    def _remember_pending_image(self, event: AstrMessageEvent, image_path: str, result: dict[str, Any]) -> None:
        key = self._conversation_key(event)
        items = self._pending_items()
        same = [x for x in items if x.get("session_key") == key]
        max_pending = max(1, int(self._setting("context_learning_max_pending", 20) or 20))
        # Keep a bounded administrator queue. The oldest pending image in the same
        # conversation is discarded when the cap is reached.
        if len(same) >= max_pending:
            oldest = min(same, key=lambda x: float(x.get("created_at", 0) or 0))
            items.remove(oldest)
            try: Path(oldest.get("image_path", "")).unlink(missing_ok=True)
            except Exception: pass
        pending_id = uuid.uuid4().hex
        pending_dir = self.images_root / "pending" / pending_id
        pending_dir.mkdir(parents=True, exist_ok=True)
        copied = self._copy_image(image_path, pending_dir, "pending")
        if not copied:
            return
        try:
            sender_id = event.get_sender_id()
        except Exception:
            sender_id = ""
        item = {
            "id": pending_id,
            "created_at": _now(),
            "session_key": key,
            "message_id": _clean(getattr(getattr(event, "message_obj", None), "message_id", ""), 120),
            "sender_id": _clean(sender_id, 120),
            "source_name": _clean(getattr(event, "message_str", "") if event else "", 1000),
            "image_path": copied.get("path"),
            "image_filename": copied.get("filename"),
            "features": _clean(result.get("features"), 12000),
            "result": {"best_match": result.get("best_match"), "matches": result.get("matches", [])[:10]},
        }
        items.append(item)
        self._save_sync()
        logger.info("[VisionMemory] 未知图片进入待确认队列 session=%s pending_id=%s", key, pending_id)

    def _purge_context_ledger(self)->None:
        ledger=self.data.get("context_ledger",{})
        if not isinstance(ledger,dict): return
        cutoff=_now()-max(60,int(self._setting("context_learning_window_minutes",360) or 360))*60
        for key in list(ledger):
            rows=ledger.get(key,[]) if isinstance(ledger.get(key),list) else []
            kept=[]
            for row in rows[-120:]:
                if float(row.get("timestamp",0) or 0)>=cutoff:
                    kept.append(row); continue
                for img in row.get("images",[]) or []:
                    path=_clean(img.get("path"),1000) if isinstance(img,dict) else ""
                    if path and str(self.images_root/"context") in path:
                        try: Path(path).unlink(missing_ok=True)
                        except Exception: pass
            if kept: ledger[key]=kept
            else: ledger.pop(key,None)

    def _purge_pending(self) -> None:
        hours = max(1, int(self._setting("pending_memory_retention_hours", 168) or 168))
        max_pending = max(1, int(self._setting("context_learning_max_pending", 20) or 20))
        cutoff = _now() - hours * 3600
        active = []
        for item in self._pending_items():
            if float(item.get("created_at", 0) or 0) < cutoff:
                try: Path(item.get("image_path", "")).unlink(missing_ok=True)
                except Exception: pass
                shutil.rmtree(self.images_root / "pending" / str(item.get("id")), ignore_errors=True)
            else:
                active.append(item)

        # Enforce the 20-item-per-conversation cap even for items that were
        # already present before this plugin version was installed.
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in active:
            grouped.setdefault(str(item.get("session_key", "unknown")), []).append(item)
        kept: list[dict[str, Any]] = []
        for key, rows in grouped.items():
            rows.sort(key=lambda x: float(x.get("created_at", 0) or 0), reverse=True)
            keep_rows = rows[:max_pending]
            drop_rows = rows[max_pending:]
            for item in drop_rows:
                try: Path(item.get("image_path", "")).unlink(missing_ok=True)
                except Exception: pass
                shutil.rmtree(self.images_root / "pending" / str(item.get("id")), ignore_errors=True)
            kept.extend(keep_rows)
        kept.sort(key=lambda x: float(x.get("created_at", 0) or 0))
        self.data["pending_confirmations"] = kept

    def _known_names(self) -> list[str]:
        names = []
        for lib in LIBRARIES:
            names.extend(_clean(x.get("name"), 80) for x in self.data["libraries"].get(lib, []) if isinstance(x, dict))
        # Known names are useful for aliases, but malformed old Bot-memory labels
        # must never become aliases.
        return sorted({x for x in names if x and not self._is_suspicious_memory_name(x)}, key=len, reverse=True)

    @staticmethod
    def _is_suspicious_memory_name(name: str) -> bool:
        name = _clean(name, 120)
        if not name:
            return True
        bad_fragments = (
            "这张图", "这个角色", "片中的人物", "图片中的人", "图片里", "图里没",
            "没名字", "没有名字", "不知道", "不认识", "没见过", "认不出来",
            "如果一张图片", "看不出", "无法识别", "不起……", "不起..."
        )
        if any(x in name for x in bad_fragments):
            return True
        # A real display name can be long, but a sentence-like label usually
        # contains punctuation or obvious sentence-ending wording.
        if len(name) > 28 and re.search(r"[。！？!?，,:：]", name):
            return True
        return False

    def _repair_legacy_memory_names(self) -> int:
        """Remove only obviously sentence-like legacy auto labels.

        v1.2 had installations that already contained names generated from Bot
        replies. Those entries should not contaminate future candidate matching.
        The repair is intentionally conservative and only targets unmistakable
        sentence fragments. Images are deleted with the bad memory because the
        image itself has no trustworthy identity label.
        """
        entries = self.data["libraries"].get("bot_memory", [])
        kept = []
        removed = 0
        for entry in entries:
            name = _clean(entry.get("name"), 120) if isinstance(entry, dict) else ""
            if isinstance(entry, dict) and self._is_suspicious_memory_name(name):
                for image in entry.get("images", []) or []:
                    try:
                        Path(image.get("path", "")).unlink(missing_ok=True)
                    except Exception:
                        pass
                shutil.rmtree(self.images_root / "bot_memory" / str(entry.get("id")), ignore_errors=True)
                removed += 1
                continue
            kept.append(entry)
        if removed:
            self.data["libraries"]["bot_memory"] = kept
            logger.warning("[VisionMemory] 已清理 %d 个旧版异常 Bot 图片库名称，避免污染后续识别", removed)
            self._save_sync()
        return removed

    def _extract_identity_claims(self, text: str) -> list[dict[str, Any]]:
        """Extract only high-confidence, image-scoped identity assertions.

        Crucially, this parser does NOT treat arbitrary Bot prose as a label.
        It accepts explicit user-like statements such as “这个角色叫做洛茜”
        and “刚才那张就是洛茜”, while rejecting questions and uncertainty.
        """
        text = _clean(text, 12000)
        if not text:
            return []
        claims: list[dict[str, Any]] = []
        # Split long prompts/history into sentence-ish chunks so an unrelated
        # later sentence cannot accidentally become the label.
        chunks = [x.strip() for x in re.split(r"[\n\r]+|(?<=[。！？!?])", text) if x.strip()]
        if not chunks:
            chunks = [text]
        known_names = self._known_names()
        unknown_reject = r"(?:谁|什么|吗|是不是|是否|可能|好像|似乎|大概|应该|不确定|不知道|认不出|认不出来|看不出|不像|像不像|猜|感觉|估计)"
        patterns = [
            # Explicit image/character scope.
            (r"(?:这个|这|那|刚才|上一张|前一张)?\s*(?:角色|人物|人|女生|男生|女孩|男孩)\s*(?:叫做|叫|名叫|名字是|是|为)\s*([\u4e00-\u9fffA-Za-z0-9_· .-]{1,32})", "character"),
            (r"(?:这张(?:图|图片)?|这幅(?:图|画)?|图里|图片里|照片里|截图里|画的是|画的就是|里面的是|这人是|这是|那张(?:图|图片)?|刚才那张|上一张|前一张)\s*(?:就是|是|叫做|叫|为)?\s*([\u4e00-\u9fffA-Za-z0-9_· .-]{1,32})", "image"),
            (r"(?:图片中的人|图片中的人物|图中的人|图中的人物|图上的是|图上的人)\s*(?:就是|是|叫做|叫|为)\s*([\u4e00-\u9fffA-Za-z0-9_· .-]{1,32})", "image"),
            (r"(?:记住|记一下|记着)[，,:： ]*(?:这张(?:图|图片)?|刚才那张|上一张|这个角色|这个人物)?\s*(?:就是|是|叫做|叫|为)\s*([\u4e00-\u9fffA-Za-z0-9_· .-]{1,32})", "remember"),
            # Explicit correction: 不是汉娜，是塔菲 / 认错了，是塔菲。
            (r"(?:不是|并不是)(?:[^，。！？!?]{0,24})?[，,：: ]+(?:而是|其实是|是)\s*([\u4e00-\u9fffA-Za-z0-9_· .-]{1,32})", "correction"),
            (r"(?:认错了|你认错了|识别错了|看错了|记错了)[，,:： ]*(?:(?:这个角色|这个人物|这张图|刚才那张|上一张|前一张|图里|图片里|其实)\s*)?(?:就是|是|叫做|叫|为)\s*([\u4e00-\u9fffA-Za-z0-9_· .-]{1,32})", "correction"),
            # Natural confirmation after a preceding image-focused exchange.
            (r"(?:对|对的|没错|就是|确实)[，,:： ]*(?:他|她|它|这个角色|这个人)?[，,:： ]*(?:就是|是|叫做|叫|为)?\s*([\u4e00-\u9fffA-Za-z0-9_· .-]{1,32})", "confirm"),
        ]
        for chunk in chunks:
            # Ignore quoted bot/assistant text in common transcript formats.
            clean_chunk = re.sub(r"^\s*(?:Bot|机器人|助手|Assistant|夏目安安)\s*[：:>]\s*", "", chunk, flags=re.I)
            if not clean_chunk:
                continue
            # “这个角色叫做洛茜，记住了吗？” is an assertion followed by a
            # request for acknowledgement, so strip that tail before deciding
            # whether the whole sentence is a question.
            assertion_chunk = re.sub(r"[，,、：:]?\s*(?:记住了吗|记得吗|知道了吗|对吧|可以吗)\s*[？?]?$", "", clean_chunk).strip()
            # Never turn uncertainty/opinion/questions into authoritative memory.
            if re.search(r"(?:我觉得|我认为|感觉|看起来|可能|好像|似乎|大概|应该|猜一下|不确定|不知道|认不出来|看不出)", assertion_chunk):
                continue
            if re.search(r"(?:吗|呢|吧)\s*[？?]?$", assertion_chunk) or re.search(r"[？?]$", assertion_chunk):
                continue
            clean_chunk = assertion_chunk or clean_chunk
            for pat, kind in patterns:
                for m in re.finditer(pat, clean_chunk, re.I):
                    phrase = _clean(m.group(1), 60).strip(" \"'“”‘’。！？!?,，：:；;。")
                    if not phrase or re.search(unknown_reject, phrase):
                        continue
                    # Trim trailing conversational words.
                    phrase = re.split(r"(?:记住了吗|记得吗|对吧|可以吗|知道了吗|好不好)$", phrase)[0].strip()
                    if not phrase or len(phrase) > 32:
                        continue
                    resolved = None
                    for known in known_names:
                        if phrase == known or (len(phrase) >= 2 and phrase in known):
                            resolved = known
                            break
                    name = resolved or phrase
                    claims.append({"name": name, "kind": kind, "evidence": clean_chunk, "confidence": 0.99 if kind in {"character", "image", "remember", "correction"} else 0.88})
        # Keep only unique claims, latest occurrence wins.
        unique = {}
        for claim in claims:
            unique[claim["name"]] = claim
        return list(unique.values())

    def _extract_identity_claim(self, text: str) -> str | None:
        claims = self._extract_identity_claims(text)
        return claims[-1]["name"] if claims else None

    def _pending_target(self, session_key: str, text: str) -> dict[str, Any] | None:
        candidates = [x for x in self._pending_items() if x.get("session_key") == session_key]
        if not candidates:
            return None
        candidates.sort(key=lambda x: float(x.get("created_at", 0) or 0), reverse=True)
        # Explicit ordinal references are strongest.
        m = re.search(r"(?:第\s*([一二三四五六七八九十\d]+)\s*张|(?:图片|图)\s*([一二三四五六七八九十\d]+))", text)
        if m:
            raw = m.group(1) or m.group(2)
            try:
                idx = int(raw)
            except Exception:
                idx = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}.get(raw, 0)
            if 1 <= idx <= len(candidates):
                return list(reversed(candidates))[idx - 1]
        # “上一张/刚才那张/这张” normally refers to the newest pending image.
        if re.search(r"(?:上一张|前一张|刚才那张|这张|这个角色|这个人物|这人|图里|图片里)", text):
            return candidates[0]
        # A bare explicit identity claim is only allowed when there is exactly
        # one pending item; otherwise guessing which image is dangerous.
        return candidates[0] if len(candidates) == 1 else None

    def _remember_context_message(self,event:AstrMessageEvent,text:str,image_records:list[dict[str,Any]]|None=None)->None:
        text=_clean(text,4000)
        if not text and not image_records: return
        key=self._conversation_key(event); ledger=self.data.setdefault("context_ledger",{})
        if not isinstance(ledger,dict): ledger={}; self.data["context_ledger"]=ledger
        rows=ledger.setdefault(key,[])
        rows.append({"timestamp":_now(),"message_id":_clean(getattr(getattr(event,"message_obj",None),"message_id",""),120),"sender_id":_clean(event.get_sender_id() if hasattr(event,"get_sender_id") else "",120),"text":text,"images":image_records or []})
        cutoff=_now()-max(30,int(self._setting("context_learning_window_minutes",360) or 360))*60
        ledger[key]=[x for x in rows[-120:] if float(x.get("timestamp",0) or 0)>=cutoff]
        if len(ledger)>50:
            oldest=min(ledger,key=lambda k:max([float(x.get("timestamp",0) or 0) for x in ledger[k]] or [0])); ledger.pop(oldest,None)

    def _recent_context_images(self,event:AstrMessageEvent|None)->list[dict[str,Any]]:
        key=self._conversation_key(event) if event else "unknown"; ledger=self.data.get("context_ledger",{})
        rows=ledger.get(key,[]) if isinstance(ledger,dict) else []; cutoff=_now()-max(5,int(self._setting("context_learning_window_minutes",360) or 360))*60
        out=[]
        for row in rows:
            if float(row.get("timestamp",0) or 0)<cutoff: continue
            for img in row.get("images",[]) or []:
                if isinstance(img,dict) and img.get("path") and Path(img.get("path")).exists(): out.append(img)
        out.sort(key=lambda x:float(x.get("timestamp",0) or 0),reverse=True); return out

    def _context_text_for_learning(self, event: AstrMessageEvent | None, fallback: str = "") -> str:
        key = self._conversation_key(event) if event else "unknown"
        ledger = self.data.get("context_ledger", {})
        rows = ledger.get(key, []) if isinstance(ledger, dict) else []
        if rows:
            return "\n".join(_clean(x.get("text"), 3000) for x in rows[-40:] if isinstance(x, dict))
        return fallback

    async def _promote_pending_to_memory(self, pending_id: str, name: str, source: str) -> bool:
        self._purge_pending()
        items = self._pending_items()
        item = next((x for x in items if x.get("id") == pending_id), None)
        if not item:
            return False
        image_path = _clean(item.get("image_path"), 1000)
        if not image_path or not Path(image_path).exists():
            items[:] = [x for x in items if x.get("id") != pending_id]
            return False
        name = _clean(name, 80).strip(" \"'“”‘’。！？!?,，：:；;")
        if not name or self._is_suspicious_memory_name(name):
            logger.warning("[VisionMemory] 拒绝异常上下文名称：%s", name)
            return False
        entry = next((x for x in self.data["libraries"]["bot_memory"] if _clean(x.get("name"), 80) == name), None)
        if entry is None:
            entry = self._new_entry(name, _clean(item.get("features"), 12000), builtin=False)
            entry["memory_source"] = "context_confirmed"
            self.data["libraries"]["bot_memory"].append(entry)
        else:
            old = _clean(entry.get("features"), 10000)
            feat = _clean(item.get("features"), 12000)
            if feat and feat not in old:
                entry["features"] = (old + "\n\n--- 上下文确认的新参考 ---\n" + feat).strip()
        entry["memory_reason"] = source
        entry["confirmed_name"] = name
        entry["confirmed_at"] = _now()
        entry["last_seen_at"] = _now()
        entry["updated_at"] = _now()
        target = self.images_root / "bot_memory" / entry["id"]
        target.mkdir(parents=True, exist_ok=True)
        copied = self._copy_image(image_path, target, Path(image_path).stem)
        if copied:
            copied["features"] = _clean(item.get("features"), 12000)
            entry.setdefault("images", []).append(copied)
            max_images = max(1, int(self._setting("max_images_per_entry", 24)))
            while len(entry["images"]) > max_images:
                old_img = entry["images"].pop(0)
                try: Path(old_img.get("path", "")).unlink(missing_ok=True)
                except Exception: pass
        items[:] = [x for x in items if x.get("id") != pending_id]
        try: Path(image_path).unlink(missing_ok=True)
        except Exception: pass
        shutil.rmtree(self.images_root / "pending" / str(pending_id), ignore_errors=True)
        await self._save()
        logger.info("[VisionMemory] ★ 上下文确认成功：pending=%s -> Bot图片库[%s] source=%s", pending_id, name, source)
        return True

    async def _promote_context_image_to_memory(self,event:AstrMessageEvent,image_record:dict[str,Any],name:str,source:str)->bool:
        name=_clean(name,80).strip(" \"'“”‘’。！？!?,，：:；;")
        if not name or self._is_suspicious_memory_name(name): return False
        path=_clean(image_record.get("path"),1000)
        if not path or not Path(path).exists(): return False
        if not _clean(image_record.get("features"),12000):
            try:
                image_record["features"]=await self._extract_features(path,"custom_objects",event)
            except Exception as exc:
                logger.warning("[VisionMemory] 纠正入库时特征补提失败：%s",_clean(exc,300))
        entry=next((x for x in self.data["libraries"]["bot_memory"] if _clean(x.get("name"),80)==name),None)
        if entry is None:
            entry=self._new_entry(name,_clean(image_record.get("features"),12000),builtin=False); self.data["libraries"]["bot_memory"].append(entry)
        else:
            feat=_clean(image_record.get("features"),12000)
            if feat and feat not in _clean(entry.get("features"),12000): entry["features"]=(entry.get("features","")+"\n\n--- 用户纠正参考特征 ---\n"+feat).strip()
        entry.update({"memory_source":"explicit_correction","memory_reason":source,"confirmed_name":name,"confirmed_at":_now(),"last_seen_at":_now(),"updated_at":_now()})
        target=self.images_root/"bot_memory"/entry["id"]; target.mkdir(parents=True,exist_ok=True)
        source_hash=_file_hash(path)
        existing_hashes={str(x.get("sha256")) for x in entry.get("images",[]) or [] if isinstance(x,dict)}
        copied=None if source_hash and source_hash in existing_hashes else self._copy_image(path,target,Path(path).stem+"_confirmed")
        if copied:
            copied["features"]=_clean(image_record.get("features"),12000); copied["confirmation_source"]=source; copied["sha256"]=source_hash; entry.setdefault("images",[]).append(copied)
            max_images=max(1,int(self._setting("max_images_per_entry",24)))
            while len(entry["images"])>max_images:
                old=entry["images"].pop(0)
                try: Path(old.get("path","")).unlink(missing_ok=True)
                except Exception: pass
        self.data.setdefault("correction_log",[]).append({"timestamp":_now(),"session_key":self._conversation_key(event),"name":name,"previous_match":image_record.get("best_match"),"image_hash":_file_hash(path),"source":source})
        self.data["correction_log"]=self.data["correction_log"][-100:]
        logger.info("[VisionMemory] ★ 显式纠错覆盖识别：%s -> %s",(image_record.get("best_match") or {}).get("name"),name)
        await self._save(); return True

    async def _try_explicit_correction(self,event:AstrMessageEvent,text:str,source:str,current_images:list[tuple[str,str]]|None=None)->bool:
        claims=self._extract_identity_claims(text)
        if not claims: return False
        claim=claims[-1]; records=[]
        for path,_ in current_images or []: records.append({"path":path,"timestamp":_now(),"features":"","best_match":None})
        records.extend(self._recent_context_images(event))
        if not records: return False
        return await self._promote_context_image_to_memory(event,records[0],claim["name"],source)

    async def _try_context_learning(self, event: AstrMessageEvent, text: str, source: str) -> bool:
        self._purge_pending()
        context = self._context_text_for_learning(event, text)
        claims = self._extract_identity_claims(context)
        if not claims:
            return False
        window = max(5, int(self._setting("context_learning_window_minutes", 360))) * 60
        pending = [x for x in self._pending_items() if x.get("session_key") == self._conversation_key(event) and _now() - float(x.get("created_at", 0) or 0) <= window]
        if not pending:
            return False
        # Use the most recent explicit claim. If it contains an image reference,
        # resolve the referenced pending image; otherwise only auto-promote when
        # exactly one pending image is active to avoid cross-image contamination.
        claim = claims[-1]
        target = self._pending_target(self._conversation_key(event), claim.get("evidence", ""))
        if target is None and len(pending) == 1:
            target = pending[0]
        if not target:
            logger.warning("[VisionMemory] 捕获到身份说明但无法安全定位对应图片，保留待确认：%s", claim.get("evidence"))
            return False
        logger.info("[VisionMemory] ★ 捕获明确身份说明 name=%s confidence=%.2f pending=%s evidence=%s", claim["name"], claim["confidence"], target.get("id"), _clean(claim.get("evidence"), 300))
        return await self._promote_pending_to_memory(target["id"], claim["name"], source)

    # NOTE: Deliberately no broad message listener here.
    # Older versions watched every message and tried to bind names from later
    # conversation context. That design is disabled permanently in v1.8: the
    # plugin does not learn Bot图片库 names from chat text. Pending images are
    # named only by an administrator in WebUI.

    @filter.on_llm_request(priority=1190)
    async def learn_identity_from_current_llm_context(self, event: AstrMessageEvent, req: Any):
        # Deliberately disabled as an identity source. req.prompt can contain old Bot
        # replies and compressed conversation history, so parsing it can reintroduce
        # the exact false-learning bug this plugin is designed to prevent. User-authored messages are no longer treated as an identity source either.
        return

    @filter.on_llm_response()
    async def observe_bot_identity_confirmation(self, event: AstrMessageEvent, resp: Any):
        # Do NOT learn a new identity from Bot's own prose. This was the main cause
        # of entries such as “不起……” and “片中的人物叫做洛茜” being saved as
        # names. Bot responses can acknowledge a user claim, but the user/group
        # statement is the authoritative source and is captured above.
        return

    # ------------------------------------------------------------------
    # Forced trigger + context injection
    # ------------------------------------------------------------------
    def _should_recognize(self, event: AstrMessageEvent) -> bool:
        if self._is_bot_self_event(event):
            return False
        if self._is_private(event):
            return True
        if not bool(self._setting("only_group_at_or_private", True)):
            return True
        return self._event_has_at_bot(event)

    @filter.on_llm_request(priority=1200)
    async def force_image_recognition(self, event: AstrMessageEvent, req: Any):
        """High-priority hook: force recognition when an eligible image message enters the LLM path."""
        if not self._should_recognize(event):
            return
        # Second hard gate for group chats: even if another AstrBot hook calls
        # this method unexpectedly, no @Bot means no image processing.
        if not self._is_private(event) and bool(self._setting("only_group_at_or_private", True)) and not self._event_has_at_bot(event):
            logger.info("[VisionMemory] 硬拦截：群聊当前消息没有真实 @Bot，禁止识图/入待命名队列/入库")
            return
        images = await self._collect_images(event)
        if not images:
            return
        # Avoid duplicate recognition in the same event when multiple framework hooks run.
        if getattr(event, "_vision_memory_result", None):
            result = getattr(event, "_vision_memory_result")
        else:
            result = await self.recognize(event, images[0][0], force=True)
            try:
                setattr(event, "_vision_memory_result", result)
            except Exception:
                pass
        context_text = self._format_context(result)
        try:
            from astrbot.core.agent.message import TextPart
            # AstrBot 4.27.x expects a real content-part object here. Passing a raw
            # string causes agent_sub_stages to call model_dump() on str and crash.
            req.extra_user_content_parts.append(TextPart(text=context_text).mark_as_temp())
        except Exception as exc:
            # Never append a raw string as a fallback: it corrupts ProviderRequest
            # serialization. If the API is unavailable, simply skip injection.
            logger.error("[VisionMemory] 临时识图上下文注入失败，已安全跳过：%s", _clean(exc, 400), exc_info=True)
        logger.info("[VisionMemory] 已将识图判定注入本轮 LLM；是否回复图片内容由主模型依据上下文决定。")

    def _format_context(self, result: dict[str, Any]) -> str:
        best = result.get("best_match") or {}
        lines = ["<vision_memory_recognition>", "本轮图片已经由 VisionMemory 强制识别。以下内容是内部识别上下文，不要求机器人必须围绕图片回复。"]
        if best:
            lines.append(f"图片人物/事物：{best.get('name')}；匹配置信度：{best.get('score', 0):.0%}；来源库：{LIBRARIES.get(best.get('library'), best.get('library'))}。")
            lines.append(f"判定依据：{best.get('reason', '')}")
        else:
            lines.append("图片没有达到库内接受阈值；请把识图结果视为未知图片，不要臆造人物身份。")
        if result.get("is_self"):
            lines.append("【重要身份规则】识别结果为夏目安安；对于当前 Bot 人格而言，图片中的人物就是 Bot 自己本人（bit自己），不是第三方人物。")
        if result.get("created_memory"):
            lines.append(f"Bot 图片库已自动记录一个新的视觉记忆栏目：{result['created_memory'].get('name')}。")
        elif result.get("pending_memory"):
            lines.append("这张图片目前未知，已保存到【待管理员命名】。插件不会根据聊天上下文、用户后续发言或 Bot 自己的回复自动给它命名；管理员必须在 WebUI 手动命名后才能加入 Bot 图片库。")
        lines.append("图片识别已经完成；是否在最终回复中提及图片、评论图片或忽略图片，由当前对话上下文决定。")
        lines.append("</vision_memory_recognition>")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # WebUI APIs / custom page
    # ------------------------------------------------------------------
    def _register_web_routes(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[VisionMemory] 当前 AstrBot 不支持 register_web_api，插件仍可执行识图，但自定义 WebUI 面板不可用。")
            return
        prefix = f"/{PLUGIN_ID}"
        routes = [
            (f"{prefix}/state", self.web_state, ["GET"]),
            (f"{prefix}/providers", self.web_providers, ["GET"]),
            (f"{prefix}/settings", self.web_settings, ["GET", "POST"]),
            (f"{prefix}/libraries/<library>", self.web_library, ["GET"]),
            (f"{prefix}/entries", self.web_entry, ["POST"]),
            (f"{prefix}/entries/<library>/<entry_id>", self.web_entry_update, ["POST", "DELETE"]),
            # Plugin Pages bridge.upload() does NOT allow query strings in its endpoint.
            # Pass the library/entry as path parameters instead.
            (f"{prefix}/upload/<library>/<entry_id>", self.web_upload, ["POST"]),
            (f"{prefix}/image/<library>/<entry_id>/<image_id>", self.web_image, ["GET"]),
            (f"{prefix}/image-data/<library>/<entry_id>/<image_id>", self.web_image_data, ["GET"]),
            (f"{prefix}/images/delete/<library>/<entry_id>/<image_id>", self.web_image_delete, ["POST"]),
            (f"{prefix}/purge", self.web_purge, ["POST"]),
            (f"{prefix}/logs", self.web_logs, ["GET"]),
            (f"{prefix}/pending", self.web_pending, ["GET"]),
            (f"{prefix}/pending/image-data/<pending_id>", self.web_pending_image_data, ["GET"]),
            (f"{prefix}/pending/promote/<pending_id>", self.web_pending_promote, ["POST"]),
            (f"{prefix}/pending/delete/<pending_id>", self.web_pending_delete, ["POST"]),
            (f"{prefix}/recognize-test", self.web_recognize_test, ["POST"]),
            (f"{prefix}/rebuild-features", self.web_rebuild_features, ["POST"]),
            (f"{prefix}/rebuild-features/status/<job_id>", self.web_rebuild_status, ["GET"]),
        ]
        for route, handler, methods in routes:
            try:
                self.context.register_web_api(route, handler, methods, f"VisionMemory: {route}")
            except TypeError:
                try:
                    self.context.register_web_api(route, handler, methods)
                except Exception as exc:
                    logger.warning("[VisionMemory] route register failed %s: %s", route, _clean(exc, 300))
            except Exception as exc:
                logger.warning("[VisionMemory] route register failed %s: %s", route, _clean(exc, 300))

    async def _run_rebuild_job(self, job_id: str, libraries: list[str]) -> None:
        job=self._rebuild_jobs.get(job_id)
        if not job: return
        total=sum(len([x for x in self.data["libraries"].get(lib,[]) if isinstance(x,dict) and (x.get("images") or x.get("features"))]) for lib in libraries)
        done=0; job.update({"status":"running","total":total,"done":0,"message":"正在读取参考图…"})
        try:
            for lib in libraries:
                for entry in [x for x in self.data["libraries"].get(lib,[]) if isinstance(x,dict)]:
                    if not entry.get("images"):
                        done+=1; job["done"]=done; continue
                    job["current"]={"library":LIBRARIES.get(lib,lib),"name":entry.get("name"),"images":len(entry.get("images",[]))}
                    stats=await self._rebuild_entry_features(entry)
                    done+=1; job["done"]=done; job["last_result"]={"name":entry.get("name"),"stats":stats}
                    self._save_sync()
            job.update({"status":"done","message":"全部参考图特征已重建。","finished_at":_now()})
            logger.info("[VisionMemory] ★ 参考图特征重建完成 job=%s",job_id)
        except Exception as exc:
            job.update({"status":"error","message":_clean(exc,500),"finished_at":_now()})
            logger.error("[VisionMemory] 参考图特征重建失败 job=%s error=%s",job_id,_clean(exc,500),exc_info=True)

    async def web_rebuild_features(self):
        payload=await request.json(default={})
        libs=payload.get("libraries") if isinstance(payload,dict) else None
        if not isinstance(libs,list) or not libs:
            libs=list(LIBRARIES)
        libs=[x for x in libs if x in LIBRARIES]
        if not libs:
            return error_response("no valid libraries",status_code=400)
        for job in self._rebuild_jobs.values():
            if job.get("status")=="running":
                return json_response({"started":False,"job":job})
        job_id=uuid.uuid4().hex[:16]
        self._rebuild_jobs[job_id]={"id":job_id,"status":"queued","total":0,"done":0,"message":"等待开始…","started_at":_now()}
        asyncio.create_task(self._run_rebuild_job(job_id,libs))
        return json_response({"started":True,"job":self._rebuild_jobs[job_id]})

    async def web_rebuild_status(self, job_id: str):
        job=self._rebuild_jobs.get(_clean(job_id,80))
        if not job:
            return error_response("job not found",status_code=404)
        return json_response({"job":job})

    async def web_state(self):
        return json_response({
            "plugin": PLUGIN_ID,
            "version": VERSION,
            "author": AUTHOR,
            "libraries": {k: {"name": v, "count": len(self.data["libraries"].get(k, []))} for k, v in LIBRARIES.items()},
            "settings": dict(self.data.get("settings", {})),
            "providers": self._provider_views(),
            "pending_count": len(self._pending_items()),
            "ccip_available": CCIP_AVAILABLE,
            "ccip_model": self._setting("ccip_model", "ccip-caformer-24-randaug-pruned"),
            "ccip_model_ready": self._setting("ccip_model", "ccip-caformer-24-randaug-pruned") in _CCIP_READY_MODELS,
            "ccip_model_verified": self._setting("ccip_model", "ccip-caformer-24-randaug-pruned") in _CCIP_VERIFIED_MODELS,
            "rebuild_jobs": [x for x in self._rebuild_jobs.values() if x.get("status") == "running"],
        })

    async def web_providers(self):
        return json_response({"providers": self._provider_views()})

    async def web_settings(self):
        if request.method == "GET":
            return json_response(dict(self.data["settings"]))
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("invalid settings", status_code=400)
        allowed = set(self.data["settings"])
        for key, value in payload.items():
            if key not in allowed:
                continue
            if key.endswith("threshold"):
                value = _pct(value)
            if key.endswith("days") or key.endswith("seconds") or key.endswith("hours") or key.endswith("minutes") or key.startswith("max_"):
                try:
                    value = int(value)
                except Exception:
                    continue
            self.data["settings"][key] = value
        # v1.9 hard invariants: contextual auto-naming is disabled, while the
        # administrator queue keeps a 7-day retention and 20-item cap.
        self.data["settings"]["auto_memory_enabled"] = False
        try:
            self.data["settings"]["pending_memory_retention_hours"] = max(1, int(self.data["settings"].get("pending_memory_retention_hours", 168) or 168))
        except Exception:
            self.data["settings"]["pending_memory_retention_hours"] = 168
        try:
            self.data["settings"]["context_learning_max_pending"] = max(1, int(self.data["settings"].get("context_learning_max_pending", 20) or 20))
        except Exception:
            self.data["settings"]["context_learning_max_pending"] = 20
        self._save_sync()
        return json_response({"saved": True, "settings": self.data["settings"]})

    async def web_library(self, library: str):
        if library not in LIBRARIES:
            return error_response("unknown library", status_code=404)
        entries = json.loads(json.dumps(self.data["libraries"][library], ensure_ascii=False))
        # Never expose server file paths to browser.
        for entry in entries:
            for image in entry.get("images", []) or []:
                image["url_key"] = f"image/{library}/{entry['id']}/{image.get('id')}"
                image.pop("path", None)
        return json_response({"key": library, "name": LIBRARIES[library], "entries": entries})

    async def web_entry(self):
        payload = await request.json(default={})
        library = _clean(payload.get("library"), 80)
        name = _clean(payload.get("name"), 160)
        features = _clean(payload.get("features"), 12000)
        if library not in LIBRARIES or not name:
            return error_response("library/name required", status_code=400)
        entry = self._new_entry(name, features, builtin=False)
        self.data["libraries"][library].append(entry)
        self._save_sync()
        return json_response({"entry": entry})

    async def web_entry_update(self, library: str, entry_id: str):
        if library not in LIBRARIES:
            return error_response("unknown library", status_code=404)
        entry = self._find_entry(library, entry_id)
        if entry is None:
            return error_response("entry not found", status_code=404)
        if request.method == "DELETE":
            self._delete_entry(library, entry_id)
            self._save_sync()
            return json_response({"deleted": True})
        payload = await request.json(default={})
        if isinstance(payload, dict) and payload.get("_delete"):
            self._delete_entry(library, entry_id)
            self._save_sync()
            return json_response({"deleted": True})
        if "name" in payload:
            entry["name"] = _clean(payload.get("name"), 160) or entry["name"]
        if "features" in payload:
            entry["features"] = _clean(payload.get("features"), 12000)
        entry["updated_at"] = _now()
        self._save_sync()
        return json_response({"entry": entry})

    async def web_upload(self, library: str, entry_id: str):
        # library/entry_id are path parameters because the AstrBot Plugin Page
        # bridge rejects query strings in upload() endpoints.
        library = _clean(library, 80)
        entry_id = _clean(entry_id, 120)
        if library not in LIBRARIES:
            return error_response("unknown library", status_code=400)
        if not entry_id:
            return error_response("entry_id required", status_code=400)
        entry = self._find_entry(library, entry_id)
        if entry is None:
            return error_response("entry not found", status_code=404)

        files = await request.files()
        upload = files.get("file") if files else None
        if PluginUploadFile is not None and not isinstance(upload, PluginUploadFile):
            return error_response("missing file", status_code=400)
        if upload is None:
            return error_response("missing file", status_code=400)

        original_name = Path(_clean(getattr(upload, "filename", "image.jpg"), 160)).name
        suffix = Path(original_name).suffix.lower()
        allowed_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
        if suffix not in allowed_suffixes:
            return error_response("只支持 JPG/JPEG/PNG/WebP/GIF/BMP 图片", status_code=400)
        content_length = getattr(upload, "content_length", None)
        try:
            if content_length is not None and int(content_length) > 20 * 1024 * 1024:
                return error_response("图片不能超过 20 MB", status_code=400)
        except Exception:
            pass
        if len(entry.get("images", [])) >= int(self._setting("max_images_per_entry", 24)):
            return error_response("image limit reached", status_code=400)
        target_dir = self.images_root / library / entry["id"]
        target_dir.mkdir(parents=True, exist_ok=True)
        # AstrBot 4.29+ appends a 32-character UUID and ".tmp" while saving.
        # Keep the on-disk name short so Windows paths do not cross MAX_PATH;
        # retain the original name separately for display and downloads.
        storage_name = f"{uuid.uuid4().hex[:16]}{suffix}"
        target = target_dir / storage_name
        try:
            await upload.save(target)
        except Exception as exc:
            logger.error("[VisionMemory] 图片保存失败 target=%s error=%s", target, _clean(exc, 500), exc_info=True)
            return error_response(f"图片保存失败，请检查数据目录权限或路径长度：{_clean(exc, 300)}", status_code=500)
        image_item = {"id": uuid.uuid4().hex, "filename": original_name, "storage_name": storage_name, "path": str(target), "created_at": _now(), "features": "", "sha256": _file_hash(str(target)), "ccip_feature": [], "ccip_model": "", "ccip_size": 0}
        entry.setdefault("images", []).append(image_item)
        entry["updated_at"] = _now()
        try:
            features = await self._extract_features(str(target), "magic_trial_characters" if library == "magic_trial_characters" else "custom_objects", None)
            image_item["features"] = features
            image_item["feature_updated_at"] = _now()
        except Exception as exc:
            image_item["features"] = ""
            logger.warning("[VisionMemory] UI upload feature extraction failed: %s", _clean(exc, 400), exc_info=True)
        if CCIP_AVAILABLE and bool(self._setting("ccip_enabled", True)):
            try:
                model=_clean(self._setting("ccip_model","ccip-caformer-24-randaug-pruned"),100)
                size=max(128,int(self._setting("ccip_reference_feature_size",384) or 384))
                await self._ccip_extract_for_image(image_item,model,size)
            except Exception as exc:
                logger.warning("[VisionMemory][CCIP] UI upload feature extraction failed: %s",_clean(exc,400))
        # Rebuild the aggregate text strictly from image-level fingerprints.
        entry["features"]="\n\n".join(f"【参考图 {i} · {x.get('filename','图片')}】\n{_clean(x.get('features'),12000)}" for i,x in enumerate(entry.get("images",[]) or [],1) if _clean(x.get("features"),12000))
        self._save_sync()
        return json_response({"entry": entry, "image": image_item})

    async def web_image(self, library: str, entry_id: str, image_id: str):
        entry = self._find_entry(library, entry_id)
        if not entry:
            return error_response("entry not found", status_code=404)
        image = next((x for x in entry.get("images", []) if x.get("id") == image_id), None)
        if not image or not Path(image.get("path", "")).exists():
            return error_response("image not found", status_code=404)
        return file_response(image["path"], filename=image.get("filename", "image.jpg"))

    async def web_image_data(self, library: str, entry_id: str, image_id: str):
        import mimetypes
        import base64
        entry = self._find_entry(library, entry_id)
        if not entry:
            return error_response("entry not found", status_code=404)
        image = next((x for x in entry.get("images", []) if x.get("id") == image_id), None)
        if not image or not Path(image.get("path", "")).exists():
            return error_response("image not found", status_code=404)
        path = Path(image["path"])
        try:
            raw = path.read_bytes()
            if len(raw) > 8 * 1024 * 1024:
                return error_response("image too large for inline preview", status_code=413)
            mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            data_url = "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")
            return json_response({"data_url": data_url, "filename": image.get("filename", path.name)})
        except Exception as exc:
            return error_response(_clean(exc, 300), status_code=500)

    async def web_image_delete(self, library: str, entry_id: str, image_id: str):
        if library not in LIBRARIES:
            return error_response("unknown library", status_code=404)
        entry = self._find_entry(library, entry_id)
        if not entry:
            return error_response("entry not found", status_code=404)
        images = entry.get("images", []) or []
        image = next((x for x in images if x.get("id") == image_id), None)
        if not image:
            return error_response("image not found", status_code=404)
        try:
            Path(image.get("path", "")).unlink(missing_ok=True)
        except Exception:
            pass
        entry["images"] = [x for x in images if x.get("id") != image_id]
        # Rebuild aggregate feature text from remaining image-level features.
        feature_blocks = [_clean(x.get("features"), 12000) for x in entry["images"] if _clean(x.get("features"), 12000)]
        entry["features"] = "\n\n--- 图片特征 ---\n".join(feature_blocks) if feature_blocks else ""
        entry["updated_at"] = _now()
        self._save_sync()
        logger.info("[VisionMemory] UI 删除单张图片 library=%s entry=%s image=%s", library, entry_id, image_id)
        return json_response({"deleted": True, "entry": entry})

    async def web_purge(self):
        removed = await self._purge_expired_memory()
        return json_response({"removed": removed})

    async def web_pending(self):
        self._purge_pending()
        items = []
        for item in self._pending_items():
            row = {k: item.get(k) for k in ("id", "created_at", "session_key", "source_name", "image_filename", "features", "result")}
            row["image_exists"] = bool(item.get("image_path") and Path(item.get("image_path")).exists())
            items.append(row)
        return json_response({"pending": items})

    async def web_pending_image_data(self, pending_id: str):
        import mimetypes
        item = next((x for x in self._pending_items() if x.get("id") == pending_id), None)
        if not item:
            return error_response("pending item not found", status_code=404)
        path = Path(item.get("image_path", ""))
        if not path.exists():
            return error_response("image not found", status_code=404)
        try:
            raw = path.read_bytes()
            if len(raw) > 8 * 1024 * 1024:
                return error_response("image too large for inline preview", status_code=413)
            mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            return json_response({"data_url": "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")})
        except Exception as exc:
            return error_response(_clean(exc, 300), status_code=500)

    async def web_pending_promote(self, pending_id: str):
        payload = await request.json(default={})
        name = _clean(payload.get("name") if isinstance(payload, dict) else "", 80)
        if not name:
            return error_response("name required", status_code=400)
        ok = await self._promote_pending_to_memory(pending_id, name, "WebUI手动确认")
        if not ok:
            return error_response("pending item not found", status_code=404)
        return json_response({"promoted": True})

    async def web_pending_delete(self, pending_id: str):
        items = self._pending_items()
        item = next((x for x in items if x.get("id") == pending_id), None)
        if not item:
            return error_response("pending item not found", status_code=404)
        try: Path(item.get("image_path", "")).unlink(missing_ok=True)
        except Exception: pass
        shutil.rmtree(self.images_root / "pending" / str(pending_id), ignore_errors=True)
        items[:] = [x for x in items if x.get("id") != pending_id]
        self._save_sync()
        return json_response({"deleted": True})

    async def web_logs(self):
        return json_response({"logs": self.data.get("recognition_log", [])[:100]})

    async def web_recognize_test(self):
        files = await request.files()
        upload = files.get("file") if files else None
        if upload is None:
            return error_response("missing file", status_code=400)
        temp = self.root / "test_uploads"
        temp.mkdir(exist_ok=True)
        target = temp / f"{uuid.uuid4().hex}_{_safe_id(getattr(upload, 'filename', 'image.jpg'))}"
        await upload.save(target)
        try:
            result = await self.recognize(SimpleWebEvent(self), str(target), force=True)
            return json_response(result)
        finally:
            target.unlink(missing_ok=True)

    def _find_entry(self, library: str, entry_id: str) -> dict[str, Any] | None:
        for entry in self.data["libraries"].get(library, []):
            if isinstance(entry, dict) and str(entry.get("id")) == str(entry_id):
                return entry
        return None

    def _delete_entry(self, library: str, entry_id: str) -> None:
        entry = self._find_entry(library, entry_id)
        if not entry:
            return
        if library == "magic_trial_characters" and entry.get("builtin"):
            # Built-in character slots remain available; delete their images/features only.
            for image in entry.get("images", []) or []:
                try:
                    Path(image.get("path", "")).unlink(missing_ok=True)
                except Exception:
                    pass
            entry["images"] = []
            entry["features"] = ""
            entry["updated_at"] = _now()
            return
        self.data["libraries"][library] = [x for x in self.data["libraries"][library] if x.get("id") != entry_id]
        shutil.rmtree(self.images_root / library / entry_id, ignore_errors=True)


class SimpleWebEvent:
    """Minimal event facade for the WebUI test-recognition endpoint."""
    def __init__(self, plugin: VisionMemoryPlugin):
        self.plugin = plugin
        self.unified_msg_origin = "webui:vision-memory"

    def is_private_chat(self):
        return True


__all__ = ["VisionMemoryPlugin"]
