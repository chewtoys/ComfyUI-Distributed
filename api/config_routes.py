import json

from aiohttp import web
import server

from ..utils.config import load_config, save_config
from ..utils.logging import debug_log, log
from ..utils.network import handle_api_error, normalize_host


def _positive_int(value):
    return value > 0


CONFIG_SCHEMA = {
    "workers": (list, None),
    "master": (dict, None),
    "settings": (dict, None),
    "tunnel": (dict, None),
    "managed_processes": (dict, None),
    "worker_timeout_seconds": (int, _positive_int),
    "debug": (bool, None),
    "auto_launch_workers": (bool, None),
    "stop_workers_on_master_exit": (bool, None),
    "master_delegate_only": (bool, None),
    "websocket_orchestration": (bool, None),
    "has_auto_populated_workers": (bool, None),
}

_SETTINGS_FIELDS = {
    "worker_timeout_seconds",
    "debug",
    "auto_launch_workers",
    "stop_workers_on_master_exit",
    "master_delegate_only",
    "websocket_orchestration",
    "has_auto_populated_workers",
}


@server.PromptServer.instance.routes.get("/distributed/config")
async def get_config_endpoint(request):
    config = load_config()
    return web.json_response(config)


@server.PromptServer.instance.routes.post("/distributed/config")
async def update_config_endpoint(request):
    """Bulk config update with schema validation."""
    try:
        data = await request.json()
    except Exception as e:
        return await handle_api_error(request, f"Invalid JSON payload: {e}", 400)

    if not isinstance(data, dict):
        return await handle_api_error(request, "Config payload must be an object", 400)

    config = load_config()
    settings = config.setdefault("settings", {})
    errors = []

    for key, value in data.items():
        if key not in CONFIG_SCHEMA:
            errors.append(f"Unknown field: {key}")
            continue

        expected_type, validator = CONFIG_SCHEMA[key]
        if not isinstance(value, expected_type):
            errors.append(f"{key}: expected {expected_type.__name__}")
            continue

        if validator and not validator(value):
            errors.append(f"{key}: value {value!r} failed validation")
            continue

        if key in _SETTINGS_FIELDS:
            settings[key] = value
        else:
            config[key] = value

    if errors:
        return web.json_response({"error": errors}, status=400)

    if save_config(config):
        return web.json_response({"status": "success", "config": config})
    return await handle_api_error(request, "Failed to save config")


@server.PromptServer.instance.routes.get("/distributed/queue_status/{job_id}")
async def queue_status_endpoint(request):
    """Check if a job queue is initialized."""
    try:
        job_id = request.match_info['job_id']
        
        # Import to ensure initialization
        from ..nodes.distributed_upscale import ensure_tile_jobs_initialized
        prompt_server = ensure_tile_jobs_initialized()
        
        async with prompt_server.distributed_tile_jobs_lock:
            exists = job_id in prompt_server.distributed_pending_tile_jobs
        
        debug_log(f"Queue status check for job {job_id}: {'exists' if exists else 'not found'}")
        return web.json_response({"exists": exists, "job_id": job_id})
    except Exception as e:
        return await handle_api_error(request, e, 500)

@server.PromptServer.instance.routes.post("/distributed/config/update_worker")
async def update_worker_endpoint(request):
    try:
        data = await request.json()
        worker_id = data.get("worker_id")
        
        if worker_id is None:
            return await handle_api_error(request, "Missing worker_id", 400)
            
        config = load_config()
        worker_found = False
        
        for worker in config.get("workers", []):
            if worker["id"] == worker_id:
                # Update all provided fields
                if "enabled" in data:
                    worker["enabled"] = data["enabled"]
                if "name" in data:
                    worker["name"] = data["name"]
                if "port" in data:
                    worker["port"] = data["port"]
                    
                # Handle host field - remove it if None
                if "host" in data:
                    if data["host"] is None:
                        worker.pop("host", None)
                    else:
                        worker["host"] = normalize_host(data["host"])
                        
                # Handle cuda_device field - remove it if None
                if "cuda_device" in data:
                    if data["cuda_device"] is None:
                        worker.pop("cuda_device", None)
                    else:
                        worker["cuda_device"] = data["cuda_device"]
                        
                # Handle extra_args field - remove it if None
                if "extra_args" in data:
                    if data["extra_args"] is None:
                        worker.pop("extra_args", None)
                    else:
                        worker["extra_args"] = data["extra_args"]
                        
                # Handle type field
                if "type" in data:
                    worker["type"] = data["type"]
                        
                worker_found = True
                break
                
        if not worker_found:
            # If worker not found and all required fields are provided, create new worker
            if all(key in data for key in ["name", "port", "cuda_device"]):
                new_worker = {
                    "id": worker_id,
                    "name": data["name"],
                    "host": normalize_host(data.get("host", "localhost")),
                    "port": data["port"],
                    "cuda_device": data["cuda_device"],
                    "enabled": data.get("enabled", False),
                    "extra_args": data.get("extra_args", ""),
                    "type": data.get("type", "local")
                }
                if "workers" not in config:
                    config["workers"] = []
                config["workers"].append(new_worker)
                worker_found = True
            else:
                return await handle_api_error(request, f"Worker {worker_id} not found and missing required fields for creation", 404)
            
        if save_config(config):
            return web.json_response({"status": "success"})
        else:
            return await handle_api_error(request, "Failed to save config")
    except Exception as e:
        return await handle_api_error(request, e, 400)

@server.PromptServer.instance.routes.post("/distributed/config/delete_worker")
async def delete_worker_endpoint(request):
    try:
        data = await request.json()
        worker_id = data.get("worker_id")
        
        if worker_id is None:
            return await handle_api_error(request, "Missing worker_id", 400)
            
        config = load_config()
        workers = config.get("workers", [])
        
        # Find and remove the worker
        worker_index = -1
        for i, worker in enumerate(workers):
            if worker["id"] == worker_id:
                worker_index = i
                break
                
        if worker_index == -1:
            return await handle_api_error(request, f"Worker {worker_id} not found", 404)
            
        # Remove the worker
        removed_worker = workers.pop(worker_index)
        
        if save_config(config):
            return web.json_response({
                "status": "success",
                "message": f"Worker {removed_worker.get('name', worker_id)} deleted"
            })
        else:
            return await handle_api_error(request, "Failed to save config")
    except Exception as e:
        return await handle_api_error(request, e, 400)

@server.PromptServer.instance.routes.post("/distributed/config/update_setting")
async def update_setting_endpoint(request):
    """Updates a specific key in the settings object."""
    try:
        data = await request.json()
        key = data.get("key")
        value = data.get("value")

        if not key or value is None:
            return await handle_api_error(request, "Missing 'key' or 'value' in request", 400)

        config = load_config()
        if 'settings' not in config:
            config['settings'] = {}
        
        config['settings'][key] = value

        if save_config(config):
            return web.json_response({"status": "success", "message": f"Setting '{key}' updated."})
        else:
            return await handle_api_error(request, "Failed to save config")
    except Exception as e:
        return await handle_api_error(request, e, 400)

@server.PromptServer.instance.routes.post("/distributed/config/update_master")
async def update_master_endpoint(request):
    """Updates master configuration."""
    try:
        data = await request.json()
        
        config = load_config()
        if 'master' not in config:
            config['master'] = {}
        
        # Update all provided fields
        if "name" in data:
            config['master']['name'] = data['name']
        if "host" in data:
            config['master']['host'] = data['host']
        if "port" in data:
            config['master']['port'] = data['port']
        if "cuda_device" in data:
            config['master']['cuda_device'] = data['cuda_device']
        if "extra_args" in data:
            config['master']['extra_args'] = data['extra_args']
            
        if save_config(config):
            return web.json_response({"status": "success", "message": "Master configuration updated."})
        else:
            return await handle_api_error(request, "Failed to save config")
    except Exception as e:
        return await handle_api_error(request, e, 400)
