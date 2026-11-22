#!/usr/bin/env node
/**
 * nanobot WhatsApp Bridge
 * 
 * This bridge connects WhatsApp Web to nanobot's Python backend
 * via WebSocket. It handles authentication, message forwarding,
 * and reconnection logic.
 * 
 * Usage:
 *   npm run build && npm start
 *   
 * Or with custom settings (AUTH_DIR must be an absolute path — node does not
 * expand `~`, and a literal one becomes a directory named `~` under the cwd,
 * where this process would then invent a second bridge token that nanobot
 * never reads):
 *   BRIDGE_PORT=3001 AUTH_DIR="$HOME/.nanobot/whatsapp-auth" npm start
 */

// Polyfill crypto for Baileys in ESM.
//
// Note this cannot do what it says: ESM hoists every import and evaluates it
// before any module-body statement, so './server.js' and Baileys behind it are
// already loaded by the time the guard runs. It is harmless because Node 20
// (what the Dockerfile installs) defines globalThis.crypto anyway. A real
// polyfill would have to live in its own module imported first.
import { webcrypto, randomBytes } from 'crypto';
if (!globalThis.crypto) {
  (globalThis as any).crypto = webcrypto;
}

import { BridgeServer } from './server.js';
import { homedir } from 'os';
import { join } from 'path';
import { chmodSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'fs';

const PORT = parseInt(process.env.BRIDGE_PORT || '3001', 10);
/**
 * Interface to bind. Defaults to loopback, which is upstream's posture and the
 * right one when nanobot spawns this process beside itself.
 *
 * It has to be settable because the alternative was worse. Running the bridge
 * as its own container, loopback meant sharing the agent's network namespace
 * (`network_mode: "service:"`), and a joined namespace dies with its owner:
 * when nanobot-user2 restarted, this process was left holding a torn-down
 * namespace and every lookup failed with EAI_AGAIN — 134 reconnects against a
 * DNS server that no longer existed, while the same name resolved fine from
 * the container that owned the namespace. A restart of the agent must not
 * quietly cost the bridge its network.
 *
 * On a compose network with no published ports, 0.0.0.0 means "the other
 * containers on this network", not the LAN. That is a real widening and worth
 * naming: user1 and user3-5 can reach the socket. They cannot use it — the
 * token lives on user2's volume and nowhere else.
 */
const HOST = process.env.BRIDGE_HOST || '127.0.0.1';
const AUTH_DIR = process.env.AUTH_DIR || join(homedir(), '.nanobot', 'whatsapp-auth');

/**
 * The shared secret for the local WebSocket, read or invented.
 *
 * Upstream required BRIDGE_TOKEN in the environment because it assumed nanobot
 * *spawns* this process and hands it one — which is what `channels login` does.
 * This house runs the bridge as its own container instead, so nobody was there
 * to provision it, and the secret had to become a deploy credential: one more
 * thing to create, and one whose absence would have been invisible.
 *
 * "Would have been": the restart loop that prompted this was not the token at
 * all. The service inherited the image's entrypoint and was running `nanobot
 * node dist/index.js`, so this file never executed. The compose comment
 * predicted a missing-token loop, a loop appeared, and the prediction got
 * believed. Provisioning our own secret is still worth having — it is one less
 * credential — but it fixed nothing that was broken at the time.
 *
 * Both processes already share AUTH_DIR — it is the volume holding the linked
 * WhatsApp session — and the Python side already reads or creates exactly this
 * file (`_load_or_create_bridge_token`). An explicit BRIDGE_TOKEN still wins,
 * so nothing that sets one today changes behaviour.
 *
 * They agree by *configuration*, not by construction: Python derives the
 * directory from `get_config_path().parent`, this side from AUTH_DIR (or $HOME).
 * Compose pins both to /home/nanobot/.nanobot/whatsapp-auth. Point either one
 * somewhere else — `nanobot --config elsewhere`, a different AUTH_DIR — and the
 * two invent different secrets and the handshake fails, so server.ts says which
 * directory it read when it rejects one.
 *
 * `wx` (exclusive) keeps two starters from each inventing a secret, but it is
 * open() then write(): the name exists before the bytes do. So losing the race
 * means waiting for the winner's bytes rather than reading whatever is there —
 * and a file still empty after that wait is a corpse, from a write that was
 * killed or ran out of disk. We take it over. Refusing to, which is what the
 * exclusive flag does on its own, is an exit(1) on every restart forever over a
 * zero-byte file; the Python side heals that same state in one pass.
 */
function resolveToken(): string {
  const fromEnv = process.env.BRIDGE_TOKEN?.trim();
  if (fromEnv) return fromEnv;

  const tokenPath = join(AUTH_DIR, 'bridge-token');
  const read = (): string => {
    try {
      return readFileSync(tokenPath, 'utf-8').trim();
    } catch {
      return '';
    }
  };

  const existing = read();
  if (existing) return existing;

  // 0o700 because this directory also holds the Baileys session — the linked
  // device itself. mkdir's mode applies only to directories it actually
  // creates, and this one already exists everywhere it matters, so the chmod is
  // the part that does the work; it is what SECURITY.md tells people to run by
  // hand. Failing it just means we do not own the directory, which is somebody
  // else's decision to have made.
  //
  // This does tighten a deliberately group-shared directory the first time it
  // creates a token. Fine here — compose runs both containers as the same
  // ${UID}:${GID} — but a deployment that split them across two uids sharing a
  // group would want 0o750 and to be told so, rather than watching its
  // permissions revert.
  mkdirSync(AUTH_DIR, { recursive: true, mode: 0o700 });
  try {
    chmodSync(AUTH_DIR, 0o700);
  } catch {
    // Not ours to tighten.
  }
  const token = randomBytes(32).toString('base64url');
  try {
    writeFileSync(tokenPath, token, { encoding: 'utf-8', mode: 0o600, flag: 'wx' });
    return token;
  } catch (err) {
    // Anything other than "somebody got here first" keeps its own errno. A
    // read-only volume or the wrong uid is the likeliest failure here, and
    // re-reading would report it as ENOENT — a missing file, for a permissions
    // problem.
    if ((err as NodeJS.ErrnoException).code !== 'EEXIST') throw err;
  }

  const idle = new Int32Array(new SharedArrayBuffer(4));
  for (let i = 0; i < 20; i++) {
    const settled = read();
    if (settled) return settled;
    Atomics.wait(idle, 0, 0, 25);
  }

  // A corpse. Take it over through a staged rename, because a plain write would
  // re-open the very zero-byte window we just spent half a second waiting out.
  // Then read back instead of trusting our own bytes: if nanobot adopted the
  // same corpse in the same moment, the file is the only value the two of us
  // can still agree on.
  const staging = `${tokenPath}.${randomBytes(6).toString('hex')}.tmp`;
  try {
    writeFileSync(staging, token, { encoding: 'utf-8', mode: 0o600 });
    renameSync(staging, tokenPath);
  } finally {
    rmSync(staging, { force: true });
  }

  Atomics.wait(idle, 0, 0, 25);
  return read() || token;
}

let TOKEN: string;
try {
  TOKEN = resolveToken();
} catch (err) {
  console.error(`Could not read or create the bridge token in ${AUTH_DIR}:`, err);
  process.exit(1);
}

console.log('🐈 nanobot WhatsApp Bridge');
console.log('========================\n');

const server = new BridgeServer(PORT, AUTH_DIR, TOKEN, HOST);

// Handle graceful shutdown
process.on('SIGINT', async () => {
  console.log('\n\nShutting down...');
  await server.stop();
  process.exit(0);
});

process.on('SIGTERM', async () => {
  await server.stop();
  process.exit(0);
});

// Start the server
server.start().catch((error) => {
  console.error('Failed to start bridge:', error);
  process.exit(1);
});
