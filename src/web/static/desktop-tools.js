// Базовые инструменты: серверные API, отмена запросов и ограниченная история.
(() => {
    'use strict';
    const D = window.DNODesktop;
    const A = window.DNODesktopAPI;
    const { message: errorMessage, normalize: normalizeError } = window.DNOWebErrors;
    const el = D.element;
    const tools = new Map();
    const states = { open: 'Ответ', closed: 'Закрыт', timeout: 'Таймаут', unreachable: 'Недоступен', error: 'Ошибка', no_reply: 'Нет ответа', cancelled: 'Остановлено' };

    async function request(path, options = {}) {
        if (!A.authenticated()) throw new Error('Войдите в систему через кнопку в верхней панели');
        const authorization = A.headers().Authorization;
        let response;
        try {
            response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...A.headers(), ...options.headers } });
        } catch (error) { throw normalizeError(error); }
        if (A.headers().Authorization !== authorization) throw new DOMException('Сеанс изменился', 'AbortError');
        if (!response.ok) {
            if (response.status === 401) {
                await A.api('/api/version');
            }
            const data = await response.json().catch(() => ({}));
            const detail = Array.isArray(data.detail) ? data.detail.map(item => item.msg).join('; ') : data.detail;
            const error = new Error(errorMessage(detail || `Ошибка сервера: ${response.status}`));
            error.status = response.status;
            throw error;
        }
        return response;
    }
    async function json(path, payload, signal) {
        try { return await (await request(path, { method: payload ? 'POST' : 'GET', body: payload ? JSON.stringify(payload) : undefined, signal })).json(); }
        catch (error) { throw normalizeError(error); }
    }
    function field(label, value, type = 'text', options = {}) {
        const wrap = el('label', 'tool-field');
        wrap.append(el('span', '', label));
        const input = el('input');
        Object.assign(input, { type, value, ...options });
        wrap.append(input);
        return { wrap, input };
    }
    function select(label, entries) {
        const wrap = el('label', 'tool-field');
        wrap.append(el('span', '', label));
        const input = el('select');
        for (const [value, text] of entries) { const option = el('option', '', text); option.value = value; input.append(option); }
        wrap.append(input);
        return { wrap, input };
    }
    function makeTool(id, title, subtitle, tag) {
        const root = document.getElementById(`tool-${id}`);
        const content = el('div', 'tool-content');
        const intro = el('div', 'tool-intro');
        const text = el('div');
        text.append(el('h2', '', title), el('p', '', subtitle));
        intro.append(text);
        if (tag) intro.append(el('span', 'tool-tag', tag));
        const status = el('div', 'tool-status', 'Войдите в систему для работы с инструментом');
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        content.append(intro);
        root.append(content);
        const tool = { id, root, content, status, stop: () => {}, refresh: () => {}, clear: () => {}, loaded: false };
        tool.message = (value, error = false) => { status.textContent = value ? errorMessage(value) : ''; status.classList.toggle('error', error); };
        tools.set(id, tool);
        return tool;
    }
    function action(text, fn, primary = false) { return D.button(text, fn, `tool-button${primary ? ' primary' : ''}`); }
    // Кнопка панели как в Qt: значок темы, название в подсказке и для экранного диктора.
    function iconAction(key, label, fn) {
        const node = D.button('', fn, 'tool-button tool-icon-button');
        node.title = label;
        node.setAttribute('aria-label', label);
        node.append(D.icon(key));
        return node;
    }
    function table(headers) {
        const wrap = el('div', 'tool-table-wrap');
        const node = el('table', 'tool-table');
        const head = el('thead');
        const row = el('tr');
        headers.forEach(header => row.append(el('th', '', header)));
        head.append(row);
        const body = el('tbody');
        node.append(head, body);
        wrap.append(node);
        return { wrap, body };
    }
    function row(body, values, prepend = false) {
        const node = el('tr');
        values.forEach(value => node.append(el('td', '', value)));
        if (prepend) body.prepend(node); else body.append(node);
        return node;
    }
    function stat(label, value = '—', note = '') {
        const node = el('div', 'tool-stat');
        const number = el('strong', '', value);
        node.append(el('span', '', label), number, el('small', '', note));
        return { node, number };
    }
    function download(name, text, type) {
        const url = URL.createObjectURL(new Blob(['\ufeff', text], { type }));
        const link = el('a');
        link.href = url; link.download = name; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    function formatDuration(seconds, { clock = false, milliseconds = true } = {}) {
        if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '—';
        if (seconds === 0) return '0 с';
        if (milliseconds && seconds < 1) { const value = Math.round(seconds * 1000); return value < 1000 ? `${value} мс` : '1 с'; }
        if (seconds < 60) {
            const rounded = Math.round(seconds * 10) / 10;
            if (rounded < 60) return `${rounded.toLocaleString('ru-RU', { maximumFractionDigits: 1 })} с`;
            seconds = rounded;
        }
        let total = Math.floor(seconds);
        const days = Math.floor(total / 86400), hours = Math.floor(total % 86400 / 3600), minutes = Math.floor(total % 3600 / 60), remainder = total % 60;
        const pad = value => String(value).padStart(2, '0');
        if (clock) return days ? `${days} д ${pad(hours)}:${pad(minutes)}:${pad(remainder)}` : hours ? `${hours}:${pad(minutes)}:${pad(remainder)}` : `${minutes}:${pad(remainder)}`;
        const parts = [];
        for (const [size, unit] of [[86400, 'д'], [3600, 'ч'], [60, 'мин'], [1, 'с']]) {
            const count = Math.floor(total / size); total %= size;
            if (count) parts.push(`${count} ${unit}`);
        }
        return parts.join(' ') || '0 с';
    }

    // Монитор опрашивает сервер только когда его окно и вкладка видимы.
    const resources = makeTool('resources', 'Монитор ресурсов', 'Нагрузка компьютера, на котором запущен DNO Tool.', 'СЕРВЕР');
    const resourceStats = el('div', 'tool-stats');
    const cpu = stat('CPU', '—', 'загрузка сервера'), ram = stat('RAM', '—', 'использовано / всего'), rss = stat('DNO Tool', '—', 'память RSS'), uptime = stat('Работает', '—', 'веб-сервер');
    resourceStats.append(cpu.node, ram.node, rss.node, uptime.node);
    const poolTable = table(['Общий пул', 'Работает', 'В очереди', 'Лимит']);
    const taskTable = table(['Пул', 'Операция', 'Состояние', 'Длительность']);
    const resourceMeta = el('p', 'tool-note');
    const resourceThumbs = el('div', 'tool-card');
    resourceThumbs.append(el('h3', '', 'Миниатюры'));
    const resourceThumbText = el('p', 'tool-note'); resourceThumbs.append(resourceThumbText);
    const resourceVnc = el('p', 'tool-note'); resourceThumbs.append(resourceVnc);
    const resourceBudget = el('p', 'tool-status error'); resourceBudget.hidden = true; resourceThumbs.append(resourceBudget);
    const resourceVeyon = el('div', 'tool-card'); resourceVeyon.hidden = true;
    resourceVeyon.append(el('h3', '', 'Veyon WebAPI'));
    const resourceVeyonText = el('p', 'tool-note'), resourceVeyonHint = el('p', 'tool-note');
    const resourceVeyonRestart = action('Перезапустить сервис Veyon', restartVeyon); resourceVeyonRestart.disabled = true;
    resourceVeyon.append(resourceVeyonText, resourceVeyonRestart, resourceVeyonHint);
    resources.content.append(resourceStats, resources.status, poolTable.wrap, resourceThumbs, resourceVeyon, el('h3', 'tool-note', 'Текущие операции'), taskTable.wrap, resourceMeta);
    const veyonStates = { running: 'работает', stopped: 'остановлен', cli_missing: 'CLI не найден', unknown: 'нет данных' };
    const number = value => value ?? '—';
    let veyonRestarting = false;
    function showVeyon(veyon) {
        resourceVeyon.hidden = !veyon;
        if (!veyon) return;
        const rss = veyon.rss_mb == null ? '—' : `${Number(veyon.rss_mb).toFixed(0)} МиБ`;
        resourceVeyonText.textContent = `Сервис: ${veyonStates[veyon.status] || 'нет данных'} · подключений: ${number(veyon.connections)} / ${number(veyon.limit)} · RSS: ${rss} · TCP: ${number(veyon.webapi_established)} · ожидают закрытия: ${number(veyon.webapi_close_wait)}`;
        resourceVeyonRestart.disabled = veyonRestarting || !veyon.can_restart;
        resourceVeyonHint.textContent = veyon.can_restart ? 'Память сервиса освобождается только перезапуском.' : 'Приложение запущено без прав администратора: запрос прав появился бы на экране сервера. Перезапустите сервис в самом приложении.';
    }
    async function restartVeyon() {
        if (veyonRestarting || !window.confirm('Перезапустить сервис Veyon? Миниатюры и сеансы связи прервутся и восстановятся через 5–10 секунд.')) return;
        veyonRestarting = true; resourceVeyonRestart.disabled = true; resourceVeyonRestart.textContent = 'Перезапуск…';
        try { await json('/api/tools/veyon/restart', {}); resources.message('Сервис Veyon перезапущен'); }
        catch (e) { resources.message(e.message, true); }
        finally { veyonRestarting = false; resourceVeyonRestart.textContent = 'Перезапустить сервис Veyon'; resourceVeyonRestart.disabled = false; }
    }
    let resourceTimer, resourceController, resourceBusy = false, resourceGeneration = 0;
    const memory = value => `${(Number(value || 0) / 1048576).toFixed(0)} МиБ`;
    async function refreshResources() {
        if (resourceBusy || !D.visible('resources') || !A.authenticated()) return;
        clearTimeout(resourceTimer); resourceBusy = true;
        resourceController = new AbortController(); const generation = resourceGeneration;
        try {
            const data = await json('/api/tools/resources', null, resourceController.signal);
            if (generation !== resourceGeneration) return;
            cpu.number.textContent = `${data.cpu_percent.toFixed(0)}%`;
            ram.number.textContent = `${data.memory.percent.toFixed(0)}%`;
            ram.node.lastChild.textContent = `${memory(data.memory.used)} / ${memory(data.memory.total)}`;
            rss.number.textContent = memory(data.process.rss);
            uptime.number.textContent = formatDuration(data.uptime_sec, { milliseconds: false });
            poolTable.body.replaceChildren();
            data.pools.forEach(pool => row(poolTable.body, [pool.label, pool.active, pool.pending, pool.max_workers]));
            taskTable.body.replaceChildren();
            (data.tasks || []).forEach(task => row(taskTable.body, [task.pool, task.operation, task.state === 'running' ? 'Выполняется' : 'В очереди', formatDuration(task.duration)]));
            resourceMeta.textContent = `PID ${data.process.pid} · потоков процесса: ${data.process.threads} · CPU: ${data.cpu_count ?? '—'} · обновление каждые 3 с`;
            const t = data.thumbnails;
            resourceThumbText.textContent = Object.keys(t).length ? `Захватов: ${t.captures ?? 0} · из кэша: ${t.cache_hits ?? 0} · занято: ${t.busy ?? 0} · вытеснений: ${t.evictions ?? 0} · кэш: ${memory(t.cache_bytes)} / ${memory(t.cache_bytes_limit)} · сессий: ${t.sessions ?? 0} / ${t.max_session_records ?? '—'} · подписок: ${t.subscriptions ?? 0}` : 'Сервис миниатюр не подключён';
            const vnc = t.backends?.vnc;
            resourceVnc.textContent = vnc ? `VNC: сеансов ${vnc.sessions ?? 0} / ${vnc.max_sessions ?? '—'} · память ${memory(vnc.memory_reserved_bytes)} / ${memory(vnc.max_memory_bytes)} · площадь экрана до ${vnc.max_screen_pixels ? (vnc.max_screen_pixels / 1e6).toLocaleString('ru-RU', { maximumFractionDigits: 1 }) : '—'} МП` : '';
            const blocked = t.allocations_blocked || vnc?.allocations_blocked, over = t.over_budget || vnc?.over_budget;
            resourceBudget.hidden = !over && !blocked;
            resourceBudget.textContent = blocked ? 'Превышены лимиты: новые выделения заблокированы' : over ? 'Превышены лимиты миниатюр' : '';
            showVeyon(data.veyon);
            resources.message(`Обновлено в ${new Date(data.timestamp * 1000).toLocaleTimeString('ru-RU')}`);
        } catch (e) { if (e.name !== 'AbortError') resources.message(e.message, true); }
        finally {
            resourceBusy = false;
            if (D.visible('resources') && A.authenticated()) resourceTimer = setTimeout(refreshResources, 3000);
        }
    }
    resources.refresh = refreshResources;
    resources.stop = () => { ++resourceGeneration; clearTimeout(resourceTimer); resourceController?.abort(); };
    resources.clear = () => { [cpu, ram, rss, uptime].forEach(item => { item.number.textContent = '—'; }); poolTable.body.replaceChildren(); taskTable.body.replaceChildren(); resourceMeta.textContent = ''; resourceThumbText.textContent = ''; resourceVnc.textContent = ''; resourceBudget.hidden = true; resourceVeyon.hidden = true; };

    function activate(tool) {
        if (A.authenticated() && D.visible(tool.id)) tool.refresh();
        else { tool.stop(); if (!A.authenticated()) tool.message('Войдите в систему через кнопку в верхней панели'); }
    }
    window.addEventListener('dno:window', event => {
        const tool = tools.get(event.detail.id);
        if (tool) activate(tool);
    });
    window.addEventListener('dno:auth', event => {
        tools.forEach(tool => {
            tool.stop();
            if (!event.detail.authenticated) tool.clear();
            activate(tool);
        });
    });
    window.addEventListener('pagehide', () => tools.forEach(tool => tool.stop()));
    window.DNODesktopTools = { makeTool, field, select, action, iconAction, table, row, stat, download, formatDuration, json, request, activate, tools, el, errorMessage };
    tools.forEach(activate);
})();
