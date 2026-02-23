import asyncio
import time
import uuid

import server

from ..utils.async_helpers import queue_prompt_payload
from ..utils.config import load_config
from ..utils.constants import (
    ORCHESTRATION_MEDIA_SYNC_CONCURRENCY,
    ORCHESTRATION_MEDIA_SYNC_TIMEOUT,
    ORCHESTRATION_WORKER_PROBE_CONCURRENCY,
    ORCHESTRATION_WORKER_PREP_CONCURRENCY,
)
from ..utils.logging import debug_log, log
from ..utils.network import build_master_url
from .orchestration.dispatch import dispatch_worker_prompt, select_active_workers
from .orchestration.media_sync import convert_paths_for_platform, fetch_worker_path_separator, sync_worker_media
from .orchestration.prompt_transform import (
    PromptIndex,
    apply_participant_overrides,
    find_nodes_by_class,
    generate_job_id_map,
    prepare_delegate_master_prompt,
    prune_prompt_for_worker,
)


prompt_server = server.PromptServer.instance


def _generate_execution_trace_id():
    return f"exec_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"


def _trace_prefix(trace_execution_id):
    return f"[Distributed][exec:{trace_execution_id}]"


def _trace_debug(trace_execution_id, message):
    debug_log(f"{_trace_prefix(trace_execution_id)} {message}")


def ensure_distributed_state():
    """Ensure prompt_server has the state used by distributed queue orchestration."""
    if not hasattr(prompt_server, "distributed_pending_jobs"):
        prompt_server.distributed_pending_jobs = {}
    if not hasattr(prompt_server, "distributed_jobs_lock"):
        prompt_server.distributed_jobs_lock = asyncio.Lock()


async def _ensure_distributed_queue(job_id):
    """Ensure a queue exists for the given distributed job ID."""
    ensure_distributed_state()
    async with prompt_server.distributed_jobs_lock:
        if job_id not in prompt_server.distributed_pending_jobs:
            prompt_server.distributed_pending_jobs[job_id] = asyncio.Queue()


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

        raw_port = worker.get("port", worker.get("listen_port", 8188))
        try:
            port = int(raw_port or 8188)
        except (TypeError, ValueError):
            log(f"[Distributed] Invalid port '{raw_port}' for worker {worker_id}; defaulting to 8188.")
            port = 8188

        workers.append(
            {
                "id": worker_id,
                "name": worker.get("name", worker_id),
                "host": worker.get("host"),
                "port": port,
                "type": worker.get("type", "local"),
            }
        )
    return workers


def _parse_positive_int(value, default):
    """Parse positive integer setting values with fallback."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _parse_positive_float(value, default):
    """Parse positive float setting values with fallback."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(1.0, parsed)


def _resolve_orchestration_limits(config):
    """Resolve bounded concurrency/timeouts for worker preparation pipeline."""
    settings = (config or {}).get("settings", {}) or {}
    worker_probe_concurrency = _parse_positive_int(
        settings.get("worker_probe_concurrency"),
        ORCHESTRATION_WORKER_PROBE_CONCURRENCY,
    )
    worker_prep_concurrency = _parse_positive_int(
        settings.get("worker_prep_concurrency"),
        ORCHESTRATION_WORKER_PREP_CONCURRENCY,
    )
    media_sync_concurrency = _parse_positive_int(
        settings.get("media_sync_concurrency"),
        ORCHESTRATION_MEDIA_SYNC_CONCURRENCY,
    )
    media_sync_timeout_seconds = _parse_positive_float(
        settings.get("media_sync_timeout_seconds"),
        ORCHESTRATION_MEDIA_SYNC_TIMEOUT,
    )
    return (
        worker_probe_concurrency,
        worker_prep_concurrency,
        media_sync_concurrency,
        media_sync_timeout_seconds,
    )


async def _prepare_worker_payload(
    worker,
    prompt_index,
    enabled_ids,
    job_id_map,
    master_url,
    delegate_master,
    trace_execution_id,
    worker_prep_semaphore,
    media_sync_semaphore,
    media_sync_timeout_seconds,
):
    """Prepare one worker prompt payload with bounded concurrency and media-sync timeout."""
    async with worker_prep_semaphore:
        worker_prompt = prompt_index.copy_prompt()

        is_remote_like = bool(worker.get("host"))
        if is_remote_like:
            path_separator = await fetch_worker_path_separator(worker, trace_execution_id=trace_execution_id)
            if path_separator:
                worker_prompt = convert_paths_for_platform(worker_prompt, path_separator)

        worker_prompt = prune_prompt_for_worker(worker_prompt)
        worker_prompt = apply_participant_overrides(
            worker_prompt,
            worker["id"],
            enabled_ids,
            job_id_map,
            master_url,
            delegate_master,
            prompt_index,
        )

        if is_remote_like:
            async with media_sync_semaphore:
                try:
                    await asyncio.wait_for(
                        sync_worker_media(worker, worker_prompt, trace_execution_id=trace_execution_id),
                        timeout=media_sync_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    _trace_debug(
                        trace_execution_id,
                        (
                            f"Media sync timed out after {media_sync_timeout_seconds:.1f}s "
                            f"for worker {worker.get('name')} ({worker.get('id')}); continuing dispatch."
                        ),
                    )

        return worker, worker_prompt


async def orchestrate_distributed_execution(
    prompt_obj,
    workflow_meta,
    client_id,
    enabled_worker_ids=None,
    delegate_master=None,
    trace_execution_id=None,
):
    """Core orchestration logic for the /distributed/queue endpoint.

    Returns:
        tuple[str, int]: (prompt_id, worker_count)
    """
    ensure_distributed_state()
    execution_trace_id = trace_execution_id or _generate_execution_trace_id()

    config = load_config()
    use_websocket = bool(config.get("settings", {}).get("websocket_orchestration", False))
    (
        worker_probe_concurrency,
        worker_prep_concurrency,
        media_sync_concurrency,
        media_sync_timeout_seconds,
    ) = _resolve_orchestration_limits(config)
    requested_ids = enabled_worker_ids if enabled_worker_ids is not None else None
    workers = _resolve_enabled_workers(config, requested_ids)
    prompt_index = PromptIndex(prompt_obj)
    _trace_debug(
        execution_trace_id,
        (
            f"Orchestration start: requested_workers={len(workers)}, "
            f"requested_ids={requested_ids if requested_ids is not None else 'enabled_only'}, "
            f"websocket={use_websocket}, "
            f"probe_concurrency={worker_probe_concurrency}, "
            f"prep_concurrency={worker_prep_concurrency}, "
            f"media_sync_concurrency={media_sync_concurrency}, "
            f"media_sync_timeout={media_sync_timeout_seconds:.1f}s"
        ),
    )

    # Respect master delegate-only configuration
    if delegate_master is None:
        delegate_master = bool(config.get("settings", {}).get("master_delegate_only", False))

    if not workers and delegate_master:
        _trace_debug(
            execution_trace_id,
            "Delegate-only requested but no workers are enabled. Falling back to master execution.",
        )
        delegate_master = False

    active_workers, delegate_master = await select_active_workers(
        workers,
        use_websocket,
        delegate_master,
        trace_execution_id=execution_trace_id,
        probe_concurrency=worker_probe_concurrency,
    )

    enabled_ids = [worker["id"] for worker in active_workers]

    discovery_prefix = f"exec_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    job_id_map = generate_job_id_map(prompt_index, discovery_prefix)

    if not job_id_map:
        _trace_debug(execution_trace_id, "No distributed nodes detected; queueing prompt on master only.")
        prompt_id = await queue_prompt_payload(prompt_obj, workflow_meta, client_id)
        return prompt_id, 0

    for job_id in job_id_map.values():
        await _ensure_distributed_queue(job_id)

    master_url = build_master_url(config=config, prompt_server_instance=prompt_server)
    master_prompt = prompt_index.copy_prompt()
    master_prompt = apply_participant_overrides(
        master_prompt,
        "master",
        enabled_ids,
        job_id_map,
        master_url,
        delegate_master,
        prompt_index,
    )

    if delegate_master:
        collector_ids = find_nodes_by_class(master_prompt, "DistributedCollector")
        upscale_nodes = find_nodes_by_class(master_prompt, "UltimateSDUpscaleDistributed")
        if upscale_nodes:
            debug_log(
                "Delegate-only master mode currently does not support UltimateSDUpscaleDistributed nodes; running full prompt on master."
            )
        elif not collector_ids:
            debug_log(
                "Delegate-only master mode requested but no collectors found in master prompt. Running full prompt on master."
            )
        else:
            master_prompt = prepare_delegate_master_prompt(master_prompt, collector_ids)

    if active_workers:
        _trace_debug(
            execution_trace_id,
            "Active distributed workers: "
            + ", ".join(f"{worker['name']} ({worker['id']})" for worker in active_workers),
        )
    worker_payloads = []
    if active_workers:
        worker_prep_semaphore = asyncio.Semaphore(worker_prep_concurrency)
        media_sync_semaphore = asyncio.Semaphore(media_sync_concurrency)
        worker_payloads = await asyncio.gather(
            *[
                _prepare_worker_payload(
                    worker,
                    prompt_index,
                    enabled_ids,
                    job_id_map,
                    master_url,
                    delegate_master,
                    execution_trace_id,
                    worker_prep_semaphore,
                    media_sync_semaphore,
                    media_sync_timeout_seconds,
                )
                for worker in active_workers
            ]
        )

    if worker_payloads:
        await asyncio.gather(
            *[
                dispatch_worker_prompt(
                    worker,
                    wprompt,
                    workflow_meta,
                    client_id,
                    use_websocket=use_websocket,
                    trace_execution_id=execution_trace_id,
                )
                for worker, wprompt in worker_payloads
            ]
        )

    prompt_id = await queue_prompt_payload(master_prompt, workflow_meta, client_id)
    _trace_debug(
        execution_trace_id,
        f"Orchestration complete: prompt_id={prompt_id}, dispatched_workers={len(worker_payloads)}, delegate_master={delegate_master}",
    )
    return prompt_id, len(worker_payloads)
