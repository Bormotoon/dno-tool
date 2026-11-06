// Рабочий стол: геометрия окон, Dock и размещение инструментов.
(() => {
    'use strict';
    const catalog = [
        ['devices', 'Устройства', 'menu_hosts'],
        ['scanner', 'Сканер', 'menu_scanner'],
        ['pinger', 'Пингер', 'menu_ping'],
        ['cameras', 'Камеры ЕЦХД', 'camera'],
        ['resources', 'Ресурсы', 'menu_monitoring'],
        // Окна хоста открываются только с карточки устройства, в Dock видны, пока открыты.
        ['device-info', 'Сведения', 'menu_info', true],
        ['terminal', 'Терминал', 'menu_terminal', true],
        ['files', 'Файлы', 'menu_sftp', true],
    ];
    const key = 'dno-desktop-v1';
    const layoutKey = 'dno-desktop-layout-v1';
    const L = window.DNOLayout;
    const windows = new Map();
    const mobileQuery = window.matchMedia('(max-width: 820px)');
    let saved = {};
    let savedLayout = {};
    let layoutMode = 'free', tileTree = null, focusedId = null, activeDrag = null;
    let tileGeometry = new Map();
    let initializing = true;
    let z = 10;
    let saveTimer;
    try { saved = JSON.parse(localStorage.getItem(key) || '{}') || {}; } catch {}
    if (typeof saved !== 'object' || Array.isArray(saved)) saved = {};
    try { savedLayout = JSON.parse(localStorage.getItem(layoutKey) || '{}') || {}; } catch {}
    layoutMode = savedLayout.mode === 'tile' ? 'tile' : 'free';
    tileTree = L.sanitize(savedLayout.tree, new Set(catalog.map(entry => entry[0])));

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }
    const iconCache = {};
    function icon(key) {
        const img = element('img', 'app-icon');
        img.dataset.appIcon = key;
        img.alt = '';
        img.src = iconCache[key] || 'favicon.ico';
        return img;
    }
    async function loadIcons() {
        try {
            const response = await fetch('/api/tools/icons');
            if (!response.ok) return;
            const icons = await response.json();
            for (const [key, svg] of Object.entries(icons)) iconCache[key] = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
            document.querySelectorAll('[data-app-icon]').forEach(img => { if (iconCache[img.dataset.appIcon]) img.src = iconCache[img.dataset.appIcon]; });
        } catch {}
    }
    function button(text, action, className = '') {
        const node = element('button', className, text);
        node.type = 'button';
        node.addEventListener('click', action);
        return node;
    }
    const desktop = element('div', 'desktop');
    desktop.id = 'desktop';
    desktop.setAttribute('aria-label', 'Рабочий стол DNO Tool');
    const header = document.querySelector('.app-header');
    header.after(desktop);
    const menu = element('div', 'desktop-menu');
    const layouts = {
        tile: '<rect x="3" y="3" width="9" height="18" rx="1"/><rect x="15" y="3" width="6" height="8" rx="1"/><rect x="15" y="14" width="6" height="7" rx="1"/>',
        free: '<rect x="3" y="3" width="12" height="11" rx="1"/><rect x="10" y="11" width="11" height="10" rx="1"/>',
        desktop: '<rect x="3" y="4" width="18" height="13" rx="1"/><path d="M8 21h8M12 17v4M7 8h10"/>',
        wallpaper: '<path d="M3 7c3-5 6 5 9 0s6 5 9 0M3 12c3-5 6 5 9 0s6 5 9 0M3 17c3-5 6 5 9 0s6 5 9 0"/>',
    };
    function layoutButton(label, glyph, action) {
        const control = button('', action, 'desktop-layout-button');
        control.title = label;
        control.setAttribute('aria-label', label);
        control.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${layouts[glyph]}</svg>`;
        control.dataset.layout = glyph;
        return control;
    }
    const tileButton = layoutButton('Автоматическое деление пространства', 'tile', () => setLayoutMode('tile'));
    const freeButton = layoutButton('Свободное размещение окон', 'free', () => setLayoutMode('free'));
    menu.append(tileButton, freeButton, layoutButton('Показать рабочий стол', 'desktop', showDesktop));
    const wallpaperButton = layoutButton('Анимация обоев', 'wallpaper', () => {
        window.DNOWallpaper?.setAnimated(!window.DNOWallpaper.animated);
    });
    function wallpaperState() {
        const animated = window.DNOWallpaper?.animated === true;
        wallpaperButton.setAttribute('aria-pressed', String(animated));
        wallpaperButton.disabled = window.DNOWallpaper?.available === false;
        wallpaperButton.title = animated ? 'Приостановить обои' : 'Включить анимацию обоев';
        if (wallpaperButton.disabled) wallpaperButton.title = 'Статические обои: WebGL недоступен';
        wallpaperButton.setAttribute('aria-label', wallpaperButton.title);
    }
    window.addEventListener('dno:wallpaper', wallpaperState);
    wallpaperState();
    menu.append(wallpaperButton);
    const clock = element('time', 'desktop-clock');
    header.insertBefore(menu, header.querySelector('.auth-box'));
    header.append(clock);
    function tick() {
        const now = new Date();
        clock.dateTime = now.toISOString();
        clock.textContent = now.toLocaleString('ru-RU', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
    }
    tick();
    setInterval(tick, 15000);
    const dock = element('nav', 'desktop-dock');
    dock.setAttribute('aria-label', 'Инструменты');
    document.body.append(dock);
    const dockReveal = button('Инструменты', () => document.body.classList.toggle('dock-peek'), 'dock-reveal');
    dockReveal.setAttribute('aria-label', 'Показать Dock с инструментами');
    document.body.append(dockReveal);
    dockReveal.addEventListener('pointerenter', () => document.body.classList.add('dock-peek'));
    dock.addEventListener('pointerleave', () => document.body.classList.remove('dock-peek'));

    function syncWorkspace() {
        const mobile = mobileQuery.matches;
        if (mobile && (!windows.get(focusedId)?.open || windows.get(focusedId)?.minimized)) {
            focusedId = [...windows.values()].filter(win => win.open && !win.minimized)
                .sort((a, b) => Number(a.node.style.zIndex) - Number(b.node.style.zIndex)).at(-1)?.id || null;
        }
        if (layoutMode === 'tile' && !mobile) updateTiles();
        const full = [...windows.values()].some(win => win.open && !win.minimized && (layoutMode === 'tile' || win.maximized || mobile));
        document.body.classList.toggle('desktop-expanded', full);
        document.body.classList.toggle('desktop-tiled', layoutMode === 'tile' && !mobile);
        if (!full) document.body.classList.remove('dock-peek');
        windows.forEach(geometry);
        windows.forEach(publishVisibility);
        if (mobile) {
            windows.forEach(win => win.node.classList.toggle('focused', win.id === focusedId));
            document.getElementById('desktopAppName').textContent = windows.get(focusedId)?.title || 'Инструменты';
        }
    }

    function updateTiles() {
        const visible = [...windows.values()].filter(win => win.open && !win.minimized);
        const area = { x: 0, y: 0, w: bounds().width, h: bounds().height };
        tileTree = L.prune(tileTree, new Set(visible.map(win => win.id)));
        let existing = L.leaves(tileTree);
        for (const win of visible) {
            if (existing.includes(win.id)) continue;
            const target = existing.includes(focusedId) ? focusedId : existing.at(-1);
            const current = L.layout(tileTree, area);
            tileTree = L.insert(tileTree, target, win.id, current.get(target)?.rect || area);
            existing = L.leaves(tileTree);
        }
        tileGeometry = L.layout(tileTree, area);
    }

    function bounds() { return { width: desktop.clientWidth, height: desktop.clientHeight }; }
    function normalize(rect, id) {
        const { width, height } = bounds();
        const minimum = 280;
        const w = Math.min(width, Math.max(Math.min(minimum, width), Number(rect.w) || 780));
        const h = Math.min(height, Math.max(Math.min(300, height), Number(rect.h) || 530));
        return {
            x: Math.max(0, Math.min(Number(rect.x) || 0, width - w)),
            y: Math.max(0, Math.min(Number(rect.y) || 0, height - h)),
            w, h,
        };
    }
    function geometry(win) {
        const tiled = layoutMode === 'tile' && !mobileQuery.matches;
        const full = win.maximized || mobileQuery.matches;
        const r = full ? { x: 0, y: 0, w: bounds().width, h: bounds().height }
            : tiled ? tileGeometry.get(win.id)?.rect || win.rect : normalize(win.rect, win.id);
        if (!tiled && !full) win.rect = r;
        Object.assign(win.node.style, { left: `${r.x}px`, top: `${r.y}px`, width: `${r.w}px`, height: `${r.h}px` });
        win.node.classList.toggle('tiled', tiled);
        win.node.classList.toggle('maximized', full);
        win.maxButton.setAttribute('aria-label', win.maximized ? 'Восстановить размер' : 'Развернуть');
        win.maxButton.title = win.maxButton.getAttribute('aria-label');
        for (const edge of ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw']) {
            const handle = win.node.querySelector(`.resize-${edge}`);
            if (handle) handle.hidden = tiled && !tileBorders(win, edge).length;
        }
    }
    function persist() {
        clearTimeout(saveTimer);
        saveTimer = setTimeout(() => {
            const value = {};
            for (const [id, win] of windows) value[id] = { rect: win.rect, open: win.open, minimized: win.minimized, maximized: layoutMode === 'tile' ? win.freeMaximized : win.maximized, order: Number(win.node.style.zIndex) };
            try {
                localStorage.setItem(key, JSON.stringify(value));
                localStorage.setItem(layoutKey, JSON.stringify({ mode: layoutMode, tree: tileTree, focused: focusedId }));
            } catch {}
        }, 180);
    }
    function isVisible(win) {
        return win.open && !win.minimized && (!mobileQuery.matches || focusedId === win.id) && !document.hidden;
    }
    function publishVisibility(win) {
        const visible = isVisible(win);
        win.node.hidden = !win.open || win.minimized || (mobileQuery.matches && focusedId !== win.id);
        win.dock.classList.toggle('running', win.open);
        win.dock.hidden = win.onDemand && !win.open;
        win.dock.setAttribute('aria-pressed', String(mobileQuery.matches ? visible : win.open && !win.minimized));
        // Смена активного экрана приостанавливает опрос, но не закрывает вкладки и SSH-сеансы.
        if (win.lastVisible === visible && win.lastOpen === win.open) return;
        win.lastVisible = visible;
        win.lastOpen = win.open;
        window.dispatchEvent(new CustomEvent('dno:window', { detail: { id: win.id, visible, open: win.open } }));
    }
    function lifecycle(win) {
        if (initializing) publishVisibility(win);
        else syncWorkspace();
    }
    function focus(win) {
        focusedId = win.id;
        if (z > 1000) {
            [...windows.values()].sort((a, b) => Number(a.node.style.zIndex) - Number(b.node.style.zIndex)).forEach((item, index) => { item.node.style.zIndex = index + 10; });
            z = 30;
        }
        win.node.style.zIndex = ++z;
        for (const item of windows.values()) item.node.classList.toggle('focused', item === win);
        document.getElementById('desktopAppName').textContent = win.title;
        if (mobileQuery.matches) syncWorkspace();
        persist();
    }
    function setLayoutMode(mode) {
        if (mobileQuery.matches || mode === layoutMode) return;
        activeDrag?.();
        windows.forEach(win => {
            if (mode === 'tile') { win.freeMaximized = win.maximized; win.maximized = false; }
            else win.maximized = win.freeMaximized || false;
        });
        layoutMode = mode;
        layoutState();
        syncWorkspace();
        persist();
    }
    function layoutState() {
        tileButton.setAttribute('aria-pressed', String(layoutMode === 'tile'));
        freeButton.setAttribute('aria-pressed', String(layoutMode === 'free'));
    }
    function open(id) {
        const win = windows.get(id);
        if (!win) return;
        if (layoutMode === 'tile' && !mobileQuery.matches) windows.forEach(item => { item.maximized = false; });
        win.open = true;
        win.minimized = false;
        geometry(win);
        lifecycle(win);
        focus(win);
        if (mobileQuery.matches) win.dock.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        win.node.focus({ preventScroll: true });
    }
    function hide(win, close) {
        if (close) win.open = false;
        else win.minimized = true;
        lifecycle(win);
        persist();
        win.dock.focus();
    }
    function maximize(win) {
        if (mobileQuery.matches) return;
        if (layoutMode === 'tile') windows.forEach(item => { if (item !== win) item.maximized = false; });
        win.maximized = !win.maximized;
        syncWorkspace();
        focus(win);
    }
    function showDesktop() {
        for (const win of windows.values()) if (win.open) { win.minimized = true; lifecycle(win); }
        persist();
    }
    function tileBorders(win, edge) {
        const placement = tileGeometry.get(win.id);
        if (!placement) return [];
        const r = placement.rect;
        const result = [];
        for (const axis of ['x', 'y']) {
            const direction = axis === 'x' ? edge.includes('e') ? 'e' : edge.includes('w') ? 'w' : null
                : edge.includes('s') ? 's' : edge.includes('n') ? 'n' : null;
            if (!direction) continue;
            for (const branch of [...placement.path].reverse()) {
                if (branch.node.axis !== axis) continue;
                const forward = direction === 'e' || direction === 's';
                if (branch.side !== (forward ? 'first' : 'second')) continue;
                const span = axis === 'x' ? branch.rect.w : branch.rect.h;
                const split = (axis === 'x' ? branch.rect.x : branch.rect.y) + branch.split;
                const boundary = axis === 'x' ? r.x + (forward ? r.w : 0) : r.y + (forward ? r.h : 0);
                if (Math.abs(boundary - split) > 1) continue;
                result.push({ ...branch, span, ratio: branch.node.ratio, startRatio: branch.split / span });
                break;
            }
        }
        return result;
    }
    function beginTilePointer(event, win, edge) {
        const borders = edge ? tileBorders(win, edge) : [];
        if (edge && !borders.length) return;
        event.preventDefault();
        activeDrag?.();
        focus(win);
        const target = event.currentTarget, x = event.clientX, y = event.clientY;
        let destination = null, moved = false;
        target.setPointerCapture(event.pointerId);
        document.body.classList.add('desktop-dragging');
        const highlight = id => windows.forEach(item => item.node.classList.toggle('tile-drop-target', item.id === id));
        const move = e => {
            const dx = e.clientX - x, dy = e.clientY - y;
            moved ||= Math.hypot(dx, dy) > 5;
            if (!moved) return;
            if (edge) {
                for (const branch of borders) L.resize(branch.node, branch.startRatio + (branch.node.axis === 'x' ? dx : dy) / branch.span, branch.span);
                syncWorkspace();
            } else {
                const origin = desktop.getBoundingClientRect();
                const px = e.clientX - origin.left, py = e.clientY - origin.top;
                destination = [...tileGeometry].find(([id, placement]) => {
                    const r = placement.rect;
                    return id !== win.id && px >= r.x && px < r.x + r.w && py >= r.y && py < r.y + r.h;
                })?.[0] || null;
                highlight(destination);
            }
        };
        const end = e => {
            target.removeEventListener('pointermove', move);
            target.removeEventListener('pointerup', end);
            target.removeEventListener('pointercancel', end);
            target.removeEventListener('lostpointercapture', end);
            document.removeEventListener('keydown', escape);
            if (e?.type === 'pointerup' && destination && moved) L.swap(tileTree, win.id, destination);
            else if (!e || e.type !== 'pointerup') borders.forEach(branch => { branch.node.ratio = branch.ratio; });
            highlight(null);
            document.body.classList.remove('desktop-dragging');
            activeDrag = null;
            if (target.hasPointerCapture?.(event.pointerId)) target.releasePointerCapture(event.pointerId);
            syncWorkspace();
            persist();
        };
        const escape = e => { if (e.key === 'Escape') { e.preventDefault(); end(); } };
        target.addEventListener('pointermove', move);
        target.addEventListener('pointerup', end);
        target.addEventListener('pointercancel', end);
        target.addEventListener('lostpointercapture', end);
        document.addEventListener('keydown', escape);
        activeDrag = () => end();
    }
    function beginPointer(event, win, edge) {
        if (mobileQuery.matches || event.button !== 0 || win.maximized || event.target.closest('button')) return;
        if (layoutMode === 'tile') return beginTilePointer(event, win, edge);
        event.preventDefault();
        focus(win);
        const target = event.currentTarget;
        const original = { ...win.rect };
        const x = event.clientX, y = event.clientY;
        target.setPointerCapture(event.pointerId);
        document.body.classList.add('desktop-dragging');
        const move = e => {
            const dx = e.clientX - x, dy = e.clientY - y;
            win.rect = { ...original };
            if (!edge) { win.rect.x += dx; win.rect.y += dy; }
            else {
                const area = bounds();
                const minimumWidth = Math.min(280, area.width);
                const minimumHeight = Math.min(300, area.height);
                let left = original.x, top = original.y;
                let right = original.x + original.w, bottom = original.y + original.h;
                if (edge.includes('e')) right = Math.max(left + minimumWidth, Math.min(area.width, right + dx));
                if (edge.includes('s')) bottom = Math.max(top + minimumHeight, Math.min(area.height, bottom + dy));
                if (edge.includes('w')) left = Math.max(0, Math.min(right - minimumWidth, left + dx));
                if (edge.includes('n')) top = Math.max(0, Math.min(bottom - minimumHeight, top + dy));
                win.rect = { x: left, y: top, w: right - left, h: bottom - top };
            }
            geometry(win);
        };
        const end = () => {
            target.removeEventListener('pointermove', move);
            target.removeEventListener('pointerup', end);
            target.removeEventListener('pointercancel', end);
            document.body.classList.remove('desktop-dragging');
            persist();
        };
        target.addEventListener('pointermove', move);
        target.addEventListener('pointerup', end);
        target.addEventListener('pointercancel', end);
    }
    const appName = element('span', 'desktop-app-name', 'Устройства');
    appName.id = 'desktopAppName';
    header.querySelector('.brand').append(appName);
    catalog.forEach(([id, title, key, onDemand = false], i) => {
        const launch = button('', () => open(id), 'dock-item');
        launch.title = title;
        launch.setAttribute('aria-label', title);
        const glyph = element('span', 'dock-icon');
        glyph.append(icon(key));
        launch.append(glyph, element('span', 'dock-label', title));
        dock.append(launch);
        const node = element('section', `desktop-window tool-${id}`);
        node.id = `window-${id}`;
        node.tabIndex = -1;
        node.setAttribute('aria-label', title);
        const bar = element('div', 'window-titlebar');
        const controls = element('div', 'window-controls');
        const win = { id, title, node, dock: launch, onDemand, open: id === 'devices', minimized: false, maximized: false, rect: { x: 24 + i * 30, y: 18 + i * 22, w: id === 'devices' ? 1080 : 780, h: 590 } };
        for (const [kind, label, action] of [
            ['close', 'Закрыть', () => hide(win, true)],
            ['minimize', 'Свернуть', () => hide(win, false)],
            ['maximize', 'Развернуть', () => maximize(win)],
        ]) {
            const control = button('', action, `window-control ${kind}`);
            control.title = label;
            control.setAttribute('aria-label', label);
            controls.append(control);
            if (kind === 'maximize') win.maxButton = control;
        }
        const name = element('span', 'window-title', title);
        name.prepend(icon(key));
        bar.append(controls, name);
        bar.addEventListener('pointerdown', e => beginPointer(e, win, ''));
        bar.addEventListener('dblclick', e => { if (!e.target.closest('button')) maximize(win); });
        const body = element('div', 'window-body');
        body.id = `tool-${id}`;
        if (id === 'devices') body.append(document.getElementById('drawerOverlay'), document.querySelector('.main-layout'));
        node.append(bar, body);
        for (const edge of ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw']) {
            const handle = element('div', `window-resize resize-${edge}`);
            handle.addEventListener('pointerdown', e => beginPointer(e, win, edge));
            node.append(handle);
        }
        node.addEventListener('pointerdown', () => focus(win));
        node.addEventListener('keydown', e => { if (e.key === 'Escape' && win.maximized) maximize(win); });
        const record = saved[id];
        if (record && typeof record === 'object') {
            if (record.rect && typeof record.rect === 'object' && ['x', 'y', 'w', 'h'].every(k => Number.isFinite(record.rect[k]))) win.rect = record.rect;
            // Вкладки и сеансы окон хоста не переживают перезагрузку страницы.
            win.open = !onDemand && typeof record.open === 'boolean' ? record.open : win.open;
            win.minimized = !onDemand && record.minimized === true;
            win.maximized = record.maximized === true;
        }
        win.freeMaximized = win.maximized;
        if (layoutMode === 'tile') win.maximized = false;
        windows.set(id, win);
        desktop.append(node);
        geometry(win);
        node.style.zIndex = record && Number.isFinite(record.order) && record.order >= 10 && record.order <= 1000 ? record.order : ++z;
        z = Math.max(z, Number(node.style.zIndex));
        lifecycle(win);
    });
    initializing = false;
    focusedId = windows.has(savedLayout.focused) ? savedLayout.focused : 'devices';
    syncWorkspace();
    const initialFocus = [...windows.values()].filter(win => win.open && !win.minimized)
        .sort((a, b) => (a.id === focusedId ? 1 : b.id === focusedId ? -1 : Number(a.node.style.zIndex) - Number(b.node.style.zIndex))).at(-1);
    if (initialFocus) focus(initialFocus);
    const themeKey = 'dno-desktop-theme';
    let theme = 'dark';
    try { theme = localStorage.getItem(themeKey) || theme; } catch {}
    function setTheme(value) {
        document.documentElement.dataset.desktopTheme = value === 'light' ? 'light' : 'dark';
        try { localStorage.setItem(themeKey, document.documentElement.dataset.desktopTheme); } catch {}
    }
    setTheme(theme);
    document.getElementById('menuToggleBtn').addEventListener('click', () => open('devices'));
    function viewportChanged() {
        activeDrag?.();
        const viewport = window.visualViewport;
        if (viewport && viewport.scale === 1) document.documentElement.style.setProperty('--mobile-viewport-height', `${viewport.height}px`);
        syncWorkspace();
    }
    window.addEventListener('resize', viewportChanged);
    window.visualViewport?.addEventListener('resize', viewportChanged);
    new ResizeObserver(syncWorkspace).observe(desktop);
    viewportChanged();
    document.addEventListener('visibilitychange', () => windows.forEach(lifecycle));
    window.addEventListener('dno:auth', loadIcons);
    header.querySelector('.logo').replaceChildren(icon('menu_hosts'));
    function close(id) {
        const win = windows.get(id);
        if (win?.open) hide(win, true);
    }
    window.DNODesktop = { element, button, icon, open, close, setTheme, windows,
        visible: id => { const win = windows.get(id); return !!win && isVisible(win); } };
    layoutState();
    loadIcons();
})();
