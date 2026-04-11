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

### LM Studio Auto Context vs Forced (24 GB RX 7900 XTX)
| Model | Auto ctx | Forced ctx | Why |
|---|---|---|---|
| gemma-4 26B | 58368 | 58368 | Matches — fits fully on GPU |
| glm-4.7 30B | 4096 | 65536 | Auto too low — weights fill VRAM |
| devstral 24B | 35914 | 61440 | Forced gives more headroom |
| qwen3.5 35B | 4096 | 94208 | Auto too low — 8/41 layers on CPU |
| nemotron 4B | 488357 | 131072 | Auto wastes KV VRAM; 131K sufficient |
| lfm2 24B | 128000 | 128000 | Model cap |

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

| Model | GPU Offload | Context Length | Notes |
|---|---|---|---|
| gemma-4-26b-a4b | 30 (max) | 58368 | Primary — best for OpenHands |
| glm-4.7-flash | 47 (max) | 65536 | Speed alt — interactive use |
| devstral-small-2 | max | 61440 | Solid coding focus |
| nemotron-3-nano-4b | max | 131072 | Quick trivial tasks |
| qwen3.5-35b-a3b | max | 94208 | Weights spill to CPU anyway |

All with: CPU Thread Pool = 7, Flash Attention = ON

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

### VRAM Budget Reference (24 GB RX 7900 XTX)

| Model | Weights on GPU | KV/token | Free for KV |
|---|---|---|---|
| GLM 4.7 Flash | 17063 MiB (47/48) | 100 KB | 7.0 GiB |
| Gemma 4 26B | 16003 MiB (31/31) | 124 KB | 8.1 GiB |
| Devstral 24B | 13302 MiB (all) | 164 KB | 10.7 GiB |
| Qwen3.5 35B | 15871 MiB (33/41) ⚠️ | 82 KB | 8.2 GiB |
| Nemotron 4B | 2429 MiB (all) | 86 KB | 21.3 GiB |
| LFM2 24B | 13745 MiB (all) | 20 KB | 10.3 GiB |
