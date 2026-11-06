"""Панель громкости: мастер, клип, Mute и громкость предпросмотра."""

from __future__ import annotations






class VideoAudioMixin:
    """Часть VideoEditorDialog: состояние и виджеты принадлежат диалогу."""

    def _set_audio_limiter(self, enabled: bool) -> None:
        self._finish_edit_sessions()
        before = self._take_snapshot()
        self.edits.master_audio(limiter=enabled)
        self.push_undo(before, "Ограничитель пиков звука")

    def _sync_mute_icon(self, muted: bool) -> None:
        from src.ui.icons import set_themed_icon

        set_themed_icon(self.btn_mute, "ve_mute" if muted else "ve_volume")

    def _sync_audio_controls(self) -> None:
        """Синхронизировать мастер и доступность регулировок выбранного клипа."""
        from src.ui.icons import set_themed_icon

        self.btn_master_mute.blockSignals(True)
        self.btn_master_mute.setChecked(self.project.master_mute)
        self.btn_master_mute.blockSignals(False)
        set_themed_icon(
            self.btn_master_mute, "ve_mute" if self.project.master_mute else "ve_volume"
        )
        found = self._selected_clip_obj()
        editable = found is not None and not any(
            track.locked
            and any(
                clip.iid == found[0].iid
                or (found[0].link_iid and clip.link_iid == found[0].link_iid)
                for clip in track.clips
            )
            for track in self.project.tracks
        )
        self.vol_slider.setEnabled(editable)
        self.btn_mute.setEnabled(editable)
        if found:
            c, _tr = found
            self.vol_slider.blockSignals(True)
            self.vol_slider.setValue(round(c.volume * 100))
            self.vol_slider.blockSignals(False)
            self.vol_value.setText(f"{round(c.volume * 100)}%")
            self.btn_mute.blockSignals(True)
            self.btn_mute.setChecked(c.mute)
            self.btn_mute.blockSignals(False)
            self._sync_mute_icon(c.mute)
        else:
            self.vol_value.setText("-")

    def _apply_preview_audio(self) -> None:
        if self.preview.render_controller is not None:
            self.refresh_preview()
            return
        found = self._selected_clip_obj()
        if found:
            c, _tr = found
            self.preview.set_audio(
                c.volume, c.mute, self.project.effective_master_volume
            )
        else:
            self.preview.set_audio(1.0, False, self.project.effective_master_volume)

    def _audio_at(self, t: float) -> tuple[float, bool]:
        """Эффективная громкость в точке t: видеоклип под плейхедом.

        Звук в превью несут видеосегменты (refresh_preview строит их
        только по V-дорожкам), поэтому смотрим V-клип. Пара после
        _sync_mate_audio хранит одно значение - какой именно клип пары
        попался, неважно. Вне клипов - выделенный, затем полная.
        """
        for tr in self.project.video_tracks():
            for c in tr.clips:
                if c.timeline_start <= t < c.timeline_end:
                    return (float(c.volume), bool(c.mute))
        found = self._selected_clip_obj()
        if found:
            c, _tr = found
            return (float(c.volume), bool(c.mute))
        return (1.0, False)

    def _apply_audio_at_playhead(self) -> None:
        """Громкость превью за плейхедом, а не за выделением.

        Тики воспроизведения и seek пересчитывают звук: заглушенный
        участок молчит, даже если выделен другой клип. set_audio дергаем
        только при смене значения - positionChanged идет несколько раз
        в секунду.
        """
        if self.preview.render_controller is not None:
            return
        vol, muted = self._audio_at(self._playhead)
        master = float(self.project.effective_master_volume)
        key = (round(vol, 3), muted, round(master, 3))
        if key == self._last_preview_audio:
            return
        self._last_preview_audio = key
        self.preview.set_audio(vol, muted, master)

    def _on_vol_slider(self, v: int) -> None:
        found = self._selected_clip_obj()
        if not found:
            return
        before = self._take_snapshot() if not self.vol_slider.isSliderDown() else None
        try:
            self.edits.clip_audio(found[0].iid, volume=max(0.0, min(2.0, v / 100)))
        except ValueError as error:
            self.notice.show_error(str(error))
            self._sync_audio_controls()
            return
        if before is not None:
            self.push_undo(before, "Громкость")
        self.vol_value.setText(f"{v}%")
        self._apply_preview_audio()
        self._update_project_title()

    def _on_vol_press(self) -> None:
        self._vol_before = self._take_snapshot()

    def _on_vol_release(self) -> None:
        before, self._vol_before = self._vol_before, None
        if before is not None:
            self.push_undo(before, "Громкость", refresh=False)

    def _on_mute_toggled(self, on: bool) -> None:
        found = self._selected_clip_obj()
        if not found:
            return
        before = self._take_snapshot()
        try:
            self.edits.clip_audio(found[0].iid, mute=on)
        except ValueError as error:
            self.notice.show_error(str(error))
            self._sync_audio_controls()
            return
        self._sync_mute_icon(on)
        self.push_undo(before, "Mute")
        self._apply_preview_audio()

    def _on_master_mute_toggled(self, on: bool) -> None:
        before = self._take_snapshot()
        self.edits.master_audio(mute=on)
        self._sync_audio_controls()
        self.push_undo(before, "Mute мастера")
        self._apply_preview_audio()

    def _on_master_slider(self, v: int) -> None:
        before = (
            self._take_snapshot() if not self.master_slider.isSliderDown() else None
        )
        self.edits.master_audio(volume=max(0.0, min(2.0, v / 100)))
        self.master_value.setText(f"{v}%")
        self._apply_preview_audio()
        self._update_project_title()
        if before is not None:
            self.push_undo(before, "Мастер-громкость", refresh=False)

    def _on_master_press(self) -> None:
        self._master_before = self._take_snapshot()

    def _on_master_release(self) -> None:
        before, self._master_before = self._master_before, None
        if before is not None:
            self.push_undo(before, "Мастер-громкость", refresh=False)
