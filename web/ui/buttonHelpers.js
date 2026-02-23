export function createButtonHelper(ui, text, onClick, style) {
    return ui.createButton(text, onClick, style);
}

export function createCheckboxSetting(id, label, tooltip, checked, onChange) {
    const group = document.createElement("div");
    group.style.cssText = "grid-column: 1 / span 2; display: flex; align-items: center; gap: 8px;";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.id = id;
    checkbox.checked = checked;
    checkbox.onchange = onChange;

    const lbl = document.createElement("label");
    lbl.htmlFor = id;
    lbl.textContent = label;
    lbl.style.cssText = "font-size: 12px; color: #ccc; cursor: pointer;";
    if (tooltip) {
        lbl.title = tooltip;
    }

    group.appendChild(checkbox);
    group.appendChild(lbl);
    return group;
}

export function createNumberSetting(id, label, tooltip, value, min, step, onChange) {
    const group = document.createElement("div");
    group.style.cssText = "grid-column: 1 / span 2; display: flex; align-items: center; gap: 6px;";

    const lbl = document.createElement("label");
    lbl.htmlFor = id;
    lbl.textContent = label;
    lbl.style.cssText = "font-size: 12px; color: #ccc;";
    if (tooltip) {
        lbl.title = tooltip;
    }

    const input = document.createElement("input");
    input.type = "number";
    input.id = id;
    input.min = String(min);
    input.step = String(step);
    input.style.cssText = "width: 80px; padding: 2px 6px; background: #222; color: #ddd; border: 1px solid #333; border-radius: 3px;";
    input.value = value;
    input.onchange = onChange;

    group.appendChild(lbl);
    group.appendChild(input);
    return group;
}
