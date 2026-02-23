import { renderSidebarContent } from './sidebarRenderer.js';
import { generateUUID } from './constants.js';

export function isRemoteWorker(extension, worker) {
    // Check if explicitly marked as cloud worker
    if (worker.type === "cloud") {
        return true;
    }
    // Otherwise check by host (backward compatibility)
    const parsed = extension._parseHostInput(worker.host || window.location.hostname);
    const host = parsed.host || window.location.hostname;
    return host !== "localhost" && host !== "127.0.0.1" && host !== window.location.hostname;
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
    const parsedHost = isRemote ? extension._parseHostInput(rawHost) : { host: window.location.hostname, port: null };
    const host = isRemote ? parsedHost.host : window.location.hostname;
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

    if ((workerType === "remote" || workerType === "cloud") && !host.trim()) {
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
        const sameHostConflict = extension.config.workers.some((w) => w.id !== workerId && w.port === port && w.host === host.trim());

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

    try {
        await extension.api.updateWorker(workerId, {
            name: name.trim(),
            type: workerType,
            host: isRemote ? host.trim() : null,
            port,
            cuda_device: isRemote ? null : cudaDevice,
            extra_args: isRemote ? null : extraArgs ? extraArgs.trim() : "",
        });

        // Update local config
        worker.name = name.trim();
        worker.type = workerType;
        if (isRemote) {
            worker.host = host.trim();
            delete worker.cuda_device;
            delete worker.extra_args;
        } else {
            delete worker.host;
            worker.cuda_device = cudaDevice;
            worker.extra_args = extraArgs ? extraArgs.trim() : "";
        }
        worker.port = port;

        // Sync to state
        extension.state.updateWorker(workerId, { enabled: worker.enabled });

        extension.app.extensionManager.toast.add({
            severity: "success",
            summary: "Settings Saved",
            detail: `Worker ${name} settings updated`,
            life: 3000,
        });

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
    extension.toggleWorkerExpanded(workerId);

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
            remoteCheckbox.checked = extension.isRemoteWorker(worker);
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
        cuda_device: extension.config.workers.length,
        enabled: true, // Default to enabled for convenience
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
        });

        // Sync to state
        extension.state.updateWorker(newId, { enabled: true });

        extension.app.extensionManager.toast.add({
            severity: "success",
            summary: "Worker Added",
            detail: `New worker created on port ${nextPort}`,
            life: 3000,
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
