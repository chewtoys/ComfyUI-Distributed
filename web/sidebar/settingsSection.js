import { createCheckboxSetting, createNumberSetting } from "../ui/buttonHelpers.js";

export function renderSettingsSection(extension) {
    const settingsSection = document.createElement("div");
    settingsSection.style.cssText = "border-top: 1px solid #444; margin-bottom: 10px;";

    const settingsToggleArea = document.createElement("div");
    settingsToggleArea.style.cssText = "padding: 16.5px 0; cursor: pointer; user-select: none;";

    const settingsHeader = document.createElement("div");
    settingsHeader.style.cssText = "display: flex; align-items: center; justify-content: space-between;";

    const workerSettingsTitle = document.createElement("h4");
    workerSettingsTitle.textContent = "Settings";
    workerSettingsTitle.style.cssText = "margin: 0; font-size: 14px;";

    const workerSettingsToggle = document.createElement("span");
    workerSettingsToggle.textContent = "▶";
    workerSettingsToggle.style.cssText = "font-size: 12px; color: #888; transition: all 0.2s ease;";

    settingsHeader.appendChild(workerSettingsTitle);
    settingsHeader.appendChild(workerSettingsToggle);
    settingsToggleArea.appendChild(settingsHeader);

    settingsToggleArea.onmouseover = () => {
        workerSettingsToggle.style.color = "#fff";
    };
    settingsToggleArea.onmouseout = () => {
        workerSettingsToggle.style.color = "#888";
    };

    const settingsSeparator = document.createElement("div");
    settingsSeparator.style.cssText = "border-bottom: 1px solid #444; margin: 0;";

    const settingsContent = document.createElement("div");
    settingsContent.style.cssText =
        "max-height: 0; overflow: hidden; opacity: 0; transition: max-height 0.3s ease, opacity 0.3s ease;";

    const settingsDiv = document.createElement("div");
    settingsDiv.style.cssText =
        "display: grid; grid-template-columns: 1fr auto; row-gap: 10px; column-gap: 10px; padding-top: 10px; align-items: center;";

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

    const generalLabel = document.createElement("div");
    generalLabel.textContent = "GENERAL";
    generalLabel.style.cssText =
        "grid-column: 1 / span 2; font-size: 11px; color: #888; letter-spacing: 0.06em; padding-top: 2px;";

    const timeoutsLabel = document.createElement("div");
    timeoutsLabel.textContent = "TIMEOUTS";
    timeoutsLabel.style.cssText =
        "grid-column: 1 / span 2; font-size: 11px; color: #888; letter-spacing: 0.06em; padding-top: 4px;";

    settingsDiv.appendChild(generalLabel);
    settingsDiv.appendChild(
        createCheckboxSetting(
            "setting-debug",
            "Debug Mode",
            "Enable verbose logging in the browser console.",
            extension.config?.settings?.debug || false,
            (event) => extension._updateSetting("debug", event.target.checked)
        )
    );
    settingsDiv.appendChild(
        createCheckboxSetting(
            "setting-auto-launch",
            "Auto-launch Local Workers on Startup",
            "Start local worker processes automatically when the master starts.",
            extension.config?.settings?.auto_launch_workers || false,
            (event) => extension._updateSetting("auto_launch_workers", event.target.checked)
        )
    );
    settingsDiv.appendChild(
        createCheckboxSetting(
            "setting-stop-on-exit",
            "Stop Local Workers on Master Exit",
            "Stop local worker processes automatically when the master exits.",
            extension.config?.settings?.stop_workers_on_master_exit !== false,
            (event) => extension._updateSetting("stop_workers_on_master_exit", event.target.checked)
        )
    );
    settingsDiv.appendChild(timeoutsLabel);
    settingsDiv.appendChild(
        createNumberSetting(
            "setting-worker-timeout",
            "Worker Timeout",
            "Seconds without a heartbeat before a worker is considered timed out. Default 60.",
            extension.config?.settings?.worker_timeout_seconds ?? 60,
            10,
            1,
            (event) => {
                const value = parseInt(event.target.value, 10);
                if (!Number.isFinite(value) || value <= 0) {
                    return;
                }
                extension._updateSetting("worker_timeout_seconds", value);
            }
        )
    );

    settingsContent.appendChild(settingsDiv);
    settingsSection.appendChild(settingsToggleArea);
    settingsSection.appendChild(settingsSeparator);
    settingsSection.appendChild(settingsContent);
    return settingsSection;
}
