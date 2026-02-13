import type { ServerEvent } from "../types";

export type EventHandler = (event: ServerEvent) => void;
export type OpenHandler = () => void;

export class ChatSocket {
  private ws: WebSocket | null = null;
  private handler: EventHandler;
  private onOpen: OpenHandler | null = null;
  private _url: string;

  constructor(handler: EventHandler) {
    this.handler = handler;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    this._url = `${proto}//${location.host}/api/sessions/chat`;
  }

  connect(onOpen?: OpenHandler): void {
    this.onOpen = onOpen ?? null;
    this.ws = new WebSocket(this._url);
    this.ws.binaryType = "arraybuffer";

    this.ws.onopen = () => {
      this.onOpen?.();
    };

    this.ws.onmessage = (e) => {
      const data = typeof e.data === "string"
        ? e.data
        : new TextDecoder().decode(e.data);
      try {
        const event: ServerEvent = JSON.parse(data);
        this.handler(event);
      } catch {
        // ignore malformed frames
      }
    };

    this.ws.onclose = () => {
      this.handler({ type: "status", status: "disconnected" });
    };

    this.ws.onerror = () => {
      this.handler({ type: "error", error: "websocket_error" });
    };
  }

  send(msg: Record<string, unknown>): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
  }

  get readyState(): number {
    return this.ws?.readyState ?? WebSocket.CLOSED;
  }
}
