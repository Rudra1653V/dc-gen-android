"""Optional phone verification. Off unless --sms."""
from __future__ import annotations

import re
import time

import requests

SMS_WAIT = 180
SMS_BASE = "https://api.sms-activate.org/stubs/handler_api.php"


def send(provider: str, key: str, country: str, max_price: float | None = None):
    if provider == "smspool":
        data = {"key": key, "country": country, "service": "discord", "quantity": 1}
        if max_price:
            data["max_price"] = max_price
        j = requests.post("https://api.smspool.net/purchase/sms", data=data,
                          timeout=60).json()
        if str(j.get("success")) != "1":
            raise RuntimeError(j.get("message") or j)
        return "+" + str(j["phonenumber"]).lstrip("+"), str(j["orderid"])

    if provider == "5sim":
        params = {"maxPrice": max_price} if max_price else {}
        j = requests.get(
            f"https://5sim.net/v1/user/buy/activation/{country}/any/discord",
            headers={"Authorization": f"Bearer {key}"}, params=params, timeout=60).json()
        if "phone" not in j:
            raise RuntimeError(j)
        return "+" + str(j["phone"]).lstrip("+"), str(j["id"])

    params = {"api_key": key, "action": "getNumber", "service": "ds", "country": country}
    if max_price:
        params["maxPrice"] = max_price
    parts = requests.get(SMS_BASE, params=params, timeout=60).text.strip().split(":")
    if parts[0] != "ACCESS_NUMBER":
        raise RuntimeError(":".join(parts))
    return "+" + parts[2].lstrip("+"), parts[1]


def poll(provider: str, key: str, order_id: str, timeout: int = SMS_WAIT) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        blob = ""
        try:
            if provider == "smspool":
                j = requests.post("https://api.smspool.net/sms/check", timeout=30,
                                  data={"key": key, "orderid": order_id}).json()
                blob = " ".join(str(j.get(k, "")) for k in ("sms", "full_sms", "code"))
            elif provider == "5sim":
                j = requests.get(f"https://5sim.net/v1/user/check/{order_id}", timeout=30,
                                 headers={"Authorization": f"Bearer {key}"}).json()
                blob = " ".join((s.get("code") or s.get("text") or "")
                                for s in (j.get("sms") or []))
            else:
                r = requests.get(SMS_BASE, timeout=30, params={
                    "api_key": key, "action": "getStatus", "id": order_id})
                if r.text.startswith("STATUS_OK"):
                    return r.text.split(":")[1].strip()
        except Exception:
            pass
        m = re.search(r"\b(\d{4,8})\b", blob)
        if m:
            return m.group(1)
        time.sleep(5)
    return None


def finish(provider: str, key: str, order_id: str) -> None:
    try:
        if provider == "smspool":
            requests.post("https://api.smspool.net/sms/orderstatus", timeout=30,
                          data={"key": key, "orderid": order_id, "status": 6})
        elif provider == "5sim":
            requests.get(f"https://5sim.net/v1/user/finish/{order_id}", timeout=30,
                         headers={"Authorization": f"Bearer {key}"})
        else:
            requests.get(SMS_BASE, timeout=30, params={
                "api_key": key, "action": "setStatus", "id": order_id, "status": 6})
    except Exception:
        pass


def cancel(provider: str, key: str, order_id: str) -> None:
    try:
        if provider == "smspool":
            requests.post("https://api.smspool.net/sms/cancel", timeout=30,
                          data={"key": key, "orderid": order_id})
        elif provider == "5sim":
            requests.get(f"https://5sim.net/v1/user/cancel/{order_id}", timeout=30,
                         headers={"Authorization": f"Bearer {key}"})
        else:
            requests.get(SMS_BASE, timeout=30, params={
                "api_key": key, "action": "setStatus", "id": order_id, "status": 8})
    except Exception:
        pass