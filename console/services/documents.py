"""Console application services grouped by business responsibility."""

import gzip
from email.utils import formatdate

from console.app_support import *  # noqa: F403


def _accepts_content_encoding(header_value: str, encoding: str) -> bool:
    explicit_quality: float | None = None
    wildcard_quality: float | None = None
    for item in header_value.lower().split(","):
        parts = [part.strip() for part in item.split(";") if part.strip()]
        if not parts:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            name, separator, value = parameter.partition("=")
            if name.strip() != "q" or not separator:
                continue
            try:
                quality = float(value.strip())
            except ValueError:
                quality = 0.0
        if parts[0] == encoding:
            explicit_quality = quality
        elif parts[0] == "*":
            wildcard_quality = quality
    selected_quality = explicit_quality if explicit_quality is not None else wildcard_quality
    return selected_quality is not None and selected_quality > 0


class DocumentServiceMixin:
    def _render_document(self, handler: BaseHTTPRequestHandler, document_id: int | None, query: dict) -> None:
        document = None
        if document_id is not None:
            document = self.repository.get_document(document_id)
            if not document:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
                return

        counts = self.repository.count_by_status()
        pending_docs = self.repository.list_documents_by_status(["review_required", "processing", "queued", "error"])
        if document is not None:
            pending_docs = self._pin_document_to_top(pending_docs, document["id"])
        mode_value = query.get("mode", [""])[0].strip().lower()
        boyi_frame_mode = document is None and str(query.get("boyi_frame", [""])[0]).strip().lower() in {"1", "true", "yes"}
        ocr_mode = document is None and mode_value == "ocr"
        yunda_mode = document is None and mode_value == "yunda"
        ronghui_mode = document is None and mode_value == "ronghui"
        active_template_name = self.template_store.get_active_template_name()
        template_spec = self._get_template_spec_for_document(document)
        manual_amap_config = {
            "amap_js_key": self.settings.amap_api_key or "YOUR_AMAP_JS_API_KEY",
            "amap_security_code": self.settings.amap_security_code or "",
        }
        manual_amap_sdk_should_load = not manual_amap_config["amap_js_key"].startswith("YOUR_")
        manual_preview_waybill_no = ""
        if document is None:
            try:
                manual_preview_waybill_no = self.repository.peek_next_manual_waybill_no()
            except Exception:
                manual_preview_waybill_no = ""

        fields = []
        preprocess_info = {}
        preprocess_quality = {"blocking_messages": [], "warning_messages": []}

        if document:
            preprocess_info = document["raw_ocr"].get("preprocess", {})
            preprocess_quality = dict(preprocess_info.get("quality", {}))
            preprocess_quality["blocking_messages"] = quality_issue_messages(
                preprocess_quality.get("blocking_issues", [])
            )
            preprocess_quality["warning_messages"] = []
            normalized_fields = self.service.coerce_fields(document["fields"], template_spec)

            for spec in template_spec["fields"]:
                entry = normalized_fields.get(spec["name"], {})
                fields.append(
                    {
                        "name": spec["name"],
                        "label": spec["label"],
                        "required": spec.get("required", False),
                        "hint": spec.get("hint", ""),
                        "value": entry.get("value", ""),
                        "confidence": entry.get("confidence", 0.0),
                        "source": entry.get("source", ""),
                        "message": entry.get("message", ""),
                    }
                )

        template = self.template_env.get_template("document.html")
        body = template.render(
            app_title=self.settings.app_title,
            document=document,
            fields=fields,
            counts=counts,
            pending_docs=pending_docs,
            queue_snapshot=self.task_queue.snapshot(),
            auto_refresh=(document and document["status"] in {"queued", "processing"}) or (
                ocr_mode and bool(counts.get("queued", 0) or counts.get("processing", 0))
            ),
            ocr_mode=ocr_mode,
            yunda_mode=yunda_mode,
            ronghui_mode=ronghui_mode,
            boyi_frame_mode=boyi_frame_mode,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
            original_url=self._runtime_url(document["original_path"]) if document else "",
            processed_url=self._runtime_url(document["processed_path"]) if document else "",
            preprocess_info=preprocess_info,
            preprocess_quality=preprocess_quality,
            raw_ocr=document["raw_ocr"] if document else {},
            available_templates=self.template_store.list_templates(),
            active_template_name=active_template_name,
            document_template_name=document["template_name"] if document else active_template_name,
            settings=self.settings,
            writers=self.repository.list_writers(),
            document_writer_id=document.get("writer_id", "") if document else "",
            manual_amap_config=manual_amap_config,
            manual_amap_sdk_should_load=manual_amap_sdk_should_load,
            manual_preview_waybill_no=manual_preview_waybill_no,
        )
        self._send_html(handler, body)

    def _pin_document_to_top(self, documents: list[dict[str, Any]], document_id: int) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        others: list[dict[str, Any]] = []
        for item in documents:
            if int(item.get("id", 0) or 0) == document_id:
                selected.append(item)
            else:
                others.append(item)
        return selected + others

    def _render_template_editor(
        self,
        handler: BaseHTTPRequestHandler,
        template_name: str | None,
        query: dict,
        *,
        spec_override: dict[str, Any] | None = None,
        template_json_override: str | None = None,
        original_template_name_override: str | None = None,
    ) -> None:
        try:
            if spec_override is not None:
                template_spec = spec_override
                original_template_name = original_template_name_override or template_name or ""
            elif template_name:
                template_spec = self.template_store.get_template_spec(template_name)
                original_template_name = template_name
            else:
                copy_from = query.get("copy_from", [""])[0].strip() or self.template_store.get_active_template_name()
                template_spec = self.template_store.build_new_template_spec(copy_from)
                original_template_name = ""
        except FileNotFoundError:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Template not found.")
            return

        template = self.template_env.get_template("template_editor.html")
        body = template.render(
            app_title=self.settings.app_title,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
            available_templates=self.template_store.list_templates(),
            active_template_name=self.template_store.get_active_template_name(),
            is_new=not bool(original_template_name),
            original_template_name=original_template_name,
            template_name_value=str(template_spec.get("template_name", "") or ""),
            description_value=str(template_spec.get("description", "") or ""),
            template_json=template_json_override if template_json_override is not None else json.dumps(template_spec, ensure_ascii=False, indent=2),
        )
        self._send_html(handler, body)

    def _build_project_modules(self) -> dict[str, ProjectModule]:
        return {
            "ocr": ProjectModule(
                slug="ocr",
                name="运单录入",
                status="ready",
                summary="支持手工录单与 OCR 图文复核，确认后写回数据库。",
                code_path="console/",
                docs_path="docs/ocr/",
                route="/modules/ocr",
                workspace_path="/ocr",
                current_focus="手工录单、批量 OCR、人工复核流转与 MySQL 回写。",
                inputs=("手工表单", "运单图片", "Qwen OCR API", "人工复核"),
                outputs=("结构化字段", "归档原图", "预处理图片", "数据库记录"),
                dependencies=(),
                consumers=("finance", "ai-service", "customer-service"),
                commands=("cd /home/deng/projects/console && ./start_backend.sh",),
            ),
            "pricing": ProjectModule(
                slug="pricing",
                name="价格获取",
                status="maintained",
                summary="基于地址库和荣辉 TMS 生成报价资产与成本底表。",
                code_path="price_scripts/",
                docs_path="docs/price_scripts/",
                route="/modules/pricing",
                workspace_path="",
                current_focus="地址标准化、TMS 批量取价与报价单产出。",
                inputs=("地址数据库", "TMS 登录态", "网点映射规则"),
                outputs=("全国报价表", "客户报价单", "价格图表", "网点报价表"),
                dependencies=(),
                consumers=("finance", "ai-service", "customer-service"),
                commands=(
                    'cd /d C:\\Users\\DENG\\Desktop\\agent\\price_scripts\\scripts\\02_tms_price_fetch && python -u batch_run.py',
                    'cd /d C:\\Users\\DENG\\Desktop\\agent\\price_scripts\\scripts\\03_finance_summary_charts && python 生成客户报价表.py',
                ),
            ),
            "finance": ProjectModule(
                slug="finance",
                name="财务对账",
                status="ready",
                summary="从融辉、韵达真实财务页面同步逐笔交易与汇总，提供费用绑定、BI 和失败审计。",
                code_path="shared/finance/ + agent/tools/ + console/finance_service.py",
                docs_path="agent/docs/finance_module.md",
                route="/modules/finance",
                workspace_path="/modules/finance",
                current_focus="真实页面逐笔采集、共享账本、费用分类、运单财务事实和同步审计。",
                inputs=("融辉财务页", "韵达财务页", "账号登录态", "费用映射", "运单号"),
                outputs=("逐笔账本", "平台汇总", "费用分类", "运单财务事实", "同步审计"),
                dependencies=(),
                consumers=("ai-service", "customer-service"),
                commands=("打开 /modules/finance，通过工作台执行同步、回填或失败重试。",),
            ),
            "customer-service": ProjectModule(
                slug="customer-service",
                name="客服系统",
                status="in-progress",
                summary="集中处理融辉和韵达的问题件实时查询、提醒、详情、回复和发布。",
                code_path="console/",
                docs_path="docs/customer_service/",
                route="/modules/customer-service",
                workspace_path="/modules/customer-service",
                current_focus="第一版只做问题件闭环；差错、调拨件等客服类别后续逐步接入。",
                inputs=("问题件", "差错", "调拨件", "平台工单", "运单状态"),
                outputs=("问题件工作台", "页面提醒", "处理回复", "发布记录", "附件上传"),
                dependencies=("ocr", "pricing", "finance", "dispatch"),
                consumers=(),
                commands=("打开 /modules/customer-service 后选择融辉或韵达业务账号并实时查询。",),
            ),
            "ai-service": ProjectModule(
                slug="ai-service",
                name="AI客服",
                status="planned",
                summary="消费 OCR、报价和财务结果，为客服问答提供统一入口。",
                code_path="agent/ + feishu/",
                docs_path="docs/ai_service/",
                route="/modules/ai-service",
                workspace_path="",
                current_focus="报价问答、查询回复和异常解释编排。",
                inputs=("OCR 字段", "客户报价表", "财务结果", "知识规则"),
                outputs=("客服回复", "报价回答", "异常说明", "工单"),
                dependencies=("ocr", "pricing", "finance"),
                consumers=(),
                commands=("待补实现。",),
            ),
            "dispatch": ProjectModule(
                slug="dispatch",
                name="货拉拉调度",
                status="in-progress",
                summary="管理车队资源，支撑调度计划与轨迹监控。",
                code_path="console/",
                docs_path="docs/dispatch/",
                route="/modules/dispatch",
                workspace_path="/dispatch",
                current_focus="车队主数据、调度面板、线路与监控。",
                inputs=("车辆信息", "运单数据", "线路规则", "司机排班"),
                outputs=("调度单", "车辆轨迹", "运力报表", "预警信息"),
                dependencies=("ocr", "pricing"),
                consumers=("finance", "ai-service", "customer-service"),
                commands=("待补实现。",),
            ),
        }

    def _build_module_view_models(self, counts: dict[str, int]) -> dict[str, dict]:
        pricing_output_dir = PROJECT_ROOT / "浠锋牸鑾峰彇鑴氭湰" / "杈撳嚭缁撴灉"
        pricing_file_count = 0
        if pricing_output_dir.exists():
            pricing_file_count = sum(1 for item in pricing_output_dir.rglob("*") if item.is_file())
        ai_dir = PROJECT_ROOT / "agent"
        ai_file_count = 0
        if ai_dir.exists():
            ai_file_count = sum(1 for item in ai_dir.rglob("*") if item.is_file())
        dispatch_dir = MODULE_DIR
        dispatch_file_count = 0
        if dispatch_dir.exists():
            dispatch_file_count = sum(1 for item in dispatch_dir.rglob("*") if item.is_file())
        total_documents = sum(counts.values())

        metrics = {
            "ocr": {
                "metric_label": "记录数",
                "metric_value": f"{total_documents} 条",
                "highlights": [
                    f"排队中 {counts.get('queued', 0)}",
                    f"处理中 {counts.get('processing', 0)}",
                    f"待复核 {counts.get('review_required', 0)}",
                    f"已确认 {counts.get('confirmed', 0)}",
                ],
                "workspace_label": "进入运单录入",
            },
            "pricing": {
                "metric_label": "产出文件",
                "metric_value": f"{pricing_file_count} 个",
                "highlights": [
                    "地址库 -> TMS 取价 -> 客户报价表",
                    "支撑客服报价与财务成本底表",
                    "作为价格资产层持续维护",
                ],
                "workspace_label": "查看价格模块",
            },
            "finance": {
                "metric_label": "数据架构",
                "metric_value": "在线账本",
                "highlights": [
                    "融辉 / 韵达真实页面逐笔采集",
                    "共享 MySQL 账本与版本化费用映射",
                    "BI、运单事实与同步失败审计",
                ],
                "workspace_label": "查看财务模块",
            },
            "customer-service": {
                "metric_label": "接入状态",
                "metric_value": "待接入",
                "highlights": [
                    "问题件、差错、调拨件集中入口",
                    "后续按平台来源逐步接入处理流程",
                    "当前不读取真实工单或第三方接口",
                ],
                "workspace_label": "查看客服系统",
            },
            "ai-service": {
                "metric_label": "文件数",
                "metric_value": f"{ai_file_count} 个",
                "highlights": [
                    "消费 OCR、报价和财务结果",
                    "处理报价问答与订单查询",
                    "当前以规划和接口对接为主",
                ],
                "workspace_label": "查看 AI 客服规划",
            },
            "dispatch": {
                "metric_label": "文件数",
                "metric_value": f"{dispatch_file_count} 个",
                "highlights": [
                    "车队资源监控",
                    "调度与线路规划",
                    "运力分配与预警",
                ],
                "workspace_label": "进入调度中心",
            },
        }

        modules: dict[str, dict] = {}
        for slug, module in self.project_modules.items():
            data = metrics[slug]
            modules[slug] = {
                "slug": module.slug,
                "name": module.name,
                "status": module.status,
                "summary": module.summary,
                "code_path": module.code_path,
                "docs_path": module.docs_path,
                "route": module.route,
                "workspace_path": module.workspace_path,
                "current_focus": module.current_focus,
                "inputs": list(module.inputs),
                "outputs": list(module.outputs),
                "dependencies": list(module.dependencies),
                "consumers": list(module.consumers),
                "commands": list(module.commands),
                "metric_label": data["metric_label"],
                "metric_value": data["metric_value"],
                "highlights": data["highlights"],
                "workspace_label": data["workspace_label"],
            }
        return modules

    def _build_relationship_cards(self) -> list[dict[str, object]]:
        return [
            {
                "title": "OCR 入库",
                "description": "运单图片先进入 OCR，再经过排队、识别和人工复核。",
                "inputs": ["图片目录", "Qwen OCR API", "复核规则"],
                "outputs": ["结构化字段", "归档图片"],
            },
            {
                "title": "报价资产",
                "description": "价格模块基于地址数据和荣辉 TMS 生成标准报价表。",
                "inputs": ["地址库", "TMS 登录态", "网点映射"],
                "outputs": ["全国报价表", "客户报价单", "价格图表"],
            },
            {
                "title": "财务对账",
                "description": "财务模块从融辉、韵达真实页面采集逐笔交易，经确定性校验后写入共享账本。",
                "inputs": ["原始财务页面", "账号登录态", "费用映射", "运单号"],
                "outputs": ["逐笔账本", "费用分类", "运单财务事实", "同步审计"],
            },
            {
                "title": "客服系统",
                "description": "客服系统集中承接问题件、差错和调拨件，后续按平台逐步接入真实处理链路。",
                "inputs": ["问题件", "差错", "调拨件", "平台工单"],
                "outputs": ["处理记录", "责任状态", "协同备注"],
            },
            {
                "title": "调度作业",
                "description": "调度模块使用业务数据监控车队运力和线路执行情况。",
                "inputs": ["运单数据", "价格底表", "车队数据", "司机排班"],
                "outputs": ["调度单", "车辆轨迹", "运力报表"],
            },
            {
                "title": "AI 客服编排",
                "description": "AI 客服模块消费 OCR、报价和财务结果，对外提供问答能力。",
                "inputs": ["OCR 字段", "报价表", "财务差异信息"],
                "outputs": ["客服回复", "异常说明", "工单"],
            },
        ]

    def _get_template_spec_for_document(self, document: dict[str, Any] | None) -> dict[str, Any]:
        template_name = ""
        if document:
            template_name = str(document.get("template_name", "") or "").strip()
        try:
            return self.template_store.get_template_spec(template_name)
        except FileNotFoundError:
            return self.template_store.get_active_template_spec()

    def _safe_return_to(self, value: str, fallback: str = "/ocr") -> str:
        candidate = (value or "").strip()
        if candidate.startswith("/") and not candidate.startswith("//") and "\r" not in candidate and "\n" not in candidate:
            return candidate
        return fallback

    def _validate_template_spec(self, spec: dict[str, Any]) -> str | None:
        if not isinstance(spec, dict):
            return "Template JSON must be an object."
        if not isinstance(spec.get("preprocess"), dict):
            return "Template JSON must include a preprocess object."
        fields = spec.get("fields")
        if not isinstance(fields, list):
            return "Template JSON must include a fields array."
        for index, field in enumerate(fields, start=1):
            if not isinstance(field, dict):
                return f"fields[{index}] must be an object."
            if not str(field.get("name", "") or "").strip():
                return f"fields[{index}] is missing name."
            if not str(field.get("label", "") or "").strip():
                return f"fields[{index}] is missing label."
        return None

    def _handle_template_select(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        template_name = values.get("template_name", "").strip()
        return_to = self._safe_return_to(values.get("return_to", ""), "/ocr")
        try:
            self.template_store.set_active_template_name(template_name)
        except FileNotFoundError:
            self._redirect_with_message(handler, return_to, "Template not found.", "warning")
            return
        self._redirect_with_message(handler, return_to, f"宸插垏鎹㈡ā鏉匡細{template_name}", "success")

    def _handle_template_save(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        original_template_name = values.get("original_template_name", "").strip()
        template_name = values.get("template_name", "").strip()
        description = values.get("description", "").strip()
        template_json = values.get("template_json", "").strip()
        set_active = values.get("set_active", "").strip() in {"1", "on", "true", "yes"}
        active_before = self.template_store.get_active_template_name()

        try:
            parsed = json.loads(template_json)
        except json.JSONDecodeError as exc:
            self._render_template_editor(
                handler,
                original_template_name or None,
                {"message": [f"Template JSON parse failed: {exc.msg}"], "kind": ["warning"]},
                spec_override={"template_name": template_name, "description": description, "preprocess": {}, "fields": []},
                template_json_override=template_json,
                original_template_name_override=original_template_name,
            )
            return

        if not isinstance(parsed, dict):
            self._render_template_editor(
                handler,
                original_template_name or None,
                {"message": ["Template JSON must be an object."], "kind": ["warning"]},
                spec_override={"template_name": template_name, "description": description, "preprocess": {}, "fields": []},
                template_json_override=template_json,
                original_template_name_override=original_template_name,
            )
            return

        parsed["template_name"] = template_name or parsed.get("template_name", "")
        parsed["description"] = description
        validation_error = self._validate_template_spec(parsed)
        if validation_error:
            self._render_template_editor(
                handler,
                original_template_name or None,
                {"message": [validation_error], "kind": ["warning"]},
                spec_override=parsed,
                template_json_override=json.dumps(parsed, ensure_ascii=False, indent=2),
                original_template_name_override=original_template_name,
            )
            return

        saved_name = self.template_store.save_template_spec(parsed, original_template_name or None)
        if original_template_name and original_template_name != saved_name:
            self.repository.rename_template_name(original_template_name, saved_name)
        if set_active or (original_template_name and original_template_name == active_before):
            self.template_store.set_active_template_name(saved_name)

        message = f"Template saved: {saved_name}"
        if set_active:
            message += " and set as active template."
        self._redirect_with_message(handler, "/ocr?mode=ocr", message, "success")

    def _handle_upload(self, handler: BaseHTTPRequestHandler) -> None:
        form = self._parse_multipart_form(handler)
        file_items = form["files"] if "files" in form else []
        if not isinstance(file_items, list):
            file_items = [file_items]
        raw_return_to = form.getvalue("return_to") if "return_to" in form else "/ocr?mode=ocr"
        return_to = self._safe_return_to(str(raw_return_to or ""), "/ocr?mode=ocr")
        selected_template = ""
        if "template_name" in form:
            raw_template = form.getvalue("template_name")
            if isinstance(raw_template, str):
                selected_template = raw_template.strip()

        queued = 0
        failed = 0
        skipped = 0
        for item in file_items:
            filename = item.filename or ""
            if not filename:
                continue
            suffix = Path(filename).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png"}:
                skipped += 1
                continue
            payload = item.file.read()
            if not payload:
                skipped += 1
                continue
            upload_item = UploadItem(
                filename=Path(filename).name,
                source_relpath=filename.replace("\\", "/"),
                payload=payload,
            )
            try:
                result = self.service.process_upload(upload_item, template_name=selected_template)
            except Exception:
                failed += 1
                continue
            if result.status == "error":
                failed += 1
            else:
                queued += 1

        message = f"Queued {queued}, failed {failed}, skipped {skipped}."
        kind = "warning" if failed else "success"
        self._redirect_with_message(handler, return_to, message, kind)

    def _handle_line_haul_contact_create(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        return_to = self._safe_return_to(values.get("return_to", ""), "/line-haul-contacts")
        payload = self._line_haul_contact_payload(values)
        if not payload["company_name"] or not payload["service_area"]:
            self._redirect_with_message(handler, return_to, "公司名称和分流站点不能为空。", "warning")
            return
        try:
            row = self.repository.create_line_haul_contact(payload)
        except Exception as exc:
            self._redirect_with_message(handler, return_to, f"新增专线分流资料失败：{exc}", "warning")
            return
        self._redirect_with_message(
            handler,
            return_to,
            f"已新增：{row.get('company_name', '')} / {row.get('service_area', '')}",
            "success",
        )

    def _handle_line_haul_contact_update(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        contact_id = self._parse_line_haul_contact_id(path, "update")
        if contact_id is None:
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "专线分流资料不存在。"})
            return
        values = self._parse_urlencoded_form(handler)
        return_to = self._safe_return_to(values.get("return_to", ""), "/line-haul-contacts")
        wants_redirect = bool(str(values.get("return_to", "") or "").strip())
        payload = self._line_haul_contact_payload(values)
        if not payload["company_name"] or not payload["service_area"]:
            if wants_redirect:
                self._redirect_with_message(handler, return_to, "公司名称和分流站点不能为空。", "warning")
                return
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "公司名称和分流站点不能为空。"})
            return
        try:
            row = self.repository.update_line_haul_contact(contact_id, payload)
        except Exception as exc:
            if wants_redirect:
                self._redirect_with_message(handler, return_to, f"保存失败：{exc}", "warning")
                return
            self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": f"保存失败：{exc}"})
            return
        if not row:
            if wants_redirect:
                self._redirect_with_message(handler, return_to, "专线分流资料不存在。", "warning")
                return
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "专线分流资料不存在。"})
            return
        if wants_redirect:
            self._redirect_with_message(
                handler,
                return_to,
                f"已保存：{row.get('company_name', '')} / {row.get('service_area', '')}",
                "success",
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "message": "已保存", "row": row})

    def _handle_line_haul_contact_import_paste(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        return_to = self._safe_return_to(values.get("return_to", ""), "/line-haul-contacts")
        paste_text = values.get("paste_text", "")
        parsed = parse_line_haul_paste(paste_text)
        rows = parsed["rows"]
        if not rows:
            self._redirect_with_message(handler, return_to, "没有可导入的有效行。", "warning")
            return
        try:
            stats = self.repository.import_line_haul_contacts(rows)
        except Exception as exc:
            self._redirect_with_message(handler, return_to, f"导入失败：{exc}", "warning")
            return
        message = (
            f"已导入 {stats.get('inserted', 0)} 条，"
            f"跳过重复 {stats.get('skipped_duplicate', 0)} 条，"
            f"跳过空行 {parsed.get('skipped_empty', 0)} 条。"
        )
        kind = "success" if stats.get("inserted", 0) else "warning"
        self._redirect_with_message(handler, return_to, message, kind)

    def _handle_waybill_status_update(self, handler: BaseHTTPRequestHandler, waybill_id: int) -> None:
        values = self._parse_urlencoded_form(handler)
        return_to = self._clean_next_url(values.get("return_to", "/waybills"))
        if not return_to.startswith("/waybills"):
            return_to = "/waybills"
        status = normalize_waybill_status(values.get("status", ""))
        if status != "cancelled":
            self._redirect_with_message(handler, return_to, "当前只支持作废运单。", "warning")
            return
        try:
            updated = self.repository.update_waybill_status(waybill_id, status)
        except Exception as exc:
            self._redirect_with_message(handler, return_to, f"运单状态更新失败：{exc}", "warning")
            return
        if not updated:
            self._redirect_with_message(handler, return_to, "运单不存在或状态未更新。", "warning")
            return
        self._redirect_with_message(handler, return_to, "运单已作废。", "success")

    def _unwrap_quote_agent_result(self, result: dict[str, Any], *, label: str) -> dict[str, Any]:
        if not result.get("ok"):
            error = result.get("error")
            payload = error if isinstance(error, dict) else {"error": str(error or f"{label}报价调用失败")}
            if result.get("status"):
                payload["status_code"] = result.get("status")
            return payload
        outer = result.get("data")
        if isinstance(outer, dict):
            if outer.get("ok") is False:
                return outer
            nested = outer.get("data")
            if isinstance(nested, dict):
                return nested
            return outer
        return {"error": f"{label}报价返回格式异常"}

    def _call_quote_agent_source(
        self,
        *,
        endpoint: str,
        label: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        timeout_sec = 75
        payload = {
            "params": {
                "address": request["receiver_address"],
                "weight": str(request["weight"]),
                "volume": str(request["volume"]),
            },
            "timeout_sec": timeout_sec,
        }
        result = self._agent_request(
            "POST",
            endpoint,
            payload=payload,
            timeout=max(timeout_sec + 15, self.settings.agent_timeout_seconds),
        )
        return self._unwrap_quote_agent_result(result, label=label)

    def _handle_quote_options(self, handler: BaseHTTPRequestHandler) -> None:
        body = self._parse_json_body(handler)
        try:
            request = parse_quote_options_request(body)
        except QuoteOptionsValidationError as exc:
            self._send_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "message": str(exc), "quotes": [], "best_provider": "", "available_count": 0},
            )
            return

        sources = {
            "ronghui": ("/internal/v1/tms/get_price", "融辉"),
            "yunda": ("/internal/v1/tms/yunda_price", "韵达"),
        }
        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(
                    self._call_quote_agent_source,
                    endpoint=endpoint,
                    label=label,
                    request=request,
                ): provider
                for provider, (endpoint, label) in sources.items()
            }
            for future in as_completed(futures):
                provider = futures[future]
                try:
                    results[provider] = future.result()
                except Exception as exc:
                    results[provider] = {"error": f"{sources[provider][1]}报价调用失败：{exc}"}

        payload = build_manual_quote_options(
            ronghui_result=results.get("ronghui") or {"error": "融辉报价无返回"},
            yunda_result=results.get("yunda") or {"error": "韵达报价无返回"},
            delivery_method=request["delivery_method"],
        )
        self._send_json(handler, HTTPStatus.OK, payload)

    def _handle_manual_waybill(self, handler: BaseHTTPRequestHandler) -> None:
        return_to = "/ocr"
        try:
            values = self._parse_urlencoded_form(handler)
            return_to = self._safe_return_to(values.get("return_to", ""), "/ocr")
            should_print = str(values.get("auto_print", "")).lower() in {"1", "true", "yes", "on"}
            result = self.service.apply_manual_waybill(values)
        except Exception as exc:
            self._redirect_with_message(handler, return_to, f"手工单保存失败：{exc}", "warning")
            return

        if not result.ok or not result.waybill_id:
            self._redirect_with_message(handler, return_to, result.message, "warning")
            return

        if not should_print:
            self._redirect_with_message(handler, return_to, result.message, "success")
            return

        self._redirect_with_message(
            handler,
            f"/waybills/{result.waybill_id}/print?autoprint=1",
            result.message,
            "success",
        )

    def _handle_review(self, handler: BaseHTTPRequestHandler, document_id: int) -> None:
        try:
            values = self._parse_urlencoded_form(handler)
            action = values.get("action", "save")
            result = self.service.apply_review(document_id, values)
        except ValueError:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
            return
        except Exception as exc:
            self._redirect_with_message(handler, f"/documents/{document_id}", f"Save failed: {exc}", "warning")
            return

        kind = "success" if result.ok else "warning"

        # If confirmed successfully, redirect to the queue to find the next document
        if action == "confirm" and result.ok:
            self._redirect_with_message(handler, "/ocr?mode=ocr", result.message, kind)
        else:
            self._redirect_with_message(handler, f"/documents/{document_id}", result.message, kind)

    def _handle_reprocess(self, handler: BaseHTTPRequestHandler, document_id: int) -> None:
        try:
            result = self.service.reprocess_document(document_id)
        except ValueError:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
            return
        kind = "success" if result.ok else "warning"
        self._redirect_with_message(handler, f"/documents/{document_id}", result.message, kind)

    def _handle_delete(self, handler: BaseHTTPRequestHandler, document_id: int) -> None:
        values = self._parse_urlencoded_form(handler)
        return_to = values.get("return_to", "").strip()
        if not return_to.startswith("/"):
            return_to = "/ocr"

        document = self.repository.get_document(document_id)
        if not document:
            self._redirect_with_message(handler, return_to, "Document does not exist or was already deleted.", "warning")
            return

        self._delete_document_files(document)
        deleted = self.repository.delete_document(document_id)
        if not deleted:
            self._redirect_with_message(handler, return_to, "Delete failed because the database row was not found.", "warning")
            return

        if return_to == f"/documents/{document_id}":
            return_to = "/ocr"
        self._redirect_with_message(handler, return_to, f"Deleted document: {document['original_name']}", "success")

    def _export_document_json(self, handler: BaseHTTPRequestHandler, document_id: int) -> None:
        document = self.repository.get_document(document_id)
        if not document:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
            return
        payload = json.dumps(document, ensure_ascii=False, indent=2)
        self._send_bytes(
            handler,
            HTTPStatus.OK,
            payload.encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _serve_runtime_file(self, handler: BaseHTTPRequestHandler, relpath: str) -> None:
        self._serve_file(handler, self.settings.runtime_dir, relpath)

    def _serve_static_file(self, handler: BaseHTTPRequestHandler, relpath: str) -> None:
        normalized_path = relpath.replace("\\", "/")
        request_path = str(getattr(handler, "path", "") or "")
        versioned_request = "v" in parse_qs(urlparse(request_path).query, keep_blank_values=True)
        immutable_asset = (
            versioned_request
            or normalized_path.startswith("vendor/")
            or normalized_path == "assets/boyi-logistics-logo-7e1f2994.webp"
        )
        cache_control = (
            "public, max-age=31536000, immutable"
            if immutable_asset
            else "public, max-age=3600, stale-while-revalidate=86400"
        )
        self._serve_file(handler, MODULE_DIR / "static", relpath, cache_control=cache_control)

    def _serve_file(
        self,
        handler: BaseHTTPRequestHandler,
        root: Path,
        relpath: str,
        *,
        cache_control: str | None = None,
    ) -> None:
        root = root.resolve()
        target = (root / Path(unquote(relpath))).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "File not found.")
            return
        if not target.exists() or not target.is_file():
            self._send_text(handler, HTTPStatus.NOT_FOUND, "File not found.")
            return
        extra_headers: dict[str, str] = {}
        if cache_control:
            stat = target.stat()
            etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
            extra_headers = {
                "ETag": etag,
                "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
            }
            request_headers = getattr(handler, "headers", {}) or {}
            if str(request_headers.get("If-None-Match", "") or "").strip() == etag:
                handler.send_response(HTTPStatus.NOT_MODIFIED)
                handler.send_header("Cache-Control", cache_control)
                for name, value in extra_headers.items():
                    handler.send_header(name, value)
                handler.end_headers()
                return
        mime_type, _ = mimetypes.guess_type(str(target))
        with target.open("rb") as handle:
            payload = handle.read()
        self._send_bytes(
            handler,
            HTTPStatus.OK,
            payload,
            mime_type or "application/octet-stream",
            cache_control=cache_control,
            extra_headers=extra_headers,
        )

    def _parse_multipart_form(self, handler: BaseHTTPRequestHandler):
        return cgi.FieldStorage(
            fp=handler.rfile,
            headers=handler.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": handler.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": handler.headers.get("Content-Length", "0"),
            },
        )

    def _parse_urlencoded_form(self, handler: BaseHTTPRequestHandler) -> dict[str, str]:
        content_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(content_length).decode("utf-8")
        parsed = parse_qs(body, keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in parsed.items()}

    def _runtime_url(self, relpath: str) -> str:
        normalized = relpath.replace("\\", "/")
        url = "/runtime/" + quote(normalized)
        target = self.settings.runtime_dir / normalized
        if target.exists() and target.is_file():
            stamp = int(target.stat().st_mtime_ns)
            return f"{url}?v={stamp}"
        return url

    def _delete_document_files(self, document: dict[str, Any]) -> None:
        runtime_paths: list[Path] = []
        for key in ("original_path", "processed_path", "artifacts_dir"):
            relpath = str(document.get(key, "") or "").strip()
            if not relpath:
                continue
            runtime_paths.append(self.settings.runtime_dir / Path(relpath))

        token = str(document.get("doc_token", "") or "").strip()
        if token:
            runtime_paths.append(self.settings.temp_dir / token)
            runtime_paths.append(self.settings.runtime_dir / "artifacts" / "processed" / token)

        seen: set[Path] = set()
        runtime_root = self.settings.runtime_dir.resolve()
        for candidate in runtime_paths:
            target = candidate.resolve()
            if target in seen:
                continue
            seen.add(target)
            try:
                target.relative_to(runtime_root)
            except ValueError:
                continue
            if not target.exists():
                continue
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)

    def _parse_document_id(self, path: str) -> int | None:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 2:
            return None
        try:
            return int(parts[1])
        except ValueError:
            return None

    def _parse_line_haul_contact_id(self, path: str, suffix: str) -> int | None:
        prefix = "/line-haul-contacts/"
        suffix_value = f"/{suffix}"
        if not path.startswith(prefix) or not path.endswith(suffix_value):
            return None
        raw = path[len(prefix) : -len(suffix_value)].strip("/")
        try:
            return int(raw)
        except ValueError:
            return None

    @staticmethod
    def _line_haul_contact_payload(values: dict[str, str]) -> dict[str, str]:
        payload = {
            "company_name": str(values.get("company_name", "") or "").strip(),
            "service_area": str(values.get("service_area", "") or "").strip(),
            "address": str(values.get("address", "") or "").strip(),
            "contact_name": str(values.get("contact_name", "") or "").strip(),
            "phone_numbers": normalize_phone_numbers(values.get("phone_numbers", "")),
            "remark": str(values.get("remark", "") or "").strip(),
            "source_text": str(values.get("source_text", "") or "").strip(),
        }
        if not payload["source_text"]:
            payload["source_text"] = " ".join(
                value
                for key, value in payload.items()
                if key not in {"source_text"} and value
            )
        return payload

    def _redirect(
        self,
        handler: BaseHTTPRequestHandler,
        location: str,
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        handler.send_response(HTTPStatus.SEE_OTHER)
        handler.send_header("Location", location)
        for header_name, header_value in headers or []:
            handler.send_header(header_name, header_value)
        handler.end_headers()

    def _redirect_with_message(
        self,
        handler: BaseHTTPRequestHandler,
        location: str,
        message: str,
        kind: str = "info",
    ) -> None:
        separator = "&" if "?" in location else "?"
        encoded_message = quote(message)
        self._redirect(handler, f"{location}{separator}message={encoded_message}&kind={kind}")

    def _send_html(self, handler: BaseHTTPRequestHandler, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send_bytes(
            handler,
            status,
            body.encode("utf-8"),
            "text/html; charset=utf-8",
            cache_control="no-store",
        )

    def _send_text(self, handler: BaseHTTPRequestHandler, status: HTTPStatus, text: str) -> None:
        payload = html.escape(text).encode("utf-8")
        self._send_bytes(handler, status, payload, "text/plain; charset=utf-8")

    def _send_json(self, handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            handler,
            status,
            body,
            "application/json; charset=utf-8",
            cache_control="no-store",
        )

    def _send_bytes(
        self,
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
        cache_control: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        response_payload = payload
        response_headers = dict(extra_headers or {})
        content_type_base = content_type.partition(";")[0].strip().lower()
        compressible = (
            content_type_base.startswith("text/")
            or content_type_base in {
                "application/javascript",
                "application/json",
                "application/xml",
                "image/svg+xml",
            }
        )
        request_headers = getattr(handler, "headers", {}) or {}
        accept_encoding = str(request_headers.get("Accept-Encoding", "") or "").lower()
        if len(payload) >= 1024 and compressible and _accepts_content_encoding(accept_encoding, "gzip"):
            compressed_payload = gzip.compress(payload, compresslevel=6, mtime=0)
            if len(compressed_payload) < len(payload):
                response_payload = compressed_payload
                response_headers["Content-Encoding"] = "gzip"
                vary_values = {
                    value.strip()
                    for value in response_headers.get("Vary", "").split(",")
                    if value.strip()
                }
                vary_values.add("Accept-Encoding")
                response_headers["Vary"] = ", ".join(sorted(vary_values))
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        if cache_control:
            handler.send_header("Cache-Control", cache_control)
        for name, value in response_headers.items():
            handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(response_payload)))
        handler.end_headers()
        handler.wfile.write(response_payload)
