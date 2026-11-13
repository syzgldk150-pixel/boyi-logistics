"""Wiring of the share refresh: settings file, NAS address, daily schedule and image sources."""
import asyncio
import base64
from datetime import datetime
from zoneinfo import ZoneInfo

import receipt_share_composition as composition

CHINA = ZoneInfo("Asia/Shanghai")


def test_settings_file_is_optional_and_parsed_without_shell_rules(tmp_path, monkeypatch):
    monkeypatch.setenv(composition.SETTINGS_FILE_ENV, str(tmp_path / "missing.env"))
    assert composition.load_share_settings() is None
    path = tmp_path / "nas.env"
    path.write_text("RECEIPT_NAS_URL=http://100.72.135.6:5005\n# note\nRECEIPT_NAS_RW_USER=rw\nRECEIPT_NAS_RW_PASS=p=a$s #x\n\n",
                    encoding="utf-8")
    settings = composition.load_share_settings(str(path))
    assert settings == {"RECEIPT_NAS_URL": "http://100.72.135.6:5005", "RECEIPT_NAS_RW_USER": "rw",
                        "RECEIPT_NAS_RW_PASS": "p=a$s #x"}  # the password is taken literally


def test_nas_store_addresses_the_archive_folder_only():
    store = composition.nas_store_from_settings(
        {"RECEIPT_NAS_URL": "http://nas:5005/", "RECEIPT_NAS_RO_USER": "ro", "RECEIPT_NAS_RO_PASS": "x"}, role="RO")
    url = store._url("ronghui/2026-11/R1/sign_aaaaaaaaaaaaaaaa.jpg")
    assert url.startswith("http://nas:5005/%E5%9B%A2%E9%98%9F%E6%96%87%E4%BB%B6-") and "%E5%9B%9E%E5%8D%95%E5%BD%92%E6%A1%A3/ronghui/" in url


def test_next_daily_run_is_noon_evening_and_night_china_time():
    def at(hour, minute):
        return composition.next_daily_run(datetime(2026, 11, 13, hour, minute, tzinfo=CHINA))
    assert (at(9, 0).hour, at(12, 0).hour, at(18, 30).hour) == (12, 18, 23)
    late = at(23, 30)
    assert (late.day, late.hour) == (14, 12)


def test_only_allowed_sources_are_fetched(monkeypatch):
    calls = []
    monkeypatch.setattr(composition, "fetch_public_receipt_image",
                        lambda url, timeout: (b"public", "image/jpeg") if "public" in url else None)

    async def yunda_image(params, timeout, *, read_lifecycle):
        calls.append(params)
        return {"content_base64": base64.b64encode(b"yunda").decode()}

    monkeypatch.setattr(composition, "fetch_receipt_image_async", yunda_image)

    async def scenario():
        loop = asyncio.get_running_loop()
        runtime = composition.ReceiptShareRuntime(
            connect=lambda: None, read_lifecycle=object(), loop=loop,
            settings={"RECEIPT_NAS_URL": "http://nas:5005", "RECEIPT_NAS_RW_USER": "rw", "RECEIPT_NAS_RW_PASS": "x"})
        fetch = runtime._fetch_image
        return [await asyncio.to_thread(fetch, *args) for args in (
            ("ronghui", "sign", "https://public/a.jpg"), ("yunda", "receipt", "https://kyinms/x.jpg"),
            ("ronghui", "receipt", "https://elsewhere/x.jpg"), ("yunda", "sign", "https://elsewhere/y.jpg"))]

    assert asyncio.run(scenario()) == [b"public", b"yunda", b"", b""]
    assert calls == [{"source_url": "https://kyinms/x.jpg"}]  # only Yunda receipt images use the platform login
