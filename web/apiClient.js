import { TIMEOUTS } from './constants.js';
import { normalizeWorkerUrl } from './urlUtils.js';

export function createApiClient(baseUrl) {
    const normalizedBaseUrl = normalizeWorkerUrl(baseUrl);

    const request = async (endpoint, options = {}, retries = TIMEOUTS.MAX_RETRIES) => {
        let lastError;
        let delay = TIMEOUTS.RETRY_DELAY; // Initial delay for exponential backoff

        for (let attempt = 0; attempt < retries; attempt++) {
            try {
                const response = await fetch(`${normalizedBaseUrl}${endpoint}`, {
                    headers: { 'Content-Type': 'application/json' },
                    ...options
                });
                
                if (!response.ok) {
                    const error = await response.json().catch(() => ({ message: 'Request failed' }));
                    throw new Error(error.message || `HTTP ${response.status}`);
                }
                
                return await response.json();
            } catch (error) {
                lastError = error;
                console.log(`API Error (attempt ${attempt + 1}/${retries}): ${endpoint} - ${error.message}`);
                if (attempt < retries - 1) {
                    await new Promise(resolve => setTimeout(resolve, delay));
                    delay *= 2; // Exponential backoff
                }
            }
        }
        throw lastError;
    };
    
    return {
        // Config endpoints
        async getConfig() {
            return request('/distributed/config');
        },
        
        async updateWorker(workerId, data) {
            return request('/distributed/config/update_worker', {
                method: 'POST',
                body: JSON.stringify({ worker_id: workerId, ...data })
            });
        },
        
        async deleteWorker(workerId) {
            return request('/distributed/config/delete_worker', {
                method: 'POST',
                body: JSON.stringify({ worker_id: workerId })
            });
        },
        
        async updateSetting(key, value) {
            return request('/distributed/config/update_setting', {
                method: 'POST',
                body: JSON.stringify({ key, value })
            });
        },
        
        async updateMaster(data) {
            return request('/distributed/config/update_master', {
                method: 'POST',
                body: JSON.stringify(data)
            });
        },
        
        // Worker management endpoints
        async launchWorker(workerId) {
            return request('/distributed/launch_worker', {
                method: 'POST',
                body: JSON.stringify({ worker_id: workerId })
            });
        },
        
        async stopWorker(workerId) {
            return request('/distributed/stop_worker', {
                method: 'POST',
                body: JSON.stringify({ worker_id: workerId })
            });
        },
        
        async getManagedWorkers() {
            return request('/distributed/managed_workers');
        },
        
        async getWorkerLog(workerId, lines = 1000) {
            return request(`/distributed/worker_log/${workerId}?lines=${lines}`);
        },
        
        async clearLaunchingFlag(workerId) {
            return request('/distributed/worker/clear_launching', {
                method: 'POST',
                body: JSON.stringify({ worker_id: workerId })
            });
        },
        
        async queueDistributed(payload) {
            return request('/distributed/queue', {
                method: 'POST',
                body: JSON.stringify(payload)
            });
        },

        async probeWorker(workerUrl, timeoutMs = TIMEOUTS.STATUS_CHECK) {
            const normalizedWorkerUrl = normalizeWorkerUrl(workerUrl);
            const response = await fetch(`${normalizedWorkerUrl}/prompt`, {
                method: 'GET',
                mode: 'cors',
                cache: 'no-store',
                signal: AbortSignal.timeout(timeoutMs)
            });

            if (!response.ok) {
                return { ok: false, status: response.status, queueRemaining: null };
            }

            const data = await response.json().catch(() => ({}));
            return {
                ok: true,
                status: response.status,
                queueRemaining: data.exec_info?.queue_remaining || 0,
            };
        },

        async dispatchToWorker(workerUrl, promptPayload) {
            const normalizedWorkerUrl = normalizeWorkerUrl(workerUrl);
            const response = await fetch(`${normalizedWorkerUrl}/prompt`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                mode: 'cors',
                body: JSON.stringify(promptPayload),
            });

            if (!response.ok) {
                throw new Error(`Worker returned ${response.status}`);
            }

            return response.json();
        },
        
        // Network info
        async getNetworkInfo() {
            return request('/distributed/network_info');
        },
        
        // Status checking (with timeout)
        async checkStatus(url, timeout = TIMEOUTS.DEFAULT_FETCH) {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), timeout);
            
            try {
                const response = await fetch(url, {
                    method: 'GET',
                    mode: 'cors',
                    signal: controller.signal
                });
                clearTimeout(timeoutId);
                
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                return await response.json();
            } catch (error) {
                clearTimeout(timeoutId);
                throw error;
            }
        },
        
        // Batch status checking
        async checkMultipleStatuses(urls) {
            return Promise.allSettled(
                urls.map(url => this.checkStatus(url))
            );
        },

        // Cloudflare tunnel management
        async startTunnel() {
            return request('/distributed/tunnel/start', {
                method: 'POST',
                body: JSON.stringify({})
            });
        },

        async stopTunnel() {
            return request('/distributed/tunnel/stop', {
                method: 'POST',
                body: JSON.stringify({})
            });
        },

        async getTunnelStatus() {
            return request('/distributed/tunnel/status');
        }
    };
}
