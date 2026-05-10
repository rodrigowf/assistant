import type { ServerEvent } from "../types";

export type EventHandler = (event: ServerEvent) => void;
export type OpenHandler = () => void;

export class ChatSocket {
  private ws: WebSocket | null = null;
  private handler: EventHandler;
  private onOpen: OpenHandler | null = null;
  private _url: string;

  constructor(handler: EventHandler, endpoint = "/api/sessions/chat") {
    this.handler = handler;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    this._url = `${proto}//${location.host}${endpoint}`;
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
        // [REPRO] log every WS event so we can compare backend-sent vs frontend-received
        // eslint-disable-next-line no-console
        console.log("[REPRO-WS]", this._url.split("/").pop(), event.type ?? "(no-type)", event);
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
