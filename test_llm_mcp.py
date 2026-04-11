#!/usr/bin/env python3
"""LLM ↔ MCP bridge test: send prompts to local LLMs via LM Studio,
forwarding tool calls to a running idalib-mcp server.

Usage:
    # Start idalib-mcp first:
    IDADIR=/opt/ida-pro-9.3 TVHEADLESS=1 uv run idalib-mcp --port 8745 tests/crackme03.elf &

    # Single prompt:
    python3 test_llm_mcp.py --model devstral "Decompile the main function"

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


# ---------------------------------------------------------------------------
# LM Studio helpers
# ---------------------------------------------------------------------------
def lms_get_models():
    """Return list of LLM model dicts from LM Studio."""
    data = _json_get(f"{LMSTUDIO_BASE}/api/v1/models")
    return [m for m in data.get("models", []) if m.get("type") == "llm"]


def lms_load_model(model_id: str):
    """Load a model; returns instance info."""
    return _json_post(f"{LMSTUDIO_BASE}/api/v1/models/load", {"model": model_id})


def lms_unload_model(instance_id: str):
    """Unload a model instance."""
    return _json_post(f"{LMSTUDIO_BASE}/api/v1/models/unload", {"instance_id": instance_id})


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
                "answer": None,
            }

        choice = resp.get("choices", [{}])[0]
        msg = choice.get("message", {})
        finish = choice.get("finish_reason", "")

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
        reasoning = msg.get("reasoning_content", "")

        return {
            "status": "ok",
            "turns": turn + 1,
            "elapsed": time.time() - start,
            "tool_calls": tool_calls_log,
            "answer": content,
            "reasoning_len": len(reasoning) if reasoning else 0,
        }

    return {
        "status": "max_turns",
        "turns": max_turns,
        "elapsed": time.time() - start,
        "tool_calls": tool_calls_log,
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

        # Load model
        print(f"\n{'='*60}")
        print(f"Loading: {model_id}")
        load_resp = lms_load_model(model_id)
        instance_id = load_resp.get("instance_id", model_id)
        print(f"  Loaded in {load_resp.get('load_time_seconds', '?')}s")

        for prompt_key, prompt_text in prompts.items():
            print(f"\n  [{model_id}] {prompt_key}:")
            result = run_agent(model_id, prompt_text, tools, verbose=verbose)
            result["model"] = model_id
            result["prompt_key"] = prompt_key
            results.append(result)

            status_icon = {"ok": "✅", "max_turns": "⚠️", "error": "❌"}.get(result["status"], "?")
            tc_summary = ", ".join(tc["name"] for tc in result["tool_calls"])
            print(f"  {status_icon} status={result['status']} "
                  f"turns={result['turns']} "
                  f"elapsed={result['elapsed']:.1f}s "
                  f"tools=[{tc_summary}]")
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
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Less verbose output")
    parser.add_argument("--output", "-o", help="Save results to JSON file")
    args = parser.parse_args()

    global MCP_BASE
    MCP_BASE = f"http://127.0.0.1:{args.mcp_port}"

    if args.list_models:
        models = lms_get_models()
        print(f"Available LLM models ({len(models)}):")
        for m in models:
            loaded = len(m.get("loaded_instances", []))
            caps = m.get("capabilities", {})
            print(f"  {m['key']:40s}  {m.get('params_string','?'):10s}  "
                  f"tool_use={caps.get('trained_for_tool_use', False)}  "
                  f"loaded={loaded}")
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
        all_models = ["devstral", "glm-4.7", "gemma-4", "nemotron", "lfm2", "qwen3.5"]
        results = run_matrix(all_models, TEST_PROMPTS, tools,
                             verbose=not args.quiet)
        if args.output:
            with open(args.output, "w") as f:
                json.dump(results, f, indent=2)
            print(f"\nResults saved to {args.output}")

        # Summary table
        print(f"\n{'='*70}")
        print("SUMMARY")
        print(f"{'='*70}")
        for r in results:
            icon = {"ok": "✅", "max_turns": "⚠️", "error": "❌"}.get(r["status"], "?")
            print(f"  {icon} {r['model']:35s} {r['prompt_key']:20s} "
                  f"turns={r['turns']} {r['elapsed']:.1f}s")
        return

    # Single prompt mode
    if not args.prompt:
        parser.print_help()
        print("\nExample prompts:")
        for k, v in TEST_PROMPTS.items():
            print(f"  --prompt-key {k}: {v[:80]}")
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
    print(f"  Loaded in {load_resp.get('load_time_seconds', '?')}s")

    print(f"\n[{model_id}] {args.prompt[:80]}...")
    result = run_agent(model_id, args.prompt, tools,
                       max_turns=args.max_turns, verbose=not args.quiet)

    icon = {"ok": "✅", "max_turns": "⚠️", "error": "❌"}.get(result["status"], "?")
    print(f"\n{icon} status={result['status']} turns={result['turns']} "
          f"elapsed={result['elapsed']:.1f}s")
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
