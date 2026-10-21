"""Failure taxonomy. Every account record carries exactly one of these codes."""
from __future__ import annotations

from enum import Enum


class FailureCode(str, Enum):
    E_BOOT_TIMEOUT        = "E_BOOT_TIMEOUT"
    E_ADB_UNAUTHORIZED    = "E_ADB_UNAUTHORIZED"
    E_DEVICE_OFFLINE      = "E_DEVICE_OFFLINE"
    E_UI_ELEMENT_MISSING  = "E_UI_ELEMENT_MISSING"
    E_UI_FIELD            = "E_UI_FIELD"
    E_APP_NOT_FOREGROUND  = "E_APP_NOT_FOREGROUND"
    E_CAPTCHA_UNSOLVED    = "E_CAPTCHA_UNSOLVED"
    E_CAPTCHA_TIMEOUT     = "E_CAPTCHA_TIMEOUT"
    E_SUBMIT_TIMEOUT      = "E_SUBMIT_TIMEOUT"
    E_RATELIMIT           = "E_RATELIMIT"
    E_NO_TOKEN            = "E_NO_TOKEN"
    E_TOKEN_INVALID       = "E_TOKEN_INVALID"
    E_EMAIL_TAKEN         = "E_EMAIL_TAKEN"
    E_EMAIL_INVALID       = "E_EMAIL_INVALID"
    E_USERNAME_TAKEN      = "E_USERNAME_TAKEN"
    E_DOB_FAIL            = "E_DOB_FAIL"
    E_NETWORK             = "E_NETWORK"
    E_PROXY_DEAD          = "E_PROXY_DEAD"
    E_VERIFY_TIMEOUT      = "E_VERIFY_TIMEOUT"
    E_APP_LOGGED_OUT      = "E_APP_LOGGED_OUT"
    E_DISK_FULL           = "E_DISK_FULL"
    E_ARCH_MISMATCH       = "E_ARCH_MISMATCH"
    E_APK_MISMATCH        = "E_APK_MISMATCH"
    E_CONFIG              = "E_CONFIG"
    E_SETUP               = "E_SETUP"
    E_HUMAN_ABORT         = "E_HUMAN_ABORT"
    E_UNKNOWN             = "E_UNKNOWN"


RETRYABLE = {
    FailureCode.E_BOOT_TIMEOUT, FailureCode.E_DEVICE_OFFLINE,
    FailureCode.E_NETWORK, FailureCode.E_PROXY_DEAD, FailureCode.E_RATELIMIT,
}


class DCGenError(Exception):
    def __init__(self, code: FailureCode, msg: str, *, detail: str | None = None):
        super().__init__(msg)
        self.code = code
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE


class HumanAbort(DCGenError):
    def __init__(self, msg: str = "aborted by human"):
        super().__init__(FailureCode.E_HUMAN_ABORT, msg)