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
