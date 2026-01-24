import { BUTTON_STYLES, UI_STYLES, STATUS_COLORS, UI_COLORS, TIMEOUTS } from './constants.js';

const cardConfigs = {
    master: {
        checkbox: { 
            enabled: true,
            masterToggle: true,
            title: "Toggle master participation in workloads"
        },
        statusDot: { 
            id: 'master-status',
            initialColor: (_, extension) => extension.isMasterParticipating() ? STATUS_COLORS.ONLINE_GREEN : STATUS_COLORS.DISABLED_GRAY,
            initialTitle: (_, extension) => extension.isMasterParticipating() ? 'Master participating' : 'Master orchestrator only',
            dynamic: true
        },
        infoText: (data, extension) => {
            const cudaDevice = extension.config?.master?.cuda_device ?? extension.masterCudaDevice;
            const cudaInfo = cudaDevice !== undefined ? `CUDA ${cudaDevice} • ` : '';
            const port = window.location.port || (window.location.protocol === 'https:' ? '443' : '80');
            const participationEnabled = extension.isMasterParticipationEnabled();
            const fallbackActive = extension.isMasterFallbackActive();
            let delegateBadge = '';
            if (!participationEnabled) {
                delegateBadge = fallbackActive
                    ? `<br><small style="color: #6bd06b;">Fallback active • Master executing</small>`
                    : `<br><small style="color: ${UI_COLORS.SECONDARY_TEXT};">Orchestrator-only mode</small>`;
            }
            return `<strong id="master-name-display">${data?.name || extension.config?.master?.name || "Master"}</strong><br><small style="color: ${UI_COLORS.MUTED_TEXT};"><span id="master-cuda-info">${cudaInfo}Port ${port}</span></small>${delegateBadge}`;
        },
        controls: { 
            type: 'master'
        },
        settings: { 
            formType: 'master', 
            id: 'master-settings',
            expandedTracker: 'masterSettingsExpanded'
        },
        hover: true,
        expand: true,
        border: 'solid'
    },
    worker: {
        checkbox: { 
            enabled: true, 
            title: "Enable/disable this worker" 
        },
        statusDot: { 
            dynamic: true,
            initialColor: (data) => data.enabled ? STATUS_COLORS.OFFLINE_RED : STATUS_COLORS.DISABLED_GRAY,
            initialTitle: (data) => data.enabled ? "Checking status..." : "Disabled",
            pulsing: (data) => data.enabled,
            id: (data) => `status-${data.id}`
        },
        infoText: (data, extension) => {
            const isRemote = extension.isRemoteWorker(data);
            const isCloud = data.type === 'cloud';
            
            if (isCloud) {
                // For cloud workers, don't show port (it's always 443)
                return `<strong>${data.name}</strong><br><small style="color: ${UI_COLORS.MUTED_TEXT};">${data.host}</small>`;
            } else if (isRemote) {
                return `<strong>${data.name}</strong><br><small style="color: ${UI_COLORS.MUTED_TEXT};">${data.host}:${data.port}</small>`;
            } else {
                const cudaInfo = data.cuda_device !== undefined ? `CUDA ${data.cuda_device} • ` : '';
                return `<strong>${data.name}</strong><br><small style="color: ${UI_COLORS.MUTED_TEXT};">${cudaInfo}Port ${data.port}</small>`;
            }
        },
        controls: { 
            dynamic: true 
        },
        settings: { 
            formType: 'worker',
            id: (data) => `settings-${data.id}`,
            expandedId: (data) => data?.id
        },
        hover: true,
        expand: true,
        border: 'solid'
    },
    blueprint: {
        checkbox: { 
            type: 'icon', 
            content: '+', 
            width: 42,
            style: `border-right: 2px dashed ${UI_COLORS.BORDER_LIGHT}; color: ${UI_COLORS.ACCENT_COLOR}; font-size: 24px; font-weight: 500;` 
        },
        statusDot: { 
            color: 'transparent', 
            border: `1px solid ${UI_COLORS.BORDER_LIGHT}` 
        },
        infoText: () => `<strong style="color: #aaa; font-size: 16px;">Add New Worker</strong><br><small style="color: ${UI_COLORS.BORDER_LIGHT};">[CUDA] • [Port]</small>`,
        controls: { 
            type: 'ghost', 
            text: 'Configure', 
            style: `border: 1px solid ${UI_COLORS.BORDER_DARK}; background: transparent; color: ${UI_COLORS.BORDER_LIGHT};` 
        },
        hover: 'placeholder',
        expand: false,
        border: 'dashed'
    },
    add: {
        checkbox: { 
            type: 'icon', 
            content: '+',
            width: 43,
            style: `border-right: 1px dashed ${UI_COLORS.BORDER_DARK}; color: ${UI_COLORS.BORDER_LIGHT}; font-size: 18px;` 
        },
        statusDot: { 
            color: 'transparent', 
            border: `1px solid ${UI_COLORS.BORDER_LIGHT}` 
        },
        infoText: () => `<span style="color: ${UI_COLORS.ICON_COLOR}; font-weight: bold; font-size: 13px;">Add New Worker</span>`,
        controls: null,
        hover: 'placeholder',
        expand: false,
        border: 'dashed',
        minHeight: '48px'
    }
};

export class DistributedUI {
    constructor() {
        // UI element styles
        this.styles = UI_STYLES;
    }

    createStatusDot(id, color = "#666", title = "Status") {
        const dot = document.createElement("span");
        if (id) dot.id = id;
        dot.style.cssText = this.styles.statusDot + ` background-color: ${color};`;
        dot.title = title;
        return dot;
    }

    createButton(text, onClick, customStyle = "") {
        const button = document.createElement("button");
        button.textContent = text;
        button.className = "distributed-button";
        button.style.cssText = BUTTON_STYLES.base + customStyle;
        if (onClick) button.onclick = onClick;
        return button;
    }

    createButtonGroup(buttons, style = "") {
        const group = document.createElement("div");
        group.style.cssText = this.styles.buttonGroup + style;
        buttons.forEach(button => group.appendChild(button));
        return group;
    }

    createWorkerControls(workerId, handlers = {}) {
        const controlsDiv = document.createElement("div");
        controlsDiv.id = `controls-${workerId}`;
        controlsDiv.style.cssText = this.styles.controlsDiv;
        
        const buttons = [];
        
        if (handlers.launch) {
            const launchBtn = this.createButton('Launch', handlers.launch);
            launchBtn.id = `launch-${workerId}`;
            launchBtn.title = "Launch this worker instance";
            buttons.push(launchBtn);
        }
        
        if (handlers.stop) {
            const stopBtn = this.createButton('Stop', handlers.stop);
            stopBtn.id = `stop-${workerId}`;
            stopBtn.title = "Stop this worker instance";
            buttons.push(stopBtn);
        }
        
        if (handlers.viewLog) {
            const logBtn = this.createButton('View Log', handlers.viewLog);
            logBtn.id = `log-${workerId}`;
            logBtn.title = "View worker log file";
            buttons.push(logBtn);
        }
        
        buttons.forEach(btn => controlsDiv.appendChild(btn));
        return controlsDiv;
    }

    createFormGroup(label, value, id, type = "text", placeholder = "") {
        const group = document.createElement("div");
        group.style.cssText = this.styles.formGroup;
        
        const labelEl = document.createElement("label");
        labelEl.textContent = label;
        labelEl.htmlFor = id;
        labelEl.style.cssText = this.styles.formLabel;
        
        const input = document.createElement("input");
        input.type = type;
        input.id = id;
        input.value = value;
        input.placeholder = placeholder;
        input.style.cssText = this.styles.formInput;
        
        group.appendChild(labelEl);
        group.appendChild(input);
        return { group, input };
    }


    createInfoBox(text) {
        const box = document.createElement("div");
        box.style.cssText = this.styles.infoBox;
        box.textContent = text;
        return box;
    }

    addHoverEffect(element, onHover, onLeave) {
        element.onmouseover = onHover;
        element.onmouseout = onLeave;
    }

    createCard(type = 'worker', options = {}) {
        const card = document.createElement("div");
        
        switch(type) {
            case 'master':
            case 'worker':
                card.style.cssText = this.styles.workerCard;
                break;
            case 'blueprint':
                card.style.cssText = this.styles.cardBase + this.styles.cardBlueprint;
                if (options.onClick) card.onclick = options.onClick;
                if (options.title) card.title = options.title;
                break;
            case 'add':
                card.style.cssText = this.styles.cardBase + this.styles.cardAdd;
                if (options.onClick) card.onclick = options.onClick;
                if (options.title) card.title = options.title;
                break;
        }
        
        if (options.onMouseEnter) {
            card.addEventListener('mouseenter', options.onMouseEnter);
        }
        if (options.onMouseLeave) {
            card.addEventListener('mouseleave', options.onMouseLeave);
        }
        
        return card;
    }

    createCardColumn(type = 'checkbox', options = {}) {
        const column = document.createElement("div");
        
        switch(type) {
            case 'checkbox':
                column.style.cssText = this.styles.checkboxColumn;
                if (options.title) column.title = options.title;
                break;
            case 'icon':
                column.style.cssText = this.styles.columnBase + this.styles.iconColumn;
                break;
            case 'content':
                column.style.cssText = this.styles.contentColumn;
                break;
        }
        
        return column;
    }

    createInfoRow(options = {}) {
        const row = document.createElement("div");
        row.style.cssText = this.styles.infoRow;
        if (options.onClick) row.onclick = options.onClick;
        return row;
    }

    createWorkerContent() {
        const content = document.createElement("div");
        content.style.cssText = this.styles.workerContent;
        return content;
    }

    createSettingsForm(fields = [], options = {}) {
        const form = document.createElement("div");
        form.style.cssText = this.styles.settingsForm;
        
        fields.forEach(field => {
            if (field.type === 'checkbox') {
                const group = document.createElement("div");
                group.style.cssText = this.styles.checkboxGroup;
                
                const checkbox = document.createElement("input");
                checkbox.type = "checkbox";
                checkbox.id = field.id;
                checkbox.checked = field.checked || false;
                if (field.onChange) checkbox.onchange = field.onChange;
                
                const label = document.createElement("label");
                label.htmlFor = field.id;
                label.textContent = field.label;
                label.style.cssText = this.styles.formLabelClickable;
                
                group.appendChild(checkbox);
                group.appendChild(label);
                form.appendChild(group);
            } else {
                const result = this.createFormGroup(field.label, field.value, field.id, field.type, field.placeholder);
                if (field.groupId) result.group.id = field.groupId;
                if (field.display) result.group.style.display = field.display;
                form.appendChild(result.group);
            }
        });
        
        if (options.buttons) {
            const buttonGroup = this.createButtonGroup(options.buttons, options.buttonStyle || " margin-top: 8px;");
            form.appendChild(buttonGroup);
        }
        
        return form;
    }


    createButtonHelper(text, onClick, style) {
        return this.createButton(text, onClick, style);
    }

    updateMasterDisplay(extension) {
        // Use persistent config value as fallback
        const cudaDevice = extension?.config?.master?.cuda_device ?? extension?.masterCudaDevice;
        
        // Update CUDA info if element exists
        const cudaInfo = document.getElementById('master-cuda-info');
        if (cudaInfo) {
            const port = window.location.port || (window.location.protocol === 'https:' ? '443' : '80');
            if (cudaDevice !== undefined && cudaDevice !== null) {
                cudaInfo.textContent = `CUDA ${cudaDevice} • Port ${port}`;
            } else {
                cudaInfo.textContent = `Port ${port}`;
            }
        }
        
        // Update name if changed
        const nameDisplay = document.getElementById('master-name-display');
        if (nameDisplay && extension?.config?.master?.name) {
            nameDisplay.textContent = extension.config.master.name;
        }
    }

    showToast(app, severity, summary, detail, life = 3000) {
        if (app.extensionManager?.toast?.add) {
            app.extensionManager.toast.add({ severity, summary, detail, life });
        }
    }

    showCloudflareWarning(extension, masterHost) {
        // Remove any existing banner first
        const existingBanner = document.getElementById('cloudflare-warning-banner');
        if (existingBanner) {
            existingBanner.remove();
        }

        // Create warning banner
        const banner = document.createElement('div');
        banner.id = 'cloudflare-warning-banner';
        banner.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            background: #ff9800;
            color: #333;
            padding: 8px 16px;
            text-align: center;
            z-index: 10000;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 16px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.2);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        `;
        
        const messageSpan = document.createElement('span');
        messageSpan.innerHTML = `Connection issue: Master address <strong>${masterHost}</strong> is not reachable. The cloudflare tunnel may be offline.`;
        messageSpan.style.fontSize = '13px';
        
        const resetButton = document.createElement('button');
        resetButton.textContent = 'Reset Master Address';
        resetButton.style.cssText = `
            background: #333;
            color: white;
            border: none;
            padding: 6px 14px;
            border-radius: 4px;
            cursor: pointer;
            font-weight: 500;
            font-size: 13px;
            transition: background 0.2s;
        `;
        resetButton.onmouseover = () => resetButton.style.background = '#555';
        resetButton.onmouseout = () => resetButton.style.background = '#333';
        
        const dismissButton = document.createElement('button');
        dismissButton.textContent = 'Dismiss';
        dismissButton.style.cssText = `
            background: transparent;
            color: #333;
            border: 1px solid #333;
            padding: 6px 14px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 13px;
            transition: opacity 0.2s;
        `;
        dismissButton.onmouseover = () => dismissButton.style.opacity = '0.7';
        dismissButton.onmouseout = () => dismissButton.style.opacity = '1';
        
        // Add click handlers
        resetButton.onclick = async () => {
            resetButton.disabled = true;
            resetButton.textContent = 'Resetting...';
            
            try {
                // Save with empty host - this will trigger auto-detection
                await extension.api.updateMaster({ 
                    name: extension.config?.master?.name || "Master",
                    host: "" 
                });
                
                // Clear the local config host so detectMasterIP() doesn't skip
                if (extension.config?.master) {
                    extension.config.master.host = "";
                }
                
                // The API call above doesn't trigger auto-detection, so we need to do it
                await extension.detectMasterIP();
                
                // Reload config to get the new detected IP
                await extension.loadConfig();
                
                // Log the new master URL for debugging
                const newMasterUrl = extension.getMasterUrl();
                extension.log(`Master host reset. New URL: ${newMasterUrl}`, "info");
                
                // Update UI if sidebar is open
                if (extension.panelElement) {
                    const hostInput = document.getElementById('master-host');
                    if (hostInput) {
                        hostInput.value = extension.config?.master?.host || "";
                    }
                }
                
                // Show success message with the actual URL that will be used
                extension.app.extensionManager.toast.add({
                    severity: "success",
                    summary: "Master Host Reset",
                    detail: `New address: ${newMasterUrl}`,
                    life: 4000
                });
                
                banner.remove();
            } catch (error) {
                resetButton.disabled = false;
                resetButton.textContent = 'Reset Master Host';
                extension.log(`Failed to reset master host: ${error.message}`, "error");
            }
        };
        
        dismissButton.onclick = () => banner.remove();
        
        // Assemble banner
        banner.appendChild(messageSpan);
        banner.appendChild(resetButton);
        banner.appendChild(dismissButton);
        
        // Add to page
        document.body.prepend(banner);
        
        // Auto-dismiss after 30 seconds
        setTimeout(() => {
            if (document.getElementById('cloudflare-warning-banner')) {
                banner.style.transition = 'opacity 0.5s';
                banner.style.opacity = '0';
                setTimeout(() => banner.remove(), 500);
            }
        }, 30000);
    }

    updateStatusDot(workerId, color, title, pulsing = false) {
        const statusDot = document.getElementById(`status-${workerId}`);
        if (!statusDot) return;
        
        statusDot.style.backgroundColor = color;
        statusDot.title = title;
        statusDot.classList.toggle('status-pulsing', pulsing);
    }

    showLogModal(extension, workerId, logData) {
        // Remove any existing modal
        const existingModal = document.getElementById('distributed-log-modal');
        if (existingModal) {
            existingModal.remove();
        }
        
        const worker = extension.config.workers.find(w => w.id === workerId);
        const workerName = worker?.name || `Worker ${workerId}`;
        
        // Create modal container
        const modal = document.createElement('div');
        modal.id = 'distributed-log-modal';
        modal.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.8);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 10000;
        `;
        
        // Create modal content
        const content = document.createElement('div');
        content.style.cssText = `
            background: #1e1e1e;
            border-radius: 8px;
            width: 90%;
            max-width: 1200px;
            height: 80%;
            display: flex;
            flex-direction: column;
            border: 1px solid #444;
        `;
        
        // Header
        const header = document.createElement('div');
        header.style.cssText = `
            padding: 15px 20px;
            border-bottom: 1px solid #444;
            display: flex;
            justify-content: space-between;
            align-items: center;
        `;
        
        const title = document.createElement('h3');
        title.textContent = `${workerName} - Log Viewer`;
        title.style.cssText = 'margin: 0; color: #fff;';
        
        const headerButtons = document.createElement('div');
        headerButtons.style.cssText = 'display: flex; gap: 20px; align-items: center;';
        
        // Auto-refresh container
        const refreshContainer = document.createElement('div');
        refreshContainer.style.cssText = 'display: flex; align-items: center; gap: 4px;';
        
        // Auto-refresh checkbox
        const refreshCheckbox = document.createElement('input');
        refreshCheckbox.type = 'checkbox';
        refreshCheckbox.id = 'log-auto-refresh';
        refreshCheckbox.checked = true; // Enabled by default
        refreshCheckbox.style.cssText = 'cursor: pointer;';
        refreshCheckbox.onchange = (e) => {
            if (e.target.checked) {
                extension.startLogAutoRefresh(workerId);
            } else {
                extension.stopLogAutoRefresh();
            }
        };
        
        const refreshLabel = document.createElement('label');
        refreshLabel.htmlFor = 'log-auto-refresh';
        refreshLabel.style.cssText = 'font-size: 12px; color: #ccc; cursor: pointer; white-space: nowrap;';
        refreshLabel.textContent = 'Auto-refresh';
        
        // Add checkbox and label to container
        refreshContainer.appendChild(refreshCheckbox);
        refreshContainer.appendChild(refreshLabel);
        
        // Close button
        const closeBtn = this.createButton('✕', 
            () => {
                extension.stopLogAutoRefresh();
                modal.remove();
            }, 
            'background-color: #c04c4c;');
        closeBtn.style.cssText += ' padding: 5px 10px; font-size: 14px; font-weight: bold;';
        
        headerButtons.appendChild(refreshContainer);
        headerButtons.appendChild(closeBtn);
        
        header.appendChild(title);
        header.appendChild(headerButtons);
        
        // Log content area
        const logContainer = document.createElement('div');
        logContainer.style.cssText = `
            flex: 1;
            overflow: auto;
            padding: 15px;
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
            font-size: 12px;
            line-height: 1.4;
            color: #ddd;
            background: #0d0d0d;
            white-space: pre-wrap;
            word-wrap: break-word;
        `;
        logContainer.id = 'distributed-log-content';
        logContainer.textContent = logData.content;
        
        // Auto-scroll to bottom
        setTimeout(() => {
            logContainer.scrollTop = logContainer.scrollHeight;
        }, 0);
        
        // Status bar
        const statusBar = document.createElement('div');
        statusBar.style.cssText = `
            padding: 10px 20px;
            border-top: 1px solid #444;
            font-size: 11px;
            color: #888;
        `;
        statusBar.textContent = `Log file: ${logData.log_file}`;
        if (logData.truncated) {
            statusBar.textContent += ` (showing last ${logData.lines_shown} lines of ${this.formatFileSize(logData.file_size)})`;
        }
        
        // Assemble modal
        content.appendChild(header);
        content.appendChild(logContainer);
        content.appendChild(statusBar);
        modal.appendChild(content);
        
        // Close on background click
        modal.onclick = (e) => {
            if (e.target === modal) {
                extension.stopLogAutoRefresh();
                modal.remove();
            }
        };
        
        // Close on Escape key
        const handleEscape = (e) => {
            if (e.key === 'Escape') {
                extension.stopLogAutoRefresh();
                modal.remove();
                document.removeEventListener('keydown', handleEscape);
            }
        };
        document.addEventListener('keydown', handleEscape);
        
        document.body.appendChild(modal);
        
        // Start auto-refresh
        extension.startLogAutoRefresh(workerId);
    }

    formatFileSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    createWorkerSettingsForm(extension, worker) {
        const form = document.createElement("div");
        form.style.cssText = "display: flex; flex-direction: column; gap: 8px;";
        
        // Name field
        const nameGroup = this.createFormGroup("Name:", worker.name, `name-${worker.id}`);
        form.appendChild(nameGroup.group);
        
        // Worker type dropdown
        const typeGroup = document.createElement("div");
        typeGroup.style.cssText = "display: flex; flex-direction: column; gap: 4px; margin: 5px 0;";
        
        const typeLabel = document.createElement("label");
        typeLabel.htmlFor = `worker-type-${worker.id}`;
        typeLabel.textContent = "Worker Type:";
        typeLabel.style.cssText = "font-size: 12px; color: #ccc;";
        
        const typeSelect = document.createElement("select");
        typeSelect.id = `worker-type-${worker.id}`;
        typeSelect.style.cssText = "padding: 4px 8px; background: #333; color: #fff; border: 1px solid #555; border-radius: 4px; font-size: 12px;";
        
        // Create options
        const localOption = document.createElement("option");
        localOption.value = "local";
        localOption.textContent = "Local";
        
        const remoteOption = document.createElement("option");
        remoteOption.value = "remote";
        remoteOption.textContent = "Remote";
        
        const cloudOption = document.createElement("option");
        cloudOption.value = "cloud";
        cloudOption.textContent = "Cloud";
        
        typeSelect.appendChild(localOption);
        typeSelect.appendChild(remoteOption);
        typeSelect.appendChild(cloudOption);
        
        // Create powered by Runpod text (initially hidden)
        const runpodText = document.createElement("a");
        runpodText.id = `runpod-text-${worker.id}`;
        runpodText.href = "https://github.com/robertvoy/ComfyUI-Distributed/blob/main/docs/worker-setup-guides.md#cloud-workers";
        runpodText.target = "_blank";
        runpodText.textContent = "Deploy Cloud Worker with Runpod";
        runpodText.style.cssText = "font-size: 12px; color: #4a90e2; text-decoration: none; margin-top: 4px; display: none; cursor: pointer;";
        
        // Store the onchange function to be assigned later
        const createOnChangeHandler = () => {
            return (e) => {
                const workerType = e.target.value;
                // Show/hide relevant fields
                const hostGroup = document.getElementById(`host-group-${worker.id}`);
                const hostInput = document.getElementById(`host-${worker.id}`);
                const portGroup = document.getElementById(`port-group-${worker.id}`);
                const portInput = document.getElementById(`port-${worker.id}`);
                const cudaGroup = document.getElementById(`cuda-group-${worker.id}`);
                const argsGroup = document.getElementById(`args-group-${worker.id}`);
                const runpodTextElem = document.getElementById(`runpod-text-${worker.id}`);
                
                // Check if elements exist before accessing them
                if (!hostGroup || !portGroup || !cudaGroup || !argsGroup || !runpodTextElem || !hostInput || !portInput) {
                    return; // Elements not ready yet
                }
                
                if (workerType === "local") {
                    hostGroup.style.display = "none";
                    portGroup.style.display = "flex";
                    cudaGroup.style.display = "flex";
                    argsGroup.style.display = "flex";
                    runpodTextElem.style.display = "none";
                } else if (workerType === "remote") {
                    hostGroup.style.display = "flex";
                    portGroup.style.display = "flex";
                    cudaGroup.style.display = "none";
                    argsGroup.style.display = "none";
                    runpodTextElem.style.display = "none";
                    // Update placeholder for remote workers
                    hostInput.placeholder = "e.g., 192.168.1.100";
                    // If switching to remote and host is localhost, clear it
                    if (hostInput.value === "localhost" || hostInput.value === "127.0.0.1") {
                        hostInput.value = "";
                    }
                } else if (workerType === "cloud") {
                    hostGroup.style.display = "flex";
                    portGroup.style.display = "flex"; // Keep port visible for cloud workers
                    cudaGroup.style.display = "none";
                    argsGroup.style.display = "none";
                    runpodTextElem.style.display = "block";
                    // Update placeholder for cloud workers
                    hostInput.placeholder = "e.g., your-cloud-worker.trycloudflare.com";
                    // Set port to 443 for cloud workers
                    portInput.value = "443";
                    // If switching to cloud and host is localhost, clear it
                    if (hostInput.value === "localhost" || hostInput.value === "127.0.0.1") {
                        hostInput.value = "";
                    }
                }
            };
        };
        
        typeGroup.appendChild(typeLabel);
        typeGroup.appendChild(typeSelect);
        typeGroup.appendChild(runpodText);
        form.appendChild(typeGroup);
        
        // Host field (only for remote workers)
        const hostGroup = this.createFormGroup("Host:", worker.host || "", `host-${worker.id}`, "text", "e.g., 192.168.1.100");
        hostGroup.group.id = `host-group-${worker.id}`;
        hostGroup.group.style.display = (extension.isRemoteWorker(worker) || worker.type === "cloud") ? "flex" : "none";
        form.appendChild(hostGroup.group);
        
        // Port field
        const portGroup = this.createFormGroup("Port:", worker.port, `port-${worker.id}`, "number");
        portGroup.group.id = `port-group-${worker.id}`;
        form.appendChild(portGroup.group);
        
        // CUDA Device field (only for local workers)
        const cudaGroup = this.createFormGroup("CUDA Device:", worker.cuda_device || 0, `cuda-${worker.id}`, "number");
        cudaGroup.group.id = `cuda-group-${worker.id}`;
        cudaGroup.group.style.display = (extension.isRemoteWorker(worker) || worker.type === "cloud") ? "none" : "flex";
        form.appendChild(cudaGroup.group);
        
        // Extra Args field (only for local workers)
        const argsGroup = this.createFormGroup("Extra Args:", worker.extra_args || "", `args-${worker.id}`);
        argsGroup.group.id = `args-group-${worker.id}`;
        argsGroup.group.style.display = (extension.isRemoteWorker(worker) || worker.type === "cloud") ? "none" : "flex";
        form.appendChild(argsGroup.group);
        
        // Buttons
        const saveBtn = this.createButton("Save", 
            () => extension.saveWorkerSettings(worker.id),
            "background-color: #4a7c4a;");
        saveBtn.style.cssText = BUTTON_STYLES.base + BUTTON_STYLES.success;
        
        const cancelBtn = this.createButton("Cancel", 
            () => extension.cancelWorkerSettings(worker.id),
            "background-color: #555;");
        cancelBtn.style.cssText = BUTTON_STYLES.base + BUTTON_STYLES.cancel;
        
        const deleteBtn = this.createButton("Delete", 
            () => extension.deleteWorker(worker.id),
            "background-color: #7c4a4a;");
        deleteBtn.style.cssText = BUTTON_STYLES.base + BUTTON_STYLES.error + BUTTON_STYLES.marginLeftAuto;
        
        const buttonGroup = this.createButtonGroup([saveBtn, cancelBtn, deleteBtn], " margin-top: 8px;");
        form.appendChild(buttonGroup);
        
        // Assign the onchange handler now that all elements are created
        typeSelect.onchange = createOnChangeHandler();
        
        // Set initial value and trigger state after all DOM elements are created
        if (worker.type === "cloud") {
            typeSelect.value = "cloud";
            // Show Runpod text immediately for cloud workers
            runpodText.style.display = "block";
        } else if (extension.isRemoteWorker(worker)) {
            typeSelect.value = "remote";
        } else {
            typeSelect.value = "local";
        }
        
        // Trigger initial state now that all elements exist
        typeSelect.dispatchEvent(new Event('change'));
        
        return form;
    }

    createSettingsToggle() {
        const settingsRow = document.createElement("div");
        settingsRow.style.cssText = this.styles.settingsToggle;
        
        const settingsTitle = document.createElement("h4");
        settingsTitle.textContent = "Settings";
        settingsTitle.style.cssText = "margin: 0; font-size: 14px;";
        
        const settingsToggle = document.createElement("span");
        settingsToggle.textContent = "▶"; // Right arrow when collapsed
        settingsToggle.style.cssText = "font-size: 12px; color: #888; transition: all 0.2s ease;";
        
        settingsRow.appendChild(settingsToggle);
        settingsRow.appendChild(settingsTitle);
        
        return { settingsRow, settingsToggle };
    }


    createCheckboxOrIconColumn(config, data, extension) {
        const column = this.createCardColumn('checkbox');
        
        if (config?.type === 'icon') {
            column.style.flex = `0 0 ${config.width || 44}px`;
            column.innerHTML = config.content || '+';
            if (config.style) {
                const styles = config.style.split(';').filter(s => s.trim());
                styles.forEach(style => {
                    const [prop, value] = style.split(':').map(s => s.trim());
                    if (prop && value) {
                        column.style[prop.replace(/-([a-z])/g, (g) => g[1].toUpperCase())] = value;
                    }
                });
            }
        } else {
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.id = `gpu-${data?.id || 'master'}`;
            checkbox.checked = config?.checked !== undefined ? config.checked : data?.enabled;
            checkbox.disabled = config?.disabled || false;
            checkbox.style.cssText = `cursor: ${config?.disabled ? 'default' : 'pointer'}; width: 16px; height: 16px;`;
            
            if (config?.opacity) checkbox.style.opacity = config.opacity;
            if (config?.title) column.title = config.title;
            
            const isMasterToggle = config?.masterToggle && typeof extension.isMasterParticipating === 'function';
            if (isMasterToggle) {
                const participationEnabled = extension.isMasterParticipationEnabled();
                const fallbackActive = extension.isMasterFallbackActive();
                const buildTitle = (enabled, fallback) => {
                    if (enabled) {
                        return "Master participating • Click to switch to orchestrator-only";
                    }
                    if (fallback) {
                        return "No workers selected • Master fallback execution active";
                    }
                    return "Master orchestrator-only • Click to re-enable participation";
                };

                checkbox.checked = participationEnabled;
                checkbox.style.pointerEvents = "none";
                column.style.cursor = "pointer";
                column.title = buildTitle(participationEnabled, fallbackActive);
                column.onclick = async (event) => {
                    if (event) {
                        event.stopPropagation();
                        event.preventDefault();
                    }
                    const nextState = !extension.isMasterParticipationEnabled();
                    const nextFallback = !nextState && extension.enabledWorkers.length === 0;
                    checkbox.checked = nextState;
                    column.title = buildTitle(nextState, nextFallback);
                    await extension.updateMasterParticipation(nextState);
                };
            } else if (config?.enabled && !config?.disabled && data?.id) {
                checkbox.style.pointerEvents = "none";
                column.style.cursor = "pointer";
                column.onclick = async () => {
                    checkbox.checked = !checkbox.checked;
                    await extension.updateWorkerEnabled(data.id, checkbox.checked);
                    extension.updateSummary();
                };
            }
            
            column.appendChild(checkbox);
        }
        
        return column;
    }

    createStatusDotHelper(config, data, extension) {
        let color = config.color || "#666";
        let title = config.title || "Status";
        let id = config.id;
        
        if (typeof config.initialColor === 'function') {
            color = config.initialColor(data, extension);
        }
        if (typeof config.initialTitle === 'function') {
            title = config.initialTitle(data, extension);
        }
        if (typeof config.id === 'function') {
            id = config.id(data);
        }
        
        const dot = this.createStatusDot(id, color, title);
        
        if (config.border) {
            dot.style.border = config.border;
        }
        
        if (config.pulsing && (typeof config.pulsing !== 'function' || config.pulsing(data))) {
            dot.classList.add('status-pulsing');
        }
        
        return dot;
    }

    createSettingsToggleHelper(expandedId, extension) {
        const arrow = document.createElement("span");
        arrow.className = "settings-arrow";
        arrow.innerHTML = "▶";
        arrow.style.cssText = this.styles.settingsArrow;
        
        const isExpanded = typeof expandedId === 'function' ? 
            extension.state.isWorkerExpanded(expandedId(extension)) : 
            (expandedId === 'master' ? false : extension.state.isWorkerExpanded(expandedId));
            
        if (isExpanded) {
            arrow.style.transform = "rotate(90deg)";
        }
        
        return arrow;
    }

    createControlsSection(config, data, extension, isRemote) {
        if (!config) return null;
        
        const controlsDiv = document.createElement("div");
        controlsDiv.id = `controls-${data?.id || 'master'}`;
        controlsDiv.style.cssText = this.styles.controlsDiv;
        
        // Always create a wrapper div for consistent layout
        const controlsWrapper = document.createElement("div");
        controlsWrapper.style.cssText = this.styles.controlsWrapper;
        
        if (config.type === 'master') {
            const participationEnabled = extension.isMasterParticipationEnabled();
            const fallbackActive = extension.isMasterFallbackActive();
            let message;
            const badge = document.createElement("div");
            badge.style.cssText = this.styles.infoBox;
            if (fallbackActive) {
                message = "No workers selected. Master fallback execution active.";
                badge.textContent = message;
                badge.style.backgroundColor = "#243024";
                badge.style.color = "#6bd06b";
                badge.style.border = "1px solid #335533";
            } else if (!participationEnabled) {
                message = "Master disabled: running as orchestrator only.";
                badge.textContent = message;
                badge.style.backgroundColor = "#3a3a3a";
                badge.style.color = "#ffcc66";
            } else {
                message = "Master participating in workflows.";
                badge.textContent = message;
            }
            controlsWrapper.appendChild(badge);
        } else if (config.dynamic && data) {
            if (isRemote) {
                const isCloud = data.type === 'cloud';
                const workerTypeText = isCloud ? "Cloud worker" : "Remote worker";
                const remoteInfo = this.createButton(workerTypeText, null, BUTTON_STYLES.info);
                remoteInfo.style.cssText = BUTTON_STYLES.base + BUTTON_STYLES.workerControl + BUTTON_STYLES.info + " color: #999; cursor: default;";
                remoteInfo.disabled = true;
                controlsWrapper.appendChild(remoteInfo);
            } else {
                const controls = this.createWorkerControls(data.id, {
                    launch: () => extension.launchWorker(data.id),
                    stop: () => extension.stopWorker(data.id),
                    viewLog: () => extension.viewWorkerLog(data.id)
                });
                
                const launchBtn = controls.querySelector(`#launch-${data.id}`);
                const stopBtn = controls.querySelector(`#stop-${data.id}`);
                const logBtn = controls.querySelector(`#log-${data.id}`);
                
                launchBtn.style.cssText = BUTTON_STYLES.base + BUTTON_STYLES.workerControl + BUTTON_STYLES.launch;
                launchBtn.title = "Launch worker (runs in background with logging)";
                
                stopBtn.style.cssText = BUTTON_STYLES.base + BUTTON_STYLES.workerControl + BUTTON_STYLES.stop + BUTTON_STYLES.hidden;
                stopBtn.title = "Stop worker";
                
                logBtn.style.cssText = BUTTON_STYLES.base + BUTTON_STYLES.workerControl + BUTTON_STYLES.log + BUTTON_STYLES.hidden;
                
                while (controls.firstChild) {
                    controlsWrapper.appendChild(controls.firstChild);
                }
            }
        } else if (config.type === 'info') {
            const infoBtn = this.createButton(config.text, null, config.style || "");
            infoBtn.style.cssText = BUTTON_STYLES.base + BUTTON_STYLES.workerControl + (config.style || BUTTON_STYLES.info) + " cursor: default;";
            infoBtn.disabled = true;
            controlsWrapper.appendChild(infoBtn);
        } else if (config.type === 'ghost') {
            const ghostBtn = document.createElement("button");
            ghostBtn.style.cssText = `flex: 1; padding: 5px 14px; font-size: 11px; font-weight: 500; border-radius: 4px; cursor: default; ${config.style || ""}`;
            ghostBtn.textContent = config.text;
            ghostBtn.disabled = true;
            controlsWrapper.appendChild(ghostBtn);
        }
        
        controlsDiv.appendChild(controlsWrapper);
        return controlsDiv;
    }

    createSettingsSection(config, data, extension) {
        const settingsDiv = document.createElement("div");
        const settingsId = typeof config.id === 'function' ? config.id(data) : config.id;
        settingsDiv.id = settingsId;
        settingsDiv.className = "worker-settings";
        
        const expandedId = typeof config.expandedId === 'function' ? config.expandedId(data) : config.expandedId;
        const isExpanded = expandedId === 'master-settings' ? false : extension.state.isWorkerExpanded(expandedId);
        
        settingsDiv.style.cssText = this.styles.workerSettings;
        
        if (isExpanded) {
            settingsDiv.classList.add("expanded");
            settingsDiv.style.padding = "12px";
            settingsDiv.style.marginTop = "8px";
            settingsDiv.style.marginBottom = "8px";
        }
        
        let settingsForm;
        if (config.formType === 'master') {
            settingsForm = this.createMasterSettingsForm(extension, data);
        } else if (config.formType === 'worker') {
            settingsForm = this.createWorkerSettingsForm(extension, data);
        }
        
        if (settingsForm) {
            settingsDiv.appendChild(settingsForm);
        }
        
        return settingsDiv;
    }

    createMasterSettingsForm(extension, data) {
        const settingsForm = document.createElement("div");
        settingsForm.style.cssText = "display: flex; flex-direction: column; gap: 8px;";
        
        const nameResult = this.createFormGroup("Name:", extension.config?.master?.name || "Master", "master-name");
        settingsForm.appendChild(nameResult.group);
        
        const hostResult = this.createFormGroup("Host:", extension.config?.master?.host || "", "master-host", "text", "Auto-detect if empty");
        settingsForm.appendChild(hostResult.group);

        // Cloudflare tunnel toggle (simple button inside master settings)
        const tunnelBtn = this.createButton("Enable Cloudflare Tunnel", (e) => extension.handleTunnelToggle(e.target), "background-color: #665533;");
        tunnelBtn.id = "cloudflare-tunnel-button";
        tunnelBtn.style.cssText = BUTTON_STYLES.base + " background-color: #665533; margin: 4px 0 -5px 0;";
        settingsForm.appendChild(tunnelBtn);
        extension.tunnelElements = { button: tunnelBtn };
        extension.updateTunnelUIElements();
        
        const saveBtn = this.createButton("Save", async () => {
            const nameInput = document.getElementById('master-name');
            const hostInput = document.getElementById('master-host');
            
            if (!extension.config.master) extension.config.master = {};
            extension.config.master.name = nameInput.value.trim() || "Master";
            
            const hostValue = hostInput.value.trim();
            
            await extension.api.updateMaster({
                host: hostValue,
                name: extension.config.master.name
            });
            
            // Reload config to refresh any updated values
            await extension.loadConfig();
            
            // If host was emptied, trigger auto-detection
            if (!hostValue) {
                extension.log("Host field cleared, triggering IP auto-detection", "debug");
                await extension.detectMasterIP();
                // Reload config again to get the auto-detected IP
                await extension.loadConfig();
                // Update the input field with the detected IP
                document.getElementById('master-host').value = extension.config?.master?.host || "";
            }
            
            document.getElementById('master-name-display').textContent = extension.config.master.name;
            this.updateMasterDisplay(extension);
            
            // Show toast notification
            if (extension.app?.extensionManager?.toast) {
                const message = !hostValue ? 
                    "Master settings saved and IP auto-detected" : 
                    "Master settings saved successfully";
                extension.app.extensionManager.toast.add({
                    severity: "success",
                    summary: "Master Updated",
                    detail: message,
                    life: 3000
                });
            }
            
            saveBtn.textContent = "Saved!";
            setTimeout(() => { saveBtn.textContent = "Save"; }, TIMEOUTS.FLASH_LONG);
        }, "background-color: #4a7c4a;");
        saveBtn.style.cssText = BUTTON_STYLES.base + BUTTON_STYLES.success;
        
        const cancelBtn = this.createButton("Cancel", () => {
            document.getElementById('master-name').value = extension.config?.master?.name || "Master";
            document.getElementById('master-host').value = extension.config?.master?.host || "";
        }, "background-color: #555;");
        cancelBtn.style.cssText = BUTTON_STYLES.base + BUTTON_STYLES.cancel;
        
        const buttonGroup = this.createButtonGroup([saveBtn, cancelBtn], " margin-top: 8px;");
        settingsForm.appendChild(buttonGroup);
        
        return settingsForm;
    }

    addPlaceholderHover(card, leftColumn, entityType) {
        card.onmouseover = () => {
            if (entityType === 'blueprint') {
                card.style.borderColor = "#777";
                card.style.backgroundColor = "rgba(255, 255, 255, 0.05)";
                leftColumn.style.color = "#999";
            } else {
                card.style.borderColor = "#666";
                card.style.backgroundColor = "rgba(255, 255, 255, 0.02)";
                leftColumn.style.color = "#888";
                leftColumn.style.borderColor = "#666";
            }
        };
        
        card.onmouseout = () => {
            if (entityType === 'blueprint') {
                card.style.borderColor = "#555";
                card.style.backgroundColor = "rgba(255, 255, 255, 0.02)";
                leftColumn.style.color = "#777";
            } else {
                card.style.borderColor = "#444";
                card.style.backgroundColor = "transparent";
                leftColumn.style.color = "#555";
                leftColumn.style.borderColor = "#444";
            }
        };
    }

    renderEntityCard(entityType, data, extension) {
        const config = cardConfigs[entityType] || {};
        const isPlaceholder = entityType === 'blueprint' || entityType === 'add';
        const isWorker = entityType === 'worker';
        const isMaster = entityType === 'master';
        const isRemote = isWorker && extension.isRemoteWorker(data);

        const cardOptions = { 
            onClick: isPlaceholder ? data?.onClick : null 
        };
        if (isPlaceholder) {
            cardOptions.title = entityType === 'blueprint' ? "Click to add your first worker" : "Click to add a new worker";
        }
        const card = this.createCard(entityType, cardOptions);

        const leftColumn = this.createCheckboxOrIconColumn(config.checkbox, data, extension);
        card.appendChild(leftColumn);

        const rightColumn = this.createCardColumn('content');

        const infoRow = this.createInfoRow();
        if (config.infoRowPadding) {
            infoRow.style.padding = config.infoRowPadding;
        }
        if (config.minHeight === 'auto') {
            infoRow.style.minHeight = 'auto';
        } else if (config.minHeight) {
            infoRow.style.minHeight = config.minHeight;
        }
        if (config.expand) {
            infoRow.title = "Click to expand settings";
            infoRow.onclick = () => {
                if (isMaster) {
                    const masterSettingsExpanded = !extension.masterSettingsExpanded;
                    extension.masterSettingsExpanded = masterSettingsExpanded;
                    const masterSettingsDiv = document.getElementById("master-settings");
                    const arrow = infoRow.querySelector('.settings-arrow');
                    if (masterSettingsExpanded) {
                        masterSettingsDiv.classList.add("expanded");
                        masterSettingsDiv.style.padding = "12px";
                        masterSettingsDiv.style.marginTop = "8px";
                        masterSettingsDiv.style.marginBottom = "8px";
                        arrow.style.transform = "rotate(90deg)";
                    } else {
                        masterSettingsDiv.classList.remove("expanded");
                        masterSettingsDiv.style.padding = "0 12px";
                        masterSettingsDiv.style.marginTop = "0";
                        masterSettingsDiv.style.marginBottom = "0";
                        arrow.style.transform = "rotate(0deg)";
                    }
                } else {
                    extension.toggleWorkerExpanded(data.id);
                }
            };
        }

        const workerContent = this.createWorkerContent();
        if (entityType === 'add') {
            workerContent.style.alignItems = "center";
        }

        const statusDot = this.createStatusDotHelper(config.statusDot, data, extension);
        workerContent.appendChild(statusDot);

        const infoSpan = document.createElement("span");
        infoSpan.innerHTML = config.infoText(data, extension);
        workerContent.appendChild(infoSpan);

        infoRow.appendChild(workerContent);

        let settingsArrow;
        if (config.expand) {
            const expandedId = config.settings?.expandedId || (isMaster ? 'master' : data?.id);
            settingsArrow = this.createSettingsToggleHelper(expandedId, extension);
            if (isMaster && !extension.masterSettingsExpanded) {
                settingsArrow.style.transform = "rotate(0deg)";
            }
            infoRow.appendChild(settingsArrow);
        }

        rightColumn.appendChild(infoRow);

        if (config.hover === true) {
            rightColumn.onmouseover = () => {
                rightColumn.style.backgroundColor = "#333";
                if (settingsArrow) settingsArrow.style.color = "#fff";
            };
            rightColumn.onmouseout = () => {
                rightColumn.style.backgroundColor = "transparent";
                if (settingsArrow) settingsArrow.style.color = "#888";
            };
        }

        const controlsDiv = this.createControlsSection(config.controls, data, extension, isRemote);
        if (controlsDiv) {
            rightColumn.appendChild(controlsDiv);
        }

        if (config.settings) {
            const settingsDiv = this.createSettingsSection(config.settings, data, extension);
            rightColumn.appendChild(settingsDiv);
        }

        card.appendChild(rightColumn);

        if (config.hover === 'placeholder') {
            this.addPlaceholderHover(card, leftColumn, entityType);
        }

        if (isWorker && !isRemote) {
            extension.updateWorkerControls(data.id);
        }

        return card;
    }
}
