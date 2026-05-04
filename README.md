# UltraRelevant Visibility Tool

How visible are your products to AI agents? Live measurement tool for B2B manufacturers.

**Live demo:** https://ultrarelevant-visibility.vercel.app

## What it measures

Eight industrial brands, scored on how often AI agents return the correct part number when a buyer asks a real procurement question. Lower score = more hallucination, more refusals, more wrong specs.

| Company             | Score   | Cells correct |
|---------------------|---------|---------------|
| ABB                 | 11/100  | 1 / 9         |
| Bosch Rexroth       | 11/100  | 1 / 9         |
| Festo               | 11/100  | 1 / 9         |
| igus                | 20/100  | 3 / 15        |
| Siemens             | 22/100  | 2 / 9         |
| SKF                 | 22/100  | 2 / 9         |
| BASF                | 33/100  | 3 / 9         |
| Schneider Electric  | 33/100  | 3 / 9         |

(Scores read directly from `cache/*.json` in this repo. Worst first.)

## API

Two read-only endpoints, no auth, free to call:

```bash
# List cached companies + scores
curl https://ultrarelevant-visibility.vercel.app/api/list

# Get the full result for one company (questions, agent answers, ground truth, per-cell verdict)
curl "https://ultrarelevant-visibility.vercel.app/api/score?company=Siemens"
```

## Methodology

For each company:

1. Generate 5 plausible procurement queries (Gemini 3 Pro). Real buyer-style questions like *"give me the part number for a Modicon M241 with 40 I/O and Ethernet/IP"*.
2. Ask 3 AI agents per query. 5 queries x 3 agents = 15 cells (some companies use the faster 3-query x 3-agent variant = 9 cells).
3. Fetch ground truth per query via Gemini search grounding against the manufacturer's own catalog and datasheets.
4. Categorize each agent answer: CORRECT / HALLUCINATION (made-up part number) / REFUSED / WRONG_SPEC / ERROR. Judge: Gemini 2.5 Flash.
5. Score = correct cells / total cells * 100.

The three agents are: **Gemini 3 Pro**, **Gemini 2.5 Flash**, **NVIDIA gpt-oss-120b** (via NIM).

## Stack

- **Frontend:** static `index.html`, vanilla JS, no build step.
- **API:** two Python serverless functions on Vercel (`api/list.py`, `api/score.py`). Pure cache lookups — Vercel hobby has a 10s timeout, the live pipeline takes 90s+.
- **Engine:** `local_engine.py` is a FastAPI app that runs the full pipeline. Used to generate the cached results in this repo. Run it yourself with the instructions below.
- **Models:** Gemini 3 Pro + Gemini 2.5 Flash + NVIDIA gpt-oss-120b.

## Run live for any company

The Vercel deploy is cache-only. To run live (no timeout, any company):

1. Deploy `render.yaml` to [Render](https://render.com).
2. Set env vars on the service: `GEMINI_API_KEY`, `NVIDIA_API_KEY`.
3. Hit `POST /analyze` with `{"company_name": "Your Company"}`, then poll `GET /status/<job_id>`.

Or run the engine locally:

```bash
pip install fastapi uvicorn requests pydantic
GEMINI_API_KEY=... NVIDIA_API_KEY=... python3 local_engine.py
# engine listens on :8766
```

## Layout

```
index.html               static UI (vanilla JS, no build step)
api/
  list.py                GET /api/list
  score.py               GET /api/score?company=<name>
cache/
  <slug>.json            cached run output, one per company
vercel.json              Vercel config (static + python serverless)
render.yaml              alternative Render deploy (live runs, no timeout)
local_engine.py          FastAPI engine for generating new cache entries
generate_cache.sh        helper to batch-run companies through the engine
```

## About

Built by [UltraRelevant](https://ultrarelevant.com). We help B2B manufacturers fix the data their AI agents need: clean catalog, structured specs, agent-readable product feeds. The visibility score is the diagnostic; UltraRelevant is the fix.

## License

MIT. See [LICENSE](LICENSE).
