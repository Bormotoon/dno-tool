// Видеостена ЕЦХД и команды общего менеджера записи приложения.
(() => {
    'use strict';
    const T = window.DNODesktopTools, D = window.DNODesktop;
    const { el, action, field, table, row, json, request, makeTool } = T;
    const tool = makeTool('cameras', 'Камеры ЕЦХД', 'Просмотр камер из каталога основного приложения.', 'ПРОСМОТР');
    const toolbar = el('div', 'camera-toolbar');
    function fullscreenIcon(exit = false) {
        const glyph = el('span', 'camera-fullscreen-glyph');
        const path = exit ? 'M8 3v5H3m18 0h-5V3M3 16h5v5m8 0v-5h5' : 'M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5';
        glyph.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${path}"/></svg>`;
        return glyph;
    }
    const modes = [[0, 'Список', 'list'], [1, '1', '1'], [4, '4', '4'], [6, '6', '6'], [7, '6 · фокус', '6_focus'], [9, '9', '9'], [16, '16', '16']];
    const modeButtons = new Map();
    modes.forEach(([count, label, key]) => {
        const button = action(label, () => setMode(count)); button.prepend(D.icon(`camera_view_${key}`));
        button.setAttribute('aria-label', count ? `Видеостена ${label}` : 'Список камер'); modeButtons.set(count, button); toolbar.append(button);
    });
    const fullscreenButton = action('', enterFullscreen);
    fullscreenButton.prepend(fullscreenIcon()); fullscreenButton.setAttribute('aria-label', 'Открыть текущую видеостену на весь экран'); fullscreenButton.setAttribute('aria-pressed', 'false'); fullscreenButton.disabled = true;
    fullscreenButton.title = 'На весь экран'; fullscreenButton.classList.add('camera-icon-button');
    const recordButton = action('', () => recordingAction('start'));
    recordButton.append(D.icon('menu_camera_record')); recordButton.title = 'Записать камеры текущей страницы'; recordButton.setAttribute('aria-label', recordButton.title); recordButton.classList.add('camera-icon-button'); recordButton.disabled = true;
    const stopButton = action('', () => recordingAction('stop_all'));
    stopButton.append(D.icon('menu_camera_stop')); stopButton.title = 'Остановить все записи камер'; stopButton.setAttribute('aria-label', stopButton.title); stopButton.classList.add('camera-icon-button'); stopButton.disabled = true;
    toolbar.append(fullscreenButton, recordButton, stopButton);
    const workspace = el('div', 'camera-workspace');
    const list = el('div', 'camera-tree'), wall = el('div', 'camera-wall'); workspace.append(list, wall);
    const fullscreenExit = action('', exitFullscreen); fullscreenExit.append(fullscreenIcon(true)); fullscreenExit.title = 'Выйти из полного экрана · Esc'; fullscreenExit.setAttribute('aria-label', fullscreenExit.title); fullscreenExit.classList.add('camera-fullscreen-exit', 'camera-icon-button'); fullscreenExit.hidden = true; workspace.append(fullscreenExit);
    const picker = el('dialog', 'camera-picker'); picker.setAttribute('aria-modal', 'true'); picker.setAttribute('aria-labelledby', 'cameraPickerTitle');
    const pickerHeading = el('h3', '', 'Добавить камеру'); pickerHeading.id = 'cameraPickerTitle';
    const pickerSearch = field('Найти камеру', '', 'search', { placeholder: 'Адрес, кабинет, название или IP' });
    const pickerOptions = el('div', 'camera-picker-options');
    picker.append(pickerHeading, pickerSearch.wrap, pickerOptions, action('Отмена', () => closePicker())); workspace.append(picker);
    picker.addEventListener('cancel', event => { event.preventDefault(); closePicker(); });
    pickerSearch.input.addEventListener('input', renderPicker);
    const pagination = el('div', 'tool-toolbar camera-pagination');
    const previous = action('←', () => { --page; render(); persist(); }), next = action('→', () => { ++page; render(); persist(); }), pageInfo = el('span', 'tool-note'); pagination.append(previous, pageInfo, next);
    previous.setAttribute('aria-label', 'Предыдущая страница камер'); previous.title = 'Предыдущая страница';
    next.setAttribute('aria-label', 'Следующая страница камер'); next.title = 'Следующая страница';
    workspace.append(pagination);
    tool.content.replaceChildren(toolbar, tool.status, workspace);
    tool.status.hidden = true;
    let noticeTimer;
    tool.message = (text, error = false) => {
        clearTimeout(noticeTimer); tool.status.textContent = text ? T.errorMessage(text) : ''; tool.status.classList.toggle('error', error); tool.status.hidden = !text;
        if (text) noticeTimer = setTimeout(() => { tool.status.hidden = true; }, error ? 10000 : 4000);
    };
    let cameras = [], board = [], boardConfigured = true, mode = 4, page = 0;
    let catalogController, loaded = false, loading = false, visible = false, generation = 0, pickerSlot = null, pickerFocus = null;
    const streams = new Map();
    let retryTimer, actionsTile = null, lastPointerType = '';
    let recordingState = { ready: false, records: [] }, recordingTimer, recordingController, recordingBusy = false, recordingRevision = 0;
    function closeTileActions() { actionsTile?.classList.remove('actions-open'); actionsTile = null; }
    wall.addEventListener('pointerdown', event => { lastPointerType = event.pointerType || ''; });
    wall.addEventListener('click', event => {
        if (event.target.closest('button, dialog, input')) return;
        if (!['touch', 'pen'].includes(lastPointerType) && !window.matchMedia('(hover: none)').matches) return;
        const tile = event.target.closest('[data-camera-iid]');
        if (!tile) return;
        const wasOpen = actionsTile === tile; closeTileActions();
        if (!wasOpen) { tile.classList.add('actions-open'); actionsTile = tile; }
    });
    document.addEventListener('pointerdown', event => { if (actionsTile && !actionsTile.contains(event.target)) closeTileActions(); });
    function cameraError(error, context = 'video') {
        if (error.status === 401) return 'Сеанс завершён. Войдите в систему заново';
        if (error.status === 403) return 'Нет доступа к камерам';
        if (error.status === 404) return 'Камера недоступна. Обновите каталог камер';
        if (navigator.onLine === false) return 'Нет подключения к сети. Ожидаем восстановления связи…';
        if ([409, 429, 503].includes(error.status)) return 'Сервис камер занят. Повторяем подключение…';
        if (context === 'catalog') return 'Не удалось загрузить каталог камер. Проверьте подключение';
        if (context === 'recording') return 'Не удалось связаться с менеджером записи. Проверьте состояние записи перед повтором';
        return 'Связь с видеостеной потеряна. Подключаемся повторно…';
    }
    let fullscreenPending = false, wasFullscreen = false;
    function isFullscreen() { return document.fullscreenElement === workspace || document.body.classList.contains('camera-cinema'); }
    function syncFullscreen() {
        window.dispatchEvent(new CustomEvent('dno:camera-fullscreen'));
        const active = isFullscreen();
        fullscreenExit.hidden = !active;
        fullscreenButton.setAttribute('aria-pressed', String(active));
        if (active && !wasFullscreen) fullscreenExit.focus({ preventScroll: true });
        else if (!active && wasFullscreen && visible) fullscreenButton.focus({ preventScroll: true });
        wasFullscreen = active;
    }
    async function enterFullscreen() {
        if (fullscreenPending || fullscreenButton.disabled || isFullscreen()) return;
        fullscreenPending = true;
        try {
            if (workspace.requestFullscreen) {
                try { await workspace.requestFullscreen(); }
                catch { if (visible) document.body.classList.add('camera-cinema'); }
            } else document.body.classList.add('camera-cinema');
            // Закрытие инструмента во время запроса не должно оставить пустой полный экран.
            if (!visible) await exitFullscreen();
            syncFullscreen();
        } finally { fullscreenPending = false; }
    }
    async function exitFullscreen() {
        document.body.classList.remove('camera-cinema');
        if (document.fullscreenElement === workspace) {
            try { await document.exitFullscreen(); }
            catch { tool.message('Не удалось выйти из полного экрана. Нажмите Escape', true); }
        }
        syncFullscreen();
    }
    document.addEventListener('fullscreenchange', syncFullscreen);
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && actionsTile) {
            event.preventDefault(); event.stopPropagation(); closeTileActions();
            (isFullscreen() ? fullscreenExit : fullscreenButton).focus({ preventScroll: true }); return;
        }
        if (event.key === 'Escape' && picker.open) {
            event.preventDefault(); event.stopPropagation(); closePicker(); return;
        }
        if (event.key === 'Escape' && isFullscreen()) {
            event.preventDefault(); event.stopPropagation(); exitFullscreen();
        }
    }, true);
    try {
        const saved = JSON.parse(localStorage.getItem('dno-camera-wall-v2') || '{}');
        mode = modes.some(([value]) => value === saved.mode) ? saved.mode : 4;
        board = Array.isArray(saved.board) ? saved.board.map(value => typeof value === 'string' ? value : '').slice(0, 256) : [];
        boardConfigured = saved.configured !== false;
        page = Number.isInteger(saved.page) && saved.page >= 0 ? Math.min(255, saved.page) : 0;
    } catch {}
    function persist() { try { localStorage.setItem('dno-camera-wall-v2', JSON.stringify({ mode, board, configured: boardConfigured, page })); } catch {} }
    function filtered(query = '') { const terms = query.toLocaleLowerCase().split(/\s+/).filter(Boolean); return cameras.filter(item => terms.every(term => `${item.address} ${item.cabinet} ${item.name} ${item.host}`.toLocaleLowerCase().includes(term))); }
    function boardItems() { return boardConfigured ? board.map(iid => cameras.find(item => item.iid === iid)) : cameras; }
    function count() { return mode === 7 ? 6 : mode; }
    function cameraCaption(camera) { return [camera.address, camera.cabinet, camera.name].filter(Boolean).join(' / '); }
    function visibleBoard() { return boardItems().slice(page * count(), (page + 1) * count()).filter(Boolean).map(item => item.iid); }
    function stopStreams() { clearTimeout(retryTimer); streams.forEach(controller => controller.abort()); streams.clear(); }
    function setMode(value) { const first = page * count(); mode = value; page = count() ? Math.floor(first / count()) : 0; render(); persist(); }
    function openCamera(camera) { board = [camera.iid, ...board.filter(iid => iid !== camera.iid)]; boardConfigured = true; page = 0; setMode(1); }
    function setSlot(index, iid) {
        if (!Number.isInteger(index) || index < 0 || index >= 256) return false;
        const items = boardItems();
        if (iid && items.some((item, position) => position !== index && item?.iid === iid)) {
            tool.message('Эта камера уже размещена в другой плитке', true); renderPicker(); return false;
        }
        if (items[index]?.iid === iid) return true;
        board = Array.from({ length: Math.max(items.length, index + 1) }, (_, position) => items[position]?.iid || '');
        board[index] = iid; boardConfigured = true; persist(); render();
        return true;
    }
    function addToBoard(camera) {
        if (!mode) mode = 4;
        const items = boardItems();
        const existing = items.findIndex(item => item?.iid === camera.iid);
        if (existing >= 0) { page = Math.floor(existing / count()); render(); persist(); return; }
        let index = items.findIndex(item => !item); if (index < 0) index = items.length;
        if (index >= 256) return tool.message('На доске уже 256 камер. Выберите «Заменить» в нужной плитке', true);
        page = Math.floor(index / count()); setSlot(index, camera.iid);
    }
    function openPicker(index) {
        closeTileActions();
        pickerSlot = index; pickerFocus = document.activeElement;
        const current = boardItems()[index]; pickerHeading.textContent = `${current ? 'Заменить камеру' : 'Добавить камеру'} · плитка ${index + 1}`;
        pickerSearch.input.value = ''; renderPicker();
        if (!picker.open) {
            try { picker.showModal?.(); } catch {}
            if (!picker.open) { picker.setAttribute('open', ''); picker.classList.add('camera-picker-fallback'); }
        }
        pickerSearch.input.focus({ preventScroll: true });
    }
    function closePicker(restoreFocus = true) {
        if (!picker.open) return;
        if (picker.close) { try { picker.close(); } catch {} }
        picker.removeAttribute('open'); picker.classList.remove('camera-picker-fallback');
        if (restoreFocus && visible) {
            const target = pickerFocus?.isConnected ? pickerFocus : wall.querySelector(`[data-camera-slot="${pickerSlot}"] button`);
            target?.focus({ preventScroll: true });
        }
        pickerSlot = null;
    }
    function renderPicker() {
        pickerOptions.replaceChildren();
        const occupied = new Set(boardItems().filter((item, index) => item && index !== pickerSlot).map(item => item.iid));
        const items = filtered(pickerSearch.input.value).filter(item => !occupied.has(item.iid));
        let group, path;
        for (const camera of items) {
            const caption = `${camera.address} / ${camera.cabinet}`;
            if (caption !== path) { path = caption; group = el('section', 'camera-picker-group'); group.append(el('h4', '', caption)); pickerOptions.append(group); }
            const button = action('', () => { if (pickerSlot !== null && setSlot(pickerSlot, camera.iid)) closePicker(); });
            button.classList.add('camera-picker-option'); button.setAttribute('aria-label', `Выбрать ${camera.name}, ${caption}`);
            const text = el('span'); text.append(el('strong', '', camera.name), el('small', '', camera.endpoint || camera.host)); button.append(D.icon('camera'), text); group.append(button);
        }
        if (!items.length) pickerOptions.append(el('p', 'tool-note', !cameras.length ? 'Каталог пуст. Добавьте камеры в основном приложении.' : occupied.size >= cameras.length ? 'Все камеры уже размещены в плитках. Освободите нужную камеру на её странице.' : 'Свободные камеры не найдены. Измените поиск.'));
    }
    function render() {
        closeTileActions();
        stopStreams(); ++generation;
        const plans = [];
        const items = boardItems();
        const pages = mode ? Math.max(1, Math.ceil(Math.min(256, Math.max(items.length, cameras.length)) / count())) : 1;
        page = Math.max(0, Math.min(page, pages - 1));
        modeButtons.forEach((button, value) => { button.classList.toggle('selected', value === mode); button.setAttribute('aria-pressed', String(value === mode)); });
        tool.root.classList.toggle('camera-board-mode', mode !== 0);
        list.hidden = mode !== 0; wall.hidden = mode === 0; pagination.hidden = mode === 0 || pages <= 1;
        fullscreenButton.disabled = mode === 0 || !visibleBoard().length;
        const columns = mode === 1 ? 1 : mode === 4 ? 2 : mode === 16 ? 4 : 3;
        workspace.style.setProperty('--camera-wall-columns', columns); workspace.style.setProperty('--camera-wall-rows', mode === 7 ? 3 : Math.max(1, Math.ceil(count() / columns)));
        list.replaceChildren(); wall.replaceChildren();
        if (mode === 0) {
            const groups = new Map();
            for (const camera of filtered()) { const key = `${camera.address}\0${camera.cabinet}`; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(camera); }
            let address, addressNode;
            for (const items of groups.values()) {
                const camera = items[0];
                if (address !== camera.address) { address = camera.address; addressNode = el('details', 'camera-address'); addressNode.open = true; const title = el('summary', '', address); title.prepend(D.icon('folder')); addressNode.append(title); list.append(addressNode); }
                const cabinet = el('details', 'camera-cabinet'); cabinet.open = true; const caption = el('summary', '', `${camera.cabinet} · ${items.length}`); caption.prepend(D.icon('folder')); cabinet.append(caption);
                const result = table(['Камера', 'Адрес RTSP', 'Просмотр']);
                for (const item of items) {
                    const tr = row(result.body, [item.name, item.endpoint || item.host, '']); tr.firstChild.prepend(D.icon('camera'));
                    tr.lastChild.append(action('Смотреть', () => openCamera(item)), action('Добавить в плитку', () => addToBoard(item))); tr.addEventListener('dblclick', () => openCamera(item));
                }
                cabinet.append(result.wrap); addressNode.append(cabinet);
            }
            if (!groups.size) list.append(el('p', 'empty-state', cameras.length ? 'Камеры не найдены' : 'Каталог пуст. Камеры добавляются в основном приложении.'));
        } else {
            pageInfo.textContent = `${page + 1} / ${pages}`; previous.disabled = page === 0; next.disabled = page >= pages - 1; wall.dataset.layout = mode;
            for (let position = 0; position < count(); ++position) {
                const index = page * count() + position, camera = items[index];
                const tile = el('section', 'camera-tile'); tile.dataset.cameraSlot = index;
                const preview = el('div', 'camera-live-frame');
                if (camera) {
                    tile.dataset.cameraIid = camera.iid;
                    tile.tabIndex = 0; tile.setAttribute('role', 'group'); tile.setAttribute('aria-label', cameraCaption(camera));
                    const img = el('img'); img.alt = camera.name; img.hidden = true;
                    const badge = el('span', 'camera-live-message live-badge camera-path-badge', cameraCaption(camera)); badge.title = cameraCaption(camera);
                    const state = el('span', 'camera-live-message camera-connection-state', 'Подключение…'); preview.append(img, badge, state); preview.addEventListener('dblclick', event => { if (!['touch', 'pen'].includes(lastPointerType) && !event.target.closest('button')) openCamera(camera); }); plans.push({ camera, img, state });
                    const actions = el('div', 'camera-tile-actions');
                    for (const [label, icon, cls, callback] of [['Заменить', 'camera', 'camera-tile-replace', () => openPicker(index)], ['Убрать из плитки', 'menu_delete', 'camera-tile-remove', () => setSlot(index, '')]]) {
                        const button = action('', callback); button.classList.add(cls); button.title = label; button.setAttribute('aria-label', label); button.append(D.icon(icon)); actions.append(button);
                    }
                    preview.append(actions);
                } else {
                    const add = action('', () => openPicker(index)); add.classList.add('camera-add-tile'); add.setAttribute('aria-label', `Добавить камеру в плитку ${index + 1}`);
                    add.disabled = index >= 256;
                    add.append(D.icon('camera'), el('strong', '', 'Добавить камеру'), el('span', '', 'Выбрать из каталога')); preview.append(add);
                }
                tile.append(preview); wall.append(tile);
            }
        }
        updateRecording(recordingState);
        if (visible && plans.length) live(plans, generation);
    }
    function updateRecording(data) {
        recordingState = data;
        recordButton.title = data.ready ? 'Записать камеры текущей страницы' : 'Подготовка менеджера записи';
        const current = new Set(visibleBoard());
        recordButton.disabled = recordingBusy || !mode || !data.ready || !data.records.some(item => current.has(item.iid) && item.online && !item.active);
        stopButton.disabled = recordingBusy || !data.records.some(item => item.active);
        recordButton.classList.toggle('recording', data.records.some(item => current.has(item.iid) && item.active));
        wall.querySelectorAll('[data-camera-iid]').forEach(tile => {
            const record = data.records.find(item => item.iid === tile.dataset.cameraIid);
            tile.classList.toggle('is-recording', !!record?.active);
            const badge = tile.querySelector('.camera-path-badge');
            const camera = cameras.find(item => item.iid === tile.dataset.cameraIid);
            if (badge && camera) badge.title = `${cameraCaption(camera)}${record?.active ? ` · Запись: ${T.formatDuration(record.elapsed || 0, { milliseconds: false })}` : ''}`;
        });
    }
    async function pollRecording() {
        clearTimeout(recordingTimer);
        if (!visible || recordingBusy) return;
        recordingController?.abort(); recordingController = new AbortController();
        const revision = ++recordingRevision;
        try {
            const data = await json('/api/tools/cameras/state', null, recordingController.signal);
            if (visible && revision === recordingRevision) updateRecording(data);
        } catch (error) {
            if (error.name !== 'AbortError' && visible && revision === recordingRevision) {
                updateRecording({ ready: false, records: [] }); recordButton.title = cameraError(error, 'recording');
            }
        } finally { if (visible && revision === recordingRevision) recordingTimer = setTimeout(pollRecording, 2000); }
    }
    async function recordingAction(action) {
        if (recordingBusy || !visible) return;
        const iids = action === 'start' ? [...new Set(visibleBoard())] : [];
        if (action === 'start' && !iids.length) return;
        clearTimeout(recordingTimer); recordingController?.abort(); ++recordingRevision;
        recordingBusy = true; updateRecording(recordingState);
        try {
            const data = await json('/api/tools/cameras/recording', { action, iids });
            if (!visible) return;
            updateRecording(data);
            const errors = (data.results || []).filter(item => !item.success);
            tool.message(errors.length ? errors.map(item => `${cameras.find(camera => camera.iid === item.iid)?.name || 'Камера'}: ${item.message}`).join('; ') : action === 'start' ? 'Запись запущена' : 'Остановка всех записей запрошена', !!errors.length);
        } catch (error) { if (visible && error.name !== 'AbortError') tool.message(cameraError(error, 'recording'), true); }
        finally { recordingBusy = false; updateRecording(recordingState); if (visible) pollRecording(); }
    }
    async function live(plans, mine, attempt = 0) {
        const controller = new AbortController(); streams.set('wall', controller);
        try {
            const response = await request('/api/tools/cameras/preview', { method: 'POST', body: JSON.stringify({ iids: [...new Set(plans.map(item => item.camera.iid))] }), signal: controller.signal });
            await T.events(response, (kind, data) => {
                if (mine !== generation) return;
                for (const { camera, img, state } of plans) {
                    if (data.iid !== camera.iid) continue;
                    if (kind === 'frame') { img.src = `data:image/jpeg;base64,${data.jpeg}`; img.hidden = false; state.hidden = true; }
                    if (kind === 'state') {
                        state.textContent = data.message === 'Прямой эфир' ? 'Подключение…' : T.errorMessage(data.message);
                        state.hidden = data.message === 'Прямой эфир' && !img.hidden;
                        if (data.message.includes('Нет связи')) { img.hidden = true; img.removeAttribute('src'); }
                    }
                }
            });
            if (visible && mine === generation && !controller.signal.aborted) throw new Error('Соединение с видеостеной прервано');
        } catch (error) {
            if (error.name !== 'AbortError' && mine === generation) {
                plans.forEach(({ state, img }) => { state.textContent = cameraError(error); state.hidden = false; img.hidden = true; });
                if (!error.status || [408, 409, 429, 500, 502, 503, 504].includes(error.status)) {
                    retryTimer = setTimeout(() => { if (visible && mine === generation && !controller.signal.aborted) live(plans, mine, attempt + 1); }, Math.min(15000, 1000 * 2 ** Math.min(attempt, 4)));
                }
            }
        }
    }
    async function loadCatalog(force = false) {
        if (loading || loaded && !force) return;
        loading = true; catalogController = new AbortController();
        try {
            const data = await json('/api/tools/cameras', null, catalogController.signal);
            cameras = data.cameras.sort((a, b) => `${a.address}\0${a.cabinet}\0${a.name}`.localeCompare(`${b.address}\0${b.cabinet}\0${b.name}`, 'ru'));
            const seen = new Set();
            board = board.map(iid => {
                if (!iid || seen.has(iid) || !cameras.some(item => item.iid === iid)) return '';
                seen.add(iid); return iid;
            });
            loaded = true; render(); persist();
        } catch (error) { if (error.name !== 'AbortError') tool.message(cameraError(error, 'catalog'), true); }
        finally { loading = false; }
    }
    tool.refresh = async () => { visible = true; const wasLoaded = loaded; await loadCatalog(); if (visible && wasLoaded) render(); if (visible) pollRecording(); };
    tool.stop = () => { visible = false; closeTileActions(); closePicker(false); exitFullscreen(); ++generation; ++recordingRevision; clearTimeout(recordingTimer); clearTimeout(noticeTimer); tool.status.hidden = true; recordingController?.abort(); catalogController?.abort(); stopStreams(); wall.querySelectorAll('img').forEach(img => img.removeAttribute('src')); };
    tool.clear = () => { loaded = false; cameras = []; updateRecording({ ready: false, records: [] }); list.replaceChildren(); wall.replaceChildren(); };
    T.activate(tool);
})();
