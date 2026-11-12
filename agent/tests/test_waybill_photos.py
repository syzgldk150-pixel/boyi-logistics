"""Sign-for photo addresses are taken only from the verified platform fields."""

import pytest

from agent.tms_runtime import waybill_photos
from agent.tms_runtime.direct_business import DirectBusinessError


def test_yunda_sign_photos_come_only_from_the_waybill_monitoring_pictures():
    payload = {"rows": [{"900000001": {"monitoring": {
        "signedPic": ["https://kyscan.yunda56.com/ws/sc/20261111/a.jpg",
                      "https://evil.example/ws/b.jpg", "http://kyscan.yunda56.com/ws/c.jpg",
                      "https://kyscan.yunda56.com/other/d.jpg"],
        "elecSigned": "https://kyscan.yunda56.com/ws/"}}}]}

    assert waybill_photos._yunda_sign_urls(payload, "900000001") == [
        "https://kyscan.yunda56.com/ws/sc/20261111/a.jpg"]
    assert waybill_photos._yunda_sign_urls(payload, "900000002") == []


def test_ronghui_sign_photo_is_the_public_download_path_on_the_tms_host():
    assert (waybill_photos._ronghui_sign_url("/unauth/download/2026/a.jpg")
            == "https://tms.ronghuiwl.com/unauth/download/2026/a.jpg")
    assert waybill_photos._ronghui_sign_url("/login/a.jpg") == ""
    assert waybill_photos._ronghui_sign_url("https://evil.example/unauth/download/a.jpg") == ""


@pytest.mark.parametrize("params", [
    {"platform": "best", "waybill_no": "900000001"},
    {"platform": "yunda", "waybill_no": "9 0"},
    {"platform": "yunda", "waybill_no": "900000001", "account_id": "x"},
])
def test_only_platform_and_waybill_number_are_accepted(params):
    with pytest.raises(DirectBusinessError):
        waybill_photos._validated(params)
