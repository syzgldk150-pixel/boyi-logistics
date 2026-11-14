"""Customer share links on the receipt page (spec ⑩a / ⑩b): customers, rules and links.

Open to accounts holding the ``receipt.share`` permission (projected by
``shared/identity_routes.py``; super administrators have it). Rules live in ``shared/receipt_share.py`` and SQL in
``shared/receipt_share_repository.py``; this mixin validates requests, writes
the rows with an event, and asks the Agent to refresh a link. The link code is
derived from the deployment secret, never stored, and only its digest is saved.
"""
from __future__ import annotations

from console.app_support import *  # noqa: F403
from shared import receipt_share as share_rules
from shared import receipt_share_repository as share_repository
from shared.receipt_share_settings import load_share_settings

SHARE_JSON_MAX_BYTES = 64 * 1024
SHARE_ACTIONS = {"disable", "enable", "reset", "renew", "refresh"}
DEFAULT_SHARE_BASE_URL = "https://r.boyi.homes"
_SHARE_ACTION_ROUTE = re.compile(r"/receipts/shares/(\d{1,18})/([a-z]+)")


class ReceiptSharesServiceMixin:
    # -- configuration ---------------------------------------------------
    def _receipt_share_settings(self) -> dict[str, str]:
        return load_share_settings() or {}

    def _receipt_share_secret(self) -> str:
        secret = str(self._receipt_share_settings().get("RECEIPT_SHARE_TOKEN_SECRET") or "")
        return secret if len(secret) >= 32 else ""

    def _receipt_share_url(self, share: Mapping[str, Any], secret: str) -> str:
        base = str(self._receipt_share_settings().get("RECEIPT_SHARE_BASE_URL") or DEFAULT_SHARE_BASE_URL).rstrip("/")
        return f"{base}/r/{share_rules.share_token(secret, share['id'], share['token_version'])}"

    def _can_manage_receipt_shares(self, handler: BaseHTTPRequestHandler) -> bool:
        user = getattr(handler, "current_admin_user", None) or current_admin_user() or {}
        return self._is_super_admin_user(user) or "receipt.share" in (user.get("access_permissions") or ())

    # -- request plumbing --------------------------------------------------
    def _share_error(self, handler: BaseHTTPRequestHandler, status: int, code: str, message: str) -> None:
        self._send_json(handler, status, {"ok": False, "error": {"code": code, "message": message}, "message": message})

    def _share_admin(self, handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
        # The route permission (receipt.share) is already enforced; a link is always tied to a real account.
        user = getattr(handler, "current_admin_user", None) or current_admin_user()
        if not isinstance(user, dict) or user.get("is_legacy_basic_auth") or int(user.get("id") or 0) <= 0:
            self._share_error(handler, HTTPStatus.FORBIDDEN, "MYSQL_ADMIN_SESSION_REQUIRED", "客户共享需要已登录的后台账号。")
            return None
        return user

    def _share_write_body(self, handler: BaseHTTPRequestHandler) -> tuple[dict[str, Any], dict[str, Any], str] | None:
        user = self._share_admin(handler)
        if user is None or not self._require_same_origin_write(handler):
            return None
        media_type = str(handler.headers.get("Content-Type") or "").partition(";")[0].strip().lower()
        try:
            length = int(handler.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            length = -1
        body: Any = None
        if media_type == "application/json" and 0 <= length <= SHARE_JSON_MAX_BYTES:
            try:
                body = json.loads(handler.rfile.read(length).decode("utf-8")) if length else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = None
        if not isinstance(body, dict):
            self._share_error(handler, HTTPStatus.BAD_REQUEST, "INVALID_JSON", "请求内容无效。")
            return None
        request_id = self._normalize_browser_request_uuid(body.pop("request_id", None))
        if not request_id or request_id != self._normalize_browser_request_uuid(
                handler.headers.get("X-Browser-Request-UUID")):
            self._share_error(handler, HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "请求标识无效，请刷新页面后重试。")
            return None
        return body, user, request_id

    def _share_run(self, handler: BaseHTTPRequestHandler, work: Callable[[], Any]) -> None:
        try:
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": work()})
        except share_rules.ReceiptShareError as exc:
            self._share_error(handler, HTTPStatus.BAD_REQUEST, "SHARE_INVALID", str(exc))
        except share_repository.ShareConflict as exc:
            self._share_error(handler, HTTPStatus.CONFLICT, "SHARE_CONFLICT", str(exc))

    @staticmethod
    def _share_actor(user: Mapping[str, Any]) -> tuple[str, str]:
        return str(user["id"]), str(user.get("display_name") or user.get("username") or "")

    # -- projections -------------------------------------------------------
    def _share_view(self, share: Mapping[str, Any], secret: str, now: datetime) -> dict[str, Any]:
        expired = share["expires_at"] is not None and share["expires_at"] <= now
        state = "disabled" if share["status"] != "active" else ("expired" if expired else "active")

        def stamp(value: Any, pattern: str) -> str:
            return value.strftime(pattern) if isinstance(value, datetime) else ""

        return {
            "id": share["id"], "record_version": share["record_version"],
            "customer_id": share["customer_id"], "customer_name": share["customer_name"],
            "customer_code": share["customer_code"],
            "url": self._receipt_share_url(share, secret) if secret else "",
            "naming_template": share["naming_template"],
            "naming_label": "默认规则" if share["naming_template"] == share_rules.DEFAULT_NAMING_TEMPLATE else "自定义规则",
            "image_types": share["image_types"], "image_types_text": share_rules.image_kinds_text(share["image_types"]),
            "scope": share["scope"],
            "expires_text": f"至 {stamp(share['expires_at'], '%Y-%m-%d')}" if share["expires_at"] else "长期有效",
            "view_count": int(share["view_count"] or 0), "last_viewed_text": stamp(share["last_viewed_at"], "%m-%d %H:%M"),
            "state": state, "state_label": {"active": "生效中", "expired": "已过期", "disabled": "已停用"}[state],
            "has_access_code": bool(share["access_code_sha256"]),
            # Copied together with the link; links made before migration 079 have no recoverable code.
            "access_code": share_rules.open_access_code(secret, share["id"], share.get("access_code_sealed")) if secret else "",
            "item_count": int(share.get("item_count") or 0),
            "refresh_state": share["refresh_state"], "refresh_message": share["refresh_message"],
            "last_refreshed_text": stamp(share["last_refreshed_at"], "%m-%d %H:%M"),
        }

    def _share_listing(self) -> dict[str, Any]:
        secret, now = self._receipt_share_secret(), datetime.now()
        with self.repository.connect() as connection:
            shares = share_repository.list_shares(connection)
            customers = share_repository.list_customers(connection)
        return {
            "configured": bool(secret),
            "shares": [self._share_view(item, secret, now) for item in shares],
            "customers": customers,
            "defaults": {"naming_template": share_rules.DEFAULT_NAMING_TEMPLATE,
                         "naming_fields": list(share_rules.NAMING_FIELDS),
                         "image_kinds": [{"value": kind, "label": share_rules.IMAGE_KIND_LABELS[kind]}
                                         for kind in share_rules.IMAGE_KINDS],
                         "match_fields": [{"value": key, "label": label} for key, label in share_rules.MATCH_FIELDS.items()]},
        }

    # -- GET ---------------------------------------------------------------
    def _render_receipt_shares(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        user = getattr(handler, "current_admin_user", None) or current_admin_user()
        if not isinstance(user, dict) or user.get("is_legacy_basic_auth") or int(user.get("id") or 0) <= 0:
            self._send_text(handler, HTTPStatus.FORBIDDEN, "客户共享需要已登录的后台账号。")
            return
        template = self.template_env.get_template("receipt_shares.html")
        self._send_html(handler, template.render(app_title=self.settings.app_title, listing=self._share_listing()))

    def _handle_receipt_shares_data(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        if self._share_admin(handler) is not None:
            self._share_run(handler, self._share_listing)

    def _handle_receipt_share_senders(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        if self._share_admin(handler) is None:
            return
        keyword = str((query.get("q") or [""])[0] or "").strip()

        def work() -> list[dict[str, Any]]:
            if len(keyword) < 2 or len(keyword) > 60:
                raise share_rules.ReceiptShareError("请输入 2 个字以上的发货人名称。")
            with self.repository.connect() as connection:
                return share_repository.sender_candidates(connection, keyword)

        self._share_run(handler, work)

    def _handle_receipt_share_access_code(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        """A random code from the server's generator (same rules as validation)."""

        if self._share_admin(handler) is not None:
            self._share_run(handler, lambda: {"access_code": share_rules.generate_access_code()})

    def _handle_receipt_share_naming_example(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        """The dialog's live example comes from the same renderer as real downloads."""

        if self._share_admin(handler) is None:
            return
        template = str((query.get("template") or [""])[0] or "")
        short_name = str((query.get("short_name") or [""])[0] or "").strip()[:40] or "客户简称"
        self._share_run(handler, lambda: {"example": share_rules.render_file_name(
            template, short_name=short_name, waybill_no="R00024388425", kind="sign",
            doc_date=datetime.now(), seq=1, shipment_no="1001-2600000001")})

    # -- POST --------------------------------------------------------------
    def _handle_receipt_share_customer(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = self._share_write_body(handler)
        if parsed is None:
            return
        body, user, _request_id = parsed

        def work() -> dict[str, Any]:
            if set(body) != {"customer_id", "name", "short_name", "match"}:
                raise share_rules.ReceiptShareError("客户资料不完整。")
            name, short_name = str(body["name"] or "").strip(), str(body["short_name"] or "").strip()
            if not 2 <= len(name) <= 160 or not 1 <= len(short_name) <= 40:
                raise share_rules.ReceiptShareError("客户全称为 2–160 个字，简称为 1–40 个字。")
            customer_id = body["customer_id"]
            if customer_id is not None and (not isinstance(customer_id, int) or customer_id <= 0):
                raise share_rules.ReceiptShareError("客户编号无效。")
            match = share_rules.normalize_match_rules(body["match"])
            actor_id, actor_name = self._share_actor(user)
            with self.repository.connect() as connection:
                saved = share_repository.save_customer(connection, customer_id=customer_id, name=name,
                                                       short_name=short_name, match=match, actor_id=actor_id)
                share_repository.add_event(connection, share_id=None, customer_id=saved,
                                           action="customer_saved", actor_id=actor_id, actor_name=actor_name,
                                           detail={"name": name, "match": match})
                probe = {"match": match, "scope": "all"}
                waybills = len(share_repository.matching_waybills(connection, probe, limit=5000))
            return {"customer_id": saved, "matching_waybills": waybills}

        self._share_run(handler, work)

    def _handle_receipt_share_preview(self, handler: BaseHTTPRequestHandler) -> None:
        """How many waybills a rule set matches, before it is saved."""

        parsed = self._share_write_body(handler)
        if parsed is None:
            return
        body, _user, _request_id = parsed

        def work() -> dict[str, Any]:
            match = share_rules.normalize_match_rules(body.get("match"))
            with self.repository.connect() as connection:
                rows = share_repository.matching_waybills(connection, {"match": match, "scope": "all"}, limit=5000)
            return {"matching_waybills": len(rows), "capped": len(rows) >= 5000,
                    "sample": [row["waybill_no"] for row in rows[:5]]}

        self._share_run(handler, work)

    def _handle_receipt_share_create(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = self._share_write_body(handler)
        if parsed is None:
            return
        body, user, request_id = parsed
        secret = self._receipt_share_secret()
        if not secret:
            self._share_error(handler, HTTPStatus.SERVICE_UNAVAILABLE, "SHARE_NOT_CONFIGURED",
                              "服务器尚未配置共享链接密钥，暂时不能生成链接。")
            return
        created: dict[str, Any] = {}

        def work() -> dict[str, Any]:
            expected = {"customer_id", "scope", "date_from", "date_to", "image_types", "expires_days", "access_code",
                        "naming_template"}
            if set(body) != expected:
                raise share_rules.ReceiptShareError("链接设置不完整。")
            scope = str(body["scope"])
            if scope not in share_rules.SHARE_SCOPES:
                raise share_rules.ReceiptShareError("共享范围无效。")
            date_from = normalize_open_date(str(body["date_from"] or "")) if scope == "by_date" else ""
            date_to = normalize_open_date(str(body["date_to"] or "")) if scope == "by_date" else ""
            if scope == "by_date" and (not date_from or not date_to or date_from > date_to):
                raise share_rules.ReceiptShareError("按发货日期共享时请选择起止日期。")
            expires_days = share_rules.normalize_expiry_days(body["expires_days"])
            image_types = share_rules.normalize_image_kinds(body["image_types"])
            template = share_rules.normalize_naming_template(body["naming_template"])
            code = share_rules.normalize_access_code(body["access_code"], new=True)
            if not isinstance(body["customer_id"], int):
                raise share_rules.ReceiptShareError("请选择客户单位。")
            expires_at = datetime.now() + timedelta(days=expires_days) if expires_days else None
            actor_id, actor_name = self._share_actor(user)
            with self.repository.connect() as connection:
                existing = share_repository.share_by_request(connection, request_id)
                if existing is None:
                    customer = share_repository.get_customer(connection, body["customer_id"])
                    if customer is None or not customer["is_active"]:
                        raise share_rules.ReceiptShareError("客户单位不存在。")
                    existing = share_repository.create_share(
                        connection, request_id=request_id, customer_id=customer["id"], scope=scope,
                        date_from=date_from, date_to=date_to, image_types=image_types, naming_template=template,
                        expires_at=expires_at, actor_id=actor_id,
                        token_digest=lambda share_id, version: share_rules.token_sha256(
                            share_rules.share_token(secret, share_id, version)),
                        access_code_digest=lambda share_id: (
                            share_rules.access_code_sha256(secret, share_id, code) if code else None),
                        access_code_sealed=(lambda share_id: share_rules.seal_access_code(secret, share_id, code))
                        if code else None)
                    share_repository.add_event(
                        connection, share_id=existing["id"], customer_id=customer["id"], action="created",
                        actor_id=actor_id, actor_name=actor_name,
                        detail={"scope": scope, "image_types": image_types, "expires_days": expires_days,
                                "has_access_code": bool(code), "naming_template": template})
            created.update(existing)
            return {"share": self._share_view(existing, secret, datetime.now()), "access_code": code}

        self._share_run(handler, work)
        if created:
            self._request_receipt_share_refresh(handler, int(created["id"]))

    def _handle_receipt_share_action(self, handler: BaseHTTPRequestHandler, share_id: int, action: str) -> None:
        parsed = self._share_write_body(handler)
        if parsed is None:
            return
        body, user, _request_id = parsed
        secret = self._receipt_share_secret()
        if action not in SHARE_ACTIONS or (action == "reset" and not secret):
            self._share_error(handler, HTTPStatus.BAD_REQUEST, "SHARE_ACTION_INVALID", "这个操作暂时不能执行。")
            return
        refresh = {"wanted": False}

        def work() -> dict[str, Any]:
            version = body.get("record_version")
            if not isinstance(version, int) or isinstance(version, bool):
                raise share_rules.ReceiptShareError("请刷新页面后重试。")
            actor_id, actor_name = self._share_actor(user)
            with self.repository.connect() as connection:
                share = share_repository.get_share(connection, share_id, for_update=True)
                if share is None:
                    raise share_repository.ShareConflict("这条链接不存在。")
                detail: dict[str, Any] = {}
                if action == "refresh":
                    updated = share
                elif action in {"disable", "enable"}:
                    updated = share_repository.update_share(
                        connection, share_id, expected_version=version,
                        status="disabled" if action == "disable" else "active")
                elif action == "renew":
                    days = share_rules.normalize_expiry_days(body.get("expires_days"))
                    detail["expires_days"] = days
                    updated = share_repository.update_share(
                        connection, share_id, expected_version=version, status="active",
                        expires_at=datetime.now() + timedelta(days=days) if days else None)
                else:  # reset: a new code; the old link stops working at once
                    next_version = int(share["token_version"]) + 1
                    updated = share_repository.update_share(
                        connection, share_id, expected_version=version, token_version=next_version,
                        token_sha256=share_rules.token_sha256(share_rules.share_token(secret, share_id, next_version)))
                if action != "refresh":
                    share_repository.add_event(connection, share_id=share_id, customer_id=share["customer_id"],
                                               action=action, actor_id=actor_id, actor_name=actor_name, detail=detail)
            refresh["wanted"] = action in {"refresh", "enable", "renew"}
            return {"share": self._share_view(updated, secret, datetime.now())}

        self._share_run(handler, work)
        if refresh["wanted"]:
            self._request_receipt_share_refresh(handler, share_id)

    def _request_receipt_share_refresh(self, handler: BaseHTTPRequestHandler, share_id: int) -> None:
        """Best effort: the daily schedule refreshes the link anyway."""

        user = getattr(handler, "current_admin_user", None) or current_admin_user()
        try:
            self._agent_request("POST", "/internal/v1/admin/receipt-shares/refresh", payload={"share_id": share_id},
                                timeout=10, console_principal=self._mysql_console_principal(user))
        except Exception:  # noqa: BLE001 - never fail the saved change because the Agent is busy
            LOGGER.warning("Receipt share refresh request failed share=%s", share_id)

    def _receipt_share_action_route(self, path: str) -> tuple[int, str] | None:
        match = _SHARE_ACTION_ROUTE.fullmatch(path)
        return (int(match.group(1)), match.group(2)) if match else None
