import { TIMEOUTS } from '../constants.js';

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

export function createLogModal() {
    let _modalEl = null;
    let _keydownHandler = null;
    let _refreshTimer = null;
    let _fetching = false;
    let _fetchLog = null;
    let _onClose = null;
    let _logContentEl = null;
    let _statusBarEl = null;
    let _refreshCheckbox = null;

    const updateLogView = (logData) => {
        if (!_logContentEl || !_statusBarEl || !logData) {
            return;
        }

        const shouldAutoScroll =
            _logContentEl.scrollTop + _logContentEl.clientHeight >= _logContentEl.scrollHeight - 50;
        _logContentEl.textContent = logData.content || '';
        if (shouldAutoScroll) {
            _logContentEl.scrollTop = _logContentEl.scrollHeight;
        }

        let statusText;
        if (logData.source === "memory") {
            statusText = "Remote worker log (in-memory buffer)";
            if (logData.truncated) {
                statusText += ` (showing last ${logData.lines_shown || 0} lines)`;
            }
        } else {
            statusText = `Log file: ${logData.log_file || 'unknown'}`;
            if (logData.truncated) {
                statusText += ` (showing last ${logData.lines_shown} lines of ${formatFileSize(logData.file_size || 0)})`;
            }
        }
        _statusBarEl.textContent = statusText;
    };

    const refreshLog = async () => {
        if (_fetching || !_fetchLog || !_refreshCheckbox?.checked) {
            return;
        }
        _fetching = true;
        try {
            const data = await _fetchLog();
            if (data) {
                updateLogView(data);
            }
        } catch (_error) {
            // Keep the modal open and continue retrying on next interval.
        } finally {
            _fetching = false;
        }
    };

    const stopRefresh = () => {
        if (_refreshTimer) {
            clearInterval(_refreshTimer);
            _refreshTimer = null;
        }
    };

    const startRefresh = () => {
        stopRefresh();
        _refreshTimer = setInterval(() => {
            refreshLog();
        }, TIMEOUTS.LOG_REFRESH);
    };

    const unmount = () => {
        stopRefresh();

        if (_keydownHandler) {
            document.removeEventListener('keydown', _keydownHandler);
            _keydownHandler = null;
        }

        if (_modalEl) {
            _modalEl.remove();
            _modalEl = null;
        }

        const onClose = _onClose;
        _onClose = null;
        if (onClose) {
            onClose();
        }
    };

    const mount = (container, { workerName, logData, onClose, fetchLog, themeClass = "" }) => {
        _onClose = onClose;
        _fetchLog = fetchLog;

        const modal = document.createElement('div');
        modal.id = 'distributed-log-modal';
        modal.className = 'log-modal';
        if (themeClass) {
            modal.classList.add(themeClass);
        }

        const content = document.createElement('div');
        content.className = 'log-modal__content';

        const header = document.createElement('div');
        header.className = 'log-modal__header';

        const title = document.createElement('h3');
        title.className = 'log-modal__title';
        title.textContent = `${workerName} - Log Viewer`;

        const headerButtons = document.createElement('div');
        headerButtons.className = 'log-modal__header-buttons';

        const refreshContainer = document.createElement('div');
        refreshContainer.className = 'log-modal__refresh';

        const refreshCheckbox = document.createElement('input');
        refreshCheckbox.type = 'checkbox';
        refreshCheckbox.id = 'log-auto-refresh';
        refreshCheckbox.className = 'log-modal__refresh-input';
        refreshCheckbox.checked = true;

        const refreshLabel = document.createElement('label');
        refreshLabel.htmlFor = 'log-auto-refresh';
        refreshLabel.className = 'log-modal__refresh-label';
        refreshLabel.textContent = 'Auto-refresh';

        refreshContainer.appendChild(refreshCheckbox);
        refreshContainer.appendChild(refreshLabel);

        const closeBtn = document.createElement('button');
        closeBtn.className = 'distributed-button log-modal__close';
        closeBtn.textContent = '✕';

        headerButtons.appendChild(refreshContainer);
        headerButtons.appendChild(closeBtn);

        header.appendChild(title);
        header.appendChild(headerButtons);

        const logContainer = document.createElement('div');
        logContainer.className = 'log-modal__body';
        logContainer.id = 'distributed-log-content';

        const statusBar = document.createElement('div');
        statusBar.className = 'log-modal__status';

        content.appendChild(header);
        content.appendChild(logContainer);
        content.appendChild(statusBar);
        modal.appendChild(content);

        closeBtn.addEventListener('click', unmount);
        modal.addEventListener('click', (event) => {
            if (event.target === modal) {
                unmount();
            }
        });

        _keydownHandler = (event) => {
            if (event.key === 'Escape') {
                unmount();
            }
        };
        document.addEventListener('keydown', _keydownHandler);

        _modalEl = modal;
        _logContentEl = logContainer;
        _statusBarEl = statusBar;
        _refreshCheckbox = refreshCheckbox;

        refreshCheckbox.addEventListener('change', () => {
            if (refreshCheckbox.checked) {
                refreshLog();
            }
        });

        container.appendChild(modal);
        updateLogView(logData);

        requestAnimationFrame(() => {
            if (_logContentEl) {
                _logContentEl.scrollTop = _logContentEl.scrollHeight;
            }
        });

        startRefresh();
    };

    return {
        mount,
        unmount,
        update: updateLogView,
    };
}
