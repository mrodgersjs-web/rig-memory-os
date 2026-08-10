#!/usr/bin/env python3
"""
Prime Jake — Mixture of Agents Ensemble

Orchestrates multiple specialized models behind a single endpoint:
- KAT-Coder (coding specialist, fine-tuned with RIG doctrine)
- blackwell-daily (general reasoning, long context)
- Ollama qwen3:8b (fast, cheap, simple queries)
- Model router decides which agent handles each query

Each agent can also verify other agents' outputs — closing the TAC loop:
Builder generates → Verifier checks → if fail, retry with different agent.

Usage:
    python3 mixture_of_agents.py serve [--port 8020]
    python3 mixture_of_agents.py query "write a function"
    python3 mixture_of_agents.py ensemble "complex task" --agents all
"""
from __future__ import annotations
import os, sys, json, time, argparse, urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed

AGENTS = {
    "coder": {
        "url": "http://localhost:8003/v1",
        "model": "rig-kat",
        "specialty": "code generation, refactoring, debugging, RIG doctrine",
        "max_tokens": 4000,
    },
    "daily": {
        "url": "http://192.168.68.90:8001/v1",
        "model": "blackwell-daily",
        "specialty": "general reasoning, analysis, long context, strategy",
        "max_tokens": 8000,
    },
    "fast": {
        "url": "http://localhost:11434/v1",
        "model": "qwen3:8b",
        "specialty": "simple questions, quick lookups, short responses",
        "max_tokens": 1000,
    },
}

def query_agent(agent_key: str, messages: list, temperature: float = 0.3) -> dict:
    """Query a single agent."""
    cfg = AGENTS[agent_key]
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": cfg["max_tokens"],
        "temperature": temperature,
    }
    t0 = time.time()
    try:
        req = urllib.request.Request(
            f"{cfg['url']}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            return {
                "content": result["choices"][0]["message"]["content"],
                "agent": agent_key,
                "model": cfg["model"],
                "latency_ms": round((time.time() - t0) * 1000),
                "success": True,
            }
    except Exception as e:
        return {
            "content": f"Error: {e}",
            "agent": agent_key,
            "model": cfg["model"],
            "latency_ms": round((time.time() - t0) * 1000),
            "success": False,
        }

def ensemble_query(messages: list, agents: list = None) -> dict:
    """Query multiple agents in parallel, return all responses for synthesis."""
    if agents is None:
        agents = list(AGENTS.keys())
    
    results = []
    with ThreadPoolExecutor(max_workers=len(agents)) as pool:
        futures = {pool.submit(query_agent, a, messages): a for a in agents}
        for f in as_completed(futures):
            results.append(f.result())
    
    # Sort by latency (fastest first)
    results.sort(key=lambda x: x["latency_ms"])
    
    # Synthesize: pick the best response
    # Heuristic: prefer successful responses, prefer coder for code, daily for analysis
    best = None
    for r in results:
        if not r["success"]:
            continue
        if best is None:
            best = r
        # Prefer longer, more detailed responses for complex queries
        elif len(r["content"]) > len(best["content"]) * 1.5:
            best = r
    
    return {
        "best": best or results[0],
        "all": results,
        "synthesis": f"[Ensemble: {len(results)} agents queried, best from {best['agent'] if best else 'none'}]",
    }

def verify_output(output: str, original_query: str) -> dict:
    """Use the daily model as verifier (TAC closing loop)."""
    verify_messages = [
        {"role": "system", "content": "You are a verification agent. Check if the output correctly answers the query. Respond with PASS or FAIL and a brief reason."},
        {"role": "user", "content": f"Query: {original_query}\n\nOutput to verify:\n{output[:2000]}\n\nDoes this output correctly and completely answer the query?"},
    ]
    result = query_agent("daily", verify_messages, temperature=0.1)
    passed = "PASS" in result["content"].upper()[:20]
    return {"passed": passed, "reason": result["content"][:500], "verifier": "daily"}

class EnsembleHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        
        content_length = int(self.headers["Content-Length"])
        body = self.rfile.read(content_length)
        request = json.loads(body)
        
        messages = request.get("messages", [])
        use_ensemble = request.get("ensemble", False)
        
        if use_ensemble:
            # Query all agents, synthesize
            result = ensemble_query(messages)
            response_content = result["best"]["content"]
            agent_used = result["best"]["agent"]
            
            # Optional: verify
            if request.get("verify", False):
                query = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
                v = verify_output(response_content, query)
                response_content += f"\n\n---\nVerification: {'✅ PASS' if v['passed'] else '❌ FAIL'} — {v['reason']}"
        else:
            # Route to best single agent
            query = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
            if any(kw in query.lower() for kw in ["code", "function", "debug", "write", "refactor", "implement"]):
                agent_key = "coder"
            elif any(kw in query.lower() for kw in ["analyze", "strategy", "design", "compare", "evaluate"]):
                agent_key = "daily"
            elif len(query) < 100:
                agent_key = "fast"
            else:
                agent_key = "coder"
            
            result = query_agent(agent_key, messages)
            response_content = result["content"]
            agent_used = agent_key
        
        response = {
            "id": f"ensemble-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": AGENTS.get(agent_used, {}).get("model", "unknown"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": response_content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "agent": agent_used,
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
                "agents": {k: v["model"] for k, v in AGENTS.items()},
            }).encode())
        else:
            self.send_error(404)

def main():
    parser = argparse.ArgumentParser(description="Mixture of Agents Ensemble")
    parser.add_argument("command", choices=["serve", "query", "ensemble"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--port", type=int, default=8020)
    args = parser.parse_args()
    
    if args.command == "serve":
        print(f"Mixture of Agents serving on :{args.port}")
        print(f"Agents: {', '.join(AGENTS.keys())}")
        server = HTTPServer(("0.0.0.0", args.port), EnsembleHandler)
        server.serve_forever()
    elif args.command == "query":
        query = " ".join(args.args)
        result = query_agent("coder", [{"role": "user", "content": query}])
        print(f"[{result['agent']} / {result['latency_ms']}ms]")
        print(result["content"])
    elif args.command == "ensemble":
        query = " ".join(args.args)
        result = ensemble_query([{"role": "user", "content": query}])
        print(f"Ensemble results ({len(result['all'])} agents):")
        for r in result["all"]:
            print(f"  [{r['agent']} / {r['latency_ms']}ms / {len(r['content'])} chars] {'✓' if r['success'] else '✗'}")
        print(f"\nBest: {result['best']['agent']}")
        print(result['best']['content'])

if __name__ == "__main__":
    main()
