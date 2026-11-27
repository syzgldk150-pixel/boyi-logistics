"""Attachments the way the original pages take them (read-only capture 2026-11-27).

Pure rules, no I/O. The Console checks an upload against :data:`LIMITS` before sending it on;
the Agent uploads with the business account and turns the platform's answer into one
descriptor (:func:`ronghui_descriptor` / :func:`yunda_descriptor`); the processing actions turn
descriptors back into what each original page saves (:func:`ronghui_attachment_rows`,
:func:`yunda_file_arr`).

* 融辉 ``POST /file/upload?sysFileUploadId=ALL`` answers ``{success, data: [{fileName, fileSize,
  fileExt, fileDir}]}``; pages save ``FILE_PATH = "/unauth/download/" + fileDir`` with the
  uploader's site/name and ``FILE_TYPE`` from the extension (``isAssetTypeAnImage``).
* 韵达 ``issue/uploadImg.html`` answers ``{code: 1, path, oldName, newName}``; the reply page saves
  ``file_arr: [{new_name, old_name, file_path}]``; at most 3 files of 5MB, names of letters,
  digits, Chinese, ``-._`` only, listed extensions only.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Mapping, Sequence

MB = 1024 * 1024
# (platform, purpose) -> (most files, largest file in bytes); from each original page's upload checks.
LIMITS: dict[tuple[str, str], tuple[int, int]] = {
    ("ronghui", "work_order"): (9, 200 * MB),         # 工单 响应/完结/催工单/申诉
    ("ronghui", "complaint_reply"): (10, 20 * MB),    # 被投诉方“投诉回复-新增”
    ("ronghui", "appeal"): (20, 100 * MB),            # 申诉登记
    ("ronghui", "complaint_image"): (1, 30 * MB),     # 投诉方 提交申诉/提交材料：每格一张（/file/upload?sysFileUploadId=IMG）
    ("yunda", "problem_reply"): (3, 5 * MB),          # 问题件 query/reply.html
    ("yunda", "transfer"): (3, 5 * MB),               # 调拨件异常图片
}
# One request through the public entry (Nginx client_max_body_size 34m) carries at most this much.
TRANSPORT_MAX = 30 * MB
YUNDA_EXTENSIONS = frozenset("jpg png gif bmp jpeg svg txt doc docx xls xlsx exe rar zip ppt pptx".split())
_YUNDA_NAME = re.compile(r"^[a-zA-Z0-9\-._一-龥]+$")
_RONGHUI_TYPES = (
    ((".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".psd", ".svg", ".tiff"), "图片"),
    ((".pdf",), "pdf"),
    ((".xls", ".csv", ".xlsx", ".doc", ".ppt"), "文件"),
    ((".txt",), "文本文件"),
    ((".rar", ".zip", ".7zip"), "压缩文件"),
)


class AttachmentError(ValueError):
    """The file or descriptor breaks an original-page rule; the message says which."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def check_upload(platform: str, purpose: str, file_name: str, size: int) -> None:
    """The checks the original page runs on one chosen file before uploading it."""
    limit = LIMITS.get((platform, purpose))
    if limit is None:
        raise AttachmentError("不支持的附件用途。")
    if not file_name or size <= 0:
        raise AttachmentError("附件文件为空。")
    if size > limit[1]:
        raise AttachmentError(f"单个文件最大{limit[1] // MB}Mb")
    if size > TRANSPORT_MAX:
        raise AttachmentError(f"博益目前只能传 {TRANSPORT_MAX // MB}MB 以内的附件，更大的请在原系统上传。")
    if platform == "yunda":
        if not _YUNDA_NAME.match(file_name):
            raise AttachmentError("不支持文件名包含符号的文件")
        if file_name.rsplit(".", 1)[-1].lower() not in YUNDA_EXTENSIONS or "." not in file_name:
            raise AttachmentError("不支持该类型的文件")


def ronghui_file_type(ext: str) -> str:
    ext = _text(ext).lower()
    ext = ext if ext.startswith(".") else f".{ext}"
    return next((label for exts, label in _RONGHUI_TYPES if ext in exts), "其它类型")


def ronghui_descriptor(result: Any) -> dict[str, Any]:
    """The upload answer as the attachment the pages keep (``FILE_PATH`` = /unauth/download/fileDir)."""
    data = result.get("data") if isinstance(result, Mapping) else None
    item = data[0] if isinstance(data, list) and data and isinstance(data[0], Mapping) else None
    if not isinstance(result, Mapping) or result.get("success") is not True or item is None or not _text(item.get("fileDir")):
        raise AttachmentError(_text(result.get("message") if isinstance(result, Mapping) else "") or "融辉附件上传未返回文件。")
    try:
        size_kb = int(round(float(item.get("fileSize") or 0) / 1024))
    except (TypeError, ValueError):
        size_kb = 0
    return {"platform": "ronghui", "file_name": _text(item.get("fileName")), "file_ext": _text(item.get("fileExt")),
            "file_size_kb": size_kb, "file_path": "/unauth/download/" + _text(item.get("fileDir")).lstrip("/")}


def yunda_descriptor(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping) or str(result.get("code")) != "1" or not _text(result.get("path")):
        raise AttachmentError(_text(result.get("msg") if isinstance(result, Mapping) else "") or "韵达附件上传失败。")
    return {"platform": "yunda", "file_name": _text(result.get("oldName")), "old_name": _text(result.get("oldName")),
            "new_name": _text(result.get("newName")), "file_path": _text(result.get("path"))}


def attachments(value: Any, platform: str, purpose: str) -> list[dict[str, Any]]:
    """Descriptors sent back with an action: shape, platform and count as the page allows."""
    if value in (None, ""):
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AttachmentError("附件格式不正确。")
    limit = LIMITS.get((platform, purpose))
    if limit is None:
        raise AttachmentError("不支持的附件用途。")
    if len(value) > limit[0]:
        raise AttachmentError(f"最多上传{limit[0]}个附件")
    result, names = [], set()
    for raw in value:
        if not isinstance(raw, Mapping) or _text(raw.get("platform")) != platform:
            raise AttachmentError("附件不属于这个平台，请重新上传。")
        path = _text(raw.get("file_path"))
        if platform == "ronghui" and not path.startswith("/unauth/download/"):
            raise AttachmentError("附件路径无效，请重新上传。")
        if platform == "yunda" and (not path or not _text(raw.get("new_name"))):
            raise AttachmentError("附件路径无效，请重新上传。")
        name = _text(raw.get("file_name") or raw.get("old_name"))
        if name in names:
            raise AttachmentError("列表已经存在该附件!")
        names.add(name)
        result.append(dict(raw))
    return result


def ronghui_attachment_rows(items: Sequence[Mapping[str, Any]], user_info: Mapping[str, Any], now: dt.datetime,
                            upload_type: str, **extra: Any) -> list[dict[str, Any]]:
    """The attachment rows a Ronghui page saves next to its record (``addInfoToPicDataGrid``)."""
    rows = []
    for item in items:
        row = {
            "FILE_NAME": _text(item.get("file_name")), "FILE_SIZE": str(item.get("file_size_kb") or 0),
            "FILE_TYPE": ronghui_file_type(_text(item.get("file_ext"))),
            "UPLOAD_SITE": _text(user_info.get("loginSiteName")), "UPLOAD_SITE_CODE": _text(user_info.get("loginSiteCode")),
            "UPLOAD_MAN": _text(user_info.get("loginEmpName")), "UPLOAD_MAN_CODE": _text(user_info.get("loginEmpCode")),
            "UPLOAD_DATE": now.strftime("%Y-%m-%d %H:%M:%S"), "FILE_PATH": _text(item.get("file_path")),
            "FILE_UPLOAD_TYPE": upload_type,
        }
        row.update(extra)
        rows.append(row)
    return rows


def yunda_file_arr(items: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [{"new_name": _text(item.get("new_name")), "old_name": _text(item.get("old_name")),
             "file_path": _text(item.get("file_path"))} for item in items]


__all__ = ["AttachmentError", "LIMITS", "TRANSPORT_MAX", "attachments", "check_upload", "ronghui_attachment_rows", "ronghui_descriptor",
           "ronghui_file_type", "yunda_descriptor", "yunda_file_arr"]
