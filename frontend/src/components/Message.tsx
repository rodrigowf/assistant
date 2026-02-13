import { useState } from "react";
import type { ChatMessage } from "../types";
import { Markdown } from "./Markdown";
import { ThinkingBlock } from "./ThinkingBlock";
import { ToolUseBlock } from "./ToolUseBlock";

interface Props {
  message: ChatMessage;
}

const LINE_THRESHOLD = 25;

function UserTextBlock({ content }: { content: string }) {
  const lineCount = content.split("\n").length;
  const isTall = lineCount > LINE_THRESHOLD;
  const [expanded, setExpanded] = useState(false);

  if (!isTall) {
    return <div className="user-text">{content}</div>;
  }

  return (
    <div className={`user-text user-text-foldable ${expanded ? "expanded" : "collapsed"}`}>
      <div className="user-text-content">{content}</div>
      <button className="user-text-toggle" onClick={() => setExpanded(!expanded)}>
        {expanded ? "Show less" : `Show all (${lineCount} lines)`}
      </button>
    </div>
  );
}

export function Message({ message }: Props) {
  const isUser = message.role === "user";

  return (
    <div className={`message ${isUser ? "message-user" : "message-assistant"}`}>
      {message.blocks.map((block, i) => {
        if (block.type === "text") {
          return isUser ? (
            <UserTextBlock key={i} content={block.content} />
          ) : (
            <Markdown key={i} content={block.content} />
          );
        }
        if (block.type === "thinking") {
          return (
            <ThinkingBlock
              key={i}
              content={block.content}
              streaming={block.streaming}
            />
          );
        }
        if (block.type === "tool_use") {
          return (
            <ToolUseBlock
              key={i}
              toolName={block.toolName}
              toolInput={block.toolInput}
              result={block.result}
              isError={block.isError}
              complete={block.complete}
            />
          );
        }
        return null;
      })}
    </div>
  );
}
