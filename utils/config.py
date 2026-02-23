"""
Configuration management for ComfyUI-Distributed.
"""
import os
import json
from .logging import log

# Import defaults for timeout fallbacks
from .constants import HEARTBEAT_TIMEOUT

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "gpu_config.json")
_config_cache = None
_config_mtime = 0.0


def _config_path():
    return CONFIG_FILE

def get_default_config():
    """Returns the default configuration dictionary. Single source of truth."""
    return {
        "master": {"host": ""},
        "workers": [],
        "settings": {
            "debug": False,
            "auto_launch_workers": False,
            "stop_workers_on_master_exit": True,
            "master_delegate_only": False,
            "websocket_orchestration": True
        },
        "tunnel": {
            "status": "stopped",
            "public_url": "",
            "pid": None,
            "log_file": "",
            "previous_master_host": ""
        }
    }

def _merge_with_defaults(data, defaults):
    """Recursively merge loaded config data with default keys."""
    if not isinstance(data, dict):
        return defaults

    merged = {}
    for key, default_value in defaults.items():
        loaded_value = data.get(key, default_value)
        if isinstance(default_value, dict) and isinstance(loaded_value, dict):
            merged[key] = _merge_with_defaults(loaded_value, default_value)
        else:
            merged[key] = loaded_value

    # Preserve unknown keys for forward compatibility.
    for key, value in data.items():
        if key not in merged:
            merged[key] = value

    return merged


def invalidate_config_cache():
    """Invalidate in-memory config cache so next load reads from disk."""
    global _config_cache, _config_mtime
    _config_cache = None
    _config_mtime = 0.0


def load_config():
    """Loads the config, falling back to defaults if the file is missing or invalid."""
    global _config_cache, _config_mtime
    path = _config_path()

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        if _config_cache is None:
            _config_cache = get_default_config()
        return _config_cache

    if _config_cache is None or mtime != _config_mtime:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            _config_cache = _merge_with_defaults(loaded, get_default_config())
        except Exception as e:
            log(f"Error loading config, using defaults: {e}")
            _config_cache = get_default_config()
        _config_mtime = mtime

    return _config_cache

def save_config(config):
    """Saves the configuration to file."""
    try:
        with open(_config_path(), 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
        invalidate_config_cache()
        return True
    except Exception as e:
        log(f"Error saving config: {e}")
        return False

def ensure_config_exists():
    """Creates default config file if it doesn't exist. Used by __init__.py"""
    if not os.path.exists(_config_path()):
        default_config = get_default_config()
        if save_config(default_config):
            from .logging import debug_log
            debug_log("Created default config file")
        else:
            log("Could not create default config file")

def get_worker_timeout_seconds(default: int = HEARTBEAT_TIMEOUT) -> int:
    """Return the unified worker timeout (seconds).

    Priority:
    1) UI-configured setting `settings.worker_timeout_seconds`
    2) Fallback to provided `default` (defaults to HEARTBEAT_TIMEOUT which itself
       can be overridden via the COMFYUI_HEARTBEAT_TIMEOUT env var)

    This value should be used anywhere we consider a worker "timed out" from the
    master's perspective (e.g., collector waits, upscaler result collection).
    """
    try:
        cfg = load_config()
        val = int(cfg.get('settings', {}).get('worker_timeout_seconds', default))
        return max(1, val)
    except Exception:
        return max(1, int(default))


def is_master_delegate_only() -> bool:
    """Returns True when master should skip local workload and act as orchestrator only."""
    try:
        cfg = load_config()
        return bool(cfg.get('settings', {}).get('master_delegate_only', False))
    except Exception:
        return False
