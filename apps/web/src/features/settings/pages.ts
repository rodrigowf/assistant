/**
 * The IA §7 settings hierarchy on web (fixes R5). Android-only pages (Connection, Audio, Wake
 * word) are not rendered here; remote logging lives under About (IA §7, mockup (e) caption "On web,
 * This device lists only Appearance"; Notifications joined it for the agent-finished notices).
 */
import type { IconName } from '@/ui/primitives';

export type SettingsPageId =
  | 'appearance'
  | 'notifications'
  | 'conversation-model'
  | 'voice'
  | 'voice-tuning'
  | 'agent-sessions'
  | 'working-directories'
  | 'mcp-servers'
  | 'account'
  | 'about';

export type SettingsGroupId = 'device' | 'server' | 'about';

export interface SettingsPageDef {
  readonly id: SettingsPageId;
  readonly title: string;
  readonly icon: IconName;
  readonly group: SettingsGroupId;
}

export const SETTINGS_PAGES: readonly SettingsPageDef[] = [
  { id: 'appearance', title: 'Appearance', icon: 'palette', group: 'device' },
  { id: 'notifications', title: 'Notifications', icon: 'notifications', group: 'device' },
  { id: 'conversation-model', title: 'Conversation model', icon: 'forum', group: 'server' },
  { id: 'voice', title: 'Voice', icon: 'record_voice_over', group: 'server' },
  { id: 'voice-tuning', title: 'Voice tuning', icon: 'tune', group: 'server' },
  { id: 'agent-sessions', title: 'Agent sessions', icon: 'smart_toy', group: 'server' },
  { id: 'working-directories', title: 'Working directories', icon: 'folder', group: 'server' },
  { id: 'mcp-servers', title: 'MCP servers', icon: 'hub', group: 'server' },
  { id: 'account', title: 'Accounts', icon: 'account_circle', group: 'server' },
  { id: 'about', title: 'About Archie', icon: 'info', group: 'about' },
];

export const DEFAULT_PAGE: SettingsPageId = 'appearance';

export function findSettingsPage(id: string | null | undefined): SettingsPageDef | null {
  return SETTINGS_PAGES.find((p) => p.id === id) ?? null;
}

/** Title of a page id (for app bars and tab titles); null for unknown ids. */
export function settingsPageTitle(id: string | null | undefined): string | null {
  return findSettingsPage(id)?.title ?? null;
}
