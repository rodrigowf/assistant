// Compat shim: replaces react-syntax-highlighter with a plain <pre><code> block.
// Safari 12 cannot handle the named capture group regexes in Prism.
import { type ReactNode } from "react";

interface Props {
  language?: string;
  style?: Record<string, unknown>;
  customStyle?: React.CSSProperties;
  children?: ReactNode;
  [key: string]: unknown;
}

function PlainCode({ children, customStyle }: Props) {
  return (
    <pre style={{ margin: 0, overflowX: "auto", fontSize: "0.85rem", ...customStyle }}>
      <code>{children}</code>
    </pre>
  );
}

export const Prism = PlainCode;
export const Light = PlainCode;
export default PlainCode;
