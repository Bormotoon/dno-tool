// DNO Tool — VNC viewer (отдельная вкладка, на базе noVNC 1.7.0)
// Параметры из URL: ?iid=<device-iid>&token=<token>&name=<label>
//
// UI реализован по образцу stock noVNC / Proxmox: вертикальный control bar
// слева, drag-handle, popout-панели (Power, Veyon, Clipboard, Settings, Extra keys).

import RFB from './lib/novnc.bundle.min.js';

// ===== Veyon compatibility patches =====
//
// Veyon присылает серверные сообщения, которых noVNC не знает:
//   - type 41 (Feature Message) — VariantArrayMessage с feature-параметрами.
//     noVNC в default-ветке _normalMsg вызывает _fail и рвёт соединение.
//   - После access-control dialog могут идти служебные сообщения Veyon.
//
// Monkey-patch _normalMsg: перехватываем type 41 и молча пропускаем его,
// корректно вычитая framing (4 байта BE length + payload), чтобы очередь
// не рассинхронизировалась.

// Префикс reverse proxy сохраняется в HTTP и WebSocket URL.
const VNC_BASE = location.pathname.slice(0, location.pathname.lastIndexOf('/'));

const ENC = {
    raw: 0,
    copyRect: 1,
    hextile: 5,
    zlib: 6,
    tight: 7,
    zrle: 16,
    jpeg: 21,
    tightPNG: -260,
    pseudoQualityLevel0: -32,
    pseudoCompressLevel0: -256,
    pseudoDesktopSize: -223,
    pseudoLastRect: -224,
    pseudoQEMUExtendedKeyEvent: -258,
    pseudoDesktopName: -307,
    pseudoFence: -312,
    pseudoContinuousUpdates: -313,
    pseudoCursor: -240,
    pseudoCursorWithAlpha: -239,
};

const _origNormalMsg = RFB.prototype._normalMsg;
RFB.prototype._normalMsg = function () {
    if (this._FBU.rects > 0) {
        return _origNormalMsg.call(this);
    }
    if (this._sock.rQlen() < 1) {
        return false;
    }
    const msgType = this._sock.rQpeek8();
    if (msgType === 41) {
        if (this._sock.rQwait('Veyon FeatureMessage header', 5, 1)) {
            return false;
        }
        this._sock.rQshift8();
        const len = this._sock.rQshift32();
        if (this._sock.rQwait('Veyon FeatureMessage payload', len, 0)) {
            return false;
        }
        this._sock.rQskipBytes(len);
        return true;
    }
    return _origNormalMsg.call(this);
};

const _origSendEncodings = RFB.prototype._sendEncodings;
RFB.prototype._sendEncodings = function () {
    const codec = this._dnoCodec || 'auto';
    if (codec === 'auto') {
        return _origSendEncodings.call(this);
    }
    const encs = [];
    if (codec === 'tight') {
        encs.push(ENC.tight, ENC.tightPNG, ENC.jpeg);
    } else if (codec === 'zlib') {
        encs.push(ENC.zlib, ENC.hextile);
    }
    encs.push(ENC.copyRect);
    encs.push(ENC.raw);
    if (codec === 'tight') {
        encs.push(ENC.pseudoQualityLevel0 + this._qualityLevel);
    }
    if (codec === 'tight' || codec === 'zlib') {
        encs.push(ENC.pseudoCompressLevel0 + this._compressionLevel);
    }
    encs.push(ENC.pseudoCursor);
    encs.push(ENC.pseudoCursorWithAlpha);
    encs.push(ENC.pseudoDesktopSize);
    encs.push(ENC.pseudoLastRect);
    encs.push(ENC.pseudoQEMUExtendedKeyEvent);
    encs.push(ENC.pseudoDesktopName);
    encs.push(ENC.pseudoFence);
    encs.push(ENC.pseudoContinuousUpdates);
    RFB.messages.clientEncodings(this._sock, encs);
};

// Область задаётся в координатах framebuffer: noVNC сам учитывает её при вводе мыши.
function desktopRegion(rfb) {
    const side = rfb._dnoScreenRegion;
    if (!['left', 'right'].includes(side) || rfb._fbWidth < 2 || !rfb._fbHeight) return null;
    const middle = Math.floor(rfb._fbWidth / 2);
    const x = side === 'right' ? middle : 0;
    return { x, y: 0, width: side === 'right' ? rfb._fbWidth - middle : middle, height: rfb._fbHeight };
}

const _origUpdateClip = RFB.prototype._updateClip;
RFB.prototype._updateClip = function () {
    const region = desktopRegion(this);
    if (!region) return _origUpdateClip.call(this);
    const display = this._display;
    display.clipViewport = true;
    display.viewportChangeSize(region.width, region.height);
    display.viewportChangePos(region.x - display.absX(0), region.y - display.absY(0));
    // Перетаскивание не должно сдвигать выбранную половину рабочего стола.
    this._setClippingViewport(false);
    this._fixScrollbars();
};

const _origUpdateScale = RFB.prototype._updateScale;
RFB.prototype._updateScale = function () {
    if (!desktopRegion(this)) return _origUpdateScale.call(this);
    const size = this._screenSize();
    this._display.autoscale(size.w, size.h);
    this._fixScrollbars();
};

// ===== UI namespace =====

const UI = {
    rfb: null,
    connected: false,
    desktopName: "",

    statusTimeout: null,
    hideKeyboardTimeout: null,
    idleControlbarTimeout: null,
    closeControlbarTimeout: null,

    controlbarGrabbed: false,
    controlbarDrag: false,
    controlbarMouseDownClientX: 0,
    controlbarMouseDownOffsetX: 0,
    controlbarMouseDownClientY: 0,
    controlbarMouseDownOffsetY: 0,

    inhibitReconnect: true,
    reconnectCallback: null,
    reconnectPassword: null,

    lastKeyboardinput: null,
    defaultKeyboardinputLen: 100,

    // Veyon/VNC state
    displayMode: 'vnc',
    veyonInstalled: false,
    vncAvailable: false,
    krfbAvailable: false,
    availabilityChecked: false,
    vncPassword: '',
    krfbPassword: '',
    manualClose: false,
    pendingCodec: null,
    lastDisconnectReason: '',

    iid: '',
    token: '',
    vt: '',            // capability-токен ноды (федерация), ?vt= в URL
    forcedView: null,  // ?view=1|0 — форс viewOnly (null = из localStorage)
    forcedBackend: null, // ?backend=vnc|krfb|veyon — форс режима (null = автодетект)
    name: '',
};

// ===== Helpers =====

function getCtrl(id) { return document.getElementById(id); }

// Заголовок Authorization для fetch (Basic из token); при vt аутентификация идёт в query.
UI.authHeaders = function () {
    const h = {};
    if (UI.token) h['Authorization'] = 'Basic ' + UI.token;
    return h;
};

// query-суффикс с аутентификацией: token ИЛИ vt.
UI.authQuery = function () {
    if (UI.token) return 'token=' + encodeURIComponent(UI.token);
    if (UI.vt) return 'vt=' + encodeURIComponent(UI.vt);
    return '';
};

UI.showStatus = function (text, statusType, time) {
    text = window.DNOWebErrors.message(text);
    const statusElem = getCtrl('vnc_status');
    if (typeof statusType === 'undefined') statusType = 'normal';
    // Ошибка не перекрывается обычными сообщениями, но успешное восстановление
    // соединения заменяет её: иначе плашка ошибки висит до клика.
    if (statusElem.classList.contains('vnc_open') && statusType !== 'success') {
        if (statusElem.classList.contains('vnc_status_error')) return;
        if (statusElem.classList.contains('vnc_status_warn') && statusType === 'normal') return;
    }
    clearTimeout(UI.statusTimeout);
    statusElem.classList.remove('vnc_status_error', 'vnc_status_warn', 'vnc_status_normal', 'vnc_status_success');
    statusElem.classList.add('vnc_status_' + statusType);
    statusElem.textContent = text;
    statusElem.classList.add('vnc_open');
    if (typeof time === 'undefined') time = 1500;
    if (statusType !== 'error') {
        UI.statusTimeout = window.setTimeout(UI.hideStatus, time);
    }
};

UI.hideStatus = function () {
    clearTimeout(UI.statusTimeout);
    getCtrl('vnc_status').classList.remove('vnc_open');
};

UI.toast = function (msg, kind) {
    msg = window.DNOWebErrors.message(msg);
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || '');
    el.textContent = msg;
    getCtrl('toastContainer').appendChild(el);
    setTimeout(() => { el.remove(); }, 4000);
};

// ===== Control bar =====

UI.activateControlbar = function () {
    clearTimeout(UI.idleControlbarTimeout);
    getCtrl('vnc_control_bar_anchor').classList.remove('vnc_idle');
    UI.idleControlbarTimeout = window.setTimeout(UI.idleControlbar, 2000);
};

UI.idleControlbar = function () {
    const bar = getCtrl('vnc_control_bar');
    if (bar.contains(document.activeElement) && document.hasFocus()) {
        UI.activateControlbar();
        return;
    }
    getCtrl('vnc_control_bar_anchor').classList.add('vnc_idle');
};

UI.keepControlbar = function () {
    clearTimeout(UI.closeControlbarTimeout);
};

UI.openControlbar = function () {
    getCtrl('vnc_control_bar').classList.add('vnc_open');
};

UI.closeControlbar = function () {
    UI.closeAllPanels();
    getCtrl('vnc_control_bar').classList.remove('vnc_open');
    if (UI.rfb) { try { UI.rfb.focus(); } catch (e) {} }
};

UI.toggleControlbar = function () {
    const bar = getCtrl('vnc_control_bar');
    if (bar.classList.contains('vnc_open')) {
        UI.closeControlbar();
    } else {
        UI.openControlbar();
    }
};

UI.getControlbarPos = function () {
    const anchor = getCtrl('vnc_control_bar_anchor');
    if (anchor.classList.contains('vnc_right')) return 'right';
    if (anchor.classList.contains('vnc_top')) return 'top';
    if (anchor.classList.contains('vnc_bottom')) return 'bottom';
    return 'left';
};

UI.toggleControlbarSide = function (pos) {
    const bar = getCtrl('vnc_control_bar');
    const barDisplayStyle = window.getComputedStyle(bar).display;
    if (barDisplayStyle !== 'none') {
        bar.style.transitionDuration = '0s';
        bar.addEventListener('transitionend', () => bar.style.transitionDuration = '', { once: true });
    }
    const anchor = getCtrl('vnc_control_bar_anchor');
    anchor.classList.remove('vnc_right', 'vnc_top', 'vnc_bottom');
    if (['right', 'top', 'bottom'].includes(pos)) {
        anchor.classList.add('vnc_' + pos);
    }
    localStorage.setItem('vncControlbarPos', pos);
    UI.controlbarDrag = true;
    UI.showControlbarHint(false, false);
};

UI.showControlbarHint = function (show, animate) {
    if (typeof animate === 'undefined') animate = true;
    const getPos = el =>
        ['right', 'top', 'bottom'].find(p => el.classList.contains('vnc_' + p)) ?? 'left';
    const anchor = getCtrl('vnc_control_bar_anchor');
    const anchorPos = getPos(anchor);
    document.querySelectorAll('.vnc_control_bar_hint').forEach((hint) => {
        const hintPos = getPos(hint.parentElement);
        const shouldShow = show && (hintPos !== anchorPos);
        hint.classList.toggle('vnc_active', shouldShow);
        hint.classList.toggle('vnc_notransition', !animate || !shouldShow);
    });
};

UI.controlbarHandleMouseDown = function (e) {
    if ((e.type === 'mousedown') && (e.button !== 0)) return;
    const ptr = e.touches ? e.touches[0] : e;
    const handle = getCtrl('vnc_control_bar_handle');
    const bounds = handle.getBoundingClientRect();
    if (e.type === 'mousedown') {
        if (handle.setCapture) handle.setCapture();
    }
    UI.controlbarGrabbed = true;
    UI.controlbarDrag = false;
    UI.showControlbarHint(true);
    UI.controlbarMouseDownClientY = ptr.clientY;
    UI.controlbarMouseDownOffsetY = ptr.clientY - bounds.top;
    UI.controlbarMouseDownClientX = ptr.clientX;
    UI.controlbarMouseDownOffsetX = ptr.clientX - bounds.left;
    e.preventDefault();
    e.stopPropagation();
    UI.keepControlbar();
    UI.activateControlbar();
};

UI.controlbarHandleMouseUp = function (e) {
    if ((e.type === 'mouseup') && (e.button !== 0)) return;
    if (UI.controlbarGrabbed && !UI.controlbarDrag) {
        UI.toggleControlbar();
        e.preventDefault();
        e.stopPropagation();
        UI.keepControlbar();
        UI.activateControlbar();
    }
    UI.controlbarGrabbed = false;
    UI.showControlbarHint(false);
};

UI.dragControlbarHandle = function (e) {
    if (!UI.controlbarGrabbed) return;
    const ptr = e.touches ? e.touches[0] : e;
    let controlBarPos = UI.getControlbarPos();

    const dragThreshold = 10;
    if (ptr.clientX < (window.innerWidth * 0.1) &&
        ptr.clientY > (window.innerHeight * 0.25) &&
        ptr.clientY < (window.innerHeight * 0.75)) {
        if (controlBarPos !== 'left') {
            UI.toggleControlbarSide('left');
            controlBarPos = 'left';
        }
    } else if (ptr.clientX > (window.innerWidth * 0.9) &&
               ptr.clientY > (window.innerHeight * 0.25) &&
               ptr.clientY < (window.innerHeight * 0.75)) {
        if (controlBarPos !== 'right') {
            UI.toggleControlbarSide('right');
            controlBarPos = 'right';
        }
    } else if (ptr.clientX > (window.innerWidth * 0.25) &&
               ptr.clientX < (window.innerWidth * 0.75) &&
               ptr.clientY < (window.innerHeight * 0.2)) {
        if (controlBarPos !== 'top') {
            UI.toggleControlbarSide('top');
            controlBarPos = 'top';
        }
    } else if (ptr.clientX > (window.innerWidth * 0.25) &&
               ptr.clientX < (window.innerWidth * 0.75) &&
               ptr.clientY > (window.innerHeight * 0.8)) {
        if (controlBarPos !== 'bottom') {
            UI.toggleControlbarSide('bottom');
            controlBarPos = 'bottom';
        }
    }

    const isVertical = controlBarPos === 'left' || controlBarPos === 'right';
    if (!UI.controlbarDrag) {
        const dragDistance = isVertical
            ? Math.abs(ptr.clientY - UI.controlbarMouseDownClientY)
            : Math.abs(ptr.clientX - UI.controlbarMouseDownClientX);
        if (dragDistance < dragThreshold) return;
        UI.controlbarDrag = true;
    }

    if (isVertical) {
        const eventY = ptr.clientY - UI.controlbarMouseDownOffsetY;
        UI.moveControlbarHandle(eventY, true);
    } else {
        const eventX = ptr.clientX - UI.controlbarMouseDownOffsetX;
        UI.moveControlbarHandle(eventX, false);
    }
    e.preventDefault();
    e.stopPropagation();
    UI.keepControlbar();
    UI.activateControlbar();
};

UI.moveControlbarHandle = function (viewportRelativeCoord, isVertical) {
    const handle = getCtrl('vnc_control_bar_handle');
    const handleSpan = isVertical ? handle.getBoundingClientRect().height : handle.getBoundingClientRect().width;
    const controlbarBounds = getCtrl('vnc_control_bar').getBoundingClientRect();
    const controlbarBoundsStart = isVertical ? controlbarBounds.top : controlbarBounds.left;
    const controlbarBoundsSpan = isVertical ? controlbarBounds.height : controlbarBounds.width;
    const margin = 10;
    if (handleSpan === 0 || controlbarBoundsSpan === 0) return;
    let newCoord = viewportRelativeCoord;
    if (newCoord < controlbarBoundsStart + margin) {
        newCoord = controlbarBoundsStart + margin;
    } else if (newCoord > controlbarBoundsStart + controlbarBoundsSpan - handleSpan - margin) {
        newCoord = controlbarBoundsStart + controlbarBoundsSpan - handleSpan - margin;
    }
    if (controlbarBoundsSpan < (handleSpan + margin * 2)) {
        newCoord = controlbarBoundsStart + (controlbarBoundsSpan - handleSpan) / 2;
    }
    const parentRelativeCoord = newCoord - controlbarBoundsStart;
    if (isVertical) {
        handle.style.transform = 'translateY(' + parentRelativeCoord + 'px)';
    } else {
        handle.style.transform = 'translateX(' + parentRelativeCoord + 'px)';
    }
};

UI.updateControlbarHandle = function () {
    const handle = getCtrl('vnc_control_bar_handle');
    const handleBounds = handle.getBoundingClientRect();
    const controlBarPos = UI.getControlbarPos();
    const isVertical = controlBarPos === 'left' || controlBarPos === 'right';
    if (isVertical) {
        UI.moveControlbarHandle(handleBounds.top, true);
    } else {
        UI.moveControlbarHandle(handleBounds.left, false);
    }
};

UI.toggleExpander = function (e) {
    this.classList.toggle('vnc_open');
};

// ===== Panels =====

UI.closeAllPanels = function () {
    UI.closeSettingsPanel();
    UI.closePowerPanel();
    UI.closeVeyonPanel();
    UI.closeClipboardPanel();
    UI.closeExtraKeys();
};

UI._closePanel = function (panelId, btnId) {
    const panel = getCtrl(panelId);
    const btn = getCtrl(btnId);
    if (panel) panel.classList.remove('vnc_open');
    if (btn) btn.classList.remove('vnc_selected');
};

UI._openPanel = function (panelId, btnId) {
    UI.closeAllPanels();
    UI.openControlbar();
    const panel = getCtrl(panelId);
    const btn = getCtrl(btnId);
    if (panel) panel.classList.add('vnc_open');
    if (btn) btn.classList.add('vnc_selected');
};

UI._togglePanel = function (panelId, btnId) {
    const panel = getCtrl(panelId);
    if (panel && panel.classList.contains('vnc_open')) {
        UI._closePanel(panelId, btnId);
    } else {
        UI._openPanel(panelId, btnId);
    }
};

UI.closeSettingsPanel = function () { UI._closePanel('vnc_settings', 'vnc_settings_button'); };
UI.openSettingsPanel = function () { UI._openPanel('vnc_settings', 'vnc_settings_button'); };
UI.toggleSettingsPanel = function () { UI._togglePanel('vnc_settings', 'vnc_settings_button'); };

UI.closePowerPanel = function () { UI._closePanel('vnc_power', 'vnc_power_button'); };
UI.openPowerPanel = function () { UI._openPanel('vnc_power', 'vnc_power_button'); };
UI.togglePowerPanel = function () { UI._togglePanel('vnc_power', 'vnc_power_button'); };

UI.closeVeyonPanel = function () { UI._closePanel('vnc_veyon', 'vnc_veyon_button'); };
UI.openVeyonPanel = function () { UI._openPanel('vnc_veyon', 'vnc_veyon_button'); };
UI.toggleVeyonPanel = function () { UI._togglePanel('vnc_veyon', 'vnc_veyon_button'); };

UI.closeClipboardPanel = function () { UI._closePanel('vnc_clipboard', 'vnc_clipboard_button'); };
UI.openClipboardPanel = function () { UI._openPanel('vnc_clipboard', 'vnc_clipboard_button'); };
UI.toggleClipboardPanel = function () { UI._togglePanel('vnc_clipboard', 'vnc_clipboard_button'); };

UI.closeExtraKeys = function () { UI._closePanel('vnc_modifiers', 'vnc_toggle_extra_keys_button'); };
UI.openExtraKeys = function () { UI._openPanel('vnc_modifiers', 'vnc_toggle_extra_keys_button'); };
UI.toggleExtraKeys = function () { UI._togglePanel('vnc_modifiers', 'vnc_toggle_extra_keys_button'); };

// ===== Visual state =====

UI.updateVisualState = function (state) {
    document.documentElement.classList.remove('vnc_connecting', 'vnc_connected', 'vnc_disconnecting', 'vnc_reconnecting');
    const transitionElem = getCtrl('vnc_transition_text');
    switch (state) {
        case 'init': break;
        case 'connecting':
            transitionElem.textContent = 'Подключение...';
            document.documentElement.classList.add('vnc_connecting');
            break;
        case 'connected':
            document.documentElement.classList.add('vnc_connected');
            break;
        case 'disconnecting':
            transitionElem.textContent = 'Отключение...';
            document.documentElement.classList.add('vnc_disconnecting');
            break;
        case 'disconnected': break;
        case 'reconnecting':
            transitionElem.textContent = 'Переподключение...';
            document.documentElement.classList.add('vnc_reconnecting');
            break;
    }
    if (UI.connected) {
        UI.updateViewClip();
        // Show drag viewport button when connected
        const dragBtn = getCtrl('vnc_view_drag_button');
        if (dragBtn) dragBtn.classList.remove('vnc_hidden');
        UI.closeControlbarTimeout = setTimeout(UI.closeControlbar, 2000);
    } else {
        UI.keepControlbar();
    }
    UI.closeAllPanels();
};

// ===== Fullscreen =====

UI.toggleFullscreen = function () {
    if (document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement) {
        if (document.exitFullscreen) document.exitFullscreen();
        else if (document.mozCancelFullScreen) document.mozCancelFullScreen();
        else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
    } else {
        const el = document.documentElement;
        if (el.requestFullscreen) el.requestFullscreen();
        else if (el.mozRequestFullScreen) el.mozRequestFullScreen();
        else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
    }
};

UI.updateFullscreenButton = function () {
    const btn = getCtrl('vnc_fullscreen_button');
    if (!btn) return;
    const supported = document.fullscreenEnabled || document.webkitFullscreenEnabled || document.mozFullScreenEnabled;
    if (supported) {
        btn.classList.remove('vnc_hidden');
    } else {
        btn.classList.add('vnc_hidden');
    }
};

// ===== Extra keys =====
// noVNC bundle exports only RFB, not KeyTable, so we use numeric keysyms.
// Standard X11 keysyms (from keysymdef.h):
const XK_Control_L = 0xFFE3;  // 65507
const XK_Alt_L     = 0xFFE9;  // 65513
const XK_Meta_L    = 0xFFE7;  // 65511 — Super/Windows key
const XK_Tab       = 0xFF09;  // 65289
const XK_Escape    = 0xFF1B;  // 65307

UI.toggleCtrl = function () {
    UI._toggleModifier(getCtrl('vnc_toggle_ctrl_button'), XK_Control_L, 'ControlLeft');
};
UI.toggleAlt = function () {
    UI._toggleModifier(getCtrl('vnc_toggle_alt_button'), XK_Alt_L, 'AltLeft');
};
UI.toggleWindows = function () {
    UI._toggleModifier(getCtrl('vnc_toggle_windows_button'), XK_Meta_L, 'MetaLeft');
};
UI._toggleModifier = function (btn, keysym, code) {
    if (!UI.rfb || !UI.connected) return;
    const baseTip = btn.dataset.baseTip || btn.dataset.tip || '';
    if (!btn.dataset.baseTip) btn.dataset.baseTip = baseTip;
    if (btn.classList.contains('vnc_selected')) {
        btn.classList.remove('vnc_selected');
        btn.dataset.tip = baseTip.replace('Отжать', 'Зажать');
        UI.rfb.sendKey(keysym, code, false);
    } else {
        btn.classList.add('vnc_selected');
        btn.dataset.tip = baseTip.replace('Зажать', 'Отжать');
        UI.rfb.sendKey(keysym, code, true);
    }
};
UI.sendTab = function () {
    if (!UI.rfb || !UI.connected) return;
    UI.rfb.sendKey(XK_Tab, 'Tab');
};
UI.sendEsc = function () {
    if (!UI.rfb || !UI.connected) return;
    UI.rfb.sendKey(XK_Escape, 'Escape');
};
UI.sendCtrlAltDel = function () {
    if (!UI.rfb || !UI.connected) return;
    UI.rfb.sendCtrlAltDel();
};

// ===== Clipboard =====

UI.clipboardSend = function () {
    if (!UI.rfb || !UI.connected) return;
    const text = getCtrl('vnc_clipboard_text').value;
    try { UI.rfb.clipboardPasteFrom(text); } catch (e) {}
};

UI.clipboardReceive = function (e) {
    getCtrl('vnc_clipboard_text').value = (e && e.detail && e.detail.text) || '';
};

// ===== Settings =====

UI.initSettings = function () {
    // Restore from localStorage
    const initChk = (id, key, def) => {
        const el = getCtrl(id);
        const v = localStorage.getItem(key);
        el.checked = (v === null) ? def : (v === 'true');
    };
    const initSel = (id, key, def) => {
        const el = getCtrl(id);
        const v = localStorage.getItem(key) || def;
        for (const opt of el.options) { if (opt.value === v) { el.value = v; break; } }
    };
    const initRange = (id, key, def) => {
        const el = getCtrl(id);
        const v = parseInt(localStorage.getItem(key), 10);
        el.value = isNaN(v) ? def : v;
    };

    initChk('vnc_setting_shared', 'vncShared', true);
    initChk('vnc_setting_view_only', 'vncViewOnly', false);
    // ?view= (нода: Просмотр/Управление) перекрывает сохранённую настройку в чекбоксе.
    if (UI.forcedView !== null) {
        const voEl = getCtrl('vnc_setting_view_only');
        if (voEl) voEl.checked = UI.forcedView;
    }
    initChk('vnc_setting_view_clip', 'vncViewClip', false);
    initSel('vnc_setting_resize', 'vncResize', 'scale');
    initSel('vnc_setting_screen_region', 'vncScreenRegion:' + UI.iid, 'all');
    initSel('vnc_setting_codec', 'vncCodec', 'auto');
    initRange('vnc_setting_quality', 'vncQuality', 6);
    initRange('vnc_setting_compression', 'vncCompression', 2);
};

UI.applyResizeMode = function () {
    const mode = getCtrl('vnc_setting_resize').value;
    localStorage.setItem('vncResize', mode);
    UI.applyScreenRegion();
};

UI.updateViewClip = function () {
    const clip = getCtrl('vnc_setting_view_clip').checked;
    localStorage.setItem('vncViewClip', clip);
    UI.applyScreenRegion();
};

UI.applyScreenRegion = function () {
    const region = getCtrl('vnc_setting_screen_region').value;
    const cropped = region !== 'all';
    getCtrl('vnc_setting_resize').disabled = cropped;
    getCtrl('vnc_setting_view_clip').disabled = cropped;
    if (!UI.rfb) return;
    UI.rfb._dnoScreenRegion = region;
    const mode = getCtrl('vnc_setting_resize').value;
    UI.rfb.resizeSession = !cropped && mode === 'remote' && UI.displayMode === 'vnc';
    UI.rfb.scaleViewport = cropped || mode === 'scale';
    UI.rfb.clipViewport = cropped || getCtrl('vnc_setting_view_clip').checked;
    UI.rfb._updateClip();
    UI.rfb._updateScale();
};

UI.updateScreenRegion = function () {
    localStorage.setItem('vncScreenRegion:' + UI.iid, getCtrl('vnc_setting_screen_region').value);
    UI.applyScreenRegion();
};

UI.updateViewOnly = function () {
    if (!UI.rfb) return;
    const vo = getCtrl('vnc_setting_view_only').checked;
    localStorage.setItem('vncViewOnly', vo);
    // ?view= форсирует режим до переподключения; настройка применяется на следующем connect().
    UI.rfb.viewOnly = (UI.forcedView !== null) ? UI.forcedView : vo;
};

UI.updateShared = function () {
    localStorage.setItem('vncShared', getCtrl('vnc_setting_shared').checked);
};

UI.updateQuality = function () {
    const v = parseInt(getCtrl('vnc_setting_quality').value, 10);
    localStorage.setItem('vncQuality', v);
    if (UI.rfb && UI.connected) UI.rfb.qualityLevel = v;
};

UI.updateCompression = function () {
    const v = parseInt(getCtrl('vnc_setting_compression').value, 10);
    localStorage.setItem('vncCompression', v);
    if (UI.rfb && UI.connected) UI.rfb.compressionLevel = v;
};

UI.applyCodec = function () {
    const sel = getCtrl('vnc_setting_codec');
    const codec = sel.value || 'auto';
    localStorage.setItem('vncCodec', codec);
    if (!UI.rfb || !UI.connected) return;
    UI.pendingCodec = codec;
    UI.lastDisconnectReason = 'codec changed';
    try { UI.rfb.disconnect(); } catch (e) {}
};

// ===== Connection =====

// Бюджет серверного handshake: TCP-connect до 10с + access-control Veyon до
// 60с. Клиентский таймаут с запасом — дальше ждать бессмысленно.
UI.CONNECT_TIMEOUT_MS = 90000;

UI.connect = function () {
    const codec = UI.pendingCodec || (localStorage.getItem('vncCodec') || 'auto');
    UI.pendingCodec = null;
    UI.lastDisconnectReason = '';
    UI.showStatus('Подключение к ' + UI.name + '...');

    const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${location.host}${VNC_BASE}/ws/vnc/${encodeURIComponent(UI.iid)}?${UI.authQuery()}&mode=${UI.displayMode}`;
    const rfbOpts = { wsProtocols: ['binary'], shared: (localStorage.getItem('vncShared') !== 'false') };
    // Пароль один на оба бекенда (/api/krfb/password и /api/vnc/password
    // отдают один и тот же veyon.vnc_password). Veyon-режим - без VNC-пароля
    // (RSA handshake выполняет серверный прокси).
    const rfbPassword = (UI.displayMode === 'krfb')
        ? (UI.krfbPassword || '')
        : (UI.vncPassword || '');
    if (UI.displayMode !== 'veyon' && rfbPassword) {
        rfbOpts.credentials = { password: rfbPassword };
    }
    const target = document.getElementById('vncCanvas');
    UI.rfb = new RFB(target, wsUrl, rfbOpts);
    UI.rfb._dnoCodec = codec;
    UI.rfb.qualityLevel = parseInt(localStorage.getItem('vncQuality') || '6', 10);
    UI.rfb.compressionLevel = parseInt(localStorage.getItem('vncCompression') || '2', 10);
    UI.rfb.showDotCursor = true;
    UI.applyScreenRegion();
    // ?view=1|0 форсирует режим (нода: «Просмотр»/«Управление»); без параметра — настройка из localStorage.
    UI.rfb.viewOnly = (UI.forcedView !== null) ? UI.forcedView : (localStorage.getItem('vncViewOnly') === 'true');

    UI.updateVisualState('connecting');

    UI.rfb.addEventListener('connect', UI.connectFinished);
    UI.rfb.addEventListener('disconnect', UI.disconnectFinished);
    UI.rfb.addEventListener('securityfailure', UI.securityFailed);
    UI.rfb.addEventListener('credentialsrequired', UI.credentialsRequired);
    UI.rfb.addEventListener('clipboard', UI.clipboardReceive);
    UI.rfb.addEventListener('desktopname', UI.updateDesktopName);

    // Страховка от вечного «Подключения...»: сервер может молчать дольше
    // бюджета handshake (очередь пула, зависший connect) — тогда рвём сами,
    // дальше отработает disconnectFinished (failover/ошибка).
    try { clearTimeout(UI._connectTimer); } catch (e) {}
    UI._connectTimer = setTimeout(function () {
        if (UI.rfb && !UI.connected && !UI.manualClose) {
            UI.lastDisconnectReason = 'timeout';
            UI.connectionFailed = true;
            UI.showStatus('Превышено время ожидания подключения', 'error');
            try { UI.rfb.disconnect(); } catch (e) {}
        }
    }, UI.CONNECT_TIMEOUT_MS);
};

UI.connectFinished = function (e) {
    UI.connected = true;
    UI.inhibitReconnect = false;
    try { clearTimeout(UI._connectTimer); } catch (e) {}
    const modeLabel = UI._modeLabel(UI.displayMode);
    if (UI.connectionFailed) {
        // После обрыва или неудачной попытки: зелёная плашка вместо ошибки,
        // через 2,5 с скрывается.
        UI.connectionFailed = false;
        UI.showStatus('Подключение восстановлено' + modeLabel, 'success', 2500);
    } else {
        UI.showStatus('Подключено' + modeLabel + ' к ' + UI.desktopName);
        UI.toast('Подключено' + modeLabel, 'success');
    }
    UI.updateVisualState('connected');
    try { UI.rfb.focus(); } catch (e) {}
};

// Метка режима для статус-бара/тостов
UI._modeLabel = function (mode) {
    if (mode === 'krfb') return ' (KRFB)';
    if (mode === 'veyon') return ' (Veyon)';
    return ' (VNC)';
};

UI.disconnectFinished = function (e) {
    const wasConnected = UI.connected;
    UI.connected = false;
    const r = UI.rfb;
    UI.rfb = undefined;
    try { clearTimeout(UI._connectTimer); } catch (ex) {}

    if (!e.detail.clean) {
        if (UI.manualClose) {
            // clean manual close — window.close() already called
            return;
        }
        UI.updateVisualState('disconnected');
        UI.connectionFailed = true;
        if (wasConnected) {
            UI.showStatus('Соединение потеряно — переподключение...', 'error');
        } else {
            UI.showStatus('Не удалось подключиться', 'error');
        }
    }

    if (UI.manualClose) return;

    // Ранний обрыв (соединение так и не установилось): режим мог быть выбран
    // по ложно-отрицательной первой пробе (холодный Veyon Server не ответил
    // за 2с → displayMode упал в 'vnc'). Один раз перепроверяем детект и,
    // если режим изменился, переподключаемся в нём — раньше для этого
    // требовалось вручную обновлять страницу. Дальше — обычный цикл.
    if (!wasConnected && !UI._modeFailoverDone) {
        UI._modeFailoverDone = true;
        UI._failoverReconnect();
        return;
    }

    // Auto-reconnect
    if (!UI.inhibitReconnect) {
        const delay = (UI.lastDisconnectReason === 'codec changed') ? 0 : 3000;
        UI.updateVisualState('reconnecting');
        UI.reconnectCallback = setTimeout(UI.reconnect, delay);
        return;
    }

    UI.updateVisualState('disconnected');
};

UI._failoverReconnect = async function () {
    if (UI.manualClose) return;
    // Форсированный backend (?backend=) не фейловерим — строго выбранный
    // сервер (открывается из UI dno-tool).
    if (UI.forcedBackend) {
        if (!UI.inhibitReconnect) {
            UI.updateVisualState('reconnecting');
            UI.reconnectCallback = setTimeout(UI.reconnect, 3000);
        } else {
            UI.updateVisualState('disconnected');
        }
        return;
    }
    const oldMode = UI.displayMode;
    UI.updateVisualState('reconnecting');
    UI.showStatus('Перепроверка режима подключения...');
    try {
        await UI.fetchBackend();
    } catch (e) { /* остаются предыдущие значения */ }
    if (UI.manualClose) return;
    if (UI.displayMode !== oldMode) {
        UI.showStatus(
            'Режим: ' + UI._modeLabel(UI.displayMode).trim() + ' — переподключение...'
        );
        UI.reconnectCallback = setTimeout(UI.reconnect, 500);
        return;
    }
    // Режим подтвердился — дальше обычный цикл reconnect (кейс «цель
    // загружается»: сервер поднимется — подхватим).
    if (!UI.inhibitReconnect) {
        UI.updateVisualState('reconnecting');
        UI.reconnectCallback = setTimeout(UI.reconnect, 3000);
        return;
    }
    UI.updateVisualState('disconnected');
};

UI.reconnect = function () {
    UI.reconnectCallback = null;
    if (UI.inhibitReconnect) return;
    UI.connect();
};

UI.cancelReconnect = function () {
    if (UI.reconnectCallback !== null) {
        clearTimeout(UI.reconnectCallback);
        UI.reconnectCallback = null;
    }
    UI.updateVisualState('disconnected');
    UI.openControlbar();
};

UI.securityFailed = function (e) {
    const msg = (e && e.detail && e.detail.reason)
        ? ('Доступ запрещён: ' + e.detail.reason)
        : 'Доступ запрещён';
    UI.showStatus(msg, 'error');
    UI.toast(msg, 'error');
};

UI.credentialsRequired = function (e) {
    const types = (e && e.detail && e.detail.types) || [];
    if (!types.includes('password')) return;
    if (UI.displayMode === 'krfb') {
        if (UI.krfbPassword) UI.rfb.sendCredentials({ password: UI.krfbPassword });
    } else if (UI.vncPassword) {
        UI.rfb.sendCredentials({ password: UI.vncPassword });
    }
};

UI.updateDesktopName = function (e) {
    const dn = (e && e.detail && e.detail.name) || '';
    if (dn) {
        UI.desktopName = dn;
        document.title = 'VNC: ' + dn;
    }
};

UI.disconnect = function () {
    UI.manualClose = true;
    UI.inhibitReconnect = true;
    try { clearTimeout(UI._connectTimer); } catch (e) {}
    if (UI.reconnectCallback) { clearTimeout(UI.reconnectCallback); UI.reconnectCallback = null; }
    if (UI.rfb) { try { UI.rfb.disconnect(); } catch (e) {} }
    window.close();
};

// ===== Backend (mode detection) =====

UI.fetchBackend = async function () {
    try {
        // Аутентификация детекции режима: при vt (федерация нод) заголовка
        // Basic нет — токен идёт в query. Без него _vnc_viewer_auth отвечает
        // 401, оба available=null, displayMode падает в дефолтный 'vnc' и
        // вьювер ломится к порту 5900 даже на хосте только с Veyon
        // («Не удалось подключиться»).
        const authQ = UI.authQuery();
        const authSuffix = authQ ? '&' + authQ : '';
        const [vncRes, veyonRes, vncPwdRes, krfbPwdRes, krfbAvRes] = await Promise.all([
            fetch(VNC_BASE + '/api/vnc/available?iid=' + encodeURIComponent(UI.iid) + authSuffix, { headers: UI.authHeaders() })
                .then(r => r.ok ? r.json() : null).catch(() => null),
            fetch(VNC_BASE + '/api/veyon/available?iid=' + encodeURIComponent(UI.iid) + authSuffix, { headers: UI.authHeaders() })
                .then(r => r.ok ? r.json() : null).catch(() => null),
            fetch(VNC_BASE + '/api/vnc/password?iid=' + encodeURIComponent(UI.iid) + authSuffix, { headers: UI.authHeaders() })
                .then(r => r.ok ? r.json() : null).catch(() => null),
            fetch(VNC_BASE + '/api/krfb/password?iid=' + encodeURIComponent(UI.iid) + authSuffix, { headers: UI.authHeaders() })
                .then(r => r.ok ? r.json() : null).catch(() => null),
            fetch(VNC_BASE + '/api/krfb/available?iid=' + encodeURIComponent(UI.iid) + authSuffix, { headers: UI.authHeaders() })
                .then(r => r.ok ? r.json() : null).catch(() => null),
        ]);
        UI.vncAvailable = !!(vncRes && vncRes.available);
        UI.veyonInstalled = !!(veyonRes && veyonRes.available);
        UI.krfbAvailable = !!(krfbAvRes && krfbAvRes.available); // TCP krfb-порт открыт
        UI.availabilityChecked = !!(vncRes !== null || veyonRes !== null);
        UI.vncPassword = (vncPwdRes && vncPwdRes.password) ? vncPwdRes.password : '';
        UI.krfbPassword = (krfbPwdRes && krfbPwdRes.password) ? krfbPwdRes.password : '';

        if (UI.forcedBackend) {
            // Строго выбранный сервер (открывается из UI dno-tool с ?backend=)
            UI.displayMode = UI.forcedBackend;
        } else if (UI.vncAvailable) {
            UI.displayMode = 'vnc';
        } else if (UI.krfbAvailable) {
            UI.displayMode = 'krfb';
        } else if (UI.veyonInstalled) {
            UI.displayMode = 'veyon';
        } else {
            UI.displayMode = 'vnc';
        }
    } catch (e) {
        UI.displayMode = UI.forcedBackend || 'vnc';
    }

    // Veyon button visibility
    const veyonBtn = getCtrl('vnc_veyon_button');
    if (veyonBtn) {
        veyonBtn.classList.toggle('vnc_hidden', !UI.veyonInstalled);
    }

    // Veyon banner
    const banner = getCtrl('vncVeyonBanner');
    if (banner) {
        banner.classList.toggle('hidden', UI.displayMode !== 'veyon');
    }
};

// ===== Power =====

UI.hostPower = async function (action) {
    try {
        const res = await fetch(VNC_BASE + '/api/host/power?' + UI.authQuery(), {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, UI.authHeaders()),
            body: JSON.stringify({ iid: UI.iid, action }),
        });
        if (!res.ok) {
            const txt = await res.text().catch(() => '');
            UI.toast('Ошибка: ' + res.status + ' ' + txt, 'error');
            return;
        }
        const data = await res.json();
        if (data.success) {
            const labels = { reboot: 'Перезагрузка отправлена', powerdown: 'Выключение отправлено', logoff: 'Завершение сеанса отправлено' };
            UI.toast(labels[action] || action + ' — выполнено', 'success');
        } else {
            UI.toast('Действие «' + action + '» не выполнено', 'error');
        }
    } catch (e) {
        UI.toast(window.DNOWebErrors.message(e), 'error');
    }
};

// ===== Veyon features =====

UI.veyonFeature = async function (feature, args) {
    try {
        const payload = args ? { iid: UI.iid, feature, arguments: args } : { iid: UI.iid, feature };
        const res = await fetch(VNC_BASE + '/api/veyon/feature?' + UI.authQuery(), {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, UI.authHeaders()),
            body: JSON.stringify(payload),
        });
        if (!res.ok) {
            const txt = await res.text().catch(() => '');
            UI.toast('Ошибка: ' + res.status + ' ' + txt, 'error');
            return;
        }
        const data = await res.json();
        if (data.success) {
            const labels = {
                lock: 'Блокировка экрана', unlock: 'Разблокировка экрана',
                reboot: 'Перезагрузка', powerdown: 'Выключение',
                message: 'Сообщение отправлено', power_on: 'Включение (WoL)',
                restart_service: 'Перезагрузка сервиса Veyon',
                ssh_lock: 'Экран заблокирован', ssh_unlock: 'Блокировка экрана снята',
                ssh_lock_input: 'Ввод заблокирован', ssh_unlock_input: 'Блокировка ввода снята',
            };
            UI.toast(labels[feature] || feature + ' — выполнено', 'success');
        } else {
            const reason = data.error ? ': ' + data.error : '';
            UI.toast('Функция «' + feature + '» не выполнена' + reason, 'error');
        }
    } catch (e) {
        UI.toast(window.DNOWebErrors.message(e), 'error');
    }
};

UI.veyonFeatureWithPrompt = async function (feature, prompts) {
    const args = {};
    for (const p of prompts) {
        const val = prompt(p.label + (p.placeholder ? ` (напр. ${p.placeholder})` : ''));
        if (val === null) return;
        const v = val.trim();
        if (p.required && !v) { UI.toast('Поле «' + p.label + '» обязательно', 'error'); return; }
        args[p.key] = v;
    }
    await UI.veyonFeature(feature, args);
};

// ===== Handlers setup =====

UI.addControlbarHandlers = function () {
    const bar = getCtrl('vnc_control_bar');
    bar.addEventListener('mousemove', UI.activateControlbar);
    bar.addEventListener('mouseup', UI.activateControlbar);
    bar.addEventListener('mousedown', UI.activateControlbar);
    bar.addEventListener('keydown', UI.activateControlbar);
    bar.addEventListener('mousedown', UI.keepControlbar);
    bar.addEventListener('keydown', UI.keepControlbar);
    bar.addEventListener('touchstart', UI.activateControlbar);
    bar.addEventListener('touchmove', UI.activateControlbar);
    bar.addEventListener('touchend', UI.activateControlbar);
    bar.addEventListener('touchstart', UI.keepControlbar);

    const handle = getCtrl('vnc_control_bar_handle');
    handle.addEventListener('mousedown', UI.controlbarHandleMouseDown);
    handle.addEventListener('mouseup', UI.controlbarHandleMouseUp);
    handle.addEventListener('mousemove', UI.dragControlbarHandle);
    handle.addEventListener('touchstart', UI.controlbarHandleMouseDown);
    handle.addEventListener('touchend', UI.controlbarHandleMouseUp);
    handle.addEventListener('touchmove', UI.dragControlbarHandle);

    window.addEventListener('resize', UI.updateControlbarHandle);

    // Drag viewport button
    const dragBtn = getCtrl('vnc_view_drag_button');
    if (dragBtn) dragBtn.addEventListener('click', UI.toggleViewDrag);

    // Keyboard button (touch) — focus canvas to bring up soft keyboard
    const kbBtn = getCtrl('vnc_keyboard_button');
    if (kbBtn) kbBtn.addEventListener('click', UI.toggleVirtualKeyboard);

    // Expanders
    document.querySelectorAll('.vnc_expander').forEach(el => el.addEventListener('click', UI.toggleExpander));
};

UI.toggleViewDrag = function () {
    if (!UI.rfb) return;
    const btn = getCtrl('vnc_view_drag_button');
    if (UI.rfb.dragViewport) {
        UI.rfb.dragViewport = false;
        btn.classList.remove('vnc_selected');
    } else {
        UI.rfb.dragViewport = true;
        btn.classList.add('vnc_selected');
    }
};

UI.toggleVirtualKeyboard = function () {
    if (!UI.rfb || !UI.connected) return;
    const input = getCtrl('vnc_keyboardinput');
    if (input.focus === document.activeElement) {
        input.blur();
    } else {
        input.focus();
    }
    try { UI.rfb.focus(); } catch (e) {}
};

UI.addExtraKeysHandlers = function () {
    getCtrl('vnc_toggle_extra_keys_button').addEventListener('click', UI.toggleExtraKeys);
    getCtrl('vnc_toggle_ctrl_button').addEventListener('click', UI.toggleCtrl);
    getCtrl('vnc_toggle_alt_button').addEventListener('click', UI.toggleAlt);
    getCtrl('vnc_toggle_windows_button').addEventListener('click', UI.toggleWindows);
    getCtrl('vnc_send_tab_button').addEventListener('click', UI.sendTab);
    getCtrl('vnc_send_esc_button').addEventListener('click', UI.sendEsc);
    getCtrl('vnc_send_ctrl_alt_del_button').addEventListener('click', UI.sendCtrlAltDel);
};

UI.addMachineHandlers = function () {
    getCtrl('vnc_power_button').addEventListener('click', UI.togglePowerPanel);
    getCtrl('vnc_shutdown_button').addEventListener('click', () => UI.hostPower('powerdown'));
    getCtrl('vnc_reboot_button').addEventListener('click', () => UI.hostPower('reboot'));
    getCtrl('vnc_logoff_button').addEventListener('click', () => UI.hostPower('logoff'));
    getCtrl('vnc_power_on_button').addEventListener('click', () => UI.veyonFeature('power_on'));
    getCtrl('vnc_ssh_lock_button').addEventListener('click', () => UI.veyonFeature('ssh_lock'));
    getCtrl('vnc_ssh_unlock_button').addEventListener('click', () => UI.veyonFeature('ssh_unlock'));
    getCtrl('vnc_ssh_lock_input_button').addEventListener('click', () => UI.veyonFeature('ssh_lock_input'));
    getCtrl('vnc_ssh_unlock_input_button').addEventListener('click', () => UI.veyonFeature('ssh_unlock_input'));
};

UI.addVeyonHandlers = function () {
    getCtrl('vnc_veyon_button').addEventListener('click', UI.toggleVeyonPanel);
    getCtrl('vnc_veyon_lock_button').addEventListener('click', () => UI.veyonFeature('lock'));
    getCtrl('vnc_veyon_unlock_button').addEventListener('click', () => UI.veyonFeature('unlock'));
    getCtrl('vnc_veyon_message_button').addEventListener('click', () => UI.veyonFeatureWithPrompt('message', [
        { key: 'text', label: 'Текст сообщения', required: true },
    ]));
    getCtrl('vnc_veyon_restart_button').addEventListener('click', () => UI.veyonFeature('restart_service'));
};

UI.addConnectionControlHandlers = function () {
    getCtrl('vnc_disconnect_button').addEventListener('click', UI.disconnect);
    getCtrl('vnc_cancel_reconnect_button').addEventListener('click', UI.cancelReconnect);
};

UI.addClipboardHandlers = function () {
    getCtrl('vnc_clipboard_button').addEventListener('click', UI.toggleClipboardPanel);
    getCtrl('vnc_clipboard_text').addEventListener('change', UI.clipboardSend);
};

UI.addSettingsHandlers = function () {
    getCtrl('vnc_settings_button').addEventListener('click', UI.toggleSettingsPanel);
    getCtrl('vnc_setting_shared').addEventListener('change', UI.updateShared);
    getCtrl('vnc_setting_view_only').addEventListener('change', UI.updateViewOnly);
    getCtrl('vnc_setting_view_clip').addEventListener('change', UI.updateViewClip);
    getCtrl('vnc_setting_resize').addEventListener('change', UI.applyResizeMode);
    getCtrl('vnc_setting_screen_region').addEventListener('change', UI.updateScreenRegion);
    getCtrl('vnc_setting_codec').addEventListener('change', UI.applyCodec);
    getCtrl('vnc_setting_quality').addEventListener('change', UI.updateQuality);
    getCtrl('vnc_setting_compression').addEventListener('change', UI.updateCompression);
};

UI.addFullscreenHandlers = function () {
    getCtrl('vnc_fullscreen_button').addEventListener('click', UI.toggleFullscreen);
    document.addEventListener('fullscreenchange', UI.updateFullscreenButton);
    document.addEventListener('webkitfullscreenchange', UI.updateFullscreenButton);
    document.addEventListener('mozfullscreenchange', UI.updateFullscreenButton);
};

UI.addStatusHandler = function () {
    getCtrl('vnc_status').addEventListener('click', UI.hideStatus);
};

UI.addVeyonBannerHandler = function () {
    const closeBtn = getCtrl('vncVeyonBannerClose');
    if (closeBtn) {
        closeBtn.addEventListener('click', function () {
            getCtrl('vncVeyonBanner').classList.add('hidden');
        });
    }
};

// ===== Custom tooltip =====

UI.initTooltip = function () {
    const tip = document.getElementById('vncTooltip');
    if (!tip) return;
    UI._tipEl = tip;
    UI._tipTimer = null;
    UI._tipTarget = null;

    // Делегирование: один listener на document, фильтруем по [data-tip]
    document.addEventListener('mouseover', function (e) {
        const el = e.target.closest('[data-tip]');
        if (!el) return;
        UI._tipTarget = el;
        UI.scheduleTip(el);
    });
    document.addEventListener('mouseout', function (e) {
        const el = e.target.closest('[data-tip]');
        if (!el) return;
        // Скрываем только если курсор ушёл за пределы элемента с data-tip
        // (relatedTarget вне его), иначе mouseout на дочерних элементах
        // будет постоянно скрывать подсказку.
        const related = e.relatedTarget;
        if (related && el.contains(related)) return;
        UI.hideTip();
    });
    document.addEventListener('focusin', function (e) {
        const el = e.target.closest('[data-tip]');
        if (!el) return;
        UI._tipTarget = el;
        UI.showTip(el);
    });
    document.addEventListener('focusout', function (e) {
        const el = e.target.closest('[data-tip]');
        if (!el) return;
        UI.hideTip();
    });
    // Скрытие при scroll/click/Esc — для отзывчивости
    window.addEventListener('scroll', UI.hideTip, true);
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') UI.hideTip();
    });
};

UI.scheduleTip = function (el) {
    UI.cancelTip();
    UI._tipTimer = setTimeout(function () { UI.showTip(el); }, 400);
};

UI.cancelTip = function () {
    if (UI._tipTimer) { clearTimeout(UI._tipTimer); UI._tipTimer = null; }
};

UI.showTip = function (el) {
    const tip = UI._tipEl;
    if (!tip) return;
    const text = el.dataset.tip;
    if (!text) { UI.hideTip(); return; }
    tip.textContent = text;
    tip.classList.remove('hidden');
    // Позиционирование по стороне control bar
    const pos = UI.getControlbarPos();
    const r = el.getBoundingClientRect();
    tip.style.left = ''; tip.style.right = ''; tip.style.top = ''; tip.style.bottom = '';
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    const vw = window.innerWidth, vh = window.innerHeight;
    if (pos === 'left') {
        let x = r.right + 6;
        if (x + tw > vw - 4) x = Math.max(4, r.left - tw - 6);
        tip.style.left = x + 'px';
        tip.style.top = Math.max(4, Math.min(vh - th - 4, r.top + r.height / 2 - th / 2)) + 'px';
    } else if (pos === 'right') {
        let x = r.left - tw - 6;
        if (x < 4) x = Math.min(vw - tw - 4, r.right + 6);
        tip.style.left = x + 'px';
        tip.style.top = Math.max(4, Math.min(vh - th - 4, r.top + r.height / 2 - th / 2)) + 'px';
    } else if (pos === 'top') {
        let y = r.bottom + 6;
        if (y + th > vh - 4) y = Math.max(4, r.top - th - 6);
        tip.style.top = y + 'px';
        tip.style.left = Math.max(4, Math.min(vw - tw - 4, r.left + r.width / 2 - tw / 2)) + 'px';
    } else { // bottom
        let y = r.top - th - 6;
        if (y < 4) y = Math.min(vh - th - 4, r.bottom + 6);
        tip.style.top = y + 'px';
        tip.style.left = Math.max(4, Math.min(vw - tw - 4, r.left + r.width / 2 - tw / 2)) + 'px';
    }
};

UI.hideTip = function () {
    UI.cancelTip();
    if (UI._tipEl) UI._tipEl.classList.add('hidden');
};

// ===== Main init =====

UI.start = async function () {
    const params = new URLSearchParams(location.search);
    UI.iid = params.get('iid') || '';
    UI.token = params.get('token') || '';
    UI.vt = params.get('vt') || '';
    UI._modeFailoverDone = false;
    const viewParam = params.get('view');
    UI.forcedView = (viewParam === '1' || viewParam === '0') ? (viewParam === '1') : null;
    const backendParam = (params.get('backend') || '').toLowerCase();
    UI.forcedBackend = ['vnc', 'krfb', 'veyon'].includes(backendParam) ? backendParam : null;
    UI.name = params.get('name') || UI.iid;
    document.title = UI.name;

    UI.initSettings();
    UI.updateFullscreenButton();

    // Restore control bar position
    const pos = localStorage.getItem('vncControlbarPos');
    if (['left', 'right', 'top', 'bottom'].includes(pos)) {
        UI.toggleControlbarSide(pos);
    } else {
        UI.toggleControlbarSide('left');
    }

    // Setup handlers
    UI.addControlbarHandlers();
    UI.addExtraKeysHandlers();
    UI.addMachineHandlers();
    UI.addVeyonHandlers();
    UI.addConnectionControlHandlers();
    UI.addClipboardHandlers();
    UI.addSettingsHandlers();
    UI.addFullscreenHandlers();
    UI.addStatusHandler();
    UI.addVeyonBannerHandler();
    UI.initTooltip();

    UI.openControlbar();
    UI.updateVisualState('init');
    document.documentElement.classList.remove('vnc_loading');

    if (!UI.iid || (!UI.token && !UI.vt)) {
        UI.showStatus('Не указано устройство или токен', 'error');
        return;
    }

    // Detect mode, then connect. Форсированный backend (?backend=) не
    // проверяет availability — строго выбранный сервер.
    if (!UI.forcedBackend) {
        await UI.fetchBackend();
    } else {
        // Пароли всё равно нужны (VNC Auth challenge от krfb/x11vnc)
        try {
            await UI.fetchBackend();
        } catch (e) { /* forced остаётся */ }
    }
    if (!UI.forcedBackend && UI.availabilityChecked && !UI.vncAvailable && !UI.veyonInstalled && !UI.krfbAvailable) {
        UI.showStatus('VNC и Veyon недоступны на хосте ' + UI.name, 'error');
        UI.toast('Не удалось подключиться: VNC и Veyon недоступны', 'error');
    } else {
        UI.connect();
    }
};

// beforeunload
window.addEventListener('beforeunload', function () {
    UI.manualClose = true;
    if (UI.rfb) { try { UI.rfb.disconnect(); } catch (e) {} }
});

// Boot
UI.start();
