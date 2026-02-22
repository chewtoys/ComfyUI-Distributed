"""
Shared logging utilities for ComfyUI-Distributed.
"""
import os
import json
import time

# Config file is in parent directory
CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "gpu_config.json")

_debug_cache: bool | None = None
_debug_cache_time: float = 0.0
_DEBUG_TTL: float = 5.0

def is_debug_enabled():
    """Check if debug is enabled."""
    global _debug_cache, _debug_cache_time

    now = time.monotonic()
    if _debug_cache is not None and (now - _debug_cache_time) < _DEBUG_TTL:
        return _debug_cache

    enabled = False
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                enabled = config.get("settings", {}).get("debug", False)
        except:
            pass

    _debug_cache = enabled
    _debug_cache_time = now
    return enabled

def debug_log(message):
    """Log debug messages only if debug is enabled in config."""
    if is_debug_enabled():
        print(f"[Distributed] {message}")

def log(message):
    """Always log important messages."""
    print(f"[Distributed] {message}")
