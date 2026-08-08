"""OCR 识别工具：调 Qwen VL API 识别物流托运单图片，提取结构化字段

轻量版：不依赖 OpenCV 预处理，直接发原图给 Qwen VL。
ECS 上 subprocess 调用，峰值内存 ~50MB（主要是 base64 编码）。
"""

import sys
import os
import json
import base64
import mimetypes
import re
from pathlib import Path
from urllib import request, error

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

QWEN_API_KEY = (
    os.getenv("QWEN_VL_API_KEY", "")
    or os.getenv("Qwen VL API", "")
    or os.getenv("DOCFLOW_QWEN_API_KEY", "")
).strip()
QWEN_ENDPOINT = (
    os.getenv("QWEN_VL_ENDPOINT", "")
    or os.getenv("DOCFLOW_QWEN_ENDPOINT", "")
    or "https://dashscope.aliyuncs.com/compatible-mode/v1"
).strip()
QWEN_MODEL = (
    os.getenv("QWEN_VL_MODEL", "")
    or os.getenv("DOCFLOW_QWEN_MODEL", "")
    or "qwen-vl-ocr"
).strip()

# 确保 endpoint 指向 chat/completions
if QWEN_ENDPOINT.endswith("/v1"):
    QWEN_ENDPOINT += "/chat/completions"
elif not QWEN_ENDPOINT.endswith("/chat/completions"):
    QWEN_ENDPOINT = QWEN_ENDPOINT.rstrip("/") + "/chat/completions"

# 提取字段定义
EXTRACT_FIELDS = {
    "waybill_no": "运单号（右上角红色流水号）",
    "destination_site": "到达站/目的站",
    "open_date": "开单日期",
    "receiver_name": "收货人姓名",
    "receiver_phone": "收货人电话",
    "receiver_address": "收货人地址",
    "sender_name": "发货人姓名",
    "sender_phone": "发货人电话",
    "goods_name_lines": "货物名称（多行用换行符分隔）",
    "package_type_lines": "包装类型（多行用换行符分隔）",
    "quantity_lines": "件数（多行用换行符分隔）",
    "weight_volume": "重量/体积",
    "delivery_method": "派送方式（送货或自提）",
    "freight_fee": "运费金额",
    "pickup_fee": "提货费",
    "delivery_fee": "送货费",
    "transfer_fee": "中转费",
    "payment_method": "结算方式",
    "remark": "备注",
}

PROMPT = "\n".join([
    "你是物流托运单结构化录入助手。请阅读整张托运单图片，只返回一个 JSON 对象。",
    "不要输出解释，不要输出 Markdown，不要输出额外字段。",
    "",
    "JSON 的键和含义：",
    *[f'- "{k}": {v}' for k, v in EXTRACT_FIELDS.items()],
    "",
    "输出规则：",
    "1. 所有字段都必须存在，无法识别时填空字符串 \"\"。",
    "2. waybill_no 只返回右上角红色运单号，不要返回发站编号。",
    "3. destination_site 只返回到达站名称，不要抄到地址。",
    "4. freight_fee 等费用字段只保留数字，不带货币符号。",
    "5. delivery_method 只能写送货或自提。",
    "6. payment_method 只返回手写/勾选的结算方式。",
    "7. 只返回 JSON 对象。",
])


def _image_to_data_url(image_path: Path) -> str:
    """将图片文件编码为 base64 data URL"""
    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    with open(image_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime_type};base64,{data}"


def _call_qwen_api(data_url: str) -> dict:
    """调用 Qwen VL API（OpenAI 兼容格式）"""
    payload = {
        "model": QWEN_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        "max_tokens": 1024,
        "temperature": 0.0,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        QWEN_ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {QWEN_API_KEY}",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        return {"error": f"Qwen API HTTP {e.code}: {err_body}"}
    except error.URLError as e:
        return {"error": f"Qwen API 连接失败: {str(e)[:200]}"}
    except Exception as e:
        return {"error": f"Qwen API 调用异常: {str(e)[:200]}"}


def _extract_json_from_text(text: str) -> dict:
    """从 LLM 返回文本中提取 JSON 对象"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {"raw_text": text}


def ocr_recognize(image_path: str) -> dict:
    """识别物流单据图片，返回结构化字段"""
    path = Path(image_path)
    if not path.exists():
        return {"error": f"图片不存在: {image_path}"}

    if not QWEN_API_KEY:
        return {"error": "Qwen VL API Key 未配置（检查 .env 中的 QWEN_VL_API_KEY 或 'Qwen VL API'）"}

    file_size_mb = path.stat().st_size / (1024 * 1024)
    if file_size_mb > 20:
        return {"error": f"图片文件过大: {file_size_mb:.1f}MB（限制 20MB）"}

    data_url = _image_to_data_url(path)
    response = _call_qwen_api(data_url)
    if "error" in response:
        return response

    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return {"error": "Qwen API 返回格式异常", "raw_response": str(response)[:500]}

    fields = _extract_json_from_text(content)

    return {
        "image_path": image_path,
        "fields": fields,
        "model": QWEN_MODEL,
    }


def main():
    params = json.loads(sys.stdin.read())
    image_path = params.get("image_path", "")

    if not image_path:
        print(json.dumps({"error": "缺少 image_path"}, ensure_ascii=False))
        sys.exit(1)

    result = ocr_recognize(image_path)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
