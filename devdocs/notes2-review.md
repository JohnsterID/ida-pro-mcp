# Review of notes2.txt — Robustness Research for ida-pro-mcp

> Reviewed against the current codebase as of commit `c57017d` (2026-04-03).
> Updated 2026-04-06 with corrected tool mappings and expanded C++ analysis.

---

## Executive Summary

The notes document provides a broad strategic analysis of making ida-pro-mcp
more robust, especially for smaller local LLMs. Its central thesis — shift the
project from "Remote Control for IDA" to "Context Provider for the LLM" — is
sound and already well-reflected in the existing codebase. However, **many of
the specific tool recommendations are already implemented**, suggesting the
analysis was based on an early or surface-level reading of the project. The
sections on C++ reconstruction workflows and the iterative rename-then-
re-decompile loop contain genuinely useful architectural thinking. The testing
strategy discussion conflates MCP server testing with LLM behavior testing and
is mostly out of scope.

**Verdict:** ~40% already implemented, ~25% actionable, ~20% out of scope
(client/LLM-side), ~15% inaccurate or superseded.

---

## Section-by-Section Assessment

### 1. Robustness Analysis: "Where the Current Setup Fails" (lines 3–15)

#### A. Contextual Depth — "Beyond the Current Function"

> **Claim:** "Most MCP implementations for IDA pull the current function."

**Status: Incorrect.** The project already provides extensive multi-function
context:

| Notes2 Suggestion | Already Implemented |
|---|---|
| XREF Awareness (callers/callees) | `xrefs_to()`, `xref_query()`, `callees()`, `func_profile()` with callers/callees |
| Type Library Injection | `type_inspect()`, `read_struct()`, `type_query()`, `declare_type()` |
| Recursive call graph | `callgraph()` with `max_depth` parameter for bounded depth-first traversal |
| CFG Summary | `basic_blocks()`, `func_profile()` |

The `analyze_component()` tool in `api_composite.py` specifically provides
multi-function contextual analysis, and `survey_binary()` in `api_survey.py`
gives a holistic binary overview. This section's premise is outdated.

#### B. Verification & "Loopback" Loops

> **Claim:** Need self-correcting scripts and consistency checks.

**Status: Partially implemented.**

- "Self-Correcting Scripts" → `py_eval()` in `api_python.py` already allows
  the LLM to run arbitrary read-only IDAPython scripts for verification.
- "Consistency Checks" on renames → `rename()` in `api_modify.py` operates in
  batch mode across functions, globals, locals, and stack variables.

**Valid gap:** There is no built-in "consistency propagation" that automatically
flags related symbols when one is renamed. This would be a useful enhancement,
though it might better live as a composite tool or agentic workflow pattern
rather than core API.

#### C. Handling Binary Complexity

> **Claim:** Add summarization tools instead of sending full disassembly.

**Status: Already implemented.** `func_profile()` provides exactly this — a
structured summary with size, complexity metrics, callers, callees, strings, and
constants without sending the full disassembly. `analyze_function()` in
`api_composite.py` provides tiered analysis with decompilation, basic blocks,
and metadata.

**Valid concern:** Automatic chunking of very large functions that exceed context
windows is not built into the server. However, decompile/disasm already support
address ranges, so the LLM (or client) can page through manually. Whether
automatic chunking belongs in the MCP server vs. the client is debatable.

---

### 2. "Is Adding More Tools Against the Project?" (lines 16–19)

> **Claim:** Tool Overload is a risk; focus on Informer Tools over Action Tools.

**Status: Valid concern, already addressed.** The project's architecture
separates informer APIs (`api_analysis`, `api_core`, `api_types`,
`api_resources`, `api_survey`) from action APIs (`api_modify`, `api_memory`).
The `@unsafe` decorator gates destructive operations. The `api_discovery.py`
module handles tool selection. The project's balance is already informer-heavy.

---

### 3. Recommended "Robustness" Upgrades (lines 20–22)

| Proposed Tool | Status |
|---|---|
| `get_type_definition` | **Already exists** as `type_inspect()` and `read_struct()` |
| `get_recursive_callgraph` | **Already exists** as `callgraph()` with `max_depth`, `max_nodes`, and `max_edges` limits |
| `run_idapython_sandbox` | **Already exists** as `py_eval()` (read-only safe execution) |
| `find_immediate_usage` | **Already exists** as `find_bytes()` with immediate search and `find()` |

All four "highest ROI" recommendations are already implemented. This strongly
suggests the notes were written without examining the current API surface.

---

### 4. Guardrails for Local Models (lines 31–34)

> **Claim:** Add schema enforcement / correction layer for malformed tool calls.

**Status: Out of scope (client-side).** MCP tool-call validation is the
responsibility of the MCP client/transport layer, not the IDA-side server.
The MCP protocol itself defines schemas via JSON Schema. Malformed JSON never
reaches the tool handlers. A "correction layer" for broken tool syntax belongs
in the LLM orchestration framework (e.g., the MCP client SDK or the agent
framework), not in `ida-pro-mcp`.

> **Claim:** "I Don't Know" trigger / uncertainty budget.

**Status: Out of scope (prompt engineering).** This is a system-prompt concern
for the LLM client, not an MCP server feature. The project does include
`utils.get_analysis_prompt()` which provides system-level guidance, but
enforcing "uncertainty budgets" is fundamentally an agent-side responsibility.

---

### 5. Context Management / RAG for Binaries (lines 35–39)

> **Claim:** Add paging/chunking and local vectorized search (FAISS/SQLite).

**Paging:** Partially valid. The existing `paginate()` helper in `utils.py`
and range parameters on `decompile()`/`disasm()` provide manual paging. Auto-
chunking could be a nice ergonomic improvement but adds complexity.

**Local RAG / FAISS:** Out of scope and architecturally wrong. Embedding binary
analysis artifacts into a vector store creates a parallel data layer that
duplicates IDA's own database. IDA already *is* the structured search engine —
`func_query()`, `find()`, `xref_query()`, `type_query()`, and `survey_binary()`
provide the same function as RAG retrieval but with precise, deterministic
results instead of approximate vector similarity. Adding FAISS would be
over-engineering.

---

### 6. Critical Missing Tools for Weak LLMs (lines 40–42)

| Proposed Tool | Assessment |
|---|---|
| `hex_calc` | **Unnecessary.** LLMs can use `py_eval("hex(0x401000 + 0x20)")` for arithmetic. Adding a dedicated tool increases tool surface for minimal gain. |
| `get_call_hierarchy` | **Already exists** as `func_profile()` which returns callers and callees in a structured hierarchy. |
| `verify_address` | **Partially valid.** Tools already return errors for invalid addresses, but a lightweight `is_valid_address(ea)` check could reduce round-trip cost for speculative queries. Low priority. |

---

### 7. Step-by-Step Triggers / Chain of Thought (lines 43–46)

> **Claim:** Require `log_reasoning` before `set_function_name`; "Safe Mode" flag.

**Status: Out of scope (agent-side).** Enforcing reasoning chains is the
responsibility of the agent/client framework, not the MCP tool server. The
`@unsafe` decorator already provides a safety gate for destructive operations.
A server-enforced "you must call tool A before tool B" dependency would violate
MCP's stateless tool model and couple the server to specific agent
architectures.

---

### 8. Testing Strategies — Ollama Cloud / Mock / Tiny (lines 53–94)

**Status: Mostly out of scope.** This section discusses how to test LLM
behavior against the MCP server, not how to test the MCP server itself. The
project has its own test framework (`framework.py`) that tests tool
correctness using idalib headlessly. The notes conflate two different testing
concerns:

1. **MCP tool correctness** (does `xrefs_to()` return correct data?) — already
   covered by the existing test suite.
2. **LLM integration quality** (does a model use the tools correctly?) — valid
   concern but belongs in a separate integration-testing project, not in the
   ida-pro-mcp core.

The Ollama Cloud setup instructions are about configuring an LLM client, not
the MCP server. They reference "IDA Pro MCP configuration" but the server
doesn't have an LLM configuration — it exposes tools, it doesn't consume them.

---

### 9. C++ Auto-Reconstruction (lines 95–130)

This is the strongest section conceptually, but testing against real binaries
with IDA Pro 9.3 + Hex-Rays reveals that **IDA's decompiler already does most
of the heavy lifting**. The gap between what's proposed and what's needed is
smaller than the notes suggest.

#### What the decompiler already gives you

Tested against the `rebin` project's testapp (GCC debug/release/hardened,
Clang, MSVC v90/v143, MinGW — C++ class hierarchy with single/multiple/virtual
inheritance, pure virtuals, templates).

**Debug builds** — the decompiler shows everything directly:
```c
// Constructor: IDA already identifies vtable writes with _vptr_ClassName
void FormalGreeter::FormalGreeter(FormalGreeter *this, const std::string *name)
{
  AbstractGreeter::AbstractGreeter(this, name);
  Logger::Logger(&this->Logger);
  Formatter::Formatter(&this->Formatter);
  this->_vptr_Greeter = (int (**)(...))off_108B8;
  this->_vptr_Logger = (int (**)(...))off_10900;
  this->_vptr_Formatter = (int (**)(...))off_10928;
}

// Virtual call: IDA shows the vtable dispatch with offset
(*((void (__fastcall **)(Greeter *))greeter->_vptr_Greeter + 2))(greeter);
```

An LLM reading this pseudocode can already see the class hierarchy, the
multiple inheritance (three vtable pointers), and the base constructor chain.
It does not need dedicated RTTI tools — the decompiler has done the work.

**Release builds** (optimized, not stripped) — RTTI symbols and demangled names
survive. IDA still shows named constructors and vtable writes. The decompiled
output is slightly less clear due to inlining but the patterns remain visible.

**Stripped / hardened builds** — vtable writes become anonymous:
```c
*(_QWORD *)(v4 - 8) = off_9AA8;
```
The `off_XXXX` replaces the named vtable, but the pattern (store to
`[first_arg + 0]`) is still visible. RTTI symbols may or may not survive
depending on compiler flags (`-fno-rtti` removes them entirely).

#### What dedicated tools would actually add

The decompiler is a **detail tool** — it shows one function at a time. The gap
is in **bulk discovery**: given a binary with 500 functions and 20 classes,
the LLM would need to decompile all functions and parse the pseudocode to
reconstruct the class map. Dedicated tools could provide the complete picture
in a single call.

However, this can also be achieved with existing tools:

| Discovery Task | Existing Tool Approach |
|---|---|
| Find all vtable symbols | `entity_query()` with name filter `_ZTV*` or `??_7*` |
| Find all RTTI symbols | `entity_query()` with name filter `_ZTI*` or `??_R0*` |
| Read vtable entries | `get_int()` in a loop at the vtable address |
| Find constructor patterns | `insn_query()` for `mov` + `lea` with vtable operands |
| Parse RTTI structures | `get_int()` / `get_bytes()` at typeinfo addresses |
| Read RTTI name strings | `get_string()` at type name addresses |
| Trace vtable references | `xrefs_to()` on vtable addresses → finds constructors |
| Build call hierarchy | `callgraph()` from constructors → finds base chains |

An LLM orchestrating these existing tools can already perform the full C++
recovery workflow. The question is whether the multi-step orchestration is
reliable enough or whether a single composite tool would be more practical.

#### Recommended approach: composite tool, not raw primitives

Rather than four separate tools (vtable_scan, rtti_parse, find_vtables,
constructor_detect), a single **`cpp_classes()`** composite tool in
`api_composite.py` would be more practical:

1. Scan named symbols for RTTI/vtable patterns (fast — just a name query).
2. For each vtable address, read the function pointer array and resolve names.
3. For Itanium ABI: follow vtable[-1] to typeinfo, read class name and base
   class info.
4. For MSVC ABI: follow vtable[-1] to COL, parse hierarchy descriptor.
5. Return a structured class hierarchy with vtable → class → bases → methods.

This is similar to how `survey_binary()` provides a one-call binary overview
and `analyze_component()` provides a one-call multi-function analysis. The
composite approach matches the project's existing patterns better than adding
four separate low-level tools.

**ABI reference for implementation:**

*GCC / Itanium ABI* (ELF, Mach-O, MinGW):
- `_ZTV*` → vtable; RTTI pointer at vtable[-1], offset-to-top at vtable[-2]
- `_ZTI*` → typeinfo: `__class_type_info` (no bases),
  `__si_class_type_info` (single base), `__vmi_class_type_info` (multiple/virtual bases)
- `_ZTS*` → mangled type name string
- `__cxa_pure_virtual` → pure virtual slot marker

*MSVC ABI* (PE):
- `??_7*` → vtable; COL pointer at vtable[-1]
- `_RTTICompleteObjectLocator` → links to type descriptor + hierarchy
- `_RTTIClassHierarchyDescriptor` → `numBaseClasses` + base class array
- `_RTTIBaseClassDescriptor` → per-base with PMD displacement info
- `_purecall` → pure virtual slot marker
- Symbol patterns: `??_R0?AV*` (type desc), `??_R4*` (COL), `??_R3*` (hierarchy)

**Already addressed:**
- Member offset tracking → `read_struct()` + struct inspection tools exist.
- "run_plugin(ClassInformer)" → `py_eval()` can already invoke any IDA plugin.
- `set_type` for applying reconstructed classes → `set_type()` and
  `declare_type()` exist.

**Test fixtures available:** The `rebin` project's testapp binaries (GCC,
Clang, MSVC v90/v143, MinGW — all with original C++ source) provide
ground-truth class hierarchies for validation across compilers and
optimization levels.

#### Existing tools that support the C++ workflow

| Workflow Step | Existing Tool | Role |
|---|---|---|
| Binary triage | `survey_binary()` | Identify binary type, segments, imports |
| Symbol search | `entity_query()`, `find()` | Find RTTI/vtable symbols by name pattern |
| Memory reading | `get_int()`, `get_bytes()` | Read RTTI structures and vtable entries |
| Xref tracing | `xrefs_to()`, `trace_data_flow()` | Find vtable references in constructors |
| Decompilation | `decompile()` | See vtable writes, virtual calls, member access in pseudocode |
| Type creation | `declare_type()` | Define C++ class structs in IDA's type library |
| Type application | `set_type()`, `type_apply_batch()` | Apply class types to functions/variables |
| Renaming | `rename()` | Batch rename methods, vtable globals, constructors |
| Verification | `decompile()`, `diff_before_after()` | Re-decompile after applying types to verify |
| Call graph | `callgraph()` | Trace constructor call chains for inheritance |
| Instruction search | `insn_query()` | Find vtable-write patterns in functions |

---

### 10. Decompiled Code as "Draft" (lines 131–151)

> **Claim:** Use decompilation for logic + MCP tools for structure, then
> iteratively refine via rename-and-re-decompile loops.

**Status: Valid and already supported.** The `diff_before_after()` tool in
`api_composite.py` is specifically designed for this workflow — it snapshots
decompilation, applies changes, and shows the diff. The `rename()` +
`decompile()` cycle works exactly as described. This section accurately
describes a workflow the project already enables.

---

## Summary of Actionable Items

### Worth Implementing (from notes2.txt + audit)

| Item | Priority | Effort | Notes |
|---|---|---|---|
| `cpp_classes()` composite | Medium | High | Single-call C++ class hierarchy extraction: scan RTTI + vtables, return structured class → bases → methods map. Handles both Itanium and MSVC ABIs. Biggest value for stripped/release builds where the decompiler shows patterns but not names. For debug builds the decompiler already provides most of this information. |
| Consistency propagation on rename | Low | Medium | Flag related symbols across functions |
| Lightweight `is_valid_address` | Low | Low | Reduces round-trips for speculative queries |

Test fixtures available: `rebin` project testapp binaries (GCC, Clang,
MSVC v90/v143, MinGW) with original C++ source code providing ground-truth
class hierarchies for validation across compilers and optimization levels.

### Not Worth Implementing

| Item | Reason |
|---|---|
| hex_calc tool | `py_eval()` covers this trivially |
| Local RAG / FAISS | IDA's own DB + existing query tools are superior |
| Tool-call correction layer | Client/transport responsibility, not server |
| Mandatory reasoning chains | Agent-side concern, violates MCP statelessness |
| Mock LLM testing infra | Out of scope for the MCP server project |
| Automatic context chunking | Better handled by client; range params already exist |

---

## Conclusion

The notes document contains useful strategic thinking but significantly
underestimates the current project's capabilities. Most "missing" tools already
exist. The strongest contributions are the C++ reconstruction workflow
design and the "Context Provider" philosophy — which the project already
embodies.

Testing against real C++ binaries with IDA Pro 9.3 + Hex-Rays shows that
the decompiler already handles much of C++ recovery automatically. In debug
builds, constructors show vtable writes with `_vptr_ClassName`, virtual calls
show vtable dispatch with offsets, and base constructor chains are visible in
pseudocode. Even in stripped builds the patterns remain visible, just with
anonymous `off_XXXX` labels instead of named vtables.

The remaining gap is **bulk discovery** — extracting the complete class
hierarchy from a binary in one call rather than decompiling functions one at
a time. A single `cpp_classes()` composite tool (similar to `survey_binary()`
for general triage) would fill this gap by scanning RTTI symbols and vtable
structures and returning a structured hierarchy. The existing tools
(`entity_query`, `get_int`, `xrefs_to`, `decompile`, `declare_type`,
`rename`) already provide all the primitives an LLM needs for the follow-up
refinement work.

Future work on C++ recovery should be scoped as one composite tool, not a
suite of low-level primitives. The generic "guardrails for dumb LLMs"
suggestions belong in the client/agent layer, not the MCP server.
