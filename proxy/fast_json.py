# -*- coding: utf-8 -*-
"""
High-Performance JSON Abstraction Layer

Provides SIMD-accelerated serialization and deserialization via orjson when available,
with transparent and 100% compatible fallback to standard library json.

Part of the LLM Telemetry Proxy.
"""

import sys
from typing import Any, Union, Optional

try:
    import orjson
    HAS_ORJSON = True
except ImportError:
    orjson = None
    HAS_ORJSON = False

import json


def json_loads(data: Union[str, bytes, bytearray, memoryview]) -> Any:
    """
    Parse JSON from str, bytes, bytearray, or memoryview.
    Utilizes Rust SIMD instructions when orjson is installed.
    """
    if HAS_ORJSON:
        return orjson.loads(data)
    if isinstance(data, (bytes, bytearray, memoryview)):
        data = bytes(data).decode("utf-8")
    return json.loads(data)


def json_dumps(
    obj: Any,
    indent: Optional[int] = None,
    ensure_ascii: bool = True,
    default: Optional[Any] = None,
) -> str:
    """
    Serialize obj to a JSON-formatted string.
    """
    if HAS_ORJSON:
        option = 0
        if indent:
            option |= orjson.OPT_INDENT_2
        b = orjson.dumps(obj, default=default, option=option)
        return b.decode("utf-8")
    return json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii, default=default)


def json_dumps_bytes(
    obj: Any,
    indent: Optional[int] = None,
    default: Optional[Any] = None,
) -> bytes:
    """
    Serialize obj directly to UTF-8 encoded bytes.
    Zero-copy in orjson; avoids string allocation + re-encoding.
    """
    if HAS_ORJSON:
        option = 0
        if indent:
            option |= orjson.OPT_INDENT_2
        return orjson.dumps(obj, default=default, option=option)
    return json.dumps(obj, indent=indent, default=default).encode("utf-8")


# Common aliases
loads = json_loads
dumps = json_dumps
dumps_bytes = json_dumps_bytes
