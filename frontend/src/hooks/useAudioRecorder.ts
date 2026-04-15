import { useState, useRef, useCallback } from "react";

export type RecordingState = "idle" | "recording" | "processing";

interface UseAudioRecorderOptions {
  /** Called when recording is complete with base64-encoded audio */
  onRecordingComplete: (audioBase64: string, format: string) => void;
  /** Called on error */
  onError?: (error: string) => void;
  /** Max recording duration in seconds (default: 60) */
  maxDuration?: number;
}

interface UseAudioRecorderResult {
  /** Current recording state */
  state: RecordingState;
  /** Start recording */
  startRecording: () => Promise<void>;
  /** Stop recording and process */
  stopRecording: () => void;
  /** Cancel recording without processing */
  cancelRecording: () => void;
  /** Recording duration in seconds */
  duration: number;
}

/**
 * Hook for recording audio using MediaRecorder API.
 * Records in webm/opus format for efficient compression.
 */
export function useAudioRecorder({
  onRecordingComplete,
  onError,
  maxDuration = 60,
}: UseAudioRecorderOptions): UseAudioRecorderResult {
  const [state, setState] = useState<RecordingState>("idle");
  const [duration, setDuration] = useState(0);

  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const durationIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const maxDurationTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cleanup = useCallback(() => {
    // Clear timers
    if (durationIntervalRef.current) {
      clearInterval(durationIntervalRef.current);
      durationIntervalRef.current = null;
    }
    if (maxDurationTimeoutRef.current) {
      clearTimeout(maxDurationTimeoutRef.current);
      maxDurationTimeoutRef.current = null;
    }

    // Stop media recorder
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== "inactive") {
      mediaRecorderRef.current.stop();
    }
    mediaRecorderRef.current = null;

    // Stop all tracks
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    }

    chunksRef.current = [];
  }, []);

  const processRecording = useCallback(async () => {
    if (chunksRef.current.length === 0) {
      setState("idle");
      return;
    }

    setState("processing");

    try {
      // Determine the MIME type from what was recorded
      const mimeType = mediaRecorderRef.current?.mimeType || "audio/webm";
      const blob = new Blob(chunksRef.current, { type: mimeType });

      // Convert to base64
      const arrayBuffer = await blob.arrayBuffer();
      const base64 = btoa(
        new Uint8Array(arrayBuffer).reduce(
          (data, byte) => data + String.fromCharCode(byte),
          ""
        )
      );

      // Determine format from MIME type
      let format = "webm";
      if (mimeType.includes("ogg")) {
        format = "ogg";
      } else if (mimeType.includes("mp4")) {
        format = "mp4";
      } else if (mimeType.includes("wav")) {
        format = "wav";
      }

      onRecordingComplete(base64, format);
    } catch (err) {
      onError?.(`Failed to process recording: ${err}`);
    } finally {
      cleanup();
      setState("idle");
      setDuration(0);
    }
  }, [onRecordingComplete, onError, cleanup]);

  const startRecording = useCallback(async () => {
    if (state !== "idle") return;

    try {
      // Request microphone access
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          sampleRate: 16000,
        },
      });
      streamRef.current = stream;

      // Determine best available MIME type
      const mimeTypes = [
        "audio/webm;codecs=opus",
        "audio/webm",
        "audio/ogg;codecs=opus",
        "audio/mp4",
      ];
      let selectedMimeType = "";
      for (const type of mimeTypes) {
        if (MediaRecorder.isTypeSupported(type)) {
          selectedMimeType = type;
          break;
        }
      }

      if (!selectedMimeType) {
        throw new Error("No supported audio MIME type found");
      }

      // Create MediaRecorder
      const mediaRecorder = new MediaRecorder(stream, {
        mimeType: selectedMimeType,
      });
      mediaRecorderRef.current = mediaRecorder;
      chunksRef.current = [];

      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          chunksRef.current.push(event.data);
        }
      };

      mediaRecorder.onstop = () => {
        processRecording();
      };

      mediaRecorder.onerror = () => {
        onError?.("Recording error occurred");
        cleanup();
        setState("idle");
        setDuration(0);
      };

      // Start recording
      mediaRecorder.start();
      setState("recording");
      setDuration(0);

      // Track duration
      const startTime = Date.now();
      durationIntervalRef.current = setInterval(() => {
        setDuration(Math.floor((Date.now() - startTime) / 1000));
      }, 100);

      // Auto-stop at max duration
      maxDurationTimeoutRef.current = setTimeout(() => {
        if (mediaRecorderRef.current?.state === "recording") {
          mediaRecorderRef.current.stop();
        }
      }, maxDuration * 1000);
    } catch (err) {
      if (err instanceof DOMException && err.name === "NotAllowedError") {
        onError?.("Microphone access denied");
      } else if (err instanceof DOMException && err.name === "NotFoundError") {
        onError?.("No microphone found");
      } else {
        onError?.(`Failed to start recording: ${err}`);
      }
      cleanup();
      setState("idle");
    }
  }, [state, maxDuration, onError, cleanup, processRecording]);

  const stopRecording = useCallback(() => {
    if (state !== "recording" || !mediaRecorderRef.current) return;

    // Clear timers first
    if (durationIntervalRef.current) {
      clearInterval(durationIntervalRef.current);
      durationIntervalRef.current = null;
    }
    if (maxDurationTimeoutRef.current) {
      clearTimeout(maxDurationTimeoutRef.current);
      maxDurationTimeoutRef.current = null;
    }

    // Stop recording - this triggers onstop which calls processRecording
    if (mediaRecorderRef.current.state === "recording") {
      mediaRecorderRef.current.stop();
    }
  }, [state]);

  const cancelRecording = useCallback(() => {
    if (state === "idle") return;

    cleanup();
    setState("idle");
    setDuration(0);
  }, [state, cleanup]);

  return {
    state,
    startRecording,
    stopRecording,
    cancelRecording,
    duration,
  };
}
