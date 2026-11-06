"""Independent installer using the operation context supplied by the worker."""

import threading
from src.workers.setup.types import RESULT_SUCCESS, RESULT_ERROR, RESULT_IGNORE
from src.workers.setup.phases import PhasedInstaller, PhaseResult


class SshInstaller(PhasedInstaller):
    service = "ssh"
    recovery_limit = (
        "Добавленный SSH-ключ автоматически не удаляется; прежние ключи сохранены."
    )

    def prepare(self, request):
        result = super().prepare(request)
        if result.code != RESULT_SUCCESS:
            return result
        credentials = self._resolve_credentials(request.device)
        key = credentials.private_key
        public = self.parameters.ssh_public_key.strip()
        if (
            key is None
            or len(public.splitlines()) != 1
            or public.split()[:2] != [key.get_name(), key.get_base64()]
        ):
            return PhaseResult(
                RESULT_ERROR,
                "Выбранная SSH-пара недоступна, зашифрована или не совпадает",
            )
        self._verification_credentials = credentials
        return result

    def _apply_install(self, device, ssh, run, write, append):
        self._install_ssh(device, ssh, append, run)

    def _install_ssh(self, device, ssh, sftp_append, run_cmd) -> None:
        svc = "ssh"
        if not self.parameters.ssh_public_key:
            self.context.log(device.host, svc, "SSH: публичный ключ не подготовлен")
            self._report(
                device, svc, RESULT_ERROR, "Публичный SSH-ключ не передан воркеру"
            )
            return

        self.context.log(device.host, svc, "Начало установки SSH-ключа")
        pubkey = self.parameters.ssh_public_key.strip()
        if not pubkey:
            self.context.log(device.host, svc, "SSH: пустой ключ")
            self._report(device, svc, RESULT_ERROR, "Пустой публичный ключ")
            return

        already, err = sftp_append(".ssh/authorized_keys", pubkey)
        if err:
            self.context.log(device.host, svc, f"SSH: ошибка — {err}")
            self._report(device, svc, RESULT_ERROR, err)
            return
        self._report(
            device,
            svc,
            RESULT_IGNORE if already else RESULT_SUCCESS,
            "Ключ уже присутствует в authorized_keys"
            if already
            else "Ключ добавлен в authorized_keys",
        )

    def verify(self, request, applied):
        device, ssh = request.device, request.ssh
        svc = self.service
        self._results = []
        try:
            self._verify_login(device, ssh)
            if self._results:
                return PhaseResult(*self._results[-1])
            return applied
        finally:
            self._results = None

    def _verify_login(self, device, ssh):
        svc = self.service
        # Verify exactly the selected key; agent/password/default keys cannot
        # make this check pass accidentally.
        import paramiko

        credentials = getattr(
            self, "_verification_credentials", None
        ) or self._resolve_credentials(device)
        if credentials.private_key is None:
            self._report(
                device,
                svc,
                RESULT_ERROR,
                "Приватный SSH-ключ недоступен для проверки входа",
            )
            return
        verification = paramiko.SSHClient()
        timer = threading.Timer(8, verification.close)
        timer.daemon = True
        try:
            host_key = ssh.get_transport().get_remote_server_key()
            port = device.port or self.parameters.ssh_port
            address = device.host if port == 22 else f"[{device.host}]:{port}"
            verification.get_host_keys().add(address, host_key.get_name(), host_key)
            self.context.register_connection(verification)
            timer.start()
            verification.connect(
                device.host,
                port=port,
                username=credentials.username,
                pkey=credentials.private_key,
                password=None,
                allow_agent=False,
                look_for_keys=False,
                timeout=5,
                banner_timeout=5,
                auth_timeout=5,
            )
        except Exception:
            self._report(
                device, svc, RESULT_ERROR, "Вход выбранным SSH-ключом не подтверждён"
            )
            return
        finally:
            timer.cancel()
            verification.close()
            self.context.unregister_connection(verification)
