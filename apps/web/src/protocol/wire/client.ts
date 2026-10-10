/**
 * Client → server messages. Always sent as TEXT frames (T-2: a binary frame makes the server
 * drop the socket). Field names are the backend's (inventory 01 §4.1, §5.2, §7.4).
 */
import type { Checkpoint } from '../types';

export interface StartMessage {
  type: 'start';
  local_id: string;
  resume_sdk_id?: string;
  resume_from?: Checkpoint;
  /** OPEN-2: subscribe only; the server answers `error{session_closed}` instead of re-creating it. */
  reattach?: true;
}

/** Voice fields of `voice_start` (V-1: omit all of them to use the server defaults). */
export interface VoiceConfigFields {
  voice_provider?: string;
  voice_model?: string;
  voice_name?: string;
  voice_transcription_language?: string;
  voice_endpoint?: string;
}

export interface VoiceStartMessage extends VoiceConfigFields {
  type: 'voice_start';
  local_id: string;
  resume_sdk_id?: string;
  /** OPEN-2, as on `start`. */
  reattach?: true;
}

export type ClientMessage =
  | StartMessage
  | VoiceStartMessage
  | { type: 'send'; text: string }
  | { type: 'interrupt' }
  | { type: 'compact' }
  | { type: 'stop' }
  | { type: 'command'; text: string }
  | { type: 'permission_response'; request_id: string; decision: 'allow' | 'deny'; message?: string; session_id?: string }
  // orchestrator only
  | { type: 'inject_text'; text: string }
  | { type: 'send_audio'; audio: string; format: string; text?: string }
  | { type: 'set_model'; model: string }
  | { type: 'get_model' }
  | { type: 'get_models' }
  | { type: 'voice_stop' }
  | { type: 'voice_event'; event: Record<string, unknown> }
  | { type: 'voice_audio_in'; audio: string }
  | { type: 'voice_recording_chunk'; channel: 'user' | 'assistant'; audio: string }
  | { type: 'voice_recording_end' };

/** Serialise for a text frame (T-2). */
export function encodeClientMessage(msg: ClientMessage): string {
  return JSON.stringify(msg);
}
