import { app } from "/scripts/app.js";
import { ENDPOINTS } from "./constants.js";

const NODE_CLASS = "DistributedValue";
const WORKER_WIDGET_PREFIX = "_dv_worker_";

// ---------------------------------------------------------------------------
// Config helpers
// ---------------------------------------------------------------------------

async function fetchWorkers() {
    try {
        const resp = await fetch(ENDPOINTS.CONFIG);
        if (!resp.ok) return [];
        const config = await resp.json();
        return config.workers || [];
    } catch {
        return [];
    }
}

// ---------------------------------------------------------------------------
// Hidden widget accessors (worker_values JSON storage)
// ---------------------------------------------------------------------------

function getWorkerValuesWidget(node) {
    return node.widgets?.find((w) => w.name === "worker_values");
}

function getHiddenWidget(node) {
    return node._dvHiddenWidget || getWorkerValuesWidget(node);
}

function getWorkerWidgets(node) {
    return (node.widgets || []).filter((w) => w.name.startsWith(WORKER_WIDGET_PREFIX));
}

function readStore(node) {
    const w = getHiddenWidget(node);
    if (!w) return {};
    try {
        const parsed = JSON.parse(w.value || "{}");
        return typeof parsed === "object" && parsed !== null ? parsed : {};
    } catch {
        return {};
    }
}

function writeStore(node, patch) {
    const w = getHiddenWidget(node);
    if (!w) return;
    const store = readStore(node);
    Object.assign(store, patch);
    w.value = JSON.stringify(store);
}

function serializeWorkerValues(node) {
    const store = readStore(node);
    // Keep metadata keys (_type, _options) but overwrite worker values
    const newStore = { _type: store._type || "STRING" };
    if (store._options) newStore._options = store._options;
    for (const w of getWorkerWidgets(node)) {
        const key = w.name.slice(WORKER_WIDGET_PREFIX.length);
        const val = w.value;
        if (val !== undefined && val !== null && val !== "") {
            newStore[key] = String(val);
        }
    }
    const hidden = getHiddenWidget(node);
    if (hidden) hidden.value = JSON.stringify(newStore);
}

// ---------------------------------------------------------------------------
// Hide the raw worker_values widget
// ---------------------------------------------------------------------------

function hideRawWidget(node) {
    const w = getWorkerValuesWidget(node);
    if (!w) return;

    // Hide any DOM elements (multiline creates a textarea)
    if (w.element) w.element.style.display = "none";
    if (w.inputEl) w.inputEl.style.display = "none";

    // Remove from visible widgets array
    const idx = node.widgets.indexOf(w);
    if (idx !== -1) node.widgets.splice(idx, 1);

    // Keep reference
    node._dvHiddenWidget = w;
}

// ---------------------------------------------------------------------------
// Connection type detection
// ---------------------------------------------------------------------------

function detectTargetType(node) {
    // Find what our output (slot 0) is connected to
    if (!node.outputs || !node.outputs[0] || !node.outputs[0].links) {
        return { type: "STRING", options: null };
    }
    const linkIds = node.outputs[0].links;
    if (!linkIds || linkIds.length === 0) {
        return { type: "STRING", options: null };
    }

    const graph = node.graph || app.graph;
    if (!graph) return { type: "STRING", options: null };

    // Use the first connection to determine type
    const linkId = linkIds[0];
    const link = graph.links?.[linkId] || graph._links?.get?.(linkId);
    if (!link) return { type: "STRING", options: null };

    const targetNode = graph.getNodeById(link.target_id);
    if (!targetNode) return { type: "STRING", options: null };

    const targetInputName = targetNode.inputs?.[link.target_slot]?.name;
    if (!targetInputName) return { type: "STRING", options: null };

    // Find the corresponding widget on the target node to get type info
    const targetWidget = targetNode.widgets?.find((w) => w.name === targetInputName);
    if (!targetWidget) {
        // Check node definition for input type
        const nodeDef = targetNode.constructor?.nodeData;
        if (nodeDef) {
            const inputDef =
                nodeDef.input?.required?.[targetInputName] ||
                nodeDef.input?.optional?.[targetInputName];
            if (inputDef) {
                if (Array.isArray(inputDef[0])) {
                    return { type: "COMBO", options: inputDef[0] };
                }
                const typeName = inputDef[0];
                if (typeName === "INT") return { type: "INT", options: null };
                if (typeName === "FLOAT") return { type: "FLOAT", options: null };
            }
        }
        return { type: "STRING", options: null };
    }

    // Detect type from widget
    if (targetWidget.type === "combo") {
        const options = targetWidget.options?.values || [];
        return { type: "COMBO", options };
    }
    if (targetWidget.type === "number") {
        // Distinguish INT vs FLOAT from the widget options
        const step = targetWidget.options?.step;
        if (step !== undefined && step % 1 === 0 && (!targetWidget.options?.precision || targetWidget.options.precision === 0)) {
            return { type: "INT", options: null };
        }
        return { type: "FLOAT", options: null };
    }

    return { type: "STRING", options: null };
}

// ---------------------------------------------------------------------------
// Worker widget creation
// ---------------------------------------------------------------------------

function removeWorkerWidgets(node) {
    for (let i = node.widgets.length - 1; i >= 0; i--) {
        if (node.widgets[i].name.startsWith(WORKER_WIDGET_PREFIX)) {
            node.widgets.splice(i, 1);
        }
    }
}

function createWorkerWidgets(node, workers, inputType, comboOptions) {
    removeWorkerWidgets(node);

    const store = readStore(node);

    for (let i = 0; i < workers.length; i++) {
        const worker = workers[i];
        const key = String(i + 1);
        const widgetName = `${WORKER_WIDGET_PREFIX}${key}`;
        const label = worker.name || worker.id || `Worker ${key}`;
        const savedValue = store[key] || "";

        let widget;
        if (inputType === "COMBO" && Array.isArray(comboOptions) && comboOptions.length > 0) {
            widget = node.addWidget(
                "combo",
                widgetName,
                savedValue || comboOptions[0],
                (value) => {
                    widget.value = value;
                    serializeWorkerValues(node);
                },
                { values: comboOptions }
            );
        } else if (inputType === "INT") {
            widget = node.addWidget(
                "number",
                widgetName,
                savedValue ? Number(savedValue) : 0,
                (value) => {
                    widget.value = value;
                    serializeWorkerValues(node);
                },
                { min: -Infinity, max: Infinity, step: 1, precision: 0 }
            );
        } else if (inputType === "FLOAT") {
            widget = node.addWidget(
                "number",
                widgetName,
                savedValue ? Number(savedValue) : 0.0,
                (value) => {
                    widget.value = value;
                    serializeWorkerValues(node);
                },
                { min: -Infinity, max: Infinity, step: 0.1, precision: 3 }
            );
        } else {
            // STRING (default)
            widget = node.addWidget(
                "string",
                widgetName,
                savedValue,
                (value) => {
                    widget.value = value;
                    serializeWorkerValues(node);
                },
                {}
            );
        }
        widget.label = label;
    }

    // Persist type metadata
    const patch = { _type: inputType };
    if (comboOptions) patch._options = comboOptions;
    writeStore(node, patch);

    // Re-serialize worker values after creating widgets
    serializeWorkerValues(node);

    // Resize node to fit
    const size = node.computeSize();
    size[0] = Math.max(size[0], node.size?.[0] || 0);
    node.setSize(size);
    if (node.setDirtyCanvas) node.setDirtyCanvas(true, true);
}

// ---------------------------------------------------------------------------
// Rebuild widgets based on current connection
// ---------------------------------------------------------------------------

function rebuildWidgets(node, workers) {
    const { type, options } = detectTargetType(node);
    createWorkerWidgets(node, workers, type, options);
}

// ---------------------------------------------------------------------------
// Extension registration
// ---------------------------------------------------------------------------

app.registerExtension({
    name: "Distributed.DistributedValue",
    async nodeCreated(node) {
        if (node.comfyClass !== NODE_CLASS) return;

        try {
            const workers = await fetchWorkers();
            node._dvWorkers = workers;

            // Hide the raw worker_values widget
            hideRawWidget(node);

            // Read stored type or detect from connection
            const store = readStore(node);
            const storedType = store._type || "STRING";
            const storedOptions = store._options || null;

            if (workers.length > 0) {
                createWorkerWidgets(node, workers, storedType, storedOptions);
            }

            // Listen for connection changes to adapt widget type
            const originalOnConnectionsChange = node.onConnectionsChange;
            node.onConnectionsChange = function (side, slotIndex, connected, linkInfo, ioSlot) {
                if (originalOnConnectionsChange) {
                    originalOnConnectionsChange.call(this, side, slotIndex, connected, linkInfo, ioSlot);
                }
                // side 2 = output; slot 0 = our "value" output
                if (slotIndex === 0 && this._dvWorkers?.length > 0) {
                    // Small delay to let litegraph finish link setup
                    setTimeout(() => rebuildWidgets(this, this._dvWorkers), 50);
                }
            };

            // Override configure for workflow load
            const originalConfigure = node.configure;
            node.configure = function (data) {
                const result = originalConfigure
                    ? originalConfigure.call(this, data)
                    : undefined;

                setTimeout(() => {
                    hideRawWidget(this);
                    if (this._dvWorkers?.length > 0) {
                        const s = readStore(this);
                        createWorkerWidgets(this, this._dvWorkers, s._type || "STRING", s._options || null);
                    }
                }, 100);

                return result;
            };
        } catch (error) {
            console.error("Error in DistributedValue extension:", error);
        }
    },
});
