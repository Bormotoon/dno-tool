// DNO Tool — веб-управление
// Копирует базовый интерфейс десктоп-приложения: дерево хостов слева,
// выбор задачи из дерева и выполнение с потоковым выводом результатов справа.

(() => {
    'use strict';
    const errorMessage = window.DNOWebErrors.message;

    // ===== Состояние =====
    const state = {
        creds: null,           // {username, password} или null
        token: null,           // base64(user:pass)
        sessionId: null,       // session_id от сервера (изоляция вкладок/заданий)
        multiUser: false,      // режим multi-user (из /api/version)
        hostsTree: [],
        // Карта устройств по iid: Map<iid, {name, host, folder}>
        // host — сетевой адрес (для пинга/иконок/миниатюр), iid — первичный ключ.
        hostsByIid: new Map(),
        groupByFolder: false, // группировать выбранные устройства по папкам
        commandsTree: [],
        tasksFlat: [],         // [{iid, name, path, task}]
        selectedHosts: new Set(),  // Set<iid> — выбранные устройства по iid
        selectedTask: null,    // {iid, name, path, task} — выбранная задача
        ws: null,
        running: false,
        results: new Map(),    // iid -> result data
        // JobManager-связанное:
        currentJobId: null,    // задание, к которому подключён основной WS (для запуска)
        currentJobSeq: 0,      // последний полученный seq по основному WS
        activeJobs: [],        // активные задания сессии (из /api/jobs/active + WS-событий)
        recentJobs: [],        // завершённые задания (из /api/jobs/recent, для replay после refresh)
        viewingJobId: null,    // задание, чьи результаты сейчас отображаются в #resultsArea
        // jobBuffer: { device_iids: [], results: Map(iid -> {status,label,output}), summary, status, task_name }
        jobBuffers: new Map(), // job_id -> буфер задания
        jobWs: new Map(),      // job_id -> WebSocket (viewer-WS, отдельный на каждое просматриваемое задание)
        jobTtlMinutes: 30,     // серверный TTL завершённых заданий (из /api/version; 0 = ∞)
        viewMode: 'list',      // 'list' | 'icons' | 'thumbs'
        onlineOnly: false,    // показывать только онлайн-устройства (как тумблер Qt)
        thumbInterval: 5,      // сек автообновления миниатюр
        thumbnailWidth: null, // preferred card width from the serving instance
        deviceSize: null, // Локальный размер карточек; null использует исходный размер режима.
        deviceSizeTimer: null,
        thumbnailScale: 1,
        thumbnailMaxDimension: 2048,
        thumbnailGeneration: {}, // iid -> current request/invalidation generation
        thumbTimer: null,
        statusTimer: null,      // таймер автообновления иконок статусов
        probeAbort: null,      // AbortController текущего SSE-зонда (статус/миниатюры);
                                  // при новом вызове предыдущий отменяется, освобождая
                                  // HTTP-соединение для параллельных команд.
        veyonAvailable: false,
        thumbnailsAvailable: false,
        featuresAvailable: false,
        powerAvailable: true,
        rfbBridgeAvailable: false,
        veyonServerPort: 11100,
        veyonServiceStats: null, // {pid, rss_mb, commit_mb, established, close_wait} — память veyon-service
        threadCount: 8,         // лимит потоков сервера для миниатюр и статусов (из /api/version)
        commandThreadCount: 8,  // лимит потоков сервера для выполнения команд
        hostStatuses: {},        // кэш последних известных статусов iid -> bool
        svgCache: { online: null, offline: null, default: null, veyon_down: null }, // data-URL'ы иконок
        thumbnailTheme: null,
        hostBackendOk: {}, // iid -> пригодность выбранного источника
        hostThumbnailStatus: {},
        hostThumbnailBackend: {},
        veyonIconCache: {}, // data-URL'ы иконок Veyon-фич (lock, unlock, ...)
        wasDown: false,          // сервер был признан недоступным (для reinit при восстановлении)
        _hostsSaveTimer: null,  // debounce таймер сохранения selectedHosts
        hostInfoCache: new Map(), // iid -> {ts, tip} — кэш live-данных тултипа (TTL 60с)
        hostInfoInflight: new Set(), // iid — запросы /api/host_info в полёте
    };
    // ===== Heartbeat / обнаружение падения сервера =====
    let _hbTimer = null;
    let _hbFailCount = 0;
    let _hbBackoff = 10000;
    const HB_INTERVAL = 10000;       // нормальный интервал (сервер жив)
    const HB_FAIL_THRESHOLD = 2;     // 2 подряд неудачи → тост «сервер недоступен»
    const HB_BACKOFF_BASE = 10000;    // старт экспоненты
    const HB_BACKOFF_MAX = 60000;     // потолок 60с
    let _serverDownToast = null;

    function startHeartbeat() {
        stopHeartbeat();
        _hbFailCount = 0;
        _hbBackoff = HB_BACKOFF_BASE;
        const tick = async () => {
            _hbTimer = null;
            try {
                await api('/api/version');
                _hbFailCount = 0;
                _hbBackoff = HB_BACKOFF_BASE;
                if (state.wasDown) {
                    state.wasDown = false;
                    hideServerDownToast();
                    await reinitAfterReconnect();
                    showToast({ title: 'Соединение восстановлено', kind: 'success', timeout: 3000 });
                }
            } catch (e) {
                if (e && e.message === 'Требуется аутентификация') {
                    // 401 — не падение сервера, auth слетел. Обычный интервал.
                    _hbTimer = setTimeout(tick, HB_INTERVAL);
                    return;
                }
                _hbFailCount++;
                if (_hbFailCount >= HB_FAIL_THRESHOLD) {
                    state.wasDown = true;
                    showServerDownToast();
                }
            }
            // Планируем следующий тик в зависимости от состояния.
            let delay;
            if (!state.wasDown) {
                delay = HB_INTERVAL;            // сервер жив — обычный интервал
            } else {
                // Backoff: 10с → 20с → 40с → 60с (потолок). Не спам запросами.
                delay = Math.min(_hbBackoff, HB_BACKOFF_MAX);
                _hbBackoff = Math.min(_hbBackoff * 2, HB_BACKOFF_MAX);
            }
            _hbTimer = setTimeout(tick, delay);
        };
        _hbTimer = setTimeout(tick, HB_INTERVAL);
    }

    function stopHeartbeat() {
        if (_hbTimer) { clearTimeout(_hbTimer); _hbTimer = null; }
        _hbFailCount = 0;
        _hbBackoff = HB_BACKOFF_BASE;
    }

    async function reinitAfterReconnect() {
        try {
            await Promise.all([loadVersion(), loadVeyonStatus(), loadHosts(), loadCommands(), loadIconSet(), loadVeyonIconSet()]);
            renderSelectedList();
        } catch (_) { /* частичные ошибки проигнорируем — heartbeat попробует ещё раз */ }
    }

    async function retryConnection() {
        try {
            await api('/api/version');
            _hbFailCount = 0;
            state.wasDown = false;
            hideServerDownToast();
            await reinitAfterReconnect();
            showToast({ title: 'Соединение восстановлено', kind: 'success', timeout: 3000 });
        } catch (_) {
            showToast({ title: 'Сервер всё ещё недоступен', kind: 'error', timeout: 2500 });
        }
    }

    function showServerDownToast() {
        if (_serverDownToast) return;
        // Сбросить иконки статусов и миниатюры в дефолтный (серый) вид —
        // сервер недоступен, последние известные статусы устарели.
        resetStatusIconsToDefault();
        _serverDownToast = showToast({
            title: 'Сервер недоступен',
            text: 'Веб-сервер не отвечает. Попробуйте переподключиться.',
            kind: 'error',
            action: retryConnection,
            actionLabel: 'Повторить',
            timeout: 0,
        });
    }

    // Сбросить иконки статусов и миниатюры в дефолтный вид (default.svg).
    // Вызывается при признании сервера недоступным — последние известные
    // статусы устарели, показываем серый placeholder. 0 сетевых запросов:
    // SVG берётся из state.svgCache (data-URL из памяти).
    function resetStatusIconsToDefault() {
        for (const iid of state.selectedHosts) nextThumbnailGeneration(iid);
        state.hostStatuses = {};
        state.hostBackendOk = {};
        state.hostThumbnailStatus = {};
        state.hostThumbnailBackend = {};
        if (state.onlineOnly) {
            // Статусы сброшены - при фильтре все карточки скрываются.
            // Только пересборка вида (без рестарта зонда: сервер недоступен).
            renderSelectedListView();
        }
        const q = tokenQuery();
        const defaultCached = state.svgCache.default;
        // Иконки статусов
        const imgs = $('selectedList').querySelectorAll('.device-item img.status-icon-img');
        imgs.forEach(img => {
            if (defaultCached) {
                img.src = defaultCached;
            } else if (state.token) {
                const iid = img.closest('.device-item')?.dataset.iid;
                if (iid) img.src = `/api/icon/${encodeURIComponent(iid)}?${q}&s=default&t=${Date.now()}`;
            }
        });
        // Левое дерево - все хосты в default (как сброс в Qt).
        const treeImgs = $('hostTree').querySelectorAll('.tree-item.device img.status-icon-img');
        treeImgs.forEach(img => {
            if (defaultCached) {
                img.src = defaultCached;
            } else if (state.token) {
                const iid = img.closest('.tree-item.device')?.dataset.iid;
                if (iid) img.src = `/api/icon/${encodeURIComponent(iid)}?${q}&s=default&t=${Date.now()}`;
            }
        });
        // Миниатюры — placeholder default, отозвать предыдущие blob-URL.
        const thumbs = $('selectedList').querySelectorAll('.device-item .thumb-wrap img.thumb-img');
        thumbs.forEach(img => {
            setThumbnailPlaceholder(img, img.dataset.iid, 'default');
        });
    }

    function hideServerDownToast() {
        if (_serverDownToast) { _serverDownToast.close(); _serverDownToast = null; }
    }

    // ===== Тосты =====
    function showToast({ title, text = '', kind = 'info', action = null, actionLabel = 'OK', timeout = 0 }) {
        title = title ? errorMessage(title) : '';
        text = text ? errorMessage(text) : '';
        const container = $('toastContainer');
        if (!container) return null;
        const node = el('div', `toast ${kind}`);
        const body = el('div', 'toast-body');
        if (title) body.appendChild(el('div', 'toast-title', title));
        if (text) body.appendChild(el('div', 'toast-text', text));
        if (action) {
            const actions = el('div', 'toast-actions');
            const btn = el('button', 'btn', actionLabel);
            btn.addEventListener('click', () => { try { action(); } finally { close(); } });
            actions.appendChild(btn);
            body.appendChild(actions);
        }
        node.appendChild(body);
        const closeBtn = el('button', 'toast-close', '✕');
        node.appendChild(closeBtn);

        let closed = false;
        let hideTimer = null;
        function close() {
            if (closed) return;
            closed = true;
            if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
            node.classList.remove('show');
            setTimeout(() => { if (node.parentNode) node.parentNode.removeChild(node); }, 200);
        }
        closeBtn.addEventListener('click', close);

        container.appendChild(node);
        // Триггер анимации появления на следующем тике
        requestAnimationFrame(() => node.classList.add('show'));
        if (timeout > 0) hideTimer = setTimeout(close, timeout);
        return { close };
    }

    // ===== Утилиты =====
    const $ = (id) => document.getElementById(id);
    const el = (tag, cls, text) => {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text != null) e.textContent = text;
        return e;
    };

    function authHeaders() {
        const h = {};
        if (state.token) h.Authorization = 'Basic ' + state.token;
        if (state.sessionId) h['X-Session-Id'] = state.sessionId;
        return h;
    }

    async function api(path, opts = {}) {
        let res;
        try {
            res = await fetch(path, {
                ...opts,
                headers: {
                    'Content-Type': 'application/json',
                    ...authHeaders(),
                    ...(opts.headers || {}),
                },
            });
        } catch (netErr) {
            if (netErr.name === 'AbortError') throw netErr;
            // Сетевая ошибка (server down / connection refused) — сервер недоступен.
            state.wasDown = true;
            showServerDownToast();
            throw window.DNOWebErrors.normalize(netErr);
        }
        if (res.status === 401) {
            state.creds = null;
            state.token = null;
            localStorage.removeItem('dno-tool_token');
            updateAuthUI();
            throw new Error('Требуется аутентификация');
        }
        if (!res.ok) {
            const txt = await res.text().catch(() => '');
            throw new Error(errorMessage(`${res.status}: ${txt || res.statusText}`));
        }
        // Сервер ответил успешно — сбрасываем счётчик heartbeat-неудач.
        _hbFailCount = 0;
        if (res.status === 204) return null;
        try { return await res.json(); }
        catch (error) { throw window.DNOWebErrors.normalize(error); }
    }

    // ===== Аутентификация =====
    function setCredentials(user, pass, sessionId = null) {
        state.creds = { username: user, password: pass };
        state.token = btoa(`${user}:${pass}`);
        localStorage.setItem('dno-tool_token', state.token);
        if (sessionId) {
            state.sessionId = sessionId;
            localStorage.setItem('dno-tool_session', sessionId);
        }
        updateAuthUI();
    }

    async function loginRequest(user, pass) {
        // POST /api/login — сервер создаёт сессию и возвращает session_id.
        try {
            const res = await fetch('/api/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username: user, password: pass }),
            });
            if (!res.ok) return null;
            const data = await res.json();
            if (data && data.session_id) {
                state.sessionId = data.session_id;
                localStorage.setItem('dno-tool_session', data.session_id);
                state.multiUser = !!data.multi_user;
                return data;
            }
        } catch (e) {
            console.warn('loginRequest: ошибка, fallback на token-only', e);
        }
        return null;
    }

    function logout() {
        clearTimeout(state.deviceSizeTimer);
        state.deviceSizeTimer = null;
        stopHeartbeat();
        hideServerDownToast();
        state.wasDown = false;
        state.creds = null;
        state.token = null;
        state.sessionId = null;
        state.currentJobId = null;
        state.currentJobSeq = 0;
        state.viewingJobId = null;
        state.activeJobs = [];
        // Закрыть все viewer-WS.
        for (const ws of state.jobWs.values()) {
            try { ws.close(); } catch {}
        }
        state.jobWs.clear();
        state.jobBuffers.clear();
        clearAllPersistedJobs();
        localStorage.removeItem('dno-tool_token');
        localStorage.removeItem('dno-tool_session');
        updateAuthUI();
        state.selectedTask = null;
        renderHosts([]);
        renderTaskTree([]);
        updateTaskCombobox();
        updateActiveJobsBadge();
        renderActiveJobsPanel();
        // Файлы интерфейса закрываются вместе с сеансом: дальше только страница входа.
        fetch('/api/logout', { method: 'POST' }).catch(() => {}).finally(() => location.reload());
    }

    function updateAuthUI() {
        if (!state.token) {
            updateThumbTimer();
            updateStatusTimer();
        }
        window.dispatchEvent(new CustomEvent('dno:auth', { detail: { authenticated: !!state.token } }));
        if (state.token) {
            $('authStatus').textContent = state.creds ? `Авторизован: ${state.creds.username}` : 'Авторизован';
            $('authStatus').classList.add('ok');
            $('loginBtn').textContent = 'Выйти';
        } else {
            $('authStatus').textContent = 'Не авторизован';
            $('authStatus').classList.remove('ok');
            $('loginBtn').textContent = 'Войти';
        }
    }

    // ===== Загрузка данных =====
    async function loadVersion() {
        try {
            const v = await api('/api/version');
            $('version').textContent = `v${v.version}`;
            if (v.thread_count) state.threadCount = v.thread_count;
            if (v.command_thread_count) state.commandThreadCount = v.command_thread_count;
            if (v.job_ttl_minutes !== undefined) state.jobTtlMinutes = v.job_ttl_minutes;
            if (v.multi_user !== undefined) state.multiUser = !!v.multi_user;
        } catch (_) { /* игнор */ }
    }

    // Сырой SVG -> data-URL (base64 для надёжности с UTF-8).
    function svgToDataUrl(svg) {
        return 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(svg)));
    }

    // Загрузить комплект SVG-иконок (online/offline/default/veyon_down) одним запросом
    // и закэшировать в state.svgCache как data-URL. После этого buildStatusIcon
    // и placeholder миниатюр ставят img.src из памяти — 0 сетевых запросов.
    // При ошибке кэш остаётся null, фронт падает на URL-подход (fallback).
    async function loadIconSet() {
        try {
            const res = await fetch('/api/icon-set', { headers: authHeaders(), cache: 'no-store' });
            if (!res.ok) return;
            const set = await res.json();
            for (const s of ['online', 'offline', 'default', 'veyon_down']) {
                const svg = set[s];
                if (svg) state.svgCache[s] = svgToDataUrl(svg);
            }
            state.thumbnailTheme = set.thumbnail || null;
            $('selectedList').querySelectorAll('.thumb-wrap').forEach(applyThumbnailTheme);
            // Дерево могло построиться раньше готовности кэша (параллельный
            // Promise.all в initAfterAuth) - перекрасить из памяти.
            refreshTreeStatusIcons();
        } catch (e) {
            console.debug('loadIconSet: ошибка, fallback на URL');
        }
    }

    // Загрузить комплект SVG-иконок Veyon-фич и закэшировать как data-URL.
    async function loadVeyonIconSet() {
        try {
            const res = await fetch('/api/veyon/icon-set', { headers: authHeaders() });
            if (!res.ok) return;
            const set = await res.json();
            for (const f of Object.keys(set)) {
                const svg = set[f];
                if (svg) state.veyonIconCache[f] = svgToDataUrl(svg);
            }
            // Применить к <img> в кнопках обеих панелей + кнопки-тогглеры
            document.querySelectorAll('#veyonFeatures img[data-vi], #vfeatToggle img[data-vi], #ctrlFeatures img[data-vi], #ctrlToggle img[data-vi]').forEach(img => {
                const key = img.getAttribute('data-vi');
                if (state.veyonIconCache[key]) img.src = state.veyonIconCache[key];
            });
        } catch (e) {
            console.debug('loadVeyonIconSet: ошибка');
        }
    }

    async function loadVeyonStatus() {
        try {
            const s = await api('/api/veyon/status');
            state.veyonAvailable = !!s.available;
            state.thumbnailsAvailable = !!(s.thumbnails_available ?? s.available);
            state.featuresAvailable = !!(s.features_available ?? s.available);
            state.powerAvailable = !!(s.power_features_available ?? true);
            state.thumbInterval = s.thumbnail_interval_sec || 5;
            state.thumbnailWidth = Number.isInteger(s.thumbnail_width) && s.thumbnail_width > 0
                ? s.thumbnail_width : null;
            state.thumbnailScale = Number.isInteger(s.thumbnail_scale) ? Math.max(1, Math.min(4, s.thumbnail_scale)) : 1;
            state.thumbnailMaxDimension = Number.isInteger(s.thumbnail_max_dimension) ? Math.max(1, Math.min(4096, s.thumbnail_max_dimension)) : 2048;
            state.rfbBridgeAvailable = !!(s.rfb_bridge_available || s.vnc_enabled);
            state.veyonServerPort = s.server_port || 11100;
            state.veyonServiceStats = {
                pid: s.service_pid ?? null,
                rss_mb: s.service_rss_mb ?? null,
                commit_mb: s.service_commit_mb ?? null,
                established: s.webapi_established ?? null,
                close_wait: s.webapi_close_wait ?? null,
            };
            updateVeyonServiceTooltip();
        } catch (_) {
            state.veyonAvailable = false;
            state.thumbnailsAvailable = false;
            state.featuresAvailable = false;
            state.powerAvailable = true;
            state.rfbBridgeAvailable = false;
            state.veyonServiceStats = null;
        }
    }

    // Тултип кнопки Veyon: статус + память сервиса Veyon (диагностика утечки).
    // Veyon не освобождает память закрытых WebAPI-соединений (upstream-баг) —
    // оператору важно видеть рост прямо в интерфейсе. Обновление подхватится
    // тултипом при следующем наведении (Tip читает data-tip на show).
    // Память из МБ в человекочитаемый вид: МБ -> ГБ после 1024.
    function fmtMemMb(mb) {
        if (mb == null) return '?';
        const v = Number(mb);
        if (!Number.isFinite(v) || v < 0) return '?';
        if (v < 1024) return `${Math.round(v)} МБ`;
        return `${(v / 1024).toFixed(1)} ГБ`;
    }

    function updateVeyonServiceTooltip() {
        const btn = document.getElementById('vfeatToggle');
        if (!btn) return;
        let tip = 'Veyon';
        if (state.veyonAvailable) {
            tip = 'Veyon: доступен';
            const st = state.veyonServiceStats;
            if (st && st.rss_mb != null) {
                const mb = Number(st.rss_mb);
                const warn = mb >= 1536 ? ' ⚠' : '';
                tip += ` · память ${fmtMemMb(st.rss_mb)}${warn}`;
                if (st.established != null) tip += ` · соединений ${st.established}`;
            }
        } else {
            tip = 'Veyon: недоступен';
        }
        btn.setAttribute('data-tip', tip);
    }

    async function loadHosts() {
        try {
            const data = await api('/api/hosts');
            state.hostsTree = Array.isArray(data) ? data : [];
            state.hostsByIid = new Map();
            state.hostStatuses = {}; // сброс кэша: дерево могло измениться
            collectHostInfo(state.hostsTree, '');
            renderHosts(state.hostsTree);
            return true;
        } catch (e) {
            console.error('loadHosts', e);
            renderHosts([]);
            return false;
        }
    }

    async function revealImportedHosts(iids) {
        if (!await loadHosts()) throw new Error('Не удалось обновить список устройств');
        for (const iid of iids) {
            const row = $('hostTree').querySelector(`.tree-item.device[data-iid="${CSS.escape(iid)}"]`);
            if (!row) continue;
            let children = row.parentElement;
            while (children?.classList.contains('tree-children')) {
                children.classList.remove('hidden');
                children.previousElementSibling?.classList.remove('collapsed');
                children = children.parentElement;
            }
            state.selectedHosts.add(iid);
            row.querySelector('input[type="checkbox"]').checked = true;
        }
        syncAllFolderCheckboxes();
        renderSelectedList();
        updateSelectedCount();
        saveSelectedHosts();
        $('hostTree').querySelector(`.tree-item.device[data-iid="${CSS.escape(iids[0] || '')}"]`)?.scrollIntoView({ block: 'nearest' });
    }

    // Рекурсивно собрать map iid -> {name, host, folder} из дерева хостов.
    function collectHostInfo(tree, parentFolder) {
        if (!Array.isArray(tree)) return;
        for (const node of tree) {
            if (node && typeof node === 'object') {
                if ('host' in node && 'iid' in node) {
                    state.hostsByIid.set(node.iid, {
                        name: node.name || node.host,
                        host: node.host,
                        folder: parentFolder || '',
                    });
                } else if ('host' in node) {
                    // Fallback: нет iid (старые данные) — используем host как ключ
                    const key = node.iid || node.host;
                    state.hostsByIid.set(key, {
                        name: node.name || node.host,
                        host: node.host,
                        folder: parentFolder || '',
                    });
                } else {
                    // Группа: один ключ -> массив детей
                    for (const k of Object.keys(node)) {
                        const folderPath = parentFolder ? `${parentFolder} / ${k}` : k;
                        collectHostInfo(node[k] || [], folderPath);
                    }
                }
            }
        }
    }

    // Описание устройства для подписи (по iid; fallback на сам iid если не найден)
    function hostLabel(iid) {
        const info = state.hostsByIid.get(iid);
        return info ? (info.name || info.host) : iid;
    }

    // Сетевой адрес устройства по iid (для пинга/иконок/миниатюр)
    function hostOf(iid) {
        const info = state.hostsByIid.get(iid);
        return info ? info.host : null;
    }

    async function loadCommands() {
        try {
            const data = await api('/api/commands');
            state.commandsTree = Array.isArray(data) ? data : [];
            state.tasksFlat = [];
            flattenTasks(state.commandsTree, []);
            renderTaskTree(state.commandsTree);
        } catch (e) {
            console.error('loadCommands', e);
            renderTaskTree([]);
        }
    }

    function flattenTasks(items, path) {
        if (!Array.isArray(items)) return;
        for (const item of items) {
            if (!item || typeof item !== 'object') continue;
            if ('name' in item && 'commands' in item) {
                state.tasksFlat.push({
                    iid: item.iid,
                    name: item.name,
                    path: path.concat(item.name).join(' → '),
                    task: item,
                });
            } else if (Object.keys(item).length === 1) {
                const [cat] = Object.keys(item);
                flattenTasks(item[cat], path.concat(cat));
            }
        }
    }

    // ===== Рендер дерева хостов (с рекурсивным выбором) =====
    function renderHosts(tree) {
        const root = $('hostTree');
        root.innerHTML = '';
        if (!tree || !tree.length) {
            root.appendChild(el('div', 'empty-state', 'Нет устройств. Импортируйте через приложение.'));
            return;
        }
        for (const node of tree) buildHostNode(node, root);
        syncAllFolderCheckboxes();
        updateSelectedCount();
    }

    function buildHostNode(node, parentEl) {
        // Группа: один ключ -> массив
        if (node && typeof node === 'object' && !('host' in node) && Object.keys(node).length === 1) {
            const [groupName] = Object.keys(node);
            const children = Array.isArray(node[groupName]) ? node[groupName] : [];

            const item = el('div', 'tree-item folder collapsed');
            const caret = el('span', 'caret', '▸');
            item.appendChild(caret);
            item.appendChild(el('span', 'icon', '📁'));
            item.appendChild(el('span', 'label', groupName));

            // Чекбокс папки
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.dataset.folder = '1';
            // Вставляем перед caret для визуального порядка
            item.insertBefore(cb, caret);

            const childWrap = el('div', 'tree-children hidden');
            for (const child of children) buildHostNode(child, childWrap);

            // Клик по строке (не по чекбоксу) — свернуть/развернуть
            item.addEventListener('click', (e) => {
                if (e.target.tagName === 'INPUT') return;
                const collapsed = item.classList.toggle('collapsed');
                childWrap.classList.toggle('hidden', collapsed);
            });

            // Чекбокс папки — рекурсивный выбор детей
            cb.addEventListener('click', (e) => e.stopPropagation());
            cb.addEventListener('change', () => {
                setFolderRecursive(item, cb.checked);
                // Qt: propagate_checkstate_up - родители пересчитываются
                // после каскада по поддереву (indeterminate/полное снятие).
                syncParentFolderCheckbox(item);
            });

            parentEl.appendChild(item);
            parentEl.appendChild(childWrap);
            return;
        }

        // Устройство
        if (node && typeof node === 'object' && 'host' in node) {
            const iid = node.iid || node.host;  // fallback на host если iid нет
            const item = el('div', 'tree-item device');
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.dataset.iid = iid;
            cb.dataset.host = node.host;
            cb.dataset.name = node.name || node.host;
            cb.checked = state.selectedHosts.has(iid);
            cb.addEventListener('click', (e) => e.stopPropagation());
            cb.addEventListener('change', () => {
                if (cb.checked) {
                    state.selectedHosts.add(iid);
                } else {
                    state.selectedHosts.delete(iid);
                    // Как в Qt: снятие чекбокса сбрасывает иконку в default.
                    resetTreeIconToDefault(iid);
                }
                syncParentFolderCheckbox(item);
                renderSelectedList();
                updateSelectedCount();
                saveSelectedHosts();
            });
            item.appendChild(cb);
            const iconWrap = el('span', 'icon icon-svg');
            iconWrap.appendChild(buildStatusIcon(iid));
            item.appendChild(iconWrap);
            item.appendChild(el('span', 'label', node.name || node.host));
            item.appendChild(el('span', 'host', node.host));
            item.dataset.iid = iid;
            item.dataset.host = node.host;
            parentEl.appendChild(item);
        }
    }

    // Рекурсивно установить состояние чекбоксов всех дочерних устройств папки
    function setFolderRecursive(folderItem, checked) {
        const childWrap = folderItem.nextElementSibling;
        if (!childWrap || !childWrap.classList.contains('tree-children')) return;

        const deviceCbs = childWrap.querySelectorAll(':scope > .tree-item.device > input[type="checkbox"]');
        deviceCbs.forEach(cb => {
            cb.checked = checked;
            cb.indeterminate = false;
            const iid = cb.dataset.iid;
            if (checked) {
                state.selectedHosts.add(iid);
            } else {
                state.selectedHosts.delete(iid);
                resetTreeIconToDefault(iid);
            }
        });
        saveSelectedHosts();

        // Рекурсивно для вложенных папок
        const folderCbs = childWrap.querySelectorAll(':scope > .tree-item.folder > input[type="checkbox"]');
        folderCbs.forEach(fcb => {
            fcb.checked = checked;
            fcb.indeterminate = false;
            setFolderRecursive(fcb.closest('.tree-item.folder'), checked);
        });

        renderSelectedList();
        updateSelectedCount();
    }

    // Пересчитать состояние чекбокса родительской папки на основе дочерних
    function syncParentFolderCheckbox(childItem) {
        let wrap = childItem.parentElement;
        while (wrap && wrap.classList.contains('tree-children')) {
            const folderItem = wrap.previousElementSibling;
            // classList.contains не принимает составной селектор ('tree-item.folder'
            // всегда false - точки не бывает в отдельном токене), из-за этого
            // пересчёт родителей никогда не выполнялся.
            if (!folderItem || !folderItem.matches('.tree-item.folder')) {
                wrap = folderItem?.parentElement;
                continue;
            }
            const fcb = folderItem.querySelector('input[type="checkbox"]');
            if (fcb) recomputeFolderCheckbox(folderItem, fcb);
            wrap = folderItem.parentElement;
        }
    }

    // Пересчитать чекбокс папки по дочерним чекбоксам
    function recomputeFolderCheckbox(folderItem, fcb) {
        const childWrap = folderItem.nextElementSibling;
        if (!childWrap) return;
        const childCbs = childWrap.querySelectorAll(':scope > .tree-item > input[type="checkbox"]');
        let checkedCount = 0;
        let indeterminateCount = 0;
        childCbs.forEach(cb => {
            if (cb.indeterminate) indeterminateCount++;
            else if (cb.checked) checkedCount++;
        });
        const total = childCbs.length;
        if (total === 0) {
            fcb.checked = false;
            fcb.indeterminate = false;
        } else if (checkedCount === total && indeterminateCount === 0) {
            fcb.checked = true;
            fcb.indeterminate = false;
        } else if (checkedCount === 0 && indeterminateCount === 0) {
            fcb.checked = false;
            fcb.indeterminate = false;
        } else {
            fcb.checked = false;
            fcb.indeterminate = true;
        }
    }

    // Пересчитать все чекбоксы папок (после перерисовки дерева)
    function syncAllFolderCheckboxes() {
        const root = $('hostTree');
        // Идём снизу вверх: сначала самые вложенные папки
        const folderItems = root.querySelectorAll('.tree-item.folder');
        // Обрабатываем в обратном порядке (вложенные первыми)
        const arr = Array.from(folderItems).reverse();
        arr.forEach(folderItem => {
            const fcb = folderItem.querySelector('input[type="checkbox"]');
            if (fcb) recomputeFolderCheckbox(folderItem, fcb);
        });
    }

    // ===== Рендер списка выбранных устройств (3 режима + опц. группировка) =====
    // Видимость устройства при фильтре "только онлайн" (Qt-паритет:
    // unknown/offline скрыты, виден только hostStatuses[iid] === true).
    function isOnlineVisible(iid) {
        if (!state.onlineOnly) return true;
        return state.hostStatuses[iid] === true;
    }

    // Чистый рендер списка без рестарта таймеров и немедленного зонда
    // (для SSE-обновлений и сброса иконок: полный renderSelectedList здесь
    // нельзя - он оборвёт текущий зонд через probeAbort и зациклит опрос).
    function renderSelectedListView() {
        const wrap = $('selectedList');
        // Режим отображения (list/icons/thumbs) всегда задаёт стили карточек.
        // Группировка лишь добавляет класс 'grouped' для заголовков групп.
        wrap.className = 'device-list mode-' + state.viewMode + (state.groupByFolder ? ' grouped' : '');
        applyDeviceSize();
        wrap.innerHTML = '';
        // Видимость панелей управления:
        // Veyon-панель целиком скрывается когда Veyon недоступен
        // (featuresAvailable === false), панель «Управление» всегда доступна
        // (power_on/reboot/powerdown через SSH/UDP, login_ssh через DBus,
        // logoff через SSH — не требуют Veyon).
        const vf = $('veyonFeatures');
        if (vf) vf.classList.toggle('hidden', !state.featuresAvailable);
        const vft = $('vfeatToggle');
        if (vft) vft.classList.toggle('hidden', !state.featuresAvailable);
        if (!state.selectedHosts.size) {
            wrap.appendChild(el('div', 'empty-state', 'Нет выбранных устройств'));
            return;
        }

        if (state.groupByFolder) {
            renderSelectedGrouped(wrap);
        } else {
            let shown = 0;
            for (const host of state.selectedHosts) {
                if (!isOnlineVisible(host)) continue;
                wrap.appendChild(buildDeviceItem(host));
                shown++;
            }
            if (!shown) {
                wrap.appendChild(el('div', 'empty-state', 'Нет устройств в сети'));
            }
        }
    }

    function renderSelectedList() {
        renderSelectedListView();
        // Запуск/остановка таймеров автообновления
        updateThumbTimer();
        updateStatusTimer();
        // Немедленный первичный batch статусов (не ждать 15с до первого тика).
        // Запускается ТОЛЬКО в режиме icons — там это и видно (раскраска иконок).
        // Немедленный первичный зонд: статус для list/icons, миниатюры для thumbs.
        // SSE отдаёт результаты по мере готовности (as_completed) — иконки/миниатюры
        // появляются постепенно, видна скорость опроса (web.thread_count).
        if (state.selectedHosts.size && (state.viewMode === 'list' || state.viewMode === 'icons')) {
            streamProbe(false);
        }
        if (state.selectedHosts.size && state.viewMode === 'thumbs') {
            // Если Veyon-миниатюры доступны — зонд с миниатюрами (wantThumb=true).
            // Если нет — обычный пинг-зонд (wantThumb=false), чтобы хотя бы
            // покрасить иконки статусов (online/offline) в режиме миниатюр.
            streamProbe(state.thumbnailsAvailable);
        }
    }

    // Рендер выбранных устройств, сгруппированных по родительским папкам.
    // Карточки устройств строятся тем же buildDeviceItem, что и без группировки —
    // отличается только наличие заголовков групп. Стиль как в десктопе:
    // заголовок группы + горизонтальная линия справа, под ним — устройства
    // в текущем режиме (list/icons/thumbs).
    function renderSelectedGrouped(wrap) {
        // Группа -> [iid]; устройства без папки попадают в «Без группы»
        const groups = new Map();
        for (const iid of state.selectedHosts) {
            const info = state.hostsByIid.get(iid);
            const folder = (info && info.folder) || 'Без группы';
            if (!groups.has(folder)) groups.set(folder, []);
            groups.get(folder).push(iid);
        }
        let shownTotal = 0;
        for (const [folder, iids] of groups) {
            // При фильтре "только онлайн" пустые группы не пропускаем, а показываем
            // свёрнутыми с заголовком "0/N" (как авто-сворачивание в Qt).
            const visible = state.onlineOnly ? iids.filter(isOnlineVisible) : iids;
            shownTotal += visible.length;
            const groupBlock = el('div', 'group-block');
            groupBlock.dataset.folder = folder;
            const header = el('div', 'group-header');
            const title = el('span', 'group-name', folder);
            const count = el('span', 'group-count',
                state.onlineOnly ? `${visible.length}/${iids.length}` : `${iids.length}`);
            const line = el('span', 'group-line');
            header.appendChild(title);
            header.appendChild(count);
            header.appendChild(line);
            groupBlock.appendChild(header);

            const body = el('div', 'group-body');
            for (const iid of visible) {
                body.appendChild(buildDeviceItem(iid));
            }
            groupBlock.appendChild(body);

            if (state.onlineOnly && !visible.length) {
                groupBlock.classList.add('collapsed');
                title.textContent = folder + ' ▸';
            }

            header.style.cursor = 'pointer';
            header.addEventListener('click', () => {
                const collapsed = groupBlock.classList.toggle('collapsed');
                title.textContent = folder + (collapsed ? ' ▸' : '');
            });

            wrap.appendChild(groupBlock);
        }
        if (state.onlineOnly && !shownTotal) {
            wrap.appendChild(el('div', 'empty-state', 'Нет устройств в сети'));
        }
    }

    const HOST_ACTION_GLYPHS = {
        vnc: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
        info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
        terminal: '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="M6 9l3 3-3 3M12 15h5"/>',
        files: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    };

    // Действия с одним хостом: только отсюда открываются окна сведений, терминала и файлов.
    function buildHostActions(iid) {
        const actions = el('div', 'host-actions');
        const add = (kind, tip, handler) => {
            const button = el('button', `host-action host-action-${kind}`);
            button.type = 'button';
            button.dataset.tip = tip;
            button.setAttribute('aria-label', `${tip}: ${hostLabel(iid)}`);
            button.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${HOST_ACTION_GLYPHS[kind]}</svg>`;
            button.addEventListener('click', event => {
                event.stopPropagation();
                handler();
            });
            actions.appendChild(button);
        };
        const openTool = id => window.DNODesktopTools?.openHostTool?.(id, iid);
        add('info', 'Сведения об устройстве', () => openTool('device-info'));
        add('terminal', 'SSH-терминал', () => openTool('terminal'));
        add('files', 'Файлы (SFTP)', () => openTool('files'));
        if (state.rfbBridgeAvailable) add('vnc', 'Удалённый доступ (VNC)', () => openVncModal(iid));
        return actions;
    }

    function buildDeviceItem(iid) {
        const info = state.hostsByIid.get(iid) || { host: iid, name: iid };
        const host = info.host;
        const item = el('div', 'device-item');
        item.dataset.iid = iid;
        item.dataset.host = host;
        item.dataset.tip = host;
        item.tabIndex = 0;
        // На телефоне действия раскрываются явно, а не от эмулированного hover.
        const revealActions = () => {
            if (!isMobile()) return;
            const open = !item.classList.contains('actions-open');
            $('selectedList').querySelectorAll('.actions-open').forEach(node => node.classList.remove('actions-open'));
            item.classList.toggle('actions-open', open);
        };
        item.addEventListener('click', event => {
            if (!event.target.closest('button')) revealActions();
        });
        item.addEventListener('keydown', event => {
            if (event.target === item && (event.key === 'Enter' || event.key === ' ')) {
                event.preventDefault();
                revealActions();
            }
        });
        // Живая подсказка: при наведении обогатить данными /api/host_info
        item.addEventListener('mouseenter', () => enrichDeviceTip(iid, item));

        // Единая кнопка удаления для всех режимов (позиционируется через CSS)
        const remove = el('button', 'remove', '✕');
        remove.type = 'button';
        remove.dataset.tip = 'Убрать';
        remove.addEventListener('click', (e) => {
            e.stopPropagation();
            state.selectedHosts.delete(iid);
            const cb = document.querySelector(`#hostTree input[data-iid="${CSS.escape(iid)}"]`);
            if (cb) {
                cb.checked = false;
                syncParentFolderCheckbox(cb.closest('.tree-item'));
            }
            resetTreeIconToDefault(iid);
            renderSelectedList();
            updateSelectedCount();
        });
        item.appendChild(remove);

        if (state.viewMode === 'list') {
            // Маленькая иконка онлайн/оффлайн для наглядности (рядом с названием).
            const iconWrap = el('span', 'icon icon-svg icon-list');
            iconWrap.appendChild(buildStatusIcon(iid));
            item.appendChild(iconWrap);
            item.appendChild(el('span', 'name', hostLabel(iid)));
            item.appendChild(buildHostActions(iid));
        } else if (state.viewMode === 'icons') {
            const iconWrap = el('span', 'icon icon-svg');
            const icon = buildStatusIcon(iid);
            setDeviceImageSize(icon);
            iconWrap.appendChild(icon);
            item.appendChild(iconWrap);
            item.appendChild(el('span', 'name', hostLabel(iid)));
            item.appendChild(buildHostActions(iid));
        } else if (state.viewMode === 'thumbs') {
            const thumbWrap = el('div', 'thumb-wrap');
            thumbWrap.appendChild(buildThumbnail(iid));
            applyThumbnailTheme(thumbWrap);
            const refresh = el('button', 'thumb-refresh');
            refresh.type = 'button';
            refresh.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>';
            refresh.dataset.tip = 'Обновить миниатюру';
            refresh.addEventListener('click', (e) => {
                e.stopPropagation();
                reloadThumbnail(iid, thumbWrap);
            });
            thumbWrap.appendChild(refresh);
            item.appendChild(thumbWrap);
            item.appendChild(buildHostActions(iid));
            const label = el('div', 'thumb-label');
            const statusIcon = buildStatusIcon(iid);
            statusIcon.classList.add('thumb-status-icon');
            label.appendChild(statusIcon);
            label.appendChild(el('span', 'name', hostLabel(iid)));
            item.appendChild(label);
        }
        return item;
    }

    // ===== Живые подсказки устройств (/api/host_info) =====

    const HOST_INFO_TTL = 60000; // кэш подсказок, мс (как в десктопе — 60 с)

    // Собрать текст подсказки из ответа /api/host_info (+статика из hostsByIid).
    function hostInfoTipText(iid, data) {
        const info = state.hostsByIid.get(iid) || {};
        const lines = [];
        if (info.name) lines.push('Имя: ' + info.name);
        if (info.host) lines.push('Хост: ' + info.host);
        const rows = (data && data.rows) || [];
        for (const r of rows) {
            if (r && r.length >= 2 && r[1]) lines.push(r[0] + ': ' + r[1]);
        }
        return lines.join('\n');
    }

    // Обогатить подсказку устройства live-данными.
    // Кэш свеж → подмена dataset.tip + мгновенное обновление открытого тултипа;
    // иначе fetch, ошибка/офлайн → остаётся host (отрицательный результат тоже кэшируется).
    async function enrichDeviceTip(iid, item) {
        if (!iid) return;
        const cached = state.hostInfoCache.get(iid);
        if (cached && (Date.now() - cached.ts) < HOST_INFO_TTL) {
            if (cached.tip) {
                item.dataset.tip = cached.tip;
                if (Tip.el && !Tip.el.classList.contains('hidden') && item.matches(':hover')) {
                    Tip.show(item);
                }
            }
            return;
        }
        if (state.hostInfoInflight.has(iid)) return;
        state.hostInfoInflight.add(iid);
        try {
            const res = await fetch('/api/host_info/' + encodeURIComponent(iid), {
                headers: authHeaders(),
            });
            let data = null;
            try { data = res.status === 200 ? await res.json() : null; } catch (_) {}
            let tip = '';
            if (data && data.ok) {
                tip = hostInfoTipText(iid, data) || item.dataset.tip;
            } else if (res.status === 401) {
                state.hostInfoInflight.delete(iid);
                return; // сессия истекла — не кэшируем, не спамим
            }
            // !ok (offline/no_data/ошибка) → кэшируем пустой tip (fallback на host)
            state.hostInfoCache.set(iid, { ts: Date.now(), tip: tip });
            if (tip) {
                item.dataset.tip = tip;
                if (Tip.el && !Tip.el.classList.contains('hidden') && item.matches(':hover')) {
                    Tip.show(item);
                }
            }
        } catch (_) {
            // сеть недоступна — без кэша, повтор при следующем hover
        } finally {
            state.hostInfoInflight.delete(iid);
        }
    }

    // Query-параметр token для img-эндпоинтов (браузер не шлёт Basic для <img>)
    function tokenQuery() {
        return state.token ? `token=${encodeURIComponent(state.token)}` : '';
    }

    // SVG-иконка статуса устройства (online/offline).
    // Первичный рендер: если статус уже известен (кэш state.hostStatuses) —
    // сразу online.svg/offline.svg, иначе default.svg (серый, мгновенно — без пинга).
    // Источник — state.svgCache (data-URL из памяти, 0 запросов); при отсутствии
    // кэша fallback на /api/icon/{iid}?s=... (один запрос).
    // Статус обновляется позже через updateOneStatusIcon (SSE /api/hosts/probe).
    function buildStatusIcon(iid) {
        const name = hostLabel(iid);
        const img = document.createElement('img');
        img.className = 'status-icon-img';
        img.alt = name;
        img.loading = 'lazy';
        img.dataset.iid = iid;
        const known = state.hostStatuses[iid];
        const s = known === true ? 'online' : known === false ? 'offline' : 'default';
        const cached = state.svgCache[s];
        if (cached) {
            img.src = cached;
        } else {
            const q = tokenQuery();
            img.src = `/api/icon/${encodeURIComponent(iid)}?${q}&s=${s}&t=${Date.now()}`;
        }
        img.addEventListener('error', () => {
            img.replaceWith(el('span', null, '🖥'));
        });
        return img;
    }

    // Обновить иконку статуса одного устройства (по iid) по результату SSE-зонда.
    // Красит и правый список, и левое дерево (как иконка хоста в Qt-дереве).
    function updateOneStatusIcon(iid, online) {
        const prevVisible = isOnlineVisible(iid);
        state.hostStatuses[iid] = !!online;
        const q = tokenQuery();
        const item = $('selectedList').querySelector(
            `.device-item[data-iid="${CSS.escape(iid)}"] img.status-icon-img`
        );
        if (item) {
            const s = online ? 'online' : 'offline';
            const cached = state.svgCache[s];
            if (cached) {
                item.src = cached;
            } else {
                item.src = `/api/icon/${encodeURIComponent(iid)}?${q}&s=${s}&t=${Date.now()}`;
            }
        }
        updateTreeStatusIcon(iid, online);
        refreshOnlineOnlyVisibility(iid, prevVisible);
    }

    // Обновить иконку хоста в левом дереве (Qt: default -> online/offline).
    // Невыбранные хосты зонд не покрывает - они остаются в default.
    function updateTreeStatusIcon(iid, online) {
        const img = $('hostTree').querySelector(
            `.tree-item.device[data-iid="${CSS.escape(iid)}"] img.status-icon-img`
        );
        if (!img) return;
        const s = online ? 'online' : 'offline';
        const cached = state.svgCache[s];
        if (cached) {
            img.src = cached;
        } else {
            const q = tokenQuery();
            img.src = `/api/icon/${encodeURIComponent(iid)}?${q}&s=${s}&t=${Date.now()}`;
        }
    }

    // Сбросить иконку хоста в дереве на default (как снятие чекбокса в Qt).
    function resetTreeIconToDefault(iid) {
        const prevVisible = isOnlineVisible(iid);
        delete state.hostStatuses[iid];

        const img = $('hostTree').querySelector(
            `.tree-item.device[data-iid="${CSS.escape(iid)}"] img.status-icon-img`
        );
        if (img) {
            const cached = state.svgCache.default;
            if (cached) {
                img.src = cached;
            } else {
                const q = tokenQuery();
                img.src = `/api/icon/${encodeURIComponent(iid)}?${q}&s=default&t=${Date.now()}`;
            }
        }
        // Снятие чекбокса: хост удалён из selectedHosts вызывающим кодом, но если
        // карточка ещё в DOM (порядок вызовов) - скрыть по фильтру.
        refreshOnlineOnlyVisibility(iid, prevVisible);
    }

    // Перекрасить все иконки дерева из кэша (после loadIconSet / перерисовки).
    // Выбранные - по hostStatuses, остальные - default.
    function refreshTreeStatusIcons() {
        const root = $('hostTree');
        if (!root) return;
        root.querySelectorAll('.tree-item.device img.status-icon-img').forEach(img => {
            const item = img.closest('.tree-item.device');
            const iid = item?.dataset.iid || img.dataset.iid;
            if (!iid) return;
            const known = state.hostStatuses[iid];
            const s = known === true ? 'online' : known === false ? 'offline' : 'default';
            const cached = state.svgCache[s];
            if (cached) img.src = cached;
        });
    }

    // Обновить <img> миниатюры одного устройства (по iid) по результату SSE-зонда.
    // Обычный пинг и состояние выбранного источника остаются независимыми.
    function updateThumbImg(iid, online, objectURL, backendOk, backend, status, generation = null) {
        if (generation !== null && !isThumbnailGenerationCurrent(iid, generation)) {
            if (objectURL) URL.revokeObjectURL(objectURL);
            return;
        }
        if (generation === null) nextThumbnailGeneration(iid);
        const prevVisible = isOnlineVisible(iid);
        state.hostStatuses[iid] = !!online;
        if (typeof backendOk === 'boolean') state.hostBackendOk[iid] = backendOk;
        else delete state.hostBackendOk[iid];
        state.hostThumbnailStatus[iid] = status || null;
        state.hostThumbnailBackend[iid] = backend || null;
        // Дерево всегда показывает online/offline (без veyon_down, как в Qt).
        updateTreeStatusIcon(iid, online);
        refreshOnlineOnlyVisibility(iid, prevVisible);
        const item = $('selectedList').querySelector(
            `.device-item[data-iid="${CSS.escape(iid)}"] .thumb-wrap img.thumb-img`
        );
        if (!item) {
            if (objectURL) URL.revokeObjectURL(objectURL);
            return;
        }
        item.title = backendOk === false && online
            ? `Источник миниатюр недоступен (${backend || 'экран'}: ${status || 'unavailable'})`
            : '';
        item.dataset.thumbnailBackend = backend || '';
        item.dataset.thumbnailStatus = status || '';
        const badge = item.closest('.device-item')?.querySelector('.thumb-status-icon');
        if (badge) {
            const stateName = online ? 'online' : 'offline';
            badge.src = state.svgCache[stateName] || `/api/icon/${encodeURIComponent(iid)}?${tokenQuery()}&s=${stateName}`;
        }
        if (objectURL) {
            item.classList.remove('is-placeholder');
            item.closest('.thumb-wrap')?.classList.remove('placeholder-frame');
            const prev = item.dataset.blobUrl;
            item.src = objectURL;
            item.dataset.blobUrl = objectURL;
            if (prev) URL.revokeObjectURL(prev);
        } else {
            let s;
            if (!online) {
                s = 'offline';
            } else if (backendOk === false) {
                s = 'veyon_down';
            } else {
                s = 'online';
            }
            setThumbnailPlaceholder(item, iid, s);
        }
    }

    function nextThumbnailGeneration(iid) {
        state.thumbnailGeneration ??= {};
        return state.thumbnailGeneration[iid] = (state.thumbnailGeneration[iid] || 0) + 1;
    }

    function isThumbnailGenerationCurrent(iid, generation) {
        return state.thumbnailGeneration?.[iid] === generation
            && state.viewMode === 'thumbs' && state.selectedHosts.has(iid);
    }

    function setThumbnailPlaceholder(img, iid, status) {
        img.classList.add('is-placeholder');
        img.closest('.thumb-wrap')?.classList.add('placeholder-frame');
        const prev = img.dataset.blobUrl;
        delete img.dataset.blobUrl;
        // Always replace the old frame, including when the icon cache is absent.
        img.style.visibility = 'hidden';
        img.removeAttribute('src');
        img.src = state.svgCache[status] || state.svgCache.default
            || `/api/icon/${encodeURIComponent(iid)}?${tokenQuery()}&s=${status}&t=${Date.now()}`;
        if (prev) URL.revokeObjectURL(prev);
    }

    async function decodedThumbnailURL(blob) {
        if (!blob.size || !/^image\/(png|jpeg|webp)$/.test(blob.type)) {
            throw new Error('Invalid thumbnail image');
        }
        const objectURL = URL.createObjectURL(blob);
        try {
            await new Promise((resolve, reject) => {
                const image = new Image();
                image.onload = resolve;
                image.onerror = () => reject(new Error('Invalid thumbnail image'));
                image.src = objectURL;
            });
            return objectURL;
        } catch (e) {
            URL.revokeObjectURL(objectURL);
            throw e;
        }
    }

    // Универсальный SSE-зонд хостов: статус и (опционально) миниатюры.
    // wantThumb=false — только TCP-пинг, иконки загораются постепенно.
    // wantThumb=true - TCP-пинг + миниатюра выбранного источника.
    // Сервер отдаёт результаты по мере завершения каждого хоста (as_completed),
    // без батчирования на клиенте — web.thread_count виден как скорость
    // постепенного появления статусов/миниатюр.
    // Дедупликация: при новом вызове предыдущий fetch отменяется (AbortController),
    // освобождая HTTP-соединение. Только последний зонд обновляет UI.
    async function streamProbe(wantThumb) {
        if (!devicesVisible() || !state.token) return;
        if (!state.selectedHosts.size) return;
        if (state.probeAbort) {
            try { state.probeAbort.abort(); } catch (_) { /* уже отменён */ }
        }
        const ac = new AbortController();
        state.probeAbort = ac;
        const myView = state.viewMode;  // фиксируем режим на момент старта
        // Сервер идентифицирует устройства по iid (персистентный uuid).
        // Резолв iid -> сетевой адрес выполняется на backend; клиенту host
        // не нужен. Каждый iid пингуется отдельно (без дедупликации по host).
        const iids = Array.from(state.selectedHosts);
        const generations = new Map(myView === 'thumbs'
            ? iids.map(iid => [iid, nextThumbnailGeneration(iid)]) : []);
        const received = new Set();
        const failThumbnails = (status, onlyMissing = false) => {
            if (ac !== state.probeAbort || state.viewMode !== myView) return;
            for (const [iid, generation] of generations) {
                if (onlyMissing && received.has(iid)) continue;
                updateThumbImg(iid, state.hostStatuses[iid] === true, null, false,
                    state.hostThumbnailBackend[iid], status, generation);
            }
        };
        try {
            const res = await fetch('/api/hosts/probe', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'text/event-stream',
                    ...authHeaders(),
                },
                body: JSON.stringify({
                    iids: iids,
                    thumbnail: !!wantThumb,
                    ...(wantThumb ? thumbnailRequestSize() : {}),
                }),
                signal: ac.signal,
            });
            if (ac !== state.probeAbort || state.viewMode !== myView) return;
            if (!res.ok || !res.body) {
                failThumbnails('unavailable');
                if (res && res.status === 401) {
                    state.creds = null; state.token = null;
                    localStorage.removeItem('dno-tool_token');
                    updateAuthUI();
                }
                return;
            }
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buf = '';
            let completed = false;
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buf += decoder.decode(value, { stream: true });
                let idx;
                while ((idx = buf.indexOf('\n\n')) !== -1) {
                    const chunk = buf.slice(0, idx);
                    buf = buf.slice(idx + 2);
                    let eventName = 'message';
                    const dataLines = [];
                    for (const line of chunk.split('\n')) {
                        if (line.startsWith('event:')) {
                            eventName = line.slice(6).trim();
                        } else if (line.startsWith('data:')) {
                            dataLines.push(line.slice(5).trim());
                        }
                    }
                    if (eventName === 'done') { completed = true; continue; }
                    if (!dataLines.length) continue;
                    try {
                        const obj = JSON.parse(dataLines.join('\n'));
                        if (ac !== state.probeAbort) return; // устарел
                        if (!obj || typeof obj.iid !== 'string') continue;
                        const iid = obj.iid;
                        if (!iids.includes(iid) || !state.selectedHosts.has(iid) || state.viewMode !== myView) continue;
                        const generation = generations.get(iid);
                        if (myView === 'thumbs' && !isThumbnailGenerationCurrent(iid, generation)) continue;
                        received.add(iid);
                        const online = !!obj.online;
                        const backendOk = Object.prototype.hasOwnProperty.call(obj, 'backend_ok')
                            ? obj.backend_ok : obj.veyon === false ? false : null;
                        const backend = obj.backend || (obj.veyon === false ? 'veyon' : null);
                        const status = obj.thumbnail_status || null;
                        if (myView === 'icons' || myView === 'list') {
                            updateOneStatusIcon(iid, online);
                        }
                        if (myView === 'thumbs') {
                            if (wantThumb) {
                                if (obj.thumbnail && backendOk !== false && (!status || status === 'ok')) {
                                    try {
                                        const b64 = obj.thumbnail;
                                        const mime = obj.mime || 'image/png';
                                        const bstr = atob(b64);
                                        const u8 = new Uint8Array(bstr.length);
                                        for (let i = 0; i < bstr.length; i++) u8[i] = bstr.charCodeAt(i);
                                        const blob = new Blob([u8], { type: mime });
                                        const objectURL = await decodedThumbnailURL(blob);
                                        if (ac !== state.probeAbort) {
                                            URL.revokeObjectURL(objectURL);
                                            return;
                                        }
                                        updateThumbImg(iid, online, objectURL, true, backend, status, generation);
                                    } catch (_) {
                                        if (ac === state.probeAbort) {
                                            updateThumbImg(iid, online, null, false, backend, 'protocol_error', generation);
                                        }
                                    }
                                } else {
                                    updateThumbImg(iid, online, null, backendOk, backend, status, generation);
                                }
                            } else {
                                // Зонд без миниатюр сохраняет обычный статус хоста.
                                updateThumbImg(iid, online, null, null, null, null, generation);
                            }
                        }
                    } catch (_) { /* битая строка — пропускаем */ }
                }
            }
            if (!completed) throw new Error('Incomplete thumbnail probe');
            failThumbnails('unavailable', true);
        } catch (e) {
            if (e.name === 'AbortError' || ac !== state.probeAbort) return;
            failThumbnails('unavailable');
            state.wasDown = true;
            showServerDownToast();
            console.debug('SSE зонд: ошибка.', e);
        } finally {
            if (state.probeAbort === ac) state.probeAbort = null;
        }
    }

    function applyThumbnailTheme(wrap) {
        const theme = state.thumbnailTheme;
        wrap.querySelectorAll('.thumb-theme-layer').forEach(node => node.remove());
        wrap.classList.remove('monitor-frame');
        if (theme?.style !== 'monitor' || (!theme.overlay && !theme.underlay)) return;
        const valid = values => Array.isArray(values) && values.length === 4 && values.every(Number.isFinite) && values[2] > 0 && values[3] > 0;
        if (!valid(theme.viewBox) || !valid(theme.hole)) return;
        const [x, y, width, height] = theme.viewBox, [sx, sy, sw, sh] = theme.hole;
        const geometry = { left: (sx - x) / width, top: (sy - y) / height, width: sw / width, height: sh / height };
        Object.entries(geometry).forEach(([key, value]) => wrap.style.setProperty(`--thumb-screen-${key}`, `${value * 100}%`));
        for (const key of ['underlay', 'overlay']) {
            if (!theme[key]?.startsWith('data:image/')) continue;
            const layer = document.createElement('img'); layer.className = `thumb-theme-layer thumb-${key}`; layer.alt = ''; layer.setAttribute('aria-hidden', 'true'); layer.src = theme[key];
            layer.addEventListener('error', () => { wrap.classList.remove('monitor-frame'); layer.remove(); });
            wrap.append(layer);
        }
        wrap.classList.add('monitor-frame');
    }

    function buildThumbnail(iid) {
        const name = hostLabel(iid);
        const img = document.createElement('img');
        img.className = 'thumb-img';
        setDeviceImageSize(img);
        img.classList.add('is-placeholder');
        img.alt = name;
        img.loading = 'lazy';
        img.dataset.iid = iid;
        if (!state.thumbnailsAvailable) {
            const q = tokenQuery();
            const known = state.hostStatuses[iid];
            const s = known === true ? 'online' : known === false ? 'offline' : 'default';
            img.src = state.svgCache[s]
                || `/api/icon/${encodeURIComponent(iid)}?${q}&s=${s}&t=${Date.now()}`;
        } else {
            const known = state.hostStatuses[iid];
            // Недоступный выбранный источник обозначается нейтрально.
            const s = known === true
                ? (state.hostBackendOk[iid] === false ? 'veyon_down' : 'online')
                : known === false ? 'offline' : 'default';
            const cached = state.svgCache[s];
            if (cached) {
                img.src = cached;
            } else {
                const q = tokenQuery();
                img.src = `/api/icon/${encodeURIComponent(iid)}?${q}&s=${s}&t=${Date.now()}`;
            }
        }
        img.addEventListener('error', () => {
            if (img.dataset.blobUrl) {
                nextThumbnailGeneration(iid);
                setThumbnailPlaceholder(img, iid, state.hostStatuses[iid] === false ? 'offline' : 'veyon_down');
            } else {
                // A broken icon must not leave pixels from a previous frame.
                img.removeAttribute('src');
            }
        });
        img.addEventListener('load', () => {
            img.style.opacity = '1';
            img.style.visibility = '';
        });
        return img;
    }

    // Ручное обновление одной миниатюры (кнопка ⟳) по iid.
    async function reloadThumbnail(iid, thumbWrap) {
        const img = thumbWrap.querySelector('img');
        if (!img) {
            thumbWrap.innerHTML = '';
            thumbWrap.appendChild(buildThumbnail(iid));
            return;
        }
        const q = tokenQuery();
        const dimensions = thumbnailRequestSize();
        const url = `/api/thumbnail/${encodeURIComponent(iid)}?${q}&width=${dimensions.width}&height=${dimensions.height}&t=${Date.now()}`;
        const generation = nextThumbnailGeneration(iid);
        let online = state.hostStatuses[iid] === true;
        const current = () => isThumbnailGenerationCurrent(iid, generation)
            && img.isConnected && thumbWrap.querySelector('img') === img;
        const apply = (objectURL, backendOk, backend, status) => {
            if (!current()) {
                if (objectURL) URL.revokeObjectURL(objectURL);
                return;
            }
            updateThumbImg(iid, online, objectURL,
                backendOk, backend, status, generation);
        };
        try {
            const res = await fetch(url, { headers: authHeaders() });
            if (!current()) return;
            const ping = res.headers.get('X-Host-Online');
            if (ping === 'true' || ping === 'false') online = ping === 'true';
            const backend = res.headers.get('X-Thumbnail-Backend') || state.hostThumbnailBackend[iid];
            const status = res.headers.get('X-Thumbnail-Status');
            if (!res.ok || (status && status !== 'ok')) {
                apply(null, status === 'busy' || status === 'cancelled' ? null : false,
                    backend, status || 'unavailable');
                return;
            }
            const blob = await res.blob();
            if (!current()) return;
            const objectURL = await decodedThumbnailURL(blob);
            apply(objectURL, true, backend, status || 'ok');
        } catch (e) {
            apply(null, false, state.hostThumbnailBackend[iid], 'unavailable');
        }
    }

    // Автообновление миниатюр: один SSE-зонд /api/hosts/probe (thumbnail=true).
    // Сервер оркеструет параллельные TCP-пинги + Veyon-миниатюры через пулы
    // (web.thread_count) и отдаёт результаты по мере готовности (as_completed).
    // Клиент не батчирует — миниатюры подменяются постепенно, как и иконки
    // статусов. Следующий цикл запускается через thumbInterval секунд после
    // завершения предыдущего. AbortController (state.probeAbort) отменяет
    // идущий зонд при перезапуске/смене режима.
    function updateThumbTimer(immediate = false) {
        if (state.thumbTimer) {
            clearTimeout(state.thumbTimer);
            state.thumbTimer = null;
        }
        // Отменить идущий зонд, если есть.
        if (state.probeAbort) {
            try { state.probeAbort.abort(); } catch (_) { /* уже отменён */ }
            state.probeAbort = null;
        }
        if (!devicesVisible() || !state.token || state.viewMode !== 'thumbs' || !state.thumbnailsAvailable) return;

        const interval = Math.max(1, state.thumbInterval) * 1000;

        const tick = async () => {
            state.thumbTimer = null;
            if (!devicesVisible() || !state.token || state.viewMode !== 'thumbs' || !state.thumbnailsAvailable) return;
            if (!state.selectedHosts.size) return;
            // Сервер недоступен — пропускаем тик, не спам запросами.
            if (state.wasDown) {
                if (state.viewMode === 'thumbs' && state.thumbnailsAvailable && state.selectedHosts.size) {
                    state.thumbTimer = setTimeout(tick, interval);
                }
                return;
            }
            await streamProbe(true);
            // Планируем следующий цикл (даже если зонд отменили — следующий
            // тик запустит новый с актуальным списком хостов).
            if (devicesVisible() && state.token && state.viewMode === 'thumbs' && state.thumbnailsAvailable && state.selectedHosts.size) {
                state.thumbTimer = setTimeout(tick, interval);
            }
        };

        state.thumbTimer = setTimeout(tick, immediate ? 0 : interval);
    }

    // Автообновление иконок статуса (online/offline) в режимах «список» и «иконки».
    // Один SSE-зонд /api/hosts/probe (thumbnail=false) — сервер оркеструет
    // параллельные TCP-пинги и отдаёт результаты по мере готовности.
    // Дедуп: повторно используем streamProbe с его AbortController —
    // если предыдущий зонд ещё в полёте, он отменяется, освобождая соединение.
    function updateStatusTimer() {
        if (state.statusTimer) {
            clearTimeout(state.statusTimer);
            state.statusTimer = null;
        }
        // Статусы (online/offline) обновляются в режимах list и icons, а также
        // в режиме thumbs когда Veyon-миниатюры недоступны — чтобы иконки миниатюр
        // покрашались по реальному статусу хостов. При доступных миниатюрах
        // thumbs статусы обновляет updateThumbTimer (тот же SSE-зонд).
        const statusMode = state.viewMode === 'list' || state.viewMode === 'icons'
            || (state.viewMode === 'thumbs' && !state.thumbnailsAvailable);
        if (!devicesVisible() || !state.token || !statusMode) return;
        if (!state.selectedHosts.size) return;

        const STATUS_INTERVAL = 15000; // 15 сек — статусы меняются реже миниатюр
        const modeOk = () => devicesVisible() && !!state.token && (state.viewMode === 'list' || state.viewMode === 'icons'
            || (state.viewMode === 'thumbs' && !state.thumbnailsAvailable));

        const tick = async () => {
            state.statusTimer = null;
            if (!modeOk()) return;
            if (!state.selectedHosts.size) return;
            // Сервер недоступен — пропускаем тик, не спам запросами.
            if (state.wasDown) {
                if (modeOk() && state.selectedHosts.size) {
                    state.statusTimer = setTimeout(tick, STATUS_INTERVAL);
                }
                return;
            }
            await streamProbe(false);
            // Планируем следующий цикл (даже если зонд отменили — следующий
            // тик запустит новый с актуальным списком хостов).
            if (modeOk() && state.selectedHosts.size) {
                state.statusTimer = setTimeout(tick, STATUS_INTERVAL);
            }
        };

        state.statusTimer = setTimeout(tick, STATUS_INTERVAL);
    }

    function updateSelectedCount() {
        $('selectedCount').textContent = `Выбрано: ${state.selectedHosts.size}`;
    }

    // ===== Рендер дерева задач (в выпадающем popup) =====
    function renderTaskTree(tree) {
        const root = $('taskTree');
        root.innerHTML = '';
        if (!tree || !tree.length) {
            root.appendChild(el('div', 'empty-state', 'Нет задач'));
            return;
        }
        for (const node of tree) buildTaskNode(node, root, []);
        // Восстановить активную подсветку, если задача уже выбрана
        if (state.selectedTask && state.selectedTask.iid) {
            const active = root.querySelector(`.tree-item.task[data-task-iid="${CSS.escape(state.selectedTask.iid)}"]`);
            if (active) active.classList.add('active');
        }
    }

    function buildTaskNode(node, parentEl, path) {
        // Категория: один ключ -> массив
        if (node && typeof node === 'object' && !('name' in node && 'commands' in node) && Object.keys(node).length === 1) {
            const [catName] = Object.keys(node);
            const children = Array.isArray(node[catName]) ? node[catName] : [];

            const item = el('div', 'tree-item folder collapsed');
            const caret = el('span', 'caret', '▸');
            item.appendChild(caret);
            item.appendChild(el('span', 'icon', '📁'));
            item.appendChild(el('span', 'label', catName));

            const childWrap = el('div', 'tree-children hidden');
            for (const child of children) buildTaskNode(child, childWrap, path.concat(catName));

            item.addEventListener('click', () => {
                const collapsed = item.classList.toggle('collapsed');
                childWrap.classList.toggle('hidden', collapsed);
            });

            parentEl.appendChild(item);
            parentEl.appendChild(childWrap);
            return;
        }

        // Задача
        if (node && typeof node === 'object' && 'name' in node && 'commands' in node) {
            const fullPath = path.concat(node.name);
            const iid = node.iid;
            const item = el('div', 'tree-item task');
            item.appendChild(el('span', 'caret', ' '));
            item.appendChild(el('span', 'icon', '▶'));
            item.appendChild(el('span', 'label', node.name));
            item.dataset.taskName = node.name;
            item.dataset.taskIid = iid || '';
            item.dataset.taskPath = fullPath.join(' → ');
            item.dataset.hasParams = (node.params && node.params.length) ? '1' : '0';

            item.addEventListener('click', () => {
                $('taskTree').querySelectorAll('.tree-item.task.active').forEach(t => t.classList.remove('active'));
                item.classList.add('active');
                state.selectedTask = {
                    iid: iid,
                    name: node.name,
                    path: fullPath.join(' → '),
                    task: node,
                };
                // Сохранить выбранную задачу (iid для восстановления после refresh).
                try {
                    localStorage.setItem('dno-tool_selected_task', state.selectedTask.iid || '');
                } catch { /* ignore */ }
                updateTaskCombobox();
                closeTaskPopup();
            });

            parentEl.appendChild(item);
        }
    }

    // ===== Combobox задач: open/close popup =====
    function openTaskPopup() {
        $('taskPopup').classList.remove('hidden');
        $('taskCombobox').classList.add('open');
        // Фокус на поиск
        setTimeout(() => $('taskSearch').focus(), 0);
    }

    function closeTaskPopup() {
        $('taskPopup').classList.add('hidden');
        $('taskCombobox').classList.remove('open');
    }

    function toggleTaskPopup() {
        if ($('taskPopup').classList.contains('hidden')) openTaskPopup();
        else closeTaskPopup();
    }

    function updateTaskCombobox() {
        const input = $('taskComboboxInput');
        if (state.selectedTask) {
            input.textContent = state.selectedTask.path;
            input.classList.remove('placeholder');
        } else {
            input.textContent = 'Выберите задачу...';
            input.classList.add('placeholder');
        }
    }

    // ===== Информация о задаче =====
    // Разобрать sftp-строку формата «source::destination» на источник и приёмник.
    function parseSftpText(raw) {
        const text = (raw || '').trim();
        if (!text) return null;
        const parts = text.split('::');
        if (parts.length < 2) {
            return { from: parts[0] || '', to: '', single: true };
        }
        return {
            from: parts[0].trim(),
            to: parts.slice(1).join('::').trim(),
        };
    }

    // Сторона вида user@host:path — удалённая, иначе — локальная.
    function isRemoteSide(side) {
        return /^[^:/@]+@[^:]+:/.test(side || '');
    }

    // Извлечь только путь из стороны, отбросив префикс user@host:.
    function pathOf(side) {
        if (!side) return '';
        const m = side.match(/^[^:/@]+@[^:]+:(.*)$/);
        return m ? m[1] : side;
    }

    // Извлечь имя файла из пути (что передаётся).
    function basenameOf(side) {
        const path = pathOf(side);
        const norm = path.replace(/\\/g, '/').replace(/\/+$/, '');
        const idx = norm.lastIndexOf('/');
        return idx >= 0 ? norm.slice(idx + 1) : norm;
    }

    // Построить карточку sftp-команды: бейдж типа + три поля (Что / Откуда / Куда).
    // login@host скрыт — показывается только путь и сторона (Локально / Удалённо).
    function buildSftpRow(raw, command, task) {
        const item = el('div', 'command-row command-row-sftp');
        item.appendChild(commandModeIcons(command, task));

        const parsed = parseSftpText(raw);
        if (!parsed) {
            item.appendChild(el('code', 'command-text', raw || ''));
            return item;
        }
        if (parsed.single) {
            const box = el('div', 'sftp-transfer');
            box.appendChild(buildSftpField('Что', basenameOf(parsed.from), null));
            item.appendChild(box);
            return item;
        }

        const box = el('div', 'sftp-transfer');
        box.appendChild(buildSftpField('Что', basenameOf(parsed.from), null));
        box.appendChild(el('div', 'sftp-arrow', '↓'));
        box.appendChild(buildSftpField('Откуда', pathOf(parsed.from), isRemoteSide(parsed.from) ? 'Удалённый компьютер' : 'Локальный компьютер'));
        box.appendChild(el('div', 'sftp-arrow', '↓'));
        box.appendChild(buildSftpField('Куда', pathOf(parsed.to), isRemoteSide(parsed.to) ? 'Удалённый компьютер' : 'Локальный компьютер'));
        item.appendChild(box);
        return item;
    }

    function buildSftpField(label, value, sideLabel) {
        const wrap = el('div', 'sftp-field');
        wrap.appendChild(el('div', 'sftp-field-label', label));
        wrap.appendChild(el('code', 'command-text', value));
        if (sideLabel) {
            const side = el('span', 'sftp-side-tag', sideLabel);
            wrap.appendChild(side);
        }
        return wrap;
    }

    function commandModeIcons(command, task) {
        const group = el('div', 'command-mode-icons');
        const type = command.type || 'ssh';
        const modes = [[`command_${type}`, { ssh: 'SSH — команда на удалённом компьютере', local: 'Команда на локальном компьютере', sftp: 'Передача файлов по SFTP' }[type] || type]];
        if (type === 'ssh') {
            const desktop = typeof command.desktop_session === 'boolean' ? command.desktop_session : task.desktop_session === true;
            if (desktop) modes.push(['command_desktop_session', 'В окружении пользователя рабочего стола']);
            if (command.background === true) modes.push(['command_background', 'Запустить и не ждать']);
        }
        for (const [key, label] of modes) {
            const badge = el('span', 'command-mode-icon');
            badge.tabIndex = 0; badge.title = label; badge.dataset.tip = label;
            badge.setAttribute('role', 'img'); badge.setAttribute('aria-label', label);
            if (window.DNODesktop) badge.append(window.DNODesktop.icon(key));
            else badge.textContent = type.toUpperCase();
            group.append(badge);
        }
        return group;
    }

    let taskHighlightController = null;
    let taskHighlightRevision = 0;
    function closeTaskInfo() {
        ++taskHighlightRevision;
        taskHighlightController?.abort();
        $('taskInfoModal').classList.add('hidden');
    }
    async function highlightCommandPreview(entries, revision) {
        taskHighlightController = new AbortController();
        const token = state.token;
        const roles = new Set(['keyword', 'command', 'comment', 'string', 'variable', 'number', 'operator', 'parameter']);
        try {
            const data = await api('/api/tools/command-highlight', {
                method: 'POST', signal: taskHighlightController.signal,
                body: JSON.stringify({ commands: entries.map(item => ({ type: item.type, text: item.text })) }),
            });
            if (revision !== taskHighlightRevision || token !== state.token || $('taskInfoModal').classList.contains('hidden')) return;
            entries.forEach((item, index) => {
                const tokens = data.commands?.[index]?.tokens;
                if (!item.node.isConnected || !Array.isArray(tokens) || tokens.length > 20000 || tokens.some(part => typeof part.text !== 'string') || tokens.map(part => part.text).join('') !== item.text) return;
                const fragment = document.createDocumentFragment();
                for (const part of tokens) fragment.append(roles.has(part.kind) ? el('span', `syntax-${part.kind}`, part.text) : document.createTextNode(part.text));
                item.node.replaceChildren(fragment);
            });
        } catch { /* Исходный текст остаётся доступным при недоступной подсветке. */ }
    }

    function showTaskInfo() {
        if (!state.selectedTask) {
            alert('Выберите задачу в дереве');
            return;
        }
        const t = state.selectedTask.task;
        taskHighlightController?.abort();
        const revision = ++taskHighlightRevision, highlights = [];
        let highlightBytes = 0;
        $('taskInfoTitle').textContent = `Информация: ${state.selectedTask.path}`;
        const body = $('taskInfoBody');
        body.innerHTML = '';

        const fields = [
            ['Имя', t.name],
            ['Описание', t.description || '—'],
            ['Таймаут', (t.timeout != null ? t.timeout + ' сек' : '—')],
        ];
        for (const [label, val] of fields) {
            const f = el('div', 'field');
            f.appendChild(el('div', 'field-label', label));
            f.appendChild(el('div', null, String(val)));
            body.appendChild(f);
        }

        if (t.params && t.params.length) {
            const f = el('div', 'field');
            f.appendChild(el('div', 'field-label', 'Параметры'));
            const wrap = el('div', null);
            for (const p of t.params) wrap.appendChild(el('span', 'param', p));
            f.appendChild(wrap);
            body.appendChild(f);
        }

        if (t.commands && t.commands.length) {
            const f = el('div', 'field');
            f.appendChild(el('div', 'field-label', 'Команды'));
            const list = el('div', 'command-list');
            for (const source of t.commands || []) {
                const c = typeof source === 'string' ? { type: /^sftp\b/.test(source) ? 'sftp' : /^local\b/.test(source) ? 'local' : 'ssh', text: source } : source || {};
                const type = c.type || 'ssh';
                if (type === 'sftp') {
                    list.appendChild(buildSftpRow(c.text || '', c, t));
                } else {
                    const item = el('div', 'command-row command-row-terminal');
                    const head = el('div', 'terminal-head');
                    head.appendChild(commandModeIcons(c, t));
                    item.appendChild(head);
                    const code = el('code', 'command-text');
                    const text = c.text || '';
                    code.textContent = text;
                    const bytes = new TextEncoder().encode(text).length;
                    if (['ssh', 'local'].includes(type) && bytes <= 131072 && highlights.length < 64 && highlightBytes + bytes <= 524288) {
                        highlights.push({ node: code, text, type }); highlightBytes += bytes;
                    }
                    item.appendChild(code);
                    list.appendChild(item);
                }
            }
            f.appendChild(list);
            body.appendChild(f);
        }

        $('taskInfoModal').classList.remove('hidden');
        if (highlights.length) highlightCommandPreview(highlights, revision);
    }

    // ===== Выполнение =====
    function wsUrl(opts = {}) {
        //构建 WS URL: token + session + опционально job_id/last_seq (для reconnect).
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        let url = `${proto}//${location.host}/ws/execute?token=${encodeURIComponent(state.token || '')}`;
        if (state.sessionId) url += `&session=${encodeURIComponent(state.sessionId)}`;
        if (opts.jobId) url += `&job_id=${encodeURIComponent(opts.jobId)}`;
        if (opts.lastSeq != null) url += `&last_seq=${encodeURIComponent(opts.lastSeq)}`;
        return url;
    }

    function ensureWs(opts = {}) {
        return new Promise((resolve, reject) => {
            if (state.ws && state.ws.readyState === WebSocket.OPEN) return resolve(state.ws);
            if (!state.token) return reject(new Error('Нет авторизации'));

            const url = wsUrl(opts);
            const ws = new WebSocket(url);
            state.ws = ws;

            ws.onopen = () => resolve(ws);
            ws.onerror = () => {
                state.wasDown = true;
                showServerDownToast();
                reject(new Error('Ошибка WebSocket'));
            };
            ws.onmessage = (ev) => handleWsMessage(ev.data);
            ws.onclose = () => {
                state.ws = null;
                if (state.running) setRunStatus('idle', 'Соединение закрыто');
                // WebSocket закрылся не из-за завершения команды — возможно, сервер упал.
                // heartbeat подтвердит/опровергнет; пока покажем тост немедленно.
                if (!state.running) {
                    state.wasDown = true;
                    showServerDownToast();
                }
            };
        });
    }

    function setRunStatus(cls, text) {
        const s = $('runStatus');
        const badge = $('jobsBadge');
        if (s) s.textContent = text;
        if (badge) badge.className = 'jobs-badge status-' + cls;
    }

    const VEYON_FEATURE_LABELS = {
        lock: 'Блокировка экрана',
        unlock: 'Разблокировка экрана',
        powerdown: 'Выключение питания',
        reboot: 'Перезагрузка',
        logoff: 'Завершение сеанса',
        logoff_webapi: 'Завершение сеанса (WebAPI)',
        lock_input: 'Блокировка ввода',
        unlock_input: 'Разблокировка ввода',
        login: 'Вход пользователя (Veyon)',
        login_ssh: 'Вход пользователя (DBus)',
        website: 'Открытие сайта',
        start_app: 'Запуск программы',
        message: 'Отправка сообщения',
        power_on: 'Включение (WoL)',
        restart_service: 'Перезагрузка сервиса Veyon',
        ssh_lock: 'Блокировка экрана (SSH)',
        ssh_unlock: 'Разблокировка экрана (SSH)',
        ssh_lock_input: 'Блокировка ввода (SSH)',
        ssh_unlock_input: 'Разблокировка ввода (SSH)',
    };

    const VEYON_FEATURE_PROMPTS = {
        login: [
            { key: 'username', label: 'Имя пользователя', type: 'text', required: true },
            { key: 'password', label: 'Пароль', type: 'password', required: true },
        ],
        login_ssh: [
            { key: 'username', label: 'Имя пользователя', type: 'text', required: true },
            { key: 'password', label: 'Пароль', type: 'password', required: true },
        ],
        website: [
            { key: 'url', label: 'URL', type: 'text', required: true, placeholder: 'https://example.com' },
        ],
        start_app: [
            { key: 'cmd', label: 'Командная строка', type: 'text', required: true, placeholder: 'firefox' },
        ],
        message: [
            { key: 'text', label: 'Текст сообщения', type: 'textarea', required: true },
        ],
    };

    async function veyonFeatureAll(feature) {
        if (!state.selectedHosts.size) return;
        const label = VEYON_FEATURE_LABELS[feature] || feature;
        const iids = [...state.selectedHosts];
        const total = iids.length;

        let arguments_ = undefined;
        const promptSpec = VEYON_FEATURE_PROMPTS[feature];
        if (promptSpec) {
            arguments_ = await askVeyonFeatureArgs(promptSpec, label);
            if (arguments_ === null) return;
        }

        // Визуальная обратная связь: кнопка в состоянии выполнения
        const btn = document.querySelector(`#veyonFeatures .vfeat-btn[data-feature="${feature}"], #ctrlFeatures .vfeat-btn[data-feature="${feature}"]`);
        if (btn) btn.classList.add('vfeat-loading');

        // power_on (Wake-on-LAN) применим и к оффлайн-хостам — это его цель.
        // Для остальных feature оффлайн-хосты пропускаем сразу (fail), чтобы
        // не блокировать пул команд 10-секундным wait_for_framebuffer таймаутом.
        const skipOffline = feature !== 'power_on';

        let ok = 0, fail = 0;
        let firstError = '';
        const promises = iids.map(iid => {
            if (skipOffline && state.hostStatuses[iid] === false) {
                fail++;
                return Promise.resolve();
            }
            const payload = arguments_
                ? { iid, feature, arguments: arguments_ }
                : { iid, feature };
            return fetch('/api/veyon/feature', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    ...authHeaders(),
                },
                body: JSON.stringify(payload),
            }).then(async res => {
                // HTTP 200 с success:false — отказ на хосте, а не успех.
                const data = res.ok ? await res.json().catch(() => ({})) : {};
                if (res.ok && data.success !== false) {
                    ok++;
                } else {
                    fail++;
                    if (!firstError && data.error) firstError = String(data.error);
                }
            }).catch(() => { fail++; });
        });

        await Promise.all(promises);

        if (btn) btn.classList.remove('vfeat-loading');

        if (fail === 0) {
            showToast({ title: `${label} → ${ok} устройств(а)`, kind: 'success', timeout: 3000 });
        } else if (ok === 0) {
            const reason = firstError ? `: ${firstError}` : '';
            showToast({ title: `${label} — ошибка`, text: `Не удалось выполнить на ${fail} устройстве(ах)${reason}`, kind: 'error', timeout: 6000 });
        } else {
            const reason = firstError ? ` (${firstError})` : '';
            showToast({ title: `${label} — частично`, text: `Успешно: ${ok}, ошибок: ${fail}${reason}`, kind: 'warn', timeout: 6000 });
        }
    }

    function askVeyonFeatureArgs(spec, title) {
        return new Promise((resolve) => {
            const form = $('paramsForm');
            form.innerHTML = '';
            const inputs = [];
            spec.forEach((p, i) => {
                const label = el('label', null, p.label);
                let input;
                if (p.type === 'textarea') {
                    input = document.createElement('textarea');
                    input.rows = 4;
                    input.style.width = '100%';
                    input.style.resize = 'vertical';
                } else {
                    input = document.createElement('input');
                    input.type = p.type || 'text';
                }
                input.name = p.key;
                if (p.placeholder) input.placeholder = p.placeholder;
                if (p.required) input.required = true;
                label.appendChild(input);
                form.appendChild(label);
                inputs.push({ key: p.key, input, required: p.required });
            });
            $('paramsModalTitle').textContent = title;
            $('paramsModal').classList.remove('hidden');

            const ok = () => {
                const result = {};
                for (const { key, input, required } of inputs) {
                    const val = (input.value || '').trim();
                    if (required && !val) {
                        input.focus();
                        return;
                    }
                    result[key] = val;
                }
                cleanup();
                $('paramsModal').classList.add('hidden');
                resolve(result);
            };
            const cancel = () => {
                cleanup();
                $('paramsModal').classList.add('hidden');
                resolve(null);
            };
            const onSubmit = (e) => { e.preventDefault(); ok(); };
            const onKeydown = (e) => {
                if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancel(); }
            };
            function cleanup() {
                $('paramsOk').removeEventListener('click', ok);
                $('paramsCancel').removeEventListener('click', cancel);
                form.removeEventListener('submit', onSubmit);
                form.removeEventListener('keydown', onKeydown);
            }
            $('paramsOk').addEventListener('click', ok);
            $('paramsCancel').addEventListener('click', cancel);
            form.addEventListener('submit', onSubmit);
            form.addEventListener('keydown', onKeydown);
            const first = inputs[0];
            if (first) setTimeout(() => first.input.focus(), 50);
        });
    }

    async function onExecute() {
        if (state.running || state.preparingExecution) return;
        if (!state.selectedHosts.size) {
            alert('Выберите хотя бы одно устройство');
            return;
        }
        if (!state.selectedTask) {
            alert('Выберите задачу в дереве');
            return;
        }

        // Задача и хосты фиксируются до диалога и ожидания WebSocket.
        const selected = state.selectedTask;
        const request = {
            task_iid: selected.iid,
            name: selected.name,
            hosts: Array.from(state.selectedHosts),
        };
        state.preparingExecution = true;
        $('executeBtn').disabled = true;
        try {
            const labels = selected.task.params;
            const selection = labels && labels.length ? await askParams(labels, request.task_iid) : { params: {}, uploads: {} };
            if (selection === null) return;
            await _doExecute(selection, request);
        } finally {
            state.preparingExecution = false;
            $('executeBtn').disabled = false;
        }
    }

    async function _doExecute(selection, request) {
        try {
            await ensureWs();
        } catch (e) {
            for (const token of Object.values(selection.uploads)) {
                api(`/api/task-uploads/${encodeURIComponent(token)}`, { method: 'DELETE' }).catch(() => {});
            }
            alert('Не удалось подключиться: ' + errorMessage(e));
            return;
        }

        // Очистка результатов
        state.results.clear();
        $('resultsArea').innerHTML = '';
        state.currentJobId = null;
        state.currentJobSeq = 0;

        state.running = true;
        $('executeBtn').classList.add('hidden');
        $('abortBtn').classList.remove('hidden');
        const totalHosts = request.hosts.length;
        setRunStatus('running', `${request.name} 0/${totalHosts}`);

        // Заглушки-карточки для каждого устройства (по iid)
        for (const iid of request.hosts) {
            addResultCard(iid, 'pending', 'Ожидание');
        }

        state.ws.send(JSON.stringify({
            action: 'execute',
            task_iid: request.task_iid,
            hosts: request.hosts,
            params: selection.params,
            uploads: selection.uploads,
        }));
    }

    function onAbort() {
        // Прервать просматриваемое задание. Если это новое задание (основной WS)
        // — шлём abort через state.ws. Если viewer — через /api/jobs/{id}/abort.
        const targetId = state.viewingJobId || state.currentJobId;
        if (!targetId) return;
        if (state.ws && state.ws.readyState === WebSocket.OPEN && targetId === state.currentJobId) {
            state.ws.send(JSON.stringify({ action: 'abort' }));
        } else {
            // Viewer-режим — POST /api/jobs/{id}/abort.
            api(`/api/jobs/${encodeURIComponent(targetId)}/abort`, { method: 'POST' })
                .then(r => {
                    if (!r || !r.success) {
                        showToast({ title: 'Не удалось прервать задание', kind: 'warn', timeout: 3000 });
                    }
                })
                .catch(e => {
                    console.warn('onAbort:', e);
                    showToast({ title: 'Ошибка прерывания', kind: 'error', timeout: 3000 });
                });
        }
    }

    function finishRun() {
        state.running = false;
        $('executeBtn').classList.remove('hidden');
        $('abortBtn').classList.add('hidden');
    }

    // ===== Результаты =====
    function addResultCard(iid, status, label) {
        const area = $('resultsArea');
        const empty = area.querySelector('.empty-state');
        if (empty) empty.remove();

        const card = el('div', `result-card ${status}`);
        card.dataset.iid = iid;

        const header = el('div', 'result-header');
        header.appendChild(el('h3', null, hostLabel(iid)));
        const badge = el('span', `status-badge badge-${status}`, label);
        header.appendChild(badge);
        header.addEventListener('click', () => card.classList.toggle('expanded'));
        header.dataset.tip = hostOf(iid) || iid;

        const body = el('div', 'result-body');

        card.appendChild(header);
        card.appendChild(body);
        area.appendChild(card);

        state.results.set(iid, { card, body, status, output: '' });
        return { card, body };
    }

    // A small, read-only terminal line renderer. Keep parser state across WS chunks:
    // CRLF and escape sequences may be split anywhere in the stream.
    class CommandOutputText {
        constructor() {
            this.completed = '';
            this.line = [];
            this.column = 0;
            this.mode = 'text';
            this.parameters = '';
        }

        write(chunk) {
            for (const ch of chunk) {
                if (this.mode === 'osc' || this.mode === 'oscEscape') {
                    if (ch === '\x07' || (this.mode === 'oscEscape' && ch === '\\')) {
                        this.mode = 'text';
                    } else {
                        this.mode = ch === '\x1b' ? 'oscEscape' : 'osc';
                    }
                    continue;
                }
                if (this.mode === 'escape') {
                    this.mode = ch === '[' ? 'csi' : ch === ']' ? 'osc' : 'text';
                    this.parameters = '';
                    continue;
                }
                if (this.mode === 'csi') {
                    if (ch >= '@' && ch <= '~') {
                        this.control(ch);
                        this.mode = 'text';
                    } else if (this.parameters.length < 128) {
                        this.parameters += ch;
                    } else {
                        this.mode = 'text';
                    }
                    continue;
                }
                if (ch === '\x1b') this.mode = 'escape';
                else if (ch === '\r') this.column = 0;
                else if (ch === '\n') {
                    this.completed += this.line.join('') + '\n';
                    this.line = [];
                    this.column = 0;
                } else if (ch === '\b') this.column = Math.max(0, this.column - 1);
                else if (ch === '\t' || ch >= ' ' && ch !== '\x7f') {
                    while (this.line.length < this.column) this.line.push(' ');
                    this.line[this.column++] = ch;
                }
            }
        }

        control(final) {
            // Colours and unsupported cursor/screen controls are consumed, not printed.
            if (!/^\d*(;\d*)*$/.test(this.parameters)) return;
            const n = Number(this.parameters.split(';')[0] || 0);
            if (final === 'K') {
                if (n === 0) this.line.length = Math.min(this.line.length, this.column);
                else if (n === 1) {
                    for (let i = 0; i <= Math.min(this.column, this.line.length - 1); i++) this.line[i] = ' ';
                } else if (n === 2) this.line = [];
            } else if (final === 'G') this.column = Math.min(16384, Math.max(1, n) - 1);
            else if (final === 'C') this.column = Math.min(16384, this.column + Math.max(1, n));
            else if (final === 'D') this.column = Math.max(0, this.column - Math.max(1, n));
        }

        text() {
            return this.completed + this.line.join('');
        }
    }

    function setResultOutput(result, output) {
        if (!result.outputText || !output.startsWith(result.output)) {
            result.outputText = new CommandOutputText();
            result.outputText.write(output);
        } else {
            result.outputText.write(output.slice(result.output.length));
        }
        // Preserve the original stream; final snapshots/replay use the same renderer.
        result.output = output;
        result.body.textContent = result.outputText.text();
    }

    function updateResultCard(iid, status, label, output) {
        const r = state.results.get(iid);
        if (!r) return;
        r.status = status;
        const wasExpanded = r.card.classList.contains('expanded');
        r.card.className = `result-card ${status}`;
        if (wasExpanded) r.card.classList.add('expanded');
        const badge = r.card.querySelector('.status-badge');
        if (badge) {
            badge.className = `status-badge badge-${status}`;
            badge.textContent = label;
        }
        if (output != null) {
            setResultOutput(r, output);
        }
    }

    function appendProgress(iid, chunk) {
        const r = state.results.get(iid);
        if (!r) return;
        setResultOutput(r, r.output + chunk);
        r.body.scrollTop = r.body.scrollHeight;
    }

    function handleWsMessage(raw) {
        let msg;
        try { msg = JSON.parse(raw); } catch { return; }
        // Маршрутизация по job_id — каждое событие несёт job_id (сервер
        // добавляет во все события). Это позволяет множественные viewer-WS
        // и обновление buffers для всех активных заданий одновременно.
        const jobId = msg.job_id || state.currentJobId;
        const isViewing = jobId && jobId === state.viewingJobId;

        // Сохранить seq в persisted (для точного reconnect этого задания).
        // Snapshot-события (снимок состояний хостов при reconnect) не учитываем —
        // их seq может быть меньше last_seq и прыжок назад «пропустит» live-события.
        if (msg.seq != null && jobId && !msg.snapshot) {
            persistJobSeq(jobId, msg.seq);
        }

        // Применить событие к буферу задания (всегда — даже если не отображается).
        if (jobId) {
            applyEventToBuffer(jobId, msg);
            // При start нового задания (через основной WS) — зарегистрировать его.
            if (msg.type === 'start' && msg.job_id) {
                const jid = msg.job_id;
                state.currentJobId = jid;
                // Добавить в activeJobs если ещё нет.
                if (!state.activeJobs.some(j => j.id === jid)) {
                    state.activeJobs.push({
                        id: jid,
                        task_iid: msg.task_iid || '',
                        task_name: msg.task_name || '',
                        device_iids: msg.device_iids || [],
                        status: 'running',
                        created_at: Date.now() / 1000,
                        latest_seq: msg.seq || 0,
                        progress: { done: 0, total: (msg.device_iids || []).length },
                    });
                }
                persistJobSeq(jid, msg.seq || 0);
                // Сделать просматриваемым (переключить UI на новое задание).
                state.viewingJobId = jid;
                updateActiveJobsBadge();
                renderActiveJobsPanel();
            }
        }

        // Обновить видимые карточки только если событие от просматриваемого задания.
        if (isViewing && msg.device_iid) {
            syncCardFromBuffer(jobId, msg.device_iid);
            // Обновить прогресс в статус-индикаторе при завершении устройства.
            if (msg.type === 'device_result' && state.running) {
                const buf2 = state.jobBuffers.get(jobId);
                if (buf2) {
                    const done = Array.from(buf2.results.values()).filter(r =>
                        r.status === 'success' || r.status === 'error').length;
                    setRunStatus('running', `${buf2.task_name} ${done}/${buf2.device_iids.length}`);
                }
            }
        }
        // Обработка статус-индикатора (без device_iid — start/finished/aborted/truncated/error).
        if (isViewing) {
            const buf = jobId ? state.jobBuffers.get(jobId) : null;
            switch (msg.type) {
                case 'start':
                    setRunStatus('running',
                        `${msg.task_name || (buf && buf.task_name) || 'Задача'} 0/${(msg.device_iids || (buf && buf.device_iids) || []).length}`);
                    break;
                case 'finished':
                    finishRun();
                    {
                        const s = msg.summary || {};
                        const taskName = (buf && buf.task_name) || 'Задача';
                        setRunStatus(s.error > 0 ? 'error' : 'done',
                            `${taskName} ${s.success || 0}/${s.total || (buf && buf.device_iids.length) || 0}`);
                    }
                    break;
                case 'aborted':
                    finishRun();
                    {
                        const buf2 = state.jobBuffers.get(jobId);
                        setRunStatus('idle', `${(buf2 && buf2.task_name) || 'Задача'} прервано`);
                        // Обновить все карточки этого задания (незавершённые → Отменено).
                        if (buf2) {
                            for (const iid of buf2.device_iids) syncCardFromBuffer(jobId, iid);
                        }
                    }
                    break;
                case 'truncated':
                    showToast({ title: 'Часть истории утеряна', kind: 'warn', timeout: 4000 });
                    break;
                case 'error':
                    if (msg.device_iid) {
                        appendProgress(msg.device_iid, `\nОшибка: ${msg.message}\n`);
                    } else {
                        console.error('Server error:', msg.message);
                        if (state.running) {
                            finishRun();
                            setRunStatus('error', 'Ошибка');
                        }
                    }
                    break;
                case 'pong':
                    break;
            }
        }

        // Обработка завершения задания (для всех WS — и основного, и viewer).
        if (jobId && (msg.type === 'finished' || msg.type === 'aborted')) {
            // Перенести из active в recent (для replay после refresh).
            state.activeJobs = state.activeJobs.filter(j => j.id !== jobId);
            const buf = state.jobBuffers.get(jobId);
            const done = buf ? Array.from(buf.results.values()).filter(r =>
                r.status === 'success' || r.status === 'error').length : 0;
            const total = buf ? buf.device_iids.length : 0;
            state.recentJobs = state.recentJobs.filter(j => j.id !== jobId);
            state.recentJobs.unshift({
                id: jobId,
                task_iid: buf ? buf.task_iid : '',
                task_name: buf ? buf.task_name : '',
                device_iids: buf ? buf.device_iids : [],
                status: msg.type,
                created_at: Date.now() / 1000,
                finished_at: Date.now() / 1000,
                summary: msg.summary || null,
                latest_seq: msg.seq || 0,
                truncated: false,
                progress: { done, total },
            });
            renderActiveJobsPanel();
            updateActiveJobsBadge();
            clearPersistedJob(jobId);
            persistRecentJob(jobId);
            // Закрыть viewer-WS этого задания (если есть).
            const vws = state.jobWs.get(jobId);
            if (vws) {
                try { vws.close(); } catch {}
                state.jobWs.delete(jobId);
            }
            // Если был основной WS на это задание — сбросить currentJobId.
            if (state.currentJobId === jobId) {
                state.currentJobId = null;
                state.currentJobSeq = 0;
            }
            // Если отображалось это задание — снять статус running.
            if (state.viewingJobId === jobId) {
                state.viewingJobId = null;
            }
        }
    }

    // ===== Persistence активных заданий (для reconnect после refresh) =====
    // В localStorage храним массив [{id, seq}, ...] для всех активных заданий.
    // Миграция со старого формата (dno-tool_job_id + dno-tool_job_seq) — при первом
    // чтении переносим в новый массив.
    function loadPersistedJobs() {
        let raw = localStorage.getItem('dno-tool_job_ids');
        if (raw) {
            try {
                const arr = JSON.parse(raw);
                if (Array.isArray(arr)) return arr;
            } catch { /* ignore */ }
        }
        // Миграция со старого формата.
        const oldId = localStorage.getItem('dno-tool_job_id');
        if (oldId) {
            const oldSeq = parseInt(localStorage.getItem('dno-tool_job_seq') || '0', 10) || 0;
            const arr = [{ id: oldId, seq: oldSeq }];
            localStorage.setItem('dno-tool_job_ids', JSON.stringify(arr));
            localStorage.removeItem('dno-tool_job_id');
            localStorage.removeItem('dno-tool_job_seq');
            return arr;
        }
        return [];
    }

    function persistJobSeq(jobId, seq) {
        const arr = loadPersistedJobs();
        const entry = arr.find(e => e.id === jobId);
        if (entry) entry.seq = seq;
        else arr.push({ id: jobId, seq });
        localStorage.setItem('dno-tool_job_ids', JSON.stringify(arr));
    }

    function clearPersistedJob(jobId) {
        const arr = loadPersistedJobs();
        const filtered = arr.filter(e => e.id !== jobId);
        localStorage.setItem('dno-tool_job_ids', JSON.stringify(filtered));
    }

    function clearAllPersistedJobs() {
        localStorage.removeItem('dno-tool_job_ids');
        localStorage.removeItem('dno-tool_job_id');
        localStorage.removeItem('dno-tool_job_seq');
    }

    // ===== Persistence завершённых заданий (recent — для replay после refresh) =====
    // В localStorage храним массив [{id, ts}, ...] для недавно завершённых заданий.
    // TTL = серверному job_ttl_minutes (0 = ∞). При истечении запись вычищается
    // на старте (clearExpiredRecentJobs). Если сервер уже вычистил задание (404),
    // запись тоже удаляется (clearRecentJob).
    const RECENT_JOBS_KEY = 'dno-tool_recent_jobs';

    function loadRecentJobsPersisted() {
        let raw = localStorage.getItem(RECENT_JOBS_KEY);
        if (raw) {
            try {
                const arr = JSON.parse(raw);
                if (Array.isArray(arr)) return arr;
            } catch { /* ignore */ }
        }
        return [];
    }

    function persistRecentJob(jobId) {
        const arr = loadRecentJobsPersisted();
        const entry = arr.find(e => e.id === jobId);
        if (entry) entry.ts = Date.now();
        else arr.push({ id: jobId, ts: Date.now() });
        localStorage.setItem(RECENT_JOBS_KEY, JSON.stringify(arr));
    }

    function clearRecentJob(jobId) {
        const arr = loadRecentJobsPersisted();
        const filtered = arr.filter(e => e.id !== jobId);
        localStorage.setItem(RECENT_JOBS_KEY, JSON.stringify(filtered));
    }

    function clearExpiredRecentJobs(ttlMinutes) {
        // 0 или отрицательное = ∞ (не вычищать).
        if (!ttlMinutes || ttlMinutes <= 0) return;
        const ttlMs = ttlMinutes * 60 * 1000;
        const now = Date.now();
        const arr = loadRecentJobsPersisted();
        const fresh = arr.filter(e => (now - (e.ts || 0)) < ttlMs);
        if (fresh.length !== arr.length) {
            localStorage.setItem(RECENT_JOBS_KEY, JSON.stringify(fresh));
        }
    }

    // ===== Buffers заданий (для переключения между ними) =====
    // jobBuffer: { device_iids: [], results: Map(iid -> {status,label,output}), summary, status, task_name, task_iid }
    function getJobBuffer(jobId) {
        let buf = state.jobBuffers.get(jobId);
        if (!buf) {
            buf = {
                device_iids: [],
                results: new Map(),
                summary: null,
                status: 'pending',
                task_name: '',
                task_iid: '',
            };
            state.jobBuffers.set(jobId, buf);
        }
        return buf;
    }

    function applyEventToBuffer(jobId, msg) {
        const buf = getJobBuffer(jobId);
        const iid = msg.device_iid;
        switch (msg.type) {
            case 'start':
                buf.status = 'running';
                buf.task_iid = msg.task_iid || buf.task_iid;
                buf.task_name = msg.task_name || buf.task_name;
                buf.device_iids = msg.device_iids || buf.device_iids;
                for (const d of buf.device_iids) {
                    if (!buf.results.has(d)) {
                        buf.results.set(d, { status: 'pending', label: 'Ожидание', output: '' });
                    }
                }
                break;
            case 'device_start':
                if (iid) buf.results.set(iid, { status: 'running', label: 'Выполняется', output: '' });
                break;
            case 'command_start':
                if (iid) {
                    const r = buf.results.get(iid);
                    if (r) {
                        if (r.status === 'pending') {
                            r.status = 'running';
                            r.label = 'Выполняется';
                        }
                        r.output += `\n$ [${msg.command_type}] ${msg.command}\n`;
                    }
                }
                break;
            case 'progress':
                if (iid) {
                    const r = buf.results.get(iid);
                    if (r) {
                        if (r.status === 'pending') {
                            r.status = 'running';
                            r.label = 'Выполняется';
                        }
                        r.output += (msg.chunk || '');
                    }
                }
                break;
            case 'device_result':
                if (iid) {
                    const r = buf.results.get(iid);
                    if (r) {
                        r.status = msg.success ? 'success' : 'error';
                        r.label = msg.success ? 'Успех' : 'Ошибка';
                        if (msg.output != null) r.output = msg.output;
                    }
                }
                break;
            case 'finished':
                buf.status = 'finished';
                buf.summary = msg.summary || null;
                break;
            case 'aborted':
                buf.status = 'aborted';
                for (const r of buf.results.values()) {
                    if (r.status === 'pending' || r.status === 'running') {
                        r.status = 'error';
                        r.label = 'Отменено';
                    }
                }
                break;
        }
    }

    function renderResultsFromBuffer(jobId) {
        const buf = state.jobBuffers.get(jobId);
        state.results.clear();
        $('resultsArea').innerHTML = '';
        if (!buf) return;
        for (const iid of buf.device_iids) {
            syncCardFromBuffer(jobId, iid);
        }
    }

    function syncCardFromBuffer(jobId, iid) {
        const buf = state.jobBuffers.get(jobId);
        if (!buf) return;
        const r = buf.results.get(iid);
        const status = r ? r.status : 'pending';
        const label = r ? r.label : 'Ожидание';
        const output = r ? r.output : '';
        if (!state.results.get(iid)) {
            addResultCard(iid, status, label);
        }
        updateResultCard(iid, status, label, output);
    }

    // ===== Сохранение выбранных хостов на сервере =====
    function saveSelectedHosts() {
        // Локальное сохранение как fallback (iid персистентны — выбор стабилен между рестартами).
        try {
            localStorage.setItem('dno-tool_selected_hosts', JSON.stringify(Array.from(state.selectedHosts)));
        } catch { /* ignore */ }
        if (!state.sessionId || !state.token) return;
        if (state._hostsSaveTimer) clearTimeout(state._hostsSaveTimer);
        state._hostsSaveTimer = setTimeout(async () => {
            try {
                await api('/api/session/hosts', {
                    method: 'POST',
                    body: JSON.stringify({ hosts: Array.from(state.selectedHosts) }),
                });
            } catch (e) {
                console.debug('saveSelectedHosts:', e);
            }
        }, 300);
    }

    async function restoreSelectedHosts() {
        if (!state.token) return;
        let iids = [];
        // 1. Серверное восстановление (если сессия ещё жива).
        if (state.sessionId) {
            try {
                const data = await api('/api/session/hosts');
                if (data && Array.isArray(data.hosts) && data.hosts.length > 0) {
                    iids = data.hosts;
                }
            } catch (e) {
                console.debug('restoreSelectedHosts (server):', e);
            }
        }
        // 2. Fallback на localStorage (iid из предыдущей сессии браузера).
        if (iids.length === 0) {
            try {
                const saved = localStorage.getItem('dno-tool_selected_hosts');
                if (saved) {
                    const parsed = JSON.parse(saved);
                    if (Array.isArray(parsed)) iids = parsed;
                }
            } catch { /* ignore */ }
        }
        // Отфильтровать iid, которых нет в текущем дереве (рестарт сервера сменил iid).
        state.selectedHosts = new Set(iids.filter(iid => state.hostsByIid.has(iid)));
        // Проставить чекбоксы в дереве после его рендера.
        $('hostTree').querySelectorAll('input[type="checkbox"][data-iid]').forEach(cb => {
            const iid = cb.dataset.iid;
            cb.checked = state.selectedHosts.has(iid);
        });
        // Обновить indeterminate родительских папок.
        $('hostTree').querySelectorAll('.tree-item.folder').forEach(folder => {
            updateFolderCheckbox(folder);
        });
        renderSelectedList();
        updateSelectedCount();
        // Если восстановили — сохранить на сервере (на случай пересоздания сессии).
        if (state.selectedHosts.size > 0) saveSelectedHosts();
    }

    function updateFolderCheckbox(folder) {
        // Пересчёт состояния чекбокса папки по детям.
        const wrap = folder.nextElementSibling;
        if (!wrap || !wrap.classList.contains('tree-children')) return;
        const children = wrap.querySelectorAll('.tree-item.device > input[type="checkbox"]');
        let total = 0, checked = 0, indeterminate = 0;
        children.forEach(cb => {
            total++;
            if (cb.indeterminate) indeterminate++;
            else if (cb.checked) checked++;
        });
        const fcb = folder.querySelector('input[type="checkbox"]');
        if (!fcb) return;
        if (indeterminate > 0 || (checked > 0 && checked < total)) {
            fcb.checked = false;
            fcb.indeterminate = true;
        } else if (total > 0 && checked === total) {
            fcb.checked = true;
            fcb.indeterminate = false;
        } else {
            fcb.checked = false;
            fcb.indeterminate = false;
        }
    }

    // ===== Активные задания (панель/бейдж) =====
    async function loadActiveJobs() {
        if (!state.token) return;
        try {
            const data = await api('/api/jobs/active');
            state.activeJobs = (data && data.jobs) || [];
            // Заполнить buffers метаданными (hosts/task) для заданий, которых
            // ещё нет в buffers (например, запущенных в другой вкладке).
            for (const j of state.activeJobs) {
                const buf = getJobBuffer(j.id);
                buf.device_iids = j.device_iids || buf.device_iids;
                buf.task_iid = j.task_iid || buf.task_iid;
                buf.task_name = j.task_name || j.task || buf.task_name;  // fallback на старое поле task
                buf.status = j.status || buf.status;
            }
            updateActiveJobsBadge();
            renderActiveJobsPanel();
        } catch (e) {
            console.debug('loadActiveJobs:', e);
        }
    }

    // ===== Недавние (завершённые) задания — для replay после refresh =====
    // Сервер отдаёт finished/aborted/error задания, ещё не вычищенные GC
    // (по TTL). Фронт открывает к ним viewer-WS с last_seq=0 — сервер досылает
    // replay событий + snapshot, live-подписки не делает (задание завершено).
    async function loadRecentJobs() {
        if (!state.token) return;
        try {
            const data = await api('/api/jobs/recent');
            state.recentJobs = (data && data.jobs) || [];
            for (const j of state.recentJobs) {
                const buf = getJobBuffer(j.id);
                buf.device_iids = j.device_iids || buf.device_iids;
                buf.task_iid = j.task_iid || buf.task_iid;
                buf.task_name = j.task_name || j.task || buf.task_name;
                buf.status = j.status || buf.status;
                if (j.summary) buf.summary = j.summary;
            }
            updateActiveJobsBadge();
            renderActiveJobsPanel();
        } catch (e) {
            console.debug('loadRecentJobs:', e);
        }
    }

    function updateActiveJobsBadge() {
        const clock = $('recentJobsClock');
        const hasRecent = state.recentJobs.length > 0;
        if (clock) clock.classList.toggle('hidden', !hasRecent);

        // Текст бейджа: «Название x/y» для просматриваемого задания,
        // иначе первого активного, иначе первого завершённого.
        let taskName = '';
        let done = 0, total = 0;
        let status = 'idle';

        if (state.viewingJobId) {
            const buf = state.jobBuffers.get(state.viewingJobId);
            if (buf && buf.task_name) {
                taskName = buf.task_name;
                total = buf.device_iids.length;
                done = Array.from(buf.results.values())
                    .filter(r => r.status === 'success' || r.status === 'error').length;
                status = buf.status === 'running' || buf.status === 'pending' ? 'running'
                    : buf.status === 'finished' ? 'done'
                    : buf.status === 'error' ? 'error'
                    : 'idle';
            }
        }
        if (!taskName) {
            const j = state.activeJobs[0] || state.recentJobs[0];
            if (j) {
                taskName = j.task_name || '';
                total = (j.device_iids || []).length;
                if (j.progress) done = j.progress.done || 0;
                status = state.activeJobs[0] ? 'running' : (j.status === 'error' ? 'error' : 'done');
            }
        }

        if (!taskName) {
            setRunStatus('idle', 'Готов');
            return;
        }
        setRunStatus(status, `${taskName} ${done}/${total}`);
    }

    // ===== Панель активных заданий (dropdown по клику на бейдж) =====
    function toggleActiveJobsPanel() {
        const panel = $('activeJobsPanel');
        if (!panel) return;
        panel.classList.toggle('hidden');
        if (!panel.classList.contains('hidden')) {
            renderActiveJobsPanel();
        }
    }

    function renderActiveJobsPanel() {
        const panel = $('activeJobsPanel');
        if (!panel) return;
        const list = panel.querySelector('.active-jobs-list');
        if (!list) return;
        list.innerHTML = '';

        const hasActive = state.activeJobs.length > 0;
        const hasRecent = state.recentJobs.length > 0;

        // Если обе секции пусты — общее empty-сообщение.
        if (!hasActive && !hasRecent) {
            const empty = el('div', 'active-jobs-empty', 'Нет заданий');
            list.appendChild(empty);
            return;
        }

        // Секция активных.
        if (hasActive) {
            list.appendChild(el('div', 'active-jobs-section-header', 'Активные'));
            for (const job of state.activeJobs) {
                list.appendChild(_renderJobItem(job, false));
            }
        }

        // Секция завершённых.
        if (hasRecent) {
            list.appendChild(el('div', 'active-jobs-section-header', 'Недавно завершённые'));
            for (const job of state.recentJobs) {
                list.appendChild(_renderJobItem(job, true));
            }
        }
    }

    // Рендер одного элемента задания (активное или завершённое).
    function _renderJobItem(job, isRecent) {
        const buf = state.jobBuffers.get(job.id);
        const total = job.device_iids ? job.device_iids.length : (buf ? buf.device_iids.length : 0);
        // Прогресс: из API (job.progress) или из буфера (по device_result).
        let done = 0;
        if (job.progress && typeof job.progress.done === 'number') {
            done = job.progress.done;
        } else if (buf) {
            for (const r of buf.results.values()) {
                if (r.status === 'success' || r.status === 'error') done++;
            }
        }
        const item = el('div', 'active-job-item');
        if (isRecent) item.classList.add('recent');
        item.dataset.jobId = job.id;
        if (job.id === state.viewingJobId) item.classList.add('active');
        const title = el('div', 'active-job-title', job.task_name || '(без имени)');
        // Статус для recent: иконка-кружок по статусу.
        const statusText = job.status === 'finished' ? 'готово'
            : job.status === 'aborted' ? 'прервано'
            : job.status === 'error' ? 'ошибка'
            : job.status;
        const metaText = `${total} уст. · ${done}/${total} · ${statusText}`;
        const meta = el('div', 'active-job-meta', metaText);
        if (isRecent) {
            const dot = el('span', `recent-status recent-status-${job.status}`);
            meta.prepend(dot);
        }
        item.appendChild(title);
        item.appendChild(meta);
        item.addEventListener('click', () => {
            switchViewingJob(job.id);
            const panel = $('activeJobsPanel');
            if (panel) panel.classList.add('hidden');
        });
        return item;
    }

    function switchViewingJob(jobId) {
        if (state.viewingJobId === jobId) return;
        state.viewingJobId = jobId;
        // Перерисовать результаты из буфера этого задания.
        renderResultsFromBuffer(jobId);
        // Кнопки Execute/Abort по состоянию буфера.
        const buf = state.jobBuffers.get(jobId);
        if (buf) {
            if (buf.status === 'running' || buf.status === 'pending') {
                state.running = true;
                $('executeBtn').classList.add('hidden');
                $('abortBtn').classList.remove('hidden');
            } else {
                state.running = false;
                $('executeBtn').classList.remove('hidden');
                $('abortBtn').classList.add('hidden');
            }
        }
        renderActiveJobsPanel();
        // updateActiveJobsBadge вызовет setRunStatus с правильным статусом и текстом.
        updateActiveJobsBadge();
    }

    // ===== Диалог параметров =====
    // Запрос параметров задачи у пользователя.
    // Список params — это описания (метки) параметров в порядке %param1%, %param2%...
    // Возвращает { param1: value, param2: value, ... } — ключи по индексу,
    // чтобы совпадало с плейсхолдерами %paramN% в шаблоне команды (как в GUI).
    function askParams(params, taskIid) {
        return new Promise((resolve) => {
            const form = $('paramsForm');
            form.innerHTML = '';
            $('paramsModalTitle').textContent = 'Параметры команды';
            const inputs = [];
            const uploaded = {};
            let cancelled = false;
            let uploading = false;
            let activeXhr = null;
            params.forEach((p, i) => {
                const paramKey = `param${i + 1}`;
                const isFile = String(p).startsWith('file:');
                const label = el('label', null, isFile ? String(p).slice(5) : p);
                const input = document.createElement('input');
                input.type = isFile ? 'file' : 'text';
                input.name = paramKey;
                label.appendChild(input);
                form.appendChild(label);
                inputs.push({ key: paramKey, input });
            });
            const progress = document.createElement('progress');
            progress.max = 100;
            progress.hidden = true;
            progress.style.width = '100%';
            const status = el('div', 'hint', '');
            status.setAttribute('role', 'status');
            form.append(progress, status);
            // Запуск доступен только при заполненных полях: пробелы значением не считаются.
            const emptyInput = () => inputs.find(({ input }) => (
                input.type === 'file' ? !input.files.length : !input.value.trim()
            ));
            const refreshOk = () => {
                const okButton = $('paramsOk');
                okButton.disabled = uploading || Boolean(emptyInput());
                okButton.title = emptyInput() ? 'Заполните все поля, чтобы запустить задачу' : '';
            };
            inputs.forEach(({ input }) => {
                input.addEventListener('input', refreshOk);
                input.addEventListener('change', refreshOk);
            });
            refreshOk();
            $('paramsModal').classList.remove('hidden');

            const removeUploads = () => {
                for (const token of Object.values(uploaded)) {
                    api(`/api/task-uploads/${encodeURIComponent(token)}`, { method: 'DELETE' }).catch(() => {});
                }
            };
            const upload = (file, key) => new Promise((done, fail) => {
                const xhr = new XMLHttpRequest();
                activeXhr = xhr;
                const query = new URLSearchParams({ task_iid: taskIid, key, filename: file.name });
                xhr.open('POST', `/api/task-uploads?${query}`);
                for (const [name, value] of Object.entries(authHeaders())) xhr.setRequestHeader(name, value);
                xhr.setRequestHeader('Content-Type', 'application/octet-stream');
                xhr.upload.onprogress = event => {
                    const percent = event.lengthComputable ? Math.round(event.loaded * 100 / event.total) : 0;
                    progress.value = percent;
                    status.textContent = `Загрузка ${file.name}: ${percent}%`;
                };
                xhr.onload = () => {
                    let data;
                    try { data = JSON.parse(xhr.responseText); } catch (_) { data = {}; }
                    if (xhr.status >= 200 && xhr.status < 300 && data.upload_id) done(data.upload_id);
                    else fail(new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось загрузить файл'));
                };
                xhr.onerror = () => fail(new Error('Ошибка соединения при загрузке файла'));
                xhr.onabort = () => fail(new Error('Загрузка отменена'));
                xhr.send(file);
            });
            const ok = async () => {
                if (uploading) return;
                const empty = emptyInput();
                if (empty) {
                    // Enter в поле не обходит недоступную кнопку запуска.
                    status.textContent = 'Заполните все поля.';
                    empty.input.focus();
                    return;
                }
                for (const { input } of inputs) {
                    if (input.type === 'file' && !input.files.length) {
                        status.textContent = 'Выберите файл для каждого файлового параметра.';
                        input.focus();
                        return;
                    }
                    if (input.type === 'file' && input.files[0].size > 256 * 1024 * 1024) {
                        status.textContent = 'Максимальный размер файла — 256 МиБ.';
                        return;
                    }
                }
                uploading = true;
                $('paramsOk').disabled = true;
                inputs.forEach(({ input }) => { input.disabled = true; });
                const result = {};
                try {
                    for (const { key, input } of inputs) {
                        if (input.type === 'file') {
                            progress.hidden = false;
                            progress.value = 0;
                            uploaded[key] = await upload(input.files[0], key);
                            if (cancelled) { removeUploads(); return; }
                        } else result[key] = input.value;
                    }
                } catch (error) {
                    removeUploads();
                    for (const key of Object.keys(uploaded)) delete uploaded[key];
                    if (!cancelled) status.textContent = error.message;
                    return;
                } finally {
                    uploading = false;
                    refreshOk();
                    inputs.forEach(({ input }) => { input.disabled = false; });
                    if (cancelled) resolve(null);
                }
                cleanup();
                $('paramsModal').classList.add('hidden');
                resolve({ params: result, uploads: uploaded });
            };
            const cancel = () => {
                cancelled = true;
                if (activeXhr) activeXhr.abort();
                removeUploads();
                cleanup();
                $('paramsModal').classList.add('hidden');
                if (!uploading) resolve(null);
            };
            const onSubmit = (e) => { e.preventDefault(); ok(); };
            const onKeydown = (e) => {
                if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancel(); }
            };
            function cleanup() {
                $('paramsOk').removeEventListener('click', ok);
                $('paramsCancel').removeEventListener('click', cancel);
                form.removeEventListener('submit', onSubmit);
                form.removeEventListener('keydown', onKeydown);
                // Кнопка общая с другими диалогами параметров.
                $('paramsOk').disabled = false;
                $('paramsOk').title = '';
            }
            $('paramsOk').addEventListener('click', ok);
            $('paramsCancel').addEventListener('click', cancel);
            form.addEventListener('submit', onSubmit);
            form.addEventListener('keydown', onKeydown);
        });
    }

    // ===== Поиск по дереву хостов =====
    function applyHostSearch(query) {
        const q = query.trim().toLowerCase();
        const items = $('hostTree').querySelectorAll('.tree-item.device');
        items.forEach(item => {
            const host = (item.dataset.host || '').toLowerCase();
            const name = (item.querySelector('.label')?.textContent || '').toLowerCase();
            const match = !q || host.includes(q) || name.includes(q);
            item.classList.toggle('match', match && !!q);
        });
    }

    // ===== Поиск по дереву задач =====
    function applyTaskSearch(query) {
        const q = query.trim().toLowerCase();
        const tree = $('taskTree');

        // Задачи: скрыть несовпадающие
        tree.querySelectorAll('.tree-item.task').forEach(item => {
            const name = (item.dataset.taskName || '').toLowerCase();
            const path = (item.dataset.taskPath || '').toLowerCase();
            const match = !q || name.includes(q) || path.includes(q);
            item.style.display = match ? '' : 'none';
        });

        // Папки: обрабатываем от самых вложенных к корневым,
        // чтобы родитель видел уже выставленный display у дочерних папок
        const folders = tree.querySelectorAll('.tree-item.folder');
        Array.from(folders).reverse().forEach(folder => {
            const wrap = folder.nextElementSibling;
            if (!wrap || !wrap.classList.contains('tree-children')) return;

            let anyVisible = false;
            wrap.querySelectorAll(':scope > .tree-item.task').forEach(t => {
                if (t.style.display !== 'none') anyVisible = true;
            });
            wrap.querySelectorAll(':scope > .tree-item.folder').forEach(sf => {
                if (sf.style.display !== 'none') anyVisible = true;
            });

            if (!q) {
                // Поиск очищен — показываем всё, восстанавливаем свёрнутое состояние
                folder.style.display = '';
                wrap.style.display = '';
                folder.classList.add('collapsed');
                wrap.classList.add('hidden');
            } else if (anyVisible) {
                // Есть совпадения — показываем и раскрываем
                folder.style.display = '';
                wrap.style.display = '';
                folder.classList.remove('collapsed');
                wrap.classList.remove('hidden');
            } else {
                // Нет совпадений — скрываем
                folder.style.display = 'none';
                wrap.style.display = 'none';
            }
        });
    }

    // ===== Сворачивание/разворачивание деревьев =====
    function expandAll(rootSel) {
        document.querySelector(rootSel).querySelectorAll('.tree-item.folder').forEach(i => i.classList.remove('collapsed'));
        document.querySelector(rootSel).querySelectorAll('.tree-children').forEach(c => c.classList.remove('hidden'));
    }
    function collapseAll(rootSel) {
        document.querySelector(rootSel).querySelectorAll('.tree-item.folder').forEach(i => i.classList.add('collapsed'));
        document.querySelector(rootSel).querySelectorAll('.tree-children').forEach(c => c.classList.add('hidden'));
    }

    // ===== Переключатель режимов отображения =====
    function deviceImageSize() {
        const preferred = state.deviceSize ?? (state.viewMode === 'thumbs' ? state.thumbnailWidth || 220 : 64);
        return Math.max(32, Math.min(512, Math.round(preferred / 8) * 8));
    }

    function setDeviceImageSize(img) {
        const size = deviceImageSize();
        img.width = size;
        img.height = size;
    }

    function thumbnailRequestSize() {
        const width = Math.min(state.thumbnailMaxDimension, deviceImageSize() * state.thumbnailScale);
        return { width, height: Math.max(1, Math.round(width * 9 / 16)) };
    }

    function applyDeviceSize() {
        const size = deviceImageSize();
        const wrap = $('selectedList');
        wrap.style.setProperty('--device-icon-size', `${size}px`);
        wrap.style.setProperty('--device-card-width', `${size + 24}px`);
        wrap.style.setProperty('--icon-columns', `repeat(auto-fill, min(100%, ${size + 24}px))`);
        wrap.style.setProperty('--thumbnail-columns', `repeat(auto-fill, min(100%, ${size}px))`);
        wrap.querySelectorAll('.device-item > .icon > img.status-icon-img, .thumb-wrap > img.thumb-img').forEach(setDeviceImageSize);
        const slider = $('deviceSizeSlider');
        slider.value = size;
        slider.disabled = state.viewMode === 'list';
        slider.setAttribute('aria-valuetext', `${size} пикселей`);
        $('deviceSizeValue').textContent = `${size} px`;
        $('deviceSizeControl').title = slider.disabled
            ? 'Размер доступен в режимах «Иконки» и «Миниатюры»'
            : 'Размер иконок и миниатюр устройств';
        $('deviceSizeControl').classList.toggle('disabled', slider.disabled);
    }

    function setViewMode(mode) {
        clearTimeout(state.deviceSizeTimer);
        state.deviceSizeTimer = null;
        state.viewMode = mode;
        localStorage.setItem('dno-tool_view_mode', mode);
        document.querySelectorAll('#viewModeSwitch .view-btn').forEach(b => {
            b.classList.toggle('active', b.dataset.mode === mode);
        });
        renderSelectedList();
    }

    // ===== Группировка выбранных устройств по папкам =====
    function setGroupByFolder(enabled) {
        state.groupByFolder = enabled;
        localStorage.setItem('dno-tool_group_by_folder', enabled ? '1' : '0');
        const btn = $('groupBtn');
        if (btn) btn.classList.toggle('active', enabled);
        renderSelectedList();
    }

    // ===== Фильтр "только онлайн" (как тумблер в Qt) =====
    function setOnlineOnly(enabled) {
        state.onlineOnly = enabled;
        localStorage.setItem('dno-tool_online_only', enabled ? '1' : '0');
        const btn = $('onlineOnlyBtn');
        if (btn) btn.classList.toggle('active', enabled);
        renderSelectedList();
    }

    // Дебаунс пересборки вида при смене видимости по SSE (без рестарта
    // таймеров/зонда - в отличие от renderSelectedList).
    let _onlineOnlyRebuildTimer = null;
    function scheduleOnlineOnlyRebuild() {
        if (_onlineOnlyRebuildTimer) return;
        _onlineOnlyRebuildTimer = setTimeout(() => {
            _onlineOnlyRebuildTimer = null;
            if (state.onlineOnly) renderSelectedListView();
        }, 300);
    }

    // Пересчитать счётчик заголовка группы ("видимо/всего" при фильтре).
    // Источник правды - state, а не DOM (скрытые карточки в DOM отсутствуют).
    function updateGroupHeaderCount(groupBlock) {
        const folder = groupBlock.dataset.folder;
        if (folder === undefined) return;
        let total = 0, shown = 0;
        for (const iid of state.selectedHosts) {
            const info = state.hostsByIid.get(iid);
            const f = (info && info.folder) || 'Без группы';
            if (f !== folder) continue;
            total++;
            if (state.hostStatuses[iid] === true) shown++;
        }
        const count = groupBlock.querySelector('.group-count');
        if (count) count.textContent = state.onlineOnly ? `${shown}/${total}` : `${total}`;
    }

    // Применить смену видимости одного хоста без полного ре-рендера.
    // Вызывается из SSE-обработчиков; полный renderSelectedList здесь нельзя -
    // он оборвёт текущий зонд через probeAbort и зациклит опрос.
    // prevVisible - видимость ДО обновления hostStatuses.
    function refreshOnlineOnlyVisibility(iid, prevVisible) {
        if (!state.onlineOnly) return;
        const visible = state.hostStatuses[iid] === true;
        if (visible === prevVisible) return;
        if (!visible) {
            // Ушёл в офлайн: мгновенно скрыть карточку, счёт группы поправить.
            const item = $('selectedList').querySelector(
                `.device-item[data-iid="${CSS.escape(iid)}"]`
            );
            if (item) {
                item.classList.add('hidden');
                const gb = item.closest('.group-block');
                if (gb) updateGroupHeaderCount(gb);
            }
        }
        // Появление (карточки нет в DOM) и финальная консистентность -
        // через дебаунс-пересборку вида (без рестарта зонда).
        scheduleOnlineOnlyRebuild();
    }

    // ===== Off-canvas drawer дерева (мобильная версия) =====
    function openDrawer() {
        $('treePanel').classList.add('open');
        $('drawerOverlay').classList.add('show');
    }
    function closeDrawer() {
        $('treePanel').classList.remove('open');
        $('drawerOverlay').classList.remove('show');
    }
    function toggleDrawer() {
        if ($('treePanel').classList.contains('open')) closeDrawer();
        else openDrawer();
    }
    function isMobile() {
        return window.matchMedia('(max-width: 820px)').matches;
    }

    // ===== Привязка событий =====
    function bindEvents() {
        // Логин
        $('loginBtn').addEventListener('click', () => {
            if (state.token) { logout(); return; }
            $('loginModal').classList.remove('hidden');
        });
        $('loginForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const u = $('loginUser').value.trim();
            const p = $('loginPass').value;
            if (!u || !p) return;
            // Сначала пробуем /api/login — сервер создаёт сессию (session_id).
            const loginData = await loginRequest(u, p);
            if (loginData) {
                setCredentials(u, p, loginData.session_id);
            } else {
                // Fallback: сервер без /api/login (старая версия) — token-only.
                setCredentials(u, p);
            }
            $('loginModal').classList.add('hidden');
            initAfterAuth();
        });
        $('loginCancel').addEventListener('click', () => $('loginModal').classList.add('hidden'));

        // Drawer (мобильный)
        $('menuToggleBtn').addEventListener('click', toggleDrawer);
        $('drawerCloseBtn').addEventListener('click', closeDrawer);
        $('drawerOverlay').addEventListener('click', closeDrawer);
        document.addEventListener('click', event => {
            if (!event.target.closest('.device-item')) {
                $('selectedList').querySelectorAll('.actions-open').forEach(node => node.classList.remove('actions-open'));
            }
        });

        // Задача
        $('taskInfoBtn').addEventListener('click', showTaskInfo);
        $('taskInfoClose').addEventListener('click', closeTaskInfo);
        $('executeBtn').addEventListener('click', onExecute);
        $('abortBtn').addEventListener('click', onAbort);

        // Часы 🕒 — кнопка-индикатор недавно завершённых заданий, открывает панель.
        const clock = $('recentJobsClock');
        if (clock) {
            clock.addEventListener('click', (e) => {
                e.stopPropagation();
                e.preventDefault();
                toggleActiveJobsPanel();
            });
        }
        // Закрытие панели заданий кликом вне её.
        document.addEventListener('click', (e) => {
            const panel = $('activeJobsPanel');
            if (!panel || panel.classList.contains('hidden')) return;
            const wrap = panel.closest('.jobs-badge-wrap') || panel;
            if (!wrap.contains(e.target)) {
                // Не закрываем если клик был по часам (они сами toggles).
                if (clock && clock.contains(e.target)) return;
                panel.classList.add('hidden');
            }
        });

        // Combobox задач: открыть/закрыть попап по клику на input
        $('taskComboboxInput').addEventListener('click', toggleTaskPopup);
        // Закрытие попапа кликом вне его (на десктопе; на мобильном — fullscreen, вне некуда)
        document.addEventListener('click', (e) => {
            const popup = $('taskPopup');
            if (popup.classList.contains('hidden')) return;
            const box = $('taskCombobox');
            if (!box.contains(e.target)) closeTaskPopup();
        });
        // Escape закрывает попап
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                closeTaskPopup();
                closeDrawer();
            }
        });
        // Поиск в попапе не должен закрывать его
        $('taskSearch').addEventListener('click', (e) => e.stopPropagation());
        $('taskExpandAllBtn').addEventListener('click', (e) => { e.stopPropagation(); expandAll('#taskTree'); });
        $('taskCollapseAllBtn').addEventListener('click', (e) => { e.stopPropagation(); collapseAll('#taskTree'); });
        $('taskSearch').addEventListener('input', (e) => applyTaskSearch(e.target.value));
        $('taskPopupCloseBtn').addEventListener('click', (e) => { e.stopPropagation(); closeTaskPopup(); });

        // Дерево хостов
        $('expandAllBtn').addEventListener('click', () => expandAll('#hostTree'));
        $('collapseAllBtn').addEventListener('click', () => collapseAll('#hostTree'));
        $('refreshHostsBtn').addEventListener('click', loadHosts);
        $('hostSearch').addEventListener('input', (e) => applyHostSearch(e.target.value));

        // Список устройств
        $('clearSelBtn').addEventListener('click', () => {
            state.selectedHosts.clear();
            $('hostTree').querySelectorAll('input[type="checkbox"]').forEach(cb => {
                cb.checked = false;
                cb.indeterminate = false;
            });
            saveSelectedHosts();
            renderSelectedList();
            updateSelectedCount();
        });

        // Группировка выбранных устройств по папкам
        $('groupBtn').addEventListener('click', () => {
            setGroupByFolder(!state.groupByFolder);
        });

        // Фильтр "только онлайн" (как тумблер в Qt, слева от view-mode-switch)
        $('onlineOnlyBtn').addEventListener('click', () => {
            setOnlineOnly(!state.onlineOnly);
        });

        // Переключатель режимов
        document.querySelectorAll('#viewModeSwitch .view-btn').forEach(btn => {
            btn.addEventListener('click', () => setViewMode(btn.dataset.mode));
        });

        // Фичи Veyon + Управление (применяются ко всем выбранным устройствам)
        document.querySelectorAll('#veyonFeatures .vfeat-btn, #ctrlFeatures .vfeat-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                veyonFeatureAll(btn.dataset.feature);
            });
        });

        // Тогглеры панелей: показать/скрыть блоки кнопок
        const vfeatToggle = $('vfeatToggle');
        if (vfeatToggle) {
            vfeatToggle.addEventListener('click', (e) => {
                e.stopPropagation();
                $('veyonFeatures').classList.toggle('open');
            });
        }
        const ctrlToggle = $('ctrlToggle');
        if (ctrlToggle) {
            ctrlToggle.addEventListener('click', (e) => {
                e.stopPropagation();
                $('ctrlFeatures').classList.toggle('open');
            });
        }

        // Результаты
        $('clearResultsBtn').addEventListener('click', () => {
            state.results.clear();
            $('resultsArea').innerHTML = '';
            $('resultsArea').appendChild(el('div', 'empty-state', 'Выберите устройства и задачу, затем нажмите «Выполнить»'));
        });
    }

    async function initAfterAuth() {
        await Promise.all([loadVersion(), loadVeyonStatus(), loadHosts(), loadCommands(), loadIconSet(), loadVeyonIconSet()]);
        // Восстановление выбранных хостов сессии (после рендера дерева).
        await restoreSelectedHosts();
        // Восстановление выбранной задачи (после рендера дерева задач).
        restoreSelectedTask();
        // Вычистить устаревшие recent-записи (по серверному TTL).
        clearExpiredRecentJobs(state.jobTtlMinutes);
        // Активные задания (для бейджа + reconnect).
        await loadActiveJobs();
        // Подхват всех активных заданий (каждое — отдельным viewer-WS).
        await reconnectAllJobs();
        // Завершённые задания (replay результатов после refresh).
        await loadRecentJobs();
        const firstRecentId = await reconnectRecentJobs();
        // Автоотображение: если активных нет, показать последнее завершённое.
        // reconnectAllJobs уже вызвал switchViewingJob для первого активного;
        // если активных не было — переключаемся на recent.
        if (state.activeJobs.length === 0 && firstRecentId) {
            switchViewingJob(firstRecentId);
        }
        startHeartbeat();
        renderSelectedList();
    }

    // Восстановление выбранной задачи из localStorage.
    // Проверяет существование задачи в загруженном дереве — если задача
    // удалена/переименована, сбрасывает выбор (иначе Execute не сработает).
    function restoreSelectedTask() {
        let savedIid = null;
        try {
            savedIid = localStorage.getItem('dno-tool_selected_task');
        } catch { /* ignore */ }
        if (!savedIid) {
            // Миграция со старого формата (path) — пытаемся найти по path.
            let savedPath = null;
            try { savedPath = localStorage.getItem('dno-tool_selected_task_path'); } catch { /* ignore */ }
            if (!savedPath) return;
            const found = state.tasksFlat.find(t => t.path === savedPath);
            if (!found) return;
            state.selectedTask = {
                iid: found.iid,
                name: found.task.name,
                path: found.path,
                task: found.task,
            };
            updateTaskCombobox();
            return;
        }
        // Найти в плоском списке задач по iid.
        const found = state.tasksFlat.find(t => t.iid === savedIid);
        if (!found) {
            // Задача удалена или iid сменился (рестарт сервера) — сбросить.
            try { localStorage.removeItem('dno-tool_selected_task'); } catch { /* ignore */ }
            return;
        }
        state.selectedTask = {
            iid: found.iid,
            name: found.task.name,
            path: found.path,
            task: found.task,
        };
        // Пометить активный элемент в дереве.
        const root = $('taskTree');
        if (root) {
            root.querySelectorAll('.tree-item.task.active').forEach(t => t.classList.remove('active'));
            const active = root.querySelector(`.tree-item.task[data-task-iid="${CSS.escape(found.iid)}"]`);
            if (active) {
                active.classList.add('active');
                // Развернуть все родительские папки, чтобы выбранная задача была видна.
                let wrap = active.parentElement;
                while (wrap && wrap.classList && wrap.classList.contains('tree-children')) {
                    const folder = wrap.previousElementSibling;
                    if (folder && folder.classList.contains('tree-item') && folder.classList.contains('folder')) {
                        folder.classList.remove('collapsed');
                        wrap.classList.remove('hidden');
                    }
                    wrap = wrap.parentElement;
                }
            }
        }
        updateTaskCombobox();
    }

    // Подхват всех активных заданий после refresh.
    // Для каждого задания из /api/jobs/active открывается отдельный viewer-WS
    // с last_seq из localStorage (для replay пропущенных событий).
    // Просматриваемым (viewingJobId) становится первое активное.
    async function reconnectAllJobs() {
        if (!state.token || state.activeJobs.length === 0) {
            // Нет активных — очистим возможный мусор persisted.
            const persisted = loadPersistedJobs();
            if (persisted.length > 0) {
                // Остались persisted задания, которых нет в active — очистить.
                for (const e of persisted) {
                    if (!state.activeJobs.some(j => j.id === e.id)) {
                        clearPersistedJob(e.id);
                    }
                }
            }
            return;
        }
        const persisted = loadPersistedJobs();
        const persistedMap = new Map(persisted.map(e => [e.id, e.seq]));

        // Сбросить UI в состояние «просмотр задания».
        state.results.clear();
        $('resultsArea').innerHTML = '';

        // Открыть viewer-WS для каждого активного задания.
        let firstActiveId = null;
        for (const job of state.activeJobs) {
            const lastSeq = persistedMap.get(job.id) || 0;
            const buf = getJobBuffer(job.id);
            buf.device_iids = job.device_iids || [];
            buf.task_iid = job.task_iid || '';
            buf.task_name = job.task_name || job.task || '';
            buf.status = job.status || 'running';
            // Заглушки карточек в буфере.
            for (const iid of buf.device_iids) {
                if (!buf.results.has(iid)) {
                    buf.results.set(iid, { status: 'pending', label: 'Восстановление', output: '' });
                }
            }
            try {
                await openViewerWs(job.id, lastSeq);
                if (firstActiveId === null) firstActiveId = job.id;
            } catch (e) {
                console.warn(`reconnectAllJobs: не удалось подключиться к ${job.id}`, e);
                // Убрать из persisted если не удалось подключиться.
                clearPersistedJob(job.id);
            }
        }

        // Показать первое активное задание как просматриваемое.
        if (firstActiveId) {
            switchViewingJob(firstActiveId);
        }
    }

    // Подхват завершённых заданий после refresh.
    // Для каждого задания из /api/jobs/recent открывается viewer-WS с last_seq=0
    // — сервер досылает полный replay событий + snapshot. Live-подписки не будет
    // (задание завершено). Если сервер уже вычистил задание (404 при WS или
    // его нет в recent) — запись убирается из persisted.
    // Возвращает id первого завершённого задания (для автоотображения,
    // если активных нет). Не вызывает switchViewingJob сам — это делает
    // вызывающий код в initAfterAuth, зная и active, и recent.
    async function reconnectRecentJobs() {
        if (!state.token || state.recentJobs.length === 0) {
            // Нет завершённых — очистим возможный мусор persisted.
            const persisted = loadRecentJobsPersisted();
            if (persisted.length > 0) {
                for (const e of persisted) {
                    if (!state.recentJobs.some(j => j.id === e.id)) {
                        clearRecentJob(e.id);
                    }
                }
            }
            return null;
        }
        // Синхронизировать persisted с серверным списком: убрать те,
        // что сервер уже вычистил.
        const recentIds = new Set(state.recentJobs.map(j => j.id));
        const persisted = loadRecentJobsPersisted();
        for (const e of persisted) {
            if (!recentIds.has(e.id)) clearRecentJob(e.id);
        }

        let firstRecentId = null;
        for (const job of state.recentJobs) {
            const buf = getJobBuffer(job.id);
            buf.device_iids = job.device_iids || [];
            buf.task_iid = job.task_iid || '';
            buf.task_name = job.task_name || job.task || '';
            buf.status = job.status || 'finished';
            if (job.summary) buf.summary = job.summary;
            // Заглушки карточек в буфере (до replay).
            for (const iid of buf.device_iids) {
                if (!buf.results.has(iid)) {
                    buf.results.set(iid, { status: 'pending', label: 'Восстановление', output: '' });
                }
            }
            try {
                await openViewerWs(job.id, 0);
                if (firstRecentId === null) firstRecentId = job.id;
            } catch (e) {
                console.warn(`reconnectRecentJobs: не удалось подключиться к ${job.id}`, e);
                clearRecentJob(job.id);
            }
        }
        return firstRecentId;
    }

    // Открыть отдельный viewer-WS для существующего задания (только просмотр).
    // Сервер досылает пропущенные события (replay) + продолжит стрим.
    function openViewerWs(jobId, lastSeq) {
        return new Promise((resolve, reject) => {
            if (!state.token) return reject(new Error('Нет авторизации'));
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const url = `${proto}//${location.host}/ws/execute?token=${encodeURIComponent(state.token)}` +
                (state.sessionId ? `&session=${encodeURIComponent(state.sessionId)}` : '') +
                `&job_id=${encodeURIComponent(jobId)}&last_seq=${encodeURIComponent(lastSeq)}`;
            const ws = new WebSocket(url);
            state.jobWs.set(jobId, ws);
            ws.onopen = () => resolve(ws);
            ws.onerror = () => {
                state.jobWs.delete(jobId);
                state.wasDown = true;
                showServerDownToast();
                reject(new Error('Ошибка viewer-WebSocket'));
            };
            ws.onmessage = (ev) => handleWsMessage(ev.data);
            ws.onclose = () => {
                state.jobWs.delete(jobId);
            };
        });
    }

    // ===== Инициализация =====
    // Перетаскиваемый горизонтальный разделитель между списком хостов и результатами.
    // Сохраняет пропорцию в localStorage, по умолчанию 50/50.
    function initSplitter() {
        const splitter = $('hSplitter');
        const top = $('topPane');
        const bottom = $('bottomPane');
        const container = splitter.parentElement;
        if (!splitter || !top || !bottom || !container) return;

        // Восстановление сохранённой пропорции (доля верхней панели, 0.1..0.9)
        // Только на десктопе — на мобильном раскладка column, инлайн-flex ломает её.
        if (!isMobile()) {
            const saved = parseFloat(localStorage.getItem('dno-tool_split_top'));
            if (saved && saved > 0.1 && saved < 0.9) {
                top.style.flex = `${saved} 1 0`;
                bottom.style.flex = `${1 - saved} 1 0`;
            }
        }

        let dragging = false;
        let startY = 0;
        let startTopH = 0;

        const onStart = (e) => {
            if (isMobile()) return;
            dragging = true;
            splitter.classList.add('dragging');
            startY = (e.touches ? e.touches[0].clientY : e.clientY);
            startTopH = top.getBoundingClientRect().height;
            document.body.style.cursor = 'row-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        };

        const onMove = (e) => {
            if (!dragging) return;
            const y = (e.touches ? e.touches[0].clientY : e.clientY);
            const delta = y - startY;
            const containerH = container.getBoundingClientRect().height;
            let newTopH = Math.max(40, Math.min(containerH - 46, startTopH + delta));
            let ratio = newTopH / containerH;
            ratio = Math.max(0.1, Math.min(0.9, ratio));
            top.style.flex = `${ratio} 1 0`;
            bottom.style.flex = `${1 - ratio} 1 0`;
        };

        const onEnd = () => {
            if (!dragging) return;
            dragging = false;
            splitter.classList.remove('dragging');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            const containerH = container.getBoundingClientRect().height;
            const topH = top.getBoundingClientRect().height;
            const ratio = Math.max(0.1, Math.min(0.9, topH / containerH));
            localStorage.setItem('dno-tool_split_top', String(ratio.toFixed(3)));
        };

        splitter.addEventListener('mousedown', onStart);
        splitter.addEventListener('touchstart', onStart, { passive: false });
        document.addEventListener('mousemove', onMove);
        document.addEventListener('touchmove', onMove, { passive: false });
        document.addEventListener('mouseup', onEnd);
        document.addEventListener('touchend', onEnd);
    }

    // Вертикальный перетаскиваемый разделитель между деревом хостов и правой
    // панелью. Меняет ширину tree-panel в диапазоне 240..600px, сохраняет
    // пропорцию в localStorage (dno-tool_split_tree_w, px).
    function initVSplitter() {
        const splitter = $('vSplitter');
        const tree = document.querySelector('.tree-panel');
        const layout = document.querySelector('.main-layout');
        if (!splitter || !tree || !layout) return;

        const MIN_W = 240;
        const MAX_W = 600;

        // Восстановление сохранённой ширины (только на десктопе).
        if (!isMobile()) {
            const saved = parseInt(localStorage.getItem('dno-tool_split_tree_w'), 10);
            if (saved && saved >= MIN_W && saved <= MAX_W) {
                tree.style.width = saved + 'px';
                layout.style.gridTemplateColumns = `${saved}px 6px 1fr`;
            }
        }

        let dragging = false;
        let startX = 0;
        let startW = 0;

        const onStart = (e) => {
            if (isMobile()) return;
            dragging = true;
            splitter.classList.add('dragging');
            startX = (e.touches ? e.touches[0].clientX : e.clientX);
            startW = tree.getBoundingClientRect().width;
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        };

        const onMove = (e) => {
            if (!dragging) return;
            const x = (e.touches ? e.touches[0].clientX : e.clientX);
            const delta = x - startX;
            const newW = Math.max(MIN_W, Math.min(MAX_W, startW + delta));
            tree.style.width = newW + 'px';
            layout.style.gridTemplateColumns = `${newW}px 6px 1fr`;
        };

        const onEnd = () => {
            if (!dragging) return;
            dragging = false;
            splitter.classList.remove('dragging');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            const w = Math.round(tree.getBoundingClientRect().width);
            localStorage.setItem('dno-tool_split_tree_w', String(w));
        };

        splitter.addEventListener('mousedown', onStart);
        splitter.addEventListener('touchstart', onStart, { passive: false });
        document.addEventListener('mousemove', onMove);
        document.addEventListener('touchmove', onMove, { passive: false });
        document.addEventListener('mouseup', onEnd);
        document.addEventListener('touchend', onEnd);
    }

    function init() {
        bindEvents();
        try {
            const savedSize = Number(localStorage.getItem('dno-tool_device_size'));
            if (Number.isInteger(savedSize) && savedSize >= 32 && savedSize <= 512 && savedSize % 8 === 0) state.deviceSize = savedSize;
        } catch {}
        $('deviceSizeSlider').addEventListener('input', event => {
            const value = Number(event.target.value);
            if (!Number.isFinite(value)) return;
            state.deviceSize = Math.max(32, Math.min(512, Math.round(value / 8) * 8));
            applyDeviceSize();
            try { localStorage.setItem('dno-tool_device_size', String(state.deviceSize)); } catch {}
            clearTimeout(state.deviceSizeTimer);
            state.deviceSizeTimer = setTimeout(() => {
                state.deviceSizeTimer = null;
                if (state.viewMode !== 'thumbs' || !state.thumbnailsAvailable || !state.selectedHosts.size || !state.token || !devicesVisible()) return;
                updateThumbTimer(true);
            }, 250);
        });
        initSplitter();
        initVSplitter();
        initVncModalEvents();
        Tip.init();
        // Сбросить инлайн-стили сплиттеров при переходе мобильный↔десктоп
        window.addEventListener('resize', () => {
            if (isMobile()) {
                const tree = document.querySelector('.tree-panel');
                const layout = document.querySelector('.main-layout');
                const top = $('topPane');
                const bottom = $('bottomPane');
                if (tree) tree.style.width = '';
                if (layout) layout.style.gridTemplateColumns = '';
                if (top) top.style.flex = '';
                if (bottom) bottom.style.flex = '';
                closeDrawer();
            }
        });
        // Приложение загрузилось: страница входа отследит следующий сбой заново.
        sessionStorage.removeItem('dno-tool_login_reload');
        const saved = localStorage.getItem('dno-tool_token');
        const savedSession = localStorage.getItem('dno-tool_session');
        if (saved) {
            state.token = saved;
            try {
                const decoded = atob(saved);
                const [u] = decoded.split(':');
                state.creds = { username: u, password: '' };
            } catch { /* ignore */ }
        }
        if (savedSession) {
            state.sessionId = savedSession;
        }
        // Восстановление режима отображения
        const savedMode = localStorage.getItem('dno-tool_view_mode');
        if (savedMode && ['list', 'icons', 'thumbs'].includes(savedMode)) {
            setViewMode(savedMode);
        }
        // Восстановление группировки по папкам
        if (localStorage.getItem('dno-tool_group_by_folder') === '1') {
            setGroupByFolder(true);
        }
        // Восстановление фильтра "только онлайн"
        if (localStorage.getItem('dno-tool_online_only') === '1') {
            setOnlineOnly(true);
        }
        updateAuthUI();
        renderSelectedList();
        renderTaskTree([]);
        updateTaskCombobox();

        if (state.token) {
            initAfterAuth().catch(err => {
                if (err.message.includes('аутентификация')) {
                    $('loginModal').classList.remove('hidden');
                }
            });
        } else {
            $('loginModal').classList.remove('hidden');
        }
    }

    // ===== Custom tooltip =====

    const Tip = {
        el: null, timer: null,

        init() {
            this.el = document.getElementById('dnoTooltip');
            if (!this.el) return;
            document.addEventListener('mouseover', (e) => {
                const t = e.target.closest('[data-tip]');
                if (!t) return;
                this.schedule(t);
            });
            document.addEventListener('mouseout', (e) => {
                const el = e.target.closest('[data-tip]');
                if (!el) return;
                const related = e.relatedTarget;
                if (related && el.contains(related)) return;
                this.hide();
            });
            document.addEventListener('focusin', (e) => {
                const t = e.target.closest('[data-tip]');
                if (!t) return;
                this.show(t);
            });
            document.addEventListener('focusout', (e) => {
                if (!e.target.closest('[data-tip]')) return;
                this.hide();
            });
            window.addEventListener('scroll', () => this.hide(), true);
            document.addEventListener('keydown', (e) => { if (e.key === 'Escape') this.hide(); });
        },

        schedule(t) {
            this.cancel();
            this.timer = setTimeout(() => this.show(t), 400);
        },
        cancel() {
            if (this.timer) { clearTimeout(this.timer); this.timer = null; }
        },
        show(t) {
            if (!this.el) return;
            const text = t.dataset.tip;
            if (!text) { this.hide(); return; }
            this.el.textContent = text;
            this.el.classList.remove('hidden');
            const r = t.getBoundingClientRect();
            const tw = this.el.offsetWidth, th = this.el.offsetHeight;
            const vw = window.innerWidth, vh = window.innerHeight;
            // Позиция: сверху от кнопки, по центру, с clamp к viewport
            let y = r.top - th - 6;
            if (y < 4) y = Math.min(vh - th - 4, r.bottom + 6);
            let x = r.left + r.width / 2 - tw / 2;
            x = Math.max(4, Math.min(vw - tw - 4, x));
            this.el.style.left = x + 'px';
            this.el.style.top = y + 'px';
        },
        hide() {
            this.cancel();
            if (this.el) this.el.classList.add('hidden');
        }
    };

    // ===== Удалённый рабочий стол (VNC через Veyon RFB bridge) =====

    function openVncModal(iid) {
        const label = hostLabel(iid);
        const token = state.token || '';
        const url = `vnc.html?iid=${encodeURIComponent(iid)}&token=${encodeURIComponent(token)}&name=${encodeURIComponent(label)}`;
        window.open(url, '_blank', 'noopener,width=1024,height=768');
    }

    function initVncModalEvents() {
        // События VNC-кнопок теперь в vnc.js (отдельная вкладка).
        // Здесь — только заглушка, чтобы init() не падал на отсутствующих элементах.
        const vncModal = $('vncModal');
        if (vncModal) vncModal.classList.add('hidden');
    }

    function devicesVisible() {
        return !window.DNODesktop || window.DNODesktop.visible('devices');
    }
    window.addEventListener('dno:window', event => {
        if (event.detail.id !== 'devices') return;
        updateThumbTimer();
        updateStatusTimer();
        if (!event.detail.visible) {
            clearTimeout(state.deviceSizeTimer);
            state.deviceSizeTimer = null;
            closeDrawer();
        }
    });
    window.DNODesktopAPI = {
        api,
        headers: authHeaders,
        authenticated: () => !!state.token,
        refreshHosts: loadHosts,
        revealHosts: revealImportedHosts,
        notify: showToast,
        token: () => state.token || '',
        hostLabel,
        refreshSettings: async () => { await Promise.all([loadVersion(), loadVeyonStatus(), loadIconSet()]); renderSelectedList(); },
    };
    document.addEventListener('DOMContentLoaded', init);
})();
