// The household's abilities as real pi tools, for a long background task.
//
// Measured on the prototype, gemma4 (8B) on the bench Ollama, 2026-09-23:
//  - given only bash and told to run `alfred-skill web search ...`, it called a
//    tool named `web` directly -- twice, then kept going -- and pi answered
//    "Tool web not found" every time. A small model reaches for a tool with
//    the right name, not for a shell command it was told about;
//  - reading the document skill's 10 KB guide mid-task was the last thing it
//    did before going silent, twice. `make_document` is the part a long task
//    needs, as a tool with its own small schema;
//  - three pages fetched at 20000 characters put it past what it could use.
// Everything goes through `alfred-skill` (nanobot/harness/alfred_skill.py), so
// a skill behaves here exactly as it does for Alfred.
//
// ALFRED_HARNESS_BENCH=1 is the model benchmark: anything that would change the
// house -- a document filed on the share, a skill action that writes -- is
// answered with a stub shaped like the real result, and web reads run for real.
// Without it, benchmark runs filed their guides into a member's folder.
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";
import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve, sep } from "node:path";

const BENCH = ["1", "true", "yes"].includes((process.env.ALFRED_HARNESS_BENCH || "").toLowerCase());
const PYTHON = process.env.ALFRED_HARNESS_PYTHON || "python3";
// A skill action that only reads. Everything else is a write, and in the
// benchmark a write is stubbed.
// The same rule as the benchmark's READ_ACTION_RE (bench/model_bench.py), plus
// the weather skill's `house_location`.
const READ_ACTION = /^(?:list|get|show|read|search|find|check|status|describe|lookup|forecast|current|today|summary|balance|who|where|house_location)/i;

// pi itself runs with no secrets in its environment (pi_runner.py). The skills
// need theirs -- the share's password, Paperless's token -- so they get the
// household's environment from a 0600 file written for this run, read here and
// handed to the skill subprocess alone.
function skillEnv(): NodeJS.ProcessEnv {
  const file = process.env.ALFRED_SKILL_ENV_FILE;
  if (!file) return process.env;
  try {
    return JSON.parse(readFileSync(file, "utf8"));
  } catch {
    return process.env;
  }
}
const SKILL_ENV = skillEnv();

function alfredSkill(args: string[], signal?: AbortSignal): Promise<string> {
  return new Promise((done) => {
    execFile(PYTHON, ["-m", "nanobot.harness.alfred_skill", ...args],
      { maxBuffer: 8 * 1024 * 1024, signal, timeout: 240_000, env: SKILL_ENV },
      (err, stdout, stderr) => {
        const out = (stdout || "").trim();
        if (err && !out) done(JSON.stringify({ error: String(stderr || err.message).slice(-800) }));
        else done(out.length > 30000 ? out.slice(0, 30000) + "\n[... cut at 30000 chars]" : out);
      });
  });
}

// read/write/edit stay inside the task's own directory: the skill environment
// file, the member's workspace (MEMORY.md, USER.md) and everything else on the
// machine are not the task's to open or overwrite.
const HOME_DIR = resolve(process.cwd());
function outside(path: unknown): boolean {
  if (typeof path !== "string" || !path) return false;
  const full = resolve(HOME_DIR, path);
  return full !== HOME_DIR && !full.startsWith(HOME_DIR + sep);
}

const text = (t: string) => ({ content: [{ type: "text" as const, text: t }], details: {} });

// Small local models often send a list or an object as a JSON *string*:
// `"sections": "[{\"heading\": ...}]"`. pi validated it against the schema,
// refused it before the tool ran, and the model retried the same call until the
// task died -- most of the document failures of NeoHorse, Ornith and MiMo
// (2026-09-25). Repaired in `prepareArguments`, which pi runs *before*
// validating, so the schema the model reads stays exactly as strict as it was:
// widening it to "a list or a string" fixed those models and cost gemma4:e4b
// its long tasks, 8-9/10 down to 3/10, the same evening.
// A model writing JSON inside a string leaves real line breaks and tabs in the
// text values, which JSON.parse refuses. Escaped here -- only inside strings,
// where a control character can never be structure. NeoHorse's documents all
// failed on this (2026-09-25): the parse failed, pi's validator wrapped the
// text into a one-item list, and the model read "sections.0: must be object".
function escapeControlsInStrings(text: string): string {
  let out = "", inString = false, escaped = false;
  for (const ch of text) {
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      else if (ch < " ") { out += ch === "\n" ? "\\n" : ch === "\t" ? "\\t" : ch === "\r" ? "\\r" : ""; continue; }
    } else if (ch === '"') inString = true;
    out += ch;
  }
  return out;
}

function fromJson(value: unknown, want: "array" | "object"): unknown {
  if (typeof value !== "string") return value;
  for (const text of [value, escapeControlsInStrings(value)]) {
    try {
      let v = JSON.parse(text);
      // A document given whole ({"title", "sections": [...]}) or one section alone.
      if (want === "array" && v && typeof v === "object" && !Array.isArray(v)) {
        v = Array.isArray(v.sections) ? v.sections : [v];
      }
      if (want === "array" ? Array.isArray(v) : (v !== null && typeof v === "object" && !Array.isArray(v))) {
        return v;
      }
    } catch { /* try the next reading */ }
  }
  // Prose, not JSON at all: the document's one text section. JSON that is
  // broken beyond the repair above stays refused.
  if (want === "array" && !/^\s*[\[{]/.test(value) && value.trim()) return [{ text: value.trim() }];
  return value;
}

function repaired(args: unknown, fix: (a: Record<string, unknown>) => void): unknown {
  if (!args || typeof args !== "object" || Array.isArray(args)) return args;
  const a = { ...(args as Record<string, unknown>) };
  fix(a);
  return a;
}

function slugOf(title: string): string {
  return title.normalize("NFKD").replace(/[̀-ͯ]/g, "")
    .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60) || "documento";
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event) => {
    if (["read", "write", "edit"].includes(event.toolName)) {
      const input = (event.input || {}) as Record<string, unknown>;
      if (outside(input.path ?? input.file_path)) {
        return { block: true, reason: "Only files in the task's working directory. " +
          "For a web page use the web tool with action fetch." };
      }
    }
    return undefined;
  });

  pi.registerTool({
    name: "web",
    label: "Web",
    description: "Search the web or read a page. action=search needs query; action=fetch needs url " +
      "and returns the page as Markdown. Read the pages you rely on, not only the search results.",
    parameters: Type.Object({
      action: Type.Union([Type.Literal("search"), Type.Literal("fetch")]),
      query: Type.Optional(Type.String()),
      url: Type.Optional(Type.String()),
      count: Type.Optional(Type.Number()),
    }),
    // A call with no action (a url means fetch), or a count of "5".
    prepareArguments: (args: unknown) => repaired(args, (a) => {
      if (a.action === undefined) a.action = a.url ? "fetch" : "search";
      if (typeof a.count === "string" && Number(a.count) > 0) a.count = Number(a.count);
    }),
    async execute(_id, p, signal) {
      const payload = p.action === "fetch" ? { url: p.url, maxChars: 8000 }
        : { query: p.query, count: Math.min(10, p.count ?? 5) };
      return text(await alfredSkill(["web", p.action, JSON.stringify(payload)], signal));
    },
  });

  pi.registerTool({
    name: "make_document",
    label: "Make document",
    description: "Create the deliverable file and get its download link. format: pdf (guides, " +
      "reports), xlsx (tables, budgets), docx (editable), pptx (slides) or html (a web page). " +
      "sections: in order, each with an optional heading and text, items (a bullet list) or " +
      "table ({headers, rows}).",
    parameters: Type.Object({
      title: Type.String(),
      format: Type.Union([Type.Literal("pdf"), Type.Literal("xlsx"), Type.Literal("docx"),
                          Type.Literal("pptx"), Type.Literal("html")]),
      sections: Type.Array(Type.Object({
        heading: Type.Optional(Type.String()),
        text: Type.Optional(Type.String()),
        items: Type.Optional(Type.Array(Type.String())),
        table: Type.Optional(Type.Object({
          headers: Type.Array(Type.String()),
          rows: Type.Array(Type.Array(Type.String())),
        })),
      })),
    }),
    prepareArguments: (args: unknown) => repaired(args, (a) => {
      a.sections = fromJson(a.sections, "array");
    }),
    async execute(_id, p, signal) {
      const sections = p.sections;
      const filename = `${slugOf(p.title)}.${p.format}`;
      if (BENCH) {
        return text(JSON.stringify({ format: p.format, file: `media/${filename}`,
          share_path: `bench/${filename}`, link: `download:bench/${filename}`,
          sections: sections.length }, null, 2));
      }
      const payload = { format: p.format, filename, title: p.title, sections };
      return text(await alfredSkill(["document", "create", JSON.stringify(payload)], signal));
    },
  });

  pi.registerTool({
    name: "skill",
    label: "Skill",
    description: "Run one of the household's skills: paperless, weather, file-share, grocery, " +
      "and more (for the deliverable file use make_document). Read a skill's guide with " +
      "skill_guide before its first use.",
    parameters: Type.Object({
      skill: Type.String({ description: "skill name, e.g. paperless" }),
      action: Type.String({ description: "action name, e.g. search_documents" }),
      args: Type.Optional(Type.Record(Type.String(), Type.Any(), { description: "the action's arguments" })),
    }),
    prepareArguments: (args: unknown) => repaired(args, (a) => {
      a.args = fromJson(a.args, "object");
    }),
    async execute(_id, p, signal) {
      const args = p.args ?? {};
      if (BENCH && !READ_ACTION.test(p.action)) {
        return text(JSON.stringify({ ok: true }));
      }
      return text(await alfredSkill([p.skill, p.action, JSON.stringify(args)], signal));
    },
  });

  pi.registerTool({
    name: "skill_guide",
    label: "Skill guide",
    description: "How to use a skill: its actions and their arguments. Use 'list' as the skill to see all of them.",
    parameters: Type.Object({ skill: Type.String() }),
    async execute(_id, p, signal) {
      return text(await alfredSkill(p.skill === "list" ? ["list"] : ["guide", p.skill], signal));
    },
  });
}
