/**
 * `@/services` (W-06): everything between the wire and the UI (spec 13 §3.4, §3.9) — REST,
 * sockets, session runtimes, pool sync, uploads, capability probes. Built on `@/protocol`
 * (the pure reducer) and writing to `@/stores`. The voice engine (W-12) plugs into
 * `ArchieRuntime` through `VoiceBridge`.
 */
export * from './http/types';
export { harnessesFromProviders, providersFromHarnesses, qwenCatalogFromModels } from './harnessFallback';
export { api, type VoiceTarget } from './http/endpoints';
export { request, http, encodePath, buildQuery, tolerate404, DEFAULT_TIMEOUT_MS, type RequestOptions, type Query } from './http/client';
export {
  ApiError,
  PayloadTooLargeError,
  NetworkError,
  TimeoutError,
  AbortedError,
  PAYLOAD_TOO_LARGE_MESSAGE,
  errorMessage,
  errorFromResponse,
  isApiError,
} from './http/errors';
export {
  configureServices,
  documentVisibility,
  getEnv,
  resetServicesEnv,
  httpUrl,
  wsUrl,
  type ServicesEnv,
  type VisibilitySource,
  type WebSocketCtor,
  type WebSocketLike,
} from './env';
export { ArchieSocket, CHAT_WS_PATH, ORCHESTRATOR_WS_PATH } from './ws/socket';
export { Reconnector, backoffPolicy, legacyPolicy, setDefaultReconnectPolicy, type ReconnectPolicy } from './ws/reconnect';
export { ConversationRuntime, EMPTY_PAGE, HISTORY_PAGE_SIZE, type RuntimeHooks } from './sessions/ConversationRuntime';
export { SessionRuntime, truncateWithRetry, type SessionRuntimeOptions } from './sessions/SessionRuntime';
export { ArchieRuntime, type ArchieRuntimeOptions, type VoiceBridge, type VoiceHooks } from './sessions/ArchieRuntime';
export { OrchestratorChannel, type AgentTurnFrame } from './sessions/orchestratorChannel';
export {
  closeSession,
  closeTab,
  deleteSession,
  fetchPool,
  forkSession,
  getArchieRuntime,
  getOrchestratorRef,
  getSessionRuntime,
  listRuntimes,
  onAgentTurn,
  onOrchestratorSwitch,
  onWatcherEvent,
  openArchie,
  openSession,
  replaceInPlace,
  replaceRunningArchie,
  respondAgentPermission,
  rewindSession,
  runtimeHooks,
  servicesStarted,
  SessionActionError,
  setSessionHidden,
  setSwitchVoiceHandler,
  startServices,
  stopServices,
  syncPool,
  titleOf,
  type AnyRuntime,
  type OpenArchieResult,
  type OpenSessionOptions,
  type StartServicesOptions,
  type SwitchVoiceHandler,
} from './sessions/manager';
export {
  duplicateSession,
  ensureMemoryTree,
  loadConfigCatalogs,
  loadGoogleVoiceModels,
  loadHarnessCatalogs,
  loadServerConfig,
  refreshMemoryTree,
  refreshSessionList,
  refreshVisuals,
  renameSession,
  renameVisualization,
  saveServerConfig,
  scheduleListRefresh,
  scheduleVisualsRefresh,
} from './catalogService';
export { uploadFile, sharedFileText, sharedTextText, type UploadOptions, type UploadProgress } from './uploads';
export { castVisualization, probeAudioModels, probeBackendCapabilities, probeCast } from './capabilitiesProbe';
export { shareFile, shareText } from './share';
