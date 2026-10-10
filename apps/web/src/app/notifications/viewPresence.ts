/**
 * Which session another Archie page (browser tab or window) of this device is showing, so a
 * background page doesn't notify about a session the user is looking at in a different page
 * (spec 12 TURN-2). Each page writes `{page, localId, at}` to localStorage when it starts or
 * stops viewing a session (and every 20 s while it does); a reader trusts entries younger than
 * 45 s from another page. A page only ever clears its own entry, so a blur racing the next
 * page's focus never erases that page's claim. localStorage is shared by every page of the
 * origin and works on Safari 12 (all access goes through the try/catch wrapper).
 */
import { generateUUID, localStore } from '@/platform';

export const VIEWING_KEY = 'archie.notify.viewing';
export const VIEWING_REFRESH_MS = 20_000;
export const VIEWING_TTL_MS = 45_000;

interface ViewingEntry {
  page: string;
  localId: string | null;
  at: number;
}

function read(): ViewingEntry | null {
  const v = localStore.getJSON<unknown>(VIEWING_KEY, null);
  if (!v || typeof v !== 'object') return null;
  const o = v as Record<string, unknown>;
  if (typeof o.page !== 'string' || typeof o.at !== 'number') return null;
  return { page: o.page, localId: typeof o.localId === 'string' ? o.localId : null, at: o.at };
}

export class ViewPresence {
  private published: string | null = null;
  private publishedAt = 0;

  constructor(
    readonly page: string = generateUUID(),
    private readonly now: () => number = Date.now,
  ) {}

  /** This page now views `localId` (null = nothing): publish on change, refresh while viewing. */
  update(localId: string | null): void {
    const t = this.now();
    if (localId) {
      if (localId === this.published && t - this.publishedAt < VIEWING_REFRESH_MS) return;
      localStore.setJSON(VIEWING_KEY, { page: this.page, localId, at: t });
      this.published = localId;
      this.publishedAt = t;
      return;
    }
    if (this.published === null) return;
    this.published = null;
    if (read()?.page === this.page) localStore.setJSON(VIEWING_KEY, { page: this.page, localId: null, at: t });
  }

  /** Another page of this device is showing `localId` right now. */
  viewedElsewhere(localId: string): boolean {
    const e = read();
    return !!e && e.page !== this.page && e.localId === localId && this.now() - e.at < VIEWING_TTL_MS;
  }
}
