export function updateTunnelUIElements(extension, isRunning, isStarting) {
    void isRunning;
    void isStarting;

    const elements = extension.tunnelElements || {};
    const status = (extension.tunnelStatus?.status || "stopped").toLowerCase();
    const tunnelButtonColorClasses = ["tunnel-button--enable", "tunnel-button--disable"];
    const tunnelStatusColorClasses = ["tunnel-status--enable", "tunnel-status--disable"];

    if (elements.button) {
        elements.button.disabled = status === "starting" || status === "stopping";
        elements.button.classList.remove(...tunnelButtonColorClasses);

        if (status === "starting") {
            elements.button.innerHTML = `<span class="tunnel-spinner"></span> Starting...`;
            elements.button.classList.add("tunnel-button--enable");
        } else if (status === "stopping") {
            elements.button.innerHTML = `<span class="tunnel-spinner"></span> Stopping...`;
            elements.button.classList.add("tunnel-button--disable");
        } else if (status === "running") {
            elements.button.textContent = "Disable Cloudflare Tunnel";
            elements.button.classList.add("tunnel-button--disable");
        } else if (status === "error") {
            elements.button.textContent = "Retry Cloudflare Tunnel";
            elements.button.classList.add("tunnel-button--disable");
        } else {
            elements.button.textContent = "Enable Cloudflare Tunnel";
            elements.button.classList.add("tunnel-button--enable");
        }
    }

    if (elements.status) {
        elements.status.textContent = status.toUpperCase();
        elements.status.classList.remove(...tunnelStatusColorClasses);
        if (status === "running" || status === "error" || status === "stopping") {
            elements.status.classList.add("tunnel-status--disable");
        } else {
            elements.status.classList.add("tunnel-status--enable");
        }
    }

    if (elements.url) {
        const url = extension.tunnelStatus?.public_url;
        if (url) {
            elements.url.innerHTML = `<a href="${url}" target="_blank" style="color: #eee; text-decoration: none;">${url}</a>`;
        } else {
            elements.url.textContent = status === "starting" ? "Requesting public URL..." : "No tunnel active";
        }
    }

    if (elements.copyBtn) {
        const hasUrl = Boolean(extension.tunnelStatus?.public_url);
        elements.copyBtn.disabled = !hasUrl;
        elements.copyBtn.style.opacity = hasUrl ? "1" : "0.5";
    }
}

export async function refreshTunnelStatus(extension) {
    try {
        const data = await extension.api.getTunnelStatus();
        extension.tunnelStatus = data.tunnel || { status: "stopped" };
        if (data.master_host !== undefined) {
            extension._applyMasterHost(data.master_host);
        }
        return extension.tunnelStatus;
    } catch (error) {
        extension.tunnelStatus = { status: "error", last_error: error.message };
        extension.log("Failed to fetch tunnel status: " + error.message, "error");
        return extension.tunnelStatus;
    } finally {
        updateTunnelUIElements(extension);
    }
}

export async function handleTunnelToggle(extension, button) {
    const currentStatus = (extension.tunnelStatus?.status || "stopped").toLowerCase();
    if (currentStatus === "starting" || currentStatus === "stopping") {
        return;
    }

    const setStatus = (status) => {
        extension.tunnelStatus = { ...(extension.tunnelStatus || {}), status };
        updateTunnelUIElements(extension);
    };

    if (currentStatus === "running") {
        setStatus("stopping");
        try {
            if (button) {
                button.innerHTML = `<span class="tunnel-spinner"></span> Stopping...`;
                button.disabled = true;
            }
            const data = await extension.api.stopTunnel();
            extension.tunnelStatus = data.tunnel || { status: "stopped" };
            if (data.master_host !== undefined) {
                extension._applyMasterHost(data.master_host);
            }
            updateTunnelUIElements(extension);
            extension.ui.showToast(extension.app, "info", "Cloudflare Tunnel Disabled", "Master address restored", 4000);
        } catch (error) {
            extension.tunnelStatus = { status: "error", last_error: error.message };
            updateTunnelUIElements(extension);
            extension.ui.showToast(extension.app, "error", "Failed to stop tunnel", error.message, 5000);
        } finally {
            if (button) {
                button.disabled = false;
            }
        }
        return;
    }

    // Start tunnel
    setStatus("starting");
    if (button) {
        button.innerHTML = `<span class="tunnel-spinner"></span> Starting...`;
        button.disabled = true;
    }
    try {
        const data = await extension.api.startTunnel();
        extension.tunnelStatus = data.tunnel || { status: "running" };
        if (data.master_host !== undefined) {
            extension._applyMasterHost(data.master_host);
        }
        updateTunnelUIElements(extension);
        const url = data.tunnel?.public_url || data.master_host;
        extension.ui.showToast(extension.app, "success", "Cloudflare Tunnel Ready", url || "Public URL created", 4500);
    } catch (error) {
        extension.tunnelStatus = { status: "error", last_error: error.message };
        updateTunnelUIElements(extension);
        extension.ui.showToast(extension.app, "error", "Failed to start tunnel", error.message, 5000);
    } finally {
        if (button) {
            button.disabled = false;
        }
    }
}
