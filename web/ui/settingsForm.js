import { BUTTON_STYLES } from '../constants.js';
import { cancelWorkerSettings, deleteWorker, isRemoteWorker, saveWorkerSettings } from '../workerSettings.js';

export function createWorkerSettingsForm(ui, extension, worker) {
    const form = document.createElement("div");
    form.style.cssText = "display: flex; flex-direction: column; gap: 8px;";

    const nameGroup = ui.createFormGroup("Name:", worker.name, `name-${worker.id}`);
    form.appendChild(nameGroup.group);

    const typeGroup = document.createElement("div");
    typeGroup.style.cssText = "display: flex; flex-direction: column; gap: 4px; margin: 5px 0;";

    const typeLabel = document.createElement("label");
    typeLabel.htmlFor = `worker-type-${worker.id}`;
    typeLabel.textContent = "Worker Type:";
    typeLabel.style.cssText = "font-size: 12px; color: #ccc;";

    const typeSelect = document.createElement("select");
    typeSelect.id = `worker-type-${worker.id}`;
    typeSelect.style.cssText = "padding: 4px 8px; background: #333; color: #fff; border: 1px solid #555; border-radius: 4px; font-size: 12px;";

    const localOption = document.createElement("option");
    localOption.value = "local";
    localOption.textContent = "Local";

    const remoteOption = document.createElement("option");
    remoteOption.value = "remote";
    remoteOption.textContent = "Remote";

    const cloudOption = document.createElement("option");
    cloudOption.value = "cloud";
    cloudOption.textContent = "Cloud";

    typeSelect.appendChild(localOption);
    typeSelect.appendChild(remoteOption);
    typeSelect.appendChild(cloudOption);

    const runpodText = document.createElement("a");
    runpodText.id = `runpod-text-${worker.id}`;
    runpodText.href = "https://github.com/robertvoy/ComfyUI-Distributed/blob/main/docs/worker-setup-guides.md#cloud-workers";
    runpodText.target = "_blank";
    runpodText.textContent = "Deploy Cloud Worker with Runpod";
    runpodText.style.cssText = "font-size: 12px; color: #4a90e2; text-decoration: none; margin-top: 4px; display: none; cursor: pointer;";

    const createOnChangeHandler = () => {
        return (e) => {
            const workerType = e.target.value;
            const hostGroup = document.getElementById(`host-group-${worker.id}`);
            const hostInput = document.getElementById(`host-${worker.id}`);
            const portGroup = document.getElementById(`port-group-${worker.id}`);
            const portInput = document.getElementById(`port-${worker.id}`);
            const cudaGroup = document.getElementById(`cuda-group-${worker.id}`);
            const argsGroup = document.getElementById(`args-group-${worker.id}`);
            const runpodTextElem = document.getElementById(`runpod-text-${worker.id}`);

            if (!hostGroup || !portGroup || !cudaGroup || !argsGroup || !runpodTextElem || !hostInput || !portInput) {
                return;
            }

            if (workerType === "local") {
                hostGroup.style.display = "none";
                portGroup.style.display = "flex";
                cudaGroup.style.display = "flex";
                argsGroup.style.display = "flex";
                runpodTextElem.style.display = "none";
            } else if (workerType === "remote") {
                hostGroup.style.display = "flex";
                portGroup.style.display = "flex";
                cudaGroup.style.display = "none";
                argsGroup.style.display = "none";
                runpodTextElem.style.display = "none";
                hostInput.placeholder = "e.g., 192.168.1.100";
                if (hostInput.value === "localhost" || hostInput.value === "127.0.0.1") {
                    hostInput.value = "";
                }
            } else if (workerType === "cloud") {
                hostGroup.style.display = "flex";
                portGroup.style.display = "flex";
                cudaGroup.style.display = "none";
                argsGroup.style.display = "none";
                runpodTextElem.style.display = "block";
                hostInput.placeholder = "e.g., your-cloud-worker.trycloudflare.com";
                portInput.value = "443";
                if (hostInput.value === "localhost" || hostInput.value === "127.0.0.1") {
                    hostInput.value = "";
                }
            }
        };
    };

    typeGroup.appendChild(typeLabel);
    typeGroup.appendChild(typeSelect);
    typeGroup.appendChild(runpodText);
    form.appendChild(typeGroup);

    const hostGroup = ui.createFormGroup("Host:", worker.host || "", `host-${worker.id}`, "text", "e.g., 192.168.1.100");
    hostGroup.group.id = `host-group-${worker.id}`;
    hostGroup.group.style.display = (isRemoteWorker(extension, worker) || worker.type === "cloud") ? "flex" : "none";
    form.appendChild(hostGroup.group);

    const portGroup = ui.createFormGroup("Port:", worker.port, `port-${worker.id}`, "number");
    portGroup.group.id = `port-group-${worker.id}`;
    form.appendChild(portGroup.group);

    const cudaGroup = ui.createFormGroup("CUDA Device:", worker.cuda_device || 0, `cuda-${worker.id}`, "number");
    cudaGroup.group.id = `cuda-group-${worker.id}`;
    cudaGroup.group.style.display = (isRemoteWorker(extension, worker) || worker.type === "cloud") ? "none" : "flex";
    form.appendChild(cudaGroup.group);

    const argsGroup = ui.createFormGroup("Extra Args:", worker.extra_args || "", `args-${worker.id}`);
    argsGroup.group.id = `args-group-${worker.id}`;
    argsGroup.group.style.display = (isRemoteWorker(extension, worker) || worker.type === "cloud") ? "none" : "flex";
    form.appendChild(argsGroup.group);

    const saveBtn = ui.createButton("Save", () => saveWorkerSettings(extension, worker.id), "background-color: #4a7c4a;");
    saveBtn.style.cssText = BUTTON_STYLES.base + BUTTON_STYLES.success;

    const cancelBtn = ui.createButton("Cancel", () => cancelWorkerSettings(extension, worker.id), "background-color: #555;");
    cancelBtn.style.cssText = BUTTON_STYLES.base + BUTTON_STYLES.cancel;

    const deleteBtn = ui.createButton("Delete", () => deleteWorker(extension, worker.id), "background-color: #7c4a4a;");
    deleteBtn.style.cssText = BUTTON_STYLES.base + BUTTON_STYLES.error + BUTTON_STYLES.marginLeftAuto;

    const buttonGroup = ui.createButtonGroup([saveBtn, cancelBtn, deleteBtn], " margin-top: 8px;");
    form.appendChild(buttonGroup);

    typeSelect.onchange = createOnChangeHandler();

    if (worker.type === "cloud") {
        typeSelect.value = "cloud";
        runpodText.style.display = "block";
    } else if (isRemoteWorker(extension, worker)) {
        typeSelect.value = "remote";
    } else {
        typeSelect.value = "local";
    }

    typeSelect.dispatchEvent(new Event('change'));

    return form;
}
