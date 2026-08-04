'use strict';
/**
 * GDB MCP Bridge.
 *
 * Publishes the active VS Code debug session over a local HTTP server so an
 * out-of-process AI agent can inspect and drive it via DAP.  This exists
 * because Linux permits exactly one ptrace tracer per process: once VS Code's
 * debug adapter owns the debuggee, a second `gdb attach` cannot join it.  The
 * only way in is through the adapter that is already attached.
 *
 * The extension stays deliberately thin.  It forwards DAP requests, records
 * events, and exposes launch control; all interpretation happens on the Python
 * side.  Zero dependencies and no build step -- plain CommonJS.
 *
 * Transport is a Unix domain socket (mode 0600) on Linux/macOS, so access is
 * scoped by filesystem permissions rather than being reachable by every user on
 * the box.  A bearer token is required regardless.
 */

const vscode = require('vscode');
const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

const MAX_EVENTS = 2000;
const MAX_OUTPUT = 256 * 1024;
const MAX_BODY = 4 * 1024 * 1024;

/** @type {http.Server | null} */
let server = null;
let token = '';
let transport = 'unix';
/** @type {string | number} */
let address = '';
let discoveryFile = '';
let statusBar = null;
let output = null;

let seq = 0;
/** @type {Array<Record<string, any>>} */
const events = [];
/** @type {Array<() => void>} */
let waiters = [];

/** @type {Array<string>} */
const programOutput = [];
let programOutputLen = 0;

/** Per-session state the DAP tracker keeps up to date. */
/** @type {Map<string, {session: any, stopped: boolean, lastStop: any, threadId: number|null}>} */
const sessions = new Map();

// ---------------------------------------------------------------------------
// events
// ---------------------------------------------------------------------------

function log(message) {
  if (output) output.appendLine(`[${new Date().toISOString()}] ${message}`);
}

function pushEvent(type, body) {
  const event = Object.assign({ seq: ++seq, time: Date.now(), type }, body || {});
  events.push(event);
  if (events.length > MAX_EVENTS) events.splice(0, events.length - MAX_EVENTS);
  const pending = waiters;
  waiters = [];
  for (const resolve of pending) {
    try {
      resolve();
    } catch (_) {
      /* ignore */
    }
  }
  return event;
}

function appendProgramOutput(text) {
  programOutput.push(text);
  programOutputLen += text.length;
  while (programOutputLen > MAX_OUTPUT && programOutput.length > 1) {
    programOutputLen -= programOutput.shift().length;
  }
}

function drainProgramOutput() {
  const text = programOutput.join('');
  programOutput.length = 0;
  programOutputLen = 0;
  return text;
}

// ---------------------------------------------------------------------------
// DAP tracker
// ---------------------------------------------------------------------------

function trackerFactory() {
  return {
    createDebugAdapterTracker(session) {
      return {
        onDidSendMessage(message) {
          if (!message || message.type !== 'event') return;
          const body = message.body || {};
          const state = sessions.get(session.id);

          switch (message.event) {
            case 'stopped':
              if (state) {
                state.stopped = true;
                state.lastStop = body;
                if (typeof body.threadId === 'number') state.threadId = body.threadId;
              }
              pushEvent('stopped', {
                sessionId: session.id,
                sessionName: session.name,
                reason: body.reason,
                description: body.description,
                text: body.text,
                threadId: body.threadId,
                allThreadsStopped: body.allThreadsStopped,
                hitBreakpointIds: body.hitBreakpointIds,
              });
              break;

            case 'continued':
              if (state) state.stopped = false;
              pushEvent('continued', {
                sessionId: session.id,
                threadId: body.threadId,
                allThreadsContinued: body.allThreadsContinued,
              });
              break;

            case 'output':
              if (typeof body.output === 'string') {
                const category = body.category || 'console';
                if (category === 'stdout' || category === 'stderr') {
                  appendProgramOutput(body.output);
                }
                pushEvent('output', {
                  sessionId: session.id,
                  category,
                  output: body.output,
                });
              }
              break;

            case 'exited':
              pushEvent('exited', { sessionId: session.id, exitCode: body.exitCode });
              break;

            case 'terminated':
              if (state) state.stopped = false;
              pushEvent('terminated', { sessionId: session.id });
              break;

            case 'thread':
              pushEvent('thread', {
                sessionId: session.id,
                reason: body.reason,
                threadId: body.threadId,
              });
              break;

            case 'breakpoint':
              pushEvent('breakpoint', {
                sessionId: session.id,
                reason: body.reason,
                breakpoint: body.breakpoint,
              });
              break;

            default:
              break;
          }
        },

        onError(error) {
          pushEvent('adapterError', {
            sessionId: session.id,
            message: String((error && error.message) || error),
          });
        },

        onExit(code, signal) {
          pushEvent('adapterExit', { sessionId: session.id, code, signal });
        },
      };
    },
  };
}

// ---------------------------------------------------------------------------
// session helpers
// ---------------------------------------------------------------------------

function resolveSession(sessionId) {
  if (sessionId) {
    const state = sessions.get(sessionId);
    if (!state) throw new Error(`No debug session with id ${sessionId}`);
    return state;
  }
  const active = vscode.debug.activeDebugSession;
  if (!active) {
    throw new Error(
      'No active debug session. Start one with F5, or call /launch with a ' +
        'launch.json configuration name.'
    );
  }
  let state = sessions.get(active.id);
  if (!state) {
    state = { session: active, stopped: false, lastStop: null, threadId: null };
    sessions.set(active.id, state);
  }
  return state;
}

function describeSession(state) {
  return {
    id: state.session.id,
    name: state.session.name,
    type: state.session.type,
    workspaceFolder: state.session.workspaceFolder
      ? state.session.workspaceFolder.uri.fsPath
      : null,
    stopped: state.stopped,
    lastStop: state.lastStop,
    threadId: state.threadId,
  };
}

// ---------------------------------------------------------------------------
// route handlers
// ---------------------------------------------------------------------------

async function routeStatus() {
  const active = vscode.debug.activeDebugSession;
  const all = [];
  for (const state of sessions.values()) all.push(describeSession(state));
  return {
    ok: true,
    lastSeq: seq,
    activeSessionId: active ? active.id : null,
    sessions: all,
    workspaceFolders: (vscode.workspace.workspaceFolders || []).map((f) => ({
      name: f.name,
      path: f.uri.fsPath,
    })),
  };
}

async function routeRequest(body) {
  const command = body && body.command;
  if (!command) throw new Error('/request needs a "command" field');
  const state = resolveSession(body.sessionId);
  const response = await state.session.customRequest(command, body.args || {});
  return { ok: true, command, body: response === undefined ? null : response };
}

async function routeEvents(query) {
  const since = Number(query.get('since') || 0) || 0;
  let wait = Number(query.get('wait') || 0) || 0;
  wait = Math.max(0, Math.min(wait, 60));

  let matching = events.filter((e) => e.seq > since);
  if (matching.length === 0 && wait > 0) {
    await new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        waiters = waiters.filter((w) => w !== finish);
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(finish, wait * 1000);
      waiters.push(finish);
    });
    matching = events.filter((e) => e.seq > since);
  }
  return { ok: true, events: matching, lastSeq: seq };
}

async function routeOutput() {
  return { ok: true, output: drainProgramOutput() };
}

async function routeConfigs() {
  const workspaces = [];
  for (const folder of vscode.workspace.workspaceFolders || []) {
    const launch = vscode.workspace.getConfiguration('launch', folder.uri);
    const list = launch.get('configurations') || [];
    workspaces.push({
      folder: folder.name,
      path: folder.uri.fsPath,
      configurations: list.map((c) => ({
        name: c.name,
        type: c.type,
        request: c.request,
        program: c.program,
        preLaunchTask: c.preLaunchTask,
      })),
    });
  }
  return { ok: true, workspaces };
}

async function routeLaunch(body) {
  const folders = vscode.workspace.workspaceFolders || [];
  if (folders.length === 0) throw new Error('No workspace folder is open');
  let folder = folders[0];
  if (body && body.folder) {
    const found = folders.find(
      (f) => f.name === body.folder || f.uri.fsPath === body.folder
    );
    if (found) folder = found;
  }
  const target = (body && (body.config || body.name)) || null;
  if (!target) {
    throw new Error(
      '/launch needs "name" (a launch.json configuration name) or "config" ' +
        '(an inline configuration object)'
    );
  }

  const before = new Set(sessions.keys());
  const started = await vscode.debug.startDebugging(folder, target);
  if (!started) {
    throw new Error(
      `VS Code refused to start ${JSON.stringify(target)}. Check the ` +
        'configuration name and that its preLaunchTask succeeds.'
    );
  }

  // startDebugging resolves before onDidStartDebugSession always fires.
  let created = null;
  for (let i = 0; i < 100 && !created; i++) {
    for (const [id, state] of sessions) {
      if (!before.has(id)) {
        created = state;
        break;
      }
    }
    if (!created) await new Promise((r) => setTimeout(r, 50));
  }

  return {
    ok: true,
    started: true,
    lastSeq: seq,
    session: created ? describeSession(created) : null,
  };
}

async function routeStop(body) {
  let target;
  try {
    target = resolveSession(body && body.sessionId).session;
  } catch (_) {
    return { ok: true, stopped: false, note: 'No active debug session' };
  }
  await vscode.debug.stopDebugging(target);
  return { ok: true, stopped: true };
}

async function routeBreakpoints(method, body) {
  if (method === 'GET') {
    return {
      ok: true,
      breakpoints: vscode.debug.breakpoints.map(describeBreakpoint),
    };
  }

  const action = (body && body.action) || 'add';
  if (action === 'clear') {
    vscode.debug.removeBreakpoints(vscode.debug.breakpoints);
    return { ok: true, cleared: true };
  }

  const specs = (body && body.breakpoints) || [];
  const created = specs.map((spec) => {
    if (spec.functionName) {
      return new vscode.FunctionBreakpoint(
        spec.functionName,
        spec.enabled !== false,
        spec.condition,
        spec.hitCondition,
        spec.logMessage
      );
    }
    if (!spec.file || typeof spec.line !== 'number') {
      throw new Error(
        'each breakpoint needs either "functionName", or "file" plus "line"'
      );
    }
    const uri = vscode.Uri.file(spec.file);
    const position = new vscode.Position(Math.max(0, spec.line - 1), 0);
    return new vscode.SourceBreakpoint(
      new vscode.Location(uri, position),
      spec.enabled !== false,
      spec.condition,
      spec.hitCondition,
      spec.logMessage
    );
  });

  if (action === 'remove') {
    vscode.debug.removeBreakpoints(created);
  } else {
    vscode.debug.addBreakpoints(created);
  }
  return {
    ok: true,
    breakpoints: vscode.debug.breakpoints.map(describeBreakpoint),
  };
}

function describeBreakpoint(bp) {
  const base = {
    id: bp.id,
    enabled: bp.enabled,
    condition: bp.condition,
    hitCondition: bp.hitCondition,
    logMessage: bp.logMessage,
  };
  if (bp instanceof vscode.SourceBreakpoint) {
    base.kind = 'source';
    base.file = bp.location.uri.fsPath;
    base.line = bp.location.range.start.line + 1;
  } else if (bp instanceof vscode.FunctionBreakpoint) {
    base.kind = 'function';
    base.functionName = bp.functionName;
  } else {
    base.kind = 'other';
  }
  return base;
}

// ---------------------------------------------------------------------------
// HTTP plumbing
// ---------------------------------------------------------------------------

function sendJson(res, status, payload) {
  const text = JSON.stringify(payload);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(text),
    'Cache-Control': 'no-store',
  });
  res.end(text);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    req.on('data', (chunk) => {
      total += chunk.length;
      if (total > MAX_BODY) {
        reject(new Error('request body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (err) {
        reject(new Error(`invalid JSON body: ${err.message}`));
      }
    });
    req.on('error', reject);
  });
}

function authorized(req) {
  const header = req.headers['authorization'] || '';
  const provided = header.startsWith('Bearer ') ? header.slice(7) : '';
  const a = Buffer.from(provided);
  const b = Buffer.from(token);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

async function dispatch(req, url, body) {
  const route = url.pathname;
  switch (route) {
    case '/status':
      return routeStatus();
    case '/request':
      return routeRequest(body);
    case '/events':
      return routeEvents(url.searchParams);
    case '/output':
      return routeOutput();
    case '/configs':
      return routeConfigs();
    case '/launch':
      return routeLaunch(body);
    case '/stop':
      return routeStop(body);
    case '/breakpoints':
      return routeBreakpoints(req.method, body);
    default:
      throw Object.assign(new Error(`unknown route ${route}`), { status: 404 });
  }
}

async function handleRequest(req, res) {
  try {
    if (!authorized(req)) {
      return sendJson(res, 401, { ok: false, error: 'bad or missing bearer token' });
    }
    const url = new URL(req.url, 'http://localhost');
    let body = {};
    if (req.method === 'POST') body = await readBody(req);
    const payload = await dispatch(req, url, body);
    sendJson(res, 200, payload);
  } catch (err) {
    const status = (err && err.status) || 400;
    log(`error: ${(err && err.stack) || err}`);
    sendJson(res, status, {
      ok: false,
      error: String((err && err.message) || err),
    });
  }
}

// ---------------------------------------------------------------------------
// server lifecycle
// ---------------------------------------------------------------------------

function bridgeDir() {
  return path.join(os.homedir(), '.gdb-mcp', 'bridges');
}

function socketPathFor() {
  const key = crypto
    .createHash('sha1')
    .update(String(process.pid) + (vscode.workspace.name || ''))
    .digest('hex')
    .slice(0, 12);
  // Socket paths are limited to ~104 bytes; keep this short and in /tmp.
  return path.join(os.tmpdir(), `gdb-mcp-${key}.sock`);
}

function writeDiscovery() {
  const dir = bridgeDir();
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  discoveryFile = path.join(dir, `${process.pid}.json`);
  const info = {
    pid: process.pid,
    token,
    transport,
    startedAt: Date.now(),
    workspaceName: vscode.workspace.name || null,
    workspaceFolders: (vscode.workspace.workspaceFolders || []).map(
      (f) => f.uri.fsPath
    ),
  };
  if (transport === 'unix') info.socketPath = address;
  else {
    info.host = '127.0.0.1';
    info.port = address;
  }
  fs.writeFileSync(discoveryFile, JSON.stringify(info, null, 2), { mode: 0o600 });
  log(`discovery file: ${discoveryFile}`);
}

function removeDiscovery() {
  if (!discoveryFile) return;
  try {
    fs.unlinkSync(discoveryFile);
  } catch (_) {
    /* already gone */
  }
  discoveryFile = '';
}

async function startServer() {
  const config = vscode.workspace.getConfiguration('gdbMcpBridge');
  if (!config.get('enabled', true)) {
    log('bridge disabled by setting');
    return;
  }

  token = crypto.randomBytes(32).toString('hex');
  const preference = config.get('transport', 'auto');
  const useUnix =
    preference === 'unix' || (preference === 'auto' && process.platform !== 'win32');

  server = http.createServer(handleRequest);
  // Long-poll /events holds connections open; don't let Node time them out.
  server.timeout = 0;
  server.keepAliveTimeout = 75 * 1000;
  server.headersTimeout = 80 * 1000;

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    if (useUnix) {
      transport = 'unix';
      const socketPath = socketPathFor();
      try {
        fs.unlinkSync(socketPath);
      } catch (_) {
        /* no stale socket */
      }
      server.listen(socketPath, () => {
        try {
          fs.chmodSync(socketPath, 0o600);
        } catch (err) {
          log(`could not chmod socket: ${err}`);
        }
        address = socketPath;
        resolve();
      });
    } else {
      transport = 'tcp';
      server.listen(config.get('port', 0) || 0, '127.0.0.1', () => {
        address = server.address().port;
        resolve();
      });
    }
  });

  writeDiscovery();
  log(`bridge listening on ${transport}:${address}`);
  updateStatusBar();
}

async function stopServer() {
  removeDiscovery();
  if (server) {
    await new Promise((resolve) => server.close(resolve));
    if (transport === 'unix' && typeof address === 'string') {
      try {
        fs.unlinkSync(address);
      } catch (_) {
        /* already gone */
      }
    }
    server = null;
  }
  // Release anyone blocked in a long poll.
  const pending = waiters;
  waiters = [];
  for (const resolve of pending) resolve();
}

function updateStatusBar() {
  if (!statusBar) return;
  if (server) {
    statusBar.text = '$(debug-alt) GDB MCP';
    statusBar.tooltip = `GDB MCP Bridge listening on ${transport}:${address}`;
    statusBar.show();
  } else {
    statusBar.hide();
  }
}

// ---------------------------------------------------------------------------
// activation
// ---------------------------------------------------------------------------

function activate(context) {
  output = vscode.window.createOutputChannel('GDB MCP Bridge');
  context.subscriptions.push(output);

  statusBar = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    100
  );
  context.subscriptions.push(statusBar);

  context.subscriptions.push(
    vscode.debug.registerDebugAdapterTrackerFactory('*', trackerFactory())
  );

  context.subscriptions.push(
    vscode.debug.onDidStartDebugSession((session) => {
      sessions.set(session.id, {
        session,
        stopped: false,
        lastStop: null,
        threadId: null,
      });
      pushEvent('sessionStarted', {
        sessionId: session.id,
        name: session.name,
        type: session.type,
      });
    })
  );

  context.subscriptions.push(
    vscode.debug.onDidTerminateDebugSession((session) => {
      sessions.delete(session.id);
      pushEvent('sessionTerminated', { sessionId: session.id, name: session.name });
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('gdbMcpBridge.showStatus', () => {
      const where = server ? `${transport}:${address}` : 'not running';
      vscode.window.showInformationMessage(
        `GDB MCP Bridge: ${where} | sessions: ${sessions.size}`
      );
      if (output) output.show();
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('gdbMcpBridge.copyEndpoint', async () => {
      await vscode.env.clipboard.writeText(
        JSON.stringify({ transport, address, token, discoveryFile }, null, 2)
      );
      vscode.window.showInformationMessage('GDB MCP Bridge endpoint copied.');
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('gdbMcpBridge.restart', async () => {
      await stopServer();
      await startServer();
      vscode.window.showInformationMessage('GDB MCP Bridge restarted.');
    })
  );

  startServer().catch((err) => {
    log(`failed to start: ${(err && err.stack) || err}`);
    vscode.window.showErrorMessage(`GDB MCP Bridge failed to start: ${err}`);
  });
}

function deactivate() {
  return stopServer();
}

module.exports = { activate, deactivate };
