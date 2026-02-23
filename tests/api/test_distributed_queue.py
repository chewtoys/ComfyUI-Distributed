import importlib.util
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, patch


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def _load_job_routes_module():
    module_path = Path(__file__).resolve().parents[2] / "api" / "job_routes.py"
    package_name = "dist_api_queue_testpkg"

    # Reset package namespace to avoid stale module state across test runs.
    for mod_name in list(sys.modules):
        if mod_name == package_name or mod_name.startswith(f"{package_name}."):
            del sys.modules[mod_name]

    root_pkg = types.ModuleType(package_name)
    root_pkg.__path__ = []
    sys.modules[package_name] = root_pkg

    api_pkg = types.ModuleType(f"{package_name}.api")
    api_pkg.__path__ = []
    sys.modules[f"{package_name}.api"] = api_pkg

    utils_pkg = types.ModuleType(f"{package_name}.utils")
    utils_pkg.__path__ = []
    sys.modules[f"{package_name}.utils"] = utils_pkg

    # aiohttp.web stub
    created_aiohttp_stub = False
    if "aiohttp" not in sys.modules:
        created_aiohttp_stub = True
        aiohttp_module = types.ModuleType("aiohttp")
        aiohttp_module.web = types.SimpleNamespace(
            json_response=lambda payload, status=200: _FakeResponse(payload, status=status)
        )
        sys.modules["aiohttp"] = aiohttp_module

    # server module stub with route decorators
    class _Routes:
        def get(self, _path):
            def _decorator(fn):
                return fn
            return _decorator

        def post(self, _path):
            def _decorator(fn):
                return fn
            return _decorator

    prompt_server_instance = types.SimpleNamespace(
        routes=_Routes(),
        distributed_jobs_lock=None,
        distributed_pending_jobs={},
    )
    server_module = types.ModuleType("server")
    server_module.PromptServer = types.SimpleNamespace(instance=prompt_server_instance)
    sys.modules["server"] = server_module

    # torch stub (only needed to satisfy import)
    if "torch" not in sys.modules:
        torch_module = types.ModuleType("torch")
        torch_module.cuda = types.SimpleNamespace(
            is_available=lambda: False,
            empty_cache=lambda: None,
            ipc_collect=lambda: None,
        )
        sys.modules["torch"] = torch_module

    # PIL stub (only needed to satisfy import)
    if "PIL" not in sys.modules:
        pil_module = types.ModuleType("PIL")
        image_module = types.ModuleType("PIL.Image")
        pil_module.Image = image_module
        sys.modules["PIL"] = pil_module
        sys.modules["PIL.Image"] = image_module

    logging_module = types.ModuleType(f"{package_name}.utils.logging")
    logging_module.debug_log = lambda *_args, **_kwargs: None
    logging_module.log = lambda *_args, **_kwargs: None
    sys.modules[f"{package_name}.utils.logging"] = logging_module

    image_module = types.ModuleType(f"{package_name}.utils.image")
    image_module.pil_to_tensor = lambda *_args, **_kwargs: None
    image_module.ensure_contiguous = lambda tensor: tensor
    sys.modules[f"{package_name}.utils.image"] = image_module

    network_module = types.ModuleType(f"{package_name}.utils.network")

    async def _handle_api_error(_request, error, status=500):
        return _FakeResponse({"status": "error", "message": str(error)}, status=status)

    network_module.handle_api_error = _handle_api_error
    sys.modules[f"{package_name}.utils.network"] = network_module

    constants_module = types.ModuleType(f"{package_name}.utils.constants")
    constants_module.MEMORY_CLEAR_DELAY = 0.0
    constants_module.JOB_INIT_GRACE_PERIOD = 10.0
    sys.modules[f"{package_name}.utils.constants"] = constants_module

    async_helpers_module = types.ModuleType(f"{package_name}.utils.async_helpers")
    async_helpers_module.queue_prompt_payload = AsyncMock(return_value="prompt_local")
    sys.modules[f"{package_name}.utils.async_helpers"] = async_helpers_module

    queue_orchestration_module = types.ModuleType(f"{package_name}.api.queue_orchestration")
    queue_orchestration_module.orchestrate_distributed_execution = AsyncMock(return_value=("prompt_dist", 1))
    sys.modules[f"{package_name}.api.queue_orchestration"] = queue_orchestration_module

    @dataclass(frozen=True)
    class _QueuePayload:
        prompt: dict
        workflow_meta: object
        client_id: str
        delegate_master: object
        enabled_worker_ids: list
        auto_prepare: bool
        trace_execution_id: object

    def _parse_queue_request_payload(data):
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object body")
        prompt = data.get("prompt")
        if not isinstance(prompt, dict):
            raise ValueError("Field 'prompt' must be an object")
        enabled = data.get("enabled_worker_ids")
        if not isinstance(enabled, list):
            raise ValueError("enabled_worker_ids required")
        client_id = data.get("client_id")
        if not isinstance(client_id, str) or not client_id.strip():
            raise ValueError("client_id required")
        return _QueuePayload(
            prompt=prompt,
            workflow_meta=data.get("workflow"),
            client_id=client_id,
            delegate_master=data.get("delegate_master"),
            enabled_worker_ids=enabled,
            auto_prepare=bool(data.get("auto_prepare", True)),
            trace_execution_id=data.get("trace_execution_id"),
        )

    queue_request_module = types.ModuleType(f"{package_name}.api.queue_request")
    queue_request_module.parse_queue_request_payload = _parse_queue_request_payload
    sys.modules[f"{package_name}.api.queue_request"] = queue_request_module

    spec = importlib.util.spec_from_file_location(f"{package_name}.api.job_routes", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)

    if created_aiohttp_stub:
        sys.modules.pop("aiohttp", None)

    return module


job_routes = _load_job_routes_module()


class DistributedQueueEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_distributed_queue_happy_path_returns_prompt_id(self):
        request = _FakeRequest(
            {
                "prompt": {"1": {"class_type": "Node"}},
                "enabled_worker_ids": ["w1"],
                "client_id": "client-1",
                "auto_prepare": True,
            }
        )
        with patch.object(
            job_routes,
            "orchestrate_distributed_execution",
            new=AsyncMock(return_value=("prompt_123", 2)),
        ):
            response = await job_routes.distributed_queue_endpoint(request)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload.get("prompt_id"), "prompt_123")
        self.assertTrue(response.payload.get("auto_prepare_supported"))

    async def test_distributed_queue_missing_prompt_returns_400(self):
        request = _FakeRequest(
            {
                "enabled_worker_ids": ["w1"],
                "client_id": "client-1",
            }
        )
        response = await job_routes.distributed_queue_endpoint(request)
        self.assertEqual(response.status, 400)
        self.assertIn("prompt", response.payload.get("message", "").lower())

    async def test_distributed_queue_missing_enabled_worker_ids_returns_400(self):
        request = _FakeRequest(
            {
                "prompt": {"1": {"class_type": "Node"}},
                "client_id": "client-1",
            }
        )
        response = await job_routes.distributed_queue_endpoint(request)
        self.assertEqual(response.status, 400)
        self.assertIn("enabled_worker_ids", response.payload.get("message", "").lower())


if __name__ == "__main__":
    unittest.main()
