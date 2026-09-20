"""Every number and address that comes from Intuit, with where it comes from.
Confirmed by the owner on 2026-09-20 (F05 plan, question 3)."""

# Minor versions 1–74 were discontinued on 2025-08-01; a lower or missing value is
# answered as 75.
# https://developer.intuit.com/app/developer/qbo/docs/learn/explore-the-quickbooks-online-api/minor-versions
MINOR_VERSION = 75

# OAuth 2.0 endpoints (Intuit discovery document; the same for sandbox and production).
# https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0
AUTHORIZATION_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
SCOPE = "com.intuit.quickbooks.accounting"

# QBO_ENVIRONMENT → API base. Development keys reach sandbox companies only (D-25).
API_BASE_URLS: dict[str, str] = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}

# Throttle: 500 requests a minute per realm, 10 concurrent per app and realm, HTTP
# 429 beyond either; Intuit advises single-threaded calls per realm. This client
# keeps one request in flight per connection.
# https://help.developer.intuit.com/s/article/API-call-limits-and-throttling
MAX_TRIES = 5
BACKOFF_FIRST_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0

# Timeouts (seconds).
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 30.0
TOKEN_TIMEOUT = 10.0

# The access token is refreshed when it has less than this left. Lifetimes are
# never assumed: both expiries are read from each token response.
# https://help.developer.intuit.com/s/article/Validity-of-Refresh-Token
REFRESH_MARGIN_SECONDS = 300

# The pending OAuth ``state`` (F05 brief).
STATE_TTL_MINUTES = 10

# Response header that identifies a call to Intuit support; safe to log.
TID_HEADER = "intuit_tid"
