# Windows-MCP: Feature & Performance Plan

## Current State (v0.6.2)

Windows-MCP is a lightweight MCP server enabling AI agents to interact with Windows OS via 12 tools (Snapshot, Click, Type, Scroll, Move, Shortcut, Wait, App, Shell, Scrape, MultiSelect, MultiEdit). It uses UIAutomation COM bindings, ThreadPool-based parallel window processing, and property caching to achieve 0.2–0.9s per action.

---

## Part 1: New Features

### F1. Element Wait / Conditional Polling

**Problem:** Agents currently must call `Snapshot` repeatedly to check if a UI element has appeared after an action (e.g., waiting for a dialog, a page load, or a spinner to disappear). This wastes round-trips and token budget.

**Proposal:** Add a `WaitForElement` tool that blocks until a matching element appears (or disappears) in the accessibility tree, with a configurable timeout.

```
WaitForElement(
    name: str | None,               # fuzzy match on element name
    control_type: str | None,       # e.g. "ButtonControl"
    condition: "appear" | "disappear",
    timeout: float = 10.0,          # seconds
    poll_interval: float = 0.3      # seconds between checks
) -> bool
```

**Key files:** `__main__.py` (tool definition), `tree/service.py` (polling loop reusing `get_state`).

---

### F2. Targeted Element Snapshot (Partial Tree)

**Problem:** Every `Snapshot` traverses the full accessibility tree of all visible windows. When an agent only needs to inspect a single window or a subtree (e.g., a dialog), this is wasteful.

**Proposal:** Add an optional `window_name` filter to `Snapshot` that restricts tree traversal to the matching window, and an `element_id` parameter to return only the subtree rooted at a previously-seen element.

**Impact:** Reduces tree traversal cost by 50–80% for focused interactions.

**Key files:** `__main__.py`, `desktop/service.py` (`get_state`), `tree/service.py` (`get_state`, `get_windowwise_nodes`).

---

### F3. Clipboard Read/Write Tool

**Problem:** Agents have no direct way to read or write the system clipboard. They must use `Shell` with PowerShell workarounds (`Get-Clipboard`, `Set-Clipboard`), which is slow and fragile for binary/image content.

**Proposal:** Add a `Clipboard` tool:

```
Clipboard(
    mode: "read" | "write",
    content: str | None = None,      # for write mode
    format: "text" | "image" = "text"
) -> str | Image
```

**Implementation:** Use `win32clipboard` (already available via pywin32) for text, and PIL for image clipboard round-trips.

**Key files:** `__main__.py`, new function in `desktop/service.py`.

---

### F4. File Drag-and-Drop

**Problem:** The current `Move` tool supports drag-and-drop between screen coordinates, but agents cannot drag files from Explorer to an application (e.g., drag a .csv into Excel). This requires synthesizing `IDropTarget` / `CF_HDROP` data.

**Proposal:** Add a `DragFile` tool:

```
DragFile(
    file_path: str,
    target_x: int,
    target_y: int
)
```

**Implementation:** Use `pyautogui` for mouse movement combined with `win32clipboard` CF_HDROP format and `ctypes` `SendInput` to simulate the full drag-drop protocol.

**Key files:** `__main__.py`, `desktop/service.py`.

---

### F5. OCR Fallback for Non-Accessible Elements

**Problem:** Some applications (games, custom-rendered UIs, PDF viewers) expose no accessibility tree. The agent sees nothing in the snapshot.

**Proposal:** Add an optional `use_ocr: bool` parameter to `Snapshot`. When enabled, and when fewer than N interactive elements are found, run lightweight OCR (Windows.Media.Ocr via WinRT or Tesseract) on the screenshot to extract text bounding boxes as pseudo-elements.

**Key files:** `__main__.py`, `desktop/service.py`, new `ocr/` module.

---

### F6. Action Macros / Batch Execution

**Problem:** Multi-step sequences (e.g., "click X, type Y, press Enter") require one MCP round-trip per action. Network latency between the LLM and the MCP server compounds.

**Proposal:** Add a `Batch` tool that accepts an ordered list of actions and executes them sequentially server-side:

```
Batch(
    actions: list[{tool: str, params: dict}]
) -> list[{status: str, result: any}]
```

**Impact:** Eliminates N-1 round-trips for N-step sequences. Particularly valuable for form-filling, menu navigation, and keyboard shortcut chains.

**Key files:** `__main__.py` (dispatch loop referencing existing tool functions).

---

### F7. Element Highlight / Visual Feedback

**Problem:** When debugging agent behavior, there is no way to visually confirm which element the agent is targeting before it clicks.

**Proposal:** Add a `Highlight` tool that draws a temporary colored rectangle overlay on a target element for a configurable duration using `win32gui` `CreateWindowEx` with `WS_EX_LAYERED`.

```
Highlight(
    x: int, y: int,
    width: int, height: int,
    duration: float = 2.0,
    color: str = "red"
)
```

**Key files:** `__main__.py`, new function in `desktop/service.py`.

---

### F8. Notification / Toast Monitoring

**Problem:** Agents cannot detect Windows toast notifications (e.g., "Download complete", "New email"). These appear briefly and are not part of the standard window list.

**Proposal:** Use the UIAutomation event system (already partially implemented in `watchdog/`) to subscribe to notification events and expose a `GetNotifications` tool returning recent toasts.

**Key files:** `watchdog/service.py`, `watchdog/event_handlers.py`, `__main__.py`.

---

## Part 2: Performance Optimizations

### P1. Eliminate the 1-Second Startup Sleep

**Problem:** `__main__.py:47` has `await asyncio.sleep(1)` in the lifespan, adding a flat 1-second delay to every server start.

**Fix:** Remove or reduce to 0.1s. The sleep was added "to simulate startup latency" per the comment — there is no real dependency requiring it. If stability is a concern, replace with a readiness check on `watchdog.start()`.

**Impact:** 1 second saved on every cold start.

**File:** `src/windows_mcp/__main__.py:47`

---

### P2. Reuse ThreadPoolExecutor Across Snapshots

**Problem:** `get_windowwise_nodes` creates a new `ThreadPoolExecutor()` on every call. Thread pool creation involves spawning OS threads, which has measurable overhead (5–15ms per snapshot).

**Fix:** Create the executor once in `Tree.__init__` and reuse it. Use a fixed `max_workers` (e.g., `min(8, len(windows_handles))`) instead of the default which scales to CPU count × 5.

```python
# In Tree.__init__:
self._executor = ThreadPoolExecutor(max_workers=8)
```

**Impact:** Eliminates thread pool creation overhead on every snapshot.

**File:** `src/windows_mcp/tree/service.py:99`

---

### P3. Reduce Redundant LegacyIAccessiblePattern Calls

**Problem:** In `tree_traversal`, `GetLegacyIAccessiblePattern()` is called up to 3 times for the same element — once for role check (line ~316), once for default action check (line ~341), and once for value extraction (line ~350). Each call is a cross-process COM round-trip.

**Fix:** Call `GetLegacyIAccessiblePattern()` once per element and reuse the result:

```python
legacy_pattern = None
def get_legacy():
    nonlocal legacy_pattern
    if legacy_pattern is None:
        legacy_pattern = node.GetLegacyIAccessiblePattern()
    return legacy_pattern
```

**Impact:** Up to 2 fewer COM calls per interactive element. For a tree with 200 interactive elements, this saves ~400 COM calls per snapshot.

**File:** `src/windows_mcp/tree/service.py:316–350`

---

### P4. Skip Offscreen Subtrees Early

**Problem:** The current traversal checks `is_offscreen` per element but still recurses into children of offscreen containers. An offscreen container's children are almost always also offscreen.

**Fix:** If a non-DOM element is offscreen and not an `EditControl`, skip its entire subtree:

```python
if is_offscreen and control_type_name != 'EditControl' and not is_dom:
    return  # prune subtree
```

**Impact:** Can eliminate 30–60% of tree traversal for applications with tabs, collapsed panels, or off-screen content.

**File:** `src/windows_mcp/tree/service.py:246`

---

### P5. Cache `CacheRequest` Objects Instead of Recreating

**Problem:** `CacheRequestFactory.create_tree_traversal_cache()` is called twice per `get_nodes` invocation (once for element, once for children), and `get_nodes` runs once per window. Each call creates a new COM `CacheRequest` object and adds ~12 properties.

**Fix:** Create two module-level singleton cache request templates and clone them per thread:

```python
_ELEMENT_CACHE_TEMPLATE = None
_CHILDREN_CACHE_TEMPLATE = None

def get_element_cache():
    global _ELEMENT_CACHE_TEMPLATE
    if _ELEMENT_CACHE_TEMPLATE is None:
        _ELEMENT_CACHE_TEMPLATE = CacheRequestFactory.create_tree_traversal_cache()
        _ELEMENT_CACHE_TEMPLATE.TreeScope = TreeScope.TreeScope_Element
    return _ELEMENT_CACHE_TEMPLATE
```

Note: COM objects are apartment-threaded, so each thread needs its own instance. But within a single `get_nodes` call, the same instance is already reused — the win here is avoiding re-creation across calls within the same thread's COM apartment.

**Impact:** Minor (saves ~2ms per window), but removes unnecessary object churn.

**File:** `src/windows_mcp/tree/cache_utils.py`, `src/windows_mcp/tree/service.py:485–489`

---

### P6. Parallelize Screenshot Capture with Tree Traversal

**Problem:** In `Desktop.get_state`, tree traversal and screenshot capture happen sequentially. The screenshot (`ImageGrab.grab`) takes 20–50ms and is I/O-bound, while tree traversal is CPU/COM-bound.

**Fix:** When `use_vision=True`, start the screenshot grab in a separate thread while tree traversal runs on the main thread:

```python
screenshot_future = None
if use_vision:
    screenshot_future = executor.submit(self.get_screenshot)

tree_state = self.tree.get_state(...)  # runs in parallel

if screenshot_future:
    screenshot = screenshot_future.result()
```

**Impact:** Saves 20–50ms per snapshot when vision is enabled, which is the common case.

**File:** `src/windows_mcp/desktop/service.py:78–96`

---

### P7. Reduce pyautogui.PAUSE from 1.0s to 0.1s

**Problem:** `pyautogui.PAUSE = 1.0` (set in both `__main__.py:20` and `desktop/service.py:41`) adds a 1-second sleep after every pyautogui call (click, type, move, scroll). This is the single largest source of latency for action tools.

**Fix:** Reduce to `0.1` or `0.05`. The 1.0s default is pyautogui's safety mechanism to allow human intervention, but for an MCP server running automated actions, it is unnecessarily conservative. The `FAILSAFE=False` is already set, so the pause serves no safety purpose.

```python
pg.PAUSE = 0.1  # was 1.0
```

**Impact:** Saves ~0.9 seconds per action tool call. For a 10-action sequence, this is 9 seconds saved.

**File:** `src/windows_mcp/__main__.py:20`, `src/windows_mcp/desktop/service.py:41`

---

### P8. Lazy-Load Heavy Imports

**Problem:** `desktop/service.py` imports `PIL`, `requests`, `markdownify`, `fuzzywuzzy`, `psutil`, `csv`, `base64`, and `subprocess` at module level. Many of these (e.g., `requests`, `markdownify`, PIL font loading) are only used by specific tools (`Scrape`, vision annotation) but add to cold start time.

**Fix:** Move heavy imports to the functions that use them:

```python
def scrape(self, url):
    import requests
    from markdownify import markdownify
    ...
```

**Impact:** Reduces cold start time by 100–300ms depending on system.

**File:** `src/windows_mcp/desktop/service.py:1–27`

---

### P9. Bounded Recursion Depth in Tree Traversal

**Problem:** `tree_traversal` recurses with no depth limit. Deeply nested UI trees (e.g., complex web pages in browser DOM mode) can cause stack overflow or extreme traversal times.

**Fix:** Add a `max_depth` parameter (default: 30) and stop recursing beyond it:

```python
def tree_traversal(self, node, ..., depth: int = 0, max_depth: int = 30):
    if depth >= max_depth:
        return
    ...
    self.tree_traversal(child, ..., depth=depth+1, max_depth=max_depth)
```

**Impact:** Prevents pathological cases. For normal UIs (depth < 15), no behavior change.

**File:** `src/windows_mcp/tree/service.py:235`

---

### P10. Connection Pooling for Analytics

**Problem:** `PostHogAnalytics` may create new HTTP connections for each event. Under frequent tool calls, this creates connection overhead.

**Fix:** Use `requests.Session()` with connection pooling for the PostHog client, or batch events and flush periodically (every 5s or 10 events) instead of per-call.

**Impact:** Reduces network overhead during intensive automation sessions. Analytics should never slow down the critical path.

**File:** `src/windows_mcp/analytics.py`

---

## Priority Matrix

| ID  | Category    | Impact | Effort | Priority |
|-----|-------------|--------|--------|----------|
| P7  | Performance | High   | Low    | **P0**   |
| P1  | Performance | Medium | Low    | **P0**   |
| P3  | Performance | High   | Low    | **P1**   |
| P4  | Performance | High   | Medium | **P1**   |
| P6  | Performance | Medium | Low    | **P1**   |
| F1  | Feature     | High   | Medium | **P1**   |
| F2  | Feature     | High   | Medium | **P1**   |
| F3  | Feature     | Medium | Low    | **P1**   |
| F6  | Feature     | High   | Medium | **P1**   |
| P2  | Performance | Low    | Low    | **P2**   |
| P5  | Performance | Low    | Low    | **P2**   |
| P8  | Performance | Medium | Low    | **P2**   |
| P9  | Performance | Medium | Low    | **P2**   |
| P10 | Performance | Low    | Low    | **P2**   |
| F4  | Feature     | Medium | High   | **P2**   |
| F5  | Feature     | High   | High   | **P2**   |
| F7  | Feature     | Low    | Medium | **P3**   |
| F8  | Feature     | Medium | Medium | **P3**   |

### Recommended execution order

1. **Quick wins (P0):** P7 (reduce PAUSE), P1 (remove startup sleep) — immediate measurable gains with single-line changes.
2. **Core optimizations (P1):** P3, P4, P6 — reduce COM overhead and enable parallelism in the hot path.
3. **High-value features (P1):** F1 (WaitForElement), F2 (partial snapshot), F6 (batch execution) — reduce agent round-trips, which is the dominant latency source.
4. **Polish (P2/P3):** Remaining items based on user demand.
