import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { addNewWorker, isRemoteWorker } from "../workerSettings.js";


describe("workerSettings remote classification", () => {
    let originalWindow;

    beforeEach(() => {
        originalWindow = globalThis.window;
        globalThis.window = {
            location: {
                hostname: "127.0.0.1",
            },
        };
    });

    afterEach(() => {
        globalThis.window = originalWindow;
    });

    it("treats explicit local worker type as local even with non-local host", () => {
        const worker = { type: "local", host: "192.168.1.50" };
        expect(isRemoteWorker({}, worker)).toBe(false);
    });

    it("treats explicit remote worker type as remote", () => {
        const worker = { type: "remote", host: "127.0.0.1" };
        expect(isRemoteWorker({}, worker)).toBe(true);
    });

    it("treats cloud worker type as remote", () => {
        const worker = { type: "cloud", host: "worker.example.com" };
        expect(isRemoteWorker({}, worker)).toBe(true);
    });

    it("falls back to host heuristic for legacy workers", () => {
        expect(isRemoteWorker({}, { host: "127.0.0.1" })).toBe(false);
        expect(isRemoteWorker({}, { host: "worker.example.com" })).toBe(true);
    });
});

describe("addNewWorker GPU availability guard", () => {
    it("falls back to a disabled remote worker when no local CUDA device is available", async () => {
        const toastAdd = vi.fn();
        const updateWorker = vi.fn().mockResolvedValue({});
        const stateUpdateWorker = vi.fn();
        const extension = {
            cudaDeviceCount: 1,
            masterCudaDevice: 0,
            panelElement: null,
            config: {
                workers: [],
                master: { cuda_device: 0 },
            },
            api: { updateWorker },
            state: { updateWorker: stateUpdateWorker, setWorkerExpanded: vi.fn() },
            app: { extensionManager: { toast: { add: toastAdd } } },
        };

        await addNewWorker(extension);

        expect(updateWorker).toHaveBeenCalledTimes(1);
        expect(updateWorker.mock.calls[0][1]).toEqual(
            expect.objectContaining({
                type: "remote",
                enabled: false,
                cuda_device: null,
                host: "",
            })
        );
        expect(extension.config.workers).toHaveLength(1);
        expect(extension.config.workers[0]).toEqual(
            expect.objectContaining({
                type: "remote",
                enabled: false,
                cuda_device: null,
                host: "",
            })
        );
        expect(stateUpdateWorker).toHaveBeenCalledWith(
            expect.any(String),
            expect.objectContaining({ enabled: false })
        );
        expect(toastAdd).toHaveBeenCalledWith(
            expect.objectContaining({
                severity: "warn",
                summary: "Remote Worker Added",
            })
        );
    });

    it("assigns the first free local CUDA device when adding a worker", async () => {
        const toastAdd = vi.fn();
        const updateWorker = vi.fn().mockResolvedValue({});
        const stateUpdateWorker = vi.fn();
        const setWorkerExpanded = vi.fn();
        const extension = {
            cudaDeviceCount: 3,
            masterCudaDevice: 0,
            panelElement: null,
            config: {
                workers: [
                    { id: "w-existing", type: "local", port: 8189, cuda_device: 1, enabled: true },
                ],
                master: { cuda_device: 0 },
            },
            api: { updateWorker },
            state: { updateWorker: stateUpdateWorker, setWorkerExpanded },
            app: { extensionManager: { toast: { add: toastAdd } } },
        };

        await addNewWorker(extension);

        expect(updateWorker).toHaveBeenCalledTimes(1);
        expect(updateWorker.mock.calls[0][1]).toEqual(
            expect.objectContaining({
                cuda_device: 2,
            })
        );
    });
});
