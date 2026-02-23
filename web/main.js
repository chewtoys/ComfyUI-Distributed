import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { DistributedUI } from './ui.js';

import { createStateManager } from './stateManager.js';
import { createApiClient } from './apiClient.js';
import { renderSidebarContent } from './sidebarRenderer.js';
import { handleInterruptWorkers, handleClearMemory } from './workerUtils.js';
import { setupInterceptor, executeParallelDistributed } from './executionUtils.js';
import { BUTTON_STYLES, PULSE_ANIMATION_CSS, TIMEOUTS, STATUS_COLORS, generateUUID } from './constants.js';
import { updateTunnelUIElements, refreshTunnelStatus, handleTunnelToggle } from './tunnelManager.js';
import { checkAllWorkerStatuses, checkMasterStatus, getWorkerUrl, checkWorkerStatus, launchWorker, stopWorker, clearLaunchingFlag, loadManagedWorkers, updateWorkerControls, viewWorkerLog, refreshLog, startLogAutoRefresh, stopLogAutoRefresh, toggleWorkerExpanded } from './workerLifecycle.js';
import { isRemoteWorker, isCloudWorker, saveWorkerSettings, cancelWorkerSettings, deleteWorker, addNewWorker } from './workerSettings.js';

class DistributedExtension {
    constructor() {
        this.config = null;
        this.originalQueuePrompt = api.queuePrompt.bind(api);
        this.statusCheckInterval = null;
        this.logAutoRefreshInterval = null;
        this.masterSettingsExpanded = false;
        this.app = app; // Store app reference for toast notifications
        this.tunnelStatus = { status: "unknown" };
        this.tunnelElements = {};
        
        // Initialize centralized state
        this.state = createStateManager();
        
        // Initialize UI component factory
        this.ui = new DistributedUI();
        
        // Initialize API client
        this.api = createApiClient(window.location.origin);
        
        // Initialize status check timeout reference
        this.statusCheckTimeout = null;
        
        // Initialize abort controller for status checks
        this.statusCheckAbortController = null;

        // Inject CSS for pulsing animation
        this.injectStyles();

        this.loadConfig().then(async () => {
            this.registerSidebarTab();
            this.setupInterceptor();
            // Don't start polling until panel opens
            // this.startStatusChecking();
            this.loadManagedWorkers();
            // Detect master IP after everything is set up
            this.detectMasterIP();
        });
    }

    // Debug logging helpers
    log(message, level = "info") {
        if (level === "debug" && !this.config?.settings?.debug) return;
        if (level === "error") {
            console.error(`[Distributed] ${message}`);
        } else {
            console.log(`[Distributed] ${message}`);
        }
    }

    injectStyles() {
        const styleId = 'distributed-styles';
        if (!document.getElementById(styleId)) {
            const style = document.createElement('style');
            style.id = styleId;
            style.textContent = PULSE_ANIMATION_CSS;
            document.head.appendChild(style);
        }
    }

    // --- State & Config Management (Single Source of Truth) ---

    get enabledWorkers() {
        return this.config?.workers?.filter(w => w.enabled) || [];
    }

    get isEnabled() {
        return this.enabledWorkers.length > 0;
    }

    isMasterParticipationEnabled() {
        return !Boolean(this.config?.settings?.master_delegate_only);
    }

    isMasterFallbackActive() {
        return Boolean(this.config?.settings?.master_delegate_only) && this.enabledWorkers.length === 0;
    }

    isMasterParticipating() {
        return this.isMasterParticipationEnabled() || this.isMasterFallbackActive();
    }

    async updateMasterParticipation(enabled) {
        if (!this.config?.settings) {
            this.config.settings = {};
        }
        const delegateOnly = !enabled;
        if (this.config.settings.master_delegate_only === delegateOnly) {
            return;
        }

        await this._updateSetting('master_delegate_only', delegateOnly);

        if (this.panelElement) {
            renderSidebarContent(this, this.panelElement);
        }
    }

    async loadConfig() {
        try {
            this.config = await this.api.getConfig();
            this.log("Loaded config: " + JSON.stringify(this.config), "debug");
            
            // Ensure default flag values
            if (!this.config.settings) {
                this.config.settings = {};
            }
            if (this.config.settings.has_auto_populated_workers === undefined) {
                this.config.settings.has_auto_populated_workers = false;
            }
            
            // Load stored master CUDA device
            this.masterCudaDevice = this.config?.master?.cuda_device ?? undefined;
            
            // Sync to state
            if (this.config.workers) {
                this.config.workers.forEach(w => {
                    this.state.updateWorker(w.id, { enabled: w.enabled });
                });
            }
        } catch (error) {
            this.log("Failed to load config: " + error.message, "error");
            this.config = { workers: [], settings: { has_auto_populated_workers: false } };
        }
    }

    _applyMasterHost(host) {
        if (!host || !this.config) return;
        if (!this.config.master) this.config.master = {};
        this.config.master.host = host;
        const hostInput = document.getElementById('master-host');
        if (hostInput) {
            hostInput.value = host;
        }
    }

    _parseHostInput(value) {
        if (!value) {
            return { host: "", port: null };
        }
        let cleaned = value.trim().replace(/^https?:\/\//i, "");
        cleaned = cleaned.split("/")[0];
        try {
            const url = new URL(`http://${cleaned}`);
            const port = url.port ? parseInt(url.port, 10) : null;
            return {
                host: url.hostname || cleaned,
                port: Number.isFinite(port) ? port : null,
            };
        } catch (error) {
            return { host: cleaned, port: null };
        }
    }

    updateTunnelUIElements(isRunning, isStarting) {
        return updateTunnelUIElements(this, isRunning, isStarting);
    }

    async refreshTunnelStatus() {
        return refreshTunnelStatus(this);
    }

    async handleTunnelToggle(button) {
        return handleTunnelToggle(this, button);
    }

    async updateWorkerEnabled(workerId, enabled) {
        const worker = this.config.workers.find(w => w.id === workerId);
        if (worker) {
            worker.enabled = enabled;
            this.state.updateWorker(workerId, { enabled });

            // Immediately update status dot based on enabled state
            const statusDot = document.getElementById(`status-${workerId}`);
            if (statusDot) {
                if (enabled) {
                    // Enabled: Start with checking state and trigger check
                    this.ui.updateStatusDot(workerId, STATUS_COLORS.OFFLINE_RED, "Checking status...", true);
                    setTimeout(() => this.checkWorkerStatus(worker), TIMEOUTS.STATUS_CHECK_DELAY);
                } else {
                    // Disabled: Set to gray
                    this.ui.updateStatusDot(workerId, STATUS_COLORS.DISABLED_GRAY, "Disabled", false);
                }
            }
        }
        
        try {
            await this.api.updateWorker(workerId, { enabled });
        } catch (error) {
            this.log("Error updating worker: " + error.message, "error");
        }

        if (this.panelElement) {
            await renderSidebarContent(this, this.panelElement);
        }
    }

    async _updateSetting(key, value) {
        // Update local config
        if (!this.config.settings) {
            this.config.settings = {};
        }
        this.config.settings[key] = value;
        
        try {
            await this.api.updateSetting(key, value);

            const prettyKey = key.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
            let detail;
            if (key === 'worker_timeout_seconds') {
                const secs = parseInt(value, 10);
                detail = `Worker Timeout set to ${Number.isFinite(secs) ? secs : value}s`;
            } else if (typeof value === 'boolean') {
                detail = `${prettyKey} ${value ? 'enabled' : 'disabled'}`;
            } else {
                detail = `${prettyKey} set to ${value}`;
            }

            app.extensionManager.toast.add({
                severity: "success",
                summary: "Setting Updated",
                detail,
                life: 2000
            });
        } catch (error) {
            this.log(`Error updating setting '${key}': ${error.message}`, "error");
            app.extensionManager.toast.add({
                severity: "error",
                summary: "Setting Update Failed",
                detail: error.message,
                life: 3000
            });
        }
    }

    // --- UI Rendering ---

    registerSidebarTab() {
        app.extensionManager.registerSidebarTab({
            id: "distributed",
            icon: "pi pi-server",
            title: "Distributed",
            tooltip: "Distributed Control Panel",
            type: "custom",
            render: (el) => {
                this.panelElement = el;
                this.onPanelOpen();
                return renderSidebarContent(this, el);
            },
            destroy: () => {
                this.onPanelClose();
            }
        });
    }
    
    onPanelOpen() {
        this.log("Panel opened - starting status polling", "debug");
        if (!this.statusCheckTimeout) {
            this.checkAllWorkerStatuses();
        }
    }
    
    onPanelClose() {
        this.log("Panel closed - stopping status polling", "debug");
        
        // Cancel any pending status checks
        if (this.statusCheckAbortController) {
            this.statusCheckAbortController.abort();
            this.statusCheckAbortController = null;
        }
        
        // Clear the timeout
        if (this.statusCheckTimeout) {
            clearTimeout(this.statusCheckTimeout);
            this.statusCheckTimeout = null;
        }
        
        this.panelElement = null;
    }

    // updateSummary removed

    // --- Core Logic & Execution ---

    setupInterceptor() {
        setupInterceptor(this);
    }

    async executeParallelDistributed(promptWrapper) {
        return executeParallelDistributed(this, promptWrapper);
    }

    startStatusChecking() {
        this.checkAllWorkerStatuses();
    }

    async checkAllWorkerStatuses() {
        return checkAllWorkerStatuses(this);
    }

    async checkMasterStatus() {
        return checkMasterStatus(this);
    }

    // Helper to build worker URL
    getWorkerUrl(worker, endpoint = '') {
        return getWorkerUrl(this, worker, endpoint);
    }

    async checkWorkerStatus(worker) {
        return checkWorkerStatus(this, worker);
    }

    async launchWorker(workerId) {
        return launchWorker(this, workerId);
    }

    async stopWorker(workerId) {
        return stopWorker(this, workerId);
    }

    async clearLaunchingFlag(workerId) {
        return clearLaunchingFlag(this, workerId);
    }

    // Generic async button action handler
    async handleAsyncButtonAction(button, action, successText, errorText, resetDelay = TIMEOUTS.BUTTON_RESET) {
        const originalText = button.textContent;
        const originalStyle = button.style.cssText;
        button.disabled = true;
        
        try {
            await action();
            button.textContent = successText;
            button.style.cssText = originalStyle;
            button.style.backgroundColor = BUTTON_STYLES.success;
            return true;
        } catch (error) {
            button.textContent = errorText || `Error: ${error.message}`;
            button.style.cssText = originalStyle;
            button.style.backgroundColor = BUTTON_STYLES.error;
            throw error;
        } finally {
            setTimeout(() => {
                button.textContent = originalText;
                button.style.cssText = originalStyle;
                button.disabled = false;
            }, resetDelay);
        }
    }

    /**
     * Cleanup method to stop intervals and listeners
     */
    cleanup() {
        if (this.statusCheckInterval) {
            clearInterval(this.statusCheckInterval);
            this.statusCheckInterval = null;
        }
        
        if (this.logAutoRefreshInterval) {
            clearInterval(this.logAutoRefreshInterval);
            this.logAutoRefreshInterval = null;
        }
        
        if (this.statusCheckTimeout) {
            clearTimeout(this.statusCheckTimeout);
            this.statusCheckTimeout = null;
        }
        
        this.log("Cleaned up intervals", "debug");
    }

    async loadManagedWorkers() {
        return loadManagedWorkers(this);
    }

    updateWorkerControls(workerId) {
        return updateWorkerControls(this, workerId);
    }

    async viewWorkerLog(workerId) {
        return viewWorkerLog(this, workerId);
    }

    async refreshLog(workerId, silent = false) {
        return refreshLog(this, workerId, silent);
    }

    isRemoteWorker(worker) {
        return isRemoteWorker(this, worker);
    }

    isCloudWorker(worker) {
        return isCloudWorker(this, worker);
    }

    getMasterUrl() {
        // Always use the detected/configured master IP for consistency
        if (this.config?.master?.host) {
            const configuredHost = this.config.master.host;
            
            // If the configured host already includes protocol, use as-is
            if (configuredHost.startsWith('http://') || configuredHost.startsWith('https://')) {
                return configuredHost;
            }
            
            // For domain names (not IPs), default to HTTPS
            const isIP = /^(\d{1,3}\.){3}\d{1,3}$/.test(configuredHost);
            const isLocalhost = configuredHost === 'localhost' || configuredHost === '127.0.0.1';
            
            if (!isIP && !isLocalhost && configuredHost.includes('.')) {
                // It's a domain name, use HTTPS
                return `https://${configuredHost}`;
            } else {
                // For IPs and localhost, use current access method
                const port = window.location.port || (window.location.protocol === 'https:' ? '443' : '80');
                if ((window.location.protocol === 'https:' && port === '443') || 
                    (window.location.protocol === 'http:' && port === '80')) {
                    return `${window.location.protocol}//${configuredHost}`;
                }
                return `${window.location.protocol}//${configuredHost}:${port}`;
            }
        }
        
        // If no master IP is set but we're on a network address, use it
        const hostname = window.location.hostname;
        if (hostname !== 'localhost' && hostname !== '127.0.0.1') {
            return window.location.origin;
        }
        
        // Fallback warning - this won't work for remote workers
        this.log("No master host configured - remote workers won't be able to connect. " +
                     "Master host should be auto-detected on startup.", "debug");
        return window.location.origin;
    }

    async detectMasterIP() {
        try {
            // Detect if we're running on Runpod
            const isRunpod = window.location.hostname.endsWith('.proxy.runpod.net');
            if (isRunpod) {
                this.log("Detected Runpod environment", "info");
            }
            
            const data = await this.api.getNetworkInfo();
            this.log("Network info: " + JSON.stringify(data), "debug");
            
            // Store CUDA device info
            if (data.cuda_device !== null && data.cuda_device !== undefined) {
                this.masterCudaDevice = data.cuda_device;
                
                // Store persistently in config if not already set or changed
                if (!this.config.master) this.config.master = {};
                if (this.config.master.cuda_device === undefined || this.config.master.cuda_device !== data.cuda_device) {
                    this.config.master.cuda_device = data.cuda_device;
                    try {
                        await this.api.updateMaster({ cuda_device: data.cuda_device });
                        this.log(`Stored master CUDA device: ${data.cuda_device}`, "debug");
                    } catch (error) {
                        this.log(`Error storing master CUDA device: ${error.message}`, "error");
                    }
                }
                
                // Update the master display with CUDA info
                this.ui.updateMasterDisplay(this);
            }
            
            // Store CUDA device count for auto-population
            if (data.cuda_device_count > 0) {
                this.cudaDeviceCount = data.cuda_device_count;
                this.log(`Detected ${this.cudaDeviceCount} CUDA devices`, "info");
                
                // Auto-populate workers if conditions are met
                const shouldAutoPopulate = 
                    !this.config.settings.has_auto_populated_workers && // Never populated before
                    (!this.config.workers || this.config.workers.length === 0); // No workers exist
                
                this.log(`Auto-population check: has_populated=${this.config.settings.has_auto_populated_workers}, workers=${this.config.workers ? this.config.workers.length : 'null'}, should_populate=${shouldAutoPopulate}`, "debug");
                
                if (shouldAutoPopulate) {
                    this.log(`Auto-populating workers based on ${this.cudaDeviceCount} CUDA devices (excluding master on CUDA ${this.masterCudaDevice})`, "info");
                    
                    const newWorkers = [];
                    let workerNum = 1;
                    let portOffset = 0;
                    
                    for (let i = 0; i < this.cudaDeviceCount; i++) {
                        // Skip the CUDA device used by master
                        if (i === this.masterCudaDevice) {
                            this.log(`Skipping CUDA ${i} (used by master)`, "debug");
                            continue;
                        }
                        
                        const worker = {
                            id: generateUUID(),
                            name: `Worker ${workerNum}`,
                            host: isRunpod ? null : "localhost",
                            port: 8189 + portOffset,
                            cuda_device: i,
                            enabled: true,
                            extra_args: isRunpod ? "--listen" : ""
                        };
                        newWorkers.push(worker);
                        workerNum++;
                        portOffset++;
                    }
                    
                    // Only proceed if we have workers to add
                    if (newWorkers.length > 0) {
                        this.log(`Auto-populating ${newWorkers.length} workers`, "info");
                        
                        // Add workers to config
                        this.config.workers = newWorkers;
                        
                        // Set the flag to prevent future auto-population
                        this.config.settings.has_auto_populated_workers = true;
                        
                        // Save each worker using the update endpoint
                        for (const worker of newWorkers) {
                            try {
                                await this.api.updateWorker(worker.id, worker);
                            } catch (error) {
                                this.log(`Error saving worker ${worker.name}: ${error.message}`, "error");
                            }
                        }
                        
                        // Save the updated settings
                        try {
                            await this.api.updateSetting('has_auto_populated_workers', true);
                        } catch (error) {
                            this.log(`Error saving auto-population flag: ${error.message}`, "error");
                        }
                        
                        this.log(`Auto-populated ${newWorkers.length} workers and saved config`, "info");
                        
                        // Show success notification
                        if (app.extensionManager?.toast) {
                            app.extensionManager.toast.add({
                                severity: "success",
                                summary: "Workers Auto-populated",
                                detail: `Automatically created ${newWorkers.length} workers based on detected CUDA devices`,
                                life: 5000
                            });
                        }
                        
                        // Reload the config to include the new workers
                        await this.loadConfig();
                    } else {
                        this.log("No additional CUDA devices available for workers (all used by master)", "debug");
                    }
                }
            }
            
            // Check if we already have a master host configured
            if (this.config?.master?.host) {
                this.log(`Master host already configured: ${this.config.master.host}`, "debug");
                return;
            }
            
            // For Runpod, use the proxy hostname as master host
            if (isRunpod) {
                const runpodHost = window.location.hostname;
                this.log(`Setting Runpod master host: ${runpodHost}`, "info");
                
                // Save the Runpod host
                await this.api.updateMaster({ host: runpodHost });
                
                // Update local config
                if (!this.config.master) this.config.master = {};
                this.config.master.host = runpodHost;
                
                // Show notification
                if (app.extensionManager?.toast) {
                    app.extensionManager.toast.add({
                        severity: "info",
                        summary: "Runpod Auto-Configuration",
                        detail: `Master host set to ${runpodHost} with --listen flag for workers`,
                        life: 5000
                    });
                }
                return; // Skip regular IP detection for Runpod
            }
            
            // Use the recommended IP from the backend
            if (data.recommended_ip && data.recommended_ip !== '127.0.0.1') {
                this.log(`Auto-detected master IP: ${data.recommended_ip}`, "info");
                
                // Save the detected IP (pass true to suppress notification)
                await this.api.updateMaster({ host: data.recommended_ip });
                
                // Update local config immediately
                if (!this.config.master) this.config.master = {};
                this.config.master.host = data.recommended_ip;
            }
        } catch (error) {
            this.log("Error detecting master IP: " + error.message, "error");
        }
    }

    async saveWorkerSettings(workerId) {
        return saveWorkerSettings(this, workerId);
    }

    cancelWorkerSettings(workerId) {
        return cancelWorkerSettings(this, workerId);
    }

    async deleteWorker(workerId) {
        return deleteWorker(this, workerId);
    }

    async addNewWorker() {
        return addNewWorker(this);
    }

    startLogAutoRefresh(workerId) {
        return startLogAutoRefresh(this, workerId);
    }

    stopLogAutoRefresh() {
        return stopLogAutoRefresh(this);
    }

    toggleWorkerExpanded(workerId) {
        return toggleWorkerExpanded(this, workerId);
    }

    _handleInterruptWorkers(button) {
        return handleInterruptWorkers(this, button);
    }

    _handleClearMemory(button) {
        return handleClearMemory(this, button);
    }
}

app.registerExtension({
    name: "Distributed.Panel",
    async setup() {
        new DistributedExtension();
    }
});
