// Файлы хоста: одна удалённая панель, локальной стороной служит браузер.
(() => {
    'use strict';
    const T = window.DNODesktopTools, A = window.DNODesktopAPI;
    const { el, iconAction, json, makeTool, errorMessage, table } = T;
    const typeNames = { dir: 'Каталог', file: 'Файл', link: 'Ссылка', other: 'Другое' };

    function formatSize(value) {
        if (!Number.isFinite(value)) return '—';
        if (value < 1024) return `${value} Б`;
        const units = ['КиБ', 'МиБ', 'ГиБ', 'ТиБ'];
        let size = value, index = -1;
        do { size /= 1024; index += 1; } while (size >= 1024 && index < units.length - 1);
        return `${size.toLocaleString('ru-RU', { maximumFractionDigits: 1 })} ${units[index]}`;
    }
    function joinPath(directory, name) { return directory.endsWith('/') ? `${directory}${name}` : `${directory}/${name}`; }

    const tool = makeTool('files', 'Файлы', 'Просмотр, загрузка и скачивание файлов хоста по SFTP.');

    function createTab(iid, root) {
        const base = `/api/tools/files/${encodeURIComponent(iid)}`;
        let current = '', parent = null, entries = [], generation = 0, listing = null, uploadXhr = null;
        const checked = new Set();

        // Панель повторяет командер SFTP в Qt: те же значки темы и порядок, затем строка пути.
        const up = iconAction('menu_up', 'Вверх', () => { if (parent) load(parent); });
        const refresh = iconAction('menu_refresh', 'Обновить', () => load(current));
        const mkdir = iconAction('menu_add_folder', 'Новый каталог', makeDirectory);
        const picker = el('input'); picker.type = 'file'; picker.multiple = true; picker.hidden = true;
        picker.addEventListener('change', () => { const files = [...picker.files]; picker.value = ''; uploadFiles(files); });
        const uploadButton = iconAction('menu_upload', 'Загрузить…', () => picker.click());
        uploadButton.title = 'Загрузить файлы… Их можно и перетащить в окно';
        const cancelUpload = iconAction('menu_abort', 'Отменить загрузку', () => uploadXhr?.abort()); cancelUpload.hidden = true;
        const download = iconAction('menu_download', 'Скачать', downloadSelected);
        const rename = iconAction('menu_rename', 'Переименовать', renameSelected);
        const remove = iconAction('menu_delete', 'Удалить', removeSelected);
        const separator = () => el('span', 'tool-separator');
        const tools = el('div', 'tool-toolbar files-actions');
        tools.setAttribute('role', 'toolbar');
        tools.setAttribute('aria-label', 'Действия с файлами');
        tools.append(up, separator(), refresh, mkdir, separator(), uploadButton, cancelUpload, picker, download, rename, remove);

        const address = el('input', 'tool-search files-path');
        address.setAttribute('aria-label', 'Путь на хосте');
        address.addEventListener('keydown', event => { if (event.key === 'Enter') load(address.value.trim()); });
        const nav = el('div', 'tool-toolbar files-nav');
        nav.append(address);

        const state = el('div', 'tool-status');
        state.setAttribute('role', 'status');
        state.setAttribute('aria-live', 'polite');
        const progress = el('progress', 'tool-progress'); progress.hidden = true; progress.max = 100;
        progress.setAttribute('aria-label', 'Прогресс загрузки');
        const list = table(['', 'Имя', 'Размер', 'Тип', 'Изменён']);
        list.wrap.classList.add('files-list');
        root.append(tools, nav, progress, state, list.wrap);
        root.addEventListener('dragover', event => { event.preventDefault(); });
        root.addEventListener('drop', event => {
            event.preventDefault();
            const files = [...(event.dataTransfer?.files || [])];
            if (files.length) uploadFiles(files);
        });

        function report(text, error = false) {
            state.textContent = text ? errorMessage(text) : '';
            state.classList.toggle('error', error);
        }
        function selection() { return entries.filter(entry => checked.has(entry.name)); }
        function syncButtons() {
            const items = selection();
            remove.disabled = !items.length;
            rename.disabled = items.length !== 1;
            download.disabled = !items.some(entry => entry.type === 'file');
        }
        function render() {
            list.body.replaceChildren();
            const sorted = [...entries].sort((a, b) => (a.type === 'dir') === (b.type === 'dir') ? a.name.localeCompare(b.name, 'ru') : a.type === 'dir' ? -1 : 1);
            for (const entry of sorted) {
                const row = el('tr');
                const box = el('input'); box.type = 'checkbox'; box.checked = checked.has(entry.name);
                box.setAttribute('aria-label', `Выбрать ${entry.name}`);
                box.addEventListener('change', () => {
                    if (box.checked) checked.add(entry.name); else checked.delete(entry.name);
                    syncButtons();
                });
                const boxCell = el('td'); boxCell.append(box);
                const nameCell = el('td', 'files-name');
                // Значок папки только у каталогов, как в Qt; место под него держит имена на одной линии.
                const glyph = entry.type === 'dir' ? window.DNODesktop.icon('folder') : el('span', 'app-icon');
                // Цель ссылки может быть каталогом: сервер это проверит при переходе.
                if (entry.type === 'dir' || entry.type === 'link') {
                    const link = T.action(entry.name, () => load(joinPath(current, entry.name)));
                    link.className = 'files-name-link';
                    link.prepend(glyph);
                    nameCell.append(link);
                } else nameCell.append(glyph, el('span', '', entry.name));
                const modified = entry.modified ? new Date(entry.modified * 1000).toLocaleString('ru-RU') : '—';
                row.append(boxCell, nameCell, el('td', '', entry.type === 'dir' ? '' : formatSize(entry.size)), el('td', '', typeNames[entry.type] || ''), el('td', '', modified));
                list.body.append(row);
            }
            if (!entries.length) {
                const row = el('tr'), cell = el('td', 'tool-note', 'Каталог пуст');
                cell.colSpan = 5; row.append(cell); list.body.append(row);
            }
            syncButtons();
        }
        async function load(path) {
            listing?.abort();
            const own = new AbortController(), mine = ++generation;
            listing = own;
            report('Загрузка каталога…');
            try {
                const data = await json(`${base}/list?path=${encodeURIComponent(path || '')}`, null, own.signal);
                if (mine !== generation) return;
                current = data.path; parent = data.parent; entries = data.entries;
                checked.clear();
                address.value = current; up.disabled = !parent;
                render();
                report(data.truncated ? `Показаны первые ${entries.length} объектов` : `Объектов: ${entries.length}`);
            } catch (error) {
                if (error.name !== 'AbortError' && mine === generation) report(error.message, true);
            } finally { if (listing === own) listing = null; }
        }
        function sendFile(file, replace) {
            return new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();
                uploadXhr = xhr;
                const query = new URLSearchParams({ directory: current, name: file.name, replace: String(replace) });
                xhr.open('POST', `${base}/upload?${query}`);
                for (const [name, value] of Object.entries(A.headers())) xhr.setRequestHeader(name, value);
                xhr.setRequestHeader('Content-Type', 'application/octet-stream');
                xhr.upload.onprogress = event => {
                    if (!event.lengthComputable) return;
                    progress.value = Math.round(event.loaded * 100 / event.total);
                    report(`${file.name}: ${formatSize(event.loaded)} из ${formatSize(event.total)}`);
                };
                xhr.onload = () => {
                    let data = {};
                    try { data = JSON.parse(xhr.responseText); } catch { /* ответ прокси */ }
                    if (xhr.status >= 200 && xhr.status < 300) resolve();
                    else reject(new Error(typeof data.detail === 'string' ? data.detail : `Ошибка сервера: ${xhr.status}`));
                };
                xhr.onerror = () => reject(new Error('Ошибка соединения при загрузке файла'));
                xhr.onabort = () => reject(new DOMException('Загрузка отменена', 'AbortError'));
                xhr.send(file);
            });
        }
        async function uploadFiles(files) {
            if (uploadXhr || !files.length) return;
            uploadButton.disabled = true; cancelUpload.hidden = false; progress.hidden = false;
            let done = 0;
            try {
                for (const file of files) {
                    const target = joinPath(current, file.name);
                    const info = await json(`${base}/stat?path=${encodeURIComponent(target)}`);
                    let replace = false;
                    if (info.exists) {
                        if (info.type !== 'file') throw new Error(`«${file.name}» уже существует и не является обычным файлом`);
                        if (!window.confirm(`Файл «${file.name}» уже существует. Заменить?`)) continue;
                        replace = true;
                    }
                    progress.value = 0;
                    await sendFile(file, replace);
                    done += 1;
                }
                await load(current);
                report(`Загружено файлов: ${done}`);
            } catch (error) {
                if (done) await load(current);
                report(error.name === 'AbortError' ? 'Загрузка отменена, файл на хосте не изменён' : error.message, error.name !== 'AbortError');
            } finally {
                uploadXhr = null;
                uploadButton.disabled = false; cancelUpload.hidden = true; progress.hidden = true;
            }
        }
        async function downloadSelected() {
            const files = selection().filter(entry => entry.type === 'file');
            try {
                for (const file of files) {
                    const data = await json(`${base}/download-ticket`, { path: joinPath(current, file.name) });
                    const link = el('a');
                    link.href = `/api/tools/files/download/${encodeURIComponent(data.ticket)}`;
                    link.download = file.name;
                    link.hidden = true;
                    root.append(link);
                    link.click();
                    link.remove();
                }
                if (files.length) report(`Запущено скачиваний: ${files.length}`);
            } catch (error) { report(error.message, true); }
        }
        async function makeDirectory() {
            const name = window.prompt('Имя нового каталога');
            if (!name) return;
            try { await json(`${base}/mkdir`, { path: joinPath(current, name) }); await load(current); }
            catch (error) { report(error.message, true); }
        }
        async function renameSelected() {
            const [item] = selection();
            if (!item) return;
            const name = window.prompt('Новое имя', item.name);
            if (!name || name === item.name) return;
            try {
                await json(`${base}/rename`, { source: joinPath(current, item.name), destination: joinPath(current, name) });
                await load(current);
            } catch (error) { report(error.message, true); }
        }
        async function removeSelected() {
            const items = selection();
            if (!items.length) return;
            const text = items.length === 1 ? `Удалить «${items[0].name}»?` : `Удалить объектов: ${items.length}?`;
            if (!window.confirm(`${text} Каталоги удаляются вместе с содержимым, вернуть их нельзя.`)) return;
            remove.disabled = true;
            report('Удаление…');
            try {
                const data = await json(`${base}/delete`, { paths: items.map(entry => joinPath(current, entry.name)) });
                await load(current);
                if (data.errors.length) report(`Удалено: ${data.removed}. Ошибки: ${data.errors.slice(0, 3).join('; ')}`, true);
                else report(`Удалено объектов: ${data.removed}`);
            } catch (error) { report(error.message, true); syncButtons(); }
        }
        load('');
        return { close() { generation += 1; listing?.abort(); uploadXhr?.abort(); } };
    }

    T.makeHostTabs(tool, { limit: 8, createTab });
    T.activate(tool);
})();
