"""Unit tests for keys/identity (FR-1) and the SSRF guard (FR-11)."""

import socket

from cortex.security import (
    generate_key,
    hash_secret,
    is_private_ip,
    key_agent_id,
    verify_secret,
)


def test_key_roundtrip_and_agent_id_extraction():
    key = generate_key("goose")
    assert key.startswith("cx-goose-")
    assert key_agent_id(key) == "goose"


def test_key_agent_id_rejects_malformed():
    assert key_agent_id("not-a-key") is None
    assert key_agent_id("cx-") is None


def test_hash_verify_roundtrip():
    key = generate_key("dsh")
    assert verify_secret(key, hash_secret(key))
    assert not verify_secret(key + "x", hash_secret(key))
    assert not verify_secret("cx-dsh-other", hash_secret(key))


def test_private_ip_ranges():
    for ip in ("127.0.0.1", "192.168.1.10", "10.0.0.5", "172.16.0.1",
               "169.254.169.254", "::1", "fe80::1", "0.0.0.0", "224.0.0.1"):
        assert is_private_ip(ip), ip
    for ip in ("8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"):
        assert not is_private_ip(ip), ip


def test_localhost_name_resolution_is_caught_by_capture_guard():
    from cortex.capture import SsrfBlocked, assert_public_url

    # localhost + a dotted-decimal private IP + a metadata endpoint: all refused
    for url in ("http://localhost:8738/healthz",
                "http://192.168.1.5/admin",
                "http://169.254.169.254/latest/meta-data/",
                "http://127.0.0.1/", "file:///etc/passwd"):
        try:
            assert_public_url(url)
            raise AssertionError(f"expected SsrfBlocked for {url}")
        except SsrfBlocked:
            pass
