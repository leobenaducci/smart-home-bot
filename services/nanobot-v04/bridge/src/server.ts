/**
 * WebSocket server for Python-Node.js bridge communication.
 * Security: binds to 127.0.0.1 only; requires token auth; rejects browser Origin headers.
 */

import { WebSocketServer, WebSocket } from 'ws';
import { WhatsAppClient, InboundMessage } from './whatsapp.js';

interface SendCommand {
  type: 'send';
  to: string;
  text: string;
}

interface SendMediaCommand {
  type: 'send_media';
  to: string;
  filePath: string;
  mimetype: string;
  caption?: string;
  fileName?: string;
}

type BridgeCommand = SendCommand | SendMediaCommand;

interface BridgeMessage {
  type: 'message' | 'status' | 'qr' | 'error';
  [key: string]: unknown;
}

export class BridgeServer {
  private wss: WebSocketServer | null = null;
  private wa: WhatsAppClient | null = null;
  private clients: Set<WebSocket> = new Set();

  constructor(private port: number, private authDir: string, private token: string,
              private host: string = '127.0.0.1') {}

  async start(): Promise<void> {
    // resolveToken() in index.ts cannot return an empty string, so this is a
    // backstop on the invariant rather than on the env var it used to name:
    // an unauthenticated bridge would accept any local process as nanobot.
    if (!this.token.trim()) {
      throw new Error(`bridge token is empty (looked in ${this.authDir})`);
    }

    // Bind to localhost only — never expose to external network
    this.wss = new WebSocketServer({
      host: this.host,
      port: this.port,
      verifyClient: (info, done) => {
        const origin = info.origin || info.req.headers.origin;
        if (origin) {
          console.warn(`Rejected WebSocket connection with Origin header: ${origin}`);
          done(false, 403, 'Browser-originated WebSocket connections are not allowed');
          return;
        }
        done(true);
      },
    });
    console.log(`🌉 Bridge server listening on ws://${this.host}:${this.port}`);
    console.log('🔒 Token authentication enabled');

    // Initialize WhatsApp client
    this.wa = new WhatsAppClient({
      authDir: this.authDir,
      onMessage: (msg) => this.broadcast({ type: 'message', ...msg }),
      onQR: (qr) => this.broadcast({ type: 'qr', qr }),
      onStatus: (status) => this.broadcast({ type: 'status', status }),
    });

    // Handle WebSocket connections
    this.wss.on('connection', (ws) => {
      // Require auth handshake as first message
      const timeout = setTimeout(() => ws.close(4001, 'Auth timeout'), 5000);
      ws.once('message', (data) => {
        clearTimeout(timeout);
        try {
          const msg = JSON.parse(data.toString());
          if (msg.type === 'auth' && msg.token === this.token) {
            console.log('🔗 Python client authenticated');
            this.setupClient(ws);
          } else {
            // The one failure mode that survives a self-provisioning token is
            // the two sides reading different files, and it is invisible from
            // here: the bridge stays up, the client retries every 5s forever,
            // and nothing crashes. Name the directory so the log says where to
            // look — this used to be a startup error that named the env var.
            console.warn(
              `Rejected auth: token mismatch. This bridge read its token from ${this.authDir}; ` +
                'nanobot must resolve the same bridge-token file (or both must set BRIDGE_TOKEN).'
            );
            ws.close(4003, 'Invalid token');
          }
        } catch {
          ws.close(4003, 'Invalid auth message');
        }
      });
    });

    // Connect to WhatsApp
    await this.wa.connect();
  }

  private setupClient(ws: WebSocket): void {
    this.clients.add(ws);

    ws.on('message', async (data) => {
      try {
        const cmd = JSON.parse(data.toString()) as BridgeCommand;
        await this.handleCommand(cmd);
        ws.send(JSON.stringify({ type: 'sent', to: cmd.to }));
      } catch (error) {
        console.error('Error handling command:', error);
        ws.send(JSON.stringify({ type: 'error', error: String(error) }));
      }
    });

    ws.on('close', () => {
      console.log('🔌 Python client disconnected');
      this.clients.delete(ws);
    });

    ws.on('error', (error) => {
      console.error('WebSocket error:', error);
      this.clients.delete(ws);
    });
  }

  private async handleCommand(cmd: BridgeCommand): Promise<void> {
    if (!this.wa) return;

    if (cmd.type === 'send') {
      await this.wa.sendMessage(cmd.to, cmd.text);
    } else if (cmd.type === 'send_media') {
      await this.wa.sendMedia(cmd.to, cmd.filePath, cmd.mimetype, cmd.caption, cmd.fileName);
    }
  }

  private broadcast(msg: BridgeMessage): void {
    const data = JSON.stringify(msg);
    for (const client of this.clients) {
      if (client.readyState === WebSocket.OPEN) {
        client.send(data);
      }
    }
  }

  async stop(): Promise<void> {
    // Close all client connections
    for (const client of this.clients) {
      client.close();
    }
    this.clients.clear();

    // Close WebSocket server
    if (this.wss) {
      this.wss.close();
      this.wss = null;
    }

    // Disconnect WhatsApp
    if (this.wa) {
      await this.wa.disconnect();
      this.wa = null;
    }
  }
}
