"""Сессии правки наложений: один ввод или жест становится одной операцией отмены."""

from __future__ import annotations






class VideoOverlayMixin:
    """Часть VideoEditorDialog: состояние и виджеты принадлежат диалогу."""

    def _on_text_changed(self) -> None:
        if self._closing:
            return
        if self._text_before is None:
            self._text_before = self._take_snapshot()
        if any(
            block.iid not in {text.iid for text in self.project.text_overlays}
            for block in self.text_panel._blocks
        ):
            self._show_text_action.setChecked(True)
            self.inspector.tabs.setCurrentIndex(1)
        # печать идет посимвольно: превью обновить пачкой через 30 мс
        self._text_timer.start()
        self._text_commit_timer.start()
        self._update_history_controls()
        self._update_project_title()

    def _do_text_changed(self) -> None:
        if self._closing:
            return
        ovs = self.text_panel.get_all()
        try:
            self.edits.texts(ovs)
        except ValueError as error:
            self.notice.show_error(str(error))
            return
        self._refreshing_selection = True
        try:
            self.timeline.set_texts(list(self.project.text_overlays))
            self.timeline.select_clip(self._sel_clip or self._sel_text or "")
        finally:
            self._refreshing_selection = False
        self.refresh_preview()

    def _commit_text_edit(self) -> None:
        """Одна сессия ввода — одна команда, без пересоздания активного поля."""
        if getattr(self, "_overlay_gesture", False):
            return
        before, self._text_before = self._text_before, None
        if before is None:
            return
        self._text_timer.stop()
        self._text_commit_timer.stop()
        self._do_text_changed()
        self.push_undo(before, "Наложения", refresh=False)
        self._update_history_controls()

    def _start_overlay_gesture(self) -> None:
        self._commit_text_edit()
        self._overlay_gesture = True

    def _finish_overlay_gesture(self) -> None:
        self._overlay_gesture = False
        self._commit_text_edit()

    def _sync_overlay_locks(self) -> None:
        locked_clips = {
            c.iid for tr in self.project.tracks if tr.locked for c in tr.clips
        }
        for block in self.text_panel._blocks:
            block.setEnabled(block.get().clip_iid not in locked_clips)

    def _apply_preview_overlay(self) -> None:
        if self.preview.render_controller is None:
            self.preview.set_overlays(list(self.project.text_overlays))

    def _has_visible_text(self) -> bool:
        return any(o.enabled and o.text for o in self.project.text_overlays)
