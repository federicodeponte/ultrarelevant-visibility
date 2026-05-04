"""Vercel serverless function: GET /api/score?company=<name>

Returns the cached visibility-tool result for that company, or 404 with a
list of available companies. Pure cache lookup — no live LLM calls (Vercel
hobby tier has 10s function timeout; the underlying tool takes 90s+).
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


def _slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def _list_available() -> list[dict]:
    out = []
    if not CACHE_DIR.exists():
        return out
    for p in sorted(CACHE_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        out.append({
            "slug": p.stem,
            "name": d.get("company", p.stem),
            "score": d.get("score", 0),
        })
    return out


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — Vercel convention
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        company_in = (qs.get("company", [""])[0] or "").strip()
        if not company_in:
            self._send(400, {"error": "company query param required", "available": _list_available()})
            return

        slug = _slug(company_in)
        # Try exact slug match first, then loose match.
        candidates = [slug]
        if "-" in slug:
            candidates.append(slug.split("-")[0])
        for cand in candidates:
            path = CACHE_DIR / f"{cand}.json"
            if path.exists():
                try:
                    d = json.loads(path.read_text())
                except Exception as e:
                    self._send(500, {"error": f"cache read failed: {e}"})
                    return
                # Add some metadata for the UI.
                d.setdefault("run_date", "Apr-May 2026")
                self._send(200, d)
                return

        # Loose: any cached company whose name contains the input
        for f in CACHE_DIR.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            if slug in (d.get("company", "")).lower().replace(" ", "-"):
                d.setdefault("run_date", "Apr-May 2026")
                self._send(200, d)
                return

        self._send(404, {
            "error": f"No cached run for '{company_in}'",
            "available": _list_available(),
        })

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "public, s-maxage=3600")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
