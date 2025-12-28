import asyncio
import json
import time
import uuid
from collections import deque

import aiohttp

import execution
import server

from .utils.config import load_config
from .utils.logging import debug_log, log


prompt_server = server.PromptServer.instance


def ensure_distributed_state():
    """Ensure prompt_server has the state used by distributed queue orchestration."""
    if not hasattr(prompt_server, "distributed_pending_jobs"):
        prompt_server.distributed_pending_jobs = {}
    if not hasattr(prompt_server, "distributed_jobs_lock"):
        prompt_server.distributed_jobs_lock = asyncio.Lock()


async def _get_client_session():
    """Get or create aiohttp client session (shared with distributed.py)."""
    if not hasattr(prompt_server, "_distributed_session"):
        prompt_server._distributed_session = aiohttp.ClientSession()
    return prompt_server._distributed_session


def _deepcopy_prompt(prompt_obj):
    """Return a deep copy of the workflow/prompt dictionary."""
    return json.loads(json.dumps(prompt_obj))


def _iter_prompt_nodes(prompt_obj):
    for node_id, node in prompt_obj.items():
        if isinstance(node, dict):
            yield str(node_id), node


def _find_nodes_by_class(prompt_obj, class_name):
    nodes = []
    for node_id, node in _iter_prompt_nodes(prompt_obj):
        if node.get("class_type") == class_name:
            nodes.append(node_id)
    return nodes


def _has_upstream_node(prompt_obj, start_node_id, target_class):
    """Depth-first search to find if start node depends on target_class."""
    visited = set()
    stack = [start_node_id]

    while stack:
        node_id = stack.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        node = prompt_obj.get(node_id)
        if not node:
            continue
        inputs = node.get("inputs", {})
        for value in inputs.values():
            if isinstance(value, list) and len(value) == 2:
                upstream_id = str(value[0])
                upstream_node = prompt_obj.get(upstream_id)
                if not upstream_node:
                    continue
                if upstream_node.get("class_type") == target_class:
                    return True
                stack.append(upstream_id)
    return False


def _find_downstream_nodes(prompt_obj, start_ids):
    """Return all nodes reachable downstream from the provided IDs."""
    adjacency = {}
    for node_id, node in _iter_prompt_nodes(prompt_obj):
        inputs = node.get("inputs", {})
        for value in inputs.values():
            if isinstance(value, list) and len(value) == 2:
                source_id = str(value[0])
                adjacency.setdefault(source_id, set()).add(str(node_id))

    connected = set(start_ids)
    queue = deque(start_ids)
    while queue:
        current = queue.popleft()
        for dependent in adjacency.get(current, ()):  # pragma: no branch - simple iteration
            if dependent not in connected:
                connected.add(dependent)
                queue.append(dependent)
    return connected


def _create_numeric_id_generator(prompt_obj):
    """Return a closure that yields new numeric string IDs."""
    max_id = 0
    for node_id in prompt_obj.keys():
        try:
            numeric = int(node_id)
        except (TypeError, ValueError):
            continue
        max_id = max(max_id, numeric)

    counter = max_id

    def _next_id():
        nonlocal counter
        counter += 1
        return str(counter)

    return _next_id


def _prepare_delegate_master_prompt(prompt_obj, collector_ids):
    """Prune master prompt so it only executes post-collector nodes in delegate mode."""
    downstream = _find_downstream_nodes(prompt_obj, collector_ids)
    nodes_to_keep = set(collector_ids)
    nodes_to_keep.update(downstream)

    pruned_prompt = {}
    for node_id in nodes_to_keep:
        node = prompt_obj.get(node_id)
        if node is not None:
            pruned_prompt[node_id] = json.loads(json.dumps(node))

    pruned_ids = set(pruned_prompt.keys())
    for node_id, node in pruned_prompt.items():
        inputs = node.get("inputs")
        if not inputs:
            continue
        for input_name, input_value in list(inputs.items()):
            if isinstance(input_value, list) and len(input_value) == 2:
                source_id = str(input_value[0])
                if source_id not in pruned_ids:
                    inputs.pop(input_name, None)
                    debug_log(
                        f"Removed upstream reference '{input_name}' from node {node_id} for delegate-only master prompt."
                    )

    next_id = _create_numeric_id_generator(pruned_prompt)
    for collector_id in collector_ids:
        collector_entry = pruned_prompt.get(collector_id)
        if not collector_entry:
            continue
        placeholder_id = next_id()
        pruned_prompt[placeholder_id] = {
            "class_type": "DistributedEmptyImage",
            "inputs": {
                "height": 64,
                "width": 64,
                "channels": 3,
            },
            "_meta": {
                "title": "Distributed Empty Image (auto-added)",
            },
        }
        collector_entry.setdefault("inputs", {})["images"] = [placeholder_id, 0]
        debug_log(
            f"Inserted placeholder node {placeholder_id} for collector {collector_id} in delegate-only master prompt."
        )

    return pruned_prompt


async def _ensure_distributed_queue(job_id):
    """Ensure a queue exists for the given distributed job ID."""
    ensure_distributed_state()
    async with prompt_server.distributed_jobs_lock:
        if job_id not in prompt_server.distributed_pending_jobs:
            prompt_server.distributed_pending_jobs[job_id] = asyncio.Queue()


def _generate_job_id_map(prompt_obj, prefix):
    """Create stable per-node job IDs for distributed nodes."""
    job_map = {}
    distributed_nodes = _find_nodes_by_class(prompt_obj, "DistributedCollector") + _find_nodes_by_class(
        prompt_obj, "UltimateSDUpscaleDistributed"
    )
    for node_id in distributed_nodes:
        job_map[node_id] = f"{prefix}_{node_id}"
    return job_map


def _resolve_enabled_workers(config, requested_ids=None):
    """Return a list of worker configs that should participate."""
    workers = []
    for worker in config.get("workers", []):
        worker_id = str(worker.get("id") or "").strip()
        if not worker_id:
            continue

        if requested_ids is not None:
            if worker_id not in requested_ids:
                continue
        elif not worker.get("enabled", False):
            continue

        workers.append(
            {
                "id": worker_id,
                "name": worker.get("name", worker_id),
                "host": worker.get("host"),
                "port": int(worker.get("port", worker.get("listen_port", 8188)) or 8188),
                "type": worker.get("type", "local"),
            }
        )
    return workers


def _resolve_master_url():
    """Best-effort reconstruction of the master's public URL."""
    cfg = load_config()
    master_cfg = cfg.get("master", {}) or {}
    configured_host = (master_cfg.get("host") or "").strip()
    configured_port = master_cfg.get("port")
    default_port = getattr(prompt_server, "port", 8188) or 8188
    port = int(configured_port or default_port)

    def _needs_https(hostname):
        hostname = hostname.lower()
        https_domains = (
            ".proxy.runpod.net",
            ".ngrok-free.app",
            ".ngrok-free.dev",
            ".ngrok.io",
            ".trycloudflare.com",
            ".cloudflare.dev",
        )
        return any(hostname.endswith(suffix) for suffix in https_domains)

    if configured_host:
        if configured_host.startswith(("http://", "https://")):
            return configured_host.rstrip("/")

        host = configured_host
        scheme = "https" if _needs_https(host) or port == 443 else "http"
        default_port_for_scheme = 443 if scheme == "https" else 80
        # For ngrok/cloud domains without explicit port, default to the scheme's default
        if configured_port is None and scheme == "https" and _needs_https(host):
            port = default_port_for_scheme
        port_part = "" if port == default_port_for_scheme else f":{port}"
        return f"{scheme}://{host}{port_part}"

    address = getattr(prompt_server, "address", "127.0.0.1") or "127.0.0.1"
    if address in ("0.0.0.0", "::"):
        address = "127.0.0.1"
    scheme = "https" if port == 443 else "http"
    default_port_for_scheme = 443 if scheme == "https" else 80
    port_part = "" if port == default_port_for_scheme else f":{port}"
    return f"{scheme}://{address}{port_part}"


def _build_worker_url(worker, endpoint=""):
    """Construct the worker base URL with optional endpoint."""
    host = (worker.get("host") or "").strip()
    port = int(worker.get("port", 8188) or 8188)

    if not host:
        host = getattr(prompt_server, "address", "127.0.0.1") or "127.0.0.1"

    if host.startswith(("http://", "https://")):
        base = host.rstrip("/")
    else:
        is_cloud = worker.get("type") == "cloud" or host.endswith(".proxy.runpod.net") or port == 443
        scheme = "https" if is_cloud else "http"
        default_port = 443 if scheme == "https" else 80
        port_part = "" if port == default_port else f":{port}"
        base = f"{scheme}://{host}{port_part}"

    return f"{base}{endpoint}"


async def _worker_is_active(worker):
    """Ping worker's /prompt endpoint to confirm it's reachable."""
    url = _build_worker_url(worker, "/prompt")
    session = await _get_client_session()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            return resp.status == 200
    except Exception:
        return False


async def _dispatch_worker_prompt(worker, prompt_obj, workflow_meta):
    """Send the prepared prompt to a worker ComfyUI instance."""
    url = _build_worker_url(worker, "/prompt")
    payload = {"prompt": prompt_obj}
    extra_data = {}
    if workflow_meta:
        extra_data.setdefault("extra_pnginfo", {})["workflow"] = workflow_meta
    if extra_data:
        payload["extra_data"] = extra_data

    session = await _get_client_session()
    async with session.post(
        url,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=60),
    ) as resp:
        resp.raise_for_status()


async def _queue_master_prompt(prompt_obj, workflow_meta, client_id):
    """Queue the master prompt through ComfyUI's prompt queue."""
    payload = {"prompt": prompt_obj}
    payload = prompt_server.trigger_on_prompt(payload)
    prompt = payload["prompt"]

    prompt_id = str(uuid.uuid4())
    valid = await execution.validate_prompt(prompt_id, prompt, None)
    if not valid[0]:
        raise RuntimeError(f"Invalid prompt: {valid[1]}")

    extra_data = {}
    if workflow_meta:
        extra_data.setdefault("extra_pnginfo", {})["workflow"] = workflow_meta
    if client_id:
        extra_data["client_id"] = client_id

    sensitive = {}
    for key in getattr(execution, "SENSITIVE_EXTRA_DATA_KEYS", []):
        if key in extra_data:
            sensitive[key] = extra_data.pop(key)

    number = getattr(prompt_server, "number", 0)
    prompt_server.number = number + 1
    prompt_queue_item = (number, prompt_id, prompt, extra_data, valid[2], sensitive)
    prompt_server.prompt_queue.put(prompt_queue_item)
    return prompt_id


def _apply_participant_overrides(
    base_prompt,
    participant_id,
    enabled_worker_ids,
    job_id_map,
    master_url,
    delegate_master,
):
    """Return a prompt copy with hidden inputs configured for master/worker."""
    prompt_copy = _deepcopy_prompt(base_prompt)
    is_master = participant_id == "master"
    worker_index_map = {wid: idx for idx, wid in enumerate(enabled_worker_ids)}
    enabled_json = json.dumps(enabled_worker_ids)

    # Distributed seed nodes
    for node_id in _find_nodes_by_class(prompt_copy, "DistributedSeed"):
        node = prompt_copy.get(node_id, {})
        inputs = node.setdefault("inputs", {})
        inputs["is_worker"] = not is_master
        if not is_master:
            idx = worker_index_map.get(participant_id, 0)
            inputs["worker_id"] = f"worker_{idx}"
        else:
            inputs["worker_id"] = ""

    # Distributed collectors
    for node_id in _find_nodes_by_class(prompt_copy, "DistributedCollector"):
        node = prompt_copy.get(node_id, {})
        inputs = node.setdefault("inputs", {})

        if _has_upstream_node(prompt_copy, node_id, "UltimateSDUpscaleDistributed"):
            inputs["pass_through"] = True
            continue

        unique_id = job_id_map.get(node_id, node_id)
        inputs["multi_job_id"] = unique_id
        inputs["is_worker"] = not is_master

        if is_master:
            inputs["enabled_worker_ids"] = enabled_json
            inputs["delegate_only"] = bool(delegate_master)
            inputs.pop("master_url", None)
            inputs.pop("worker_id", None)
        else:
            inputs["master_url"] = master_url
            inputs["worker_id"] = participant_id
            inputs["enabled_worker_ids"] = enabled_json
            inputs["delegate_only"] = False

    # Distributed upscaler nodes
    for node_id in _find_nodes_by_class(prompt_copy, "UltimateSDUpscaleDistributed"):
        node = prompt_copy.get(node_id, {})
        inputs = node.setdefault("inputs", {})
        unique_id = job_id_map.get(node_id, node_id)
        inputs["multi_job_id"] = unique_id
        inputs["is_worker"] = not is_master
        if is_master:
            inputs["enabled_worker_ids"] = enabled_json
            inputs.pop("master_url", None)
            inputs.pop("worker_id", None)
        else:
            inputs["master_url"] = master_url
            inputs["worker_id"] = participant_id
            inputs["enabled_worker_ids"] = enabled_json

    return prompt_copy


async def orchestrate_distributed_execution(
    prompt_obj,
    workflow_meta,
    client_id,
    enabled_worker_ids=None,
    delegate_master=None,
):
    """Core orchestration logic for the /distributed/queue endpoint.

    Returns:
        tuple[str, int]: (prompt_id, worker_count)
    """
    ensure_distributed_state()

    config = load_config()
    requested_ids = enabled_worker_ids if enabled_worker_ids is not None else None
    workers = _resolve_enabled_workers(config, requested_ids)

    # Respect master delegate-only configuration
    if delegate_master is None:
        delegate_master = bool(config.get("settings", {}).get("master_delegate_only", False))

    if not workers and delegate_master:
        debug_log("Delegate-only requested but no workers are enabled. Falling back to master execution.")
        delegate_master = False

    # Filter to active workers
    active_workers = []
    for worker in workers:
        if await _worker_is_active(worker):
            active_workers.append(worker)
        else:
            log(f"[Distributed] Worker {worker['name']} ({worker['id']}) is offline, skipping.")

    if not active_workers and delegate_master:
        debug_log("All workers offline while delegate-only requested; enabling master participation.")
        delegate_master = False

    enabled_ids = [worker["id"] for worker in active_workers]

    discovery_prefix = f"exec_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    job_id_map = _generate_job_id_map(prompt_obj, discovery_prefix)

    if not job_id_map:
        prompt_id = await _queue_master_prompt(prompt_obj, workflow_meta, client_id)
        return prompt_id, 0

    for job_id in job_id_map.values():
        await _ensure_distributed_queue(job_id)

    master_url = _resolve_master_url()
    master_prompt = _apply_participant_overrides(
        prompt_obj,
        "master",
        enabled_ids,
        job_id_map,
        master_url,
        delegate_master,
    )

    if delegate_master:
        collector_ids = _find_nodes_by_class(master_prompt, "DistributedCollector")
        upscale_nodes = _find_nodes_by_class(master_prompt, "UltimateSDUpscaleDistributed")
        if upscale_nodes:
            debug_log(
                "Delegate-only master mode currently does not support UltimateSDUpscaleDistributed nodes; running full prompt on master."
            )
        elif not collector_ids:
            debug_log(
                "Delegate-only master mode requested but no collectors found in master prompt. Running full prompt on master."
            )
        else:
            master_prompt = _prepare_delegate_master_prompt(master_prompt, collector_ids)

    worker_payloads = []
    for worker in active_workers:
        worker_prompt = _apply_participant_overrides(
            prompt_obj,
            worker["id"],
            enabled_ids,
            job_id_map,
            master_url,
            delegate_master,
        )
        worker_payloads.append((worker, worker_prompt))

    if worker_payloads:
        await asyncio.gather(
            *[_dispatch_worker_prompt(worker, wprompt, workflow_meta) for worker, wprompt in worker_payloads]
        )

    prompt_id = await _queue_master_prompt(master_prompt, workflow_meta, client_id)
    return prompt_id, len(worker_payloads)
