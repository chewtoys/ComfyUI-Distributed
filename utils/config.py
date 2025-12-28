"""
Configuration management for ComfyUI-Distributed.
"""
import os
import json
from .logging import log

# Import defaults for timeout fallbacks
from .constants import HEARTBEAT_TIMEOUT

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "gpu_config.json")

def get_default_config():
    """Returns the default configuration dictionary. Single source of truth."""
    return {
        "master": {"host": ""},
        "workers": [],
        "settings": {
            "debug": False,
            "auto_launch_workers": False,
            "stop_workers_on_master_exit": True,
            "master_delegate_only": False
        },
        "tunnel": {
            "status": "stopped",
            "public_url": "",
            "pid": None,
            "log_file": "",
            "previous_master_host": ""
        }
    }

def load_config():
    """Loads the config, falling back to defaults if the file is missing or invalid."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
                defaults = get_default_config()
                for key, value in defaults.items():
                    if key not in data:
                        data[key] = value
                return data
        except Exception as e:
            log(f"Error loading config, using defaults: {e}")
    return get_default_config()

def save_config(config):
    """Saves the configuration to file."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        log(f"Error saving config: {e}")
        return False

def ensure_config_exists():
    """Creates default config file if it doesn't exist. Used by __init__.py"""
    if not os.path.exists(CONFIG_FILE):
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
