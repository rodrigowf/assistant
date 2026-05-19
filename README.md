<p align="center">
  <img src="assets/logo.svg" alt="Assistant logo" width="160" />
</p>

# Personal Assistant

**Talk to your coding agents. Use them for anything.**

Whether on your phone or in the browser, collaborate naturally with integrated history, memory, and multi-agent workflows—accessible through standard text chat or real-time voice conversations.

---

## What Makes This Special

### 🗣️ **Native Human-Agent Collaboration**
Directly converse with your development environment and automate *any* task. This system is architected for fluid interaction:
- **Separated Layers**: Keeps the orchestrator conversation layer separate from specialized agents, preserving clean architecture while feeling like you're talking directly to the code editor or personal assistant.
- **Real-Time Voice**: The orchestrator session can be a voice conversation via WebRTC with sub-100ms latency, server-side VAD (no push-to-talk), and full barge-in support.
- **Context-Aware**: Voice, text, and multi-agent turns all share the same memory and semantically indexed history.

### 🤖 **Strategic Multi-Agent Orchestration**
A central orchestrator agent (text or voice) coordinates multiple sessions simultaneously for comprehensive control:
- **Action for Anything**: Use the orchestrator to have agents handle coding, manage your calendar, draft emails, or control devices—whatever you need.
- **Dynamic Interface**: Agent sessions appear as tabs, auto-spawning or closing as directed by the orchestrator.
- **Cross-Session Recall**: Search memory and all past conversations for instant context across any active agent.

### 🔌 **Choice of Backends, Your Provider**
This system is truly model-agnostic. You pick the engines and harnesses you need at installation:
- **Agent Harnesses**: The core coding agents—structural skeletons that come in flavors like Claude Code, Qwen Code, and Gemini CLI. Use them interchangeably.
- **Orchestration Providers**: The thinking brain of the orchestrator can call Anthropic (Claude), OpenAI, or anything compatible with standard OpenAI or Anthropic SDKs (Qwen, Gemini, etc.).

### 🔍 **Radically Transparent & Hackable**
The codebase is small, readable, and explicitly designed to evolve with you:
- **Inspectable Source**: Python backend and React frontend—small enough to read in an afternoon. No black boxes.
- **Self-Modifying**: The assistant can fix its own bugs, add tools, or create skills, hot-reloading changes automatically.
- **Personal/Private Separation**: Your private data (memory, history, credentials) lives separately from the public framework code.

---

## Ready to Deploy?

[Full Installation Details](INSTALL.md)

```bash
git clone https://github.com/rodrigowf/assistant.git
cd assistant
./install.sh                    # Interactive setup
```

---

*This is a personal project driven by collaboration and authenticity. See `rodrigo_personal_context.md` for the user vision.*
