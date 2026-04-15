import { useState, useMemo } from "react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import * as Diff from "diff";
import {
  MdCode,
  MdEdit,
  MdDescription,
  MdTerminal,
  MdSearch,
  MdLanguage,
  MdSmartToy,
  MdChecklist,
  MdHelpOutline,
  MdAutoAwesome,
  MdEditNote,
  MdBook,
  MdNavigation,
  MdTouchApp,
  MdKeyboard,
  MdCameraAlt,
  MdContentCopy,
  MdBolt,
  MdList,
  MdNetworkCheck,
  MdSpeed,
  MdBuild,
  MdError,
  MdCheckCircle,
  MdMoreHoriz,
  MdOpenInNew,
  MdClose,
  MdVisibility,
  MdSend,
  MdStop,
  MdHistory,
} from "react-icons/md";

interface Props {
  toolName: string;
  toolInput: Record<string, unknown>;
  result?: string;
  isError?: boolean;
  complete: boolean;
}

type ToolInput = Record<string, unknown>;

// ---------------------------------------------------------------------------
// Tool Categories (semantic, action-based coloring)
// ---------------------------------------------------------------------------

type ToolCategory = "read" | "write" | "execute" | "script" | "navigate" | "capture" | "interact" | "todo" | "task" | "agent" | "search" | "system";

function getToolCategory(toolName: string): ToolCategory {
  // Read/inspect tools (passive observation)
  if (["Read", "Glob", "Grep", "WebFetch", "WebSearch"].includes(toolName)) return "read";

  // Write/modify tools (creation, modification)
  if (["Write", "Edit", "NotebookEdit"].includes(toolName)) return "write";

  // Todo tracking (task list, progress)
  if (toolName === "TodoWrite") return "todo";

  // Task delegation (subagents)
  if (toolName === "Task") return "task";

  // Execute tools (running processes, terminal)
  if (["Bash", "Skill", "EnterPlanMode", "ExitPlanMode"].includes(toolName)) return "execute";

  // User interaction
  if (toolName === "AskUserQuestion") return "interact";

  // Orchestrator agent session tools
  if (["list_agent_sessions", "open_agent_session", "close_agent_session",
       "read_agent_session", "send_to_agent_session", "interrupt_agent_session",
       "list_history"].includes(toolName)) return "agent";

  // Orchestrator search tools
  if (["search_history", "search_memory"].includes(toolName)) return "search";

  // Orchestrator file tools — reuse existing categories
  if (toolName === "read_file") return "read";
  if (toolName === "write_file") return "write";

  // Browser MCP tools - categorize by action type
  if (toolName.startsWith("mcp__chrome-devtools__")) {
    const action = toolName.replace("mcp__chrome-devtools__", "");
    // Navigation/interaction actions
    if (["navigate_page", "click", "hover", "drag", "fill", "fill_form", "press_key", "handle_dialog", "new_page", "close_page", "select_page", "resize_page", "wait_for"].includes(action))
      return "navigate";
    // Capture/recording actions
    if (["take_screenshot", "take_snapshot", "performance_start_trace", "performance_stop_trace", "performance_analyze_insight"].includes(action))
      return "capture";
    // Script execution (blend of execute + navigate)
    if (action === "evaluate_script")
      return "script";
    // Inspection actions (console, network)
    if (["list_console_messages", "list_network_requests", "get_console_message", "get_network_request", "list_pages", "emulate"].includes(action))
      return "read";
  }

  return "system";
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatFilePath(path: unknown): string {
  if (typeof path !== "string") return String(path);
  const parts = path.split("/");
  if (parts.length > 3) {
    return `.../${parts.slice(-2).join("/")}`;
  }
  return path;
}

function getLanguageFromPath(filePath: string): string {
  const ext = filePath.split(".").pop()?.toLowerCase() || "";
  const langMap: Record<string, string> = {
    ts: "typescript",
    tsx: "tsx",
    js: "javascript",
    jsx: "jsx",
    py: "python",
    rs: "rust",
    go: "go",
    rb: "ruby",
    java: "java",
    c: "c",
    cpp: "cpp",
    h: "c",
    hpp: "cpp",
    css: "css",
    scss: "scss",
    html: "html",
    json: "json",
    yaml: "yaml",
    yml: "yaml",
    md: "markdown",
    sh: "bash",
    bash: "bash",
    zsh: "bash",
    sql: "sql",
    graphql: "graphql",
    dockerfile: "dockerfile",
    toml: "toml",
    xml: "xml",
  };
  return langMap[ext] || "text";
}

// ---------------------------------------------------------------------------
// Summary formatter (for collapsed view)
// ---------------------------------------------------------------------------

function shortId(id: unknown): string {
  if (typeof id !== "string") return "";
  return id.slice(0, 8);
}

function formatToolSummary(toolName: string, input: ToolInput): string {
  switch (toolName) {
    case "Read":
      return input.file_path ? `Read ${formatFilePath(input.file_path)}` : "Read";
    case "Write":
      return input.file_path ? `Write ${formatFilePath(input.file_path)}` : "Write";
    case "Edit":
      return input.file_path ? `Edit ${formatFilePath(input.file_path)}` : "Edit";
    case "Bash": {
      // Show description if available, otherwise show truncated command
      if (input.description) {
        return String(input.description);
      }
      if (input.command) {
        const cmd = String(input.command);
        return cmd.slice(0, 60) + (cmd.length > 60 ? "..." : "");
      }
      return "Bash";
    }
    case "Glob":
      return input.pattern ? `Glob ${input.pattern}` : "Glob";
    case "Grep":
      return input.pattern ? `Grep "${input.pattern}"` : "Grep";
    case "WebFetch":
      return input.url ? `Fetch ${input.url}` : "WebFetch";
    case "WebSearch":
      return input.query ? `Search "${input.query}"` : "WebSearch";
    case "Task":
      return input.description ? `Task: ${input.description}` : "Task";
    case "TodoWrite":
      return "Update todos";
    case "AskUserQuestion":
      return "Ask user";
    case "Skill":
      return input.skill ? `/${input.skill}` : "Skill";
    case "EnterPlanMode":
      return "Enter plan mode";
    case "ExitPlanMode":
      return "Exit plan mode";
    case "NotebookEdit":
      return input.notebook_path ? `Edit notebook ${formatFilePath(input.notebook_path)}` : "Edit notebook";

    // Orchestrator tools
    case "list_agent_sessions":
      return "List active sessions";
    case "open_agent_session":
      return input.resume_sdk_id ? "Resume session" : "Open agent session";
    case "close_agent_session":
      return input.session_id ? `Close session ${shortId(input.session_id)}` : "Close session";
    case "read_agent_session":
      return input.session_id ? `Read session ${shortId(input.session_id)}` : "Read session";
    case "send_to_agent_session": {
      if (input.message) {
        const msg = String(input.message);
        return msg.slice(0, 60) + (msg.length > 60 ? "..." : "");
      }
      return "Send to agent";
    }
    case "interrupt_agent_session":
      return input.session_id ? `Interrupt session ${shortId(input.session_id)}` : "Interrupt session";
    case "list_history":
      return "List session history";
    case "search_history":
      return input.query ? `Search history "${input.query}"` : "Search history";
    case "search_memory":
      return input.query ? `Search memory "${input.query}"` : "Search memory";
    case "read_file":
      return input.path ? `Read ${formatFilePath(input.path)}` : "Read file";
    case "write_file":
      return input.path ? `Write ${formatFilePath(input.path)}` : "Write file";
  }

  // MCP Chrome DevTools
  if (toolName.startsWith("mcp__chrome-devtools__")) {
    const mcpTool = toolName.replace("mcp__chrome-devtools__", "");
    switch (mcpTool) {
      case "navigate_page":
        if (input.type === "reload") return "Reload page";
        if (input.type === "back") return "Go back";
        if (input.type === "forward") return "Go forward";
        return input.url ? `Navigate to ${input.url}` : "Navigate";
      case "click":
        return input.dblClick ? "Double click" : "Click";
      case "fill":
        return "Fill input";
      case "fill_form":
        return "Fill form";
      case "take_screenshot":
        return "Screenshot";
      case "take_snapshot":
        return "Snapshot";
      case "evaluate_script":
        return "Run script";
      case "emulate":
        return "Emulate device";
      case "hover":
        return "Hover";
      case "drag":
        return "Drag";
      case "press_key":
        return input.key ? `Press ${input.key}` : "Press key";
      case "wait_for":
        return input.text ? `Wait for "${input.text}"` : "Wait";
      case "list_pages":
        return "List pages";
      case "list_console_messages":
        return "List console";
      case "list_network_requests":
        return "List network";
      case "select_page":
        return `Select page #${input.pageId}`;
      case "new_page":
        return "New page";
      case "close_page":
        return "Close page";
      case "resize_page":
        return `Resize to ${input.width}×${input.height}`;
      case "performance_start_trace":
        return "Start trace";
      case "performance_stop_trace":
        return "Stop trace";
      case "get_console_message":
        return "Get console message";
      case "get_network_request":
        return "Get network request";
      case "handle_dialog":
        return input.action === "accept" ? "Accept dialog" : "Dismiss dialog";
      case "upload_file":
        return "Upload file";
      default:
        return mcpTool.replace(/_/g, " ");
    }
  }

  // Generic MCP tool
  if (toolName.startsWith("mcp__")) {
    const parts = toolName.split("__");
    return parts.length >= 3 ? parts[2].replace(/_/g, " ") : toolName;
  }

  return toolName;
}

// ---------------------------------------------------------------------------
// Icon helper
// ---------------------------------------------------------------------------

function getToolIcon(toolName: string, complete: boolean, isError?: boolean): React.ReactNode {
  const iconClass = "tool-icon-svg";

  if (!complete) return <MdMoreHoriz className={iconClass} />;
  if (isError) return <MdError className={iconClass} />;

  switch (toolName) {
    case "Read":
      return <MdDescription className={iconClass} />;
    case "Write":
      return <MdCode className={iconClass} />;
    case "Edit":
      return <MdEdit className={iconClass} />;
    case "Bash":
      return <MdTerminal className={iconClass} />;
    case "Glob":
    case "Grep":
      return <MdSearch className={iconClass} />;
    case "WebFetch":
      return <MdLanguage className={iconClass} />;
    case "WebSearch":
      return <MdSearch className={iconClass} />;
    case "Task":
      return <MdSmartToy className={iconClass} />;
    case "TodoWrite":
      return <MdChecklist className={iconClass} />;
    case "AskUserQuestion":
      return <MdHelpOutline className={iconClass} />;
    case "Skill":
      return <MdAutoAwesome className={iconClass} />;
    case "EnterPlanMode":
    case "ExitPlanMode":
      return <MdEditNote className={iconClass} />;
    case "NotebookEdit":
      return <MdBook className={iconClass} />;

    // Orchestrator tools
    case "list_agent_sessions":
      return <MdList className={iconClass} />;
    case "open_agent_session":
      return <MdOpenInNew className={iconClass} />;
    case "close_agent_session":
      return <MdClose className={iconClass} />;
    case "read_agent_session":
      return <MdVisibility className={iconClass} />;
    case "send_to_agent_session":
      return <MdSend className={iconClass} />;
    case "interrupt_agent_session":
      return <MdStop className={iconClass} />;
    case "list_history":
      return <MdHistory className={iconClass} />;
    case "search_history":
    case "search_memory":
      return <MdSearch className={iconClass} />;
    case "read_file":
      return <MdDescription className={iconClass} />;
    case "write_file":
      return <MdCode className={iconClass} />;
  }

  if (toolName.startsWith("mcp__chrome-devtools__")) {
    const mcpTool = toolName.replace("mcp__chrome-devtools__", "");
    switch (mcpTool) {
      case "navigate_page":
        return <MdNavigation className={iconClass} />;
      case "click":
      case "hover":
      case "drag":
        return <MdTouchApp className={iconClass} />;
      case "fill":
      case "fill_form":
      case "press_key":
        return <MdKeyboard className={iconClass} />;
      case "take_screenshot":
        return <MdCameraAlt className={iconClass} />;
      case "take_snapshot":
        return <MdContentCopy className={iconClass} />;
      case "evaluate_script":
        return <MdBolt className={iconClass} />;
      case "list_console_messages":
      case "get_console_message":
        return <MdList className={iconClass} />;
      case "list_network_requests":
      case "get_network_request":
        return <MdNetworkCheck className={iconClass} />;
      case "performance_start_trace":
      case "performance_stop_trace":
        return <MdSpeed className={iconClass} />;
      default:
        return <MdBuild className={iconClass} />;
    }
  }

  return <MdBuild className={iconClass} />;
}

// ---------------------------------------------------------------------------
// Specialized input renderers
// ---------------------------------------------------------------------------

function WriteInputView({ filePath, content }: { filePath: string; content: string }) {
  const language = getLanguageFromPath(filePath);
  return (
    <div className="tool-write-view">
      <div className="tool-field">
        <span className="field-label">File</span>
        <span className="field-value">{filePath}</span>
      </div>
      <div className="tool-code-block">
        <div className="code-header">
          <span className="code-lang">{language}</span>
        </div>
        <SyntaxHighlighter
          language={language}
          style={oneDark}
          customStyle={{
            margin: 0,
            borderRadius: "0 0 var(--radius) var(--radius)",
            fontSize: "0.8rem",
            maxHeight: "400px",
          }}
        >
          {content}
        </SyntaxHighlighter>
      </div>
    </div>
  );
}

function EditInputView({ filePath, oldString, newString, replaceAll }: {
  filePath: string;
  oldString: string;
  newString: string;
  replaceAll?: boolean;
}) {
  // Generate unified diff
  const diffLines = useMemo(() => {
    const changes = Diff.diffLines(oldString, newString);
    return changes.flatMap((part) => {
      const lines = part.value.split('\n');
      // Remove trailing empty line from split
      if (lines[lines.length - 1] === '') lines.pop();
      return lines.map((line) => ({
        type: part.added ? 'add' : part.removed ? 'remove' : 'context',
        content: line,
      }));
    });
  }, [oldString, newString]);

  return (
    <div className="tool-edit-view">
      <div className="tool-field">
        <span className="field-label">File</span>
        <span className="field-value">{filePath}</span>
      </div>
      {replaceAll && (
        <div className="tool-field">
          <span className="field-label">Mode</span>
          <span className="field-value">Replace all occurrences</span>
        </div>
      )}
      <div className="unified-diff">
        {diffLines.map((line, i) => (
          <div key={i} className={`diff-line diff-line-${line.type}`}>
            <span className="diff-marker">
              {line.type === 'add' ? '+' : line.type === 'remove' ? '-' : ' '}
            </span>
            <span className="diff-content">{line.content || ' '}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function BashInputView({ command, description }: { command: string; description?: string }) {
  return (
    <div className="tool-bash-view">
      {description && (
        <div className="tool-field">
          <span className="field-label">Description</span>
          <span className="field-value">{description}</span>
        </div>
      )}
      <div className="tool-code-block">
        <div className="code-header">
          <span className="code-lang">bash</span>
        </div>
        <SyntaxHighlighter
          language="bash"
          style={oneDark}
          customStyle={{
            margin: 0,
            borderRadius: "0 0 var(--radius) var(--radius)",
            fontSize: "0.8rem",
          }}
        >
          {command}
        </SyntaxHighlighter>
      </div>
    </div>
  );
}

function TaskInputView({ description, prompt, subagentType }: {
  description?: string;
  prompt: string;
  subagentType?: string;
}) {
  return (
    <div className="tool-task-view">
      {description && (
        <div className="tool-field">
          <span className="field-label">Task</span>
          <span className="field-value task-description">{description}</span>
        </div>
      )}
      {subagentType && (
        <div className="tool-field">
          <span className="field-label">Agent</span>
          <span className="field-value agent-type">{subagentType}</span>
        </div>
      )}
      <div className="tool-field multiline">
        <span className="field-label">Prompt</span>
        <pre className="field-value task-prompt">{prompt}</pre>
      </div>
    </div>
  );
}

function SendToAgentInputView({ sessionId, message }: { sessionId: string; message: string }) {
  return (
    <div className="agent-session-view">
      <div className="tool-field">
        <span className="field-label">Session</span>
        <span className="field-value">{sessionId}</span>
      </div>
      <div className="tool-field multiline">
        <span className="field-label">Message</span>
        <pre className="field-value task-prompt">{message}</pre>
      </div>
    </div>
  );
}

function SearchInputView({ query, maxResults }: { query: string; maxResults?: number }) {
  return (
    <div className="search-input-view">
      <div className="tool-field">
        <span className="field-label">Query</span>
        <span className="field-value">{query}</span>
      </div>
      {maxResults !== undefined && (
        <div className="tool-field">
          <span className="field-label">Max</span>
          <span className="field-value">{maxResults}</span>
        </div>
      )}
    </div>
  );
}

function FileReadInputView({ path }: { path: string }) {
  return (
    <div className="tool-field">
      <span className="field-label">File</span>
      <span className="field-value">{path}</span>
    </div>
  );
}

function ReadInputView({
  filePath,
  offset,
  limit,
}: {
  filePath: string;
  offset?: number;
  limit?: number;
}) {
  return (
    <div className="tool-fields">
      <div className="tool-field">
        <span className="field-label">File</span>
        <span className="field-value file-path">{filePath}</span>
      </div>
      {(offset !== undefined || limit !== undefined) && (
        <div className="tool-field">
          <span className="field-label">Range</span>
          <span className="field-value">
            {offset !== undefined && limit !== undefined
              ? `lines ${offset + 1}–${offset + limit}`
              : offset !== undefined
              ? `from line ${offset + 1}`
              : `first ${limit} lines`}
          </span>
        </div>
      )}
    </div>
  );
}

function GrepInputView({
  pattern,
  path,
  glob,
  outputMode,
  lineNumbers,
  context,
}: {
  pattern: string;
  path?: string;
  glob?: string;
  outputMode?: string;
  lineNumbers?: boolean;
  context?: number;
}) {
  return (
    <div className="tool-fields">
      <div className="tool-field">
        <span className="field-label">Pattern</span>
        <span className="field-value code-value">{pattern}</span>
      </div>
      {path && (
        <div className="tool-field">
          <span className="field-label">Path</span>
          <span className="field-value file-path">{path}</span>
        </div>
      )}
      {glob && (
        <div className="tool-field">
          <span className="field-label">Glob</span>
          <span className="field-value code-value">{glob}</span>
        </div>
      )}
      {(outputMode || lineNumbers !== undefined || context !== undefined) && (
        <div className="tool-field">
          <span className="field-label">Options</span>
          <span className="field-value">
            {[
              outputMode && outputMode !== "files_with_matches" ? outputMode : null,
              lineNumbers ? "line numbers" : null,
              context !== undefined ? `±${context} context` : null,
            ]
              .filter(Boolean)
              .join(", ") || "default"}
          </span>
        </div>
      )}
    </div>
  );
}

function EvaluateScriptInputView({
  func,
  args,
}: {
  func: string;
  args?: Array<{ uid: string }>;
}) {
  return (
    <div className="tool-fields">
      <div className="tool-field multiline">
        <span className="field-label">Script</span>
        <SyntaxHighlighter
          language="javascript"
          style={oneDark}
          customStyle={{
            margin: 0,
            padding: "8px 10px",
            borderRadius: "var(--radius)",
            fontSize: "0.78rem",
            maxHeight: "200px",
            overflow: "auto",
          }}
        >
          {func}
        </SyntaxHighlighter>
      </div>
      {args && args.length > 0 && (
        <div className="tool-field">
          <span className="field-label">Args</span>
          <span className="field-value">
            {args.map((a) => a.uid).join(", ")}
          </span>
        </div>
      )}
    </div>
  );
}

function GenericInputView({ input }: { input: ToolInput }) {
  return <pre className="generic-json">{JSON.stringify(input, null, 2)}</pre>;
}

// ---------------------------------------------------------------------------
// Input renderer dispatcher
// ---------------------------------------------------------------------------

function renderToolInput(toolName: string, input: ToolInput) {
  switch (toolName) {
    case "Read":
      if (input.file_path) {
        return (
          <ReadInputView
            filePath={String(input.file_path)}
            offset={input.offset !== undefined ? Number(input.offset) : undefined}
            limit={input.limit !== undefined ? Number(input.limit) : undefined}
          />
        );
      }
      break;
    case "Grep":
      if (input.pattern) {
        return (
          <GrepInputView
            pattern={String(input.pattern)}
            path={input.path ? String(input.path) : undefined}
            glob={input.glob ? String(input.glob) : undefined}
            outputMode={input.output_mode ? String(input.output_mode) : undefined}
            lineNumbers={input["-n"] !== undefined ? Boolean(input["-n"]) : undefined}
            context={input.context !== undefined ? Number(input.context) :
                     input["-C"] !== undefined ? Number(input["-C"]) : undefined}
          />
        );
      }
      break;
    case "Write":
      if (input.file_path && input.content) {
        return (
          <WriteInputView
            filePath={String(input.file_path)}
            content={String(input.content)}
          />
        );
      }
      break;
    case "Edit":
      if (input.file_path && input.old_string !== undefined && input.new_string !== undefined) {
        return (
          <EditInputView
            filePath={String(input.file_path)}
            oldString={String(input.old_string)}
            newString={String(input.new_string)}
            replaceAll={Boolean(input.replace_all)}
          />
        );
      }
      break;
    case "Bash":
      if (input.command) {
        return (
          <BashInputView
            command={String(input.command)}
            description={input.description ? String(input.description) : undefined}
          />
        );
      }
      break;
    case "Task":
      if (input.prompt) {
        return (
          <TaskInputView
            description={input.description ? String(input.description) : undefined}
            prompt={String(input.prompt)}
            subagentType={input.subagent_type ? String(input.subagent_type) : undefined}
          />
        );
      }
      break;

    // Orchestrator tools
    case "send_to_agent_session":
      if (input.session_id && input.message) {
        return (
          <SendToAgentInputView
            sessionId={String(input.session_id)}
            message={String(input.message)}
          />
        );
      }
      break;
    case "read_agent_session":
      if (input.session_id) {
        return (
          <div className="agent-session-view">
            <div className="tool-field">
              <span className="field-label">Session</span>
              <span className="field-value">{String(input.session_id)}</span>
            </div>
            {input.max_messages !== undefined && (
              <div className="tool-field">
                <span className="field-label">Max</span>
                <span className="field-value">{String(input.max_messages)} messages</span>
              </div>
            )}
          </div>
        );
      }
      break;
    case "search_history":
    case "search_memory":
      if (input.query) {
        return (
          <SearchInputView
            query={String(input.query)}
            maxResults={input.max_results !== undefined ? Number(input.max_results) : undefined}
          />
        );
      }
      break;
    case "read_file":
      if (input.path) {
        return <FileReadInputView path={String(input.path)} />;
      }
      break;
    case "write_file":
      if (input.path && input.content) {
        return (
          <WriteInputView
            filePath={String(input.path)}
            content={String(input.content)}
          />
        );
      }
      break;

    // Chrome DevTools MCP tools
    case "mcp__chrome-devtools__evaluate_script":
      if (input.function) {
        return (
          <EvaluateScriptInputView
            func={String(input.function)}
            args={Array.isArray(input.args) ? input.args as Array<{ uid: string }> : undefined}
          />
        );
      }
      break;
  }

  // Default: show JSON
  return <GenericInputView input={input} />;
}

// ---------------------------------------------------------------------------
// Specialized blocks for non-collapsible tools
// ---------------------------------------------------------------------------

function TaskBlock({ toolName, toolInput, result, isError, complete }: Props) {
  const summary = formatToolSummary(toolName, toolInput);
  const icon = getToolIcon(toolName, complete, isError);
  const category = getToolCategory(toolName);

  return (
    <div className={`tool-block tool-${category} ${isError ? "tool-error" : ""}`}>
      <div className="tool-header">
        <span className="tool-icon">{icon}</span>
        <span className="tool-name">{summary}</span>
        {complete && (
          <span className={`tool-status ${isError ? "status-error" : "status-ok"}`}>
            {isError ? <MdError className="status-icon" /> : <MdCheckCircle className="status-icon" />}
            {isError ? "error" : "done"}
          </span>
        )}
      </div>
      <div className="tool-task-content">
        {renderToolInput(toolName, toolInput)}
        {result !== undefined && (
          <details open={result.length < 2000}>
            <summary className="tool-section-header">
              Output {result.length > 500 && `(${result.length} chars)`}
            </summary>
            <div className={`tool-result ${isError ? "result-error" : ""}`}>
              <pre>{result}</pre>
            </div>
          </details>
        )}
      </div>
    </div>
  );
}

// Todo item type
interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "completed";
  activeForm?: string;
}

// TodoWrite block - always expanded, shows todos as a formatted list
function TodoWriteBlock({ toolInput, isError, complete }: Props) {
  const icon = getToolIcon("TodoWrite", complete, isError);
  const category = getToolCategory("TodoWrite");

  // Parse todos from toolInput
  const todos: TodoItem[] = Array.isArray(toolInput.todos)
    ? (toolInput.todos as TodoItem[])
    : [];

  return (
    <div className={`tool-block tool-${category} ${isError ? "tool-error" : ""}`}>
      <div className="tool-header">
        <span className="tool-icon">{icon}</span>
        <span className="tool-name">Update todos</span>
        {complete && (
          <span className={`tool-status ${isError ? "status-error" : "status-ok"}`}>
            {isError ? <MdError className="status-icon" /> : <MdCheckCircle className="status-icon" />}
            {isError ? "error" : "done"}
          </span>
        )}
      </div>
      <div className="todo-block-content">
        {todos.length > 0 ? (
          <ul className="todo-list">
            {todos.map((todo, idx) => {
              const statusClass = `todo-item-${todo.status.replace("_", "-")}`;
              return (
                <li key={idx} className={`todo-item ${statusClass}`}>
                  <span className={`todo-indicator todo-indicator-${todo.status.replace("_", "-")}`}>
                    {todo.status === "completed" && <MdCheckCircle />}
                  </span>
                  <span className="todo-text">
                    {todo.status === "in_progress" && todo.activeForm
                      ? todo.activeForm
                      : todo.content}
                  </span>
                </li>
              );
            })}
          </ul>
        ) : (
          <span className="no-params">No todos</span>
        )}
      </div>
    </div>
  );
}

// Bash block - shows description + command in collapsed header, only output when expanded
// Collapsed: Shows truncated preview (5 lines max for multi-line, single line with ellipsis)
// Expanded: Shows full command with line wrapping, plus output
const BASH_PREVIEW_LINES = 5;

function BashBlock({ toolInput, result, isError, complete }: Props) {
  const [expanded, setExpanded] = useState(false);
  const icon = getToolIcon("Bash", complete, isError);
  const category = getToolCategory("Bash");

  const command = toolInput.command ? String(toolInput.command) : "";
  const description = toolInput.description ? String(toolInput.description) : null;

  // Split command into lines for preview truncation
  const commandLines = command.split("\n");
  const isMultiLine = commandLines.length > 1;
  const isTruncated = commandLines.length > BASH_PREVIEW_LINES;
  const previewCommand = isTruncated
    ? commandLines.slice(0, BASH_PREVIEW_LINES).join("\n")
    : command;

  return (
    <div className={`tool-block tool-${category} ${isError ? "tool-error" : ""}`}>
      <button
        className="tool-toggle bash-toggle"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="tool-icon">{icon}</span>
        <div className="bash-header-content">
          {description && <span className="bash-description">{description}</span>}
          <div className={`bash-command-wrapper ${expanded ? "bash-command-expanded" : ""}`}>
            <SyntaxHighlighter
              language="bash"
              style={oneDark}
              customStyle={{
                margin: 0,
                padding: "4px 8px",
                borderRadius: "4px",
                fontSize: "0.8rem",
                background: "var(--bg-elevated)",
                ...(expanded
                  ? {
                      // Expanded: show full command with line wrapping
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                      overflow: "visible",
                    }
                  : isMultiLine
                  ? {
                      // Collapsed multi-line: show preview with limited height
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                      overflow: "hidden",
                    }
                  : {
                      // Collapsed single-line: truncate with ellipsis
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }),
              }}
            >
              {expanded ? command : previewCommand}
            </SyntaxHighlighter>
            {!expanded && isTruncated && (
              <div className="bash-truncated-indicator">
                ... ({commandLines.length - BASH_PREVIEW_LINES} more lines)
              </div>
            )}
          </div>
        </div>
        {complete && (
          <span className={`tool-status ${isError ? "status-error" : "status-ok"}`}>
            {isError ? <MdError className="status-icon" /> : <MdCheckCircle className="status-icon" />}
            {isError ? "error" : "done"}
          </span>
        )}
        <span className="toggle-arrow">{expanded ? "▼" : "▶"}</span>
      </button>
      {expanded && result !== undefined && (
        <div className="tool-content">
          <details open>
            <summary className="tool-section-header">
              Output {result.length > 500 && `(${result.length} chars)`}
            </summary>
            <div className={`tool-result ${isError ? "result-error" : ""}`}>
              <pre>{result}</pre>
            </div>
          </details>
        </div>
      )}
    </div>
  );
}

// Send-to-agent block — shows message preview in header (like BashBlock)
function SendToAgentBlock({ toolInput, result, isError, complete }: Props) {
  const [expanded, setExpanded] = useState(false);
  const icon = getToolIcon("send_to_agent_session", complete, isError);
  const category = getToolCategory("send_to_agent_session");

  const sessionId = toolInput.session_id ? String(toolInput.session_id) : "";
  const message = toolInput.message ? String(toolInput.message) : "";

  return (
    <div className={`tool-block tool-${category} ${isError ? "tool-error" : ""}`}>
      <button
        className="tool-toggle bash-toggle"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="tool-icon">{icon}</span>
        <div className="send-agent-header-content">
          {sessionId && <span className="send-agent-session-id">session {shortId(sessionId)}</span>}
          <span className="send-agent-message-preview">{message || "Send to agent"}</span>
        </div>
        {complete && (
          <span className={`tool-status ${isError ? "status-error" : "status-ok"}`}>
            {isError ? <MdError className="status-icon" /> : <MdCheckCircle className="status-icon" />}
            {isError ? "error" : "done"}
          </span>
        )}
        <span className="toggle-arrow">{expanded ? "▼" : "▶"}</span>
      </button>
      {expanded && (
        <div className="tool-content">
          <details open>
            <summary className="tool-section-header">Input</summary>
            <div className="tool-input">
              <SendToAgentInputView sessionId={sessionId} message={message} />
            </div>
          </details>
          {result !== undefined && (
            <details open={result.length < 500}>
              <summary className="tool-section-header">
                Output {result.length > 500 && `(${result.length} chars)`}
              </summary>
              <div className={`tool-result ${isError ? "result-error" : ""}`}>
                <pre>{result}</pre>
              </div>
            </details>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ToolUseBlock({ toolName, toolInput, result, isError, complete }: Props) {
  const [expanded, setExpanded] = useState(false);

  // Task tool is always expanded (non-collapsible)
  if (toolName === "Task") {
    return <TaskBlock {...{ toolName, toolInput, result, isError, complete }} />;
  }

  // TodoWrite tool shows formatted todo list (always expanded)
  if (toolName === "TodoWrite") {
    return <TodoWriteBlock {...{ toolName, toolInput, result, isError, complete }} />;
  }

  // Bash tool has special header showing description + command
  if (toolName === "Bash") {
    return <BashBlock {...{ toolName, toolInput, result, isError, complete }} />;
  }

  // Send-to-agent shows message preview in header (like Bash)
  if (toolName === "send_to_agent_session") {
    return <SendToAgentBlock {...{ toolName, toolInput, result, isError, complete }} />;
  }

  const summary = formatToolSummary(toolName, toolInput);
  const icon = getToolIcon(toolName, complete, isError);
  const category = getToolCategory(toolName);

  return (
    <div className={`tool-block tool-${category} ${isError ? "tool-error" : ""}`}>
      <button
        className="tool-toggle"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="tool-icon">{icon}</span>
        <span className="tool-name">{summary}</span>
        {complete && (
          <span className={`tool-status ${isError ? "status-error" : "status-ok"}`}>
            {isError ? <MdError className="status-icon" /> : <MdCheckCircle className="status-icon" />}
            {isError ? "error" : "done"}
          </span>
        )}
        <span className="toggle-arrow">{expanded ? "▼" : "▶"}</span>
      </button>
      {expanded && (
        <div className="tool-content">
          <details open>
            <summary className="tool-section-header">Input</summary>
            <div className="tool-input">
              {renderToolInput(toolName, toolInput)}
            </div>
          </details>
          {result !== undefined && (
            <details open={result.length < 500}>
              <summary className="tool-section-header">
                Output {result.length > 500 && `(${result.length} chars)`}
              </summary>
              <div className={`tool-result ${isError ? "result-error" : ""}`}>
                <pre>{result}</pre>
              </div>
            </details>
          )}
        </div>
      )}
    </div>
  );
}
