from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class QueueRequestPayload:
    prompt: Dict[str, Any]
    workflow_meta: Any
    client_id: str
    delegate_master: Optional[bool]
    enabled_worker_ids: List[str]
    auto_prepare: bool
    trace_execution_id: Optional[str]


def parse_queue_request_payload(data: Any) -> QueueRequestPayload:
    """Parse and validate /distributed/queue payload into a normalized shape."""
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object body")

    auto_prepare_raw = data.get("auto_prepare", True)
    if not isinstance(auto_prepare_raw, bool):
        raise ValueError("auto_prepare must be a boolean when provided")
    auto_prepare = auto_prepare_raw

    prompt = data.get("prompt")
    # Auto-prepare is always on server-side; keep the field for wire compatibility.
    if prompt is None:
        workflow_payload = data.get("workflow")
        if isinstance(workflow_payload, dict):
            candidate_prompt = workflow_payload.get("prompt")
            if isinstance(candidate_prompt, dict):
                prompt = candidate_prompt

    if not isinstance(prompt, dict):
        raise ValueError("Field 'prompt' must be an object")

    enabled_ids_raw = data.get("enabled_worker_ids")
    workers_field = data.get("workers")
    if enabled_ids_raw is None and workers_field is not None:
        if not isinstance(workers_field, list):
            raise ValueError("Field 'workers' must be a list when provided")
        enabled_ids_raw = []
        for entry in workers_field:
            worker_id = entry.get("id") if isinstance(entry, dict) else entry
            if worker_id is not None:
                enabled_ids_raw.append(str(worker_id))

    if enabled_ids_raw is None:
        raise ValueError("enabled_worker_ids required")
    else:
        if not isinstance(enabled_ids_raw, list):
            raise ValueError("enabled_worker_ids must be a list of worker IDs")
        enabled_ids = [str(worker_id).strip() for worker_id in enabled_ids_raw if str(worker_id).strip()]

    delegate_master = data.get("delegate_master")
    if delegate_master is not None and not isinstance(delegate_master, bool):
        raise ValueError("delegate_master must be a boolean when provided")

    client_id = data.get("client_id")
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("client_id required")
    client_id = client_id.strip()

    trace_execution_id = data.get("trace_execution_id")
    if trace_execution_id is not None:
        if not isinstance(trace_execution_id, str):
            raise ValueError("trace_execution_id must be a string when provided")
        trace_execution_id = trace_execution_id.strip() or None

    return QueueRequestPayload(
        prompt=prompt,
        workflow_meta=data.get("workflow"),
        client_id=client_id,
        delegate_master=delegate_master,
        enabled_worker_ids=enabled_ids,
        auto_prepare=auto_prepare,
        trace_execution_id=trace_execution_id,
    )
