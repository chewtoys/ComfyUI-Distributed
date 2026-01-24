import { api } from "../../scripts/api.js";
import { findNodesByClass, findImageReferences, hasUpstreamNode, pruneWorkflowForWorker, getCachedWorkerSystemInfo, findCollectorDownstreamNodes } from './workerUtils.js';
import { TIMEOUTS } from './constants.js';

/**
 * Convert paths in the API prompt to match the target platform's separator
 * @param {Object} apiPrompt - The workflow API prompt
 * @param {string} targetSeparator - The target path separator ('\\' or '/')
 * @returns {Object} The converted API prompt
 */
function convertPathsForPlatform(apiPrompt, targetSeparator) {
    // Validate target separator
    if (!targetSeparator || (targetSeparator !== '/' && targetSeparator !== '\\')) {
        console.warn('[Distributed] Invalid target separator:', targetSeparator, '- skipping path conversion');
        return apiPrompt;
    }
    
    // Regex to identify likely file paths with extensions
    const isLikelyFilename = (value) => {
        return value.match(/\.(ckpt|safetensors|pt|pth|bin|yaml|json|png|jpg|jpeg|webp|gif|bmp|latent|txt|vae|lora|embedding)(\s*\[\w+\])?$/i);
    };
    const isImageOrVideo = (value) => {
        return value.match(/\.(png|jpg|jpeg|webp|gif|bmp|mp4|avi|mov|mkv|webm)(\s*\[\w+\])?$/i);
    };
    
    function convert(obj) {
        if (typeof obj === 'string') {
            // Only convert strings that look like file paths
            if ((obj.includes('\\') || obj.includes('/')) && isLikelyFilename(obj)) {
                const trimmed = obj.trim();
                const hasDrive = /^[A-Za-z]:\\\\|^[A-Za-z]:\//.test(trimmed);
                const isAbsolute = trimmed.startsWith('/') || trimmed.startsWith('\\\\');
                const hasProtocol = /^\w+:\/\//.test(trimmed);

                // For annotated relative image/video paths, keep forward slashes
                if (!hasDrive && !isAbsolute && !hasProtocol && isImageOrVideo(trimmed)) {
                    return trimmed.replace(/[\\\\]/g, '/');
                }
                // Otherwise replace any path separator with the worker's target separator
                return trimmed.replace(/[\\\\\/]/g, targetSeparator);
            }
            return obj;
        } else if (Array.isArray(obj)) {
            return obj.map(convert);
        } else if (typeof obj === 'object' && obj !== null) {
            const newObj = {};
            for (const [key, value] of Object.entries(obj)) {
                newObj[key] = convert(value);
            }
            return newObj;
        }
        return obj;
    }
    
    return convert(apiPrompt);
}

export function setupInterceptor(extension) {
    api.queuePrompt = async (number, prompt) => {
        if (extension.isEnabled) {
            const hasCollector = findNodesByClass(prompt.output, "DistributedCollector").length > 0;
            const hasDistUpscale = findNodesByClass(prompt.output, "UltimateSDUpscaleDistributed").length > 0;
            const hasDistQueue = findNodesByClass(prompt.output, "DistributedQueue").length > 0;

            if (hasCollector || hasDistUpscale) {
                const result = await executeParallelDistributed(extension, prompt);
                // Immediate status check for instant feedback
                extension.checkAllWorkerStatuses();
                // Another check after a short delay to catch state changes
                setTimeout(() => extension.checkAllWorkerStatuses(), TIMEOUTS.POST_ACTION_DELAY);
                return result;
            }

            // DistributedQueue: route entire workflow to least-busy worker
            if (hasDistQueue) {
                const result = await executeQueueDistributed(extension, prompt);
                extension.checkAllWorkerStatuses();
                setTimeout(() => extension.checkAllWorkerStatuses(), TIMEOUTS.POST_ACTION_DELAY);
                return result;
            }
        }
        return extension.originalQueuePrompt(number, prompt);
    };
}

export async function executeParallelDistributed(extension, promptWrapper) {
    try {
        const executionPrefix = "exec_" + Date.now(); // Unique ID for this specific execution
        const enabledWorkers = extension.enabledWorkers;
        
        // Pre-flight health check on all enabled workers
        const activeWorkers = await performPreflightCheck(extension, enabledWorkers);
        
        // Case: Enabled workers but all offline
        if (activeWorkers.length === 0 && enabledWorkers.length > 0) {
            extension.log("No active workers found. All enabled workers are offline.");
            if (extension.ui?.showToast) {
                extension.ui.showToast(extension.app, "error", "All Workers Offline", 
                    `${enabledWorkers.length} worker(s) enabled but all are offline or unreachable. Check worker connections and try again.`, 5000);
            }
            // Fall back to master-only execution
            return extension.originalQueuePrompt(0, promptWrapper);
        }
        
        extension.log(`Pre-flight check: ${activeWorkers.length} of ${enabledWorkers.length} workers are active`, "debug");

        // Check if master host might be unreachable by workers (cloudflare tunnel down)
        const masterHost = extension.config?.master?.host || '';
        const isCloudflareHost = /\.(trycloudflare\.com|cloudflare\.dev)$/i.test(masterHost);

        if (isCloudflareHost && activeWorkers.length > 0) {
            // Try to verify if the cloudflare tunnel is actually up
            try {
                const testUrl = `${window.location.protocol}//${masterHost}/prompt`;
                const response = await fetch(testUrl, {
                    method: 'GET',
                    mode: 'cors',
                    cache: 'no-cache',
                    signal: AbortSignal.timeout(3000) // 3 second timeout
                });

                if (!response.ok) {
                    throw new Error('Master not reachable');
                }
            } catch (error) {
                // Cloudflare tunnel appears to be down
                extension.log(`Master host ${masterHost} is not reachable - cloudflare tunnel may be down`, "error");

                if (extension.ui?.showCloudflareWarning) {
                    extension.ui.showCloudflareWarning(extension, masterHost);
                }

                // Stop execution - workers won't be able to send results back
                extension.log("Blocking execution - workers cannot reach master at cloudflare domain", "error");
                return null; // This will prevent the workflow from running
            }
        }

        // Find all distributed nodes in the workflow
        const collectorNodes = findNodesByClass(promptWrapper.output, "DistributedCollector");
        const upscaleNodes = findNodesByClass(promptWrapper.output, "UltimateSDUpscaleDistributed");
        const allDistributedNodes = [...collectorNodes, ...upscaleNodes];

        const wantsDelegate = Boolean(extension.config?.settings?.master_delegate_only);
        let masterDelegateActive = false;
        if (wantsDelegate) {
            if (activeWorkers.length === 0) {
                extension.log("Master delegate-only mode disabled: no active workers detected. Falling back to master execution.", "debug");
            } else if (upscaleNodes.length > 0) {
                extension.log("Master delegate-only mode is not yet supported for UltimateSDUpscaleDistributed nodes. Falling back to normal execution.", "warn");
            } else if (collectorNodes.length === 0) {
                extension.log("Master delegate-only mode requested but no DistributedCollector nodes found. Falling back to normal execution.", "debug");
            } else {
                masterDelegateActive = true;
                extension.log("Master delegate-only mode enabled: master will orchestrate without running upstream nodes.", "debug");
            }
        }
        
        // Map original node IDs to truly unique job IDs for this specific run
        const job_id_map = new Map(allDistributedNodes.map(node => [node.id, `${executionPrefix}_${node.id}`]));
        
        // Prepare a separate job queue on the backend for each unique job ID
        const preparePromises = Array.from(job_id_map.values()).map(uniqueId => prepareDistributedJob(extension, uniqueId));
        await Promise.all(preparePromises);

        const jobs = [];
        // Use only active workers
        const participants = ['master', ...activeWorkers.map(w => w.id)];

        for (const participantId of participants) {
            const options = { 
                enabled_worker_ids: activeWorkers.map(w => w.id), 
                workflow: promptWrapper.workflow,
                job_id_map: job_id_map, // Pass the map of unique IDs
                delegate_master: masterDelegateActive && participantId === 'master'
            };
            
            const jobApiPrompt = await prepareApiPromptForParticipant(
                extension, promptWrapper.output, participantId, options
            );
            
            if (participantId === 'master') {
                jobs.push({ type: 'master', promptWrapper: { ...promptWrapper, output: jobApiPrompt } });
            } else {
                const worker = activeWorkers.find(w => w.id === participantId);
                if (worker) {
                    const job = {
                        type: 'worker',
                        worker,
                        prompt: jobApiPrompt,
                        workflow: promptWrapper.workflow
                    };
                    
                    // Add image references if found for remote workers
                    if (options._imageReferences) {
                        job.imageReferences = options._imageReferences;
                    }
                    
                    jobs.push(job);
                }
            }
        }
        
        const result = await executeJobs(extension, jobs);
        return result;
    } catch (error) {
        extension.log("Parallel execution failed: " + error.message, "error");
        throw error;
    }
}

/**
 * Execute a workflow via DistributedQueue - routes to the least-busy worker.
 * The workflow runs ONLY on the selected worker, not on the master.
 */
export async function executeQueueDistributed(extension, promptWrapper) {
    try {
        const enabledWorkers = extension.enabledWorkers;

        if (enabledWorkers.length === 0) {
            extension.log("DistributedQueue: No enabled workers. Falling back to local execution.", "warn");
            return extension.originalQueuePrompt(0, promptWrapper);
        }

        // Fetch queue status from all enabled workers
        const workerStatuses = await fetchWorkerQueueStatuses(extension, enabledWorkers);

        if (workerStatuses.length === 0) {
            extension.log("DistributedQueue: No reachable workers. Falling back to local execution.", "warn");
            return extension.originalQueuePrompt(0, promptWrapper);
        }

        // Select the least-busy worker
        const selectedWorker = selectLeastBusyWorker(extension, workerStatuses);
        const worker = selectedWorker.worker;
        const queueRemaining = selectedWorker.queueRemaining;

        extension.log(`DistributedQueue: Routing to ${worker.name} (queue_remaining=${queueRemaining})`, "info");

        // Prepare the prompt with skip_dispatch=True for DistributedQueue nodes
        const promptToSend = JSON.parse(JSON.stringify(promptWrapper.output));
        markSkipDispatch(promptToSend);

        // Dispatch to the selected worker
        const workerUrl = extension.getWorkerUrl(worker);
        try {
            const response = await fetch(`${workerUrl}/prompt`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                mode: 'cors',
                body: JSON.stringify({
                    prompt: promptToSend,
                    extra_data: { extra_pnginfo: { workflow: promptWrapper.workflow } },
                    client_id: api.clientId
                })
            });

            if (!response.ok) {
                throw new Error(`Worker returned ${response.status}`);
            }

            const result = await response.json();
            extension.log(`DistributedQueue: Dispatched to ${worker.name}, prompt_id=${result.prompt_id}`, "info");

            return { prompt_id: result.prompt_id, worker_id: worker.id };
        } catch (error) {
            extension.log(`DistributedQueue: Failed to dispatch to ${worker.name}: ${error.message}`, "error");
            // Fall back to local execution
            return extension.originalQueuePrompt(0, promptWrapper);
        }
    } catch (error) {
        extension.log("DistributedQueue execution failed: " + error.message, "error");
        throw error;
    }
}

async function fetchWorkerQueueStatuses(extension, workers) {
    const statuses = [];

    const checkPromises = workers.map(async (worker) => {
        const url = extension.getWorkerUrl(worker, '/prompt');
        try {
            const response = await fetch(url, {
                method: 'GET',
                mode: 'cors',
                cache: 'no-store',
                signal: AbortSignal.timeout(TIMEOUTS.STATUS_CHECK)
            });

            if (response.ok) {
                const data = await response.json();
                const queueRemaining = data.exec_info?.queue_remaining || 0;
                return { worker, queueRemaining, online: true };
            }
        } catch (error) {
            extension.log(`DistributedQueue: Worker ${worker.name} unreachable: ${error.message}`, "debug");
        }
        return null;
    });

    const results = await Promise.all(checkPromises);
    return results.filter(r => r !== null);
}

function selectLeastBusyWorker(extension, statuses) {
    // Find idle workers (queue_remaining == 0)
    const idleWorkers = statuses.filter(s => s.queueRemaining === 0);

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
    return statuses.reduce((min, s) => s.queueRemaining < min.queueRemaining ? s : min);
}

function markSkipDispatch(promptObj) {
    for (const node of Object.values(promptObj)) {
        if (node && typeof node === 'object' && node.class_type === 'DistributedQueue') {
            node.inputs = node.inputs || {};
            node.inputs.skip_dispatch = true;
        }
    }
}

export async function prepareApiPromptForParticipant(extension, baseApiPrompt, participantId, options = {}) {
    let jobApiPrompt = JSON.parse(JSON.stringify(baseApiPrompt));
    const isMaster = participantId === 'master';
    const delegateMaster = Boolean(options.delegate_master);
    
    // Find all distributed nodes once (before pruning)
    let collectorNodes = findNodesByClass(jobApiPrompt, "DistributedCollector");
    const upscaleNodes = findNodesByClass(jobApiPrompt, "UltimateSDUpscaleDistributed");
    let allDistributedNodes = [...collectorNodes, ...upscaleNodes];

    if (isMaster && delegateMaster) {
        if (upscaleNodes.length > 0) {
            extension.log("Delegate-only master mode does not support UltimateSDUpscaleDistributed nodes yet. Using full prompt.", "warn");
        } else if (collectorNodes.length === 0) {
            extension.log("Delegate-only master mode requested but no collectors found in master prompt. Using full prompt.", "debug");
        } else {
            jobApiPrompt = prepareMasterDelegatePrompt(extension, jobApiPrompt, collectorNodes);
            collectorNodes = findNodesByClass(jobApiPrompt, "DistributedCollector");
            allDistributedNodes = [...collectorNodes, ...upscaleNodes];
        }
    }
    
    // For workers, handle platform-specific path conversion
    if (!isMaster) {
        const workerInfo = extension.config.workers.find(w => w.id === participantId);
        
        if (workerInfo && workerInfo.host) {
            // Remote or cloud worker - needs path translation
            try {
                const workerUrl = extension.getWorkerUrl(workerInfo);
                const systemInfo = await getCachedWorkerSystemInfo(workerUrl);
                const targetSeparator = systemInfo?.platform?.path_separator;
                
                if (targetSeparator) {
                    // Convert paths to match worker's platform
                    jobApiPrompt = convertPathsForPlatform(jobApiPrompt, targetSeparator);
                    extension.log(`Converted paths for ${systemInfo.platform.system} worker ${participantId} (separator: '${targetSeparator}')`, "debug");
                } else {
                    extension.log(`No path separator found for worker ${participantId}, skipping path conversion`, "debug");
                }
            } catch (e) {
                extension.log(`Failed to get system info for worker ${participantId}: ${e.message}`, "warn");
                // Continue without path conversion
            }
        }
        
        // Prune the workflow to only include distributed node dependencies
        if (allDistributedNodes.length > 0) {
            jobApiPrompt = pruneWorkflowForWorker(extension, jobApiPrompt, allDistributedNodes);
        }
    }
    
    // Handle image references for remote workers
    if (!isMaster && options.enabled_worker_ids) {
        // Check if this is a remote worker
        const workerId = participantId;
        const workerInfo = extension.config.workers.find(w => w.id === workerId);
        const isRemote = workerInfo && workerInfo.host;
        
        if (isRemote) {
            // Find all image/video references in the pruned workflow
            const imageReferences = findImageReferences(extension, jobApiPrompt);
            if (imageReferences.size > 0) {
                extension.log(`Found ${imageReferences.size} media references (images/videos) for remote worker ${workerId}`, "debug");
                // Store image references for later processing
                options._imageReferences = imageReferences;
            }
        }
    }
    
    // Handle Distributed seed nodes
    const distributorNodes = findNodesByClass(jobApiPrompt, "DistributedSeed");
    if (distributorNodes.length > 0) {
        extension.log(`Found ${distributorNodes.length} seed node(s)`, "debug");
    }
    
    for (const seedNode of distributorNodes) {
        const { inputs } = jobApiPrompt[seedNode.id];
        inputs.is_worker = !isMaster;
        if (!isMaster) {
            const workerIndex = options.enabled_worker_ids.indexOf(participantId);
            inputs.worker_id = `worker_${workerIndex}`;
            extension.log(`Set seed node ${seedNode.id} for worker ${workerIndex}`, "debug");
        }
    }
    
    // Handle Distributed collector nodes (already found above)
    for (const collector of collectorNodes) {
        const { inputs } = jobApiPrompt[collector.id];
        
        // Check if this collector is downstream from a distributed upscaler
        const hasUpstreamDistributedUpscaler = hasUpstreamNode(
            jobApiPrompt, 
            collector.id, 
            'UltimateSDUpscaleDistributed'
        );
        
        if (hasUpstreamDistributedUpscaler) {
            // Set pass_through mode for this collector
            inputs.pass_through = true;
            extension.log(`Collector ${collector.id} set to pass-through mode (downstream from distributed upscaler)`, "debug");
        } else {
            // Normal collector behavior
            // Get the unique job ID from the map created for this execution
            const uniqueJobId = options.job_id_map ? options.job_id_map.get(collector.id) : collector.id;
            
            // Use the truly unique ID for this execution
            inputs.multi_job_id = uniqueJobId;
            inputs.is_worker = !isMaster;
            if (isMaster) {
                inputs.enabled_worker_ids = JSON.stringify(options.enabled_worker_ids || []);
                if (delegateMaster) {
                    inputs.delegate_only = true;
                }
            } else {
                inputs.master_url = extension.getMasterUrl();
                // Also make the worker_job_id unique to prevent potential caching issues
                inputs.worker_job_id = `${uniqueJobId}_worker_${participantId}`;
                inputs.worker_id = participantId;
            }
        }
    }
    
    // Handle Ultimate SD Upscale Distributed nodes
    for (const upscaleNode of upscaleNodes) {
        const { inputs } = jobApiPrompt[upscaleNode.id];
        
        // Get the unique job ID from the map
        const uniqueJobId = options.job_id_map ? options.job_id_map.get(upscaleNode.id) : upscaleNode.id;
        
        inputs.multi_job_id = uniqueJobId;
        inputs.is_worker = !isMaster;
        
        if (isMaster) {
            inputs.enabled_worker_ids = JSON.stringify(options.enabled_worker_ids || []);
        } else {
            inputs.master_url = extension.getMasterUrl();
            inputs.worker_id = participantId;
            // Workers also need the enabled_worker_ids to calculate tile distribution
            inputs.enabled_worker_ids = JSON.stringify(options.enabled_worker_ids || []);
        }
    }
    
    return jobApiPrompt;
}

export async function prepareDistributedJob(extension, multi_job_id) {
    try {
        await extension.api.prepareJob(multi_job_id);
    } catch (error) {
        extension.log("Error preparing job: " + error.message, "error");
        throw error;
    }
}

function createNumericIdGenerator(promptObj) {
    let maxId = 0;
    for (const key of Object.keys(promptObj)) {
        const numeric = parseInt(key, 10);
        if (!Number.isNaN(numeric)) {
            maxId = Math.max(maxId, numeric);
        }
    }
    return () => {
        maxId += 1;
        return String(maxId);
    };
}

function prepareMasterDelegatePrompt(extension, apiPrompt, collectorNodes) {
    const collectorIds = collectorNodes.map((node) => node.id);
    const nodesToKeep = findCollectorDownstreamNodes(apiPrompt, collectorIds);

    // Ensure collectors themselves are present
    collectorIds.forEach((id) => nodesToKeep.add(id));

    const prunedPrompt = {};
    nodesToKeep.forEach((nodeId) => {
        const nodeData = apiPrompt[nodeId];
        if (nodeData) {
            prunedPrompt[nodeId] = JSON.parse(JSON.stringify(nodeData));
        }
    });

    const prunedIds = new Set(Object.keys(prunedPrompt));

    // Remove dangling references to trimmed nodes
    for (const [nodeId, node] of Object.entries(prunedPrompt)) {
        if (!node.inputs) continue;
        for (const [inputName, inputValue] of Object.entries(node.inputs)) {
            if (Array.isArray(inputValue) && inputValue.length === 2) {
                const sourceId = String(inputValue[0]);
                if (!prunedIds.has(sourceId)) {
                    delete node.inputs[inputName];
                    extension.log(`Removed upstream reference '${inputName}' from node ${nodeId} for delegate-only master prompt.`, "debug");
                }
            }
        }
    }

    const nextId = createNumericIdGenerator(prunedPrompt);
    collectorIds.forEach((collectorId) => {
        const collectorEntry = prunedPrompt[collectorId];
        if (!collectorEntry) return;

        const placeholderId = nextId();
        prunedPrompt[placeholderId] = {
            class_type: "DistributedEmptyImage",
            inputs: {
                height: 64,
                width: 64,
                channels: 3
            }
        };
        prunedIds.add(placeholderId);

        collectorEntry.inputs = collectorEntry.inputs || {};
        collectorEntry.inputs.images = [placeholderId, 0];
    });

    extension.log(`Prepared delegate-only master prompt with ${Object.keys(prunedPrompt).length} nodes (kept ${nodesToKeep.size}).`, "debug");
    return prunedPrompt;
}

export async function executeJobs(extension, jobs) {
    let masterPromptId = null;
    
    // Pre-load all unique images before dispatching to workers
    const allImageReferences = new Map();
    for (const job of jobs) {
        if (job.type === 'worker' && job.imageReferences) {
            for (const [filename, info] of job.imageReferences) {
                allImageReferences.set(filename, info);
            }
        }
    }
    
    if (allImageReferences.size > 0) {
        extension.log(`Pre-loading ${allImageReferences.size} unique media file(s) for all workers`, "debug");
        await loadImagesForWorker(extension, allImageReferences);
    }
    
    // Now dispatch jobs in parallel
    const promises = jobs.map(job => {
        if (job.type === 'master') {
            return extension.originalQueuePrompt(0, job.promptWrapper).then(result => {
                masterPromptId = result;
                return result;
            });
        } else {
            return dispatchToWorker(extension, job.worker, job.prompt, job.workflow, job.imageReferences);
        }
    });
    await Promise.all(promises);
    
    // Trigger immediate status check for instant feedback
    extension.checkAllWorkerStatuses();
    
    return masterPromptId || { "prompt_id": "distributed-job-dispatched" };
}

async function dispatchToWorker(extension, worker, prompt, workflow, imageReferences) {
    const workerUrl = extension.getWorkerUrl(worker);
    
    // Debug logging - always log to console for debugging
    extension.log(`[Distributed] === Dispatching to ${worker.name} (${worker.id}) ===`, "debug");
    extension.log('[Distributed] Worker URL: ' + workerUrl, "debug");
    
    // Handle image uploads for remote workers
    if (imageReferences && imageReferences.size > 0) {
        // Check if this is a local worker (same host as master)
        const isLocalWorker = workerUrl.includes('127.0.0.1') || workerUrl.includes('localhost');
        
        if (isLocalWorker) {
            extension.log(`[Distributed] Skipping image processing for local worker ${worker.name} (shares filesystem with master)`, "debug");
        } else {
            extension.log(`[Distributed] Processing ${imageReferences.size} image(s) for remote worker`, "debug");
            
            try {
                // Load images from master
                const images = await loadImagesForWorker(extension, imageReferences);
                
                // Upload images to worker
                if (images.length > 0) {
                    await uploadImagesToWorker(extension, workerUrl, images);
                    extension.log(`[Distributed] Successfully uploaded ${images.length} image(s) to worker`, "debug");
                }
            } catch (error) {
                extension.log(`Failed to process images for worker ${worker.name}: ${error.message}`, "error");
                // Continue with workflow execution even if image upload fails
            }
        }
    }
    
    const promptToSend = {
        prompt,
        workflow,
        client_id: api.clientId
    };

    extension.log('[Distributed] Prompt data: ' + JSON.stringify(promptToSend), "debug");

    try {
        if (extension.config?.settings?.websocket_orchestration) {
            const wsUrl = buildWorkerWebSocketUrl(workerUrl);
            await dispatchPromptViaWebSocket(extension, wsUrl, promptToSend);
            return;
        }
        await fetch(`${workerUrl}/prompt`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            mode: 'cors',
            body: JSON.stringify({
                prompt,
                extra_data: { extra_pnginfo: { workflow } },
                client_id: api.clientId
            })
        });
    } catch (e) {
        extension.log(`Failed to connect to worker ${worker.name} at ${workerUrl}: ${e.message}`, "error");
    }
}

function buildWorkerWebSocketUrl(workerUrl) {
    const url = new URL(workerUrl);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    url.pathname = '/distributed/worker_ws';
    url.search = '';
    return url.toString();
}

function generateRequestId() {
    if (crypto.randomUUID) {
        return crypto.randomUUID();
    }
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = Math.random() * 16 | 0;
        const v = c === 'x' ? r : (r & 0x3 | 0x8);
        return v.toString(16);
    });
}

async function dispatchPromptViaWebSocket(extension, wsUrl, payload) {
    const requestId = generateRequestId();
    const message = {
        type: 'dispatch_prompt',
        request_id: requestId,
        prompt: payload.prompt,
        workflow: payload.workflow,
        client_id: payload.client_id
    };

    await new Promise((resolve, reject) => {
        const ws = new WebSocket(wsUrl);
        const timeoutId = setTimeout(() => {
            ws.close();
            reject(new Error('Websocket dispatch timed out.'));
        }, 60000);

        ws.addEventListener('open', () => {
            ws.send(JSON.stringify(message));
        });

        ws.addEventListener('message', (event) => {
            try {
                const data = JSON.parse(event.data || '{}');
                if (data.type !== 'dispatch_ack' || data.request_id !== requestId) {
                    return;
                }
                clearTimeout(timeoutId);
                ws.close();
                if (data.ok) {
                    resolve();
                } else {
                    reject(new Error(data.error || 'Worker rejected websocket dispatch.'));
                }
            } catch (error) {
                clearTimeout(timeoutId);
                ws.close();
                reject(error);
            }
        });

        ws.addEventListener('error', () => {
            clearTimeout(timeoutId);
            ws.close();
            reject(new Error('Websocket connection failed.'));
        });
    });
    extension.log(`[Distributed] Websocket dispatch succeeded for ${wsUrl}`, "debug");
}

export async function loadImagesForWorker(extension, imageReferences) {
    const images = [];
    
    // Use a cache to avoid loading the same image multiple times
    if (!extension._imageCache) {
        extension._imageCache = new Map();
    }
    
    for (const [filename, info] of imageReferences) {
        try {
            // Check cache first
            if (extension._imageCache.has(filename)) {
                images.push(extension._imageCache.get(filename));
                extension.log(`Using cached image: ${filename}`, "debug");
                continue;
            }
            
            // Limit cache size
            if (extension._imageCache.size >= 10) {
                const oldestKey = extension._imageCache.keys().next().value;
                extension._imageCache.delete(oldestKey);
                extension.log(`Evicted oldest cache entry: ${oldestKey} (cache limit reached)`, "debug");
            }
            
            // Load image from master's filesystem via API
            try {
                const data = await extension.api.loadImage(filename);
                const imageData = {
                    name: filename,
                    image: data.image_data,
                    hash: data.hash  // Include hash from the response
                };
                images.push(imageData);
                
                // Cache the image for future use
                extension._imageCache.set(filename, imageData);
                extension.log(`Loaded and cached image: ${filename}`, "debug");
            } catch (loadError) {
                extension.log(`Failed to load image ${filename}: ${loadError.message}`, "error");
                throw loadError;
            }
        } catch (error) {
            extension.log(`Error loading image ${filename}: ${error.message}`, "error");
        }
    }
    
    // Clear cache after a reasonable time to avoid memory issues
    setTimeout(() => {
        if (extension._imageCache && extension._imageCache.size > 0) {
            extension.log(`Clearing image cache (${extension._imageCache.size} images)`, "debug");
            extension._imageCache.clear();
        }
    }, TIMEOUTS.IMAGE_CACHE_CLEAR); // Clear after 30 seconds
    
    return images;
}

export async function uploadImagesToWorker(extension, workerUrl, images) {
    // Upload images to worker's ComfyUI instance
    for (const imageData of images) {
        // Check if file already exists with matching hash
        if (imageData.hash) {
            try {
                const checkResponse = await fetch(`${workerUrl}/distributed/check_file`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    mode: 'cors',
                    body: JSON.stringify({ 
                        filename: imageData.name, 
                        hash: imageData.hash 
                    })
                });
                
                if (checkResponse.ok) {
                    const result = await checkResponse.json();
                    if (result.exists && result.hash_matches) {
                        extension.log(`File ${imageData.name} already exists on worker with matching hash, skipping upload`, "debug");
                        continue;
                    }
                }
            } catch (error) {
                // If check fails, proceed with upload
                extension.log(`Failed to check file existence for ${imageData.name}: ${error.message}`, "debug");
            }
        }
        
        const formData = new FormData();
        
        // Detect MIME type from base64 header (supports both image and video)
        const mimeMatch = imageData.image.match(/^data:((?:image|video)\/\w+);base64,/);
        const mimeType = mimeMatch ? mimeMatch[1] : 'image/png'; // Default to PNG if not detected
        
        // Convert base64 to blob
        const base64Data = imageData.image.replace(/^data:(?:image|video)\/\w+;base64,/, '');
        const byteCharacters = atob(base64Data);
        const byteNumbers = new Array(byteCharacters.length);
        for (let i = 0; i < byteCharacters.length; i++) {
            byteNumbers[i] = byteCharacters.charCodeAt(i);
        }
        const byteArray = new Uint8Array(byteNumbers);
        const blob = new Blob([byteArray], { type: mimeType });
        
        // Use original filename without heavy cleaning
        let cleanName = imageData.name;
        let subfolder = '';
        
        // Extract subfolder if present (handle both slash styles)
        if (cleanName.includes('/') || cleanName.includes('\\')) {
            const parts = cleanName.replace(/\\/g, '/').split('/');
            subfolder = parts.slice(0, -1).join('/');
            cleanName = parts[parts.length - 1];
        }
        
        formData.append('image', blob, cleanName);
        formData.append('type', 'input');
        formData.append('subfolder', subfolder);
        formData.append('overwrite', 'true');
        
        try {
            const response = await fetch(`${workerUrl}/upload/image`, {
                method: 'POST',
                mode: 'cors',
                body: formData
            });
            
            if (!response.ok) {
                throw new Error(`Upload failed: ${response.statusText}`);
            }
            
            extension.log(`Uploaded image to worker: ${imageData.name} -> ${subfolder}/${cleanName}`, "debug");
        } catch (error) {
            extension.log(`Failed to upload ${imageData.name}: ${error.message}`, "error");
            // Continue with other images
        }
    }
}

export async function performPreflightCheck(extension, workers) {
    if (workers.length === 0) return [];
    
    extension.log(`Performing pre-flight health check on ${workers.length} workers...`, "debug");
    const startTime = Date.now();
    
    const checkPromises = workers.map(async (worker) => {
        const url = extension.getWorkerUrl(worker, '/prompt');
        
        extension.log(`Pre-flight checking ${worker.name} at: ${url}`, "debug");
        
        try {
            const response = await fetch(url, {
                method: 'GET',
                mode: 'cors',
                cache: 'no-store',
                signal: AbortSignal.timeout(TIMEOUTS.STATUS_CHECK)
            });

            if (response.ok) {
                extension.log(`Worker ${worker.name} is active`, "debug");
                return { worker, active: true };
            } else {
                extension.log(`Worker ${worker.name} returned ${response.status}`, "debug");
                return { worker, active: false };
            }
        } catch (error) {
            if (error?.name === 'AbortError') {
                extension.log(`Worker ${worker.name} pre-flight check timed out; assuming active`, "debug");
                return { worker, active: true, uncertain: true };
            }
            extension.log(`Worker ${worker.name} is offline or unreachable: ${error.message}`, "debug");
            return { worker, active: false };
        }
    });
    
    const results = await Promise.all(checkPromises);
    const activeWorkers = results.filter(r => r.active).map(r => r.worker);
    
    const elapsed = Date.now() - startTime;
    extension.log(`Pre-flight check completed in ${elapsed}ms. Active workers: ${activeWorkers.length}/${workers.length}`, "debug");
    
    // Update UI status indicators for inactive workers
    results.filter(r => !r.active).forEach(r => {
        const statusDot = document.getElementById(`status-${r.worker.id}`);
        if (statusDot) {
            // Remove pulsing animation once status is determined
            statusDot.classList.remove('status-pulsing');
            statusDot.style.backgroundColor = "#c04c4c"; // Red for offline
            statusDot.title = "Offline - Cannot connect";
        }
    });
    
    return activeWorkers;
}
