"""One TLS context for every outbound HTTP client.

httpx builds a fresh context (loading certifi's bundle) for each AsyncClient, which costs seconds on
Windows; building it once makes a new client nearly free.
"""
from __future__ import annotations

import functools
import ssl

import certifi


@functools.cache
def tls() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())
