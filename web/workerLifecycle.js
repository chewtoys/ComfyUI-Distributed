import { BUTTON_STYLES, TIMEOUTS, STATUS_COLORS, ENDPOINTS } from './constants.js';
import { normalizeWorkerUrl } from './urlUtils.js';

export { normalizeWorkerUrl };

function setStatusDotClass(dot, statusClass) {
    if (!dot) {
        return;
    }
    const classes = [
        "worker-status--online",
        "worker-status--offline",
        "worker-status--unknown",
        "worker-status--processing",
    ];
    dot.classList.remove(...classes);
    if (statusClass) {
        dot.classList.add(statusClass);
    }
}

export async function checkAllWorkerStatuses(extension) {
    // Don't continue if panel is closed
    if (!extension.panelElement) {
        return;
    }

    // Create new abort controller for this round of checks
    extension.statusCheckAbortController = new AbortController();

    // Check master status
    checkMasterStatus(extension);

    if (!extension.config || !extension.config.workers) {
        return;
    }

    for (const worker of extension.config.workers) {
        // Check status for enabled workers OR workers that are launching
        if (worker.enabled || extension.state.isWorkerLaunching(worker.id)) {
            checkWorkerStatus(extension, worker);
        }
    }

    // Determine next interval based on current state
    let isActive = extension.state.getMasterStatus() === "processing"; // Master is busy

    // Check workers for activity
    extension.config.workers.forEach((worker) => {
        const ws = extension.state.getWorker(worker.id); // Get worker state
        if (ws.launching || ws.status?.processing) {
            // Launching or processing
            isActive = true;
        }
    });

    // Set next delay: 1s if active, 5s if idle
    const nextInterval = isActive ? 1000 : 5000;

    // Schedule the next check
    extension.statusCheckTimeout = setTimeout(() => checkAllWorkerStatuses(extension), nextInterval);
}

export async function checkMasterStatus(extension) {
    try {
        const response = await fetch(`${window.location.origin}${ENDPOINTS.PROMPT}`, {
            method: "GET",
            signal: AbortSignal.timeout(TIMEOUTS.STATUS_CHECK),
        });

        if (response.ok) {
            const data = await response.json();
            const queueRemaining = data.exec_info?.queue_remaining || 0;
            const isProcessing = queueRemaining > 0;

            // Update master status in state
            extension.state.setMasterStatus(isProcessing ? "processing" : "online");

            // Update master status dot
            const statusDot = document.getElementById("master-status");
            if (statusDot) {
                if (!extension.isMasterParticipating()) {
                    if (isProcessing) {
                        setStatusDotClass(statusDot, "worker-status--processing");
                        statusDot.title = `Orchestrating (${queueRemaining} in queue)`;
                    } else {
                        setStatusDotClass(statusDot, "worker-status--unknown");
                        statusDot.title = "Master orchestrator only";
                    }
                } else if (isProcessing) {
                    setStatusDotClass(statusDot, "worker-status--processing");
                    statusDot.title = `Processing (${queueRemaining} in queue)`;
                } else {
                    setStatusDotClass(statusDot, "worker-status--online");
                    statusDot.title = "Online";
                }
            }
        }
    } catch (error) {
        // Master is always online (we're running on it), so keep it green
        const statusDot = document.getElementById("master-status");
        if (statusDot) {
            setStatusDotClass(
                statusDot,
                extension.isMasterParticipating() ? "worker-status--online" : "worker-status--unknown"
            );
            statusDot.title = extension.isMasterParticipating() ? "Online" : "Master orchestrator only";
        }
    }
}

// Helper to build worker URL
export function getWorkerUrl(extension, worker, endpoint = "") {
    const parsed = extension._parseHostInput(worker.host || window.location.hostname);
    const host = parsed.host || window.location.hostname;
    const resolvedPort = parsed.port || worker.port;

    // Cloud workers always use HTTPS
    const isCloud = worker.type === "cloud";

    // Detect if we're running on Runpod (for local workers on Runpod infrastructure)
    const isRunpodProxy = host.endsWith(".proxy.runpod.net");

    // For local workers on Runpod, construct the port-specific proxy URL
    let finalHost = host;
    if (!worker.host && isRunpodProxy) {
        const match = host.match(/^(.*)\.proxy\.runpod\.net$/);
        if (match) {
            const podId = match[1];
            const domain = "proxy.runpod.net";
            finalHost = `${podId}-${worker.port}.${domain}`;
        } else {
            // Fallback or log error if no match (shouldn't happen)
            console.error(`[Distributed] Failed to parse Runpod proxy host: ${host}`);
        }
    }

    // Determine protocol: HTTPS for cloud, Runpod proxies, or port 443
    const useHttps = isCloud || isRunpodProxy || resolvedPort === 443;
    const protocol = useHttps ? "https" : "http";

    // Only add port if non-standard
    const defaultPort = useHttps ? 443 : 80;
    const needsPort = !isRunpodProxy && resolvedPort !== defaultPort;
    const portStr = needsPort ? `:${resolvedPort}` : "";

    // NOTE: Runpod host rewriting here intentionally diverges from Python-side heuristics
    // in utils.network.build_worker_url. Preserve current browser behavior for now.
    return normalizeWorkerUrl(`${protocol}://${finalHost}${portStr}${endpoint}`);
}

export async function checkWorkerStatus(extension, worker) {
    // Assume caller ensured enabled; proceed with check
    const url = getWorkerUrl(extension, worker, ENDPOINTS.PROMPT);

    try {
        // Combine timeout with abort controller signal
        const timeoutSignal = AbortSignal.timeout(TIMEOUTS.STATUS_CHECK);
        const signal = extension.statusCheckAbortController
            ? AbortSignal.any([timeoutSignal, extension.statusCheckAbortController.signal])
            : timeoutSignal;

        const response = await fetch(url, {
            method: "GET",
            mode: "cors",
            signal,
        });

        if (response.ok) {
            const data = await response.json();
            const queueRemaining = data.exec_info?.queue_remaining || 0;
            const isProcessing = queueRemaining > 0;

            // Update status
            extension.state.setWorkerStatus(worker.id, {
                online: true,
                processing: isProcessing,
                queueCount: queueRemaining,
            });

            // Update status dot based on processing state
            if (isProcessing) {
                extension.ui.updateStatusDot(worker.id, STATUS_COLORS.PROCESSING_YELLOW, `Online - Processing (${queueRemaining} in queue)`, false);
            } else {
                extension.ui.updateStatusDot(worker.id, STATUS_COLORS.ONLINE_GREEN, "Online - Idle", false);
            }

            // Clear launching state since worker is now online
            if (extension.state.isWorkerLaunching(worker.id)) {
                extension.state.setWorkerLaunching(worker.id, false);
                clearLaunchingFlag(extension, worker.id);
            }
        } else {
            throw new Error(`HTTP ${response.status}`);
        }
    } catch (error) {
        // Don't process aborted requests
        if (error.name === "AbortError") {
            return;
        }

        // Worker is offline or unreachable
        extension.state.setWorkerStatus(worker.id, {
            online: false,
            processing: false,
            queueCount: 0,
        });

        // Check if worker is launching
        if (extension.state.isWorkerLaunching(worker.id)) {
            extension.ui.updateStatusDot(worker.id, STATUS_COLORS.PROCESSING_YELLOW, "Launching...", true);
        } else if (worker.enabled) {
            // Only update to red if not currently launching AND still enabled
            extension.ui.updateStatusDot(worker.id, STATUS_COLORS.OFFLINE_RED, "Offline - Cannot connect", false);
        }
        // If disabled, don't update the dot (leave it gray)

        extension.log(`Worker ${worker.id} status check failed: ${error.message}`, "debug");
    }

    // Update control buttons based on new status
    const updatedInPlace = extension.updateWorkerCard?.(worker.id, extension.state.getWorkerStatus(worker.id));
    if (!updatedInPlace) {
        updateWorkerControls(extension, worker.id);
    }
}

export async function launchWorker(extension, workerId) {
    const worker = extension.config.workers.find((w) => w.id === workerId);

    // If worker is disabled, enable it first
    if (!worker.enabled) {
        await extension.updateWorkerEnabled(workerId, true);

        // Update the checkbox UI
        const checkbox = document.getElementById(`gpu-${workerId}`);
        if (checkbox) {
            checkbox.checked = true;
        }
    }

    // Re-query button AFTER updateWorkerEnabled (which may re-render sidebar)
    const launchBtn = document.querySelector(`#controls-${workerId} button`);

    extension.ui.updateStatusDot(workerId, STATUS_COLORS.PROCESSING_YELLOW, "Launching...", true);
    extension.state.setWorkerLaunching(workerId, true);

    // Allow 90 seconds for worker to launch (model loading can take time)
    setTimeout(() => {
        extension.state.setWorkerLaunching(workerId, false);
    }, TIMEOUTS.LAUNCH);

    if (!launchBtn) {
        return;
    }

    try {
        // Disable button immediately
        launchBtn.disabled = true;

        const result = await extension.api.launchWorker(workerId);
        if (result) {
            extension.log(`Launched ${worker.name} (PID: ${result.pid})`, "info");
            if (result.log_file) {
                extension.log(`Log file: ${result.log_file}`, "debug");
            }

            extension.state.setWorkerManaged(workerId, {
                pid: result.pid,
                log_file: result.log_file,
                started_at: Date.now(),
            });

            // Update controls immediately to hide launch button and show stop/log buttons
            updateWorkerControls(extension, workerId);
            setTimeout(() => checkWorkerStatus(extension, worker), TIMEOUTS.STATUS_CHECK);
        }
    } catch (error) {
        // Check if worker was already running
        if (error.message && error.message.includes("already running")) {
            extension.log(`Worker ${worker.name} is already running`, "info");
            updateWorkerControls(extension, workerId);
            setTimeout(() => checkWorkerStatus(extension, worker), TIMEOUTS.STATUS_CHECK_DELAY);
        } else {
            extension.log(`Error launching worker: ${error.message || error}`, "error");

            // Re-enable button on error
            if (launchBtn) {
                launchBtn.disabled = false;
            }
        }
    }
}

export async function stopWorker(extension, workerId) {
    const worker = extension.config.workers.find((w) => w.id === workerId);
    const stopBtn = document.querySelectorAll(`#controls-${workerId} button`)[1];

    // Provide immediate feedback
    if (stopBtn) {
        stopBtn.disabled = true;
        stopBtn.textContent = "Stopping...";
        stopBtn.style.backgroundColor = "#666";
    }

    try {
        const result = await extension.api.stopWorker(workerId);
        if (result) {
            extension.log(`Stopped worker: ${result.message}`, "info");
            extension.state.setWorkerManaged(workerId, null);

            // Immediately update status to offline
            extension.ui.updateStatusDot(workerId, STATUS_COLORS.OFFLINE_RED, "Offline");
            extension.state.setWorkerStatus(workerId, { online: false });

            // Flash success feedback
            if (stopBtn) {
                stopBtn.style.backgroundColor = BUTTON_STYLES.success;
                stopBtn.textContent = "Stopped!";
                setTimeout(() => {
                    updateWorkerControls(extension, workerId);
                }, TIMEOUTS.FLASH_SHORT);
            }

            // Verify status after a short delay
            setTimeout(() => checkWorkerStatus(extension, worker), TIMEOUTS.STATUS_CHECK);
        } else {
            extension.log(`Failed to stop worker: ${result.message}`, "error");

            // Flash error feedback
            if (stopBtn) {
                stopBtn.style.backgroundColor = BUTTON_STYLES.error;
                stopBtn.textContent = result.message.includes("already stopped") ? "Not Running" : "Failed";

                // If already stopped, update status immediately
                if (result.message.includes("already stopped")) {
                    extension.ui.updateStatusDot(workerId, STATUS_COLORS.OFFLINE_RED, "Offline");
                    extension.state.setWorkerStatus(workerId, { online: false });
                }

                setTimeout(() => {
                    updateWorkerControls(extension, workerId);
                }, TIMEOUTS.FLASH_MEDIUM);
            }
        }
    } catch (error) {
        extension.log(`Error stopping worker: ${error}`, "error");

        // Reset button on error
        if (stopBtn) {
            stopBtn.style.backgroundColor = BUTTON_STYLES.error;
            stopBtn.textContent = "Error";
            setTimeout(() => {
                updateWorkerControls(extension, workerId);
            }, TIMEOUTS.FLASH_MEDIUM);
        }
    }
}

export async function clearLaunchingFlag(extension, workerId) {
    try {
        await extension.api.clearLaunchingFlag(workerId);
        extension.log(`Cleared launching flag for worker ${workerId}`, "debug");
    } catch (error) {
        extension.log(`Error clearing launching flag: ${error.message || error}`, "error");
    }
}

export async function loadManagedWorkers(extension) {
    try {
        const result = await extension.api.getManagedWorkers();

        // Check for launching workers
        for (const [workerId, info] of Object.entries(result.managed_workers)) {
            extension.state.setWorkerManaged(workerId, info);

            // If worker is marked as launching, add to launchingWorkers set
            if (info.launching) {
                extension.state.setWorkerLaunching(workerId, true);
                extension.log(`Worker ${workerId} is in launching state`, "debug");
            }
        }

        // Update UI for all workers
        if (extension.config?.workers) {
            extension.config.workers.forEach((w) => updateWorkerControls(extension, w.id));
        }
    } catch (error) {
        extension.log(`Error loading managed workers: ${error}`, "error");
    }
}

export function updateWorkerControls(extension, workerId) {
    const controlsDiv = document.getElementById(`controls-${workerId}`);

    if (!controlsDiv) {
        return;
    }

    const worker = extension.config.workers.find((w) => w.id === workerId);
    if (!worker) {
        return;
    }

    // Skip button updates for remote workers
    if (extension.isRemoteWorker(worker)) {
        return;
    }

    // Ensure we check for string ID
    const managedInfo = extension.state.getWorker(workerId).managed;
    const status = extension.state.getWorkerStatus(workerId);

    // Update button states - buttons are now inside a wrapper div
    const launchBtn = document.getElementById(`launch-${workerId}`);
    const stopBtn = document.getElementById(`stop-${workerId}`);
    const logBtn = document.getElementById(`log-${workerId}`);

    // Show log button immediately if we have log file info (even if worker is still starting)
    if (managedInfo?.log_file && logBtn) {
        logBtn.style.display = "";
    } else if (logBtn && !managedInfo) {
        logBtn.style.display = "none";
    }

    if (status?.online || managedInfo) {
        // Worker is running or we just launched it
        launchBtn.style.display = "none"; // Hide launch button when running

        if (managedInfo) {
            // Only show stop button if we manage this worker
            stopBtn.style.display = "";
            stopBtn.disabled = false;
            stopBtn.textContent = "Stop";
            stopBtn.style.backgroundColor = "#7c4a4a"; // Red when enabled
        } else {
            // Hide stop button for workers launched outside UI
            stopBtn.style.display = "none";
        }
    } else {
        // Worker is not running
        launchBtn.style.display = ""; // Show launch button
        launchBtn.disabled = false;
        launchBtn.textContent = "Launch";
        launchBtn.style.backgroundColor = "#4a7c4a"; // Always green

        stopBtn.style.display = "none"; // Hide stop button when not running
    }
}

export async function viewWorkerLog(extension, workerId) {
    const managedInfo = extension.state.getWorker(workerId).managed;
    if (!managedInfo?.log_file) {
        return;
    }

    const logBtn = document.getElementById(`log-${workerId}`);

    // Provide immediate feedback
    if (logBtn) {
        logBtn.disabled = true;
        logBtn.textContent = "Loading...";
        logBtn.style.backgroundColor = "#666";
    }

    try {
        // Fetch log content
        const data = await extension.api.getWorkerLog(workerId, 1000);

        // Create modal dialog
        extension.ui.showLogModal(extension, workerId, data);

        // Restore button
        if (logBtn) {
            logBtn.disabled = false;
            logBtn.textContent = "View Log";
            logBtn.style.backgroundColor = "#685434"; // Keep the yellow color
        }
    } catch (error) {
        extension.log("Error viewing log: " + error.message, "error");
        extension.app.extensionManager.toast.add({
            severity: "error",
            summary: "Error",
            detail: `Failed to load log: ${error.message}`,
            life: 5000,
        });

        // Flash error and restore button
        if (logBtn) {
            logBtn.style.backgroundColor = BUTTON_STYLES.error;
            logBtn.textContent = "Error";
            setTimeout(() => {
                logBtn.disabled = false;
                logBtn.textContent = "View Log";
                logBtn.style.backgroundColor = "#685434"; // Keep the yellow color
            }, TIMEOUTS.FLASH_LONG);
        }
    }
}

export async function refreshLog(extension, workerId, silent = false) {
    const logContent = document.getElementById("distributed-log-content");
    if (!logContent) {
        return;
    }

    try {
        const data = await extension.api.getWorkerLog(workerId, 1000);

        // Update content
        const shouldAutoScroll = logContent.scrollTop + logContent.clientHeight >= logContent.scrollHeight - 50;
        logContent.textContent = data.content;

        // Auto-scroll if was at bottom
        if (shouldAutoScroll) {
            logContent.scrollTop = logContent.scrollHeight;
        }

        // Only show toast if not in silent mode (manual refresh)
        if (!silent) {
            extension.app.extensionManager.toast.add({
                severity: "success",
                summary: "Log Refreshed",
                detail: "Log content updated",
                life: 2000,
            });
        }
    } catch (error) {
        // Only show error toast if not in silent mode
        if (!silent) {
            extension.app.extensionManager.toast.add({
                severity: "error",
                summary: "Refresh Failed",
                detail: error.message,
                life: 3000,
            });
        }
    }
}

export function startLogAutoRefresh(extension, workerId) {
    // Stop any existing auto-refresh
    stopLogAutoRefresh(extension);

    // Refresh every 2 seconds
    extension.logAutoRefreshInterval = setInterval(() => {
        refreshLog(extension, workerId, true); // silent mode
    }, TIMEOUTS.LOG_REFRESH);
}

export function stopLogAutoRefresh(extension) {
    if (extension.logAutoRefreshInterval) {
        clearInterval(extension.logAutoRefreshInterval);
        extension.logAutoRefreshInterval = null;
    }
}

export function toggleWorkerExpanded(extension, workerId) {
    const gpuDiv = document.querySelector(`[data-worker-id="${workerId}"]`);
    const settingsDiv = gpuDiv?.querySelector(`#settings-${workerId}`) || document.getElementById(`settings-${workerId}`);
    const settingsArrow = gpuDiv?.querySelector(".settings-arrow");

    if (!settingsDiv) {
        return;
    }

    if (extension.state.isWorkerExpanded(workerId)) {
        extension.state.setWorkerExpanded(workerId, false);
        settingsDiv.classList.remove("expanded");
        if (settingsArrow) {
            settingsArrow.style.transform = "rotate(0deg)";
        }
        // Animate padding to 0
        settingsDiv.style.padding = "0 12px";
        settingsDiv.style.marginTop = "0";
        settingsDiv.style.marginBottom = "0";
    } else {
        extension.state.setWorkerExpanded(workerId, true);
        settingsDiv.classList.add("expanded");
        if (settingsArrow) {
            settingsArrow.style.transform = "rotate(90deg)";
        }
        // Animate padding to full
        settingsDiv.style.padding = "12px";
        settingsDiv.style.marginTop = "8px";
        settingsDiv.style.marginBottom = "8px";
    }
}
