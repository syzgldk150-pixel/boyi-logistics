"""Attachments as the original pages take them (2026-11-27, shared/original_page_attachments.py)."""
import datetime as dt

import pytest

from shared.original_page_attachments import (
    MB, TRANSPORT_MAX, AttachmentError, attachments, check_upload, ronghui_attachment_rows, ronghui_descriptor,
    ronghui_file_type, yunda_descriptor, yunda_file_arr,
)


@pytest.mark.parametrize("platform,purpose,name,size,message", [
    ("yunda", "problem_reply", "回复.png", 6 * MB, "单个文件最大5Mb"),
    ("yunda", "problem_reply", "a&b.png", 100, "不支持文件名包含符号的文件"),
    ("yunda", "problem_reply", "a.pdf", 100, "不支持该类型的文件"),
    ("ronghui", "complaint_reply", "a.pdf", 21 * MB, "单个文件最大20Mb"),
    ("ronghui", "work_order", "a.pdf", TRANSPORT_MAX + 1, "30MB 以内"),
    ("ronghui", "unknown", "a.pdf", 10, "不支持的附件用途"),
])
def test_uploads_are_checked_like_the_original_pages(platform, purpose, name, size, message):
    with pytest.raises(AttachmentError, match=message):
        check_upload(platform, purpose, name, size)


def test_good_uploads_pass():
    check_upload("yunda", "problem_reply", "回复-1_a.png", 4 * MB)
    check_upload("ronghui", "work_order", "照片 (1).jpg", 25 * MB)


def test_platform_answers_become_the_descriptors_pages_keep():
    ronghui = ronghui_descriptor({"success": True, "data": [{"fileName": "a.png", "fileSize": 20480, "fileExt": ".png",
                                                            "fileDir": "2026/11/27/abc.png"}]})
    assert ronghui == {"platform": "ronghui", "file_name": "a.png", "file_ext": ".png", "file_size_kb": 20,
                       "file_path": "/unauth/download/2026/11/27/abc.png"}
    yunda = yunda_descriptor({"code": 1, "path": "/upload/x.png", "oldName": "a.png", "newName": "x.png"})
    assert yunda_file_arr([yunda]) == [{"new_name": "x.png", "old_name": "a.png", "file_path": "/upload/x.png"}]
    for bad in ({"success": False, "message": "上传失败"}, {"success": True, "data": []}):
        with pytest.raises(AttachmentError):
            ronghui_descriptor(bad)
    with pytest.raises(AttachmentError, match="不合规"):
        yunda_descriptor({"code": 0, "msg": "不合规"})


def test_attachments_sent_back_are_checked_and_saved_like_the_page():
    item = {"platform": "ronghui", "file_name": "a.png", "file_ext": ".png", "file_size_kb": 20, "file_path": "/unauth/download/x"}
    with pytest.raises(AttachmentError, match="最多上传3个附件"):
        attachments([dict(item, platform="yunda", new_name=str(i), file_path="/p") for i in range(4)], "yunda", "problem_reply")
    with pytest.raises(AttachmentError, match="路径无效"):
        attachments([dict(item, file_path="/etc/passwd")], "ronghui", "work_order")
    with pytest.raises(AttachmentError, match="不属于这个平台"):
        attachments([item], "yunda", "problem_reply")
    with pytest.raises(AttachmentError, match="已经存在"):
        attachments([item, item], "ronghui", "work_order")
    rows = ronghui_attachment_rows(attachments([item], "ronghui", "work_order"),
                                   {"loginSiteName": "邵阳大祥站", "loginSiteCode": "S1", "loginEmpName": "合成员工", "loginEmpCode": "E1"},
                                   dt.datetime(2026, 11, 27, 9, 30), "投诉方附件", ORDER_GUID="G1")
    assert rows == [{"FILE_NAME": "a.png", "FILE_SIZE": "20", "FILE_TYPE": "图片", "UPLOAD_SITE": "邵阳大祥站", "UPLOAD_SITE_CODE": "S1",
                     "UPLOAD_MAN": "合成员工", "UPLOAD_MAN_CODE": "E1", "UPLOAD_DATE": "2026-11-27 09:30:00",
                     "FILE_PATH": "/unauth/download/x", "FILE_UPLOAD_TYPE": "投诉方附件", "ORDER_GUID": "G1"}]
    assert [ronghui_file_type(ext) for ext in (".PDF", "xlsx", ".txt", ".zip", ".mp4")] == ["pdf", "文件", "文本文件", "压缩文件", "其它类型"]
