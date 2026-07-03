"""Experimental keepalive patch for EvalScope's Responses bridge.

EvalScope's native Responses stream is the default SWE Bench Pro path. This
patch is retained only for diagnostics in environments where Codex disconnects
before the upstream model returns; container Codex v0.142.0 completed the
official-order shard smoke on the native path and did not exit cleanly with this
keepalive patch enabled.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator


def _frame(event: str, data: dict[str, Any], sequence_number: int) -> bytes:
    body = {**data, "sequence_number": sequence_number, "type": event}
    return f"event: {event}\ndata: {json.dumps(body, ensure_ascii=False)}\n\n".encode("utf-8")


def _shell_response(*, response_id: str, created_at: int, model: str) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "in_progress",
        "model": model,
        "output": [],
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }


async def _renumber_tail_frames(chunks: AsyncIterator[bytes], *, start_sequence_number: int) -> AsyncIterator[bytes]:
    sequence_number = start_sequence_number
    skipped = 0
    async for chunk in chunks:
        if skipped < 2:
            skipped += 1
            continue
        text = chunk.decode("utf-8")
        event_line, data_line, _ = text.split("\n", 2)
        payload = json.loads(data_line.removeprefix("data: "))
        sequence_number += 1
        payload["sequence_number"] = sequence_number
        yield f"{event_line}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def install_responses_keepalive_patch(*, ping_interval_s: float = 10.0) -> None:
    """Patch ModelProxyServer._respond_streaming_responses in this process."""
    from evalscope.agent.external.bridge import server as bridge_server
    from evalscope.agent.external.bridge.sse_responses import stream_responses_payload
    from evalscope.agent.external.bridge.translate_responses import model_output_to_responses_payload

    model_proxy_server = bridge_server.ModelProxyServer
    if getattr(model_proxy_server, "_codex_responses_keepalive_patch", False):
        return

    async def _respond_streaming_responses(
        self,
        request,
        session,
        body,
        chat_messages,
        tool_infos,
        tool_choice,
        gen_config,
    ):
        response = await self._prepare_sse_response(request)
        started = time.monotonic()
        response_id = f"resp_{uuid.uuid4().hex[:24]}"
        created_at = int(time.time())
        model_name = body.get("model") or ""
        shell = _shell_response(response_id=response_id, created_at=created_at, model=model_name)
        sequence_number = 1
        await response.write(_frame("response.created", {"response": shell}, sequence_number))
        sequence_number += 1
        await response.write(_frame("response.in_progress", {"response": shell}, sequence_number))

        generate_task = asyncio.create_task(
            session.model.generate_async(
                input=chat_messages,
                tools=tool_infos or None,
                tool_choice=tool_choice,
                config=gen_config,
            )
        )
        try:
            while True:
                try:
                    output = await asyncio.wait_for(asyncio.shield(generate_task), timeout=ping_interval_s)
                    break
                except asyncio.TimeoutError:
                    sequence_number += 1
                    await response.write(_frame("response.in_progress", {"response": shell}, sequence_number))

            latency_ms = (time.monotonic() - started) * 1000
            session.recorder.record_responses_turn(body, output, latency_ms=latency_ms)
            bridge_server._log_turn(session, output, latency_ms, mode="stream")
            payload = model_output_to_responses_payload(output, request_model=model_name)
            payload["id"] = response_id
            payload["created_at"] = created_at
            async for chunk in _renumber_tail_frames(
                stream_responses_payload(payload),
                start_sequence_number=sequence_number,
            ):
                await response.write(chunk)
        except Exception as exc:  # pragma: no cover - upstream-dependent
            bridge_server._log_upstream_failure(session, exc, mode="stream")
            sequence_number += 1
            err_payload = {
                "type": "error",
                "code": "api_error",
                "message": repr(exc),
                "param": None,
                "sequence_number": sequence_number,
            }
            try:
                await response.write(f"event: error\ndata: {json.dumps(err_payload)}\n\n".encode("utf-8"))
            except ConnectionResetError:
                pass
        finally:
            if not generate_task.done():
                generate_task.cancel()
        try:
            await response.write_eof()
        except ConnectionResetError:
            pass
        return response

    model_proxy_server._respond_streaming_responses = _respond_streaming_responses
    model_proxy_server._codex_responses_keepalive_patch = True
