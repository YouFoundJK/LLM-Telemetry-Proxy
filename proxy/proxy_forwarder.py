#!/usr/bin/env python3
"""
Inference request lifecycle, non-streaming execution, concurrency gating, and simple pass-through forwarding.
Part of the LLM Telemetry Proxy.
"""

import sys
import time
import json
import uuid
import random
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Union

import aiohttp
from aiohttp import web

try:
    from proxy.repo_paths import resolve_repo_root, REPO_ROOT
except ImportError:
    from repo_paths import resolve_repo_root, REPO_ROOT

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from proxy.fast_json import json_loads, json_dumps, json_dumps_bytes
except ImportError:
    from fast_json import json_loads, json_dumps, json_dumps_bytes

try:
    from proxy.model_router import build_upstream_url, apply_upstream_api_key, AcquiredSlot
except ImportError:
    from model_router import build_upstream_url, apply_upstream_api_key, AcquiredSlot

from proxy.telemetry_db import (
    classify_endpoint,
    fetch_server_load,
    log_call,
    log_proxy_call,
    _token_budget,
)
from proxy.proxy_stream import (
    evaluate_retry_condition,
    handle_streaming_upstream,
)
import proxy.payload_inspector as payload_inspector

DEFAULT_UPSTREAM = "https://llm.ai.e-infra.cz/v1"
UPSTREAM = DEFAULT_UPSTREAM
_model_router = None
_upstream_session_key = "upstream_session"
_retry_429_max = 3
_tlog_fn = lambda msg: print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def set_forwarder_globals(router, upstream, session_key, retry_max, tlog):
    global _model_router, UPSTREAM, _upstream_session_key, _retry_429_max, _tlog_fn
    _model_router = router
    UPSTREAM = upstream
    _upstream_session_key = session_key
    _retry_429_max = retry_max
    _tlog_fn = tlog


def _extract_prompt_tokens(payload: dict) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    prompt_text = ""
    if "messages" in payload and isinstance(payload["messages"], list):
        for msg in payload["messages"]:
            if isinstance(msg, dict):
                c = msg.get("content", "")
                if isinstance(c, str):
                    prompt_text += c + " "
                elif isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and part.get("text"):
                            prompt_text += str(part["text"]) + " "
    elif "prompt" in payload:
        p = payload["prompt"]
        prompt_text = p if isinstance(p, str) else (" ".join(str(x) for x in p) if isinstance(p, list) else "")
    elif "input" in payload:
        inp = payload["input"]
        prompt_text = inp if isinstance(inp, str) else (" ".join(str(x) for x in inp) if isinstance(inp, list) else "")
    return max(1, len(prompt_text) // 4) if prompt_text else None


def _log_forward_failure(model, path, method, call_type, input_tokens, output_tokens, reasoning_tokens,
                         ttfb_ms, t_start, status_code, error, route_name, upstream_url,
                         server_running, server_tok_s, server_model, req_id, req_headers,
                         payload, is_stream_req, req_seq, client_ip, logged):
    t_total = (time.monotonic() - t_start) * 1000
    try:
        log_call(model, path, input_tokens, output_tokens, ttfb_ms, t_total, None,
                 server_running, server_tok_s, server_model, status_code, error, call_type,
                 route_name=route_name, upstream_url=upstream_url)
        log_proxy_call(path, method, call_type, model, status_code, error, 1 if logged else 0, ttfb_ms, t_total,
                       route_name=route_name, upstream_url=upstream_url)
    except Exception:
        pass

    if payload_inspector._raw_logging_enabled:
        err_record = payload_inspector.make_raw_payload_record(
            req_id=req_id, path=path, method=method, call_type=call_type, model=model,
            client_ip=client_ip, req_headers=dict(req_headers) if req_headers else {},
            payload_obj=payload, status_code=status_code, resp_headers={}, is_stream=is_stream_req,
            ttfb_ms=ttfb_ms, total_ms=t_total, tokens_per_s=None,
            input_tokens=input_tokens, output_tokens=output_tokens, reasoning_tokens=reasoning_tokens,
            content_text="", reasoning_text="", tool_calls=None, raw_resp_json=None,
            error=error, seq=req_seq,
        )
        payload_inspector.append_raw_payload(err_record)
        if payload_inspector._raw_subscribers:
            asyncio.create_task(payload_inspector.broadcast_raw_payload(err_record))


async def handle_proxy(request: web.Request) -> web.StreamResponse:
    path = request.path
    method = request.method
    call_type = classify_endpoint(path)

    if call_type in ("model_list", "model_info", "props", "other"):
        return await _simple_forward(request, path, method)

    body = await request.read()
    model, input_tokens, payload = None, None, None
    try:
        if body:
            payload = json_loads(body)
            model = payload.get("model")
            if payload.get("stream") and not payload.get("stream_options"):
                payload["stream_options"] = {"include_usage": True}
                body = json_dumps_bytes(payload)
            input_tokens = _extract_prompt_tokens(payload)
    except Exception:
        pass

    route_candidates = _model_router.resolve_chain(model)
    admit_route, admit_limiter, admit_already_acquired = await _model_router.select_admission_route(route_candidates)
    route_sequence = [admit_route] + [r for r in route_candidates if r.route_id != admit_route.route_id]

    req_id = f"req_{uuid.uuid4().hex[:12]}"
    req_seq = payload_inspector.next_raw_payload_seq()
    t_start = time.monotonic()
    ttfb_ms, status_code, error, output_tokens, reasoning_tokens, tokens_per_s = None, None, None, None, None, None
    logged, headers_prepared, response = False, False, None
    is_stream_req = bool(payload.get("stream") if isinstance(payload, dict) else False)

    if payload_inspector._raw_logging_enabled and payload_inspector._raw_subscribers:
        start_record = payload_inspector.make_raw_payload_start_record(
            req_id=req_id, path=path, method=method, call_type=call_type, model=model,
            client_ip=request.remote, req_headers=dict(request.headers), payload_obj=payload,
            is_stream=is_stream_req, seq=req_seq,
        )
        asyncio.create_task(payload_inspector.broadcast_raw_payload(start_record))

    try:
        for route_idx, route_res in enumerate(route_sequence):
            route_name = route_res.route_name
            resolved_base = UPSTREAM if (route_res.is_default and UPSTREAM != DEFAULT_UPSTREAM) else route_res.upstream_url
            upstream_url = build_upstream_url(resolved_base, path)

            is_einfra = ("e-infra.cz" in resolved_base.lower()) or (resolved_base.strip() == DEFAULT_UPSTREAM.strip())
            if is_einfra:
                server_running, server_tok_s, server_model = await fetch_server_load(model)
            else:
                server_running, server_tok_s, server_model = None, None, None

            headers = dict(request.headers)
            headers.pop("Host", None)
            headers.pop("host", None)
            if body and "Content-Length" in headers:
                headers["Content-Length"] = str(len(body))
            headers = apply_upstream_api_key(headers, route_res.api_key)

            route_timeout = route_res.timeout or _model_router.default_timeout
            connect_timeout = float(route_timeout.get("connect", 10.0))
            first_byte_timeout = float(route_timeout.get("first_byte", 25.0))
            sock_read_timeout = float(route_timeout.get("sock_read", 60.0))
            total_timeout = float(route_timeout.get("total", 180.0))
            req_timeout = aiohttp.ClientTimeout(connect=connect_timeout, sock_read=sock_read_timeout, total=total_timeout)

            target_limiter = _model_router.get_limiter(route_res.route_id)
            retry_policy = dict(route_res.retry_policy or _model_router.default_retry_policy)
            if route_res.is_default:
                if _retry_429_max == 0:
                    retry_policy["enabled"] = False
                    retry_policy["max_retries"] = 0
                else:
                    retry_policy["max_retries"] = _retry_429_max

            max_retries = int(retry_policy.get("max_retries", 0)) if retry_policy.get("enabled", True) else 0
            _tlog_fn(f"[telemetry] [REQ START] req_id={req_id} model={model} path={path} is_stream={is_stream_req} max_retries={max_retries} max_rpm={route_res.max_rpm} route='{route_name}' upstream='{upstream_url}'")

            attempt = 0
            active_upstream_url = upstream_url
            cascade_to_next = False
            cascade_reason = None

            while attempt <= max_retries:
                retry_needed, retry_delay, retry_reason = False, 0.0, None
                if attempt > 0 and route_res.fallback_upstream_url:
                    active_upstream_url = build_upstream_url(route_res.fallback_upstream_url, path)
                    _tlog_fn(f"[telemetry] [FALLBACK ROUTING] req_id={req_id} attempt {attempt+1} directed to fallback: {active_upstream_url}")

                is_initial_acquired = (route_idx == 0 and attempt == 0 and admit_already_acquired)
                slot_context = AcquiredSlot(target_limiter, already_acquired=is_initial_acquired)

                async with slot_context:
                    req_session = request.app.get(_upstream_session_key) if hasattr(request, "app") and _upstream_session_key in request.app else None
                    owns_session = False
                    if req_session is None or req_session.closed:
                        req_session = aiohttp.ClientSession(timeout=req_timeout)
                        owns_session = True

                    try:
                        async with req_session.request(
                            method, active_upstream_url, headers=headers,
                            data=body if body else None, params=request.query, timeout=req_timeout,
                        ) as upstream_resp:
                            status_code = upstream_resp.status
                            content_type = upstream_resp.headers.get("Content-Type", "")
                            is_stream_candidate = "text/event-stream" in content_type and status_code == 200

                            sniff_needed, sniff_delay, sniff_reason = evaluate_retry_condition(
                                status_code=status_code, headers=upstream_resp.headers,
                                body_text_or_json=None, attempt=attempt, retry_policy=retry_policy,
                            )

                            if sniff_needed and attempt < max_retries:
                                target_limiter.record_retry_attempt()
                                _tlog_fn(f"[telemetry] [RETRY TRIGGERED] req_id={req_id} model={model} path={path} reason='{sniff_reason}' attempt={attempt+1}/{max_retries+1}. Re-dispatching with delay={sniff_delay:.2f}s...")
                                retry_needed, retry_delay, retry_reason = True, sniff_delay, sniff_reason

                            elif sniff_reason and attempt >= max_retries and route_idx < len(route_sequence) - 1:
                                target_limiter.record_retry_failed()
                                cascade_to_next = True
                                cascade_reason = sniff_reason
                                break

                            elif is_stream_candidate:
                                s_resp, s_retry, s_delay, s_reason = await handle_streaming_upstream(
                                    request=request, upstream_resp=upstream_resp, attempt=attempt,
                                    max_retries=max_retries, retry_policy=retry_policy, target_limiter=target_limiter,
                                    req_id=req_id, model=model, path=path, method=method, call_type=call_type,
                                    payload=payload, req_seq=req_seq, t_start=t_start, first_byte_timeout=first_byte_timeout,
                                    route_name=route_name, active_upstream_url=active_upstream_url,
                                    server_running=server_running, server_tok_s=server_tok_s, server_model=server_model,
                                    tlog_fn=_tlog_fn,
                                )
                                if s_retry and attempt < max_retries:
                                    retry_needed, retry_delay, retry_reason = True, s_delay, s_reason
                                elif s_retry and attempt >= max_retries:
                                    if route_idx < len(route_sequence) - 1:
                                        cascade_to_next = True
                                        cascade_reason = s_reason
                                        break
                                    return s_resp
                                else:
                                    return s_resp

                            else:
                                resp_body = await upstream_resp.read()
                                t_first_byte = time.monotonic()
                                ttfb_ms = (t_first_byte - t_start) * 1000
                                t_total = (time.monotonic() - t_start) * 1000

                                resp_data = None
                                try:
                                    resp_data = json_loads(resp_body)
                                except Exception:
                                    pass

                                b_needed, b_delay, b_reason = evaluate_retry_condition(
                                    status_code=status_code, headers=upstream_resp.headers,
                                    body_text_or_json=resp_data if resp_data else resp_body,
                                    attempt=attempt, retry_policy=retry_policy,
                                )

                                if b_needed and attempt < max_retries:
                                    target_limiter.record_retry_attempt()
                                    _tlog_fn(f"[telemetry] [RETRY TRIGGERED] req_id={req_id} model={model} path={path} reason='{b_reason}' attempt={attempt+1}/{max_retries+1}. Re-dispatching with delay={b_delay:.2f}s...")
                                    retry_needed, retry_delay, retry_reason = True, b_delay, b_reason
                                elif b_reason and attempt >= max_retries and route_idx < len(route_sequence) - 1:
                                    target_limiter.record_retry_failed()
                                    cascade_to_next = True
                                    cascade_reason = b_reason
                                    break
                                else:
                                    if attempt > 0:
                                        if status_code and 200 <= status_code < 300:
                                            target_limiter.record_retry_absorbed()
                                            _tlog_fn(f"[telemetry] [RETRY ABSORBED] req_id={req_id} model={model} path={path} recovered valid response on attempt {attempt+1}/{max_retries+1}!")
                                        else:
                                            target_limiter.record_retry_failed()
                                            _tlog_fn(f"[telemetry] [RETRY FAILED] req_id={req_id} model={model} path={path} failed on attempt {attempt+1}/{max_retries+1} (status={status_code})")

                                    resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")}
                                    resp_headers["X-Proxy-Retries-Attempted"] = str(attempt)
                                    resp_headers["X-Proxy-Rate-Limit-Absorbed"] = "1" if (attempt > 0 and status_code and 200 <= status_code < 300) else "0"

                                    budget_headers, allowed = {}, True
                                    try:
                                        resp_text, resp_reasoning, resp_tool_calls = None, None, None
                                        if isinstance(resp_data, dict):
                                            u = resp_data.get("usage")
                                            if isinstance(u, dict):
                                                input_tokens = u.get("prompt_tokens", input_tokens)
                                                output_tokens = u.get("completion_tokens")
                                                details = u.get("completion_tokens_details")
                                                reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else u.get("reasoning_tokens")
                                            choices = resp_data.get("choices")
                                            if isinstance(choices, list) and len(choices) > 0 and isinstance(choices[0], dict):
                                                msg = choices[0].get("message")
                                                if isinstance(msg, dict):
                                                    resp_text, resp_reasoning, resp_tool_calls = msg.get("content"), msg.get("reasoning_content"), msg.get("tool_calls")
                                                if not resp_text and "text" in choices[0]:
                                                    resp_text = choices[0].get("text")
                                            if not output_tokens and resp_text:
                                                output_tokens = max(1, len(resp_text) // 4)
                                            if resp_data.get("error"):
                                                err_obj = resp_data["error"]
                                                error = (err_obj.get("message") or err_obj.get("type") or str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
                                            elif resp_data.get("message") and status_code and (status_code < 200 or status_code >= 300):
                                                error = str(resp_data["message"])
                                            elif resp_data.get("detail") and status_code and (status_code < 200 or status_code >= 300):
                                                error = str(resp_data["detail"])
                                        elif status_code and (status_code < 200 or status_code >= 300):
                                            error = resp_body.decode("utf-8", errors="replace")[:200].strip()

                                        if not error and status_code and (status_code < 200 or status_code >= 300):
                                            error = f"HTTP {status_code}"

                                        try:
                                            allowed, budget_status = _token_budget.record_and_check(input_tokens or 0, output_tokens or 0)
                                            budget_headers = {
                                                "X-Token-Budget-Used": str(budget_status["total_used"]),
                                                "X-Token-Budget-Remaining": str(budget_status["remaining"]),
                                                "X-Token-Budget-Percentage": str(budget_status["percentage_used"]),
                                                "X-Token-Budget-Limit": str(budget_status["daily_limit"]),
                                            }
                                            resp_headers.update(budget_headers)
                                        except Exception as b_err:
                                            print(f"[telemetry] token budget check error: {b_err}", file=sys.stderr)

                                        if output_tokens and t_total > 0:
                                            tokens_per_s = output_tokens / (t_total / 1000)
                                        if not allowed:
                                            error = "token_budget_exceeded"

                                        log_call(model, path, input_tokens, output_tokens, ttfb_ms, t_total, tokens_per_s,
                                                 server_running, server_tok_s, server_model, status_code, error, call_type,
                                                 route_name=route_name, upstream_url=active_upstream_url, retries_attempted=attempt,
                                                 absorbed_429=1 if (attempt > 0 and status_code and 200 <= status_code < 300) else 0)
                                        logged = True
                                        log_proxy_call(path, method, call_type, model, status_code, error, 1, ttfb_ms, t_total,
                                                       route_name=route_name, upstream_url=active_upstream_url, retries_attempted=attempt,
                                                       absorbed_429=1 if (attempt > 0 and status_code and 200 <= status_code < 300) else 0)

                                        if payload_inspector._raw_logging_enabled:
                                            raw_record = payload_inspector.make_raw_payload_record(
                                                req_id=req_id, path=path, method=method, call_type=call_type, model=model,
                                                client_ip=request.remote, req_headers=dict(request.headers), payload_obj=payload,
                                                status_code=status_code, resp_headers=dict(upstream_resp.headers), is_stream=False,
                                                ttfb_ms=ttfb_ms, total_ms=t_total, tokens_per_s=tokens_per_s, input_tokens=input_tokens,
                                                output_tokens=output_tokens, reasoning_tokens=reasoning_tokens, content_text=resp_text,
                                                reasoning_text=resp_reasoning, tool_calls=resp_tool_calls, raw_resp_json=resp_data,
                                                error=error, seq=req_seq,
                                            )
                                            payload_inspector.append_raw_payload(raw_record)
                                            if payload_inspector._raw_subscribers:
                                                asyncio.create_task(payload_inspector.broadcast_raw_payload(raw_record))
                                    except Exception as tel_err:
                                        print(f"[telemetry] batch telemetry error: {tel_err}", file=sys.stderr)

                                    if not allowed:
                                        error_msg = (
                                            f"🚫 DAILY TOKEN BUDGET EXCEEDED\n\n"
                                            f"Used: {budget_status['total_used']:,} tokens ({budget_status['percentage_used']:.1f}% of daily limit)\n"
                                            f"Limit: {budget_status['daily_limit']:,} tokens/day\n"
                                            f"Remaining: {budget_status['remaining']:,} tokens\n\n"
                                            f"Token cap enforced by proxy. Requests blocked until 24h window rolls."
                                        )
                                        return web.json_response({"error": {"message": error_msg, "type": "token_budget_exceeded"}}, status=429, headers=budget_headers)

                                    try:
                                        return web.Response(status=upstream_resp.status, body=resp_body, headers=resp_headers)
                                    except Exception:
                                        return web.Response(status=upstream_resp.status, body=resp_body)
                    except (aiohttp.ClientError, ConnectionResetError, ConnectionRefusedError, BrokenPipeError,
                            asyncio.IncompleteReadError, asyncio.TimeoutError) as net_err:
                        is_timeout = isinstance(net_err, asyncio.TimeoutError)
                        can_retry = retry_policy.get("retry_on_timeout", False) if is_timeout else retry_policy.get("retry_on_disconnect", True)
                        if attempt < max_retries and can_retry:
                            target_limiter.record_retry_attempt()
                            retry_needed = True
                            retry_delay = 0.0 if str(retry_policy.get("mode", "immediate")).lower() == "immediate" else (min(float(retry_policy.get("max_retry_after_seconds", 10.0)), 0.5 * (2 ** attempt)) + random.uniform(0.1, 0.4))
                            retry_reason = f"Upstream {'timeout' if is_timeout else 'disconnect/no response'}: {type(net_err).__name__} ({net_err})"
                            _tlog_fn(f"[telemetry] [RETRY TRIGGERED] req_id={req_id} model={model} path={path} reason='{retry_reason}' attempt={attempt+1}/{max_retries+1}. Re-dispatching with delay={retry_delay:.2f}s...")
                        elif route_idx < len(route_sequence) - 1:
                            target_limiter.record_retry_failed()
                            cascade_to_next = True
                            cascade_reason = f"{type(net_err).__name__}: {net_err}"
                            break
                        else:
                            raise
                    finally:
                        if owns_session and not req_session.closed:
                            await req_session.close()

                if retry_needed:
                    attempt += 1
                    if retry_delay > 0:
                        await asyncio.sleep(retry_delay)
                    continue
                else:
                    break

            if cascade_to_next:
                next_route_name = route_sequence[route_idx + 1].route_name
                _tlog_fn(f"[telemetry] [ROUTE FAILOVER] req_id={req_id} route='{route_name}' exhausted retries ({cascade_reason}). Cascading to next priority route: '{next_route_name}'")
                continue
            else:
                break

        return web.json_response(
            {"error": {"message": "Upstream route exhausted with no response", "type": "proxy_error"}},
            status=status_code or 502,
        )

    except asyncio.TimeoutError as to_err:
        to_msg = str(to_err).strip()
        error = f"upstream_first_byte_timeout: {to_msg}" if "first-byte" in to_msg.lower() else (f"upstream_timeout: {to_msg}" if to_msg else f"upstream_timeout: limit {total_timeout}s exceeded")
        _log_forward_failure(model, path, method, call_type, input_tokens, output_tokens, reasoning_tokens,
                             ttfb_ms, t_start, 504, error, route_name, active_upstream_url if 'active_upstream_url' in locals() else upstream_url,
                             server_running, server_tok_s, server_model, req_id, request.headers, payload, is_stream_req, req_seq, request.remote, logged)
        if headers_prepared and response is not None:
            try:
                if not response.is_eof():
                    await response.write_eof()
            except Exception:
                pass
            return response
        return web.json_response({"error": {"message": f"Upstream timeout: {error}", "type": "upstream_timeout"}}, status=504)

    except asyncio.CancelledError as cancel_err:
        cancel_detail = str(cancel_err).strip() or "Client disconnected / request aborted"
        error = f"client_cancelled: {cancel_detail}"
        _log_forward_failure(model, path, method, call_type, input_tokens, output_tokens, reasoning_tokens,
                             ttfb_ms, t_start, 499, error, route_name, active_upstream_url if 'active_upstream_url' in locals() else upstream_url,
                             server_running, server_tok_s, server_model, req_id, request.headers, payload, is_stream_req, req_seq, request.remote, logged)
        raise

    except Exception as e:
        is_net = isinstance(e, (aiohttp.ClientError, ConnectionResetError, ConnectionRefusedError, BrokenPipeError, asyncio.IncompleteReadError))
        detail = str(e).strip() or type(e).__name__
        error = (f"upstream_network_error: {detail}" if is_net else f"proxy_internal_error: {detail}")[:200]
        err_type = "upstream_network_error" if is_net else "proxy_internal_error"
        _log_forward_failure(model, path, method, call_type, input_tokens, output_tokens, reasoning_tokens,
                             ttfb_ms, t_start, status_code or 502, error, route_name, active_upstream_url if 'active_upstream_url' in locals() else upstream_url,
                             server_running, server_tok_s, server_model, req_id, request.headers, payload, is_stream_req, req_seq, request.remote, logged)
        if headers_prepared and response is not None:
            try:
                if not response.is_eof():
                    await response.write_eof()
            except Exception:
                pass
            return response
        return web.json_response({"error": {"message": error, "type": err_type}}, status=502)


async def _simple_forward(request, path, method):
    """Forward non-inference calls (model list, props, etc.) without logging to api_calls."""
    route_candidates = _model_router.resolve_chain(None)
    admit_route, admit_limiter, admit_already_acquired = await _model_router.select_admission_route(route_candidates)
    route_sequence = [admit_route] + [r for r in route_candidates if r.route_id != admit_route.route_id]

    raw_headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    req_body = await request.read() if request.can_read_body else None
    if req_body and "Content-Length" in raw_headers:
        raw_headers["Content-Length"] = str(len(req_body))

    t_start = time.monotonic()
    status_code, error = None, None
    last_upstream_url = UPSTREAM

    try:
        for route_idx, route_res in enumerate(route_sequence):
            resolved_base = UPSTREAM if (route_res.is_default and UPSTREAM != DEFAULT_UPSTREAM) else route_res.upstream_url
            upstream_url = build_upstream_url(resolved_base, path)
            last_upstream_url = upstream_url
            headers = apply_upstream_api_key(dict(raw_headers), route_res.api_key)

            route_timeout = route_res.timeout or _model_router.default_timeout
            req_timeout = aiohttp.ClientTimeout(
                connect=float(route_timeout.get("connect", 10.0)),
                sock_read=float(route_timeout.get("sock_read", 60.0)),
                total=float(route_timeout.get("total", 180.0)),
            )

            target_limiter = _model_router.get_limiter(route_res.route_id)
            retry_policy = dict(route_res.retry_policy or _model_router.default_retry_policy)
            if route_res.is_default:
                if _retry_429_max == 0:
                    retry_policy["enabled"] = False
                    retry_policy["max_retries"] = 0
                else:
                    retry_policy["max_retries"] = _retry_429_max
            max_retries = int(retry_policy.get("max_retries", 0)) if retry_policy.get("enabled", True) else 0

            attempt = 0
            cascade_to_next = False
            cascade_reason = None

            while attempt <= max_retries:
                retry_needed, retry_delay, retry_reason = False, 0.0, None

                is_initial_acquired = (route_idx == 0 and attempt == 0 and admit_already_acquired)
                slot_context = AcquiredSlot(target_limiter, already_acquired=is_initial_acquired)

                async with slot_context:
                    req_session = request.app.get(_upstream_session_key) if hasattr(request, "app") and _upstream_session_key in request.app else None
                    owns_session = False
                    if req_session is None or req_session.closed:
                        req_session = aiohttp.ClientSession(timeout=req_timeout)
                        owns_session = True

                    try:
                        async with req_session.request(
                            method, upstream_url, headers=headers,
                            data=req_body if req_body else None, params=request.query, timeout=req_timeout,
                        ) as upstream_resp:
                            status_code = upstream_resp.status
                            body = await upstream_resp.read()

                            sniff_needed, sniff_delay, sniff_reason = evaluate_retry_condition(
                                status_code=status_code, headers=upstream_resp.headers,
                                body_text_or_json=body, attempt=attempt, retry_policy=retry_policy,
                            )

                            if sniff_needed and attempt < max_retries:
                                target_limiter.record_retry_attempt()
                                print(f"[telemetry] Upstream rate limit in _simple_forward on {path} ({sniff_reason}, attempt {attempt+1}/{max_retries+1}). Re-dispatching with delay={sniff_delay:.2f}s...", file=sys.stderr)
                                retry_needed, retry_delay, retry_reason = True, sniff_delay, sniff_reason
                            elif sniff_reason and attempt >= max_retries and route_idx < len(route_sequence) - 1:
                                target_limiter.record_retry_failed()
                                cascade_to_next = True
                                cascade_reason = sniff_reason
                                break
                            else:
                                if attempt > 0:
                                    if status_code and 200 <= status_code < 300:
                                        target_limiter.record_retry_absorbed()
                                    else:
                                        target_limiter.record_retry_failed()

                                t_total = (time.monotonic() - t_start) * 1000
                                if status_code and (status_code < 200 or status_code >= 300):
                                    error = f"HTTP {status_code}"

                                try:
                                    log_proxy_call(path, method, classify_endpoint(path), None, status_code, error, 0, None, t_total,
                                                   route_name=route_res.route_name, upstream_url=upstream_url,
                                                   retries_attempted=attempt, absorbed_429=1 if (attempt > 0 and status_code and 200 <= status_code < 300) else 0)
                                except Exception as tel_err:
                                    print(f"[telemetry] _simple_forward log error: {tel_err}", file=sys.stderr)

                                simple_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in ("content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive", "upgrade")}
                                simple_headers["X-Proxy-Retries-Attempted"] = str(attempt)
                                simple_headers["X-Proxy-Rate-Limit-Absorbed"] = "1" if (attempt > 0 and status_code and 200 <= status_code < 300) else "0"

                                try:
                                    return web.Response(status=upstream_resp.status, body=body, headers=simple_headers)
                                except Exception:
                                    return web.Response(status=upstream_resp.status, body=body)
                    except (aiohttp.ClientError, ConnectionResetError, ConnectionRefusedError, BrokenPipeError,
                            asyncio.IncompleteReadError, asyncio.TimeoutError) as net_err:
                        if attempt < max_retries and retry_policy.get("retry_on_disconnect", True):
                            target_limiter.record_retry_attempt()
                            retry_needed = True
                            retry_delay = 0.0 if str(retry_policy.get("mode", "immediate")).lower() == "immediate" else (0.5 * (2 ** attempt) + random.uniform(0.05, 0.2))
                            retry_reason = f"Upstream connection failure: {type(net_err).__name__}"
                            print(f"[telemetry] Upstream connection dropped in _simple_forward on {path} ({retry_reason}, attempt {attempt+1}/{max_retries+1}). Re-dispatching with delay={retry_delay:.2f}s...", file=sys.stderr)
                        elif route_idx < len(route_sequence) - 1:
                            target_limiter.record_retry_failed()
                            cascade_to_next = True
                            cascade_reason = f"{type(net_err).__name__}: {net_err}"
                            break
                        else:
                            raise
                    finally:
                        if owns_session and not req_session.closed:
                            await req_session.close()

                if retry_needed:
                    attempt += 1
                    if retry_delay > 0:
                        await asyncio.sleep(retry_delay)
                    continue
                else:
                    break

            if cascade_to_next:
                next_route_name = route_sequence[route_idx + 1].route_name
                print(f"[telemetry] [_simple_forward FAILOVER] route='{route_res.route_name}' exhausted retries ({cascade_reason}). Cascading to next priority route: '{next_route_name}'", file=sys.stderr)
                continue
            else:
                break

        return web.json_response(
            {"error": {"message": "Upstream route exhausted with no response", "type": "proxy_error"}},
            status=status_code or 502,
        )

    except asyncio.TimeoutError as to_err:
        to_msg = str(to_err).strip()
        error = f"upstream_timeout: {to_msg}" if to_msg else "upstream_timeout"
        try:
            log_proxy_call(path, method, classify_endpoint(path), None, 504, error, 0, None, (time.monotonic() - t_start) * 1000,
                           route_name=_model_router.default_name, upstream_url=last_upstream_url)
        except Exception:
            pass
        return web.json_response({"error": {"message": f"Upstream timeout: {error}", "type": "upstream_timeout"}}, status=504)
    except asyncio.CancelledError as cancel_err:
        cancel_detail = str(cancel_err).strip() or "Client disconnected / request aborted"
        error = f"client_cancelled: {cancel_detail}"
        try:
            log_proxy_call(path, method, classify_endpoint(path), None, 499, error, 0, None, (time.monotonic() - t_start) * 1000,
                           route_name=_model_router.default_name, upstream_url=last_upstream_url)
        except Exception:
            pass
        raise
    except Exception as e:
        is_net = isinstance(e, (aiohttp.ClientError, ConnectionResetError, ConnectionRefusedError, BrokenPipeError, asyncio.IncompleteReadError))
        detail = str(e).strip() or type(e).__name__
        error = (f"upstream_network_error: {detail}" if is_net else f"proxy_internal_error: {detail}")[:200]
        err_type = "upstream_network_error" if is_net else "proxy_internal_error"
        try:
            log_proxy_call(path, method, classify_endpoint(path), None, None, error, 0, None, (time.monotonic() - t_start) * 1000,
                           route_name=_model_router.default_name, upstream_url=last_upstream_url)
        except Exception:
            pass
        return web.json_response({"error": {"message": error, "type": err_type}}, status=502)
