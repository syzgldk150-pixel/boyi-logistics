"""Console application services grouped by business responsibility."""

from console.app_support import *  # noqa: F403


class WaybillsReceiptsServiceMixin:
    def _render_dispatch(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        dispatch_config = {
            "amap_js_key":        self.settings.amap_api_key or "YOUR_AMAP_JS_API_KEY",
            "amap_security_code": self.settings.amap_security_code or "",
        }
        dispatch_sdk_should_load = not dispatch_config["amap_js_key"].startswith("YOUR_")
        template = self.template_env.get_template("dispatch.html")
        body = template.render(
            app_title=self.settings.app_title,
            dispatch_config=dispatch_config,
            dispatch_sdk_should_load=dispatch_sdk_should_load,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    def _render_tracking(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        template = self.template_env.get_template("tracking.html")
        body = template.render(
            app_title=self.settings.app_title,
            initial_tracking_number=query.get("tracking_number", [""])[0].strip(),
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    @staticmethod
    def _waybill_sync_date_span(filters: dict[str, Any]) -> tuple[str, str, str]:
        date_from = str(filters.get("date_from") or "").strip()
        date_to = str(filters.get("date_to") or "").strip()
        if not date_from and not date_to:
            return "", "", ""

        start_text = (date_from or date_to).replace("/", "-")
        end_text = (date_to or date_from).replace("/", "-")
        try:
            start_date = datetime.strptime(start_text, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_text, "%Y-%m-%d").date()
        except ValueError:
            return "", "", "开单日期格式无效，已跳过外部刷新。"
        if start_date > end_date:
            return "", "", "开单开始日期不能晚于结束日期，已跳过外部刷新。"
        if (end_date - start_date).days + 1 > 31:
            return "", "", "开单日期范围超过 31 天，已跳过外部刷新，请缩小范围后重试。"
        return start_date.isoformat(), end_date.isoformat(), ""

    @staticmethod
    def _waybill_sync_providers(source: str) -> list[str]:
        if source == "all":
            return ["ronghui", "yunda"]
        if source in {"ronghui", "yunda"}:
            return [source]
        return []

    @staticmethod
    def _waybill_default_date() -> str:
        return datetime.now().strftime("%Y/%m/%d")

    @staticmethod
    def _waybill_sync_message_from_agent(provider_label: str, tool_result: dict[str, Any]) -> str:
        fetched = tool_result.get("fetched")
        sql_upserted = tool_result.get("sql_upserted")
        sql_deleted = tool_result.get("sql_deleted_stale")
        parts = []
        if fetched not in (None, ""):
            parts.append(f"拉取 {fetched} 条")
        if sql_upserted not in (None, ""):
            parts.append(f"入库 {sql_upserted} 条")
        if sql_deleted not in (None, "", 0):
            parts.append(f"清理旧数据 {sql_deleted} 条")
        return f"已刷新{provider_label}" + (f"（{'，'.join(parts)}）" if parts else "")

    @staticmethod
    def _waybill_agent_error_text(result: dict[str, Any]) -> str:
        error = result.get("error")
        if isinstance(error, dict):
            return str(error.get("error") or error.get("message") or error.get("detail") or error)
        if error:
            return str(error)
        return "智能服务调用失败。"

    def _render_waybills(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        def first_value(name: str, default: str = "") -> str:
            return str(query.get(name, [default])[0] or "").strip()

        def positive_int(name: str, default: int) -> int:
            try:
                return max(int(first_value(name, str(default))), 1)
            except ValueError:
                return default

        requested_date_from = normalize_open_date(first_value("date_from")).replace("-", "/")
        requested_date_to = normalize_open_date(first_value("date_to")).replace("-", "/")
        filters = {
            "q": first_value("q"),
            "date_from": requested_date_from,
            "date_to": requested_date_to,
            "status": first_value("status", "all").lower() or "all",
            "source": first_value("source", "all").lower() or "all",
            "payment_method": first_value("payment_method"),
            "delivery_method": first_value("delivery_method"),
            "sort": first_value("sort", "open_date_desc") or "open_date_desc",
        }
        if filters["status"] != "all":
            filters["status"] = normalize_waybill_status(filters["status"])
        if filters["source"] not in {"all", *WAYBILL_SOURCE_LABELS.keys()}:
            filters["source"] = "all"
        if filters["sort"] not in {"open_date_desc", "open_date_asc"}:
            filters["sort"] = "open_date_desc"
        page = positive_int("page", 1)
        page_size = min(max(positive_int("page_size", 50), 10), 100)
        has_requested_date_filter = bool(requested_date_from or requested_date_to)
        has_active_non_date_filters = any(
            str(filters.get(name, "") or "").strip()
            for name in ("q", "payment_method", "delivery_method")
        ) or filters["status"] != "all" or filters["source"] != "all"
        if not has_requested_date_filter and not has_active_non_date_filters:
            today = self._waybill_default_date()
            filters["date_from"] = today
            filters["date_to"] = today
        has_active_filters = has_requested_date_filter or has_active_non_date_filters

        status_options = [
            {"value": "all", "label": "全部状态", "tone": "muted"},
            *[
                {"value": value, "label": label, "tone": WAYBILL_STATUS_TONES[value]}
                for value, label in WAYBILL_STATUS_LABELS.items()
            ],
        ]
        source_options = [
            {"value": "all", "label": "全部来源"},
            *[
                {"value": value, "label": label}
                for value, label in WAYBILL_SOURCE_LABELS.items()
            ],
        ]
        payment_options = ["", "现付", "寄付", "到付", "提付", "月结"]
        delivery_options = ["", "送货", "自提", "派送"]
        sort_options = [
            {"value": "open_date_desc", "label": "按开单日期倒序"},
            {"value": "open_date_asc", "label": "按开单日期正序"},
        ]

        def empty_result() -> dict[str, Any]:
            result = {
                "rows": [],
                "summary": {
                    "total": 0,
                    "manual_count": 0,
                    "ocr_count": 0,
                    "fee_total": "0.00",
                    "opening_cost_total": "0.00",
                    "insurance_total": "0.00",
                    "cod_total": "0.00",
                    "pickup_payment_total": "0.00",
                    "invalid_money_count": 0,
                    "latest_created_at": "",
                    "latest_open_date": "",
                },
                "pagination": {
                    "page": 1,
                    "page_size": page_size,
                    "total": 0,
                    "total_pages": 1,
                    "offset": 0,
                    "has_prev": False,
                    "has_next": False,
                },
            }
            return result

        sync_status: dict[str, list[str]] = {"messages": [], "warnings": []}
        if has_requested_date_filter and filters["source"] in {
            "all",
            "ronghui",
            "yunda",
        }:
            sync_status["warnings"].append(
                "当前列表只读取已持久化快照；GET 查询不会刷新外部来源，请从自动化页面显式提交同步计划。"
            )

        if has_active_filters:
            try:
                result = self.repository.search_waybills(filters, page=page, page_size=page_size)
                db_error = ""
            except Exception as exc:
                result = empty_result()
                db_error = str(exc)
        else:
            result = empty_result()
            db_error = ""

        pagination = result["pagination"]
        base_query = {
            **{
                k: v
                for k, v in filters.items()
                if str(v) and not (k in {"status", "source"} and v == "all")
            },
            "page_size": str(pagination["page_size"]),
        }
        prev_url = ""
        next_url = ""
        if pagination["has_prev"]:
            prev_url = "/waybills?" + urlencode({**base_query, "page": pagination["page"] - 1})
        if pagination["has_next"]:
            next_url = "/waybills?" + urlencode({**base_query, "page": pagination["page"] + 1})
        current_url = "/waybills?" + urlencode({**base_query, "page": pagination["page"]})

        template = self.template_env.get_template("waybills.html")
        body = template.render(
            app_title=self.settings.app_title,
            filters=filters,
            rows=result["rows"],
            summary=result["summary"],
            pagination=pagination,
            prev_url=prev_url,
            next_url=next_url,
            current_url=current_url,
            status_options=status_options,
            source_options=source_options,
            payment_options=payment_options,
            delivery_options=delivery_options,
            sort_options=sort_options,
            db_error=db_error,
            sync_status=sync_status,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    @staticmethod
    def _receipt_default_date() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _receipt_filters_from_query(self, query: dict[str, list[str]]) -> dict[str, str]:
        def first_value(name: str, default: str = "") -> str:
            return str((query or {}).get(name, [default])[0] or "").strip()

        platform = first_value("platform", "all").lower() or "all"
        if platform not in {"all", "yunda", "ronghui"}:
            platform = "all"
        photo_status = first_value("photo_status", "all").lower() or "all"
        if photo_status not in {"all", "has_photo", "missing_photo"}:
            photo_status = "all"
        date_from = first_value("date_from")
        date_to = first_value("date_to")
        if not date_from and not date_to:
            today = self._receipt_default_date()
            date_from = today
            date_to = today
        return {
            "platform": platform,
            "direction": "send",
            "q": first_value("q"),
            "receipt_status": first_value("receipt_status", "all") or "all",
            "audit_status": first_value("audit_status", "all") or "all",
            "photo_status": photo_status,
            "date_from": date_from,
            "date_to": date_to,
        }

    def _receipt_positive_int(self, query: dict[str, list[str]], name: str, default: int) -> int:
        try:
            return max(int(str(query.get(name, [str(default)])[0] or default)), 1)
        except (TypeError, ValueError):
            return default

    def _receipt_query_requested(self, query: dict[str, list[str]]) -> bool:
        raw = str(query.get("queried", [""])[0] or "").strip().lower()
        return raw in {"1", "true", "yes"}

    def _empty_receipt_search_result(self, *, page_size: int) -> dict[str, Any]:
        return {
            "rows": [],
            "pagination": {
                "page": 1,
                "page_size": page_size,
                "total": 0,
                "total_pages": 1,
                "offset": 0,
                "has_prev": False,
                "has_next": False,
            },
        }

    def _render_receipts(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        filters = self._receipt_filters_from_query(query)
        page = self._receipt_positive_int(query, "page", 1)
        page_size = min(max(self._receipt_positive_int(query, "page_size", 50), 10), 100)
        query_requested = self._receipt_query_requested(query)
        db_error = ""
        if not query_requested:
            result = self._empty_receipt_search_result(page_size=page_size)
        else:
            try:
                result = self.repository.search_receipts(filters, page=page, page_size=page_size)
            except Exception as exc:
                result = self._empty_receipt_search_result(page_size=page_size)
                db_error = str(exc)
        pagination = result["pagination"]
        base_query = {
            key: value
            for key, value in filters.items()
            if str(value or "").strip() and value != "all"
        }
        if query_requested:
            base_query["queried"] = "1"
        base_query["page_size"] = str(pagination["page_size"])
        prev_url = ""
        next_url = ""
        if pagination.get("has_prev"):
            prev_url = "/receipts?" + urlencode({**base_query, "page": pagination["page"] - 1})
        if pagination.get("has_next"):
            next_url = "/receipts?" + urlencode({**base_query, "page": pagination["page"] + 1})
        template = self.template_env.get_template("receipts.html")
        body = template.render(
            app_title=self.settings.app_title,
            filters=filters,
            rows=result["rows"],
            pagination=pagination,
            prev_url=prev_url,
            next_url=next_url,
            query_requested=query_requested,
            db_error=db_error,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    def _handle_receipts_data(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        filters = self._receipt_filters_from_query(query)
        page = self._receipt_positive_int(query, "page", 1)
        page_size = min(max(self._receipt_positive_int(query, "page_size", 50), 10), 100)
        if not self._receipt_query_requested(query):
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": self._empty_receipt_search_result(page_size=page_size)})
            return
        try:
            result = self.repository.search_receipts(filters, page=page, page_size=page_size)
        except Exception as exc:
            self._send_json(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "message": f"回单列表查询失败：{exc}", "data": {"rows": [], "pagination": {}}},
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": result})

    def _parse_receipt_path_id(self, path: str, prefix: str) -> int | None:
        raw = str(path or "").strip().rstrip("/")
        if not raw.startswith(prefix):
            return None
        tail = raw[len(prefix) :].strip("/")
        if not tail or "/" in tail:
            return None
        try:
            return int(tail)
        except ValueError:
            return None

    def _receipt_detail_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple, set)):
            parts = [self._receipt_detail_text(item) for item in value]
            return " / ".join(part for part in parts if part)
        if isinstance(value, dict):
            for key in ("text", "value", "name", "title", "link"):
                text = self._receipt_detail_text(value.get(key))
                if text:
                    return text
            parts = [self._receipt_detail_text(item) for item in value.values()]
            return " / ".join(part for part in parts if part)
        text = str(value).strip()
        if text.startswith("="):
            text = text[1:].strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            text = text[1:-1].strip()
        return re.sub(r"\s+", " ", text).strip()

    def _receipt_detail_missing(self, summary: dict[str, Any]) -> list[str]:
        return [key for key in RECEIPT_DETAIL_KEYS if not self._receipt_detail_text(summary.get(key))]

    def _receipt_detail_append_source(self, sources: list[str], source: str) -> None:
        if source and source not in sources:
            sources.append(source)

    def _receipt_detail_merge_missing(
        self,
        summary: dict[str, str],
        values: dict[str, Any],
        *,
        source: str,
        sources: list[str],
    ) -> bool:
        filled = False
        for key in RECEIPT_DETAIL_KEYS:
            if self._receipt_detail_text(summary.get(key)):
                continue
            text = self._receipt_detail_text(values.get(key))
            if not text:
                continue
            summary[key] = text
            filled = True
        if filled:
            self._receipt_detail_append_source(sources, source)
        return filled

    def _receipt_detail_summary_from_record(self, record: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
        raw_payload = record.get("raw_payload")
        existing = record.get("detail_summary")
        if isinstance(existing, dict):
            base = {key: self._receipt_detail_text(existing.get(key)) for key in RECEIPT_DETAIL_KEYS}
        else:
            base = DocumentRepository._receipt_detail_summary(raw_payload, record)
            base = {key: self._receipt_detail_text(base.get(key)) for key in RECEIPT_DETAIL_KEYS}
        if not self._receipt_detail_text(base.get("waybill_no")):
            base["waybill_no"] = self._receipt_detail_text(record.get("waybill_no") or record.get("receipt_no"))

        sources: list[str] = []
        raw_only = DocumentRepository._receipt_detail_summary(raw_payload, {})
        if any(self._receipt_detail_text(raw_only.get(key)) for key in RECEIPT_DETAIL_KEYS):
            self._receipt_detail_append_source(sources, "raw_payload")
        return base, sources

    def _receipt_detail_weight_volume(self, value: Any) -> dict[str, str]:
        text = self._receipt_detail_text(value)
        if not text:
            return {}
        result: dict[str, str] = {}
        weight_match = re.search(r"实际重量\s*[:：]?\s*([^/；;|,\n]+)", text)
        if weight_match:
            result["actual_weight"] = weight_match.group(1).strip()
        volume_match = re.search(r"(?:^|[/；;|,\s])体积(?!重)\s*[:：]?\s*([^/；;|,\n]+)", text)
        if volume_match:
            result["volume"] = volume_match.group(1).strip()
        return result

    def _receipt_detail_from_local_waybill(self, waybill: dict[str, Any] | None) -> dict[str, str]:
        if not isinstance(waybill, dict):
            return {}
        values = {
            "recipient_name": waybill.get("receiver_name"),
            "recipient_address": waybill.get("receiver_address"),
            "goods_name": waybill.get("goods_name_lines"),
            "package_type": waybill.get("package_type_lines"),
            "piece_count": waybill.get("quantity_lines"),
            "waybill_no": waybill.get("waybill_no"),
        }
        values.update(self._receipt_detail_weight_volume(waybill.get("weight_volume")))
        return {key: self._receipt_detail_text(values.get(key)) for key in RECEIPT_DETAIL_KEYS if self._receipt_detail_text(values.get(key))}

    def _receipt_detail_platform(self, record: dict[str, Any]) -> str:
        return str(record.get("platform") or "").strip().lower()

    def _receipt_detail_should_query_tms(self, record: dict[str, Any], waybill_no: str) -> bool:
        platform = self._receipt_detail_platform(record)
        code = str(waybill_no or "").strip().upper()
        return platform in {"ronghui", "r7"} or code.startswith("R")

    def _receipt_detail_should_query_feishu(self, record: dict[str, Any], waybill_no: str) -> bool:
        return self._receipt_detail_platform(record) == "yunda" and bool(str(waybill_no or "").strip())

    def _receipt_detail_first_matching_row(self, rows: list[Any], waybill_no: str) -> tuple[dict[str, Any] | None, str]:
        wanted = str(waybill_no or "").strip()
        dict_rows = [row for row in rows if isinstance(row, dict)]
        if not dict_rows:
            return None, "未返回详情行"
        matches = []
        for row in dict_rows:
            for key in ("waybill_no", "bill_code", "billCode", "tracking_number", "trackingNumber"):
                if self._receipt_detail_text(row.get(key)) == wanted:
                    matches.append(row)
                    break
        if len(matches) == 1:
            return matches[0], ""
        if not matches and len(dict_rows) == 1:
            return dict_rows[0], ""
        return None, f"返回 {len(dict_rows)} 行但无法精确匹配单号 {wanted}"

    def _receipt_detail_rows_from_agent_payload(self, payload: Any) -> list[Any]:
        current = payload
        for _ in range(4):
            if isinstance(current, list):
                return current
            if not isinstance(current, dict):
                return []
            for key in ("records", "rows", "items"):
                value = current.get(key)
                if isinstance(value, list):
                    return value
            current = current.get("data")
        return []

    def _receipt_detail_from_tms(self, waybill_no: str) -> tuple[dict[str, str], str]:
        payload = {
            "params": {
                "bill_codes": [waybill_no],
                "decrypt_masked": True,
                "browser_headless": True,
                "browser_timeout_ms": 30_000,
                "browser_batch_size": 1,
                "browser_max_workers": 1,
                "max_workers": 1,
            },
            "timeout_sec": max(45, min(120, int(getattr(self.settings, "agent_timeout_seconds", 30) or 30) + 15)),
        }
        response = self._agent_request(
            "POST",
            "/internal/v1/tms/query_waybill_detail",
            payload=payload,
            timeout=max(50, payload["timeout_sec"] + 5),
        )
        if not response.get("ok"):
            return {}, self._receipt_detail_text(response.get("error")) or "融辉详情接口不可达"
        data = response.get("data")
        if isinstance(data, dict) and data.get("ok") is False:
            return {}, self._receipt_detail_text(data.get("message") or data.get("error")) or "融辉详情接口返回失败"
        row, error = self._receipt_detail_first_matching_row(self._receipt_detail_rows_from_agent_payload(data), waybill_no)
        if error:
            return {}, error
        if not row:
            return {}, "融辉详情接口未返回数据"
        values = {
            "recipient_name": row.get("recipient_name"),
            "recipient_address": row.get("recipient_address"),
            "goods_name": row.get("goods_name"),
            "package_type": row.get("package_type"),
            "piece_count": row.get("quantity") or row.get("piece_count"),
            "actual_weight": row.get("actual_weight"),
            "volume": row.get("volume"),
            "waybill_no": row.get("tracking_number") or row.get("waybill_no") or row.get("bill_code"),
        }
        return {key: self._receipt_detail_text(values.get(key)) for key in RECEIPT_DETAIL_KEYS if self._receipt_detail_text(values.get(key))}, ""

    def _enrich_receipt_detail_record(self, record: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(record)
        summary, sources = self._receipt_detail_summary_from_record(enriched)
        errors: list[str] = []
        waybill_no = self._receipt_detail_text(summary.get("waybill_no") or enriched.get("waybill_no"))

        if waybill_no and self._receipt_detail_missing(summary):
            platform = self._receipt_detail_platform(enriched)
            try:
                waybill = self.repository.get_waybill_by_no(waybill_no, source=platform if platform else None)
            except Exception as exc:
                waybill = None
                errors.append(f"local_waybills: {exc}")
            self._receipt_detail_merge_missing(
                summary,
                self._receipt_detail_from_local_waybill(waybill),
                source="local_waybills",
                sources=sources,
            )

        if waybill_no and self._receipt_detail_missing(summary) and self._receipt_detail_should_query_tms(enriched, waybill_no):
            values, error = self._receipt_detail_from_tms(waybill_no)
            if error:
                errors.append(f"tms_detail: {error}")
            self._receipt_detail_merge_missing(summary, values, source="tms_detail", sources=sources)

        enriched["detail_summary"] = {key: self._receipt_detail_text(summary.get(key)) for key in RECEIPT_DETAIL_KEYS}
        enriched["detail_summary_source"] = ",".join(sources) if sources else "无数据"
        enriched["detail_summary_missing"] = self._receipt_detail_missing(enriched["detail_summary"])
        enriched["feishu_detail_query_available"] = bool(
            waybill_no
            and enriched["detail_summary_missing"]
            and self._receipt_detail_should_query_feishu(enriched, waybill_no)
        )
        if errors:
            enriched["detail_summary_error"] = "；".join(errors)
        else:
            enriched.pop("detail_summary_error", None)
        return enriched

    def _handle_receipt_detail(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        receipt_id = self._parse_receipt_path_id(path, "/receipts/")
        if receipt_id is None:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "回单不存在。"})
            return
        try:
            detail = self.repository.get_receipt_detail(receipt_id)
        except Exception as exc:
            self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": f"回单详情查询失败：{exc}"})
            return
        if not detail:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "回单不存在。"})
            return
        record = detail.get("record") if isinstance(detail, dict) else None
        if isinstance(record, dict):
            detail = dict(detail)
            detail["record"] = self._enrich_receipt_detail_record(record)
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": detail})

    @staticmethod
    def _parse_receipt_feishu_detail_path_id(path: str) -> int | None:
        raw = str(path or "").strip().rstrip("/")
        prefix = "/receipts/"
        suffix = "/feishu-detail-query"
        if not raw.startswith(prefix) or not raw.endswith(suffix):
            return None
        tail = raw[len(prefix) : -len(suffix)].strip("/")
        if not tail or "/" in tail:
            return None
        try:
            value = int(tail)
        except ValueError:
            return None
        return value if value > 0 else None

    def _handle_receipt_feishu_detail_query(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
    ) -> None:
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        receipt_id = self._parse_receipt_feishu_detail_path_id(path)
        if receipt_id is None:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "message": "回单不存在。"},
            )
            return
        try:
            record = self.repository.get_receipt_record(receipt_id)
        except Exception as exc:
            self._send_json(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "message": f"回单详情查询失败：{exc}"},
            )
            return
        if not isinstance(record, dict):
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "message": "回单不存在。"},
            )
            return
        waybill_no = self._receipt_detail_text(record.get("waybill_no"))
        if not self._receipt_detail_should_query_feishu(record, waybill_no):
            self._send_json(
                handler,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {
                    "ok": False,
                    "error_code": "FEISHU_RECEIPT_QUERY_NOT_APPLICABLE",
                    "message": "仅支持按韵达运单号提交飞书回单详情精确查询。",
                },
            )
            return
        command_result = self._submit_console_tool_command(
            trusted_context=trusted_context,
            browser_request_uuid=str(
                handler.headers.get("X-Browser-Request-UUID") or ""
            ),
            tool_name="query_receipt_feishu_detail",
            arguments={"waybill_no": waybill_no},
            entity_refs=[
                {
                    "entity_type": "receipt",
                    "entity_id": str(receipt_id),
                    "source_system": "yunda",
                    "relation_type": "subject",
                    "metadata": {},
                },
                {
                    "entity_type": "waybill",
                    "entity_id": waybill_no,
                    "source_system": "yunda",
                    "relation_type": "related",
                    "metadata": {},
                },
            ],
            console_entry=f"/receipts/{receipt_id}/feishu-detail-query",
        )
        self._send_console_command_receipt(
            handler,
            command_result,
            message="飞书回单详情精确查询已提交，请在事项中心查看运行证据。",
        )

    def _parse_receipt_audit_path_id(self, path: str) -> int | None:
        raw = str(path or "").strip().rstrip("/")
        prefix = "/receipts/"
        suffix = "/audit"
        if not raw.startswith(prefix) or not raw.endswith(suffix):
            return None
        tail = raw[len(prefix) : -len(suffix)].strip("/")
        if not tail or "/" in tail:
            return None
        try:
            return int(tail)
        except ValueError:
            return None

    def _parse_receipt_audit_body(self, handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        raw_body = self._read_request_body(handler)
        if not raw_body:
            return {}
        content_type = str(handler.headers.get("Content-Type") or "").lower()
        if "json" in content_type:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                return {}
            return payload if isinstance(payload, dict) else {}
        parsed = parse_qs(raw_body.decode("utf-8", errors="replace"), keep_blank_values=True)
        return {str(key): str(values[-1] if values else "") for key, values in parsed.items()}

    def _handle_receipt_audit(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        receipt_id = self._parse_receipt_audit_path_id(path)
        if receipt_id is None:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "回单不存在。"})
            return
        body = self._parse_receipt_audit_body(handler)
        result_value = str(body.get("result") or "").strip().lower()
        if result_value not in {"passed", "failed"}:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "审核结果必须是 passed 或 failed。"})
            return
        reason = str(body.get("reason") or "").strip()
        try:
            record = self.repository.get_receipt_record(receipt_id)
        except Exception as exc:
            self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": f"回单详情查询失败：{exc}"})
            return
        if not record:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "回单不存在。"})
            return

        operator = str((getattr(handler, "current_admin_user", None) or {}).get("username") or "")
        params = {
            "receipt_id": receipt_id,
            "platform": str(record.get("platform") or "").strip(),
            "direction": str(record.get("direction") or "").strip(),
            "result": result_value,
            "reason": reason,
            "waybill_no": str(record.get("waybill_no") or "").strip(),
            "receipt_no": str(record.get("receipt_no") or "").strip(),
            "return_waybill_no": str(record.get("return_waybill_no") or "").strip(),
        }
        audit_log_request = {
            key: params[key]
            for key in (
                "receipt_id",
                "platform",
                "direction",
                "result",
                "reason",
                "waybill_no",
                "receipt_no",
                "return_waybill_no",
            )
        }
        entity_refs = [
            {
                "entity_type": "receipt",
                "entity_id": str(receipt_id),
                "source_system": params["platform"],
                "relation_type": "subject",
                "metadata": {},
            },
            {
                "entity_type": "waybill",
                "entity_id": params["waybill_no"],
                "source_system": params["platform"],
                "relation_type": "related",
                "metadata": {},
            },
        ]
        command_result = self._submit_console_tool_command(
            trusted_context=trusted_context,
            browser_request_uuid=str(
                handler.headers.get("X-Browser-Request-UUID") or ""
            ),
            tool_name="receipts_audit",
            arguments=params,
            entity_refs=entity_refs,
            console_entry=f"/receipts/{receipt_id}/audit",
        )
        receipt = command_result.get("data") if isinstance(command_result.get("data"), dict) else {}
        self.repository.record_receipt_audit_log(
            receipt_id=receipt_id,
            platform=params["platform"],
            direction=params["direction"],
            action="audit_submit",
            result_status="submitted" if command_result.get("ok") else "failed",
            operator=operator,
            request_summary=audit_log_request,
            response_status=str(
                receipt.get("status")
                or command_result.get("error_code")
                or command_result.get("status")
                or ""
            ),
            message=str(
                receipt.get("run_id")
                or command_result.get("error")
                or "智能服务任务提交失败"
            ),
        )
        self._send_console_command_receipt(
            handler,
            command_result,
            message="回单审核计划已提交，请在事项中心完成审批并查看执行结果。",
        )
        return

    def _handle_receipt_attachment(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        query: dict[str, list[str]] | None = None,
    ) -> None:
        attachment_id = self._parse_receipt_path_id(path, "/receipts/attachments/")
        if attachment_id is None:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "附件不存在。"})
            return
        try:
            attachment = self.repository.get_receipt_attachment(attachment_id)
        except Exception as exc:
            self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": f"附件查询失败：{exc}"})
            return
        if not attachment:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "附件不存在。"})
            return
        target = self._resolve_receipt_attachment_target(attachment)
        if not target:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "message": "本地缓存缺失。", "source_url": str(attachment.get("source_url") or "")},
            )
            return
        with target.open("rb") as handle:
            payload = handle.read()
        content_type = self._receipt_image_mime_type(payload)
        if not content_type:
            self._send_json(
                handler,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"ok": False, "message": "附件不是受支持的图片格式。"},
            )
            return
        extra_headers = {
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        }
        if self._receipt_download_requested(query):
            filename = self._receipt_attachment_download_filename(attachment, target, content_type or "")
            extra_headers["Content-Disposition"] = self._content_disposition_attachment(filename)
        self._send_bytes(
            handler,
            HTTPStatus.OK,
            payload,
            content_type or "application/octet-stream",
            extra_headers=extra_headers,
        )

    def _handle_receipts_image_archive(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        if not self._receipt_query_requested(query):
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请先查询回单列表后再下载图片。"})
            return
        filters = self._receipt_filters_from_query(query)
        try:
            attachments = self.repository.list_receipt_image_attachments_for_filters(filters)
        except Exception as exc:
            self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": f"回单图片查询失败：{exc}"})
            return
        if not attachments:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "当前查询列表没有可下载的回单图片。"})
            return
        archive_buffer = io.BytesIO()
        used_names: set[str] = set()
        added = 0
        with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for attachment in attachments:
                target = self._resolve_receipt_attachment_target(attachment)
                if not target:
                    continue
                with target.open("rb") as handle:
                    content_type = self._receipt_image_mime_type(handle.read(16))
                if not content_type:
                    continue
                base_name = self._receipt_archive_entry_base_name(attachment)
                suffix = self._receipt_archive_entry_suffix(attachment, target, content_type or "")
                archive_name = self._unique_receipt_archive_entry_name(base_name, suffix, used_names)
                archive.write(target, archive_name)
                added += 1
        if added <= 0:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "当前查询列表没有可下载的本地回单图片。"})
            return
        filename = self._receipt_archive_filename(filters)
        payload = archive_buffer.getvalue()
        self._send_bytes(
            handler,
            HTTPStatus.OK,
            payload,
            "application/zip",
            extra_headers={"Content-Disposition": self._content_disposition_attachment(filename)},
        )

    def _resolve_receipt_attachment_target(self, attachment: dict[str, Any]) -> Path | None:
        runtime_root = self.settings.runtime_dir.resolve()

        def resolve_local_path(local_path: str) -> Path | None:
            candidate = Path(str(local_path or "").replace("\\", "/"))
            if not str(candidate):
                return None
            target = candidate.resolve() if candidate.is_absolute() else (runtime_root / candidate).resolve()
            try:
                target.relative_to(runtime_root)
            except ValueError:
                return None
            return target if target.exists() and target.is_file() else None

        target = resolve_local_path(str(attachment.get("local_path") or "").strip())
        if target and self._is_receipt_attachment_image_file(target):
            return target
        local_path = self._cache_receipt_attachment_from_source(attachment)
        if not local_path:
            return None
        return resolve_local_path(local_path)

    def _receipt_archive_filename(self, filters: dict[str, Any]) -> str:
        date_from = str(filters.get("date_from") or "").strip()
        date_to = str(filters.get("date_to") or "").strip()
        if date_from and date_to:
            return f"receipt-images-{date_from}-{date_to}.zip"
        return "receipt-images.zip"

    def _receipt_archive_entry_base_name(self, attachment: dict[str, Any]) -> str:
        waybill_no = self._safe_receipt_archive_name_part(
            attachment.get("waybill_no") or attachment.get("receipt_no") or attachment.get("id") or "receipt"
        )
        remark_suffix = self._receipt_remark_suffix(attachment.get("receipt_raw_payload"))
        return f"{waybill_no}-{remark_suffix}" if remark_suffix else waybill_no

    def _receipt_archive_entry_suffix(self, attachment: dict[str, Any], target: Path, content_type: str) -> str:
        candidates = [
            target.suffix,
            Path(str(attachment.get("display_name") or "").replace("\\", "/")).suffix,
            Path(urlparse(str(attachment.get("source_url") or "")).path).suffix,
            mimetypes.guess_extension(str(content_type or "").split(";", 1)[0].strip()),
        ]
        for suffix in candidates:
            suffix_text = str(suffix or "").strip().lower()
            if suffix_text in {".gif", ".jpeg", ".jpg", ".png", ".webp"}:
                return suffix_text
        return ".bin"

    def _unique_receipt_archive_entry_name(self, base_name: str, suffix: str, used_names: set[str]) -> str:
        cleaned_base = self._safe_receipt_archive_name_part(base_name)
        cleaned_suffix = str(suffix or "").strip()
        if cleaned_suffix and not cleaned_suffix.startswith("."):
            cleaned_suffix = f".{cleaned_suffix}"
        candidate = f"{cleaned_base}{cleaned_suffix or '.bin'}"
        index = 2
        while candidate in used_names:
            candidate = f"{cleaned_base}-{index}{cleaned_suffix or '.bin'}"
            index += 1
        used_names.add(candidate)
        return candidate

    def _safe_receipt_archive_name_part(self, value: Any) -> str:
        cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "").strip())
        return cleaned.strip("._-") or "receipt"

    def _receipt_remark_suffix(self, raw_payload: Any) -> str:
        for value in self._receipt_remark_values(raw_payload):
            match = RECEIPT_REMARK_TOKEN_RE.search(value)
            if match:
                return f"{match.group(1)[-3:]}-{match.group(2)}"
        return ""

    def _receipt_remark_values(self, raw_payload: Any) -> list[str]:
        values: list[str] = []

        def collect_scalars(item: Any) -> None:
            if isinstance(item, (str, int, float)):
                values.append(str(item))
                return
            if isinstance(item, dict):
                for value in item.values():
                    collect_scalars(value)
                return
            if isinstance(item, (list, tuple)):
                for value in item:
                    collect_scalars(value)

        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for key, value in item.items():
                    key_text = str(key or "")
                    key_lower = key_text.lower()
                    is_remark_key = any(fragment in key_lower or fragment in key_text for fragment in RECEIPT_REMARK_KEY_FRAGMENTS)
                    if is_remark_key:
                        collect_scalars(value)
                    elif isinstance(value, (dict, list, tuple)):
                        walk(value)
                return
            if isinstance(item, (list, tuple)):
                for value in item:
                    walk(value)

        walk(raw_payload)
        return values

    def _receipt_download_requested(self, query: dict[str, list[str]] | None) -> bool:
        raw = str((query or {}).get("download", [""])[0] or "").strip().lower()
        return raw in {"1", "true", "yes", "download"}

    def _receipt_attachment_download_filename(
        self,
        attachment: dict[str, Any],
        target: Path,
        content_type: str,
    ) -> str:
        attachment_id = int(attachment.get("id") or 0)
        display_name = str(attachment.get("display_name") or "").strip()
        base_name = Path(display_name.replace("\\", "/")).name.strip() if display_name else ""
        fallback = f"receipt-{attachment_id or 'attachment'}"
        if not base_name:
            base_name = fallback
        suffix = Path(base_name).suffix
        if not suffix:
            suffix = target.suffix or mimetypes.guess_extension(content_type or "") or ""
            if suffix:
                base_name = f"{base_name}{suffix}"
        return base_name

    def _content_disposition_attachment(self, filename: str) -> str:
        cleaned = "".join("_" if ord(ch) < 32 or ch in {'"', "\\", "/", ":"} else ch for ch in str(filename or "receipt"))
        cleaned = cleaned.strip().strip(".") or "receipt"
        ascii_name = "".join(ch if 32 <= ord(ch) < 127 else "_" for ch in cleaned).strip() or "receipt"
        return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(cleaned.encode('utf-8'))}"

    def _cache_receipt_attachment_from_source(self, attachment: dict[str, Any]) -> str:
        source_url = self._normalize_receipt_attachment_source_url(attachment, str(attachment.get("source_url") or ""))
        if not source_url:
            return ""
        fetched = self._fetch_receipt_attachment_source(attachment, source_url)
        if not fetched:
            return ""
        payload, _upstream_content_type = fetched
        content_type = self._receipt_image_mime_type(payload)
        if not content_type:
            return ""
        digest = hashlib.sha256(payload).hexdigest()
        attachment_id = int(attachment.get("id") or 0)
        platform = str(attachment.get("platform") or "unknown").strip().lower() or "unknown"
        record_id = int(attachment.get("record_id") or 0)
        url_suffix = Path(urlparse(source_url).path).suffix.lower()
        ext = url_suffix if url_suffix in RECEIPT_IMAGE_SUFFIXES else ""
        if not ext:
            guessed = mimetypes.guess_extension(str(content_type or "").split(";", 1)[0].strip())
            ext = guessed if guessed in RECEIPT_IMAGE_SUFFIXES else ".bin"
        relative_path = Path("receipts") / platform / str(record_id or "unknown") / f"{attachment_id}_{digest[:12]}{ext}"
        target = (self.settings.runtime_dir / relative_path).resolve()
        runtime_root = self.settings.runtime_dir.resolve()
        try:
            target.relative_to(runtime_root)
        except ValueError:
            return ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        relpath = str(relative_path).replace("\\", "/")
        try:
            self.repository.update_receipt_attachment_cache(
                attachment_id,
                local_path=relpath,
                file_hash=digest,
                mime_type=content_type,
                file_size=len(payload),
            )
        except Exception:
            pass
        attachment["local_path"] = relpath
        attachment["file_hash"] = digest
        attachment["mime_type"] = content_type
        attachment["file_size"] = len(payload)
        return relpath

    @staticmethod
    def _receipt_image_mime_type(payload: bytes) -> str:
        if payload.startswith(b"\xff\xd8"):
            return "image/jpeg"
        if payload.startswith(b"\x89PNG"):
            return "image/png"
        if payload.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
            return "image/webp"
        return ""

    @classmethod
    def _looks_like_receipt_image(cls, payload: bytes) -> bool:
        return bool(cls._receipt_image_mime_type(payload))

    def _is_receipt_attachment_image_file(self, target: Path) -> bool:
        try:
            with target.open("rb") as handle:
                return self._looks_like_receipt_image(handle.read(16))
        except OSError:
            return False

    def _normalize_receipt_attachment_source_url(self, attachment: dict[str, Any], source_url: str) -> str:
        raw = html.unescape(str(source_url or "").strip()).replace("\\", "/")
        if not raw:
            return ""
        if raw.startswith("//"):
            raw = f"https:{raw}"
        platform = str(attachment.get("platform") or "").strip().lower()
        parsed = urlparse(raw)
        if platform == "ronghui":
            host_path = raw.lstrip("/")
            if any(host_path.lower().startswith(f"{host}/") for host in RONGHUI_DIRECT_ATTACHMENT_HOSTS):
                raw = f"https://{host_path}"
                parsed = urlparse(raw)
            elif "ronghuiwl.com" in str(parsed.netloc).lower():
                embedded_host_path = (parsed.path or "").lstrip("/")
                if any(embedded_host_path.lower().startswith(f"{host}/") for host in RONGHUI_DIRECT_ATTACHMENT_HOSTS):
                    raw = f"https://{embedded_host_path}"
                    if parsed.query:
                        raw = f"{raw}?{parsed.query}"
                    parsed = urlparse(raw)
        host = str(parsed.hostname or "").strip().lower()
        if host in RONGHUI_DIRECT_ATTACHMENT_HOSTS and parsed.scheme.lower() != "https":
            raw = parsed._replace(scheme="https").geturl()
        return raw

    def _fetch_direct_receipt_attachment(self, source_url: str) -> tuple[bytes, str] | None:
        parsed = urlparse(source_url)
        host = str(parsed.hostname or "").strip().lower()
        if parsed.scheme.lower() != "https" or host not in RONGHUI_DIRECT_ATTACHMENT_HOSTS:
            return None
        suffix = Path(parsed.path or "").suffix.lower()
        if suffix not in RECEIPT_IMAGE_SUFFIXES:
            return None
        request = Request(source_url, headers={"Accept": "image/*,*/*", "User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(request, timeout=180) as response:
                status_code = int(getattr(response, "status", 200) or 200)
                if status_code < 200 or status_code >= 300:
                    return None
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if content_type and not content_type.startswith("image/"):
                    return None
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > RECEIPT_ATTACHMENT_MAX_BYTES:
                        return None
                    chunks.append(chunk)
        except (HTTPError, URLError, OSError, TimeoutError, ValueError):
            return None
        payload = b"".join(chunks)
        if not self._looks_like_receipt_image(payload):
            return None
        safe_content_type = self._receipt_image_mime_type(payload)
        return (payload, safe_content_type) if safe_content_type else None

    def _fetch_receipt_attachment_source(self, attachment: dict[str, Any], source_url: str) -> tuple[bytes, str] | None:
        source_url = self._normalize_receipt_attachment_source_url(attachment, source_url)
        if not source_url:
            return None
        direct_attachment = self._fetch_direct_receipt_attachment(source_url)
        if direct_attachment:
            return direct_attachment
        # Authenticated third-party page fetches previously reused the active
        # original-page proxy.  That surface is disabled until it can run on an
        # origin isolated from the Console administrator session.  Public,
        # allow-listed image CDN URLs above remain available.
        return None

    def _handle_receipts_sync(self, handler: BaseHTTPRequestHandler) -> None:
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        raw_body = self._read_request_body(handler)
        content_type = str(handler.headers.get("Content-Type") or "").lower()
        body: dict[str, Any] = {}
        if raw_body and "json" in content_type:
            try:
                parsed_body = json.loads(raw_body.decode("utf-8"))
                body = parsed_body if isinstance(parsed_body, dict) else {}
            except json.JSONDecodeError:
                body = {}
        elif raw_body:
            parsed = parse_qs(raw_body.decode("utf-8", errors="replace"), keep_blank_values=True)
            body = {str(key): str(values[-1] if values else "") for key, values in parsed.items()}
        safe_params = {
            "platform": str(body.get("platform", "") or "all").strip().lower()
            or "all",
            "direction": "send",
            "date_from": str(body.get("date_from", "") or "").strip(),
            "date_to": str(body.get("date_to", "") or "").strip(),
            "q": str(body.get("q", "") or "").strip(),
            "receipt_status": str(body.get("receipt_status", "") or "").strip(),
        }
        for optional_name in ("date_type", "code_type"):
            value = str(body.get(optional_name, "") or "").strip()
            if value:
                safe_params[optional_name] = value
        if safe_params["platform"] not in {"all", "ronghui", "yunda"}:
            self._send_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "message": "回单平台参数无效。"},
            )
            return
        has_date_from = bool(safe_params["date_from"])
        has_date_to = bool(safe_params["date_to"])
        if has_date_from != has_date_to:
            self._send_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "message": "请选择完整的更新时间范围：开始日期和结束日期必须同时填写。"},
            )
            return
        if not has_date_from and not has_date_to:
            today = self._receipt_default_date()
            safe_params["date_from"] = today
            safe_params["date_to"] = today
        for date_name in ("date_from", "date_to"):
            value = safe_params[date_name]
            if not value:
                continue
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                self._send_json(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "message": "更新时间范围格式无效，请使用 YYYY-MM-DD。"},
                )
                return
        safe_params["max_pages"] = RECEIPT_QUERY_MAX_PAGES
        safe_params["timeout_sec"] = RECEIPT_QUERY_SOURCE_TIMEOUT_SEC
        command_result = self._submit_console_tool_command(
            trusted_context=trusted_context,
            browser_request_uuid=str(
                handler.headers.get("X-Browser-Request-UUID") or ""
            ),
            tool_name="receipts_sync",
            arguments=safe_params,
            entity_refs=[],
            console_entry="/receipts/sync",
        )
        operator = str(
            (getattr(handler, "current_admin_user", None) or {}).get("username")
            or ""
        )
        receipt = (
            command_result.get("data")
            if isinstance(command_result.get("data"), dict)
            else {}
        )
        self.repository.record_receipt_audit_log(
            action="sync_submit",
            result_status="submitted" if command_result.get("ok") else "failed",
            operator=operator,
            request_summary=safe_params,
            response_status=str(
                receipt.get("status")
                or command_result.get("error_code")
                or command_result.get("status")
                or ""
            ),
            message=str(
                receipt.get("run_id")
                or command_result.get("error")
                or "智能服务任务提交失败"
            ),
        )
        self._send_console_command_receipt(
            handler,
            command_result,
            message="回单同步计划已提交，请在事项中心完成审批并查看运行结果。",
        )

    def _render_line_haul_contacts(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        def first_value(name: str, default: str = "") -> str:
            return str(query.get(name, [default])[0] or "").strip()

        def positive_int(name: str, default: int) -> int:
            try:
                return max(int(first_value(name, str(default))), 1)
            except ValueError:
                return default

        filters = {
            "q": first_value("q"),
        }
        page = positive_int("page", 1)
        page_size = min(max(positive_int("page_size", 50), 20), 100)
        has_search = bool(filters["q"])
        db_error = ""

        def empty_result() -> dict[str, Any]:
            return {
                "rows": [],
                "summary": {
                    "total": 0,
                    "active_count": 0,
                    "inactive_count": 0,
                },
                "pagination": {
                    "page": 1,
                    "page_size": page_size,
                    "total": 0,
                    "total_pages": 1,
                    "offset": 0,
                    "has_prev": False,
                    "has_next": False,
                },
            }

        if has_search:
            try:
                result = self.repository.search_line_haul_contacts_page(filters, page=page, page_size=page_size)
            except Exception as exc:
                result = empty_result()
                db_error = str(exc)
        else:
            result = empty_result()

        pagination = result["pagination"]
        base_query = {
            **{k: v for k, v in filters.items() if str(v)},
            "page_size": str(pagination["page_size"]),
        }
        current_query = dict(base_query)
        if has_search:
            current_query["page"] = str(pagination["page"])
        return_to = "/line-haul-contacts"
        if current_query:
            return_to += "?" + urlencode(current_query)
        prev_url = ""
        next_url = ""
        if pagination["has_prev"]:
            prev_url = "/line-haul-contacts?" + urlencode({**base_query, "page": pagination["page"] - 1})
        if pagination["has_next"]:
            next_url = "/line-haul-contacts?" + urlencode({**base_query, "page": pagination["page"] + 1})

        display_rows = self._line_haul_contact_display_rows(result["rows"])
        template = self.template_env.get_template("line_haul_contacts.html")
        body = template.render(
            app_title=self.settings.app_title,
            filters=filters,
            rows=display_rows,
            summary=result["summary"],
            pagination=pagination,
            prev_url=prev_url,
            next_url=next_url,
            return_to=return_to,
            has_search=has_search,
            db_error=db_error,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    @staticmethod
    def _line_haul_contact_display_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        display_rows: list[dict[str, Any]] = []
        index = 0
        while index < len(rows):
            company_name = str(rows[index].get("company_name", "") or "")
            group_end = index + 1
            while (
                group_end < len(rows)
                and str(rows[group_end].get("company_name", "") or "") == company_name
            ):
                group_end += 1
            group_size = group_end - index
            for row_index in range(index, group_end):
                display_row = dict(rows[row_index])
                display_row["show_company"] = row_index == index
                display_row["company_rowspan"] = group_size if row_index == index else 0
                display_rows.append(display_row)
            index = group_end
        return display_rows

    def _render_waybill_print(self, handler: BaseHTTPRequestHandler, waybill_id: int, query: dict) -> None:
        waybill = self.repository.get_waybill(waybill_id)
        if not waybill:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Waybill not found.")
            return
        autoprint = str(query.get("autoprint", [""])[0]).lower() in {"1", "true", "yes"}
        print_preview = str(query.get("preview", [""])[0]).lower() in {"1", "true", "yes"}
        template = self.template_env.get_template("waybill_print.html")
        body = template.render(
            app_title=self.settings.app_title,
            waybill=waybill,
            autoprint=autoprint,
            print_preview=print_preview,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    @staticmethod
    def _first_tracking_text(row: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = row.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @classmethod
    def _normalize_tracking_payload(cls, data: dict[str, Any]) -> dict[str, Any]:
        return data

    def _handle_tracking_query(self, handler: BaseHTTPRequestHandler) -> None:
        content_length = int(handler.headers.get("Content-Length", 0))
        raw = handler.rfile.read(content_length) if content_length else b""
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        tracking_number = str(body.get("tracking_number", "")).strip()
        if not tracking_number:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "请输入运单号"})
            return

        result = self._agent_request(
            "POST",
            "/internal/v1/tms/tracking_query",
            payload={
                "params": {"tracking_number": tracking_number, "decrypt_masked": True},
                "timeout_sec": 180,
            },
            timeout=max(195, self.settings.agent_timeout_seconds),
        )
        if not result.get("ok"):
            error = result.get("error")
            message = "单号查询服务暂时不可用，请稍后重试。"
            if isinstance(error, dict):
                message = str(error.get("error") or error.get("message") or message)
            elif error:
                message = str(error)
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"error": message})
            return

        data = result.get("data")
        if not isinstance(data, dict):
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"error": "单号查询服务返回格式异常。"})
            return
        if data.get("ok") is False and data.get("error"):
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"error": str(data.get("error") or data.get("message") or "单号查询失败。")},
            )
            return
        if isinstance(data.get("data"), dict) and data.get("type") is None:
            data = data["data"]
        if data.get("ok") is False or data.get("error"):
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"error": str(data.get("error") or data.get("message") or "单号查询失败。")},
            )
            return
        data = self._normalize_tracking_payload(data)
        self._send_json(handler, HTTPStatus.OK, data)
