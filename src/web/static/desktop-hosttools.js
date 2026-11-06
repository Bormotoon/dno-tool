// Общая основа окон хоста: вкладка на каждый хост, окно живёт, пока открыта вкладка.
(() => {
    'use strict';
    const T = window.DNODesktopTools, D = window.DNODesktop, A = window.DNODesktopAPI;
    const { el } = T;
    const openers = new Map();

    // Вкладки хранят только корневой узел: содержимое окно создаёт само.
    function makeHostTabs(tool, { limit, createTab }) {
        const tabs = new Map();
        let active = null;
        const bar = el('div', 'host-tabs-bar');
        bar.setAttribute('role', 'tablist');
        const body = el('div', 'host-tabs-body');
        tool.content.append(tool.status, bar, body);
        tool.content.classList.add('host-tabs-tool');

        function select(iid) {
            active = iid;
            for (const [key, tab] of tabs) {
                const selected = key === iid;
                tab.root.hidden = !selected;
                tab.button.classList.toggle('active', selected);
                tab.button.setAttribute('aria-selected', String(selected));
                if (selected) tab.onShow?.();
            }
        }
        function closeTab(iid) {
            const tab = tabs.get(iid);
            if (!tab) return;
            tab.close?.();
            tab.root.remove();
            tab.wrap.remove();
            tabs.delete(iid);
            if (active === iid) active = null;
            select(active ?? [...tabs.keys()].at(-1) ?? null);
            // Без вкладок окну нечего показывать, а без окна исчезает и значок в Dock.
            if (!tabs.size) D.close(tool.id);
        }
        function openHost(iid) {
            if (tabs.has(iid)) { select(iid); return; }
            if (tabs.size >= limit) {
                tool.message(`Открыто максимальное число вкладок: ${limit}. Закройте одну из них`, true);
                return;
            }
            tool.message('');
            const wrap = el('div', 'host-tab');
            const button = D.button(A.hostLabel(iid), () => select(iid), 'host-tab-button');
            button.setAttribute('role', 'tab');
            const closer = D.button('✕', () => closeTab(iid), 'host-tab-close');
            closer.setAttribute('aria-label', `Закрыть вкладку ${A.hostLabel(iid)}`);
            wrap.append(button, closer);
            bar.append(wrap);
            const root = el('div', 'host-tab-content');
            body.append(root);
            const tab = { button, root, wrap };
            tabs.set(iid, tab);
            Object.assign(tab, createTab(iid, root, A.hostLabel(iid)));
            select(iid);
        }
        function closeAll() { [...tabs.keys()].forEach(closeTab); }
        tool.refresh = () => {};
        tool.stop = () => {};
        tool.clear = closeAll;
        // Закрытие окна завершает сеансы: вернуться к ним из Dock уже нельзя.
        window.addEventListener('dno:window', event => {
            if (event.detail.id === tool.id && !event.detail.open) closeAll();
        });
        openers.set(tool.id, openHost);
        return { openHost, closeAll, active: () => (active ? tabs.get(active) : null), tabs };
    }

    // Единственная точка открытия окон хоста: кнопки на карточке устройства.
    T.openHostTool = (id, iid) => {
        const open = openers.get(id);
        if (!open) return;
        D.open(id);
        open(iid);
    };
    T.makeHostTabs = makeHostTabs;
})();
