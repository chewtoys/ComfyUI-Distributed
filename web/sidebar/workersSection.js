import { addNewWorker } from "../workerSettings.js";

export function renderWorkersSection(extension) {
    const workersSection = document.createElement("div");
    workersSection.style.cssText = "flex: 1; overflow-y: auto; margin-bottom: 15px;";

    const workersList = document.createElement("div");
    const workers = extension.config?.workers || [];

    if (workers.length === 0) {
        const blueprintDiv = extension.ui.renderEntityCard(
            "blueprint",
            { onClick: () => addNewWorker(extension) },
            extension
        );
        workersList.appendChild(blueprintDiv);
    }

    workers.forEach((worker) => {
        const workerCard = extension.ui.renderEntityCard("worker", worker, extension);
        workersList.appendChild(workerCard);
    });

    workersSection.appendChild(workersList);

    if (workers.length > 0) {
        const addWorkerDiv = extension.ui.renderEntityCard(
            "add",
            { onClick: () => addNewWorker(extension) },
            extension
        );
        workersSection.appendChild(addWorkerDiv);
    }

    return workersSection;
}
