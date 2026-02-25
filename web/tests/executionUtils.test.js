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

    it("markSkipDispatch on empty prompt does not throw", () => {
        expect(() => markSkipDispatch({})).not.toThrow();
    });

    it("markSkipDispatch initialises missing inputs object before setting flag", () => {
        const prompt = { "1": { class_type: NODE_CLASSES.DISTRIBUTED_QUEUE } };
        markSkipDispatch(prompt);
        expect(prompt["1"].inputs.skip_dispatch).toBe(true);
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

    it("selectLeastBusyWorker with a single idle worker always returns it", () => {
        const extension = {};
        const statuses = [{ worker: { id: "w1" }, queueRemaining: 0 }];
        expect(selectLeastBusyWorker(extension, statuses)).toBe(statuses[0]);
        expect(selectLeastBusyWorker(extension, statuses)).toBe(statuses[0]);
    });

    it("selectLeastBusyWorker round-robin wraps around correctly", () => {
        const extension = {};
        const statuses = [
            { worker: { id: "w1" }, queueRemaining: 0 },
            { worker: { id: "w2" }, queueRemaining: 0 },
            { worker: { id: "w3" }, queueRemaining: 0 },
        ];
        const results = Array.from({ length: 4 }, () => selectLeastBusyWorker(extension, statuses));
        expect(results[0]).toBe(statuses[0]);
        expect(results[1]).toBe(statuses[1]);
        expect(results[2]).toBe(statuses[2]);
        expect(results[3]).toBe(statuses[0]); // wraps around
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

    it("selectLeastBusyWorker returns only busy worker when queue length is 1", () => {
        const extension = {};
        const statuses = [{ worker: { id: "w1" }, queueRemaining: 3 }];
        expect(selectLeastBusyWorker(extension, statuses)).toBe(statuses[0]);
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
