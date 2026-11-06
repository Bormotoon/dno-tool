// Общие понятные сообщения о сетевых сбоях для рабочего стола и VNC.
(() => {
    'use strict';
    function message(error, fallback = 'Не удалось выполнить запрос. Повторите попытку') {
        const text = typeof error === 'string' ? error : error?.message || '';
        if (error?.name === 'AbortError') return 'Запрос отменён';
        if (error?.name === 'TimeoutError') return 'Сервер не ответил вовремя. Повторите попытку';
        const network = /failed to f?etch|fetch failed|network\s*error|network request failed|load failed|network connection (?:was )?lost|connection (?:was )?(?:closed|reset|refused)|ERR_(?:NETWORK|CONNECTION|INTERNET)|body stream.*(?:error|aborted)/i.test(text);
        if (network || ['TypeError', 'NetworkError'].includes(error?.name)) {
            return navigator.onLine === false
                ? 'Нет подключения к сети. Проверьте соединение'
                : 'Не удалось связаться с сервером. Проверьте подключение и повторите попытку';
        }
        if (error?.name === 'SyntaxError') return 'Не удалось прочитать ответ сервера. Повторите попытку';
        return text || fallback;
    }
    function normalize(error) {
        if (error?.name === 'AbortError') return error;
        const result = new Error(message(error));
        if (error?.status) result.status = error.status;
        return result;
    }
    window.DNOWebErrors = Object.freeze({ message, normalize });
})();
