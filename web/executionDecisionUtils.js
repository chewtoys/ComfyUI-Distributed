import { NODE_CLASSES } from "./constants.js";


export function selectLeastBusyWorker(extension, statuses) {
    // Find idle workers (queue_remaining == 0)
    const idleWorkers = statuses.filter((s) => s.queueRemaining === 0);

    if (idleWorkers.length > 0) {
        // Round-robin among idle workers
        if (!extension._distributedQueueRRIndex) {
            extension._distributedQueueRRIndex = 0;
        }
        const index = extension._distributedQueueRRIndex % idleWorkers.length;
        extension._distributedQueueRRIndex++;
        return idleWorkers[index];
    }

    // No idle workers - pick the one with the shortest queue
    return statuses.reduce((min, s) => (s.queueRemaining < min.queueRemaining ? s : min));
}


export function markSkipDispatch(promptObj) {
    for (const node of Object.values(promptObj)) {
        if (node && typeof node === "object" && node.class_type === NODE_CLASSES.DISTRIBUTED_QUEUE) {
            node.inputs = node.inputs || {};
            node.inputs.skip_dispatch = true;
        }
    }
}
