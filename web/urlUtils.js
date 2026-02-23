export function normalizeWorkerUrl(rawUrl) {
    if (!rawUrl || typeof rawUrl !== "string") {
        return "";
    }

    const trimmed = rawUrl.trim();
    if (!trimmed) {
        return "";
    }

    const withProtocol = /^https?:\/\//i.test(trimmed) ? trimmed : `http://${trimmed}`;

    try {
        const parsed = new URL(withProtocol);
        if (parsed.pathname === "/") {
            parsed.pathname = "";
        }
        return parsed.toString().replace(/\/$/, "");
    } catch (error) {
        return withProtocol.replace(/\/$/, "");
    }
}

export function parseHostInput(value) {
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

export function buildWorkerUrl(worker, endpoint = "", windowLocation = window.location) {
    const parsed = parseHostInput(worker?.host || windowLocation.hostname);
    const host = parsed.host || windowLocation.hostname;
    const resolvedPort = parsed.port || worker?.port || 8188;

    const isCloud = worker?.type === "cloud";
    const isRunpodProxy = host.endsWith(".proxy.runpod.net");

    let finalHost = host;
    if (!worker?.host && isRunpodProxy) {
        const match = host.match(/^(.*)\.proxy\.runpod\.net$/);
        if (match) {
            finalHost = `${match[1]}-${resolvedPort}.proxy.runpod.net`;
        } else {
            console.error(`[Distributed] Failed to parse Runpod proxy host: ${host}`);
        }
    }

    const useHttps = isCloud || isRunpodProxy || resolvedPort === 443;
    const protocol = useHttps ? "https" : "http";
    const defaultPort = useHttps ? 443 : 80;
    const needsPort = !isRunpodProxy && resolvedPort !== defaultPort;
    const portPart = needsPort ? `:${resolvedPort}` : "";

    return normalizeWorkerUrl(`${protocol}://${finalHost}${portPart}${endpoint}`);
}

export function buildWorkerWebSocketUrl(workerUrl) {
    const normalized = normalizeWorkerUrl(workerUrl);
    const wsBase = normalized.replace(/^http:\/\//i, "ws://").replace(/^https:\/\//i, "wss://");
    return `${wsBase}/distributed/worker_ws`;
}

export function getMasterUrl(config, windowLocation = window.location, log = null) {
    const masterHost = config?.master?.host;
    if (masterHost) {
        const configuredHost = masterHost;

        // If the configured host already includes protocol, use as-is.
        if (configuredHost.startsWith("http://") || configuredHost.startsWith("https://")) {
            return configuredHost;
        }

        // For domain names (not IPs), default to HTTPS.
        const isIP = /^(\d{1,3}\.){3}\d{1,3}$/.test(configuredHost);
        const isLocalhost = configuredHost === "localhost" || configuredHost === "127.0.0.1";

        if (!isIP && !isLocalhost && configuredHost.includes(".")) {
            return `https://${configuredHost}`;
        }

        const protocol = windowLocation.protocol || "http:";
        const port = windowLocation.port || (protocol === "https:" ? "443" : "80");
        if ((protocol === "https:" && port === "443") || (protocol === "http:" && port === "80")) {
            return `${protocol}//${configuredHost}`;
        }
        return `${protocol}//${configuredHost}:${port}`;
    }

    const hostname = windowLocation.hostname;
    if (hostname !== "localhost" && hostname !== "127.0.0.1") {
        return windowLocation.origin;
    }

    if (typeof log === "function") {
        log(
            "No master host configured - remote workers won't be able to connect. Master host should be auto-detected on startup.",
            "debug",
        );
    }
    return windowLocation.origin;
}
