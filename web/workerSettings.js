import { renderSidebarContent } from './sidebarRenderer.js';
import { generateUUID } from './constants.js';
import { parseHostInput } from './urlUtils.js';
import { toggleWorkerExpanded } from './workerLifecycle.js';

const WORKERS_CHANGED_EVENT = "distributed:workers-changed";

function emitWorkersChanged(extension) {
    if (typeof window === "undefined" || typeof window.dispatchEvent !== "function") {
        return;
    }
    window.dispatchEvent(new CustomEvent(WORKERS_CHANGED_EVENT, {
        detail: { workers: extension.config?.workers || [] },
    }));
}

export function isRemoteWorker(extension, worker) {
    const workerType = String(worker?.type || "").toLowerCase();

    // Explicit type always wins over host heuristics.
    if (workerType === "cloud" || workerType === "remote") {
        return true;
    }
    if (workerType === "local") {
        return false;
    }

    // Otherwise check by host (backward compatibility)
    const parsed = parseHostInput(worker?.host || window.location.hostname);
    const host = String(parsed.host || window.location.hostname || "").toLowerCase();
    const currentHost = String(parseHostInput(window.location.hostname).host || window.location.hostname || "").toLowerCase();
    const localHosts = new Set(["", "localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"]);
    return !(localHosts.has(host) || host === currentHost);
}

export function isCloudWorker(extension, worker) {
    return worker.type === "cloud";
}

export async function saveWorkerSettings(extension, workerId) {
    const worker = extension.config.workers.find((w) => w.id === workerId);
    if (!worker) {
        return;
    }

    // Get form values
    const name = document.getElementById(`name-${workerId}`).value;
    const workerType = document.getElementById(`worker-type-${workerId}`).value;
    const isRemote = workerType === "remote" || workerType === "cloud";
    const isCloud = workerType === "cloud";
    const rawHost = isRemote ? document.getElementById(`host-${workerId}`).value : window.location.hostname;
    const parsedHost = isRemote ? parseHostInput(rawHost) : { host: window.location.hostname, port: null };
    const host = isRemote ? parsedHost.host : window.location.hostname;
    const hostTrimmed = (host || "").trim();
    let port = parseInt(document.getElementById(`port-${workerId}`).value);
    const cudaDevice = isRemote ? undefined : parseInt(document.getElementById(`cuda-${workerId}`).value);
    const extraArgs = isRemote ? undefined : document.getElementById(`args-${workerId}`).value;

    if (isRemote && Number.isFinite(parsedHost.port)) {
        port = parsedHost.port;
    }

    // Validate
    if (!name.trim()) {
        extension.app.extensionManager.toast.add({
            severity: "error",
            summary: "Validation Error",
            detail: "Worker name is required",
            life: 3000,
        });
        return;
    }

    if ((workerType === "remote" || workerType === "cloud") && !hostTrimmed) {
        extension.app.extensionManager.toast.add({
            severity: "error",
            summary: "Validation Error",
            detail: "Host is required for remote workers",
            life: 3000,
        });
        return;
    }

    if (!isCloud && (isNaN(port) || port < 1 || port > 65535)) {
        extension.app.extensionManager.toast.add({
            severity: "error",
            summary: "Validation Error",
            detail: "Port must be between 1 and 65535",
            life: 3000,
        });
        return;
    }

    // Check for port conflicts
    // Remote workers can reuse ports, but local workers cannot share ports with each other or master
    if (!isRemote) {
        // Check if port conflicts with master
        const masterPort = parseInt(window.location.port) || (window.location.protocol === "https:" ? 443 : 80);
        if (port === masterPort) {
            extension.app.extensionManager.toast.add({
                severity: "error",
                summary: "Port Conflict",
                detail: `Port ${port} is already in use by the master server`,
                life: 3000,
            });
            return;
        }

        // Check if port conflicts with other local workers
        const localPortConflict = extension.config.workers.some(
            (w) => w.id !== workerId && w.port === port && !w.host // local workers have no host or host is null
        );

        if (localPortConflict) {
            extension.app.extensionManager.toast.add({
                severity: "error",
                summary: "Port Conflict",
                detail: `Port ${port} is already in use by another local worker`,
                life: 3000,
            });
            return;
        }
    } else {
        // For remote workers, only check conflicts with other workers on the same host
        const sameHostConflict = extension.config.workers.some((w) => w.id !== workerId && w.port === port && w.host === hostTrimmed);

        if (sameHostConflict) {
            extension.app.extensionManager.toast.add({
                severity: "error",
                summary: "Port Conflict",
                detail: `Port ${port} is already in use by another worker on ${host}`,
                life: 3000,
            });
            return;
        }
    }

    const wasUnconfiguredRemote =
        (worker.type === "remote" || worker.type === "cloud") &&
        (!String(worker.host || "").trim()) &&
        !worker.enabled;
    const nextEnabled = isRemote && hostTrimmed && wasUnconfiguredRemote ? true : worker.enabled;

    try {
        await extension.api.updateWorker(workerId, {
            name: name.trim(),
            type: workerType,
            host: isRemote ? hostTrimmed : null,
            port,
            cuda_device: isRemote ? null : cudaDevice,
            extra_args: isRemote ? null : extraArgs ? extraArgs.trim() : "",
            enabled: nextEnabled,
        });

        // Update local config
        worker.name = name.trim();
        worker.type = workerType;
        if (isRemote) {
            worker.host = hostTrimmed;
            delete worker.cuda_device;
            delete worker.extra_args;
        } else {
            delete worker.host;
            worker.cuda_device = cudaDevice;
            worker.extra_args = extraArgs ? extraArgs.trim() : "";
        }
        worker.port = port;
        worker.enabled = nextEnabled;

        // Sync to state
        extension.state.updateWorker(workerId, { enabled: nextEnabled });
        emitWorkersChanged(extension);

        extension.app.extensionManager.toast.add({
            severity: "success",
            summary: "Settings Saved",
            detail: nextEnabled && wasUnconfiguredRemote
                ? `Worker ${name} configured and enabled`
                : `Worker ${name} settings updated`,
            life: 3000,
        });

        // Keep post-save card height consistent with the default collapsed layout.
        extension.state.setWorkerExpanded(workerId, false);

        // Refresh the UI
        if (extension.panelElement) {
            renderSidebarContent(extension, extension.panelElement);
        }
    } catch (error) {
        extension.app.extensionManager.toast.add({
            severity: "error",
            summary: "Save Failed",
            detail: error.message,
            life: 5000,
        });
    }
}

export function cancelWorkerSettings(extension, workerId) {
    // Collapse the settings panel
    toggleWorkerExpanded(extension, workerId);

    // Reset form values to original
    const worker = extension.config.workers.find((w) => w.id === workerId);
    if (worker) {
        document.getElementById(`name-${workerId}`).value = worker.name;
        document.getElementById(`host-${workerId}`).value = worker.host || "";
        document.getElementById(`port-${workerId}`).value = worker.port;
        document.getElementById(`cuda-${workerId}`).value = worker.cuda_device || 0;
        document.getElementById(`args-${workerId}`).value = worker.extra_args || "";

        // Reset remote checkbox
        const remoteCheckbox = document.getElementById(`remote-${workerId}`);
        if (remoteCheckbox) {
            remoteCheckbox.checked = isRemoteWorker(extension, worker);
        }
    }
}

export async function deleteWorker(extension, workerId) {
    const worker = extension.config.workers.find((w) => w.id === workerId);
    if (!worker) {
        return;
    }

    // Confirm deletion
    if (!confirm(`Are you sure you want to delete worker "${worker.name}"?`)) {
        return;
    }

    try {
        await extension.api.deleteWorker(workerId);

        // Remove from local config
        const index = extension.config.workers.findIndex((w) => w.id === workerId);
        if (index !== -1) {
            extension.config.workers.splice(index, 1);
        }
        emitWorkersChanged(extension);

        extension.app.extensionManager.toast.add({
            severity: "success",
            summary: "Worker Deleted",
            detail: `Worker ${worker.name} has been removed`,
            life: 3000,
        });

        // Refresh the UI
        if (extension.panelElement) {
            renderSidebarContent(extension, extension.panelElement);
        }
    } catch (error) {
        extension.app.extensionManager.toast.add({
            severity: "error",
            summary: "Delete Failed",
            detail: error.message,
            life: 5000,
        });
    }
}

export async function addNewWorker(extension) {
    const toInt = (value) => {
        const parsed = Number.parseInt(value, 10);
        return Number.isFinite(parsed) ? parsed : null;
    };

    const totalCudaDevices = toInt(extension.cudaDeviceCount);
    const masterCudaDevice = toInt(extension.masterCudaDevice ?? extension.config?.master?.cuda_device);
    const localWorkers = (extension.config?.workers || []).filter((w) => !isRemoteWorker(extension, w));

    let selectedCudaDevice = extension.config.workers.length;
    let fallbackToRemote = false;
    if (totalCudaDevices !== null && totalCudaDevices > 0) {
        const usedCudaDevices = new Set();
        for (const worker of localWorkers) {
            const cudaIdx = toInt(worker.cuda_device);
            if (cudaIdx !== null) {
                usedCudaDevices.add(cudaIdx);
            }
        }
        if (masterCudaDevice !== null) {
            usedCudaDevices.add(masterCudaDevice);
        }

        const availableCudaDevices = [];
        for (let i = 0; i < totalCudaDevices; i++) {
            if (!usedCudaDevices.has(i)) {
                availableCudaDevices.push(i);
            }
        }

        if (availableCudaDevices.length === 0) {
            fallbackToRemote = true;
            selectedCudaDevice = null;
        } else {
            selectedCudaDevice = availableCudaDevices[0];
        }
    }

    // Generate new worker ID using UUID (fallback for non-secure contexts)
    const newId = generateUUID();

    // Find next available port
    const usedPorts = extension.config.workers.map((w) => w.port);
    let nextPort = 8189;
    while (usedPorts.includes(nextPort)) {
        nextPort++;
    }

    // Create new worker object
    const newWorker = {
        id: newId,
        name: `Worker ${extension.config.workers.length + 1}`,
        port: nextPort,
        type: fallbackToRemote ? "remote" : "local",
        host: fallbackToRemote ? "" : null,
        cuda_device: selectedCudaDevice,
        enabled: fallbackToRemote ? false : true, // Remote fallback starts disabled until configured
        extra_args: "",
    };

    // Add to config
    extension.config.workers.push(newWorker);

    // Save immediately
    try {
        await extension.api.updateWorker(newId, {
            name: newWorker.name,
            port: newWorker.port,
            cuda_device: newWorker.cuda_device,
            extra_args: newWorker.extra_args,
            enabled: newWorker.enabled,
            host: newWorker.host,
            type: newWorker.type,
        });

        // Sync to state
        extension.state.updateWorker(newId, { enabled: newWorker.enabled });
        emitWorkersChanged(extension);

        extension.app.extensionManager.toast.add({
            severity: fallbackToRemote ? "warn" : "success",
            summary: fallbackToRemote ? "Remote Worker Added" : "Worker Added",
            detail: fallbackToRemote
                ? `No local GPU available, so a disabled remote worker was added on port ${nextPort}. Configure host and enable it.`
                : `New worker created on port ${nextPort}`,
            life: fallbackToRemote ? 5000 : 3000,
        });

        // Refresh UI and expand the new worker
        extension.state.setWorkerExpanded(newId, true);
        if (extension.panelElement) {
            renderSidebarContent(extension, extension.panelElement);
        }
    } catch (error) {
        extension.app.extensionManager.toast.add({
            severity: "error",
            summary: "Failed to Add Worker",
            detail: error.message,
            life: 5000,
        });
    }
}
