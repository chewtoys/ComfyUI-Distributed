import { BUTTON_STYLES } from './constants.js';
export async function renderSidebarContent(extension, el) {
    // Panel is being opened/rendered
    extension.log("Panel opened", "debug");
    if (!el) {
        extension.log("No element provided to renderSidebarContent", "debug");
        return;
    }
    // Prevent infinite recursion
    if (extension._isRendering) {
        extension.log("Already rendering, skipping", "debug");
        return;
    }
    extension._isRendering = true;
    function createCheckboxSetting(id, label, tooltip, checked, onChange) {
        const group = document.createElement("div");
        group.style.cssText = "grid-column: 1 / span 2; display: flex; align-items: center; gap: 8px;";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.id = id;
        checkbox.checked = checked;
        checkbox.onchange = onChange;
        const lbl = document.createElement("label");
        lbl.htmlFor = id;
        lbl.textContent = label;
        lbl.style.cssText = "font-size: 12px; color: #ccc; cursor: pointer;";
        if (tooltip) lbl.title = tooltip;
        group.appendChild(checkbox);
        group.appendChild(lbl);
        return group;
    }
    function createNumberSetting(id, label, tooltip, value, min, step, onChange) {
        const group = document.createElement("div");
        group.style.cssText = "grid-column: 1 / span 2; display: flex; align-items: center; gap: 6px;";
        const lbl = document.createElement("label");
        lbl.htmlFor = id;
        lbl.textContent = label;
        lbl.style.cssText = "font-size: 12px; color: #ccc;";
        if (tooltip) lbl.title = tooltip;
        const input = document.createElement("input");
        input.type = "number";
        input.id = id;
        input.min = String(min);
        input.step = String(step);
        input.style.cssText = "width: 80px; padding: 2px 6px; background: #222; color: #ddd; border: 1px solid #333; border-radius: 3px;";
        input.value = value;
        input.onchange = onChange;
        group.appendChild(lbl);
        group.appendChild(input);
        return group;
    }
    try {
        // Store reference to the panel element
        extension.panelElement = el;
        // Show loading indicator
        el.innerHTML = '';
        const loadingDiv = document.createElement("div");
        loadingDiv.style.cssText = "display: flex; align-items: center; justify-content: center; height: calc(100vh - 100px); color: #888;";
        loadingDiv.innerHTML = `<svg width="24" height="24" viewBox="0 0 24 24" style="color: #888;">
            <circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-dasharray="40 40"/>
        </svg>`;
        el.appendChild(loadingDiv);
        // Add rotation animation
        const style = document.createElement('style');
        style.textContent = `
            @keyframes rotate {
                from { transform: rotate(0deg); }
                to { transform: rotate(360deg); }
            }
        `;
        document.head.appendChild(style);
        loadingDiv.querySelector('svg').style.animation = 'rotate 1s linear infinite';
        // Preload data outside render
        await extension.loadConfig();
        await extension.loadManagedWorkers();
        await extension.refreshTunnelStatus();
        extension.tunnelElements = {};
        el.innerHTML = '';
        // Create toolbar header to match ComfyUI style
        const toolbar = document.createElement("div");
        toolbar.className = "p-toolbar p-component border-x-0 border-t-0 rounded-none px-2 py-1 min-h-8";
        toolbar.style.cssText = "border-bottom: 1px solid #444; background: transparent; display: flex; align-items: center;";
        const toolbarStart = document.createElement("div");
        toolbarStart.className = "p-toolbar-start";
        toolbarStart.style.cssText = "display: flex; align-items: center;";
        const titleSpan = document.createElement("span");
        titleSpan.className = "text-xs 2xl:text-sm truncate";
        titleSpan.textContent = "COMFYUI DISTRIBUTED";
        titleSpan.title = "ComfyUI Distributed";
        toolbarStart.appendChild(titleSpan);
        toolbar.appendChild(toolbarStart);
        const toolbarCenter = document.createElement("div");
        toolbarCenter.className = "p-toolbar-center";
        toolbar.appendChild(toolbarCenter);
        const toolbarEnd = document.createElement("div");
        toolbarEnd.className = "p-toolbar-end";
        toolbar.appendChild(toolbarEnd);
        el.appendChild(toolbar);
        // Main container with adjusted padding
        const container = document.createElement("div");
        container.style.cssText = "padding: 15px; display: flex; flex-direction: column; height: calc(100% - 32px);";
        // Detect master info on panel open (in case CUDA info wasn't available at startup)
        extension.log(`Panel opened. CUDA device count: ${extension.cudaDeviceCount}, Workers: ${extension.config?.workers?.length || 0}`, "debug");
        if (!extension.cudaDeviceCount) {
            await extension.detectMasterIP();
        }
        // Now render with guaranteed up-to-date config
        // Master Node Section
        const masterDiv = extension.ui.renderEntityCard('master', extension.config?.master, extension);
        container.appendChild(masterDiv);
        // Workers Section (no heading)
        const gpuSection = document.createElement("div");
        gpuSection.style.cssText = "flex: 1; overflow-y: auto; margin-bottom: 15px;";
        const gpuList = document.createElement("div");
        const workers = extension.config?.workers || [];
        // If no workers exist, show a full blueprint placeholder first
        if (workers.length === 0) {
            const blueprintDiv = extension.ui.renderEntityCard('blueprint', { onClick: () => extension.addNewWorker() }, extension);
            gpuList.appendChild(blueprintDiv);
        }
        // Show existing workers
        workers.forEach(worker => {
            const gpuDiv = extension.ui.renderEntityCard('worker', worker, extension);
            gpuList.appendChild(gpuDiv);
        });
        gpuSection.appendChild(gpuList);
        // Only show the minimal "Add Worker" box if there are existing workers
        if (workers.length > 0) {
            const addWorkerDiv = extension.ui.renderEntityCard('add', { onClick: () => extension.addNewWorker() }, extension);
            gpuSection.appendChild(addWorkerDiv);
        }
        container.appendChild(gpuSection);
        const actionsSection = document.createElement("div");
        actionsSection.style.cssText = "padding-top: 10px; margin-bottom: 15px; border-top: 1px solid #444;";
        // Create a row for both buttons
        const buttonRow = document.createElement("div");
        buttonRow.style.cssText = "display: flex; gap: 8px;";
        const clearMemButton = extension.ui.createButtonHelper(
            "Clear Worker VRAM",
            (e) => extension._handleClearMemory(e.target),
            BUTTON_STYLES.clearMemory
        );
        clearMemButton.title = "Clear VRAM on all enabled worker GPUs (not master)";
        clearMemButton.style.cssText = BUTTON_STYLES.base + " flex: 1;" + BUTTON_STYLES.clearMemory;
        const interruptButton = extension.ui.createButtonHelper(
            "Interrupt Workers",
            (e) => extension._handleInterruptWorkers(e.target),
            BUTTON_STYLES.interrupt
        );
        interruptButton.title = "Cancel/interrupt execution on all enabled worker GPUs";
        interruptButton.style.cssText = BUTTON_STYLES.base + " flex: 1;" + BUTTON_STYLES.interrupt;
        buttonRow.appendChild(clearMemButton);
        buttonRow.appendChild(interruptButton);
        actionsSection.appendChild(buttonRow);
        container.appendChild(actionsSection);
        // Settings section
        const settingsSection = document.createElement("div");
        // Top separator only; spacing handled by the clickable toggle area for equal top/bottom spacing
        settingsSection.style.cssText = "border-top: 1px solid #444; margin-bottom: 10px;";
        // Settings header with toggle (full-area clickable between separators)
        const settingsToggleArea = document.createElement("div");
        // Equal spacing above header (to top separator) and below header (to bottom separator)
        settingsToggleArea.style.cssText = "padding: 16.5px 0; cursor: pointer; user-select: none;";
        const settingsHeader = document.createElement("div");
        settingsHeader.style.cssText = "display: flex; align-items: center; justify-content: space-between;";
        const workerSettingsTitle = document.createElement("h4");
        workerSettingsTitle.textContent = "Settings";
        workerSettingsTitle.style.cssText = "margin: 0; font-size: 14px;";
        const workerSettingsToggle = document.createElement("span");
        workerSettingsToggle.textContent = "▶"; // Right arrow when collapsed
        workerSettingsToggle.style.cssText = "font-size: 12px; color: #888; transition: all 0.2s ease;";
        settingsHeader.appendChild(workerSettingsTitle);
        settingsHeader.appendChild(workerSettingsToggle);
        settingsToggleArea.appendChild(settingsHeader);
        // Hover effect for toggle area
        settingsToggleArea.onmouseover = () => { workerSettingsToggle.style.color = "#fff"; };
        settingsToggleArea.onmouseout = () => { workerSettingsToggle.style.color = "#888"; };
        // A small separator shown only when collapsed (to make the section boundary obvious)
        const settingsSeparator = document.createElement("div");
        // No margin so the bottom spacing is controlled by settingsToggleArea padding-bottom
        settingsSeparator.style.cssText = "border-bottom: 1px solid #444; margin: 0;";
        // Collapsible settings content
        const settingsContent = document.createElement("div");
        settingsContent.style.cssText = "max-height: 0; overflow: hidden; opacity: 0; transition: max-height 0.3s ease, opacity 0.3s ease;";
        const settingsDiv = document.createElement("div");
        settingsDiv.style.cssText = "display: grid; grid-template-columns: 1fr auto; row-gap: 10px; column-gap: 10px; padding-top: 10px; align-items: center;";
        // Toggle functionality
        let settingsExpanded = false;
        settingsToggleArea.onclick = () => {
            settingsExpanded = !settingsExpanded;
            if (settingsExpanded) {
                settingsContent.style.maxHeight = "200px";
                settingsContent.style.opacity = "1";
                workerSettingsToggle.style.transform = "rotate(90deg)";
                settingsSeparator.style.display = "none";
            } else {
                settingsContent.style.maxHeight = "0";
                settingsContent.style.opacity = "0";
                workerSettingsToggle.style.transform = "rotate(0deg)";
                settingsSeparator.style.display = "block";
            }
        };
        // Section: General
        const generalLabel = document.createElement("div");
        generalLabel.textContent = "GENERAL";
        generalLabel.style.cssText = "grid-column: 1 / span 2; font-size: 11px; color: #888; letter-spacing: 0.06em; padding-top: 2px;";
        // Section: Timeouts
        const timeoutsLabel = document.createElement("div");
        timeoutsLabel.textContent = "TIMEOUTS";
        timeoutsLabel.style.cssText = "grid-column: 1 / span 2; font-size: 11px; color: #888; letter-spacing: 0.06em; padding-top: 4px;";
        settingsDiv.appendChild(generalLabel);
        settingsDiv.appendChild(createCheckboxSetting(
            "setting-debug", "Debug Mode",
            "Enable verbose logging in the browser console.",
            extension.config?.settings?.debug || false,
            (e) => extension._updateSetting('debug', e.target.checked)
        ));
        settingsDiv.appendChild(createCheckboxSetting(
            "setting-auto-launch", "Auto-launch Local Workers on Startup",
            "Start local worker processes automatically when the master starts.",
            extension.config?.settings?.auto_launch_workers || false,
            (e) => extension._updateSetting('auto_launch_workers', e.target.checked)
        ));
        settingsDiv.appendChild(createCheckboxSetting(
            "setting-stop-on-exit", "Stop Local Workers on Master Exit",
            "Stop local worker processes automatically when the master exits.",
            extension.config?.settings?.stop_workers_on_master_exit !== false,
            (e) => extension._updateSetting('stop_workers_on_master_exit', e.target.checked)
        ));
        settingsDiv.appendChild(timeoutsLabel);
        settingsDiv.appendChild(createNumberSetting(
            "setting-worker-timeout", "Worker Timeout",
            "Seconds without a heartbeat before a worker is considered timed out. Default 60.",
            extension.config?.settings?.worker_timeout_seconds ?? 60,
            10, 1,
            (e) => {
                const v = parseInt(e.target.value, 10);
                if (!Number.isFinite(v) || v <= 0) return;
                extension._updateSetting('worker_timeout_seconds', v);
            }
        ));
        settingsContent.appendChild(settingsDiv);
        settingsSection.appendChild(settingsToggleArea);
        settingsSection.appendChild(settingsSeparator);
        settingsSection.appendChild(settingsContent);
        container.appendChild(settingsSection);
        el.appendChild(container);
        // Start checking worker statuses immediately in parallel
        setTimeout(() => extension.checkAllWorkerStatuses(), 0);
    } finally {
        // Always reset the rendering flag
        extension._isRendering = false;
    }
}
