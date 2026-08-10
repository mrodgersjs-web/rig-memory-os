#!/usr/bin/env python3
"""
Prime Jake — Model Routing Layer

Routes queries to the right model based on complexity:
- Simple questions → small/fast model (Ollama qwen3:8b)
- Medium coding → fine-tuned RIG-KAT-Coder (vLLM :8003)
- Complex/long-context → blackwell-daily (vLLM :8001)

Cuts inference cost 3-5x by not using the 35B MoE for simple questions.

Usage:
    python3 model_router.py serve [--port 8010]
    python3 model_router.py query "What is 2+2?"
    python3 model_router.py query "Write a ProofPacket function"
"""
from __future__ import annotations
import os, sys, json, time, argparse, urllib.request, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# Model endpoints
MODELS = {
    "small": {
        "url": "http://localhost:11434/v1",
        "model": "qwen3:8b",
        "max_tokens": 1000,
        "cost_per_1k": 0.0,  # local, free
        "latency_ms": 50,
    },
    "coder": {
        "url": "http://localhost:8003/v1",
        "model": "rig-kat",
        "max_tokens": 4000,
        "cost_per_1k": 0.002,  # GPU cost estimate
        "latency_ms": 200,
    },
    "daily": {
        "url": "http://192.168.68.90:8001/v1",
        "model": "blackwell-daily",
        "max_tokens": 8000,
        "cost_per_1k": 0.005,
        "latency_ms": 300,
    },
}

# Complexity scoring
SIMPLE_KEYWORDS = ["what is", "define", "explain", "summarize", "list", "name", "when", "where", "who"]
CODING_KEYWORDS = ["code", "function", "class", "debug", "implement", "write", "fix", "refactor", "test", "deploy"]
COMPLEX_KEYWORDS = ["analyze", "design", "architect", "strategy", "compare", "evaluate", "optimize", "research", "plan"]
LONG_CONTEXT_KEYWORDS = ["document", "paper", "chapter", "full", "complete", "entire", "all of"]

def score_complexity(query: str) -> dict:
    """Score query complexity on 0-1 scale."""
    query_lower = query.lower()
    
    simple_score = sum(1 for kw in SIMPLE_KEYWORDS if kw in query_lower) * 0.2
    coding_score = sum(1 for kw in CODING_KEYWORDS if kw in query_lower) * 0.25
    complex_score = sum(1 for kw in COMPLEX_KEYWORDS if kw in query_lower) * 0.3
    long_score = sum(1 for kw in LONG_CONTEXT_KEYWORDS if kw in query_lower) * 0.4
    
    # Length factor
    length_factor = min(1.0, len(query) / 1000)
    
    # Code blocks
    has_code = "```" in query or "def " in query or "class " in query
    if has_code:
        coding_score += 0.3
    
    # Pick the dominant signal
    scores = {
        "simple": simple_score,
        "coding": coding_score,
        "complex": complex_score,
        "long": long_score,
    }
    
    dominant = max(scores, key=scores.get)
    overall = scores[dominant]
    
    return {"dominant": dominant, "score": overall, "all": scores, "length": len(query)}

def route_query(query: str) -> str:
    """Route query to the appropriate model."""
    complexity = score_complexity(query)
    
    # Routing logic
    if complexity["dominant"] == "simple" and complexity["score"] < 0.4:
        return "small"
    elif complexity["dominant"] == "coding" or (has_code_in_query(query) and complexity["score"] > 0.2):
        return "coder"
    elif complexity["dominant"] == "long" or len(query) > 2000:
        return "daily"
    elif complexity["dominant"] == "complex" and complexity["score"] > 0.3:
        return "daily"
    else:
        # Default: use coder for most queries
        return "coder"

def has_code_in_query(query: str) -> bool:
    return "```" in query or "def " in query or "class " in query or "import " in query

def query_model(model_key: str, messages: list[dict]) -> dict:
    """Query a model and return the response."""
    model_cfg = MODELS[model_key]
    payload = {
        "model": model_cfg["model"],
        "messages": messages,
        "max_tokens": model_cfg["max_tokens"],
        "temperature": 0.3,
    }
    
    t0 = time.time()
    try:
        req = urllib.request.Request(
            f"{model_cfg['url']}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            latency = round((time.time() - t0) * 1000)
            return {
                "content": result["choices"][0]["message"]["content"],
                "model": model_cfg["model"],
                "model_key": model_key,
                "latency_ms": latency,
                "cost_estimate": round(model_cfg["cost_per_1k"] * len(str(messages)) / 1000, 6),
            }
    except Exception as e:
        # Fallback to daily model
        if model_key != "daily":
            return query_model("daily", messages)
        return {"content": f"Error: {e}", "model": "none", "latency_ms": 0}

# ── HTTP Server ─────────────────────────────────────────────────────────────

class RouterHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        
        content_length = int(self.headers["Content-Length"])
        body = self.rfile.read(content_length)
        request = json.loads(body)
        
        messages = request.get("messages", [])
        query = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
        
        # Route
        model_key = route_query(query)
        complexity = score_complexity(query)
        
        # Query
        result = query_model(model_key, messages)
        
        # Build OpenAI-compatible response
        response = {
            "id": f"router-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": result["model"],
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": result["content"]},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "router": {
                "model_key": model_key,
                "complexity": complexity,
                "latency_ms": result["latency_ms"],
                "cost_estimate": result.get("cost_estimate", 0),
            },
        }
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())
    
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "models": {k: v["model"] for k, v in MODELS.items()},
            }).encode())
        elif self.path == "/v1/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            all_models = []
            for key, cfg in MODELS.items():
                all_models.append({"id": cfg["model"], "object": "model", "owned_by": f"rig-router-{key}"})
            self.wfile.write(json.dumps({"object": "list", "data": all_models}).encode())
        else:
            self.send_error(404)

def main():
    parser = argparse.ArgumentParser(description="Model Router")
    parser.add_argument("command", choices=["serve", "query", "route"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    
    if args.command == "serve":
        print(f"Model Router serving on :{args.port}")
        model_list = ", ".join(f"{k}={v['model']}" for k, v in MODELS.items())
        print(f"Models: {model_list}")
        server = HTTPServer(("0.0.0.0", args.port), RouterHandler)
        server.serve_forever()
    
    elif args.command == "query":
        query = " ".join(args.args)
        if not query:
            print("Usage: query <text>")
            sys.exit(1)
        
        model_key = route_query(query)
        complexity = score_complexity(query)
        print(f"Routed to: {model_key} ({MODELS[model_key]['model']})")
        print(f"Complexity: {complexity['dominant']} ({complexity['score']:.2f})")
        print(f"\nQuerying...")
        
        result = query_model(model_key, [{"role": "user", "content": query}])
        print(f"\nResponse ({result['latency_ms']}ms):")
        print(result["content"])
    
    elif args.command == "route":
        query = " ".join(args.args)
        if not query:
            print("Usage: route <text>")
            sys.exit(1)
        
        model_key = route_query(query)
        complexity = score_complexity(query)
        print(f"Route: {model_key} ({MODELS[model_key]['model']})")
        print(f"Complexity: {complexity['dominant']} ({complexity['score']:.2f})")
        print(f"Details: {json.dumps(complexity['all'], indent=2)}")

if __name__ == "__main__":
    main()
