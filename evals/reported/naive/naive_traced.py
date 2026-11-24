"""Runs the untouched naive agent and logs token usage per session to a jsonl file."""
import json, os, time, sys
os.environ["AGENT"] = "naive"
LOG = os.environ["NAIVE_USAGE_LOG"]
from agent_service import naive_agent
from agent_service.app import app  # noqa

_cur = {"sid": None}
_orig_get_client = naive_agent.get_client
_orig_handle = naive_agent.handle

def _log(rec):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")

class _Msgs:
    def __init__(self, m): self.m = m
    def create(self, **kw):
        t = time.time()
        r = self.m.create(**kw)
        u = r.usage
        _log({"sid": _cur["sid"], "kind": "model", "model": kw.get("model"), "dur": round(time.time()-t, 3),
              "in": u.input_tokens, "out": u.output_tokens,
              "cr": getattr(u, "cache_read_input_tokens", 0) or 0, "cw": getattr(u, "cache_creation_input_tokens", 0) or 0,
              "stop": r.stop_reason})
        return r

class _Client:
    def __init__(self, c): self.messages = _Msgs(c.messages)

naive_agent.get_client = lambda: _Client(_orig_get_client())

async def handle(req):
    _cur["sid"] = req.session_id
    try:
        return await _orig_handle(req)
    except Exception as e:
        _log({"sid": req.session_id, "kind": "exception", "err": repr(e)[:300]})
        raise
naive_agent.handle = handle
