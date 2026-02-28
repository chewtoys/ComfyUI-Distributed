import { describe, expect, it } from "vitest";

import { buildWorkerWebSocketUrl } from "../urlUtils.js";


describe("execution decision helpers", () => {
    it("buildWorkerWebSocketUrl converts http/https to ws/wss", () => {
        expect(buildWorkerWebSocketUrl("http://worker.local:8188")).toBe(
            "ws://worker.local:8188/distributed/worker_ws"
        );
        expect(buildWorkerWebSocketUrl("https://worker.example.com")).toBe(
            "wss://worker.example.com/distributed/worker_ws"
        );
    });
});
