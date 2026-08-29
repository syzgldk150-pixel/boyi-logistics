"""Credential, metadata, and browser-state persistence for session profiles."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import uuid

from agent.tms_runtime.session_support import *  # noqa: F403
from shared.runtime_events import publish_tms_session_alert


_LOGIN_RESULT_FILE = "operation_result.json"
_LOGIN_STATE_FILES = (
    "storage_state.json",
    "cookies.json",
    "pending_storage_state.json",
    "pending_login_state.json",
    "session_meta.json",
)
_LOGIN_STAGE_INPUT_FILES = (*_LOGIN_STATE_FILES, "login_profile.json")


class SessionPersistenceMixin:
    @staticmethod
    def _should_alert_session_disconnected(previous: dict[str, Any], payload: dict[str, Any]) -> bool:
        previous_status = str(previous.get("status") or "").strip()
        next_status = str(payload.get("status") or "").strip()
        return previous_status == "authenticated" and next_status in {"expired", "logged_out", "error"}

    def _start_status_alert(self, payload: dict[str, Any]) -> None:
        alert_payload = dict(payload)

        def runner() -> None:
            try:
                publish_tms_session_alert(alert_payload)
            except Exception:
                logger.warning("Failed to send TMS session status alert", exc_info=True)

        threading.Thread(target=runner, name="tms-session-alert", daemon=True).start()

    def _empty_credentials(self) -> dict[str, str]:
        return {
            "username": "",
            "password": "",
            "phone": "",
            "updated_at": "",
        }

    def _has_complete_credentials(self, payload: dict[str, Any]) -> bool:
        fields = ["username", "password"]
        if self._require_phone:
            fields.append("phone")
        return all(str(payload.get(field) or "").strip() for field in fields)

    def _load_saved_credentials_locked(self) -> dict[str, str]:
        raw = self._state_store.read_dict(self._login_profile_path)
        if raw is None:
            return self._empty_credentials()
        payload = self._empty_credentials()
        payload.update(
            {
                "username": str(raw.get("username") or "").strip(),
                "password": str(raw.get("password") or "").strip(),
                "phone": str(raw.get("phone") or "").strip(),
                "updated_at": str(raw.get("updated_at") or "").strip(),
            }
        )
        return payload

    def _load_env_credentials_locked(self) -> dict[str, str]:
        payload = self._empty_credentials()
        payload.update(
            {
                "username": _env_first(self._username_envs),
                "password": _env_first(self._password_envs),
                "phone": _env_first(self._phone_envs),
            }
        )
        return payload

    def _credentials_status_locked(self) -> dict[str, Any]:
        saved = self._load_saved_credentials_locked()
        env = self._load_env_credentials_locked()
        has_manual_credentials = self._has_complete_credentials(saved)
        has_env_credentials = self._has_complete_credentials(env)
        credential_source = "saved" if has_manual_credentials else "env" if has_env_credentials else ""
        return {
            **saved,
            "password": "",
            "has_saved_credentials": has_manual_credentials or has_env_credentials,
            "has_manual_credentials": has_manual_credentials,
            "has_env_credentials": has_env_credentials,
            "credential_source": credential_source,
        }

    def _manual_credentials_status_locked(self) -> dict[str, Any]:
        """Return only credentials explicitly saved through account management."""
        saved = self._load_saved_credentials_locked()
        has_manual_credentials = self._has_complete_credentials(saved)
        return {
            **saved,
            "password": "",
            "has_saved_credentials": has_manual_credentials,
            "has_manual_credentials": has_manual_credentials,
            "has_env_credentials": False,
            "credential_source": "saved" if has_manual_credentials else "",
        }

    def _save_credentials_locked(self, *, username: str, password: str, phone: str) -> dict[str, Any]:
        existing = self._load_saved_credentials_locked()
        incoming_password = str(password or "").strip()
        if incoming_password in {"", SAVED_PASSWORD_MASK} and existing.get("password"):
            incoming_password = str(existing.get("password") or "").strip()
        payload = {
            "username": str(username or "").strip(),
            "password": incoming_password,
            "phone": str(phone or "").strip(),
            "updated_at": _format_ts(_now_ts()),
        }
        missing = []
        if not payload["username"]:
            missing.append("账号")
        if not payload["password"]:
            missing.append("密码")
        if self._require_phone and not payload["phone"]:
            missing.append("手机号")
        if missing:
            raise TMSAuthStateError("AUTH_REQUIRED", f"{'、'.join(missing)}不能为空。")
        self._state_store.write_dict(self._login_profile_path, payload)
        self._bump_state_epoch_locked()
        return self._credentials_status_locked()

    def get_saved_credentials(self) -> dict[str, Any]:
        with self._lock:
            return self._credentials_status_locked()

    def get_manual_credentials(self) -> dict[str, Any]:
        with self._lock:
            return self._manual_credentials_status_locked()

    def save_credentials(self, *, username: str, password: str, phone: str) -> dict[str, Any]:
        with self._lock:
            return self._save_credentials_locked(username=username, password=password, phone=phone)

    def clear_saved_credentials(self) -> dict[str, Any]:
        with self._lock:
            try:
                self._state_store.remove(self._login_profile_path)
            except Exception:
                logger.exception("Failed to remove saved TMS credentials file: %s", self._login_profile_path)
            self._bump_state_epoch_locked()
            return self._credentials_status_locked()

    def resolve_login_config(self) -> LoginConfig:
        base_origin = (_env_first(self._base_origin_envs) or self._base_origin_default).rstrip("/")
        login_path = _env_first(self._login_path_envs) or self._login_path_default
        home_path = _env_first(self._home_path_envs) or self._home_path_default
        saved = self._load_saved_credentials_locked()
        username = (
            saved.get("username")
            or _env_first(self._username_envs)
            or ""
        ).strip()
        password = (
            saved.get("password")
            or _env_first(self._password_envs)
            or ""
        ).strip()
        phone = (
            saved.get("phone")
            or _env_first(self._phone_envs)
            or ""
        ).strip()
        return LoginConfig(
            base_origin=base_origin,
            login_url=_join_origin_path(base_origin, login_path),
            home_url=_join_origin_path(base_origin, home_path),
            username=username,
            password=password,
            phone=phone,
        )

    def _load_meta(self) -> dict[str, Any]:
        payload = self._state_store.read_dict(self._meta_path)
        if payload is None:
            if self._meta_path.exists():
                return {
                    "status": "error",
                    "label": _status_label("error"),
                    "last_validation_at": "",
                    "last_error_summary": "运行态元数据损坏",
                    "authenticated_at": "",
                    "pending_since": "",
                    "expires_at": "",
                }
            return {
                "status": "logged_out",
                "label": _status_label("logged_out"),
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "",
                "pending_since": "",
                "expires_at": "",
            }
        return payload

    def _read_meta_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._save_meta(self._load_meta())

    def _bump_state_epoch_locked(self) -> int:
        self._state_epoch = int(getattr(self, "_state_epoch", 0)) + 1
        return self._state_epoch

    def _save_meta(self, meta: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "status": str(meta.get("status") or "logged_out"),
            "label": _status_label(str(meta.get("status") or "logged_out")),
            "last_validation_at": str(meta.get("last_validation_at") or ""),
            "last_error_summary": str(meta.get("last_error_summary") or ""),
            "authenticated_at": str(meta.get("authenticated_at") or ""),
            "pending_since": str(meta.get("pending_since") or ""),
            "expires_at": str(meta.get("expires_at") or ""),
        }
        if payload["status"] == "pending_code":
            for key in (
                "captcha_image",
                "captcha_image_mime",
                "captcha_captured_at",
                "challenge_type",
                "challenge_label",
            ):
                if meta.get(key):
                    payload[key] = str(meta.get(key) or "")
        self._state_store.write_dict(self._meta_path, payload)
        self._health_snapshot_meta = dict(payload)
        try:
            import traceback as _tb
            stack = _tb.extract_stack(limit=8)[:-1]
            caller = " <- ".join(f"{frame.name}:{frame.lineno}" for frame in reversed(stack[-4:]))
            logger.info(
                "session_meta save: status=%s authenticated_at=%s last_validation_at=%s caller=%s",
                payload["status"],
                payload["authenticated_at"] or "-",
                payload["last_validation_at"] or "-",
                caller,
            )
        except Exception:
            pass
        return payload

    def _close_pending_locked(self) -> None:
        pending = self._pending
        self._pending = None
        if pending is None:
            return
        for target in (pending.context, pending.browser, pending.playwright):
            if target is None:
                continue
            try:
                close = getattr(target, "close", None) or getattr(target, "stop", None)
                if close:
                    close()
            except Exception:
                logger.exception("Failed to close pending TMS browser handle")

    def _login_worker_command(self, stage_dir: Path) -> list[str]:
        return [
            sys.executable,
            "-m",
            "agent.tms_runtime.session_login_worker",
            "--profile",
            self.profile_name,
            "--state-dir",
            str(stage_dir),
        ]

    def _snapshot_login_state_locked(self, stage_dir: Path) -> None:
        stage_dir.mkdir(parents=True, exist_ok=False)
        stage_dir.chmod(0o700)
        for name in _LOGIN_STAGE_INPUT_FILES:
            source = self._state_dir / name
            if not source.exists():
                continue
            target = stage_dir / name
            shutil.copyfile(source, target)
            target.chmod(0o600)

    @staticmethod
    def _terminate_process_tree(
        process: subprocess.Popen[Any],
        *,
        process_group_id: int | None = None,
    ) -> None:
        if os.name == "posix":
            # The worker is started in a fresh session. Keep the pgid captured
            # at launch because the leader may exit while Playwright children
            # remain alive, making a later ``getpgid(pid)`` impossible.
            pgid = int(process_group_id or process.pid)
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            # Always address the original process group after the grace
            # period. Reaping the leader does not prove its browser children
            # have exited.
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:  # pragma: no cover - ECS production runs on Linux
            if process.poll() is not None:
                return
            process.terminate()
            try:
                process.wait(timeout=2)
                return
            except subprocess.TimeoutExpired:
                process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def _commit_staged_login_state_locked(self, stage_dir: Path) -> None:
        # Session metadata is the visibility marker and must move last.
        for name in _LOGIN_STATE_FILES:
            source = stage_dir / name
            target = self._state_dir / name
            if source.exists():
                source.chmod(0o600)
                os.replace(source, target)
            else:
                target.unlink(missing_ok=True)
        self._health_snapshot_meta = self._load_meta()

    def _discard_login_stage(self, stage_dir: Path) -> None:
        try:
            resolved_stage = stage_dir.resolve()
            resolved_root = (self._state_dir / ".login_ops").resolve()
            if resolved_stage.parent != resolved_root:
                raise RuntimeError("login staging path escaped its profile directory")
            shutil.rmtree(resolved_stage, ignore_errors=True)
            try:
                resolved_root.rmdir()
            except OSError:
                pass
        except FileNotFoundError:
            return

    def _run_staged_login_operation(self, action: str, code: str) -> dict[str, Any]:
        if action not in {"send", "submit"}:
            raise TMSAuthStateError("AUTH_UNAVAILABLE", "不支持的登录操作。")
        if not self._login_operation_lock.acquire(blocking=False):
            raise TMSAuthStateError(
                "BLOCKED_LOGIN",
                "该账号已有登录操作正在执行；本次请求未排队。",
            )

        token = uuid.uuid4().hex
        stage_dir = self._state_dir / ".login_ops" / token
        epoch = -1
        process: subprocess.Popen[Any] | None = None
        process_group_id: int | None = None
        try:
            with self._lock:
                epoch = self._bump_state_epoch_locked()
                self._active_login_token = token
                self._snapshot_login_state_locked(stage_dir)

            popen_kwargs: dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "text": True,
            }
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            process = subprocess.Popen(self._login_worker_command(stage_dir), **popen_kwargs)
            if os.name == "posix":
                try:
                    process_group_id = os.getpgid(process.pid)
                except ProcessLookupError:
                    # ``start_new_session=True`` makes the worker pid its pgid;
                    # retain that identity even if the leader exited quickly.
                    process_group_id = process.pid
            request_payload = json.dumps({"action": action, "code": code}, ensure_ascii=False)
            try:
                process.communicate(
                    input=request_payload,
                    timeout=self._browser_action_timeout_sec,
                )
            except subprocess.TimeoutExpired as exc:
                self._terminate_process_tree(
                    process,
                    process_group_id=process_group_id,
                )
                raise TMSAuthStateError(
                    "LOGIN_TIMEOUT",
                    f"登录操作超过 {self._browser_action_timeout_sec:g} 秒，已终止浏览器进程。",
                ) from exc

            envelope = self._state_store.read_dict(stage_dir / _LOGIN_RESULT_FILE)
            if process.returncode != 0 or envelope is None:
                raise TMSAuthStateError("AUTH_UNAVAILABLE", "登录工作进程未返回有效结果。")

            commit_staged_state = bool(envelope.get("commit_staged_state"))
            with self._lock:
                if self._active_login_token != token or self._state_epoch != epoch:
                    raise TMSAuthStateError(
                        "BLOCKED_LOGIN",
                        "登录期间账号状态已变化，本次旧结果未提交。",
                    )
                if commit_staged_state:
                    self._commit_staged_login_state_locked(stage_dir)

            if envelope.get("ok") is not True:
                raise TMSAuthStateError(
                    str(envelope.get("error_code") or "AUTH_REQUIRED"),
                    str(envelope.get("error") or "登录操作失败。"),
                )
            result = envelope.get("result")
            if not isinstance(result, dict):
                raise TMSAuthStateError("AUTH_UNAVAILABLE", "登录工作进程返回格式异常。")
            return result
        finally:
            if process is not None and process.poll() is None:
                self._terminate_process_tree(
                    process,
                    process_group_id=process_group_id,
                )
            with self._lock:
                if self._active_login_token == token:
                    self._active_login_token = None
            self._discard_login_stage(stage_dir)
            self._login_operation_lock.release()

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self._bump_state_epoch_locked()
            self._close_pending_locked()
            for path in (
                self._storage_state_path,
                self._cookies_path,
                self._pending_storage_state_path,
                self._pending_login_state_path,
            ):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    logger.exception("Failed to remove TMS session state file: %s", path)
            return self._save_meta(
                {
                    "status": "logged_out",
                    "last_validation_at": _format_ts(_now_ts()),
                    "last_error_summary": "",
                    "authenticated_at": "",
                    "pending_since": "",
                    "expires_at": "",
                }
            )

    def _read_login_error(self, page: Any) -> str:
        try:
            return str(
                page.evaluate(
                    """
                    () => {
                      const box = document.querySelector('#showError');
                      const text = (document.querySelector('#errorSpan') || {}).innerText || '';
                      if (!text) return '';
                      if (!box) return text.trim();
                      const style = window.getComputedStyle(box);
                      const visible = style.display !== 'none' && style.visibility !== 'hidden';
                      return visible ? text.trim() : '';
                    }
                    """
                )
                or ""
            ).strip()
        except Exception:
            return ""

    def _load_storage_state(self) -> dict[str, Any]:
        if not self._storage_state_path.exists():
            return {"cookies": [], "origins": []}
        try:
            return json.loads(self._storage_state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TMSAuthStateError("AUTH_REQUIRED", f"共享登录态文件损坏: {exc}") from exc

    def _normalize_ronghui_user_context_state_locked(self) -> tuple[str, str, bool]:
        storage_state = self._load_storage_state()
        config = self.resolve_login_config()
        host = urlparse(config.base_origin).hostname or "tms.ronghuiwl.com"
        changed, context_status = normalize_ronghui_user_info_storage_state(storage_state, host=host)
        if changed:
            self._state_store.write_dict(self._storage_state_path, storage_state)
            cookies = storage_state.get("cookies")
            if isinstance(cookies, list):
                self._cookies_path.write_text(
                    json.dumps(cookies, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        error_by_status = {
            "missing": "融辉登录态缺少页面用户上下文，请重新登录。",
            "incomplete": "融辉登录态的页面用户上下文不完整，请重新登录。",
            "conflicting": "融辉登录态的页面用户上下文不一致，请重新登录。",
        }
        return context_status, error_by_status.get(context_status, ""), changed

    def _invalidate_ronghui_context_locked(self, error_text: str) -> None:
        for path in (self._pending_storage_state_path, self._pending_login_state_path):
            path.unlink(missing_ok=True)
        self._save_meta(
            {
                "status": "expired",
                "last_validation_at": _format_ts(_now_ts()),
                "last_error_summary": error_text,
                "authenticated_at": "",
                "pending_since": "",
                "expires_at": "",
            }
        )

    def _persist_storage_state_locked(self, context: Any, page: Any) -> dict[str, Any]:
        storage_state = context.storage_state(path=str(self._storage_state_path))
        if not isinstance(storage_state, dict):
            storage_state = self._load_storage_state()
        cookies = storage_state.get("cookies", [])
        self._cookies_path.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if not self._is_yunda_mode():
            context_status, context_error, _context_changed = self._normalize_ronghui_user_context_state_locked()
            if context_status != "ready":
                self._invalidate_ronghui_context_locked(context_error)
                raise TMSAuthStateError("AUTH_REQUIRED", context_error)
        expires_at = ""
        cookie_expiries = [
            float(cookie.get("expires"))
            for cookie in cookies
            if isinstance(cookie, dict) and cookie.get("expires") not in (None, "", -1)
        ]
        if cookie_expiries:
            expires_at = _format_ts(min(cookie_expiries))
        for path in (self._pending_storage_state_path, self._pending_login_state_path):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to remove pending Yunda login state file: %s", path)
        self._save_meta(
            {
                "status": "authenticated",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": _format_ts(_now_ts()),
                "pending_since": "",
                "expires_at": expires_at,
            }
        )
        meta = self._load_meta()
        logger.info("%s shared session persisted", "Yunda" if self._is_yunda_mode() else "TMS")
        return meta

    def _ensure_login_prerequisites(self, config: LoginConfig, *, require_phone: bool | None = None) -> None:
        missing = []
        if not config.username:
            missing.append("/".join(self._username_envs))
        if not config.password:
            missing.append("/".join(self._password_envs))
        phone_required = self._require_phone if require_phone is None else bool(require_phone)
        if phone_required and not config.phone:
            missing.append("/".join(self._phone_envs))
        if missing:
            raise TMSAuthStateError("AUTH_REQUIRED", f"缺少登录配置: {', '.join(missing)}")
