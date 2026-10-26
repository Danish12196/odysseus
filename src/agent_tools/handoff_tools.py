"""Hand a long piece of work to the durable background queue."""

from __future__ import annotations

import json


class HandoffBackgroundTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        raw = (content or "").strip()
        prompt = raw
        max_steps = None
        if raw.startswith("{"):
            try:
                args = json.loads(raw)
            except (ValueError, TypeError):
                args = None
            if isinstance(args, dict):
                prompt = str(args.get("prompt") or args.get("task") or "").strip()
                try:
                    max_steps = int(args["max_steps"]) if args.get("max_steps") else None
                except (TypeError, ValueError):
                    max_steps = None
        session_id = ctx.get("session_id")
        if not session_id:
            return {"error": "handoff_background: no active chat to report back to.", "exit_code": 1}
        from src.handoff import enqueue_handoff
        return enqueue_handoff(
            prompt,
            session_id=session_id,
            owner=ctx.get("owner"),
            max_steps=max_steps,
        )
