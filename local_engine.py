"""
UltraRelevant Visibility Tool — v2 (URL input, Gemini-grounded, source-attributed)

Honest engine: ONE agent (Gemini 3 Pro with Google Search grounding). No multi-agent
matrix dressing. The wedge is source attribution — when an AI gets the answer right,
who did it cite? The manufacturer's own site, or a distributor / marketplace?

Pipeline:
  1. URL in (e.g. https://igus.de). Extract canonical manufacturer domain.
  2. Discover the company's products via grounded Gemini visiting the URL.
  3. Generate 5 procurement-style queries.
  4. Run each query through Gemini Pro WITH google_search grounding. Capture answer
     text + grounding source URLs.
  5. Source attribution per query: classify each source URL as
     manufacturer / distributor / marketplace / other (against the input domain
     and a maintained distributor list).
  6. Categorize each answer: CORRECT / HALLUCINATION / REFUSED / WRONG_SPEC.
     Judge is Gemini Pro grounded — uses search to verify the answer independently.
  7. Score:
       Visibility Score        = % of queries with CORRECT answers
       Source Authority Score  = % of CORRECT answers where ALL/MAJORITY sources
                                 were the manufacturer's own domain (not distributor /
                                 marketplace).

Async polling protocol:
  POST /analyze     -> {job_id, status: "pending"}
  GET /status/<id>  -> {status, progress, result?}
  GET /             -> serves index.html
  GET /health       -> {ok, keys}
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
KEYS_PATH = Path.home() / ".config" / "ai-sidecar" / "keys.json"


# ---------- Security helpers ----------
def _safe_err(e: Exception) -> str:
    """Return a sanitized error string with any API key values redacted."""
    msg = str(e)
    msg = re.sub(r"([?&])key=[^&\s]*", r"\1key=REDACTED", msg)
    return f"{type(e).__name__}: {msg}"


# ---------- Key loading ----------
def _load_secret(name: str, *fallback_keys: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
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
GEMINI_PRO_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-preview:generateContent"


# ---------- Domain handling ----------
# Common B2B distributors / marketplaces. Source domains classified against this list.
DISTRIBUTOR_DOMAINS = {
    # Industrial / electronics distributors
    "tme.eu", "tme.com", "rs-online.com", "rsdelivers.com", "rs-components.com",
    "misumi.com", "misumi-ec.com", "misumiusa.com", "mouser.com", "mouser.de",
    "digikey.com", "digikey.de", "farnell.com", "uk.farnell.com", "newark.com",
    "element14.com", "octopart.com", "findchips.com",
    "cebeo.be", "rexel.com", "rexel.de", "sonepar.com", "sonepar.de",
    "hennlich.com", "hennlich.de", "conrad.com", "conrad.de", "voelkner.de",
    "reichelt.com", "reichelt.de", "distrelec.com", "buerklin.com",
    "automation24.com", "automation24.de",
    # Industrial wholesalers
    "grainger.com", "msc.com", "mscdirect.com", "fastenal.com",
    "wuerth.com", "wuerth.de", "hoffmann-group.com", "hahn-kolb.de",
    # Bearings / power transmission specialist distributors
    "boca-bearings.com", "bearings-direct.com", "bearings.com.au",
    # German MRO
    "kaiser-kraft.de", "manomano.de", "schaefer-shop.de",
}

MARKETPLACE_DOMAINS = {
    "amazon.com", "amazon.de", "amazon.co.uk", "amazon.fr", "amazon.it", "amazon.es",
    "alibaba.com", "1688.com", "aliexpress.com", "ebay.com", "ebay.de",
    "tradeindia.com", "indiamart.com", "made-in-china.com",
    "globalsources.com", "thomasnet.com",
}


def normalize_input_to_domain(value: str) -> str:
    """Accept a URL or bare domain. Return canonical lowercase host (no www., no path)."""
    v = value.strip()
    if not v:
        raise ValueError("empty input")
    # If user typed a domain without scheme, add one for urlparse.
    if "://" not in v:
        v = "https://" + v
    parsed = urlparse(v)
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"could not extract host from: {value}")
    if host.startswith("www."):
        host = host[4:]
    # Sanity: must look like a domain
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", host):
        raise ValueError(f"invalid domain: {host}")
    return host


def domain_root(host: str) -> str:
    """Reduce 'shop.igus.com' -> 'igus.com'. Naive registrable-domain heuristic.

    Handles common 2-part TLDs (co.uk, com.au, co.jp, etc.).
    """
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    two_part_tlds = {
        "co.uk", "co.jp", "co.kr", "co.in", "co.za", "co.nz",
        "co.id", "co.th",
        "com.au", "com.br", "com.cn", "com.mx", "com.tr",
        "com.sg", "com.my", "com.ph", "com.vn", "com.hk", "com.tw",
        "or.jp", "ne.jp", "ac.jp", "ad.jp",
        "or.kr", "re.kr",
        "ac.uk", "gov.uk", "org.uk",
    }
    last_two = ".".join(parts[-2:])
    if last_two in two_part_tlds and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def manufacturer_domain_variants(canonical_host: str) -> set[str]:
    """Given igus.de, return {igus.de, igus.com, igus.eu, ...} — siblings of the
    same brand on different TLDs. We use the registrable-domain *label* (igus)
    and accept any TLD."""
    root = domain_root(canonical_host)
    # extract brand label (first part of root)
    brand = root.split(".")[0]
    return {brand}


def classify_source(source_host: str, manufacturer_brand: str) -> str:
    """Return one of: manufacturer | distributor | marketplace | other."""
    if not source_host:
        return "other"
    host = source_host.lower()
    if host.startswith("www."):
        host = host[4:]
    root = domain_root(host)
    label = root.split(".")[0]

    # Manufacturer match: brand label (e.g. 'igus' matches igus.de, igus.com, shop.igus.eu)
    if label == manufacturer_brand:
        return "manufacturer"
    # Strip vertexaisearch redirect wrappers
    if root in DISTRIBUTOR_DOMAINS or host in DISTRIBUTOR_DOMAINS:
        return "distributor"
    if root in MARKETPLACE_DOMAINS or host in MARKETPLACE_DOMAINS:
        return "marketplace"
    # check distributors at the registrable-domain level (handles subdomains)
    for d in DISTRIBUTOR_DOMAINS:
        if root == d or host.endswith("." + d):
            return "distributor"
    for m in MARKETPLACE_DOMAINS:
        if root == m or host.endswith("." + m):
            return "marketplace"
    return "other"


def host_from_uri(uri: str) -> str:
    """Extract host from a URI. Gemini grounding gives vertexaisearch.cloud.google.com
    redirect URLs — we follow them with HEAD to recover the real domain."""
    if not uri:
        return ""
    try:
        return urlparse(uri).hostname or ""
    except Exception:
        return ""


def _is_ssrf_blocked(hostname: str) -> bool:
    """Return True if the hostname resolves to a private/loopback/link-local address."""
    try:
        infos = socket.getaddrinfo(hostname, None)
        for info in infos:
            ip_str = info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return True
    except Exception:
        pass
    return False


def resolve_grounding_uri(uri: str, timeout: int = 5) -> str:
    """Gemini grounding emits vertexaisearch redirect URLs. Follow them once to get
    the real publisher domain. Falls back to the redirect host on any error.

    SSRF protection: any resolved host that maps to RFC-1918 / loopback /
    link-local addresses is rejected; the original URI host is returned instead.
    """
    host = host_from_uri(uri)
    if "vertexaisearch.cloud.google.com" not in host and "googleusercontent.com" not in host:
        # Not a redirect URI — still check the host itself against SSRF
        if host and _is_ssrf_blocked(host):
            return host  # blocked, return as-is without fetching
        return host  # already a real domain
    # Validate the redirect URI host before fetching
    if host and _is_ssrf_blocked(host):
        return host
    try:
        r = requests.head(uri, allow_redirects=True, timeout=timeout)
        resolved_host = host_from_uri(r.url) or host
        # Validate the resolved destination
        if resolved_host and _is_ssrf_blocked(resolved_host):
            return host
        return resolved_host
    except Exception:
        # try GET with stream as fallback
        try:
            r = requests.get(uri, allow_redirects=True, timeout=timeout, stream=True)
            resolved_host = host_from_uri(r.url) or host
            r.close()
            # Validate the resolved destination
            if resolved_host and _is_ssrf_blocked(resolved_host):
                return host
            return resolved_host
        except Exception:
            return host


# ---------- LLM transport ----------
def _gemini_call(url: str, prompt: str, grounded: bool = False, timeout: int = 180) -> dict:
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


def call_gemini_pro(prompt: str, grounded: bool = False, timeout: int = 180) -> dict:
    return _gemini_call(GEMINI_PRO_URL, prompt, grounded=grounded, timeout=timeout)


# ---------- Stage 0: Validate URL ----------
def validate_url(url: str) -> dict:
    """Resolve the host and confirm it answers."""
    host = normalize_input_to_domain(url)
    try:
        socket.gethostbyname(host)
    except Exception as e:
        raise ValueError(f"could not resolve {host}: {e}")
    return {"host": host, "url": f"https://{host}/"}


# ---------- Stage 1: Generate queries ----------
QUERY_GEN_PROMPT = """You are a B2B procurement engineer choosing real industrial products.

Visit {url} (use Google Search to learn what {host} sells — product families, catalog structure, typical part-number formats). Then generate 5 plausible procurement-style queries a buyer would ask Claude / GPT / Gemini about products from this manufacturer.

Each query must:
- Cover a different product family if {host} has multiple lines (energy chains, bearings, motors, sensors, fasteners — whatever is real for this manufacturer).
- Be specific enough that the right answer would name a real, orderable SKU or part number from {host}.
- Be the kind of question where a hallucinated answer would actually cost real money (wrong part ordered, project delayed).
- Be 1-2 sentences, plain procurement language. No preambles, no markdown.

Return ONLY a JSON array of 5 strings, no commentary:
["query 1", "query 2", "query 3", "query 4", "query 5"]
"""


def generate_queries(host: str) -> list[str]:
    url = f"https://{host}/"
    raw = call_gemini_pro(
        QUERY_GEN_PROMPT.format(host=host, url=url),
        grounded=True,
        timeout=180,
    )["text"]
    txt = raw.strip()
    if txt.startswith("```"):
        txt = txt.split("```")[1]
        if txt.startswith("json"):
            txt = txt[4:]
        txt = txt.strip()
    # Pull the first JSON array out of the text in case of preamble.
    start = txt.find("[")
    end = txt.rfind("]")
    if start >= 0 and end > start:
        txt = txt[start : end + 1]
    queries = json.loads(txt)
    if not isinstance(queries, list) or len(queries) < 1:
        raise ValueError("Bad query list from Gemini")
    return [str(q).strip() for q in queries[:5] if str(q).strip()]


# ---------- Stage 2: Run grounded query ----------
AGENT_SYSTEM = (
    "You are answering a B2B procurement question. If a real, orderable part number "
    "from a manufacturer's catalog is the right answer, give it. Use Google Search to "
    "ground your answer in real catalog data. If you cannot answer with a specific SKU, "
    "say so plainly. Be concise (under 200 words). Do NOT invent part numbers."
)


def query_agent(query: str) -> dict:
    """Run one query through Gemini Pro with grounding. Returns text + sources."""
    try:
        full = AGENT_SYSTEM + "\n\nQuestion: " + query
        result = call_gemini_pro(full, grounded=True, timeout=180)
        return {"text": result["text"], "sources": result["sources"], "error": None}
    except Exception as e:
        return {"text": "", "sources": [], "error": _safe_err(e)}


# ---------- Stage 3: Source attribution ----------
def attribute_sources(sources: list[dict], manufacturer_brand: str) -> dict:
    """For each source, resolve its real domain and classify it.

    Returns:
      {
        "domains": [{"host": str, "uri": str, "title": str, "class": str}, ...],
        "counts": {"manufacturer": n, "distributor": n, "marketplace": n, "other": n},
        "primary": "manufacturer" | "distributor" | "marketplace" | "other" | "none",
      }
    """
    domains = []
    counts = {"manufacturer": 0, "distributor": 0, "marketplace": 0, "other": 0}

    # Resolve in parallel — vertex redirect URLs need HEAD requests.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(resolve_grounding_uri, s["uri"]): s for s in sources}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                real_host = fut.result()
            except Exception:
                real_host = host_from_uri(s["uri"])
            cls = classify_source(real_host, manufacturer_brand)
            counts[cls] = counts.get(cls, 0) + 1
            domains.append({
                "host": real_host or host_from_uri(s["uri"]),
                "uri": s["uri"],
                "title": s.get("title", ""),
                "class": cls,
            })
    total = sum(counts.values())
    if total == 0:
        primary = "none"
    else:
        # Manufacturer wins ties. Then distributor. Then marketplace. Then other.
        priority = ["manufacturer", "distributor", "marketplace", "other"]
        primary = max(priority, key=lambda c: counts.get(c, 0))
        if counts.get(primary, 0) == 0:
            primary = "none"
    return {"domains": domains, "counts": counts, "primary": primary, "total_sources": total}


# ---------- Stage 4: Categorize ----------
CATEGORIES = ["CORRECT", "HALLUCINATION", "REFUSED", "WRONG_SPEC", "ERROR"]

JUDGE_SYSTEM = """You are a strict procurement judge with Google Search access. Given a query and an agent's response, independently verify whether the answer is correct.

Use Google Search to check the manufacturer's catalog and authoritative distributors (TME, RS, Mouser, Misumi, Octopart, etc.). Then classify the agent response into EXACTLY one category:

- CORRECT: The agent gave a verifiable, real, orderable SKU/answer that you confirmed against the manufacturer's catalog or an authoritative distributor.
- HALLUCINATION: The agent gave a confident specific SKU/part number that you cannot verify exists (wrong format, invalid concatenation, made up).
- REFUSED: The agent declined to answer, deflected to "check the website", or gave only generic advice with no specific SKU.
- WRONG_SPEC: The agent gave a number/spec that contradicts the catalog datasheet (off by 25%+, wrong temperature point, wrong units).

Return ONLY a JSON object: {"category": "CORRECT|HALLUCINATION|REFUSED|WRONG_SPEC", "rationale": "1-2 sentences citing what you found"}"""

JUDGE_PROMPT_TEMPLATE = """Manufacturer: {host}

Query: {q}

Agent response:
{a}

Independently verify the answer with Google Search. Classify. Return JSON only."""


def categorize(host: str, q: str, agent_text: str) -> dict:
    if not agent_text:
        return {"category": "ERROR", "rationale": "No agent response"}
    try:
        prompt = JUDGE_SYSTEM + "\n\n" + JUDGE_PROMPT_TEMPLATE.format(host=host, q=q, a=agent_text)
        result = call_gemini_pro(prompt, grounded=True, timeout=180)
        raw = result["text"]
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
        return {"category": "ERROR", "rationale": f"Judge failed: {_safe_err(e)}"}


# ---------- Pipeline ----------
def analyze_pipeline(url_input: str, progress_cb=None) -> dict:
    started = time.time()
    log: list[str] = []

    def step(msg: str) -> None:
        line = f"[{round(time.time() - started, 1):>5}s] {msg}"
        log.append(line)
        if progress_cb:
            progress_cb(msg)
        print(line, flush=True)

    # Stage 0: validate URL
    info = validate_url(url_input)
    host = info["host"]
    brand = host.split(".")[0]  # 'igus' from 'igus.de'
    # Strip subdomains: 'shop.igus' becomes wrong — use registrable root label
    brand = domain_root(host).split(".")[0]
    step(f"Stage 0: validated {host} (brand={brand})")

    # Stage 1: generate queries
    step(f"Stage 1: generating queries (grounded against {host})")
    queries = generate_queries(host)
    step(f"  Generated {len(queries)} queries")

    # Stage 2 + 3: run grounded query, attribute sources (parallel across queries)
    step(f"Stage 2: running {len(queries)} grounded queries against Gemini Pro")
    rows: list[dict] = [None] * len(queries)  # type: ignore
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(query_agent, q): (i, q) for i, q in enumerate(queries)}
        for fut in as_completed(futs):
            i, q = futs[fut]
            agent_resp = fut.result()
            attribution = attribute_sources(agent_resp.get("sources", []), brand)
            rows[i] = {
                "query": q,
                "agent": agent_resp,
                "attribution": attribution,
            }
    step(f"Stage 3: source attribution complete")

    # Stage 4: categorize each row
    step(f"Stage 4: categorizing answers")
    with ThreadPoolExecutor(max_workers=5) as pool:
        cat_futs = {}
        for i, row in enumerate(rows):
            ar = row["agent"]
            if ar["error"]:
                row["category"] = "ERROR"
                row["rationale"] = ar["error"]
            else:
                cat_futs[pool.submit(categorize, host, row["query"], ar["text"])] = i
        for fut in as_completed(cat_futs):
            i = cat_futs[fut]
            res = fut.result()
            rows[i]["category"] = res["category"]
            rows[i]["rationale"] = res["rationale"]

    # Stage 5: scores
    total = len(rows)
    correct_rows = [r for r in rows if r["category"] == "CORRECT"]
    correct = len(correct_rows)
    visibility = round((correct / total) * 100) if total else 0

    # Source authority: of the CORRECT rows, what % had primary=manufacturer?
    manufacturer_correct = sum(1 for r in correct_rows if r["attribution"]["primary"] == "manufacturer")
    if correct == 0:
        source_authority = 0
    else:
        source_authority = round((manufacturer_correct / correct) * 100)

    breakdown = {c: 0 for c in CATEGORIES}
    source_breakdown = {"manufacturer": 0, "distributor": 0, "marketplace": 0, "other": 0, "none": 0}
    for r in rows:
        breakdown[r["category"]] = breakdown.get(r["category"], 0) + 1
        primary = r["attribution"]["primary"]
        source_breakdown[primary] = source_breakdown.get(primary, 0) + 1

    elapsed = round(time.time() - started, 1)
    step(
        f"Stage 5: visibility={visibility} ({correct}/{total}), "
        f"source_authority={source_authority} ({manufacturer_correct}/{correct} manufacturer-sourced) "
        f"in {elapsed}s"
    )

    return {
        "input": url_input,
        "host": host,
        "brand": brand,
        "visibility_score": visibility,
        "source_authority_score": source_authority,
        "total_queries": total,
        "correct_queries": correct,
        "manufacturer_correct": manufacturer_correct,
        "breakdown": breakdown,
        "source_breakdown": source_breakdown,
        "rows": rows,
        "log": log,
        "elapsed_seconds": elapsed,
    }


# ---------- Async job store ----------
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _run_job(job_id: str, url_input: str) -> None:
    def progress_cb(msg: str) -> None:
        with _jobs_lock:
            _jobs[job_id]["progress"] = msg
            _jobs[job_id]["updated_at"] = time.time()

    try:
        result = analyze_pipeline(url_input, progress_cb=progress_cb)
        with _jobs_lock:
            _jobs[job_id]["status"] = "complete"
            _jobs[job_id]["result"] = result
            _jobs[job_id]["progress"] = "Done"
            _jobs[job_id]["updated_at"] = time.time()
    except Exception as e:
        traceback.print_exc()
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = _safe_err(e)
            _jobs[job_id]["updated_at"] = time.time()


# ---------- FastAPI ----------
app = FastAPI(
    title="UltraRelevant Visibility Tool",
    version="2.0.0",
    description=(
        "URL in. Honest visibility + source-authority scores out. "
        "Async polling: POST /analyze returns a job_id; GET /status/<job_id> returns progress/result."
    ),
    openapi_url="/openapi.json",
    docs_url="/docs",
)


class AnalyzeRequest(BaseModel):
    url: str = Field(..., description="Manufacturer URL or domain (e.g. https://igus.de or igus.de).", min_length=3, max_length=400)


class AnalyzeResponse(BaseModel):
    job_id: str
    status: str = "pending"
    host: str | None = None


class StatusResponse(BaseModel):
    job_id: str
    status: str
    progress: str | None = None
    elapsed: float | None = None
    result: dict | None = None
    error: str | None = None


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "engine": "ultrarelevant",
        "keys": {"gemini": bool(GEMINI_KEY)},
    }


@app.get("/")
def root() -> FileResponse:
    return FileResponse(str(ROOT / "index.html"))


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(body: AnalyzeRequest) -> dict:
    if not GEMINI_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY missing in runtime")
    try:
        info = validate_url(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job_id = f"job_{uuid.uuid4().hex[:12]}"
    started = time.time()
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "pending",
            "input": body.url,
            "host": info["host"],
            "started_at": started,
            "updated_at": started,
            "progress": "Queued",
            "result": None,
            "error": None,
        }

    threading.Thread(target=_run_job, args=(job_id, body.url), daemon=True).start()
    return {"job_id": job_id, "status": "pending", "host": info["host"]}


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



# CORS for static frontends pointing at this engine
_CORS_ALLOWED_ORIGINS = {
    "https://demo.ultrarelevant.com",
    "https://ultrarelevant-visibility.vercel.app",
}
_CORS_VERCEL_PATTERN = re.compile(r"^https://[a-zA-Z0-9-]+-[a-zA-Z0-9-]+\.vercel\.app$")


def _cors_origin(request) -> str | None:
    origin = request.headers.get("origin", "")
    if not origin:
        return None
    if origin in _CORS_ALLOWED_ORIGINS:
        return origin
    if _CORS_VERCEL_PATTERN.match(origin):
        return origin
    return None


@app.middleware("http")
async def cors_middleware(request, call_next):
    allowed_origin = _cors_origin(request)
    response = await call_next(request)
    if allowed_origin:
        response.headers["Access-Control-Allow-Origin"] = allowed_origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.options("/{full_path:path}")
def options_handler(full_path: str) -> JSONResponse:
    return JSONResponse({}, status_code=200)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8766"))
    print(f"GEMINI_API_KEY: {'set' if GEMINI_KEY else 'MISSING'}")
    print(f"Engine starting on http://0.0.0.0:{port}/")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
