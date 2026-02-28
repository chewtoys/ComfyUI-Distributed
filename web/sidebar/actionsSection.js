import { BUTTON_STYLES } from "../constants.js";

export function renderActionsSection(extension) {
    const actionsSection = document.createElement("div");
    actionsSection.style.cssText =
        "padding-top: 10px; margin-bottom: 15px; border-top: 1px solid var(--dist-divider, #444);";

    const buttonRow = document.createElement("div");
    buttonRow.style.cssText = "display: flex; gap: 8px;";

    const clearMemButton = extension.ui.createButtonHelper(
        "Clear Worker VRAM",
        (event) => extension._handleClearMemory(event.target),
        BUTTON_STYLES.clearMemory
    );
    clearMemButton.title = "Clear VRAM on all enabled worker GPUs (not master)";
    clearMemButton.style.cssText = BUTTON_STYLES.base + " flex: 1;" + BUTTON_STYLES.clearMemory;

    const interruptButton = extension.ui.createButtonHelper(
        "Interrupt Workers",
        (event) => extension._handleInterruptWorkers(event.target),
        BUTTON_STYLES.interrupt
    );
    interruptButton.title = "Cancel/interrupt execution on all enabled worker GPUs";
    interruptButton.style.cssText = BUTTON_STYLES.base + " flex: 1;" + BUTTON_STYLES.interrupt;

    buttonRow.appendChild(clearMemButton);
    buttonRow.appendChild(interruptButton);
    actionsSection.appendChild(buttonRow);
    return actionsSection;
}
