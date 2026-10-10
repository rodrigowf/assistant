/** `api.*`: one typed function per REST endpoint, grouped by domain (spec 13 §3.4). */
import { accounts, env } from './accounts';
import { auth } from './auth';
import { catalogs, config } from './config';
import { debug } from './debug';
import { memory } from './memory';
import { sessions } from './sessions';
import { visuals } from './visuals';
import { voice } from './voice';

export type { VoiceTarget } from './voice';

export const api = { sessions, config, catalogs, voice, memory, visuals, auth, accounts, env, debug };
