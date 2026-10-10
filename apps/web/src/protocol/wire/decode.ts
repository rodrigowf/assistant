/**
 * Tolerant frame decoder (spec 12 T-1, T-3). Accepts binary (UTF-8 JSON, the backend's form,
 * G-2) or text frames. Never throws:
 * - not JSON, not an object, or no string `type` → `{ok: false}` (log and drop; never reaches the
 *   reducer, T-3);
 * - a known `type` → fields of the wrong JSON type are coerced to safe defaults (required) or
 *   removed (optional), so the reducer can trust the static types;
 * - an unknown `type` → `{type: 'unknown', original_type, raw}` (the reducer ignores it).
 */
import type { ServerFrame, ServerFrameType } from './server';

type FieldKind = 'str' | 'str?' | 'num' | 'num?' | 'bool' | 'bool?' | 'obj' | 'obj?' | 'arr?' | 'any';
type Schema = Readonly<Record<string, FieldKind>>;

const STR = 'str';
const OPT_STR = 'str?';
const OPT_NUM = 'num?';
const OPT_BOOL = 'bool?';
const OBJ = 'obj';
const OPT_OBJ = 'obj?';

const SCHEMAS: Readonly<Record<ServerFrameType, Schema>> = {
  session_started: {
    session_id: STR,
    context_window: OPT_NUM,
    replay_overflow: OPT_BOOL,
    voice: OPT_BOOL,
    voice_initiator: OPT_BOOL,
    model_info: OPT_OBJ,
    jsonl_id: OPT_STR,
    voice_provider: OPT_STR,
    voice_model: OPT_STR,
    voice_name: OPT_STR,
    voice_transcription_language: OPT_STR,
    voice_recording_enabled: OPT_BOOL,
    voice_session_update: OPT_OBJ,
    voice_connection_info: OPT_OBJ,
    voice_connection_error: OPT_STR,
  },
  status: { status: STR, detail: OPT_STR },
  user_message: { text: STR, queued: OPT_BOOL, source: OPT_STR },
  text_delta: { text: STR },
  text_complete: { text: STR },
  thinking_delta: { text: STR },
  thinking_complete: { text: STR },
  tool_use: { tool_use_id: STR, tool_name: STR, tool_input: OBJ },
  tool_result: { tool_use_id: STR, output: 'any', is_error: OPT_BOOL },
  tool_executing: { tool_use_id: STR, tool_name: OPT_STR },
  tool_progress: { tool_use_id: STR, tool_name: OPT_STR, elapsed_seconds: 'num', message: STR },
  turn_complete: {
    cost: OPT_NUM,
    usage: OPT_OBJ,
    input_tokens: OPT_NUM,
    output_tokens: OPT_NUM,
    num_turns: OPT_NUM,
    session_id: OPT_STR,
    is_error: OPT_BOOL,
    result: OPT_STR,
  },
  compact_complete: { trigger: OPT_STR, summary: OPT_STR, tokens_before: OPT_NUM, tokens_after: OPT_NUM },
  session_stalled: { elapsed_seconds: 'num', last_tool_name: OPT_STR, last_tool_use_id: OPT_STR },
  permission_request: { request_id: STR, tool_name: STR, tool_input: OBJ },
  permission_resolved: { request_id: STR, decision: STR, responder: OPT_STR, message: OPT_STR },
  session_terminated: { reason: STR, detail: OPT_STR, sdk_session_id: OPT_STR },
  session_stopped: {},
  error: { error: STR, detail: OPT_STR },
  model_changed: { model_info: OPT_OBJ },
  model_info: { model_info: OPT_OBJ },
  models_list: { models: 'arr?' },
  nested_session_event: { session_id: STR, event_type: STR, event_data: OBJ },
  agent_session_opened: { session_id: STR, sdk_session_id: OPT_STR, is_orchestrator: OPT_BOOL },
  agent_session_closed: { session_id: STR, is_orchestrator: OPT_BOOL },
  orchestrator_switch: { sdk_session_id: STR, title: OPT_STR, voice: OPT_BOOL, from_session_id: OPT_STR },
  agent_turn_started: { session_id: STR, sdk_session_id: OPT_STR, provider: OPT_STR },
  agent_turn_finished: {
    session_id: STR,
    sdk_session_id: OPT_STR,
    provider: OPT_STR,
    title: OPT_STR,
    status: STR,
    preview: OPT_STR,
    error: OPT_STR,
  },
  visualization_changed: { visualizations: 'arr?', files: 'arr?' },
  memory_changed: { changes: 'arr?' },
  audio_upload: {},
  ping: {},
  voice_event: { event: OBJ },
  voice_owner_active: { active: 'bool', owner_local_id: OPT_STR },
  voice_command: { command: OBJ },
  voice_audio_out: { audio: STR },
  voice_connection_error: { detail: OPT_STR },
  voice_ending: { reason: OPT_STR, session_id: OPT_STR },
  voice_ended: { reason: OPT_STR, session_id: OPT_STR },
  voice_stopped: {},
};

export function isPlainObject(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null && !Array.isArray(x);
}

function has(o: object, k: string): boolean {
  return Object.prototype.hasOwnProperty.call(o, k);
}

function fits(kind: FieldKind, v: unknown): boolean {
  switch (kind) {
    case 'str':
      return typeof v === 'string';
    case 'str?':
      return typeof v === 'string' || v === null;
    case 'num':
      return typeof v === 'number' && isFinite(v);
    case 'num?':
      return (typeof v === 'number' && isFinite(v)) || v === null;
    case 'bool':
      return typeof v === 'boolean';
    case 'bool?':
      return typeof v === 'boolean' || v === null;
    case 'obj':
      return isPlainObject(v);
    case 'obj?':
      return isPlainObject(v) || v === null;
    case 'arr?':
      return Array.isArray(v) || v === null;
    default:
      return true;
  }
}

function fallback(kind: FieldKind): unknown {
  switch (kind) {
    case 'str':
      return '';
    case 'num':
      return 0;
    case 'bool':
      return false;
    case 'obj':
      return {};
    default:
      return undefined;
  }
}

/** Validate a parsed JSON value as a frame. Exported for inputs that are already objects. */
export function coerceFrame(value: unknown): DecodeResult {
  if (!isPlainObject(value)) return { ok: false, reason: 'not a JSON object' };
  const type = value.type;
  if (typeof type !== 'string') return { ok: false, reason: 'missing string "type"' };

  const omit = new Set<string>();
  // Resume cursor: only a finite number + non-empty string count (spec 12 §3.6).
  if (has(value, 'seq') && !(typeof value.seq === 'number' && isFinite(value.seq))) omit.add('seq');
  if (has(value, 'stream_id') && !(typeof value.stream_id === 'string' && value.stream_id !== '')) omit.add('stream_id');
  const out: Record<string, unknown> = {};
  for (const k of Object.keys(value)) if (!omit.has(k)) out[k] = value[k];

  if (!has(SCHEMAS, type)) {
    const unknown: Record<string, unknown> = { type: 'unknown', original_type: type, raw: value };
    if (has(out, 'seq')) unknown.seq = out.seq;
    if (has(out, 'stream_id')) unknown.stream_id = out.stream_id;
    return { ok: true, frame: unknown as unknown as ServerFrame };
  }

  const schema = SCHEMAS[type as ServerFrameType];
  for (const field of Object.keys(schema)) {
    const kind = schema[field] as FieldKind;
    if (kind === 'any') continue;
    const present = has(out, field);
    if (present && fits(kind, out[field])) continue;
    const def = fallback(kind);
    if (def !== undefined) out[field] = def;
    else if (present) omit.add(field);
  }

  if (type === 'session_started' && has(out, 'resume_state')) {
    const rs = out.resume_state;
    const valid =
      isPlainObject(rs) &&
      typeof rs.stream_id === 'string' &&
      rs.stream_id !== '' &&
      typeof rs.next_seq === 'number' &&
      isFinite(rs.next_seq);
    if (!valid) omit.add('resume_state');
  }

  if (omit.size === 0) return { ok: true, frame: out as unknown as ServerFrame };
  const clean: Record<string, unknown> = {};
  for (const k of Object.keys(out)) if (!omit.has(k)) clean[k] = out[k];
  return { ok: true, frame: clean as unknown as ServerFrame };
}

export type DecodeResult = { ok: true; frame: ServerFrame } | { ok: false; reason: string };

export type Utf8Decoder = (bytes: Uint8Array) => string;

interface TextDecoderLike {
  decode(bytes: Uint8Array): string;
}

let platformDecoder: TextDecoderLike | null | undefined;

function defaultUtf8(bytes: Uint8Array): string {
  if (platformDecoder === undefined) {
    const Ctor = (globalThis as unknown as { TextDecoder?: new (label?: string) => TextDecoderLike }).TextDecoder;
    platformDecoder = Ctor ? new Ctor('utf-8') : null;
  }
  return platformDecoder ? platformDecoder.decode(bytes) : utf8Decode(bytes);
}

/** Minimal UTF-8 decoder (fallback where `TextDecoder` is missing). Invalid bytes → U+FFFD. */
export function utf8Decode(bytes: Uint8Array): string {
  let out = '';
  let i = 0;
  const n = bytes.length;
  const at = (k: number): number => bytes[k] ?? 0;
  const cont = (k: number): boolean => k < n && (at(k) & 0xc0) === 0x80;
  while (i < n) {
    const b = at(i);
    let cp = 0xfffd;
    let len = 1;
    if (b < 0x80) {
      cp = b;
    } else if (b >= 0xc2 && b < 0xe0 && cont(i + 1)) {
      cp = ((b & 0x1f) << 6) | (at(i + 1) & 0x3f);
      len = 2;
    } else if (b >= 0xe0 && b < 0xf0 && cont(i + 1) && cont(i + 2)) {
      cp = ((b & 0x0f) << 12) | ((at(i + 1) & 0x3f) << 6) | (at(i + 2) & 0x3f);
      len = 3;
      if (cp < 0x800 || (cp >= 0xd800 && cp < 0xe000)) cp = 0xfffd;
    } else if (b >= 0xf0 && b < 0xf5 && cont(i + 1) && cont(i + 2) && cont(i + 3)) {
      cp = ((b & 0x07) << 18) | ((at(i + 1) & 0x3f) << 12) | ((at(i + 2) & 0x3f) << 6) | (at(i + 3) & 0x3f);
      len = 4;
      if (cp < 0x10000 || cp > 0x10ffff) cp = 0xfffd;
    }
    if (cp > 0xffff) {
      const c = cp - 0x10000;
      out += String.fromCharCode(0xd800 + (c >> 10), 0xdc00 + (c & 0x3ff));
    } else {
      out += String.fromCharCode(cp);
    }
    i += len;
  }
  return out;
}

/**
 * Decode one WebSocket message. `data` is what the socket delivered: an `ArrayBuffer` (web with
 * `binaryType = "arraybuffer"`), a byte view, or a string (text frame).
 */
export function decodeFrame(data: unknown, utf8: Utf8Decoder = defaultUtf8): DecodeResult {
  let text: string;
  try {
    if (typeof data === 'string') text = data;
    else if (data instanceof ArrayBuffer) text = utf8(new Uint8Array(data));
    else if (ArrayBuffer.isView(data)) text = utf8(new Uint8Array(data.buffer, data.byteOffset, data.byteLength));
    else return { ok: false, reason: 'unsupported frame data' };
  } catch {
    return { ok: false, reason: 'undecodable bytes' };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { ok: false, reason: 'invalid JSON' };
  }
  return coerceFrame(parsed);
}
