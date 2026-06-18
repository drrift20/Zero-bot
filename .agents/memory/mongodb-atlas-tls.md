---
name: MongoDB Atlas TLS on Replit
description: Connecting motor/pymongo to Atlas from Replit's NixOS environment fails with TLSV1_ALERT_INTERNAL_ERROR — cause and fix.
---

## The Rule
When `motor.motor_asyncio.AsyncIOMotorClient` raises `SSL handshake failed: TLSV1_ALERT_INTERNAL_ERROR` connecting to MongoDB Atlas from Replit, the root cause is **Atlas Network Access blocking Replit's IP**, not an actual TLS/OpenSSL version mismatch.

## Why
Atlas returns TLS alert 80 (internal_error) as a deliberate rejection when the source IP is not whitelisted — it does not return a clean TCP RST. This looks exactly like an OpenSSL incompatibility but is actually a firewall-level block.

## How to Apply
1. Go to MongoDB Atlas → **Security** → **Network Access**.
2. Add `0.0.0.0/0` (Allow Access from Anywhere).
3. Wait ~30 seconds. Connection succeeds with default `tlsCAFile=certifi.where()`.
4. Do NOT change TLS version settings — TLS 1.3 works fine once the IP is allowed.

## What Does NOT Help
- `tlsAllowInvalidCertificates=True` (still blocked at network level)
- Patching `ssl.SSLContext` with cipher lists or version caps
- Using `OPENSSL_CONF` env var overrides
- Switching Python versions (all share the same NixOS OpenSSL 3.6.0)
