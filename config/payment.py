# -*- coding: utf-8 -*-
"""Payment-method qualification integration settings."""
from config.env_loader import apply_env_overrides

# Path to the requested standalone qualification-test checkout checker.  The
# vendored copy in core/ remains a fallback for deployments without the sibling
# checkout, while this default uses the sibling implementation when available.
PAYMENT_QUALIFICATION_PATH = "/home/yeyingxingchen/Xinghai/qualification-test"
# Optional HTTP API mode. When set, account checks POST to this base URL
# instead of importing/running the checker in this process.
PAYMENT_QUALIFICATION_API_BASE = ""
PAYMENT_QUALIFICATION_API_PATH = "/api/gcash/check"
PAYMENT_QUALIFICATION_API_KEY = ""
PAYMENT_METHOD_AUTO_CHECK_AFTER_REGISTER = False
PAYMENT_METHOD_CHECK_WORKERS = 2
PAYMENT_METHOD_CHECK_QUEUE_LIMIT = 200
PAYMENT_METHOD_CHECK_TIMEOUT = 180.0
PAYMENT_METHOD_CHECK_RETRIES = 2
PAYMENT_METHOD_CHECK_MIN_INTERVAL = 0.8
PAYMENT_METHOD_CHECK_REGIONS = "gcash,card,paypal_uk,paypal_nl,ideal_nl,momo_vn,gopay_id,upi_in,blik_pl,pix_br"
# Fallback proxy for all configured checkout regions.  The checker requires a
# country-capable proxy; this is intentionally separate from plan-check proxy.
PAYMENT_METHOD_CHECK_PROXY = ""
# Optional per-region overrides, one ``preset=proxy`` entry per line.  A
# ``default=proxy`` entry supplies the fallback for regions without an override.
PAYMENT_METHOD_CHECK_PROXIES = []

apply_env_overrides(globals(), {
    "PAYMENT_QUALIFICATION_PATH": "str",
    "PAYMENT_QUALIFICATION_API_BASE": "str",
    "PAYMENT_QUALIFICATION_API_PATH": "str",
    "PAYMENT_QUALIFICATION_API_KEY": "str",
    "PAYMENT_METHOD_AUTO_CHECK_AFTER_REGISTER": "bool",
    "PAYMENT_METHOD_CHECK_WORKERS": "int",
    "PAYMENT_METHOD_CHECK_QUEUE_LIMIT": "int",
    "PAYMENT_METHOD_CHECK_TIMEOUT": "float",
    "PAYMENT_METHOD_CHECK_RETRIES": "int",
    "PAYMENT_METHOD_CHECK_MIN_INTERVAL": "float",
    "PAYMENT_METHOD_CHECK_REGIONS": "str",
    "PAYMENT_METHOD_CHECK_PROXY": "str",
    "PAYMENT_METHOD_CHECK_PROXIES": "list_str_multiline",
})
