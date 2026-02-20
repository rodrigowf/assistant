/** Voice API client — fetches ephemeral tokens from the backend. */

const VOICE_SESSION_URL = "/api/orchestrator/voice/session";
const OPENAI_REALTIME_URL = "https://api.openai.com/v1/realtime";
const VOICE_MODEL = "gpt-realtime";

export interface EphemeralTokenResponse {
  client_secret: {
    value: string;
    expires_at: number;
  };
  id: string;
  model: string;
  voice: string;
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

/** Post an SDP offer to OpenAI Realtime API and return the SDP answer. */
export async function exchangeSDP(ephemeralKey: string, offerSdp: string): Promise<string> {
  const res = await fetch(`${OPENAI_REALTIME_URL}?model=${VOICE_MODEL}`, {
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
