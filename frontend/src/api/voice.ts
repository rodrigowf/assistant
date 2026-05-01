/** Voice API client — fetches ephemeral tokens from the backend. */

const VOICE_SESSION_URL = "/api/orchestrator/voice/session";
const VOICE_MODEL = "gpt-realtime";

/** Connection metadata for any voice provider, returned alongside legacy
 * client_secret fields for backward compatibility. New code should prefer
 * connection_info.endpoint over the hardcoded URL below. */
export interface VoiceConnectionInfo {
  connection_type: "webrtc" | "websocket";
  endpoint: string;
  ephemeral_token: string | null;
  expires_at: number | null;
  audio_in_format: { sample_rate: number; encoding: string };
  audio_out_format: { sample_rate: number; encoding: string };
  model: string;
  voice: string;
}

export interface EphemeralTokenResponse {
  // Legacy OpenAI-shaped fields (still present for openai provider).
  client_secret: {
    value: string;
    expires_at: number;
  };
  model: string;
  voice: string;
  // Provider-agnostic metadata — preferred for new clients.
  connection_info?: VoiceConnectionInfo;
}

/** Fetch a short-lived ephemeral token from the backend. */
export async function fetchEphemeralToken(): Promise<EphemeralTokenResponse> {
  const res = await fetch(VOICE_SESSION_URL, { method: "POST" });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`Failed to get voice session token: ${res.status} ${detail}`);
  }
  return res.json();
}

/** Post an SDP offer to OpenAI Realtime API and return the SDP answer.
 * Uses the canonical /v1/realtime/calls endpoint when callUrl is provided
 * (from connection_info.endpoint), falling back to the legacy /v1/realtime URL.
 */
export async function exchangeSDP(
  ephemeralKey: string,
  offerSdp: string,
  callUrl?: string,
): Promise<string> {
  const url = callUrl ?? `https://api.openai.com/v1/realtime/calls?model=${VOICE_MODEL}`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${ephemeralKey}`,
      "Content-Type": "application/sdp",
    },
    body: offerSdp,
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`OpenAI SDP exchange failed: ${res.status} ${detail}`);
  }
  return res.text();
}
