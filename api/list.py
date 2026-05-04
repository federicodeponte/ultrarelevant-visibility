"""Vercel serverless function: GET /api/list

Returns the list of cached companies + their score for the front-end's
"try one of these" buttons.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        out = []
        if CACHE_DIR.exists():
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
        # Sort by score ascending (worst first — more impactful for a demo
        # whose entire pitch is "AI agents get this wrong").
        out.sort(key=lambda c: c["score"])
        body = json.dumps({"companies": out}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "public, s-maxage=300")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
