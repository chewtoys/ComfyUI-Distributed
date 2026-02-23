import { describe, expect, it } from "vitest";

import { NODE_CLASSES } from "../constants.js";
import { markSkipDispatch, selectLeastBusyWorker } from "../executionDecisionUtils.js";
import { buildWorkerWebSocketUrl } from "../urlUtils.js";


describe("execution decision helpers", () => {
    it("markSkipDispatch sets skip_dispatch=true on DistributedQueue nodes only", () => {
        const prompt = {
            "1": {
                class_type: NODE_CLASSES.DISTRIBUTED_QUEUE,
                inputs: { seed: 1 },
            },
            "2": {
                class_type: "KSampler",
                inputs: { seed: 2 },
            },
            "3": {
                class_type: NODE_CLASSES.DISTRIBUTED_QUEUE,
                inputs: {},
            },
        };

        markSkipDispatch(prompt);

        expect(prompt["1"].inputs.skip_dispatch).toBe(true);
        expect(prompt["3"].inputs.skip_dispatch).toBe(true);
        expect(prompt["2"].inputs.skip_dispatch).toBeUndefined();
    });

    it("selectLeastBusyWorker round-robins among idle workers", () => {
        const extension = {};
        const statuses = [
            { worker: { id: "w1" }, queueRemaining: 0 },
            { worker: { id: "w2" }, queueRemaining: 0 },
        ];

        expect(selectLeastBusyWorker(extension, statuses)).toBe(statuses[0]);
        expect(selectLeastBusyWorker(extension, statuses)).toBe(statuses[1]);
        expect(selectLeastBusyWorker(extension, statuses)).toBe(statuses[0]);
    });

    it("selectLeastBusyWorker chooses shortest queue when no idle workers", () => {
        const extension = {};
        const statuses = [
            { worker: { id: "w1" }, queueRemaining: 5 },
            { worker: { id: "w2" }, queueRemaining: 2 },
            { worker: { id: "w3" }, queueRemaining: 7 },
        ];

        expect(selectLeastBusyWorker(extension, statuses)).toBe(statuses[1]);
    });

    it("buildWorkerWebSocketUrl converts http/https to ws/wss", () => {
        expect(buildWorkerWebSocketUrl("http://worker.local:8188")).toBe(
            "ws://worker.local:8188/distributed/worker_ws"
        );
        expect(buildWorkerWebSocketUrl("https://worker.example.com")).toBe(
            "wss://worker.example.com/distributed/worker_ws"
        );
    });
});
