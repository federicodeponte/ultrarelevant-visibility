"""Quick sanity tests for fixes 1 and 2."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

# We need to monkeypatch GEMINI_KEY so the module loads without errors
import importlib, types

# Patch _load_secret before import to avoid real key loading issues
import local_engine as eng

PASS = 0
FAIL = 0

def check(name, condition):
    global PASS, FAIL
    if condition:
        print(f"PASS: {name}")
        PASS += 1
    else:
        print(f"FAIL: {name}")
        FAIL += 1

# --- Fix 1: _safe_err redacts key= params ---
class FakeException(Exception):
    pass

e1 = FakeException("POST https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro?key=AIzaSyABCDEFGH123456 failed")
result1 = eng._safe_err(e1)
check("_safe_err redacts ?key= param", "key=REDACTED" in result1)
check("_safe_err does not leak raw key", "AIzaSyABCDEFGH123456" not in result1)
check("_safe_err preserves exception type", result1.startswith("FakeException:"))

e2 = FakeException("https://example.com/path?foo=bar&key=AIzaSyXYZ&baz=qux")
result2 = eng._safe_err(e2)
check("_safe_err redacts &key= param", "key=REDACTED" in result2 and "AIzaSyXYZ" not in result2)
check("_safe_err preserves other params", "foo=bar" in result2)

e3 = FakeException("connection refused")
result3 = eng._safe_err(e3)
check("_safe_err no-op on clean message", "connection refused" in result3)

# --- Fix 2: _is_ssrf_blocked rejects metadata IP ---
# 169.254.169.254 is AWS/GCP metadata endpoint (link-local)
# We mock getaddrinfo to avoid actual DNS
import socket as _socket
import ipaddress

orig_getaddrinfo = _socket.getaddrinfo

def mock_getaddrinfo_metadata(host, port, *args, **kwargs):
    return [(None, None, None, None, ("169.254.169.254", 0))]

def mock_getaddrinfo_localhost(host, port, *args, **kwargs):
    return [(None, None, None, None, ("127.0.0.1", 0))]

def mock_getaddrinfo_private(host, port, *args, **kwargs):
    return [(None, None, None, None, ("10.0.0.1", 0))]

def mock_getaddrinfo_public(host, port, *args, **kwargs):
    return [(None, None, None, None, ("1.2.3.4", 0))]

# Test 169.254.169.254 (link-local metadata)
_socket.getaddrinfo = mock_getaddrinfo_metadata
check("_is_ssrf_blocked: 169.254.169.254 blocked", eng._is_ssrf_blocked("metadata.internal") == True)

# Test 127.0.0.1 (loopback)
_socket.getaddrinfo = mock_getaddrinfo_localhost
check("_is_ssrf_blocked: 127.0.0.1 blocked", eng._is_ssrf_blocked("localhost") == True)

# Test 10.x.x.x (private)
_socket.getaddrinfo = mock_getaddrinfo_private
check("_is_ssrf_blocked: 10.0.0.1 blocked", eng._is_ssrf_blocked("internal.corp") == True)

# Test public IP
_socket.getaddrinfo = mock_getaddrinfo_public
check("_is_ssrf_blocked: public IP allowed", eng._is_ssrf_blocked("example.com") == False)

_socket.getaddrinfo = orig_getaddrinfo

# --- Fix 3: skf.com not in DISTRIBUTOR_DOMAINS ---
check("skf.com NOT in DISTRIBUTOR_DOMAINS", "skf.com" not in eng.DISTRIBUTOR_DOMAINS)

# --- Fix 5: two_part_tlds expansion ---
# Re-read the set from the function by calling domain_root with test cases
check("co.nz recognized", eng.domain_root("www.example.co.nz") == "example.co.nz")
check("co.id recognized", eng.domain_root("www.example.co.id") == "example.co.id")
check("com.sg recognized", eng.domain_root("www.example.com.sg") == "example.com.sg")
check("com.my recognized", eng.domain_root("www.example.com.my") == "example.com.my")
check("com.hk recognized", eng.domain_root("www.example.com.hk") == "example.com.hk")
check("or.jp recognized", eng.domain_root("www.example.or.jp") == "example.or.jp")
check("or.kr recognized", eng.domain_root("www.example.or.kr") == "example.or.kr")
check("re.kr recognized", eng.domain_root("www.example.re.kr") == "example.re.kr")

print(f"\n{'='*40}")
print(f"Results: {PASS} PASS, {FAIL} FAIL")
if FAIL:
    sys.exit(1)
