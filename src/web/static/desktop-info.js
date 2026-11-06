// Сведения об устройстве: разделы приходят по мере сбора, отчёт формирует сервер.
(() => {
    'use strict';
    const T = window.DNODesktopTools;
    const { el, action, select, request, makeTool, errorMessage } = T;

    const tool = makeTool('device-info', 'Сведения об устройстве', 'Оборудование, сеть, пользователи и диски выбранного хоста.');

    function createTab(iid, root, label) {
        let controller = null, requestId = null, finished = false;
        const sections = new Map();
        const profile = select('Объём', [['brief', 'Краткие сведения'], ['full', 'Полные сведения']]);
        const start = action('Собрать', collect, true);
        const stop = action('Отменить', () => controller?.abort()); stop.disabled = true;
        const toolbar = el('div', 'tool-toolbar');
        toolbar.append(profile.wrap, start, stop);
        const format = select('Формат отчёта', [['txt', 'Текст'], ['json', 'JSON']]);
        const save = action('Сохранить отчёт', saveReport); save.disabled = true;
        save.prepend(window.DNODesktop.icon('menu_save'));
        toolbar.append(format.wrap, save);
        const state = el('div', 'tool-status');
        state.setAttribute('role', 'status');
        state.setAttribute('aria-live', 'polite');
        const progress = el('progress', 'tool-progress'); progress.max = 1; progress.value = 0;
        progress.setAttribute('aria-label', 'Прогресс сбора');
        const list = el('div', 'device-info-sections');
        root.append(toolbar, progress, state, list);

        function report(text, error = false) {
            state.textContent = text ? errorMessage(text) : '';
            state.classList.toggle('error', error);
        }
        function renderRows(data, node) {
            if (!data.rows.length) { node.append(el('p', 'tool-note', 'Нет данных')); return; }
            const wrap = el('div', 'tool-table-wrap');
            const table = el('table', 'tool-table device-info-table');
            const head = el('thead'), headRow = el('tr');
            data.columns.forEach(column => headRow.append(el('th', '', column)));
            head.append(headRow);
            const body = el('tbody');
            for (const cells of data.rows) {
                const row = el('tr');
                cells.forEach(cell => row.append(el('td', '', cell.display)));
                body.append(row);
            }
            table.append(head, body);
            wrap.append(table);
            node.append(wrap);
        }
        function placeholder(name) {
            const node = el('details', 'tool-card device-info-section');
            const summary = el('summary');
            const title = el('span', 'device-info-name', name), badge = el('span', 'tool-tag', 'Ожидание');
            summary.append(title, badge);
            const content = el('div', 'device-info-body');
            node.append(summary, content);
            list.append(node);
            sections.set(name, { node, badge, content });
        }
        function showCategory(name, data) {
            if (!sections.has(name)) placeholder(name);
            const section = sections.get(name);
            section.badge.textContent = data.status_label || data.status;
            section.badge.classList.toggle('device-info-problem', !['complete', 'no_data'].includes(data.status));
            section.content.replaceChildren();
            if (data.diagnostic) section.content.append(el('p', 'tool-note', data.diagnostic));
            renderRows(data, section.content);
            if (sections.size <= 6 && data.rows.length) section.node.open = true;
        }
        async function collect() {
            if (controller) return;
            const own = new AbortController();
            controller = own; finished = false; requestId = null;
            start.disabled = true; stop.disabled = false; save.disabled = true;
            sections.clear(); list.replaceChildren();
            progress.value = 0; progress.max = 1;
            report('Подключение…');
            try {
                const response = await request(`/api/tools/device-info/${encodeURIComponent(iid)}`, {
                    method: 'POST', body: JSON.stringify({ profile: profile.input.value }), signal: own.signal,
                });
                await T.events(response, (kind, data) => {
                    if (kind === 'start') {
                        requestId = data.request_id;
                        progress.max = Math.max(1, data.expected.length);
                        data.expected.forEach(placeholder);
                    } else if (kind === 'progress') {
                        progress.value = data.done;
                        report(data.message);
                    } else if (kind === 'category') {
                        showCategory(data.name, data);
                    } else if (kind === 'done') {
                        finished = true;
                        Object.entries(data.categories).forEach(([name, category]) => showCategory(name, category));
                        progress.value = progress.max;
                        report(data.diagnostic ? `${data.status_label}. ${data.diagnostic}` : data.status_label, !['complete', 'partial', 'no_data'].includes(data.status));
                    } else if (kind === 'error') {
                        report(data.message, true);
                    }
                });
                if (!finished && !own.signal.aborted && !state.classList.contains('error')) report('Соединение с сервером прервано', true);
            } catch (error) {
                if (error.name === 'AbortError') report('Сбор отменён');
                else report(error.message, true);
            } finally {
                if (controller === own) controller = null;
                start.disabled = false; stop.disabled = true;
                save.disabled = !finished || !requestId;
            }
        }
        async function saveReport() {
            const kind = format.input.value;
            save.disabled = true;
            try {
                const response = await request(`/api/tools/device-info/report/${encodeURIComponent(requestId)}?format=${kind}`);
                const type = response.headers.get('content-type') || 'text/plain';
                T.download(`${label}.${kind}`, await response.text(), type);
            } catch (error) { report(error.message, true); }
            finally { save.disabled = !finished || !requestId; }
        }
        collect();
        return { close: () => controller?.abort() };
    }

    T.makeHostTabs(tool, { limit: 8, createTab });
    T.activate(tool);
})();
