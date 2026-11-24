"""The agent service. Run: uvicorn agent_service.app:app --port 8000

Set AGENT=naive to run the baseline naive agent instead (for comparison in evals).
"""

import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, HTTPException  # noqa: E402

from .schemas import ChatRequest, ChatResponse  # noqa: E402

app = FastAPI(title="City Care front desk agent", version="0.2")

_service = None


def get_service():
    """The CityCareService, built on first use (so importing this module needs no API key)."""
    global _service
    if _service is None:
        from agentcore import AnthropicLLM

        from .hospital_api import HospitalAPI
        from .orchestrator import CityCareService

        _service = CityCareService(HospitalAPI(), AnthropicLLM())
    return _service


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, service=Depends(get_service)) -> ChatResponse:
    if os.getenv("AGENT", "service") == "naive":
        from . import naive_agent

        return await naive_agent.handle(req)
    return await service.handle(req)


@app.get("/trace/{session_id}")
async def trace(session_id: str, service=Depends(get_service)):
    data = service.trace(session_id)
    if not data["traces"]:
        raise HTTPException(status_code=404, detail="No trace for this session")
    return data
