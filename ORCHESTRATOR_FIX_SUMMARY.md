# Orchestrator Text Mode Bug Fixes

**Date:** 2026-02-21
**Status:** ✅ Complete

## Summary

Fixed two critical bugs in the orchestrator that caused text mode to lose conversation context when resuming sessions or switching from voice mode.

---

## Issues Fixed

### 🔴 **Issue #1: History Not Passed to Text Mode System Prompt**

**Location:** `orchestrator/agent.py` line 76

**Problem:**
The orchestrator agent was building the system prompt WITHOUT conversation history in text mode:
```python
system = build_system_prompt(self._config, self._context)
```

**Impact:**
- Agent had NO memory of previous conversation when switching from voice to text mode
- Resuming text sessions lost all context
- Led to nonsensical responses like "Hello! How can I help?" when user asked "can you remember what we were talking about?"

**Fix:**
```python
system = build_system_prompt(
    self._config,
    self._context,
    history=self._history,  # ✅ Now passes conversation history
)
```

### 🔴 **Issue #2: JSONL History Loading Was Not Robust**

**Location:** `orchestrator/session.py` `_load_history()` method (now refactored)

**Problem:**
- The history loader was monolithic and embedded in `OrchestratorSession`
- Only handled basic user/assistant messages
- Tool calls and voice mode entries were not properly reconstructed
- No separation of concerns between persistence and session management

**Impact:**
- Mixed voice/text sessions had incomplete history restoration
- Tool call context was lost between sessions
- Code was difficult to test and maintain

**Fix:**
- Created dedicated `orchestrator/persistence.py` module
- Implemented `HistoryLoader` class with proper state machine for reconstructing conversation history
- Implemented `HistoryWriter` class for clean JSONL writing
- Comprehensive handling of:
  - Text mode messages
  - Voice transcriptions and responses
  - Tool calls and results
  - Multi-message tool execution flows
  - Invalid/corrupted JSONL lines
- Added 7 comprehensive unit tests (all passing ✅)

---

## Code Structure Improvements

### New Files

**`orchestrator/persistence.py`** (~180 lines)
- `HistoryLoader`: Reconstructs conversation history from JSONL
- `HistoryWriter`: Appends events to JSONL with error handling
- Clean separation of persistence logic from session management
- Extensive documentation and type hints

**`tests/test_orchestrator_persistence.py`** (~280 lines)
- 7 comprehensive test cases covering:
  - Empty files
  - Simple text conversations
  - Tool calls and results
  - Voice mode transcriptions
  - Invalid JSON handling
  - Multiple sequential tool calls
  - Writing and reading roundtrips

### Modified Files

**`orchestrator/agent.py`**
- ✅ Now passes `history=self._history` to `build_system_prompt()`
- Ensures text mode has conversation context in system prompt

**`orchestrator/session.py`**
- ✅ Refactored to use `HistoryLoader` and `HistoryWriter`
- Removed 150+ lines of monolithic `_load_history()` method
- Removed `_append_jsonl()` method (now handled by `HistoryWriter`)
- Added `self._writer: HistoryWriter` instance
- Cleaner separation of concerns: session lifecycle vs persistence

**`orchestrator/prompt.py`**
- ✅ Updated `_history_section()` docstring to clarify it's used for both text and voice modes
- Made it clear that history in system prompt is important for context continuity

**`tests/test_orchestrator.py`**
- ✅ Updated `test_resume_loads_history` to use new `_writer` API
- ✅ Updated assertions to match content block format

---

## Technical Details

### History Reconstruction Algorithm

The new `HistoryLoader` uses a state machine to reconstruct conversation history:

1. **Read JSONL**: Parse all valid JSON lines, skip invalid ones
2. **Reconstruct Messages**: Build proper Anthropic API message format
   - User messages: `{"role": "user", "content": "text"}`
   - Assistant messages: `{"role": "assistant", "content": [{"type": "text", ...}, {"type": "tool_use", ...}]}`
   - Tool results: `{"role": "user", "content": [{"type": "tool_result", ...}]}`
3. **State Management**:
   - `pending_assistant_blocks`: Accumulates assistant content (text + tool calls)
   - `pending_tool_results`: Accumulates tool results into user message
   - Smart flushing: Knows when to close an assistant message vs keep accumulating

### Message Format

**JSONL Storage Format:**
```jsonl
{"type": "user", "message": {"role": "user", "content": "..."}, "timestamp": "..."}
{"type": "assistant", "message": {"role": "assistant", "content": "..."}, "timestamp": "..."}
{"type": "tool_use", "tool_call_id": "...", "tool_name": "...", "tool_input": {...}}
{"type": "tool_result", "tool_call_id": "...", "output": "..."}
{"type": "voice_interrupted", "timestamp": "..."}
{"type": "orchestrator_meta", "session_id": "...", ...}
```

**Reconstructed History Format (Anthropic API):**
```python
[
    {"role": "user", "content": "text"},
    {"role": "assistant", "content": [{"type": "text", "text": "..."}, {"type": "tool_use", ...}]},
    {"role": "user", "content": [{"type": "tool_result", ...}]},
]
```

---

## Testing

### Test Coverage

**New Tests:** 7 tests in `test_orchestrator_persistence.py`
- ✅ Empty file handling
- ✅ Simple conversations
- ✅ Tool calls with results
- ✅ Voice mode transcriptions
- ✅ Writer append operations
- ✅ Invalid JSON graceful handling
- ✅ Multiple sequential tool calls

**Existing Tests:** All 46 orchestrator tests passing
- ✅ Updated to use new `_writer` API
- ✅ Updated assertions for content block format
- ✅ No regressions

### Run Tests

```bash
# All orchestrator tests
python -m pytest tests/ -v -k "orchestrator or persistence"

# Just persistence tests
python -m pytest tests/test_orchestrator_persistence.py -v

# All tests
python -m pytest tests/ -v
```

---

## Migration Notes

### Breaking Changes

**For code directly using `OrchestratorSession`:**
- ❌ `session._append_jsonl(data)` removed
- ✅ Use `session._writer.append(data)` instead

**For code reading history:**
- ❌ `session._load_history()` removed (was private anyway)
- ✅ Use `HistoryLoader(jsonl_path).load()` for direct access
- ℹ️ `OrchestratorSession` automatically uses `HistoryLoader` internally

### No User-Facing Changes

All changes are internal to the orchestrator implementation. The API surface (`session.start()`, `session.send()`, `session.stop()`) remains unchanged.

---

## Benefits

### 1. **Correctness** ✅
- Text mode now properly retains conversation context
- Voice → text mode transitions preserve full conversation history
- Resuming sessions works correctly with complete context

### 2. **Code Quality** ✅
- Modular design: persistence logic separated from session management
- Single Responsibility Principle: each class has one clear purpose
- Testable: persistence logic can be tested independently
- Maintainable: smaller, focused files with clear boundaries

### 3. **Robustness** ✅
- Handles invalid JSONL lines gracefully
- Supports mixed text/voice conversations
- Properly reconstructs complex tool call sequences
- Comprehensive error handling and logging

### 4. **Documentation** ✅
- Extensive docstrings explaining format transformations
- Clear comments on state machine logic
- Test cases serve as usage examples

---

## Future Improvements

### Potential Enhancements (Not Blocking)

1. **Performance**: For very long sessions (1000+ messages), consider:
   - Lazy loading: only load last N messages
   - Streaming parser: don't load entire file into memory
   - Index file: jump to specific message ranges

2. **Compression**: For large JSONL files:
   - Gzip compression for archived sessions
   - Compact format: abbreviate field names

3. **Validation**: Add schema validation:
   - Ensure JSONL entries match expected format
   - Detect corrupted/incomplete entries

4. **Migration**: Add migration utilities:
   - Convert old format JSONL to new format
   - Repair corrupted session files

---

## Verification

### How to Verify the Fix

1. **Start an orchestrator session in voice mode**
2. **Have a conversation** (ask questions, use tools, etc.)
3. **Switch to text mode** (close voice, send text message)
4. **Verify:** The agent should remember the entire conversation from voice mode
5. **Resume the session** (stop and restart with same session_id)
6. **Verify:** Full history is restored including both voice and text messages

### Before the Fix

```
User (voice): "Hey, can you search for information about X?"
Assistant (voice): [performs search, discusses results]
User (text): "can you remember what we were talking about?"
Assistant (text): "Hello! How can I assist you today?" ❌
```

### After the Fix

```
User (voice): "Hey, can you search for information about X?"
Assistant (voice): [performs search, discusses results]
User (text): "can you remember what we were talking about?"
Assistant (text): "Yes, we were just discussing X. I found [results]..." ✅
```

---

## Conclusion

These fixes address the root cause of the orchestrator's memory issues in text mode. The refactoring improves code organization, testability, and maintainability while ensuring conversation continuity across mode switches and session resumes.

**All tests passing ✅**
**No regressions ✅**
**Production ready ✅**
