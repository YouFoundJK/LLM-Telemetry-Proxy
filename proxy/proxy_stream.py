#!/usr/bin/env python3
"""
Upstream rate-limit sniffing, backoff calculation, and deferred streaming response processing.
Part of the LLM Telemetry Proxy.
"""

import sys
import time
import json
import random
import email.utils
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Union

import aiohttp
from aiohttp import web

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from proxy.telemetry_db import (
    log_call,
    log_proxy_call,
    _token_budget,
)
import proxy.payload_inspector as payload_inspector


def parse_retry_after(header_val: Optional[str]) -> Optional[float]:
    """Parse integer/float seconds or RFC 7231 HTTP-date from Retry-After header."""
    if not header_val:
        return None
    val_str = str(header_val).strip()
    if not val_str:
        return None
    try:
        sec = float(val_str)
        return max(0.0, sec)
    except (ValueError, TypeError):
        pass
    try:
        dt = email.utils.parsedate_to_datetime(val_str)
        if dt is not None:
            now_utc = datetime.now(timezone.utc)
            delta = (dt - now_utc).total_seconds()
            return max(0.0, delta)
    except Exception:
        pass
    return None


def evaluate_retry_condition(
    status_code: Optional[int],
    headers: Optional[Any],
    body_text_or_json: Optional[Any],
    attempt: int,
    retry_policy: Optional[dict],
) -> tuple:
    """
    Evaluates whether an upstream response qualifies as a transient rate-limit / capacity hiccup
    and calculates the retry delay (or 0.0s for immediate zero-delay retry).

    Returns:
        (should_retry: bool, retry_delay_seconds: float, reason: Optional[str])
    """
    if not retry_policy or not retry_policy.get("enabled", True):
        return False, 0.0, None

    retry_on_status = set(retry_policy.get("retry_on_status", [429, 502, 503, 504, 529]))
    retry_patterns = [p.lower() for p in retry_policy.get("retry_on_body_patterns", [])]
    retry_on_empty = bool(retry_policy.get("retry_on_empty", True))
    max_retry_after = float(retry_policy.get("max_retry_after_seconds", 10.0))
    mode = str(retry_policy.get("mode", "immediate")).lower()

    is_retryable = False
    reason = None

    if status_code in retry_on_status:
        is_retryable = True
        reason = f"HTTP {status_code}"

    # Check for empty response body (0 bytes / whitespace)
    if not is_retryable and retry_on_empty:
        if body_text_or_json is None:
            if status_code and status_code >= 400:
                is_retryable = True
                reason = f"HTTP {status_code} with no body"
        elif isinstance(body_text_or_json, (bytes, str)) and not body_text_or_json.strip():
            is_retryable = True
            reason = "Empty response returned from upstream (0 bytes)"

    # Check body content, error objects, and choices
    if not is_retryable and body_text_or_json:
        body_str = ""
        if isinstance(body_text_or_json, dict):
            err_val = body_text_or_json.get("error", "")
            detail_val = body_text_or_json.get("detail", "")
            msg_val = body_text_or_json.get("message", "")
            choices_val = ""
            choices = body_text_or_json.get("choices")
            if isinstance(choices, list):
                if len(choices) == 0 and retry_on_empty:
                    is_retryable = True
                    reason = "Empty choices list returned from upstream"
                elif len(choices) > 0 and isinstance(choices[0], dict):
                    msg = choices[0].get("message")
                    delta = choices[0].get("delta")
                    txt = choices[0].get("text")
                    c_text = None
                    tc_val = None
                    r_text = None
                    if isinstance(msg, dict):
                        c_text = msg.get("content")
                        tc_val = msg.get("tool_calls")
                        r_text = msg.get("reasoning_content")
                    elif isinstance(delta, dict):
                        c_text = delta.get("content")
                        tc_val = delta.get("tool_calls")
                        r_text = delta.get("reasoning_content")
                    elif isinstance(txt, str):
                        c_text = txt
                    
                    if c_text is not None:
                        choices_val = str(c_text)
                    
                    has_content = bool(c_text and str(c_text).strip()) or bool(r_text and str(r_text).strip()) or bool(tc_val)
                    if retry_on_empty and not has_content:
                        is_retryable = True
                        reason = "Empty content/no response returned in upstream choice"

            body_str = f"{err_val} {detail_val} {msg_val} {choices_val}".lower()
        elif isinstance(body_text_or_json, str):
            body_str = body_text_or_json.lower()
        elif isinstance(body_text_or_json, bytes):
            try:
                body_str = body_text_or_json.decode("utf-8", errors="ignore").lower()
            except Exception:
                pass

        if not is_retryable:
            for pat in retry_patterns:
                if pat in body_str:
                    is_retryable = True
                    reason = f"Body pattern match: '{pat}'"
                    break

    if not is_retryable:
        return False, 0.0, None

    max_retries = int(retry_policy.get("max_retries", 0))
    if attempt >= max_retries or max_retries <= 0:
        return False, 0.0, reason

    # Evaluate delay
    delay = 0.0
    retry_after_hdr = None
    if headers:
        for k, v in (headers.items() if hasattr(headers, "items") else []):
            if str(k).lower() in ("retry-after", "x-ratelimit-reset-requests", "ratelimit-reset-requests"):
                retry_after_hdr = v
                break

    parsed_after = parse_retry_after(retry_after_hdr) if retry_after_hdr else None
    if parsed_after is not None:
        if parsed_after > max_retry_after:
            return False, 0.0, None
        delay = parsed_after

    if delay == 0.0:
        if mode == "immediate":
            delay = 0.0
        else:
            delay = min(max_retry_after, 0.5 * (2 ** attempt)) + random.uniform(0.1, 0.4)

    return True, delay, reason


async def handle_streaming_upstream(
    request: web.Request,
    upstream_resp: aiohttp.ClientResponse,
    attempt: int,
    max_retries: int,
    retry_policy: dict,
    target_limiter: Any,
    req_id: str,
    model: str,
    path: str,
    method: str,
    call_type: str,
    payload: dict,
    req_seq: int,
    t_start: float,
    first_byte_timeout: float,
    route_name: str,
    active_upstream_url: str,
    server_running: Any,
    server_tok_s: Any,
    server_model: Any,
    tlog_fn: Any,
) -> Tuple[Optional[web.StreamResponse], bool, float, Optional[str]]:
    """
    Handles streaming responses with deferred client handshake and retry sniff detection.
    Returns: (response, retry_needed, retry_delay, retry_reason)
    """
    content_type = upstream_resp.headers.get("Content-Type", "")
    buffered_chunks = []
    stream_retry_needed = False
    stream_delay = 0.0
    stream_reason = None
    has_real_content = False

    # First-byte deadline to fail fast before client drops connection
    try:
        chunk_res = await asyncio.wait_for(
            upstream_resp.content.readchunk(),
            timeout=first_byte_timeout
        )
        first_chunk = chunk_res[0] if isinstance(chunk_res, tuple) else chunk_res
        if first_chunk:
            buffered_chunks.append(first_chunk)
    except asyncio.TimeoutError:
        tlog_fn(f"[telemetry] [TIMEOUT FIRST BYTE] req_id={req_id} model={model} upstream stalled for {first_byte_timeout}s without emitting any token chunk.")
        raise asyncio.TimeoutError(f"Upstream first-byte timeout ({first_byte_timeout}s)")
    except Exception:
        first_chunk = None

    if buffered_chunks:
        try:
            text = buffered_chunks[0].decode("utf-8", errors="replace")
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    chunk_data = json.loads(line[6:])
                    if isinstance(chunk_data, dict):
                        if chunk_data.get("error"):
                            s_needed, s_delay, s_reason = evaluate_retry_condition(
                                status_code=None,
                                headers=upstream_resp.headers,
                                body_text_or_json=chunk_data,
                                attempt=attempt,
                                retry_policy=retry_policy,
                            )
                            if s_needed:
                                stream_retry_needed = True
                                stream_delay = s_delay
                                stream_reason = s_reason
                                break
                        
                        choices = chunk_data.get("choices")
                        if isinstance(choices, list) and len(choices) > 0 and isinstance(choices[0], dict):
                            delta = choices[0].get("delta")
                            txt = choices[0].get("text")
                            if isinstance(delta, dict):
                                c_str = delta.get("content")
                                r_str = delta.get("reasoning_content")
                                t_calls = delta.get("tool_calls")
                                f_call = delta.get("function_call")
                                if (c_str and str(c_str).strip()) or (r_str and str(r_str).strip()) or t_calls or f_call:
                                    has_real_content = True
                                    break
                            elif txt and str(txt).strip():
                                has_real_content = True
                                break
        except Exception:
            pass

    # Read further chunks if needed to determine content
    if not stream_retry_needed and not has_real_content and buffered_chunks:
        async for chunk in upstream_resp.content:
            buffered_chunks.append(chunk)
            try:
                text = chunk.decode("utf-8", errors="replace")
                for line in text.split("\n"):
                    line = line.strip()
                    if line.startswith("data: ") and line != "data: [DONE]":
                        chunk_data = json.loads(line[6:])
                        if isinstance(chunk_data, dict):
                            if chunk_data.get("error"):
                                s_needed, s_delay, s_reason = evaluate_retry_condition(
                                    status_code=None,
                                    headers=upstream_resp.headers,
                                    body_text_or_json=chunk_data,
                                    attempt=attempt,
                                    retry_policy=retry_policy,
                                )
                                if s_needed:
                                    stream_retry_needed = True
                                    stream_delay = s_delay
                                    stream_reason = s_reason
                                    break
                            
                            choices = chunk_data.get("choices")
                            if isinstance(choices, list) and len(choices) > 0 and isinstance(choices[0], dict):
                                delta = choices[0].get("delta")
                                txt = choices[0].get("text")
                                if isinstance(delta, dict):
                                    c_str = delta.get("content")
                                    r_str = delta.get("reasoning_content")
                                    t_calls = delta.get("tool_calls")
                                    f_call = delta.get("function_call")
                                    if (c_str and str(c_str).strip()) or (r_str and str(r_str).strip()) or t_calls or f_call:
                                        has_real_content = True
                                        break
                                elif txt and str(txt).strip():
                                    has_real_content = True
                                    break
            except Exception:
                pass

            if stream_retry_needed or has_real_content:
                break

    # If upstream stream ended with 0 content / 0 tool calls
    if not stream_retry_needed and not has_real_content and retry_policy.get("retry_on_empty", True):
        stream_retry_needed = True
        stream_delay = 0.0
        stream_reason = "Upstream model returned empty content (0 tokens in stream)"

    if stream_retry_needed and attempt < max_retries:
        target_limiter.record_retry_attempt()
        tlog_fn(
            f"[telemetry] [RETRY TRIGGERED] req_id={req_id} model={model} path={path} "
            f"reason='{stream_reason}' attempt={attempt+1}/{max_retries+1}. Re-dispatching stream with delay={stream_delay:.2f}s..."
        )
        return None, True, stream_delay, stream_reason

    if attempt > 0:
        target_limiter.record_retry_absorbed()
        tlog_fn(
            f"[telemetry] [RETRY ABSORBED] req_id={req_id} model={model} path={path} "
            f"recovered valid stream output on attempt {attempt+1}/{max_retries+1}!"
        )

    stream_headers = {
        "Content-Type": content_type,
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Proxy-Retries-Attempted": str(attempt),
        "X-Proxy-Rate-Limit-Absorbed": "1" if attempt > 0 else "0",
    }
    response = web.StreamResponse(
        status=upstream_resp.status,
        headers=stream_headers,
    )
    await response.prepare(request)

    t_first_byte = None
    collected_usage = None
    content_chars = 0
    collected_content = ""
    collected_reasoning = ""
    collected_tool_calls = []
    error = None
    input_tokens = None
    output_tokens = None
    reasoning_tokens = None
    tokens_per_s = None

    def _parse_sse_chunk(raw_bytes: bytes):
        nonlocal t_first_byte, collected_usage, content_chars, collected_content, collected_reasoning, error
        if t_first_byte is None:
            t_first_byte = time.monotonic()
        try:
            text = raw_bytes.decode("utf-8", errors="replace")
            for line in text.split("\n"):
                if line.startswith("data: ") and line.strip() != "data: [DONE]":
                    try:
                        chunk_data = json.loads(line[6:])
                        if isinstance(chunk_data, dict):
                            if chunk_data.get("usage") and isinstance(chunk_data["usage"], dict):
                                collected_usage = chunk_data["usage"]
                            choices = chunk_data.get("choices")
                            if isinstance(choices, list):
                                for choice in choices:
                                    if isinstance(choice, dict):
                                        delta = choice.get("delta")
                                        if isinstance(delta, dict):
                                            if delta.get("content"):
                                                content_chars += len(delta["content"])
                                                collected_content += delta["content"]
                                            if delta.get("reasoning_content"):
                                                content_chars += len(delta["reasoning_content"])
                                                collected_reasoning += delta["reasoning_content"]
                            if chunk_data.get("error"):
                                err_obj = chunk_data["error"]
                                if isinstance(err_obj, dict):
                                    error = err_obj.get("message") or err_obj.get("type") or str(err_obj)
                                else:
                                    error = str(err_obj)
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass

    client_disconnected = False
    stream_error = None
    try:
        for b_chunk in buffered_chunks:
            await response.write(b_chunk)
            _parse_sse_chunk(b_chunk)

        async for chunk in upstream_resp.content:
            await response.write(chunk)
            _parse_sse_chunk(chunk)

        await response.write_eof()
    except (RuntimeError, ConnectionResetError, BrokenPipeError, AssertionError, ConnectionError, OSError) as client_disconn_err:
        client_disconnected = True
        disconn_detail = str(client_disconn_err).strip() or type(client_disconn_err).__name__
        error = f"client_cancelled: {disconn_detail}"
        tlog_fn(f"[telemetry] [CLIENT DISCONNECTED] req_id={req_id} model={model}: {disconn_detail}")
    except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as up_err:
        stream_error = up_err
        if isinstance(up_err, asyncio.TimeoutError):
            err_detail = str(up_err).strip() or "read timeout"
            error = f"upstream_stream_timeout: {err_detail}"
        elif isinstance(up_err, aiohttp.ClientError):
            err_detail = str(up_err).strip() or type(up_err).__name__
            error = f"upstream_network_error: {err_detail}"
        else:
            err_detail = str(up_err).strip() or type(up_err).__name__
            error = f"upstream_stream_error: {err_detail}"
        tlog_fn(f"[telemetry] [UPSTREAM STREAM ERROR] req_id={req_id} model={model}: {error}")

    if stream_error and not client_disconnected:
        try:
            err_code = 504 if isinstance(stream_error, asyncio.TimeoutError) else 502
            err_msg = str(stream_error).strip() or type(stream_error).__name__
            sse_err = json.dumps({
                "error": {
                    "message": f"Upstream stream error: {err_msg}",
                    "type": "upstream_stream_error",
                    "code": err_code,
                }
            })
            await response.write(f"data: {sse_err}\n\n".encode("utf-8"))
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
        except (RuntimeError, ConnectionResetError, BrokenPipeError, AssertionError, ConnectionError, OSError) as write_err:
            client_disconnected = True
            disconn_detail = str(write_err).strip() or type(write_err).__name__
            error = f"client_cancelled: {disconn_detail}"

    if client_disconnected:
        t_total = (time.monotonic() - t_start) * 1000
        if t_first_byte is None:
            t_first_byte = time.monotonic()
        ttfb_ms = (t_first_byte - t_start) * 1000
        try:
            log_call(model, path, input_tokens, output_tokens,
                     ttfb_ms, t_total, tokens_per_s,
                     server_running, server_tok_s, server_model,
                     499, error, call_type,
                     route_name=route_name, upstream_url=active_upstream_url,
                     retries_attempted=attempt, absorbed_429=1 if attempt > 0 else 0)
            log_proxy_call(path, method, call_type, model, 499, error, 1, ttfb_ms, t_total,
                           route_name=route_name, upstream_url=active_upstream_url,
                           retries_attempted=attempt, absorbed_429=1 if attempt > 0 else 0)
            if payload_inspector._raw_logging_enabled:
                raw_record = payload_inspector.make_raw_payload_record(
                    req_id=req_id, path=path, method=method, call_type=call_type, model=model,
                    client_ip=request.remote, req_headers=dict(request.headers), payload_obj=payload,
                    status_code=499, resp_headers=dict(upstream_resp.headers), is_stream=True,
                    ttfb_ms=ttfb_ms, total_ms=t_total, tokens_per_s=tokens_per_s,
                    input_tokens=input_tokens, output_tokens=output_tokens, reasoning_tokens=reasoning_tokens,
                    content_text=collected_content, reasoning_text=collected_reasoning,
                    tool_calls=collected_tool_calls if collected_tool_calls else None,
                    raw_resp_json=None, error=error, seq=req_seq,
                )
                payload_inspector.append_raw_payload(raw_record)
                if payload_inspector._raw_subscribers:
                    asyncio.create_task(payload_inspector.broadcast_raw_payload(raw_record))
        except Exception:
            pass
        return response, False, 0.0, None

    status_code = upstream_resp.status
    if stream_error:
        status_code = 504 if isinstance(stream_error, asyncio.TimeoutError) else 502

    # Telemetry post-processing
    try:
        if status_code and (status_code < 200 or status_code >= 300) and not error:
            error = f"HTTP {status_code}"

        if collected_usage and isinstance(collected_usage, dict):
            input_tokens = collected_usage.get("prompt_tokens", input_tokens)
            output_tokens = collected_usage.get("completion_tokens")
            details = collected_usage.get("completion_tokens_details")
            if isinstance(details, dict):
                reasoning_tokens = details.get("reasoning_tokens")
            else:
                reasoning_tokens = collected_usage.get("reasoning_tokens")
            if not output_tokens or output_tokens == 0:
                output_tokens = max(1, content_chars // 4)
        elif content_chars > 0 and not output_tokens:
            output_tokens = max(1, content_chars // 4)

        # Record token budget
        try:
            _token_budget.record_and_check(input_tokens or 0, output_tokens or 0)
        except Exception as b_err:
            print(f"[telemetry] token budget record error: {b_err}", file=sys.stderr)

        t_total = (time.monotonic() - t_start) * 1000
        if t_first_byte is None:
            t_first_byte = time.monotonic()
        ttfb_ms = (t_first_byte - t_start) * 1000
        if output_tokens and t_total > 0:
            tokens_per_s = output_tokens / (t_total / 1000)

        log_call(model, path, input_tokens, output_tokens,
                 ttfb_ms, t_total, tokens_per_s,
                 server_running, server_tok_s, server_model,
                 status_code, error, call_type,
                 route_name=route_name, upstream_url=active_upstream_url,
                 retries_attempted=attempt, absorbed_429=1 if attempt > 0 else 0)
        log_proxy_call(path, method, call_type, model, status_code, error, 1, ttfb_ms, t_total,
                       route_name=route_name, upstream_url=active_upstream_url,
                       retries_attempted=attempt, absorbed_429=1 if attempt > 0 else 0)

        if payload_inspector._raw_logging_enabled:
            raw_record = payload_inspector.make_raw_payload_record(
                req_id=req_id,
                path=path,
                method=method,
                call_type=call_type,
                model=model,
                client_ip=request.remote,
                req_headers=dict(request.headers),
                payload_obj=payload,
                status_code=status_code,
                resp_headers=dict(upstream_resp.headers),
                is_stream=True,
                ttfb_ms=ttfb_ms,
                total_ms=t_total,
                tokens_per_s=tokens_per_s,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                content_text=collected_content,
                reasoning_text=collected_reasoning,
                tool_calls=collected_tool_calls if collected_tool_calls else None,
                raw_resp_json=None,
                error=error,
                seq=req_seq,
            )
            payload_inspector.append_raw_payload(raw_record)
            if payload_inspector._raw_subscribers:
                asyncio.create_task(payload_inspector.broadcast_raw_payload(raw_record))
    except Exception as tel_err:
        print(f"[telemetry] streaming telemetry error: {tel_err}", file=sys.stderr)

    return response, False, 0.0, None
