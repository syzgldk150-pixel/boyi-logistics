"""Versioned, encrypted global LLM configuration.

Only fixed official provider endpoints are accepted.  API keys are encrypted
with an environment-managed AES-256-GCM master key and are never returned by
public status or audit methods.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

import pymysql
from Crypto.Cipher import AES
from openai import AsyncOpenAI

from shared.redaction import redact_text


PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "env_key": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "timeout": 30,
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env_key": "GLM_API_KEY",
        "default_model": "glm-4-flash",
        "timeout": 30,
    },
}

MASTER_KEY_ENV = "AGENT_LLM_CONFIG_MASTER_KEY"
MASTER_KEY_VERSION_ENV = "AGENT_LLM_CONFIG_KEY_VERSION"


class LLMSettingsError(RuntimeError):
    """Safe configuration error suitable for an administrator response."""


@dataclass(frozen=True)
class RuntimeLLMConfig:
    provider: str
    model_id: str
    api_key: str
    source: str
    config_version_id: int | None = None


def _now() -> dt.datetime:
    return dt.datetime.now().replace(tzinfo=None)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _safe_error(exc: BaseException) -> str:
    return redact_text(str(exc) or type(exc).__name__)[:500]


def _master_key() -> bytes:
    raw = str(os.getenv(MASTER_KEY_ENV) or "").strip()
    if not raw:
        raise LLMSettingsError(
            f"{MASTER_KEY_ENV} is missing; API keys cannot be saved or decrypted"
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise LLMSettingsError(f"{MASTER_KEY_ENV} must be valid base64") from exc
    if len(key) != 32:
        raise LLMSettingsError(f"{MASTER_KEY_ENV} must decode to exactly 32 bytes")
    return key


def encryption_available() -> bool:
    try:
        _master_key()
        return True
    except LLMSettingsError:
        return False


def _encrypt_api_key(api_key: str) -> tuple[bytes, bytes, bytes, str, str]:
    value = str(api_key or "").strip()
    if not value:
        raise LLMSettingsError("API key is required")
    cipher = AES.new(_master_key(), AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(value.encode("utf-8"))
    key_version = str(os.getenv(MASTER_KEY_VERSION_ENV) or "v1").strip() or "v1"
    hint = f"{value[:3]}…{value[-3:]}" if len(value) >= 8 else "configured"
    return ciphertext, cipher.nonce, tag, key_version, hint


def _decrypt_api_key(ciphertext: bytes, nonce: bytes, tag: bytes) -> str:
    try:
        cipher = AES.new(_master_key(), AES.MODE_GCM, nonce=bytes(nonce))
        plaintext = cipher.decrypt_and_verify(bytes(ciphertext), bytes(tag))
        value = plaintext.decode("utf-8").strip()
    except LLMSettingsError:
        raise
    except Exception as exc:
        raise LLMSettingsError("stored API key cannot be decrypted or authenticated") from exc
    if not value:
        raise LLMSettingsError("stored API key decrypted to an empty value")
    return value


class LLMSettingsRepository:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connection_factory = connection_factory

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        connection = self._connection_factory()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _cursor(connection: Any) -> Any:
        try:
            return connection.cursor(pymysql.cursors.DictCursor)
        except TypeError:
            return connection.cursor()

    @staticmethod
    def _one(cursor: Any) -> dict[str, Any] | None:
        row = cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, Mapping):
            return dict(row)
        names = [str(item[0]) for item in (cursor.description or ())]
        return dict(zip(names, row))

    @classmethod
    def _all(cls, cursor: Any) -> list[dict[str, Any]]:
        rows = cursor.fetchall() or []
        return [dict(row) if isinstance(row, Mapping) else dict(zip(
            [str(item[0]) for item in (cursor.description or ())], row
        )) for row in rows]

    @staticmethod
    def _provider(provider: str) -> str:
        value = str(provider or "").strip().lower()
        if value not in PROVIDERS:
            raise LLMSettingsError("provider must be deepseek or glm")
        return value

    @staticmethod
    def _model(model_id: str) -> str:
        value = str(model_id or "").strip()
        if not value or len(value) > 191:
            raise LLMSettingsError("model_id is required and must be at most 191 characters")
        return value

    def _audit(
        self,
        cursor: Any,
        *,
        action: str,
        actor: str,
        provider: str | None = None,
        config_id: int | None = None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        api_key_changed: bool = False,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO llm_config_audit_logs (
                action, provider, config_version_id, before_json, after_json,
                api_key_changed, changed_by, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                action,
                provider,
                config_id,
                _json(dict(before)) if before else None,
                _json(dict(after)) if after else None,
                1 if api_key_changed else 0,
                actor,
                _now(),
            ),
        )

    def public_status(self) -> dict[str, Any]:
        with self._connection() as connection:
            cursor = self._cursor(connection)
            try:
                cursor.execute(
                    """
                    SELECT c.provider, c.id, c.credential_version, c.key_hint,
                           c.created_at, c.revoked_at
                    FROM llm_provider_credentials c
                    INNER JOIN (
                        SELECT provider, MAX(id) AS latest_id
                        FROM llm_provider_credentials
                        WHERE revoked_at IS NULL GROUP BY provider
                    ) latest ON latest.latest_id = c.id
                    """
                )
                credential_rows = self._all(cursor)
                cursor.execute(
                    """
                    SELECT id, provider, model_id, status, test_result_json,
                           test_error_code, test_error_message, tested_at,
                           activated_at, deactivated_at, created_by, created_at
                    FROM llm_config_versions ORDER BY id DESC LIMIT 100
                    """
                )
                versions = self._all(cursor)
                cursor.execute(
                    "SELECT COUNT(*) AS total FROM llm_config_versions WHERE activated_at IS NOT NULL"
                )
                ever_activated = int((self._one(cursor) or {}).get("total") or 0) > 0
                cursor.execute(
                    """
                    SELECT provider, model_id, source, discovered_at, last_seen_at
                    FROM llm_model_catalog ORDER BY provider, model_id
                    """
                )
                models = self._all(cursor)
            finally:
                cursor.close()
        credentials = {
            str(row["provider"]): {
                "configured": True,
                "key_hint": str(row.get("key_hint") or "configured"),
                "credential_version": int(row["credential_version"]),
                "updated_at": str(row.get("created_at") or ""),
            }
            for row in credential_rows
        }
        safe_versions = []
        for row in versions:
            result = row.get("test_result_json")
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except json.JSONDecodeError:
                    result = None
            safe_versions.append(
                {
                    "id": int(row["id"]),
                    "provider": str(row["provider"]),
                    "model_id": str(row["model_id"]),
                    "status": str(row["status"]),
                    "test_result": result,
                    "test_error_code": row.get("test_error_code"),
                    "test_error_message": row.get("test_error_message"),
                    "tested_at": str(row.get("tested_at") or ""),
                    "activated_at": str(row.get("activated_at") or ""),
                    "deactivated_at": str(row.get("deactivated_at") or ""),
                    "created_by": str(row.get("created_by") or ""),
                    "created_at": str(row.get("created_at") or ""),
                }
            )
        active = next((item for item in safe_versions if item["status"] == "active"), None)
        return {
            "providers": [
                {
                    "provider": name,
                    "base_url": cfg["base_url"],
                    **credentials.get(name, {"configured": False, "key_hint": ""}),
                }
                for name, cfg in PROVIDERS.items()
            ],
            "active": active,
            "versions": safe_versions,
            "models": [
                {
                    "provider": str(row["provider"]),
                    "model_id": str(row["model_id"]),
                    "source": str(row["source"]),
                    "discovered_at": str(row.get("discovered_at") or ""),
                    "last_seen_at": str(row.get("last_seen_at") or ""),
                }
                for row in models
            ],
            "encryption_available": encryption_available(),
            "environment_managed": not ever_activated,
        }

    def save_candidate(
        self,
        *,
        provider: str,
        model_id: str,
        api_key: str | None,
        actor: str,
    ) -> int:
        provider_value = self._provider(provider)
        model_value = self._model(model_id)
        actor_value = str(actor or "").strip()
        if not actor_value:
            raise LLMSettingsError("actor is required")
        submitted_key = str(api_key or "").strip()
        with self._connection() as connection:
            cursor = self._cursor(connection)
            try:
                cursor.execute(
                    """
                    SELECT id, key_hint FROM llm_provider_credentials
                    WHERE provider = %s AND revoked_at IS NULL
                    ORDER BY id DESC LIMIT 1
                    """,
                    (provider_value,),
                )
                existing_credential = self._one(cursor)
                mask_only = bool(submitted_key) and all(
                    character in "*•●·xX" for character in submitted_key
                )
                preserve_key = (
                    not submitted_key
                    or mask_only
                    or submitted_key == "configured"
                    or bool(
                        existing_credential
                        and submitted_key == str(existing_credential.get("key_hint") or "")
                    )
                )
                key_changed = not preserve_key
                credential_id = 0
                if key_changed:
                    ciphertext, nonce, tag, key_version, hint = _encrypt_api_key(submitted_key)
                    cursor.execute(
                        "SELECT COALESCE(MAX(credential_version), 0) AS version FROM llm_provider_credentials WHERE provider = %s FOR UPDATE",
                        (provider_value,),
                    )
                    version = int((self._one(cursor) or {}).get("version") or 0) + 1
                    cursor.execute(
                        """
                        INSERT INTO llm_provider_credentials (
                            provider, credential_version, encrypted_api_key, nonce,
                            auth_tag, key_version, key_hint, created_by, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (provider_value, version, ciphertext, nonce, tag, key_version, hint, actor_value, _now()),
                    )
                    credential_id = int(cursor.lastrowid)
                else:
                    if not existing_credential:
                        raise LLMSettingsError("a complete API key is required for this provider")
                    credential_id = int(existing_credential["id"])
                cursor.execute(
                    """
                    INSERT INTO llm_config_versions (
                        provider, model_id, credential_id, status, created_by, created_at
                    ) VALUES (%s, %s, %s, 'draft', %s, %s)
                    """,
                    (provider_value, model_value, credential_id, actor_value, _now()),
                )
                config_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    INSERT INTO llm_model_catalog (
                        provider, model_id, source, discovered_at, last_seen_at
                    ) VALUES (%s, %s, 'manual', %s, %s)
                    ON DUPLICATE KEY UPDATE last_seen_at = VALUES(last_seen_at)
                    """,
                    (provider_value, model_value, _now(), _now()),
                )
                self._audit(
                    cursor,
                    action="save_candidate",
                    actor=actor_value,
                    provider=provider_value,
                    config_id=config_id,
                    after={"provider": provider_value, "model_id": model_value, "status": "draft"},
                    api_key_changed=key_changed,
                )
            finally:
                cursor.close()
        return config_id

    def _config_with_secret(self, cursor: Any, config_id: int) -> RuntimeLLMConfig:
        cursor.execute(
            """
            SELECT v.id, v.provider, v.model_id, c.encrypted_api_key, c.nonce,
                   c.auth_tag, c.revoked_at
            FROM llm_config_versions v
            INNER JOIN llm_provider_credentials c ON c.id = v.credential_id
            WHERE v.id = %s
            """,
            (int(config_id),),
        )
        row = self._one(cursor)
        if not row:
            raise LLMSettingsError("configuration version does not exist")
        if row.get("revoked_at") is not None:
            raise LLMSettingsError("the active provider credential has been cleared")
        return RuntimeLLMConfig(
            provider=str(row["provider"]),
            model_id=str(row["model_id"]),
            api_key=_decrypt_api_key(row["encrypted_api_key"], row["nonce"], row["auth_tag"]),
            source="database",
            config_version_id=int(row["id"]),
        )

    def candidate_config(self, config_id: int) -> RuntimeLLMConfig:
        with self._connection() as connection:
            cursor = self._cursor(connection)
            try:
                return self._config_with_secret(cursor, config_id)
            finally:
                cursor.close()

    def active_config(self) -> RuntimeLLMConfig | None:
        with self._connection() as connection:
            cursor = self._cursor(connection)
            try:
                cursor.execute("SELECT id FROM llm_config_versions WHERE status = 'active' ORDER BY activated_at DESC, id DESC LIMIT 1")
                row = self._one(cursor)
                return self._config_with_secret(cursor, int(row["id"])) if row else None
            finally:
                cursor.close()

    def runtime_descriptor(self) -> tuple[dict[str, Any] | None, bool]:
        """Return non-secret runtime identity and whether DB activation ever occurred."""

        with self._connection() as connection:
            cursor = self._cursor(connection)
            try:
                cursor.execute(
                    """
                    SELECT v.id, v.provider, v.model_id, c.revoked_at
                    FROM llm_config_versions v
                    INNER JOIN llm_provider_credentials c ON c.id = v.credential_id
                    WHERE v.status = 'active'
                    ORDER BY v.activated_at DESC, v.id DESC LIMIT 1
                    """
                )
                active = self._one(cursor)
                cursor.execute(
                    "SELECT COUNT(*) AS total FROM llm_config_versions WHERE activated_at IS NOT NULL"
                )
                ever_activated = int((self._one(cursor) or {}).get("total") or 0) > 0
            finally:
                cursor.close()
        descriptor = None if not active else {
            "provider": str(active["provider"]),
            "model_id": str(active["model_id"]),
            "source": "database",
            "config_version_id": int(active["id"]),
            "credential_available": active.get("revoked_at") is None,
        }
        return descriptor, ever_activated

    def store_models(self, provider: str, model_ids: list[str]) -> None:
        provider_value = self._provider(provider)
        now = _now()
        with self._connection() as connection:
            cursor = self._cursor(connection)
            try:
                for model_id in sorted(set(model_ids)):
                    model_value = self._model(model_id)
                    cursor.execute(
                        """
                        INSERT INTO llm_model_catalog (
                            provider, model_id, source, discovered_at, last_seen_at
                        ) VALUES (%s, %s, 'api', %s, %s)
                        ON DUPLICATE KEY UPDATE source = 'api', last_seen_at = VALUES(last_seen_at)
                        """,
                        (provider_value, model_value, now, now),
                    )
            finally:
                cursor.close()

    def record_test(self, config_id: int, result: Mapping[str, Any], error: str = "") -> None:
        passed = bool(result.get("passed")) and not error
        with self._connection() as connection:
            cursor = self._cursor(connection)
            try:
                cursor.execute(
                    """
                    UPDATE llm_config_versions
                    SET status = %s, test_result_json = %s,
                        test_error_code = %s, test_error_message = %s, tested_at = %s
                    WHERE id = %s AND status IN ('draft', 'tested')
                    """,
                    (
                        "tested" if passed else "draft",
                        _json(dict(result)),
                        None if passed else "LLM_COMPATIBILITY_TEST_FAILED",
                        None if passed else str(error or "one or more compatibility tests failed")[:500],
                        _now(),
                        int(config_id),
                    ),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise LLMSettingsError("only draft or tested configurations can be tested")
            finally:
                cursor.close()

    def activate(self, config_id: int, *, actor: str) -> None:
        with self._connection() as connection:
            cursor = self._cursor(connection)
            try:
                cursor.execute("SELECT * FROM llm_config_versions WHERE id = %s FOR UPDATE", (int(config_id),))
                target = self._one(cursor)
                if not target:
                    raise LLMSettingsError("configuration version does not exist")
                if str(target["status"]) != "tested":
                    raise LLMSettingsError("configuration must pass all tests before activation")
                result = target.get("test_result_json")
                if isinstance(result, str):
                    result = json.loads(result)
                if not isinstance(result, Mapping) or not bool(result.get("passed")):
                    raise LLMSettingsError("configuration test result is not eligible for activation")
                self._config_with_secret(cursor, int(config_id))
                cursor.execute("UPDATE llm_config_versions SET status = 'inactive', deactivated_at = %s WHERE status = 'active'", (_now(),))
                cursor.execute("UPDATE llm_config_versions SET status = 'active', activated_at = %s, deactivated_at = NULL WHERE id = %s", (_now(), int(config_id)))
                self._audit(
                    cursor,
                    action="activate",
                    actor=actor,
                    provider=str(target["provider"]),
                    config_id=int(config_id),
                    after={"provider": target["provider"], "model_id": target["model_id"], "status": "active"},
                )
            finally:
                cursor.close()

    def rollback(self, *, actor: str, config_id: int | None = None) -> int:
        with self._connection() as connection:
            cursor = self._cursor(connection)
            try:
                if config_id is None:
                    cursor.execute("SELECT id FROM llm_config_versions WHERE status = 'inactive' ORDER BY deactivated_at DESC, id DESC LIMIT 1")
                else:
                    cursor.execute("SELECT id FROM llm_config_versions WHERE id = %s AND status = 'inactive'", (int(config_id),))
                row = self._one(cursor)
                if not row:
                    raise LLMSettingsError("no previously verified configuration is available")
                target_id = int(row["id"])
            finally:
                cursor.close()
        self.activate_inactive(target_id, actor=actor)
        return target_id

    def activate_inactive(self, config_id: int, *, actor: str) -> None:
        with self._connection() as connection:
            cursor = self._cursor(connection)
            try:
                cursor.execute("SELECT * FROM llm_config_versions WHERE id = %s FOR UPDATE", (int(config_id),))
                target = self._one(cursor)
                if not target or str(target["status"]) != "inactive":
                    raise LLMSettingsError("rollback target is not an inactive verified version")
                result = target.get("test_result_json")
                if isinstance(result, str):
                    result = json.loads(result)
                if not isinstance(result, Mapping) or not bool(result.get("passed")):
                    raise LLMSettingsError("rollback target has no passing test result")
                self._config_with_secret(cursor, int(config_id))
                cursor.execute("UPDATE llm_config_versions SET status = 'inactive', deactivated_at = %s WHERE status = 'active'", (_now(),))
                cursor.execute("UPDATE llm_config_versions SET status = 'active', activated_at = %s, deactivated_at = NULL WHERE id = %s", (_now(), int(config_id)))
                self._audit(cursor, action="rollback", actor=actor, provider=str(target["provider"]), config_id=int(config_id), after={"provider": target["provider"], "model_id": target["model_id"], "status": "active"})
            finally:
                cursor.close()

    def clear_credentials(self, provider: str, *, actor: str) -> None:
        provider_value = self._provider(provider)
        with self._connection() as connection:
            cursor = self._cursor(connection)
            try:
                cursor.execute("UPDATE llm_provider_credentials SET revoked_at = %s WHERE provider = %s AND revoked_at IS NULL", (_now(), provider_value))
                self._audit(cursor, action="clear_credentials", actor=actor, provider=provider_value, api_key_changed=True)
            finally:
                cursor.close()

    def audit_logs(self, *, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self._connection() as connection:
            cursor = self._cursor(connection)
            try:
                cursor.execute(
                    """
                    SELECT id, action, provider, config_version_id, before_json,
                           after_json, api_key_changed, changed_by, created_at
                    FROM llm_config_audit_logs ORDER BY id DESC LIMIT %s
                    """,
                    (safe_limit,),
                )
                rows = self._all(cursor)
            finally:
                cursor.close()
        return [{**row, "api_key_changed": bool(row.get("api_key_changed"))} for row in rows]


class LLMCompatibilityService:
    def __init__(self, repository: LLMSettingsRepository) -> None:
        self.repository = repository

    @staticmethod
    def _client(config: RuntimeLLMConfig) -> AsyncOpenAI:
        provider = PROVIDERS[config.provider]
        return AsyncOpenAI(
            api_key=config.api_key,
            base_url=provider["base_url"],
            timeout=provider["timeout"],
        )

    async def refresh_models(self, config_id: int) -> list[str]:
        config = self.repository.candidate_config(config_id)
        client = self._client(config)
        try:
            response = await client.models.list()
            model_ids = sorted({str(item.id).strip() for item in response.data if str(item.id).strip()})
        except Exception as exc:
            raise LLMSettingsError(f"model list request failed: {_safe_error(exc)}") from exc
        finally:
            await client.close()
        if not model_ids:
            raise LLMSettingsError("model list endpoint returned no models")
        self.repository.store_models(config.provider, model_ids)
        return model_ids

    async def test_candidate(self, config_id: int) -> dict[str, Any]:
        config = self.repository.candidate_config(config_id)
        client = self._client(config)
        started = time.perf_counter()
        checks = {"chat": False, "tool_call": False, "finance_json": False}
        error = ""
        try:
            chat = await client.chat.completions.create(
                model=config.model_id,
                messages=[{"role": "user", "content": "Reply with exactly OK."}],
                max_tokens=8,
            )
            checks["chat"] = bool(chat.choices)
            tool = await client.chat.completions.create(
                model=config.model_id,
                messages=[{"role": "user", "content": "Call the health_check tool once."}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "health_check",
                        "description": "Return service health.",
                        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                }],
                tool_choice="auto",
                max_tokens=80,
            )
            checks["tool_call"] = bool(tool.choices and tool.choices[0].message.tool_calls)
            structured = await client.chat.completions.create(
                model=config.model_id,
                messages=[{
                    "role": "user",
                    "content": (
                        "Return JSON only for an unknown logistics finance fee. "
                        "Required keys: fee_level, canonical_subject, reason, confidence, uncertainties. "
                        "fee_level must be waybill, operating, or unclassified."
                    ),
                }],
                response_format={"type": "json_object"},
                max_tokens=240,
            )
            content = structured.choices[0].message.content if structured.choices else ""
            payload = json.loads(content or "")
            checks["finance_json"] = (
                isinstance(payload, dict)
                and payload.get("fee_level") in {"waybill", "operating", "unclassified"}
                and all(key in payload for key in ("canonical_subject", "reason", "confidence", "uncertainties"))
            )
            if not all(checks.values()):
                error = "one or more required compatibility checks returned an invalid result"
        except Exception as exc:
            error = _safe_error(exc)
        finally:
            await client.close()
        result = {
            "passed": all(checks.values()) and not error,
            "provider": config.provider,
            "model_id": config.model_id,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "checks": checks,
        }
        self.repository.record_test(config_id, result, error=error)
        if error:
            result["error"] = error
        return result


def environment_runtime_config() -> RuntimeLLMConfig | None:
    configured = []
    for provider, cfg in PROVIDERS.items():
        api_key = str(os.getenv(cfg["env_key"]) or "").strip()
        if api_key:
            configured.append(
                RuntimeLLMConfig(
                    provider=provider,
                    model_id=str(cfg["default_model"]),
                    api_key=api_key,
                    source="environment",
                )
            )
    if not configured:
        return None
    # Historical deployments treated DeepSeek as primary.  This deterministic
    # choice is used only until the first database configuration is activated.
    return next((item for item in configured if item.provider == "deepseek"), configured[0])
