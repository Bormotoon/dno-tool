// Сканер использует общий с Qt движок; пингер показывает временной ряд ICMP.
(() => {
    'use strict';
    const T = window.DNODesktopTools, D = window.DNODesktop;
    const { el, field, select, action, table, row, stat, json, request, makeTool, errorMessage } = T;
    async function events(response, callback) {
        const reader = response.body.getReader(), decoder = new TextDecoder();
        let buffer = '';
        try {
            while (true) {
                const chunk = await reader.read();
                if (chunk.done) break;
                buffer += decoder.decode(chunk.value, { stream: true }).replace(/\r/g, '');
                if (buffer.length > 16 * 1024 * 1024) throw new Error('Слишком большой ответ сервера');
                let index;
                while ((index = buffer.indexOf('\n\n')) !== -1) {
                    const message = buffer.slice(0, index); buffer = buffer.slice(index + 2);
                    const lines = message.split('\n');
                    const kind = lines.find(line => line.startsWith('event:'))?.slice(6).trim() || 'message';
                    const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n');
                    if (data) callback(kind, JSON.parse(data));
                }
            }
        } catch (error) { throw window.DNOWebErrors.normalize(error); }
        finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
    }
    T.events = events;
    function check(label, checked = true) {
        const value = field(label, '', 'checkbox'); value.input.checked = checked; value.wrap.classList.add('tool-check'); return value;
    }
    function buttons(root, entries) { entries.forEach(([text, fn]) => root.append(action(text, fn))); }
    function csv(value) { return `"${String(value ?? '').replace(/"/g, '""')}"`; }

    const scanner = makeTool('scanner', 'Сканер хостов', 'Обнаружение устройств, определение имён, MAC-адресов и типов.');
    const target = field('Диапазон сети', '', 'text', { placeholder: 'IPv4, CIDR или 192.168.1.1-254', required: true });
    const mode = select('Тип сканирования', [['icmp', 'ICMP'], ['tcp', 'По открытым TCP-портам'], ['icmp_tcp', 'ICMP + TCP']]);
    const launch = el('form', 'tool-toolbar');
    const start = action('Сканировать', startScan, true), stop = action('Отменить', () => scanner.stop()); stop.disabled = true;
    start.prepend(D.icon('menu_scanner')); stop.prepend(D.icon('menu_abort'));
    launch.append(target.wrap, mode.wrap, start, stop);
    launch.addEventListener('submit', e => { e.preventDefault(); startScan(); });
    const options = el('div', 'scanner-options');
    const names = check('Имена (DNS / NetBIOS)'), mac = check('Определять MAC-адреса'), classify = check('Определять тип устройства');
    options.append(names.wrap, mac.wrap, classify.wrap);
    const advanced = el('details', 'tool-card tool-advanced'); advanced.append(el('summary', '', 'Дополнительно'));
    const grid = el('div', 'settings-grid');
    const threads = field('Потоки', '32', 'number', { min: '1', max: '128', required: true });
    const timeout = field('Таймаут, с', '1', 'number', { min: '.1', max: '10', step: '.1', required: true });
    const backend = select('Способ пинга', [['system', 'Системный ping'], ['icmplib', 'Библиотека icmplib']]);
    const ports = field('TCP-порты через запятую', '22,80,443', 'text');
    const signatures = check('Уточнять тип по открытым портам'), udp = check('UDP-пробы (NetBIOS / SNMP public)');
    grid.append(threads.wrap, timeout.wrap, backend.wrap, ports.wrap, signatures.wrap, udp.wrap); advanced.append(grid);
    classify.input.addEventListener('change', () => { signatures.input.disabled = !classify.input.checked; udp.input.disabled = !classify.input.checked; });
    const progress = el('progress', 'tool-progress'); progress.max = 1; progress.value = 0; progress.setAttribute('aria-label', 'Прогресс текущего этапа');
    const filter = field('Поиск в результатах', '', 'search', { placeholder: 'Имя, IP или MAC' });
    const type = select('Тип устройства', [['', 'Все типы']]);
    const online = check('Только найденные', true);
    const filters = el('div', 'tool-toolbar'); filters.append(filter.wrap, type.wrap, online.wrap);
    const resultsTable = table(['', 'Имя', 'IP-адрес', 'Порты', 'MAC-адрес', 'Тип', 'Состояние']); resultsTable.wrap.classList.add('scanner-results');
    resultsTable.body.parentElement.setAttribute('role', 'treegrid');
    resultsTable.body.parentElement.setAttribute('aria-label', 'Найденные хосты по подсетям');
    const pager = el('div', 'tool-toolbar');
    const previousPage = action('← Строки', () => { --resultPage; render(); }), nextPage = action('Строки →', () => { ++resultPage; render(); }), pageLabel = el('span', 'tool-note');
    pager.append(previousPage, pageLabel, nextPage);
    const toolbar = el('div', 'tool-toolbar');
    const folder = field('Папка в устройствах', 'Сканер', 'text', { maxLength: 128, placeholder: 'Новое или существующее имя папки' });
    const importButton = action('Добавить отмеченные', importSelected, true); importButton.prepend(D.icon('menu_import')); importButton.disabled = true;
    buttons(toolbar, [['Отметить все', () => mark(true)], ['Снять отметки', () => mark(false)], ['Очистить результаты', clear], ['Экспорт CSV', exportResults]]);
    toolbar.append(folder.wrap, importButton);
    const summary = el('p', 'tool-note');
    const importStatus = el('div', 'tool-status scanner-import-status', 'Можно ввести своё имя папки — она будет создана автоматически.'); importStatus.setAttribute('role', 'status'); importStatus.setAttribute('aria-live', 'polite');
    const reveal = action('Показать в устройствах', async () => {
        D.open('devices');
        try { if (importedIids.length) await window.DNODesktopAPI.revealHosts?.(importedIids); }
        catch (error) { importStatus.textContent = errorMessage(error); importStatus.classList.add('error'); }
    }); reveal.hidden = true; toolbar.append(reveal);
    scanner.content.append(launch, options, advanced, progress, scanner.status, filters, resultsTable.wrap, pager, summary, toolbar, importStatus);
    let controller, results = new Map(), checked = new Set(), scanId = null, loaded = false, generation = 0;
    let importBusy = false, importedIids = [];
    const collapsedSubnets = new Set();
    const subnetFor = host => host.split('.').slice(0, 3).join('.') + '.0/24';
    let resultPage = 0;
    function visibleResults() {
        const query = filter.input.value.toLocaleLowerCase();
        return [...results.values()].filter(item => (!online.input.checked || item.online) && (!type.input.value || item.device_type === type.input.value) && `${item.hostname} ${item.host} ${item.mac_address}`.toLocaleLowerCase().includes(query))
            .sort((a, b) => a.host.split('.').reduce((n, v) => n * 256 + Number(v), 0) - b.host.split('.').reduce((n, v) => n * 256 + Number(v), 0));
    }
    let renderTimer;
    function render() {
        clearTimeout(renderTimer); renderTimer = null;
        const entries = visibleResults();
        resultPage = Math.max(0, Math.min(resultPage, Math.ceil(entries.length / 500) - 1));
        previousPage.disabled = resultPage === 0; nextPage.disabled = (resultPage + 1) * 500 >= entries.length;
        pageLabel.textContent = entries.length ? `${resultPage * 500 + 1}–${Math.min(entries.length, (resultPage + 1) * 500)} из ${entries.length}` : 'Нет результатов';
        pager.hidden = entries.length <= 500;
        resultsTable.body.replaceChildren();
        const groups = new Map();
        for (const item of entries) {
            const key = subnetFor(item.host);
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(item);
        }
        let subnet;
        for (const item of entries.slice(resultPage * 500, (resultPage + 1) * 500)) {
            const network = subnetFor(item.host);
            if (network !== subnet) {
                subnet = network;
                const group = el('tr', 'scanner-network-group');
                group.dataset.scanSubnet = network; group.setAttribute('aria-level', '1'); group.setAttribute('aria-expanded', String(!collapsedSubnets.has(network)));
                const members = groups.get(network), candidates = members.filter(value => value.online);
                const selected = candidates.filter(value => checked.has(value.host)).length;
                const checkCell = el('td'), selectGroup = el('input'); selectGroup.type = 'checkbox';
                selectGroup.checked = !!candidates.length && selected === candidates.length; selectGroup.indeterminate = selected > 0 && selected < candidates.length;
                selectGroup.disabled = !!controller || importBusy || !candidates.length; selectGroup.setAttribute('aria-label', `Отметить подсеть ${network}`);
                selectGroup.addEventListener('change', () => { candidates.forEach(value => selectGroup.checked ? checked.add(value.host) : checked.delete(value.host)); render(); });
                checkCell.append(selectGroup);
                const cell = el('td'); cell.colSpan = 6;
                const toggle = action('', () => { if (collapsedSubnets.has(network)) collapsedSubnets.delete(network); else collapsedSubnets.add(network); render(); });
                toggle.classList.add('scanner-subnet-toggle'); toggle.setAttribute('aria-expanded', String(!collapsedSubnets.has(network)));
                toggle.append(el('span', 'scanner-caret', collapsedSubnets.has(network) ? '▸' : '▾'), D.icon('folder'), el('span', '', `${network} · ${members.length}`));
                cell.append(toggle); group.append(checkCell, cell); resultsTable.body.append(group);
            }
            const tr = row(resultsTable.body, ['', item.hostname || item.host, item.host, item.ports.join(', '), item.mac_address, item.device_type, item.online ? item.state === 'done' ? 'В сети' : 'Анализ…' : 'Нет ответа']);
            tr.dataset.scanHost = item.host;
            tr.dataset.scanParent = network; tr.setAttribute('aria-level', '2'); tr.hidden = collapsedSubnets.has(network);
            tr.children[1].classList.add('scanner-host-name');
            const hostContent = el('span', 'scanner-host-content');
            const iconKey = item.type_icon_key || item.icon_key || (item.state === 'scan' ? 'scan_ping' : item.state === 'scan2' ? 'scan_analyze' : item.online ? 'online' : 'offline');
            hostContent.append(D.icon(iconKey), el('span', '', item.hostname || item.host)); tr.children[1].replaceChildren(hostContent);
            const cb = el('input'); cb.type = 'checkbox'; cb.checked = checked.has(item.host); cb.disabled = !!controller || importBusy || !item.online;
            cb.setAttribute('aria-label', `Добавить ${item.host}`);
            cb.addEventListener('change', () => { if (cb.checked) checked.add(item.host); else checked.delete(item.host); render(); });
            tr.firstChild.append(cb);
            tr.children[5].title = item.type_reason || '';
            if (item.online) tr.lastChild.classList.add('good');
        }
        updateSummary(entries.length);
    }
    function updateSummary(count) {
        summary.textContent = `Показано: ${count} из ${results.size} · Найдено: ${[...results.values()].filter(item => item.online).length} · Отмечено: ${checked.size}`;
        importButton.disabled = importBusy || !!controller || !scanId || !checked.size;
    }
    function mark(value) { if (importBusy || controller) return; visibleResults().filter(item => item.online).forEach(item => value ? checked.add(item.host) : checked.delete(item.host)); render(); }
    function clear() { if (importBusy) return; if (controller) { scanner.stop(); return; } results.clear(); checked.clear(); scanId = null; render(); progress.value = 0; scanner.message('Результаты очищены'); }
    function exportResults() {
        const records = [['Имя', 'IP', 'Порты', 'MAC', 'Тип', 'Состояние'], ...visibleResults().map(item => [item.hostname, item.host, item.ports.join(', '), item.mac_address, item.device_type, item.online ? 'В сети' : 'Нет ответа'])];
        T.download('dno-hosts.csv', records.map(values => values.map(csv).join(';')).join('\r\n'), 'text/csv;charset=utf-8');
    }
    [filter.input, type.input, online.input].forEach(input => input.addEventListener('input', () => { resultPage = 0; render(); }));
    async function startScan() {
        if (importBusy || controller || !launch.reportValidity() || ![threads, timeout].every(item => item.input.reportValidity())) return;
        const numbers = ports.input.value.split(',').map(value => value.trim()).filter(Boolean);
        if (numbers.some(value => !/^\d+$/.test(value))) return scanner.message('Введите TCP-порты через запятую', true);
        controller = new AbortController(); const mine = ++generation;
        start.disabled = true; stop.disabled = false; results.clear(); checked.clear(); scanId = null; resultPage = 0; render();
        scanner.message('Запуск сканирования…');
        let finished = false;
        try {
            const response = await request('/api/tools/scanner/run', { method: 'POST', signal: controller.signal, body: JSON.stringify({ target: target.input.value.trim(), mode: mode.input.value, ports: numbers.map(Number), threads: Number(threads.input.value), timeout: Number(timeout.input.value), ping_backend: backend.input.value, resolve_names: names.input.checked, resolve_mac: mac.input.checked, classify_enabled: classify.input.checked, use_port_signatures: signatures.input.checked, udp_probes: udp.input.checked }) });
            await events(response, (kind, data) => {
                if (mine !== generation) return;
                if (kind === 'start') { scanId = data.scan_id; progress.max = data.total || 1; }
                if (kind === 'host') { results.set(data.host, data); if (!renderTimer) renderTimer = setTimeout(render, 150); }
                if (kind === 'phase') {
                    progress.max = Math.max(1, data.total); progress.value = data.done;
                    const phases = { ping: mode.input.value === 'tcp' ? 'TCP-порты' : 'ICMP', tcp_discovery: 'TCP после ICMP', names: 'Имена', ports: 'Пробы', finish: 'Анализ' };
                    scanner.message(`${phases[data.phase] || data.phase}: ${data.done}/${data.total} · Найдено: ${data.found}`);
                }
                if (kind === 'done') { finished = true; scanner.message(data.status === 'success' ? 'Сканирование завершено' : data.error || 'Сканирование отменено', data.status === 'error'); }
            });
            if (!finished && mine === generation) scanner.message('Соединение прервано. Полученные результаты сохранены', true);
            const kinds = [...new Set([...results.values()].map(item => item.device_type).filter(Boolean))].sort();
            type.input.replaceChildren();
            for (const value of ['', ...kinds]) { const option = el('option', '', value || 'Все типы'); option.value = value; type.input.append(option); }
        } catch (error) { if (mine === generation) scanner.message(error.name === 'AbortError' ? 'Сканирование отменено' : error.message, error.name !== 'AbortError'); }
        finally { controller = null; start.disabled = false; stop.disabled = true; render(); }
    }
    async function importSelected() {
        if (importBusy || controller || !scanId || !checked.size) return;
        const report = (text, error = false) => { importStatus.textContent = text; importStatus.classList.toggle('error', error); scanner.message(text, error); importStatus.scrollIntoView({ block: 'nearest' }); };
        if (checked.size > 4096) return report('За один импорт можно добавить до 4096 хостов. Уточните фильтр или импортируйте несколько групп', true);
        if (!folder.input.reportValidity()) return;
        const destination = folder.input.value.trim() || 'Сканер', mine = generation;
        importBusy = true; start.disabled = true; folder.input.disabled = true; reveal.hidden = true; render();
        report(`Добавление ${checked.size} хостов в папку «${destination}»…`);
        try {
            const data = await json('/api/tools/scanner/import', { scan_id: scanId, hosts: [...checked], folder: destination });
            if (mine !== generation) return;
            const message = data.message || `Добавлено: ${data.added || 0}, уже существуют: ${data.skipped || 0}`;
            report(message, !!data.errors?.length);
            window.DNODesktopAPI.notify?.({ title: 'Добавление хостов', text: message, kind: data.errors?.length ? 'error' : 'info', timeout: 10000 });
            const failed = new Set((data.errors || []).map(item => item.host));
            checked = new Set([...checked].filter(host => failed.has(host)));
            importedIids = [...(data.devices || []), ...(data.existing || [])].map(item => item.iid).filter(Boolean);
            reveal.hidden = !importedIids.length;
            if (await window.DNODesktopAPI.refreshHosts?.() === false) report(`${message}. Не удалось обновить список устройств; повторите обновление`, true);
        } catch (error) {
            if (mine === generation) {
                report(errorMessage(error), true);
                window.DNODesktopAPI.notify?.({ title: 'Хосты не добавлены', text: errorMessage(error), kind: 'error', timeout: 0 });
            }
        }
        finally { importBusy = false; start.disabled = false; folder.input.disabled = false; render(); }
    }
    scanner.refresh = async () => {
        if (loaded) return;
        try {
            const data = await json('/api/tools/scanner/config');
            threads.input.value = data.threads; timeout.input.value = data.timeout; backend.input.value = data.ping_backend;
            ports.input.value = Object.entries(data.ports).filter(([, enabled]) => enabled).map(([port]) => port).join(',');
            if (!target.input.value && typeof data.network === 'string') target.input.value = data.network;
            backend.input.querySelector('[value="icmplib"]').disabled = !data.icmplib_available;
            loaded = true; scanner.message('Выберите тип сканирования и диапазон. Все проверки выполняются с сервера');
        } catch (error) { scanner.message(error.message, true); }
    };
    scanner.stop = () => controller?.abort();
    scanner.clear = () => { ++generation; results.clear(); checked.clear(); scanId = null; loaded = false; render(); };

    const pinger = makeTool('pinger', 'Пингер', 'Непрерывный ICMP-опрос одного хоста с графиком и статистикой.', 'ICMP');
    const host = field('IP или имя хоста', '127.0.0.1', 'text', { required: true });
    const interval = field('Интервал, с', '1', 'number', { min: '.2', max: '60', step: '.1', required: true });
    const wait = field('Таймаут, с', '1', 'number', { min: '.1', max: '3', step: '.1', required: true });
    const pingStart = action('Старт', startPing, true), pause = action('Пауза', pausePing), pingStop = action('Стоп', () => pinger.stop()); pause.disabled = true; pingStop.disabled = true;
    pingStart.prepend(D.icon('menu_ping'));
    const controls = el('form', 'tool-toolbar'); controls.append(host.wrap, interval.wrap, wait.wrap, pingStart, pause, pingStop); controls.addEventListener('submit', e => { e.preventDefault(); startPing(); });
    const stats = el('div', 'tool-stats');
    const sent = stat('Отправлено'), loss = stat('Потери'), current = stat('Текущая', '—', 'мс'), avg = stat('Средняя', '—', 'мс'), range = stat('Min / Max', '—', 'мс'); stats.append(sent.node, loss.node, current.node, avg.node, range.node);
    const chart = el('div', 'ping-time-chart'); chart.setAttribute('role', 'img'); chart.setAttribute('aria-label', 'График задержки по времени в миллисекундах');
    const follow = check('Следить за графиком', true);
    const footer = el('div', 'tool-toolbar'); footer.append(follow.wrap, action('Очистить', clearPing), action('Экспорт CSV', () => T.download('dno-ping.csv', [['Время', 'Хост', 'Состояние', 'RTT, мс', 'TTL'], ...samples.map(item => [new Date(item.timestamp * 1000).toISOString(), host.input.value, item.state, item.rtt_ms, item.ttl])].map(values => values.map(csv).join(';')).join('\r\n'), 'text/csv;charset=utf-8')));
    pinger.content.append(controls, stats, chart, footer, pinger.status);
    let running = false, paused = false, pingController, timer, pingGeneration = 0, samples = [], total = 0, received = 0, sum = 0, measured = 0, minimum = Infinity, maximum = 0, started = 0;
    function clearPing() { samples = []; total = 0; received = 0; sum = 0; measured = 0; minimum = Infinity; maximum = 0; started = Date.now(); renderPing(); }
    function svgNode(tag, attrs, text) { const node = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value); if (text != null) node.textContent = text; return node; }
    function renderPing() {
        sent.number.textContent = total; loss.number.textContent = total ? `${((total - received) / total * 100).toFixed(1)}%` : '—';
        const last = samples.at(-1); current.number.textContent = last?.state === 'open' ? last.rtt_ms ?? '—' : '—';
        avg.number.textContent = measured ? (sum / measured).toFixed(1) : '—'; range.number.textContent = measured ? `${minimum.toFixed(1)} / ${maximum.toFixed(1)}` : '—';
        const visible = follow.input.checked && samples.length ? samples.filter(item => item.t >= samples.at(-1).t - 300) : samples;
        const start = visible.length ? visible[0].t : 0, end = Math.max(start + 10, visible.at(-1)?.t || 0), ceiling = Math.max(10, ...visible.map(item => item.rtt_ms || 0)) * 1.15;
        const svg = svgNode('svg', { viewBox: '0 0 900 260', preserveAspectRatio: 'none' });
        for (let i = 0; i <= 4; i++) {
            const y = 20 + i * 50;
            svg.append(svgNode('line', { x1: 52, y1: y, x2: 880, y2: y, class: 'ping-grid' }), svgNode('text', { x: 44, y: y + 4, 'text-anchor': 'end' }, Math.round(ceiling * (1 - i / 4))));
        }
        for (let i = 0; i <= 4; i++) svg.append(svgNode('text', { x: 52 + i * 207, y: 245, 'text-anchor': 'middle' }, T.formatDuration(Math.round(start + (end - start) * i / 4), { clock: true })));
        let segment = [];
        function flush() { if (segment.length) svg.append(svgNode('polyline', { points: segment.join(' '), class: 'ping-line' })); segment = []; }
        visible.forEach(item => {
            const x = 52 + (item.t - start) / (end - start) * 828;
            if (item.state !== 'open' || item.rtt_ms == null) { flush(); svg.append(svgNode('circle', { cx: x, cy: 220, r: 3, class: 'ping-loss' })); return; }
            const y = 220 - item.rtt_ms / ceiling * 200;
            segment.push(`${x},${y}`); svg.append(svgNode('circle', { cx: x, cy: y, r: 2.5, class: 'ping-point' }));
        }); flush(); chart.replaceChildren(svg);
    }
    follow.input.addEventListener('change', renderPing);
    function startPing() {
        if (running || !controls.reportValidity()) return;
        clearPing(); running = true; paused = false; ++pingGeneration;
        host.input.disabled = true; interval.input.disabled = true; wait.input.disabled = true;
        pingStart.disabled = true; pause.disabled = false; pingStop.disabled = false; pause.textContent = 'Пауза';
        tick(pingGeneration);
    }
    function pausePing() {
        if (!running) return; paused = !paused; ++pingGeneration; clearTimeout(timer); pingController?.abort(); pause.textContent = paused ? 'Продолжить' : 'Пауза';
        if (paused) pinger.message('Опрос на паузе. Данные сохранены'); else tick(pingGeneration);
    }
    async function tick(mine) {
        if (!running || paused || mine !== pingGeneration) return;
        pingController = new AbortController(); const began = Date.now();
        try {
            const data = await json('/api/tools/ping', { target: host.input.value.trim(), timeout: Number(wait.input.value) }, pingController.signal);
            if (!running || paused || mine !== pingGeneration) return;
            if (data.state === 'error') throw new Error(data.diagnostic || 'Ошибка выполнения ping');
            ++total;
            if (data.state === 'open') { ++received; if (data.rtt_ms != null) { ++measured; sum += data.rtt_ms; minimum = Math.min(minimum, data.rtt_ms); maximum = Math.max(maximum, data.rtt_ms); } }
            data.t = (Date.now() - started) / 1000; samples.push(data); if (samples.length > 10000) samples.shift();
            renderPing(); pinger.message(`${host.input.value} · ответов ${received}/${total} · интервал ${interval.input.value} с`);
        } catch (error) { if (error.name !== 'AbortError' && mine === pingGeneration) { pinger.stop(); pinger.message(error.message, true); } }
        finally { if (running && !paused && mine === pingGeneration) timer = setTimeout(() => tick(mine), Math.max(0, Number(interval.input.value) * 1000 - (Date.now() - began))); }
    }
    pinger.stop = () => { running = false; paused = false; ++pingGeneration; clearTimeout(timer); pingController?.abort(); [host, interval, wait].forEach(item => { item.input.disabled = false; }); pingStart.disabled = false; pause.disabled = true; pingStop.disabled = true; pinger.message(`Опрос остановлен · ${total} проб`); };
    pinger.refresh = () => { if (!running) pinger.message('Введите IP или имя хоста и нажмите «Старт»'); };
    pinger.clear = clearPing;
    clearPing();
    T.activate(scanner); T.activate(pinger);
})();
