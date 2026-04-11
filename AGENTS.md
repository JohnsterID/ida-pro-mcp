# AGENTS.md — Repository Knowledge for AI Agents

## Project Overview
IDA Pro MCP Server: exposes IDA Pro / idalib functionality to MCP clients.
See CLAUDE.md for full development guidance.

## Key Patterns

### parse_address() resolves names
`parse_address()` in `utils.py` resolves both hex addresses AND symbol names via
`idaapi.get_name_ea()`. All tools that accept addresses (xrefs_to, callees,
get_bytes, disasm, etc.) automatically support passing function/symbol names.
Merged in PR #349.

### Test Commands
```bash
# Single fixture
IDADIR=/opt/ida-pro-9.3 TVHEADLESS=1 uv run ida-mcp-test tests/crackme03.elf -q
# Both fixtures
IDADIR=/opt/ida-pro-9.3 TVHEADLESS=1 uv run ida-mcp-test tests/typed_fixture.elf -q
# All IDA versions
bash /workspace/project/test_all_versions.sh
```

### Git Authorship
Author: JohnsterID <69278611+JohnsterID@users.noreply.github.com>
No Co-authored-by lines. See GIT_COMMIT_AUTHORSHIP_INSTRUCTIONS.md.

---

## Local LLM Testing with LM Studio

### Hardware
i9-13900H, 64GB RAM, AMD RX 7900 XTX (24GB VRAM, ROCm)

### Setup
Start MCP server on mapped port for LM Studio access:
```bash
IDADIR=/opt/ida-pro-9.3 TVHEADLESS=1 uv run idalib-mcp --host 0.0.0.0 --port 8011 tests/crackme03.elf
```
Port 8011 inside container maps to host:38123 for LM Studio at 192.168.0.241:1234.

Run LLM test matrix (MCP server must be running):
```bash
python3 test_llm_mcp.py --mcp-port 8011 --matrix        # all models × all prompts
python3 test_llm_mcp.py --mcp-port 8011 -m devstral "Decompile main"  # single test
python3 test_llm_mcp.py --list-models                    # check available models
```

### LM Studio Load API Parameters (confirmed 2026-04-11)
```
context_length      — max tokens (auto-calculates from VRAM if omitted)
flash_attention     — DEFAULTS TO FALSE, must explicitly set true
echo_load_config    — returns actual applied config in response
num_experts         — MoE expert count
offload_kv_cache_to_gpu — defaults true
eval_batch_size     — defaults 512
```
`gpu_offload` (layer count) is NOT an API parameter — set in LM Studio UI.

### flash_attention Behavior by Architecture (from 2026-04-11.1.log)
| Model | Architecture | Default flash_attn | Notes |
|---|---|---|---|
| gemma-4 | gemma4 | auto → enabled | Native FA support, auto-enables |
| glm-4.7 | deepseek2 | auto → enabled | Native FA support, auto-enables |
| nemotron | nemotron_h | **disabled** | MUST send flash_attention=true |
| devstral | mistral3 | needs explicit | enabled when sent |
| lfm2 | lfm2moe | needs explicit | enabled when sent |
| qwen3.5 | qwen35moe | needs explicit | enabled when sent |

Compute buffer with flash OFF vs ON (nemotron 131K ctx):
10523 MiB → 407 MiB (**26x reduction**). Always send flash_attention=true.

### VRAM Budget (24 GB / 24560 MiB, all with flash_attention, from log)
| Model | Layers | GPU Weights | KV GPU | KV CPU | Compute | Total GPU | Free |
|---|---|---|---|---|---|---|---|
| gemma 58K | 31/31 | 16003 | 2040 | - | 533 | 18576 | 5984 ✅ |
| glm 65K | 47/48 | 17063 | 3312 | 72 | 401 | 20776 | 3784 ✅ |
| devstral 61K | 41/41 | 13302 | 9600 | - | 344 | 23246 | 1314 ✅ |
| qwen 94K | 33/41 | 15871 | 1472 | 368 | 497 | 17840 | 6720 ✅ |
| nemotron 131K | 43/43 | 2429 | 2048 | - | 407 | 4884 | 19676 ✅ |
| lfm2 128K | 41/41 | 13745 | 2500 | - | 391 | 16636 | 7924 ✅ |

All values in MiB. Devstral at 61K is tightest (1314 free) but tested working.

### Other LM Studio Notes
- Model unload requires `instance_id` (not `model`) field
- `/v1/chat/completions` (OAI-compat) is more reliable for tool use than native MCP
- Native MCP via `/api/v1/chat` `integrations` key works but crashes with Gemma 4
- `kv_unified=true` for all models — `n_parallel` does NOT multiply KV cache
- `usage.completion_tokens_details.reasoning_tokens` tracks thinking overhead

### LLM Model Rankings (v3 — 2026-04-08, flash_attention was OFF)

**⚠️ Speed numbers below were measured WITHOUT flash_attention.** Gemma 4 measured
76 tok/s with flash_attention=true (2026-04-11) vs 12 tok/s without. All models
need re-benchmarking with flash_attention=true.

| Rank | Model | Params | Context | Score | tok/s (no FA) |
|---|---|---|---|---|---|
| 1 | gemma-4-26b-a4b | 26B-A4B | 58K | 500/500 100% | 12 (76 w/ FA) |
| 2 | glm-4.7-flash | 30B | 65K | 430/500 86% | 55 |
| 3 | devstral-small-2 | 24B | 61K | 430/500 86% | 9.5 |
| 4 | qwen3.5-35b-a3b | 35B-A3B | 94K | 430/500 86% | 16 |
| 5 | nemotron-3-nano-4b | 4B | 131K | 400/500 80% | 55 |
| 6 | lfm2-24b-a2b | 64x1.3B | 128K | 330/500 66% | 60 |

### Per-Test Breakdown

| Model | Tool Call | Multi-Tool | Error Recovery | Code Gen | Instruction |
|---|---|---|---|---|---|
| Gemma 4 26B | 100 | 100 | 100 | 100 | 100 |
| GLM 4.7 Flash | 100 | 100 | 30 | 100 | 100 |
| Devstral 24B | 100 | 100 | 30 | 100 | 100 |
| Qwen3.5 35B | 100 | 100 | 30 | 100 | 100 |
| Nemotron 4B | 100 | 100 | 0 | 100 | 100 |
| LFM2 24B | 100 | 0 | 30 | 100 | 100 |

### Key Findings from Log Analysis

**Error recovery is the decisive test.** Gemma 4 is the ONLY model that
autonomously retries with a corrected tool call after receiving an error.
All others mention the fix in text but don't make the actual tool call —
in OpenHands this means the agent stalls.

**Reasoning token budgets matter.** Qwen3.5's v1 code-gen failure was caused by
`max_tokens=2048` — it spent 2047 tokens on thinking, leaving 1 for output.
Set `max_tokens=4096` minimum for reasoning models (GLM, Qwen, Gemma, Nemotron).

**CPU layer spill kills speed.** Qwen3.5 spills 8/41 layers (4.3 GB) to CPU,
halving inference speed. GLM spills only 1/48 (negligible). All others fit 100% on GPU.

**Gemma 4 uses ISWA (Interleaved Sliding Window Attention)** — dual KV caches
(full + sliding window) may explain its superior error recovery via better
attention to recent context while maintaining global awareness.

**LFM2 gets WORSE with more context** — from 2 tool calls at 32K to 1 at 128K.
Multi-tool failure is architectural, not context-related.

**Devstral v1 "failure" was a test bug** — Mistral's Jinja template requires
strict role alternation; our test had invalid `user→tool→user` sequence.

### Optimal LM Studio Settings

| Model | Context | flash_attention | Notes |
|---|---|---|---|
| gemma-4-26b-a4b | 58368 | true (auto-enables) | 31/31 on GPU |
| glm-4.7-flash | 65536 | true (auto-enables) | 47/48 on GPU |
| devstral-small-2 | 61440 | true (must send) | 41/41; tight but tested |
| nemotron-3-nano-4b | 131072 | true (must send) | 43/43 on GPU |
| qwen3.5-35b-a3b | 94208 | true (must send) | 33/41; 8 layers on CPU |
| lfm2-24b-a2b | 128000 | true (must send) | model cap |

test_llm_mcp.py sends both flash_attention=true and context_length automatically.

### OpenHands Configuration

**Primary (Gemma 4 — only model with 100% autonomous error recovery):**
```
Custom Model:  openai/google/gemma-4-26b-a4b
Base URL:      http://192.168.0.241:1234/v1
API Key:       lmstudio
```
76 tok/s with flash_attention=true (was 12 without — test_llm_mcp now sends it).

**Speed alt (GLM 4.7 Flash):**
```
Custom Model:  openai/zai-org/glm-4.7-flash
Base URL:      http://192.168.0.241:1234/v1
API Key:       lmstudio
```
~61 tok/s (needs re-bench with flash_attention). Weak on autonomous error recovery.
