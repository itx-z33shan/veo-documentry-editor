/* Headless smoke check for the dashboard's clip-folder UI.
 *
 * app.js has no build step and no browser here, so this harness evaluates the
 * real scripts inside a minimal DOM/HTTP stub and drives the same events the
 * browser would fire: a folder pick, then the save form submit. It asserts the
 * queue the UI builds and the exact requests it sends to /api/upload.
 */
"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.join(__dirname, "..");
const listeners = new Map();

function makeElement(tag, id) {
  const element = {
    tagName: String(tag).toUpperCase(),
    id: id || "",
    className: "",
    dataset: {},
    hidden: false,
    disabled: false,
    checked: false,
    value: "",
    files: [],
    innerHTML: "",
    textContent: "",
    style: {},
    _events: {},
  };
  element.classList = {
    add: (name) => { element.className += ` ${name}`; },
    remove: (name) => { element.className = element.className.split(" ").filter((c) => c && c !== name).join(" "); },
    toggle: (name, on) => { if (on) element.classList.add(name); else element.classList.remove(name); },
    contains: (name) => element.className.split(" ").indexOf(name) !== -1,
  };
  element.addEventListener = (name, handler) => {
    (element._events[name] = element._events[name] || []).push(handler);
  };
  element.removeEventListener = () => {};
  element.dispatch = (name, event) => {
    const queue = (element._events[name] || []).slice();
    queue.forEach((handler) => handler(event || { preventDefault() {}, stopPropagation() {}, target: element }));
    return queue.length;
  };
  element.closest = (selector) => (selector === ".upload-card" ? document.querySelector("#clips-card") : null);
  element.querySelector = (selector) => document.querySelector(selector);
  element.querySelectorAll = () => [];
  element.click = () => { element.clicks = (element.clicks || 0) + 1; };
  element.reset = () => {};
  return element;
}

const elements = new Map();
function byId(id) {
  if (!elements.has(id)) elements.set(id, makeElement(id.endsWith("-file") || id.endsWith("-folder") ? "input" : "div", id));
  return elements.get(id);
}

// The handful of collection selectors app.js uses during bindEvents().
const groups = {
  ".workflow-card": [],  // replaced below with a raw-clips card stub
  "[data-next-step]": [],
  "[data-prev-step]": [],
  "[data-go-step]": [],
  ".step-panel": [],
  ".step-link": [],
  ".workflow-clips-field": [byId("clips-card"), byId("clips-queue-note")],
  ".workflow-clips-setting": [],
  ".workflow-master-field": [],
  ".workflow-master-setting": [],
  ".workflow-music-field": [],
  ".workflow-music-setting": [],
  ".upload-card": [byId("clips-card")],
  "input[type=file]": [
    byId("master-file"), byId("clips-file"), byId("clips-folder"),
    byId("narration-file"), byId("music-file"), byId("transcript-file"),
  ],
};
groups["input[type=file]"].forEach((input) => {
  input.type = "file";
  if (input.id === "clips-file" || input.id === "clips-folder" || input.id === "music-file") input.multiple = true;
});

const document = {
  querySelector(selector) {
    if (selector.startsWith("[data-for=")) {
      const id = selector.match(/"(.+)"/)[1];
      return byId(`${id}-choice`);
    }
    if (selector.startsWith("#")) return byId(selector.slice(1));
    return groups[selector] && groups[selector][0] ? groups[selector][0] : makeElement("div");
  },
  querySelectorAll(selector) {
    return groups[selector] || [];
  },
  addEventListener(name, handler) {
    if (!listeners.has(name)) listeners.set(name, []);
    listeners.get(name).push(handler);
  },
};

const requests = [];
class XMLHttpRequest {
  open(method, url) {
    this.method = method;
    this.url = new URL(url, "http://dashboard.local");
  }

  setRequestHeader() {}

  send(body) {
    const record = {
      field: this.url.searchParams.get("field"),
      name: this.url.searchParams.get("name"),
      replaceClips: this.url.searchParams.get("replaceClips"),
      final: this.url.searchParams.get("final"),
      size: body && body.size,
    };
    requests.push(record);
    setTimeout(() => {
      const response = { ok: true, saved: { field: record.field, name: record.name } };
      if (record.final === "true") response.media = mediaPayload();
      this.status = 200;
      this.response = response;
      (this._events.load || []).forEach((handler) => handler());
    }, 0);
  }

  abort() {
    (this._events.abort || []).forEach((handler) => handler());
  }

  addEventListener(name, handler) {
    (this._events = this._events || {})[name] = (this._events[name] || []).concat(handler);
  }

  get upload() {
    const self = this;
    if (!self._uploadEvents) self._uploadEvents = {};
    return {
      addEventListener(name, handler) {
        (self._uploadEvents[name] = self._uploadEvents[name] || []).push(handler);
      },
    };
  }
}

function mediaPayload() {
  return {
    health: { ffmpeg_available: true, message: "Media inspection is ready." },
    master: { exists: false },
    narration: { exists: true, name: "narration.aac", size_bytes: 4096, media: { duration: 120 } },
    music: { exists: false },
    transcript: { exists: false },
    clips: {
      count: 1,
      audio_clip_count: 1,
      total_bytes: 100,
      metadata: { exists: true, name: "metadata.json", size_bytes: 54 },
      files: [{ name: "clip_001.mp4", size_bytes: 100, exists: true, media: { duration: 8, has_audio: true } }],
    },
    recommendation: { workflow: "clips-embedded", title: "Use source clips", reason: "", warnings: [] },
  };
}

const sandbox = {
  document,
  console,
  // window.* and the shared listener registry used by the DOM stub above.
  addEventListener: (name, handler) => {
    if (!listeners.has(name)) listeners.set(name, []);
    listeners.get(name).push(handler);
  },
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval,
  URL,
  URLSearchParams,
  Set,
  Map,
  Math,
  Number,
  JSON,
  Array,
  Object,
  Promise,
  Error,
  RegExp,
  String,
  Boolean,
  fetch: async (url) => ({
    ok: true,
    json: async () => (String(url).startsWith("/api/media") ? { ok: true, media: mediaPayload() } : { ok: true, job: { status: "idle", logs: [], output_files: [] } }),
  }),
  XMLHttpRequest,
  self: null,
};
function HTMLInputElementStub() {}
HTMLInputElementStub.prototype.webkitdirectory = true;
sandbox.HTMLInputElement = HTMLInputElementStub;
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.globalThis = sandbox;

const context = vm.createContext(sandbox);
["clips-folder.js", "app.js"].forEach((file) => {
  vm.runInContext(fs.readFileSync(path.join(ROOT, "web", "static", file), "utf8"), context, { filename: file });
});

function tick() {
  return new Promise((resolve) => setTimeout(resolve, 5));
}

// A raw-clips workflow card, so the harness exercises the queue path.
const clipsCard = makeElement("button", "workflow-clips-card");
clipsCard.dataset.workflow = "clips-embedded";
groups[".workflow-card"].push(clipsCard);

(async () => {
  // Boot exactly like the browser does after the deferred scripts run.
  (listeners.get("DOMContentLoaded") || []).forEach((handler) => handler());
  await tick();
  clipsCard.dispatch("click");
  await tick();

  assert.strictEqual(byId("intake-note").textContent, "No clips queued.", "empty queue copy");
  assert.strictEqual(groups["input[type=file]"].length, 6);

  // The workspace already holds one clip from an earlier save, and the
  // "replace existing source clips" box defaults to on in index.html.
  byId("replace-clips").checked = true;

  // 1. A folder pick: two clips, a subfolder twin, plus junk to be ignored.
  const folder = byId("clips-folder");
  folder.files = [
    { name: "clip_001.mp4", webkitRelativePath: "veo/clip_001.mp4", size: 100 },
    { name: "clip_002.mp4", webkitRelativePath: "veo/clip_002.mp4", size: 200 },
    { name: "clip_001.mp4", webkitRelativePath: "veo/pick-2/clip_001.mp4", size: 300 },
    { name: "poster.jpg", webkitRelativePath: "veo/poster.jpg", size: 10 },
    { name: ".DS_Store", webkitRelativePath: "veo/.DS_Store", size: 3 },
    { name: "metadata.json", webkitRelativePath: "veo/metadata.json", size: 54 },
  ];
  assert.ok(folder.dispatch("change"), "change handler bound to the folder input");
  await tick();

  const queuePanel = byId("clips-queue");
  const choice = byId("clips-choice");
  assert.match(choice.textContent, /3 clips queued from “veo”/);
  assert.match(choice.textContent, /600 B/);
  assert.strictEqual(byId("pick-clips-folder").hidden, false, "folder picker is offered when supported");
  assert.match(byId("intake-note").textContent, /3 clips queued/);
  assert.match(queuePanel.innerHTML, /clip_001\.mp4/);
  assert.match(queuePanel.innerHTML, /pick-2_clip_001\.mp4/);
  assert.match(queuePanel.innerHTML, /not a supported video file/);
  assert.match(queuePanel.innerHTML, /hidden or system file/);
  assert.match(queuePanel.innerHTML, /metadata\.json sidecar/);
  assert.match(byId("upload-button").innerHTML, /Save &amp; inspect 3 clips/);
  assert.ok(queuePanel.hidden === false, "queue panel is shown once clips are staged");

  // 2. Save: one request per clip, replace only on the first, sidecar last.
  byId("upload-form").dispatch("submit", { preventDefault() {} });
  await tick();
  await tick();

  // Natural order keeps root clips before the subfolder copy, and the
  // subfolder twin is the one renamed. Only the first request may replace.
  assert.deepStrictEqual(requests.map((request) => [request.field, request.name, request.replaceClips, request.final]), [
    ["clips", "clip_001.mp4", "true", "false"],
    ["clips", "clip_002.mp4", "false", "false"],
    ["clips", "pick-2_clip_001.mp4", "false", "false"],
    ["clips", "metadata.json", "false", "true"],
  ]);
  assert.strictEqual(requests[0].size, 100, "the first clip streams its own bytes");
  assert.strictEqual(requests[2].size, 300, "the subfolder clip keeps its own payload");
  assert.strictEqual(requests[3].size, 54, "the sidecar is streamed like any other file");
  assert.match(byId("upload-progress-label").textContent, /Assets saved locally/);
  assert.strictEqual(byId("intake-note").textContent, "No clips queued.", "queue empties after a clean save");

  // 3. Re-picking the same folder: queued while replacing, skipped when the
  // queue is appended to the clips already saved in the workspace.
  folder.files = [
    { name: "clip_001.mp4", webkitRelativePath: "veo/clip_001.mp4", size: 100 },
  ];
  folder.dispatch("change");
  await tick();
  assert.match(byId("clips-choice").textContent, /1 clip queued from .veo. . 100 B/);
  byId("replace-clips").checked = false;
  byId("replace-clips").dispatch("change");
  await tick();
  assert.match(byId("clips-choice").textContent, /0 B . 1 already saved/);
  assert.match(byId("clips-queue").innerHTML, /skipped on save/);
  const before = requests.length;
  byId("upload-form").dispatch("submit", { preventDefault() {} });
  await tick();
  await tick();
  assert.strictEqual(requests.length, before, "nothing is re-uploaded for an unchanged workspace");
  assert.match(byId("flash").textContent, /already saved in the workspace/);

  console.log("dashboard clip-folder UI checks passed.");
})().catch((error) => {
  console.error(`FAIL - ${error.message}`);
  process.exit(1);
});
