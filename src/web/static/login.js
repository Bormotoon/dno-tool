// Страница входа: единственный скрипт, доступный без авторизации.
(() => {
    'use strict';
    const panel = document.getElementById('panel');
    const form = document.getElementById('form');
    const submit = document.getElementById('submit');
    const status = document.getElementById('status');
    const RELOAD_FLAG = 'dno-tool_login_reload';

    function show(message = '') {
        panel.hidden = false;
        status.textContent = message;
        document.getElementById('user').focus();
    }
    // Если после перезагрузки снова открылся вход, браузер не сохранил cookie.
    // Повторная перезагрузка зациклилась бы, поэтому сообщаем о причине.
    function enter() {
        if (sessionStorage.getItem(RELOAD_FLAG)) {
            sessionStorage.removeItem(RELOAD_FLAG);
            show('Браузер не сохранил данные входа. Разрешите cookie для этого адреса');
            return;
        }
        sessionStorage.setItem(RELOAD_FLAG, '1');
        location.reload();
    }
    async function restore() {
        const token = localStorage.getItem('dno-tool_token');
        if (!token) { sessionStorage.removeItem(RELOAD_FLAG); show(); return; }
        try {
            const response = await fetch('/api/asset-session', { method: 'POST', headers: { Authorization: `Basic ${token}` } });
            if (response.ok) { enter(); return; }
            if (response.status === 401) localStorage.removeItem('dno-tool_token');
            show(response.status === 401 ? '' : `Ошибка сервера: ${response.status}`);
        } catch {
            show('Сервер недоступен');
        }
    }
    form.addEventListener('submit', async event => {
        event.preventDefault();
        submit.disabled = true;
        status.textContent = '';
        try {
            const response = await fetch('/api/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username: document.getElementById('user').value.trim(), password: document.getElementById('pass').value }),
            });
            if (response.status === 401) { status.textContent = 'Неверный логин или пароль'; return; }
            if (!response.ok) { status.textContent = `Ошибка сервера: ${response.status}`; return; }
            const data = await response.json();
            localStorage.setItem('dno-tool_token', data.token);
            if (data.session_id) localStorage.setItem('dno-tool_session', data.session_id);
            sessionStorage.removeItem(RELOAD_FLAG);
            enter();
        } catch {
            status.textContent = 'Сервер недоступен';
        } finally {
            submit.disabled = false;
        }
    });
    restore();
})();
