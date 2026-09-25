/**
 * WhatsApp client wrapper using Baileys.
 * Based on OpenClaw's working implementation.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  downloadMediaMessage,
  extractMessageContent as baileysExtractMessageContent,
} from '@whiskeysockets/baileys';

import { Boom } from '@hapi/boom';
import qrcode from 'qrcode-terminal';
import pino from 'pino';
import { readFile, writeFile, mkdir, rm } from 'fs/promises';
import { join, basename } from 'path';
import { existsSync } from 'fs';
import { randomBytes } from 'crypto';

const VERSION = '0.1.0';

/**
 * Baileys types the protocol version as a fixed 3-tuple, not an array, and
 * `makeWASocket` will not take a `number[]`. Worth a named type rather than a
 * cast at the call site: everything here — the pinned env var, the remembered
 * file, the fetched value — arrives as something looser and has to be checked
 * into this shape, and `toVersion` is the one place that check lives.
 */
type WAVersion = [number, number, number];

function toVersion(value: unknown): WAVersion | null {
  if (!Array.isArray(value) || value.length !== 3) return null;
  const parts = value.map((n) => (typeof n === 'string' ? parseInt(n, 10) : n));
  if (!parts.every((n) => typeof n === 'number' && Number.isFinite(n))) return null;
  return [parts[0], parts[1], parts[2]] as WAVersion;
}

export interface InboundMessage {
  id: string;
  sender: string;
  pn: string;
  content: string;
  timestamp: number;
  isGroup: boolean;
  /** Display name for the conversation: the group's subject, or the contact's
   *  own pushName. Without it every chat shows as a raw JID — and JIDs are
   *  increasingly LIDs (`266163485409350@lid`), WhatsApp's privacy-preserving
   *  id, which is not a phone number and not recognisable as anybody. */
  chatName?: string;
  /** Who *wrote* this message, as they call themselves. Distinct from
   *  chatName: in a group that is the group's subject, and the writer is one
   *  of many. Alfred answers the person who addressed him, so he needs the
   *  person, not the room. Empty on our own messages. */
  senderName?: string;
  /** True when the linked account itself wrote this — WhatsApp's own assertion
   *  of identity, and how the Python side knows the owner from everyone else. */
  fromMe?: boolean;
  wasMentioned?: boolean;
  media?: string[];
}

export interface WhatsAppClientOptions {
  authDir: string;
  onMessage: (msg: InboundMessage) => void;
  onQR: (qr: string) => void;
  onStatus: (status: string) => void;
}

export class WhatsAppClient {
  private sock: any = null;
  private options: WhatsAppClientOptions;
  private reconnecting = false;
  /** Pending reconnect, so disconnect() can call it off. */
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  /** Set by disconnect(): a scheduled reconnect must not resurrect a socket
   *  somebody closed on purpose. */
  private stopped = false;
  /** Consecutive failed attempts, for the backoff. Reset when a connection
   *  actually opens — not when one is merely attempted. */
  private attempts = 0;
  /** jid -> group subject. groupMetadata is a network round trip and a rate
   *  limit, and a group's name changes about never. */
  private groupNames = new Map<string, string>();

  /** A group's name, or '' if it cannot be had right now. Never throws: a
   *  missing name must cost the label, not the message. */
  private async groupSubject(jid: string): Promise<string> {
    const cached = this.groupNames.get(jid);
    if (cached !== undefined) return cached;
    try {
      const meta = await this.sock.groupMetadata(jid);
      const subject = meta?.subject || '';
      if (subject) this.groupNames.set(jid, subject);
      return subject;
    } catch {
      return '';
    }
  }

  constructor(options: WhatsAppClientOptions) {
    this.options = options;
  }

  private normalizeJid(jid: string | undefined | null): string {
    return (jid || '').split(':')[0];
  }

  private wasMentioned(msg: any): boolean {
    if (!msg?.key?.remoteJid?.endsWith('@g.us')) return false;

    const candidates = [
      msg?.message?.extendedTextMessage?.contextInfo?.mentionedJid,
      msg?.message?.imageMessage?.contextInfo?.mentionedJid,
      msg?.message?.videoMessage?.contextInfo?.mentionedJid,
      msg?.message?.documentMessage?.contextInfo?.mentionedJid,
      msg?.message?.audioMessage?.contextInfo?.mentionedJid,
    ];
    const mentioned = candidates.flatMap((items) => (Array.isArray(items) ? items : []));
    if (mentioned.length === 0) return false;

    const selfIds = new Set(
      [this.sock?.user?.id, this.sock?.user?.lid, this.sock?.user?.jid]
        .map((jid) => this.normalizeJid(jid))
        .filter(Boolean),
    );
    return mentioned.some((jid: string) => selfIds.has(this.normalizeJid(jid)));
  }

  /**
   * Which WhatsApp Web protocol version to speak.
   *
   * `fetchLatestBaileysVersion()` asks a remote endpoint what WhatsApp is
   * currently running and, when that call fails, quietly answers with the
   * version bundled into the library instead. That default goes stale, and
   * WhatsApp rejects a stale one with **405** — which is how a transient
   * network blip turned into a two-hour outage on 2026-08-17 that read like a
   * WhatsApp or account problem. The only clue was the version in the log going
   * *backwards* (2.3000.1043857760 → 2.3000.1027934701) between the connection
   * that worked and the 35 that did not.
   *
   * So: remember the last version that actually connected, and prefer it over
   * the library's default when the fetch fails. It will eventually go stale
   * too, but a version that worked an hour ago is a better guess than one
   * frozen at release, and the failure says so out loud instead of retrying in
   * silence.
   *
   * Written only from `markVersionWorking`, after a connection opens — not
   * here. A version that merely *fetched* has proved nothing.
   */
  private versionPath(): string {
    return join(this.options.authDir, 'wa-version.json');
  }

  /** Set when WhatsApp answers 405: the version we offered was refused. */
  private versionWasRefused = false;

  private async rememberedVersion(): Promise<WAVersion | null> {
    try {
      const raw = JSON.parse(await readFile(this.versionPath(), 'utf-8'));
      return toVersion(raw?.version);
    } catch {
      // Never recorded one, which is only true before the first success.
      return null;
    }
  }

  private async resolveVersion(): Promise<WAVersion> {
    // Manual escape hatch, highest precedence. For the case this whole helper
    // exists for: the fetch is answering confidently with a version WhatsApp
    // will not accept, and somebody needs to pin a known-good one right now
    // without waiting for a library release. WA_VERSION=2.3000.1043857760
    const pinned = (process.env.WA_VERSION || '').trim();
    if (pinned) {
      const parsed = toVersion(pinned.split('.'));
      if (parsed) {
        console.log(`Using pinned WhatsApp version ${pinned} (WA_VERSION)`);
        return parsed;
      }
      console.error(`WA_VERSION is not a three-part version: ${pinned}`);
    }

    // A 405 last time means the version we used was refused. Prefer the one
    // that is known to have worked, if it is a different one — reacting to the
    // rejection rather than guessing up front, because the fetch can fail
    // *confidently*: on 2026-08-17 it returned a stale version as though it
    // were current, so preferring the remembered one unconditionally would
    // have been wrong on every ordinary day.
    if (this.versionWasRefused) {
      const remembered = await this.rememberedVersion();
      if (remembered) {
        console.error(
          `⚠ WhatsApp refused the last version. Retrying with ${remembered.join('.')}, ` +
          `which connected before.`);
        return remembered;
      }
    }

    let fetched: WAVersion | null = null;
    let isLatest = false;
    try {
      const res: any = await fetchLatestBaileysVersion();
      fetched = toVersion(res?.version);
      if (fetched) isLatest = !!res.isLatest;
      if (res?.error) {
        console.error('Could not fetch the current WhatsApp version:', res.error?.message ?? res.error);
      }
    } catch (err) {
      console.error('Could not fetch the current WhatsApp version:', err);
    }

    if (fetched && isLatest) return fetched;

    const remembered = await this.rememberedVersion();

    if (remembered) {
      console.error(
        `⚠ Using the last version that connected (${remembered.join('.')}) — the ` +
        `current one could not be fetched. If this keeps failing with 405, ` +
        `WhatsApp has moved on and the bridge needs a newer Baileys.`);
      return remembered;
    }
    if (fetched) {
      console.error(
        `⚠ Falling back to the library's bundled version (${fetched.join('.')}). ` +
        `Nothing has connected yet, so there is no better guess. A 405 from here ` +
        `means this default is stale.`);
      return fetched;
    }
    throw new Error('no WhatsApp version available: fetch failed and nothing was remembered');
  }

  private async markVersionWorking(version: WAVersion): Promise<void> {
    try {
      await mkdir(this.options.authDir, { recursive: true });
      await writeFile(this.versionPath(), JSON.stringify({ version }), 'utf-8');
    } catch {
      // Remembering is an optimisation; failing to is not worth a reconnect.
    }
  }

  async connect(): Promise<void> {
    const logger = pino({ level: 'silent' });
    const { state, saveCreds } = await useMultiFileAuthState(this.options.authDir);
    const version = await this.resolveVersion();

    console.log(`Using Baileys version: ${version.join('.')}`);

    // Create socket following OpenClaw's pattern
    this.sock = makeWASocket({
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, logger),
      },
      version,
      logger,
      printQRInTerminal: false,
      browser: ['nanobot', 'cli', VERSION],
      syncFullHistory: false,
      markOnlineOnConnect: false,
    });

    // Handle WebSocket errors
    if (this.sock.ws && typeof this.sock.ws.on === 'function') {
      this.sock.ws.on('error', (err: Error) => {
        console.error('WebSocket error:', err.message);
      });
    }

    // Handle connection updates
    this.sock.ev.on('connection.update', async (update: any) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        // Display QR code in terminal
        console.log('\n📱 Scan this QR code with WhatsApp (Linked Devices):\n');
        qrcode.generate(qr, { small: true });
        this.options.onQR(qr);
      }

      if (connection === 'close') {
        const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
        const loggedOut = statusCode === DisconnectReason.loggedOut;
        // 515. Not a failure at all: WhatsApp closes the socket the moment a
        // pairing completes and expects the device straight back on a new one.
        const restartRequired = statusCode === DisconnectReason.restartRequired;

        console.log(`Connection closed. Status: ${statusCode}, Logged out: ${loggedOut}`);
        // 405 is WhatsApp refusing the protocol version, not a network or auth
        // problem — the one failure a different version can fix.
        if (statusCode === 405) this.versionWasRefused = true;
        this.options.onStatus('disconnected');

        if (loggedOut) {
          // A 401 means these credentials will never work again: the phone
          // unlinked the device, or the pairing was dropped. Reconnecting with
          // them is a loop, which is why this used to stop here — but stopping
          // *and keeping them* is a dead end, and it was reached: the file left
          // behind is what `wa_linked()` reads on the admin page, so the portal
          // reported a paired bridge, offered no code, and the only way back
          // was a shell.
          //
          // Clear, then reconnect — in that order. `resetAuth()` is awaited
          // rather than fired off, because `connect()` re-reads the directory
          // and a reconnect that overtakes the delete comes back holding the
          // credentials that were just rejected.
          console.log('Credentials were rejected; clearing them and starting a fresh pairing.');
          if (!this.stopped) {
            void this.resetAuth().then(() => {
              if (!this.reconnecting && !this.stopped) this.scheduleReconnect(2000);
            });
          }
        } else if (restartRequired) {
          // Come back at once, and do not count this as a failed attempt.
          //
          // This is what broke pairing. The backoff treated 515 like any other
          // drop, so after a few timed-out QR codes the delay had climbed to a
          // minute — and the device that had *just* paired went missing for
          // that minute. WhatsApp dropped the session, the next connection got
          // 401, and the log read as "linked, then immediately unlinked".
          // Every scan died in that gap. Observed twice, at attempts 5 and 6.
          this.attempts = 0;
          if (!this.reconnecting && !this.stopped) this.scheduleReconnect(0);
        } else if (!this.reconnecting && !this.stopped) {
          this.scheduleReconnect();
        }
      } else if (connection === 'open') {
        console.log('✅ Connected to WhatsApp');
        this.attempts = 0;
        this.versionWasRefused = false;
        // Only now is this version known to work — recorded here rather than
        // where it was fetched, so a version that merely downloaded is never
        // mistaken for one WhatsApp accepted.
        void this.markVersionWorking(version);
        this.options.onStatus('connected');
      }
    });

    // Save credentials on update
    this.sock.ev.on('creds.update', saveCreds);

    // Handle incoming messages
    this.sock.ev.on('messages.upsert', async ({ messages, type }: { messages: any[]; type: string }) => {
      if (type !== 'notify') return;

      for (const msg of messages) {
        // `fromMe` used to be skipped outright, which is the obvious rule for a
        // bot answering strangers and exactly wrong for an assistant reading its
        // owner's account: it made the account holder the one person who could
        // not address Alfred. He typed "alfred ..." and nothing was stored, no
        // turn ran, and no reply came, with nothing in any log to say why.
        //
        // Forwarded and tagged instead. `fromMe` is WhatsApp's own assertion
        // that this is the linked account talking, which is a far better
        // identity signal than a number in a config file: nothing else can
        // claim it. The one thing that must not come back is what we ourselves
        // sent, or Alfred answers his own replies.
        if (msg.key.fromMe && this.sentIds.includes(msg.key.id || '')) continue;
        if (msg.key.remoteJid === 'status@broadcast') continue;

        const unwrapped = baileysExtractMessageContent(msg.message);
        if (!unwrapped) continue;

        const content = this.getTextContent(unwrapped);
        let fallbackContent: string | null = null;
        const mediaPaths: string[] = [];

        if (unwrapped.imageMessage) {
          fallbackContent = '[Image]';
          const path = await this.downloadMedia(msg, unwrapped.imageMessage.mimetype ?? undefined);
          if (path) mediaPaths.push(path);
        } else if (unwrapped.documentMessage) {
          fallbackContent = '[Document]';
          const path = await this.downloadMedia(msg, unwrapped.documentMessage.mimetype ?? undefined,
            unwrapped.documentMessage.fileName ?? undefined);
          if (path) mediaPaths.push(path);
        } else if (unwrapped.videoMessage) {
          fallbackContent = '[Video]';
          const path = await this.downloadMedia(msg, unwrapped.videoMessage.mimetype ?? undefined);
          if (path) mediaPaths.push(path);
        }

        const finalContent = content || (mediaPaths.length === 0 ? fallbackContent : '') || '';
        if (!finalContent && mediaPaths.length === 0) continue;

        const isGroup = msg.key.remoteJid?.endsWith('@g.us') || false;
        const wasMentioned = this.wasMentioned(msg);

        // pushName is whoever *wrote* the message, so it names the chat only
        // for an incoming one-to-one. On a message we sent it is our own name,
        // which would label Jana's chat "Alex". Left empty in that case: the
        // store keeps the name it already had rather than overwriting it with
        // nothing.
        const chatName = isGroup
          ? await this.groupSubject(msg.key.remoteJid)
          : (msg.key.fromMe ? '' : (msg.pushName || ''));

        this.options.onMessage({
          id: msg.key.id || '',
          sender: msg.key.remoteJid || '',
          pn: msg.key.remoteJidAlt || '',
          content: finalContent,
          timestamp: msg.messageTimestamp as number,
          isGroup,
          chatName,
          senderName: msg.key.fromMe ? '' : (msg.pushName || ''),
          fromMe: !!msg.key.fromMe,
          ...(isGroup ? { wasMentioned } : {}),
          ...(mediaPaths.length > 0 ? { media: mediaPaths } : {}),
        });
      }
    });
  }

  private async downloadMedia(msg: any, mimetype?: string, fileName?: string): Promise<string | null> {
    try {
      const mediaDir = join(this.options.authDir, '..', 'media');
      await mkdir(mediaDir, { recursive: true });

      const buffer = await downloadMediaMessage(msg, 'buffer', {}) as Buffer;

      let outFilename: string;
      if (fileName) {
        // Documents have a filename — use it with a unique prefix to avoid collisions
        const prefix = `wa_${Date.now()}_${randomBytes(4).toString('hex')}_`;
        outFilename = prefix + fileName;
      } else {
        const mime = mimetype || 'application/octet-stream';
        // Derive extension from mimetype subtype (e.g. "image/png" → ".png", "application/pdf" → ".pdf")
        const ext = '.' + (mime.split('/').pop()?.split(';')[0] || 'bin');
        outFilename = `wa_${Date.now()}_${randomBytes(4).toString('hex')}${ext}`;
      }

      const filepath = join(mediaDir, outFilename);
      await writeFile(filepath, buffer);

      return filepath;
    } catch (err) {
      console.error('Failed to download media:', err);
      return null;
    }
  }

  private getTextContent(message: any): string | null {
    // Text message
    if (message.conversation) {
      return message.conversation;
    }

    // Extended text (reply, link preview)
    if (message.extendedTextMessage?.text) {
      return message.extendedTextMessage.text;
    }

    // Image with optional caption
    if (message.imageMessage) {
      return message.imageMessage.caption || '';
    }

    // Video with optional caption
    if (message.videoMessage) {
      return message.videoMessage.caption || '';
    }

    // Document with optional caption
    if (message.documentMessage) {
      return message.documentMessage.caption || '';
    }

    // Voice/Audio message
    if (message.audioMessage) {
      return `[Voice Message]`;
    }

    return null;
  }

  /**
   * Ids of messages this bridge sent. `fromMe` is now forwarded rather than
   * dropped, so without this Alfred's own reply comes straight back as input —
   * and a reply that happens to contain his name would answer itself, forever.
   * Bounded, because only the last few seconds can possibly echo back.
   */
  private sentIds: string[] = [];

  private remember(id?: string | null): void {
    if (!id) return;
    this.sentIds.push(id);
    if (this.sentIds.length > 200) this.sentIds.splice(0, this.sentIds.length - 200);
  }

  async sendMessage(to: string, text: string): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
    }

    const sent = await this.sock.sendMessage(to, { text });
    this.remember(sent?.key?.id);
  }

  async sendMedia(
    to: string,
    filePath: string,
    mimetype: string,
    caption?: string,
    fileName?: string,
  ): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
    }

    const buffer = await readFile(filePath);
    const category = mimetype.split('/')[0];

    if (category === 'image') {
      await this.sock.sendMessage(to, { image: buffer, caption: caption || undefined, mimetype });
    } else if (category === 'video') {
      await this.sock.sendMessage(to, { video: buffer, caption: caption || undefined, mimetype });
    } else if (category === 'audio') {
      await this.sock.sendMessage(to, { audio: buffer, mimetype });
    } else {
      const name = fileName || basename(filePath);
      await this.sock.sendMessage(to, { document: buffer, mimetype, fileName: name });
    }
  }

  /**
   * Queue another connect attempt.
   *
   * Three things this fixes, all of which only start to matter now that the
   * bridge runs as its own long-lived container rather than a process somebody
   * started to scan a QR code:
   *
   * 1. `connect()` returns a promise, and the old timer neither awaited nor
   *    caught it. Under Node 20 an unhandled rejection terminates the process
   *    by default — so a transient DNS failure five seconds after a disconnect
   *    killed the bridge outright, and WhatsApp went quiet until somebody
   *    noticed. Caught, it is just another failed attempt.
   * 2. Retrying at a flat five seconds forever hammers a network that is down.
   *    Backing off to a minute costs nothing when the outage is brief and stops
   *    a busy loop when it is not.
   * 3. The timer survived `disconnect()`, so an intentional shutdown was
   *    followed by a reconnect five seconds later.
   */
  /** Throw away a credential set WhatsApp has rejected.

   * Everything except `bridge-token`, which is this bridge's own handle for
   * the websocket and has nothing to do with the WhatsApp session -- removing
   * it would make the assistant unable to talk to its own bridge on top of
   * being unpaired.
   */
  private async resetAuth(): Promise<void> {
    try {
      const { readdir } = await import('fs/promises');
      for (const entry of await readdir(this.options.authDir)) {
        if (entry === 'bridge-token') continue;
        await rm(join(this.options.authDir, entry), { recursive: true, force: true });
      }
      this.attempts = 0;          // a fresh pairing is not a failed retry
    } catch (err) {
      console.error('Could not clear the rejected credentials:', err);
    }
  }

  private scheduleReconnect(delayOverride?: number): void {
    if (this.stopped) return;
    this.reconnecting = true;
    // Backing off to a minute is right for a paired bridge that lost the
    // network. It is wrong for one nobody has paired yet: a QR expires in
    // seconds, so the person holding the phone is left looking at a dead code
    // with no replacement coming for a minute, which is indistinguishable from
    // pairing being broken. While there are no credentials, somebody is
    // standing there — come back fast enough to draw the next code.
    const ceiling = existsSync(join(this.options.authDir, 'creds.json')) ? 60000 : 10000;
    const delay = delayOverride ?? Math.min(5000 * 2 ** this.attempts, ceiling);
    // An override is a deliberate schedule, not a retry, so it must not push
    // the backoff up for the attempts that follow it.
    if (delayOverride === undefined) this.attempts += 1;
    console.log(`Reconnecting in ${Math.round(delay / 1000)}s (attempt ${this.attempts})...`);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.reconnecting = false;
      if (this.stopped) return;
      this.connect().catch((err) => {
        console.error('Reconnect failed:', err);
        // Never rethrow from here: there is no caller left to handle it, and an
        // unhandled rejection is the process.
        if (!this.reconnecting) this.scheduleReconnect();
      });
    }, delay);
  }

  async disconnect(): Promise<void> {
    this.stopped = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.reconnecting = false;
    if (this.sock) {
      this.sock.end(undefined);
      this.sock = null;
    }
  }
}
