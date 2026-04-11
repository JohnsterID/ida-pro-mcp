#!/usr/bin/env python3
"""LLM ↔ MCP bridge test: send prompts to local LLMs via LM Studio,
forwarding tool calls to a running idalib-mcp server.

Usage:
    # Start idalib-mcp first:
    IDADIR=/opt/ida-pro-9.3 TVHEADLESS=1 uv run idalib-mcp --port 8745 tests/crackme03.elf &

    # Single prompt:
    python3 test_llm_mcp.py -m devstral "Decompile the main function"

    # Matrix test (all models × all prompts):
    python3 test_llm_mcp.py --matrix

    # List available models:
    python3 test_llm_mcp.py --list-models
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LMSTUDIO_BASE = "http://192.168.0.241:1234"
MCP_BASE = "http://127.0.0.1:8745"
MAX_TURNS = 10
DEFAULT_MAX_TOKENS = 4096

# Per-model context_length overrides (24 GB RX 7900 XTX, ROCm).
#
# LM Studio auto-calculates context from free VRAM after loading weights.
# For models that fit fully on GPU (gemma, devstral, nemotron, lfm2),
# auto works well.  For GLM and Qwen, auto gives only 4096 because
# weights nearly fill VRAM — we force higher values, accepting that
# some KV cache or layers will spill to CPU.
#
# flash_attention behavior (from 2026-04-11.1.log):
#   gemma-4, glm-4.7: "auto" → auto-enabled by LM Studio (native FA support)
#   nemotron:          "disabled" unless explicitly set — MUST send true
#   devstral, lfm2, qwen3.5: "enabled" when sent
# We always send flash_attention=true for consistency.
#
# VRAM budget at forced values (all with flash_attention, from log):
#   gemma  58K: 16003 wt + 2040 KV + 533 compute = 18576 (5984 free) ✅
#   glm    65K: 17063 wt + 3384 KV + 401 compute = 20848 (3712 free) ✅
#   devstr 61K: 13302 wt + 9600 KV + 344 compute = 23246 (1314 free) ✅
#   qwen   94K: 15871 wt + 1840 KV + 497 compute = 18208 (6352 free) ✅
#   nemo  131K:  2429 wt + 2048 KV + 407 compute =  4884 (19676 free) ✅
#   lfm2  128K: 13745 wt + 2500 KV + 391 compute = 16636 (7924 free) ✅
MODEL_CONFIGS = {
    "gemma-4": {
        "context_length": 58368,   # matches auto; 31/31 layers on GPU
    },
    "glm-4.7": {
        "context_length": 65536,   # auto=4096 too low; 47/48 layers, 72 MiB KV on CPU
    },
    "devstral": {
        "context_length": 61440,   # auto=35914; 61440 tested OK, 1.3 GiB headroom
    },
    "qwen3.5": {
        "context_length": 94208,   # auto=4096 too low; 33/41 layers, 368 MiB KV on CPU
    },
    "nemotron": {
        "context_length": 131072,  # auto=488K wastes VRAM; 131K sufficient
    },
    "lfm2": {
        "context_length": 128000,  # model's max_context_length cap
    },
}


def _model_config(model_id: str) -> dict:
    """Look up MODEL_CONFIGS entry by partial key match against a full model id."""
    model_lower = model_id.lower()
    for key, cfg in MODEL_CONFIGS.items():
        if key in model_lower:
            return cfg
    return {}


# ---------------------------------------------------------------------------
# LM Studio helpers
# ---------------------------------------------------------------------------
def lms_get_models():
    """Return list of LLM model dicts from LM Studio."""
    data = _json_get(f"{LMSTUDIO_BASE}/api/v1/models")
    return [m for m in data.get("models", []) if m.get("type") == "llm"]


def lms_load_model(model_id: str, context_length: int | None = None):
    """Load a model with flash_attention + context_length; returns instance info."""
    payload = {
        "model": model_id,
        "flash_attention": True,
        "echo_load_config": True,
    }
    ctx = context_length or _model_config(model_id).get("context_length")
    if ctx:
        payload["context_length"] = ctx
    return _json_post(f"{LMSTUDIO_BASE}/api/v1/models/load", payload)


def lms_unload_model(instance_id: str):
    """Unload a model instance."""
    return _json_post(f"{LMSTUDIO_BASE}/api/v1/models/unload", {"instance_id": instance_id})


def lms_warmup(model_id: str):
    """Send a trivial chat to warm up KV caches (important for Gemma 4 ISWA)."""
    lms_chat(model_id, [{"role": "user", "content": "hi"}], max_tokens=1)


def lms_chat(model_id: str, messages: list[dict], tools: list[dict] | None = None,
             max_tokens: int = DEFAULT_MAX_TOKENS, temperature: float = 0.1) -> dict:
    """Send chat completion request with optional tool definitions."""
    payload = {
        "model": model_id,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return _json_post(f"{LMSTUDIO_BASE}/v1/chat/completions", payload)


# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------
def mcp_call(method: str, params: dict | None = None) -> dict:
    """Send a JSON-RPC 2.0 request to the idalib-mcp server."""
    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000),
        "method": method,
        "params": params or {},
    }
    return _json_post(f"{MCP_BASE}/mcp", payload)


def mcp_get_tools() -> list[dict]:
    """Fetch tool list from MCP server and convert to OpenAI function format."""
    resp = mcp_call("tools/list")
    mcp_tools = resp.get("result", {}).get("tools", [])
    openai_tools = []
    for t in mcp_tools:
        schema = t.get("inputSchema", {})
        # Strip outputSchema / extra keys that OpenAI format doesn't use
        openai_tools.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": schema,
            },
        })
    return openai_tools


def mcp_call_tool(name: str, arguments: dict) -> str:
    """Call an MCP tool and return the text result."""
    resp = mcp_call("tools/call", {"name": name, "arguments": arguments})
    result = resp.get("result", resp.get("error", {}))

    # MCP returns {"content": [{"type": "text", "text": "..."}]}
    if isinstance(result, dict):
        content = result.get("content", [])
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict)]
            if texts:
                return "\n".join(texts)
        # Error case
        if "message" in result:
            return f"ERROR: {result['message']}"

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Core agent loop
# ---------------------------------------------------------------------------
def _extract_usage(resp: dict) -> dict:
    """Pull token counts and compute tok/s from a chat completion response."""
    usage = resp.get("usage", {})
    details = usage.get("completion_tokens_details", {})
    return {
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "reasoning_tokens": details.get("reasoning_tokens", 0),
    }


def run_agent(model_id: str, prompt: str, tools: list[dict],
              max_turns: int = MAX_TURNS, verbose: bool = True) -> dict:
    """Run an agentic loop: prompt → tool calls → results → final answer."""
    messages = [
        {"role": "system", "content": (
            "You are a reverse engineering assistant with access to IDA Pro tools. "
            "Use the provided tools to analyze binaries. You can pass either hex "
            "addresses (like 0x1234) or function names (like 'main', 'check_pw') "
            "to any tool that accepts addresses."
        )},
        {"role": "user", "content": prompt},
    ]

    tool_calls_log = []
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    truncated = False
    start = time.time()

    for turn in range(max_turns):
        resp = lms_chat(model_id, messages, tools)

        if "error" in resp:
            return {
                "status": "error",
                "error": str(resp["error"]),
                "turns": turn + 1,
                "elapsed": time.time() - start,
                "tool_calls": tool_calls_log,
                "usage": total_usage,
                "truncated": truncated,
                "answer": None,
            }

        # Accumulate token usage across turns
        turn_usage = _extract_usage(resp)
        for k in total_usage:
            total_usage[k] += turn_usage[k]

        choice = resp.get("choices", [{}])[0]
        msg = choice.get("message", {})
        finish = choice.get("finish_reason", "")

        if finish == "length":
            truncated = True
            if verbose:
                ratio = turn_usage["reasoning_tokens"] / max(turn_usage["completion_tokens"], 1)
                print(f"    ⚠ finish_reason=length (reasoning used "
                      f"{turn_usage['reasoning_tokens']}/{turn_usage['completion_tokens']} "
                      f"completion tokens = {ratio:.0%})")

        # Append assistant message to history
        messages.append(msg)

        # Check for tool calls
        tc_list = msg.get("tool_calls", [])
        if tc_list:
            for tc in tc_list:
                fn = tc.get("function", {})
                fn_name = fn.get("name", "")
                fn_args_raw = fn.get("arguments", "{}")
                try:
                    fn_args = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else fn_args_raw
                except json.JSONDecodeError:
                    fn_args = {}

                if verbose:
                    print(f"    -> {fn_name}({json.dumps(fn_args)[:120]})")

                tool_result = mcp_call_tool(fn_name, fn_args)
                tool_calls_log.append({
                    "name": fn_name,
                    "args": fn_args,
                    "result_len": len(tool_result),
                })

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": tool_result[:8000],  # Truncate huge results
                })
            continue  # Next turn

        # No tool calls → final answer
        content = msg.get("content", "") or ""

        elapsed = time.time() - start
        tok_s = total_usage["completion_tokens"] / elapsed if elapsed > 0 else 0

        return {
            "status": "ok",
            "turns": turn + 1,
            "elapsed": elapsed,
            "tok_s": round(tok_s, 1),
            "tool_calls": tool_calls_log,
            "usage": total_usage,
            "truncated": truncated,
            "answer": content,
        }

    elapsed = time.time() - start
    tok_s = total_usage["completion_tokens"] / elapsed if elapsed > 0 else 0

    return {
        "status": "max_turns",
        "turns": max_turns,
        "elapsed": elapsed,
        "tok_s": round(tok_s, 1),
        "tool_calls": tool_calls_log,
        "usage": total_usage,
        "truncated": truncated,
        "answer": None,
    }


# ---------------------------------------------------------------------------
# Test prompts
# ---------------------------------------------------------------------------
TEST_PROMPTS = {
    "survey": "Use survey_binary to get an overview of this binary.",
    "decompile_main": "Decompile the main function and explain what it does.",
    "xrefs_check_pw": "Find what calls the check_pw function using xrefs_to.",
    "list_strings": "List the strings in this binary using list_strings or entity_query.",
    "rename_roundtrip": (
        "Find the main function address with lookup_funcs, then rename it to "
        "'test_renamed_main' using rename, verify with lookup_funcs, then "
        "rename it back to 'main'."
    ),
}


# ---------------------------------------------------------------------------
# Matrix runner
# ---------------------------------------------------------------------------
def resolve_model(partial: str, available: list[dict]) -> str | None:
    """Resolve a partial model name to a full model key."""
    partial_lower = partial.lower()
    for m in available:
        key = m["key"]
        if partial_lower in key.lower():
            return key
    return None


def run_matrix(models: list[str], prompts: dict[str, str], tools: list[dict],
               verbose: bool = True) -> list[dict]:
    """Run all model × prompt combinations."""
    results = []
    available = lms_get_models()

    for model_partial in models:
        model_id = resolve_model(model_partial, available)
        if not model_id:
            print(f"  ⚠ Model '{model_partial}' not found, skipping")
            continue

        # Load model with optimal context_length + flash_attention
        print(f"\n{'='*60}")
        print(f"Loading: {model_id}")
        load_resp = lms_load_model(model_id)
        instance_id = load_resp.get("instance_id", model_id)
        lcfg = load_resp.get("load_config", {})
        print(f"  {load_resp.get('load_time_seconds', '?'):.1f}s  "
              f"ctx={lcfg.get('context_length', '?')}  "
              f"flash={lcfg.get('flash_attention', '?')}  "
              f"kv_gpu={lcfg.get('offload_kv_cache_to_gpu', '?')}")

        # Warmup KV caches
        print("  Warming up...", end="", flush=True)
        t0 = time.time()
        lms_warmup(model_id)
        print(f" {time.time()-t0:.1f}s")

        for prompt_key, prompt_text in prompts.items():
            print(f"\n  [{model_id}] {prompt_key}:")
            result = run_agent(model_id, prompt_text, tools, verbose=verbose)
            result["model"] = model_id
            result["prompt_key"] = prompt_key
            results.append(result)

            status_icon = {"ok": "✅", "max_turns": "⚠️", "error": "❌"}.get(result["status"], "?")
            tc_summary = ", ".join(tc["name"] for tc in result["tool_calls"])
            usage = result.get("usage", {})
            trunc = " TRUNCATED" if result.get("truncated") else ""
            print(f"  {status_icon} status={result['status']} "
                  f"turns={result['turns']} "
                  f"{result['elapsed']:.1f}s "
                  f"{result.get('tok_s', 0)} tok/s "
                  f"(comp={usage.get('completion_tokens',0)} "
                  f"reason={usage.get('reasoning_tokens',0)}){trunc}")
            print(f"  tools=[{tc_summary}]")
            if result["answer"]:
                print(f"  answer: {result['answer'][:200]}...")

        # Unload model
        lms_unload_model(instance_id)
        print(f"  Unloaded {model_id}")

    return results


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _json_get(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _json_post(url: str, payload: dict, timeout: int = 600) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return {"error": f"HTTP {e.code}: {body[:500]}"}
    except urllib.error.URLError as e:
        return {"error": f"Connection error: {e.reason}"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="LLM ↔ MCP bridge test")
    parser.add_argument("prompt", nargs="?", help="Single prompt to run")
    parser.add_argument("--model", "-m", default="devstral",
                        help="Model name (partial match, default: devstral)")
    parser.add_argument("--matrix", action="store_true",
                        help="Run all models × all prompts")
    parser.add_argument("--list-models", action="store_true",
                        help="List available LM Studio models")
    parser.add_argument("--list-tools", action="store_true",
                        help="List available MCP tools")
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS,
                        help=f"Max agent turns (default: {MAX_TURNS})")
    parser.add_argument("--mcp-port", type=int, default=8745,
                        help="MCP server port (default: 8745)")
    parser.add_argument("--lmstudio", default=None,
                        help="LM Studio base URL (default: %(default)s)")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Less verbose output")
    parser.add_argument("--output", "-o", help="Save results to JSON file")
    args = parser.parse_args()

    global MCP_BASE, LMSTUDIO_BASE
    MCP_BASE = f"http://127.0.0.1:{args.mcp_port}"
    if args.lmstudio:
        LMSTUDIO_BASE = args.lmstudio.rstrip("/")

    if args.list_models:
        models = lms_get_models()
        print(f"Available LLM models ({len(models)}) at {LMSTUDIO_BASE}:")
        for m in models:
            loaded = len(m.get("loaded_instances", []))
            key = m["key"]
            cfg = _model_config(key)
            ctx = cfg.get("context_length", "")
            ctx_str = f"  ctx={ctx}" if ctx else ""
            print(f"  {key:50s}  {m.get('params_string','?'):10s}  "
                  f"loaded={loaded}{ctx_str}")
        return

    if args.list_tools:
        tools = mcp_get_tools()
        print(f"Available MCP tools ({len(tools)}):")
        for t in tools:
            fn = t["function"]
            params = fn.get("parameters", {}).get("properties", {})
            param_names = ", ".join(params.keys())
            print(f"  {fn['name']:30s}  ({param_names})")
            if not args.quiet:
                print(f"    {fn['description'][:100]}")
        return

    # Get MCP tools
    try:
        tools = mcp_get_tools()
        print(f"Connected to MCP server ({len(tools)} tools)")
    except Exception as e:
        print(f"❌ Cannot connect to MCP server at {MCP_BASE}: {e}")
        print("   Start it with: IDADIR=/opt/ida-pro-9.3 TVHEADLESS=1 "
              "uv run idalib-mcp --port 8745 tests/crackme03.elf")
        sys.exit(1)

    if args.matrix:
        # Ranked order from v3 testing (best → worst)
        all_models = ["gemma-4", "glm-4.7", "devstral", "qwen3.5", "nemotron", "lfm2"]
        results = run_matrix(all_models, TEST_PROMPTS, tools,
                             verbose=not args.quiet)
        if args.output:
            with open(args.output, "w") as f:
                json.dump(results, f, indent=2)
            print(f"\nResults saved to {args.output}")

        # Summary table
        print(f"\n{'='*80}")
        print("SUMMARY")
        print(f"{'='*80}")
        for r in results:
            icon = {"ok": "✅", "max_turns": "⚠️", "error": "❌"}.get(r["status"], "?")
            trunc = " TRUNC" if r.get("truncated") else ""
            print(f"  {icon} {r['model']:40s} {r['prompt_key']:20s} "
                  f"turns={r['turns']} {r['elapsed']:5.1f}s "
                  f"{r.get('tok_s', 0):5.1f} tok/s{trunc}")
        return

    # Single prompt mode
    if not args.prompt:
        parser.print_help()
        print("\nExample prompts:")
        for k, v in TEST_PROMPTS.items():
            print(f"  {k}: {v[:80]}")
        return

    available = lms_get_models()
    model_id = resolve_model(args.model, available)
    if not model_id:
        print(f"❌ Model '{args.model}' not found. Available:")
        for m in available:
            print(f"  {m['key']}")
        sys.exit(1)

    print(f"Loading {model_id}...")
    load_resp = lms_load_model(model_id)
    instance_id = load_resp.get("instance_id", model_id)
    lcfg = load_resp.get("load_config", {})
    print(f"  {load_resp.get('load_time_seconds', '?'):.1f}s  "
          f"ctx={lcfg.get('context_length', '?')}  "
          f"flash={lcfg.get('flash_attention', '?')}  "
          f"kv_gpu={lcfg.get('offload_kv_cache_to_gpu', '?')}")

    # Warmup
    print("  Warming up...", end="", flush=True)
    t0 = time.time()
    lms_warmup(model_id)
    print(f" {time.time()-t0:.1f}s")

    print(f"\n[{model_id}] {args.prompt[:80]}...")
    result = run_agent(model_id, args.prompt, tools,
                       max_turns=args.max_turns, verbose=not args.quiet)

    icon = {"ok": "✅", "max_turns": "⚠️", "error": "❌"}.get(result["status"], "?")
    usage = result.get("usage", {})
    trunc = " TRUNCATED" if result.get("truncated") else ""
    print(f"\n{icon} status={result['status']} turns={result['turns']} "
          f"{result['elapsed']:.1f}s {result.get('tok_s', 0)} tok/s "
          f"(comp={usage.get('completion_tokens',0)} "
          f"reason={usage.get('reasoning_tokens',0)}){trunc}")
    if result.get("answer"):
        print(f"\nAnswer:\n{result['answer']}")
    if result.get("error"):
        print(f"\nError: {result['error']}")

    lms_unload_model(instance_id)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)


if __name__ == "__main__":
    main()
