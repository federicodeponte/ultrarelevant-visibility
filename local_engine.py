"""
UltraRelevant Visibility Tool — Floom port (async polling)

Measures how visible / how correct a company's product data is when AI agents
are queried. Three agents:

  1. Gemini 3 Pro      (gemini-3-pro-preview)
  2. Gemini 2.5 Flash  (gemini-2.5-flash)
  3. NVIDIA gpt-oss-120b (openai/gpt-oss-120b via NIM)

Pipeline (one LLM call per stage, parallelized across cells):
  1. Generate plausible procurement queries (Gemini 3 Pro)
  2. Query 3 agents per query (3 x 3 = 9 cells)
  3. Fetch ground truth via Gemini search grounding (per query)
  4. Categorize each cell vs ground truth (Gemini 2.5 Flash as judge)
  5. Score: correct_count / total_cells * 100

Async polling protocol (in-app, not Floom job queue):
  - POST /analyze     -> {job_id, status: "pending"}             (<1s)
  - GET /status/<id>  -> {status, progress, result?}             (poll every 2s)
  - GET /             -> serves index.html
  - GET /health       -> {ok, keys: {gemini, nvidia}}

Run locally:
  python3 floom_app.py
  # or: uvicorn floom_app:app --host 0.0.0.0 --port 8766

The Floom runtime exposes secrets as env vars; reading via context.get_secret()
when available, else direct os.environ + ~/.config/ai-sidecar/keys.json fallback
for local dev.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
KEYS_PATH = Path.home() / ".config" / "ai-sidecar" / "keys.json"


# ---------- Key loading ----------
def _load_secret(name: str, *fallback_keys: str) -> str | None:
    """Resolve a secret from (in order):
    1. floom context.get_secret if running inside Floom runtime
    2. environment variable
    3. ~/.config/ai-sidecar/keys.json (local dev)
    """
    # 1) floom SDK
    try:
        from floom import context as _ctx  # type: ignore
        try:
            return _ctx.get_secret(name)
        except Exception:
            pass
    except Exception:
        pass

    # 2) env
    if os.environ.get(name):
        return os.environ[name]

    # 3) local keys file
    if KEYS_PATH.exists():
        try:
            data = json.loads(KEYS_PATH.read_text())
        except Exception:
            data = {}
        for k in (name.lower(), *fallback_keys):
            if data.get(k):
                return data[k]
    return None


GEMINI_KEY = _load_secret("GEMINI_API_KEY", "gemini_paid", "gemini")
NVIDIA_KEY = _load_secret("NVIDIA_API_KEY", "nvidia")

GEMINI_PRO_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-preview:generateContent"
GEMINI_FLASH_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
MODEL_NVIDIA = "openai/gpt-oss-120b"

AGENTS = ["Gemini 3 Pro", "Gemini 2.5 Flash", "GPT-OSS-120B"]


# ---------- LLM transport ----------
def _gemini_call(url: str, prompt: str, grounded: bool = False, timeout: int = 120) -> dict:
    """Returns {text, sources?: [{title, uri}]}"""
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY missing")
    body: dict = {"contents": [{"parts": [{"text": prompt}]}]}
    if grounded:
        body["tools"] = [{"google_search": {}}]
    r = requests.post(
        f"{url}?key={GEMINI_KEY}",
        headers={"Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )
    r.raise_for_status()
    d = r.json()
    cand = d["candidates"][0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    sources = []
    gm = cand.get("groundingMetadata") or {}
    for chunk in gm.get("groundingChunks", []) or []:
        web = chunk.get("web", {})
        if web.get("uri"):
            sources.append({"title": web.get("title", ""), "uri": web["uri"]})
    return {"text": text, "sources": sources}


def call_gemini_pro(prompt: str, grounded: bool = False, timeout: int = 120) -> dict:
    return _gemini_call(GEMINI_PRO_URL, prompt, grounded=grounded, timeout=timeout)


def call_gemini_flash(prompt: str, grounded: bool = False, timeout: int = 120) -> dict:
    return _gemini_call(GEMINI_FLASH_URL, prompt, grounded=grounded, timeout=timeout)


def call_nvidia(model: str, prompt: str, system: str = "", timeout: int = 90) -> str:
    if not NVIDIA_KEY:
        raise RuntimeError("NVIDIA_API_KEY missing")
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    r = requests.post(
        NVIDIA_URL,
        headers={"Authorization": f"Bearer {NVIDIA_KEY}", "Content-Type": "application/json"},
        json={"model": model, "messages": msgs, "max_tokens": 800, "temperature": 0.2},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


# ---------- Stage 1: Generate queries ----------
QUERY_GEN_PROMPT = """You are simulating a B2B procurement engineer who uses AI assistants to find the right industrial product.

Generate 3 plausible procurement-style queries someone would ask Claude/GPT/Gemini about products from the company "{company}".

Each query must:
- Specify enough constraints that a correct answer would name a real, orderable SKU or part number from {company}
- Be the kind of question where a wrong/hallucinated SKU would actually cost money in a real procurement workflow
- Cover different product families if {company} has multiple lines
- Be 1-2 sentences each. No preambles.

Return ONLY a JSON array of 3 strings, no commentary:
["query 1", "query 2", "query 3"]
"""


def generate_queries(company: str) -> list[str]:
    raw = call_gemini_pro(QUERY_GEN_PROMPT.format(company=company))["text"]
    txt = raw.strip()
    if txt.startswith("```"):
        txt = txt.split("```")[1]
        if txt.startswith("json"):
            txt = txt[4:]
        txt = txt.strip()
    queries = json.loads(txt)
    if not isinstance(queries, list) or len(queries) < 1:
        raise ValueError("Bad query list from Gemini")
    return queries[:3]


# ---------- Stage 2: Query agents ----------
AGENT_SYSTEM = (
    "You are answering a procurement question. If a real, orderable part number "
    "from the manufacturer's catalog is the right answer, give it. If you cannot "
    "answer with a specific SKU, say so plainly. Be concise (under 200 words)."
)


def query_agent(agent: str, prompt: str) -> dict:
    try:
        full = AGENT_SYSTEM + "\n\n" + prompt
        if agent == "Gemini 3 Pro":
            text = call_gemini_pro(full)["text"]
        elif agent == "Gemini 2.5 Flash":
            text = call_gemini_flash(full)["text"]
        elif agent == "GPT-OSS-120B":
            text = call_nvidia(MODEL_NVIDIA, prompt, AGENT_SYSTEM)
        else:
            raise ValueError(f"Unknown agent {agent}")
        return {"agent": agent, "text": text, "error": None}
    except Exception as e:
        return {"agent": agent, "text": "", "error": f"{type(e).__name__}: {e}"}


# ---------- Stage 3: Ground truth ----------
GT_PROMPT = """You are a fact-checker. Use Google Search to find the answer to this B2B procurement question, citing the manufacturer's official catalog or authorized distributors (TME, RS, Mouser, Misumi, Octopart, etc.).

Question: {q}

Return:
- The most likely correct answer (or "Unanswerable as posed" if the question is missing required inputs)
- The real SKU/part number if applicable, with format verified against the catalog
- A 1-2 sentence rationale
- Source URLs

Be brutally honest: if the question is under-specified or has no single correct SKU, say so. Do not invent answers."""


def fetch_ground_truth(q: str) -> dict:
    try:
        result = call_gemini_pro(GT_PROMPT.format(q=q), grounded=True, timeout=180)
        return {"text": result["text"], "sources": result["sources"], "error": None}
    except Exception as e:
        return {"text": "", "sources": [], "error": f"{type(e).__name__}: {e}"}


# ---------- Stage 4: Categorize ----------
CATEGORIES = ["CORRECT", "HALLUCINATION", "REFUSED", "WRONG_SPEC", "ERROR"]

JUDGE_SYSTEM = """You are a strict procurement judge. Given a query, an agent response, and the ground truth, classify the agent response into EXACTLY one category:

- CORRECT: The agent gave a verifiable, real, orderable SKU/answer that matches the ground truth.
- HALLUCINATION: The agent gave a confident specific SKU/part number that does not exist in the manufacturer's catalog (wrong format, invalid concatenation, made up).
- REFUSED: The agent declined to answer, deflected to "check the website", or gave only generic advice with no specific SKU.
- WRONG_SPEC: The agent gave a number/spec that contradicts the ground truth datasheet (off by 25%+, wrong temperature point, wrong units).

Return ONLY a JSON object: {"category": "CORRECT|HALLUCINATION|REFUSED|WRONG_SPEC", "rationale": "1-2 sentences"}"""

JUDGE_PROMPT_TEMPLATE = """Query: {q}

Agent response:
{a}

Ground truth:
{gt}

Classify. Return JSON only."""


def categorize(q: str, agent_text: str, gt_text: str) -> dict:
    if not agent_text:
        return {"category": "ERROR", "rationale": "No agent response"}
    try:
        prompt = JUDGE_SYSTEM + "\n\n" + JUDGE_PROMPT_TEMPLATE.format(
            q=q, a=agent_text, gt=gt_text or "(ground truth unavailable)"
        )
        raw = call_gemini_flash(prompt)["text"]
        txt = raw.strip()
        if txt.startswith("```"):
            txt = txt.split("```")[1]
            if txt.startswith("json"):
                txt = txt[4:]
            txt = txt.strip()
        start = txt.find("{")
        end = txt.rfind("}")
        if start >= 0 and end > start:
            txt = txt[start : end + 1]
        d = json.loads(txt)
        cat = d.get("category", "ERROR").upper()
        if cat not in CATEGORIES:
            cat = "ERROR"
        return {"category": cat, "rationale": d.get("rationale", "")}
    except Exception as e:
        return {"category": "ERROR", "rationale": f"Judge failed: {type(e).__name__}: {e}"}


# ---------- Pipeline ----------
def analyze_pipeline(company: str, progress_cb=None) -> dict:
    started = time.time()
    log: list[str] = []

    def step(msg: str) -> None:
        line = f"[{round(time.time() - started, 1):>5}s] {msg}"
        log.append(line)
        if progress_cb:
            progress_cb(msg)
        print(line, flush=True)

    step(f"Stage 1: Generating queries for {company}")
    queries = generate_queries(company)
    step(f"  Generated {len(queries)} queries")

    rows = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        # Submit all agent calls + ground-truth fetches in parallel
        agent_futs = {}  # fut -> (qi, agent)
        gt_futs = {}  # fut -> qi
        for qi, q in enumerate(queries):
            for a in AGENTS:
                fut = pool.submit(query_agent, a, q)
                agent_futs[fut] = (qi, a)
            gt_futs[pool.submit(fetch_ground_truth, q)] = qi

        # Collect agent results
        agent_results = {qi: {} for qi in range(len(queries))}
        for fut in as_completed(agent_futs):
            qi, a = agent_futs[fut]
            agent_results[qi][a] = fut.result()
        step(f"Stage 2: Collected {len(queries) * len(AGENTS)} agent responses")

        # Collect ground truth
        gt_results = {}
        for fut in as_completed(gt_futs):
            qi = gt_futs[fut]
            gt_results[qi] = fut.result()
        step(f"Stage 3: Collected {len(gt_results)} ground-truth fetches")

    for qi, q in enumerate(queries):
        rows.append(
            {
                "query": q,
                "agents": agent_results[qi],
                "ground_truth": gt_results[qi],
            }
        )

    # Stage 4: categorize each cell
    step("Stage 4: Categorizing cells")
    with ThreadPoolExecutor(max_workers=9) as pool:
        cat_futs = {}
        for ri, row in enumerate(rows):
            for agent in AGENTS:
                ar = row["agents"][agent]
                if ar["error"]:
                    row["agents"][agent]["category"] = "ERROR"
                    row["agents"][agent]["rationale"] = ar["error"]
                else:
                    fut = pool.submit(
                        categorize, row["query"], ar["text"], row["ground_truth"]["text"]
                    )
                    cat_futs[fut] = (ri, agent)
        for fut in as_completed(cat_futs):
            ri, agent = cat_futs[fut]
            res = fut.result()
            rows[ri]["agents"][agent]["category"] = res["category"]
            rows[ri]["agents"][agent]["rationale"] = res["rationale"]

    # Stage 5: score
    total = len(rows) * len(AGENTS)
    correct = 0
    breakdown = {c: 0 for c in CATEGORIES}
    per_agent = {a: {c: 0 for c in CATEGORIES} for a in AGENTS}
    for row in rows:
        for agent in AGENTS:
            cat = row["agents"][agent]["category"]
            breakdown[cat] = breakdown.get(cat, 0) + 1
            per_agent[agent][cat] = per_agent[agent].get(cat, 0) + 1
            if cat == "CORRECT":
                correct += 1
    score = round((correct / total) * 100) if total else 0

    elapsed = round(time.time() - started, 1)
    step(f"Stage 5: Score = {score}/100 ({correct}/{total}) in {elapsed}s")

    return {
        "company": company,
        "score": score,
        "breakdown": breakdown,
        "per_agent": per_agent,
        "total_cells": total,
        "correct_cells": correct,
        "rows": rows,
        "agents": AGENTS,
        "log": log,
        "elapsed_seconds": elapsed,
    }


# ---------- Async job store ----------
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _run_job(job_id: str, company: str) -> None:
    def progress_cb(msg: str) -> None:
        with _jobs_lock:
            _jobs[job_id]["progress"] = msg
            _jobs[job_id]["updated_at"] = time.time()

    try:
        result = analyze_pipeline(company, progress_cb=progress_cb)
        with _jobs_lock:
            _jobs[job_id]["status"] = "complete"
            _jobs[job_id]["result"] = result
            _jobs[job_id]["progress"] = "Done"
            _jobs[job_id]["updated_at"] = time.time()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = f"{type(e).__name__}: {e}"
            _jobs[job_id]["updated_at"] = time.time()


# ---------- FastAPI ----------
app = FastAPI(
    title="UltraRelevant Visibility Tool",
    version="1.0.0",
    description=(
        "Score how visible and how correct a company's product data is when AI agents "
        "are asked real procurement questions. Async polling: POST /analyze returns a "
        "job_id; GET /status/<job_id> returns progress/result."
    ),
    openapi_url="/openapi.json",
    docs_url="/docs",
)


class AnalyzeRequest(BaseModel):
    company_name: str = Field(..., description="Company to analyze (e.g. 'igus', 'festo').", min_length=1, max_length=120)


class AnalyzeResponse(BaseModel):
    job_id: str
    status: str = "pending"


class StatusResponse(BaseModel):
    job_id: str
    status: str  # pending | complete | error
    progress: str | None = None
    elapsed: float | None = None
    result: dict | None = None
    error: str | None = None


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "keys": {
            "gemini": bool(GEMINI_KEY),
            "nvidia": bool(NVIDIA_KEY),
        },
        "agents": AGENTS,
    }


@app.get("/")
def root() -> FileResponse:
    return FileResponse(str(ROOT / "index.html"))


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(body: AnalyzeRequest) -> dict:
    company = body.company_name.strip()
    if not company:
        raise HTTPException(status_code=400, detail="company_name required")
    if not GEMINI_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY missing in runtime")
    if not NVIDIA_KEY:
        raise HTTPException(status_code=500, detail="NVIDIA_API_KEY missing in runtime")

    job_id = f"job_{uuid.uuid4().hex[:12]}"
    started = time.time()
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "pending",
            "company": company,
            "started_at": started,
            "updated_at": started,
            "progress": "Queued",
            "result": None,
            "error": None,
        }

    threading.Thread(target=_run_job, args=(job_id, company), daemon=True).start()
    return {"job_id": job_id, "status": "pending"}


@app.get("/status/{job_id}", response_model=StatusResponse)
def status(job_id: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    elapsed = round(job["updated_at"] - job["started_at"], 1)
    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": job.get("progress"),
        "elapsed": elapsed,
        "result": job.get("result"),
        "error": job.get("error"),
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8766"))
    print(f"GEMINI_API_KEY: {'set' if GEMINI_KEY else 'MISSING'}")
    print(f"NVIDIA_API_KEY: {'set' if NVIDIA_KEY else 'MISSING'}")
    print(f"Server starting on http://0.0.0.0:{port}/")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
