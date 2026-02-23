# Import everything needed from the main module
from .distributed import (
    NODE_CLASS_MAPPINGS as DISTRIBUTED_CLASS_MAPPINGS, 
    NODE_DISPLAY_NAME_MAPPINGS as DISTRIBUTED_DISPLAY_NAME_MAPPINGS
)

# Import utilities
from .utils.config import ensure_config_exists, CONFIG_FILE
from .utils.logging import debug_log

# Import distributed upscale nodes
from .nodes.distributed_upscale import (
    NODE_CLASS_MAPPINGS as UPSCALE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS as UPSCALE_DISPLAY_NAME_MAPPINGS
)

WEB_DIRECTORY = "./web"

ensure_config_exists()

# Merge node mappings
NODE_CLASS_MAPPINGS = {**DISTRIBUTED_CLASS_MAPPINGS, **UPSCALE_CLASS_MAPPINGS}
NODE_DISPLAY_NAME_MAPPINGS = {**DISTRIBUTED_DISPLAY_NAME_MAPPINGS, **UPSCALE_DISPLAY_NAME_MAPPINGS}

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']

debug_log("Loaded Distributed nodes.")
debug_log(f"Config file: {CONFIG_FILE}")
debug_log(f"Available nodes: {list(NODE_CLASS_MAPPINGS.keys())}")
