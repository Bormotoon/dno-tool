// SSH-терминал: xterm.js получает байты напрямую, сеанс живёт, пока открыта вкладка.
(() => {
    'use strict';
    const T = window.DNODesktopTools, A = window.DNODesktopAPI;
    const { el, action, makeTool } = T;
    const closeReasons = {
        4403: 'Терминал работает только по HTTPS. Включите HTTPS в настройках веб-сервера или откройте интерфейс на самом компьютере',
        4404: 'Устройство не найдено',
        4429: 'Открыто максимальное число терминалов',
        4401: 'Войдите в систему заново',
    };

    const tool = makeTool('terminal', 'SSH-терминал', 'Сеанс открывается от учётной записи из связки хоста и завершается при закрытии вкладки.');

    function createTab(iid, root) {
        const toolbar = el('div', 'tool-toolbar terminal-toolbar');
        const state = el('div', 'tool-status');
        state.setAttribute('role', 'status');
        state.setAttribute('aria-live', 'polite');
        const reconnect = action('Подключиться заново', connect); reconnect.hidden = true;
        toolbar.append(state, reconnect);
        const screen = el('div', 'terminal-screen');
        root.append(toolbar, screen);

        const term = new window.Terminal({
            cursorBlink: true, fontSize: 13, scrollback: 5000, convertEol: false,
            fontFamily: 'ui-monospace, "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace',
            theme: { background: '#0d1117', foreground: '#d7dde5' },
        });
        const fit = new window.FitAddon.FitAddon();
        term.loadAddon(fit);
        term.open(screen);
        let socket = null, disposed = false, resizeTimer = 0;
        const encoder = new TextEncoder();

        // Пустая панель скрыта: иначе над экраном терминала остаётся лишняя полоса.
        function syncToolbar() { toolbar.hidden = !state.textContent && reconnect.hidden; }
        function report(text, error = false) {
            state.textContent = text ? T.errorMessage(text) : '';
            state.classList.toggle('error', error);
            syncToolbar();
        }
        function sendSize() {
            if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
        }
        function fitNow() {
            if (root.hidden || !screen.clientWidth || !screen.clientHeight) return;
            try { fit.fit(); } catch { /* окно ещё не получило размер */ }
        }
        function connect() {
            if (socket) return;
            reconnect.hidden = true;
            report('Подключение…');
            fitNow();
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const query = new URLSearchParams({ token: A.token(), cols: term.cols, rows: term.rows });
            const own = new WebSocket(`${proto}//${location.host}/ws/terminal/${encodeURIComponent(iid)}?${query}`);
            own.binaryType = 'arraybuffer';
            socket = own;
            let shown = false, serverError = '';
            own.onmessage = event => {
                if (socket !== own) return;
                if (typeof event.data === 'string') {
                    try {
                        const message = JSON.parse(event.data);
                        if (message.type === 'closed') serverError = message.error || '';
                    } catch { /* служебный кадр другой версии */ }
                    return;
                }
                if (!shown) { shown = true; report(''); }
                term.write(new Uint8Array(event.data));
            };
            own.onclose = event => {
                if (socket !== own) return;
                socket = null;
                if (disposed) return;
                reconnect.hidden = false;
                syncToolbar();
                const known = closeReasons[event.code];
                if (known || serverError) report(known || serverError, true);
                else report(shown ? 'Сеанс завершён' : 'Не удалось подключиться к серверу', !shown);
            };
        }
        term.onData(data => {
            if (socket?.readyState === WebSocket.OPEN) socket.send(encoder.encode(data));
        });
        term.onBinary(data => {
            if (socket?.readyState === WebSocket.OPEN) socket.send(Uint8Array.from(data, char => char.charCodeAt(0)));
        });
        term.onResize(sendSize);
        const observer = new ResizeObserver(() => {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(fitNow, 60);
        });
        observer.observe(screen);
        connect();
        return {
            onShow() { requestAnimationFrame(() => { fitNow(); term.focus(); }); },
            close() {
                disposed = true;
                clearTimeout(resizeTimer);
                observer.disconnect();
                const own = socket;
                socket = null;
                own?.close();
                term.dispose();
            },
        };
    }

    const tabs = T.makeHostTabs(tool, { limit: 8, createTab });
    window.addEventListener('dno:window', event => {
        if (event.detail.id === 'terminal' && event.detail.visible) tabs.active()?.onShow?.();
    });
    window.addEventListener('pagehide', tabs.closeAll);
    T.activate(tool);
})();
