/* Node-runnable checks for the clip folder queue rules (no test framework). */
"use strict";

const assert = require("assert");
const path = require("path");
const { planClipFiles, sanitizeClipName, naturalCompare, isIgnorablePath } =
  require(path.join(__dirname, "..", "web", "static", "clips-folder.js"));

const cases = [];
function test(name, fn) {
  cases.push([name, fn]);
}

test("keeps only supported video files and reports the rest as skipped", () => {
  const plan = planClipFiles([
    { name: "clip_001.mp4", relativePath: "veo/clip_001.mp4", size: 10 },
    { name: "clip_002.MOV", relativePath: "veo/clip_002.MOV", size: 11 },
    { name: "poster.jpg", relativePath: "veo/poster.jpg", size: 5 },
    { name: "notes.txt", relativePath: "veo/notes.txt", size: 5 },
    { name: "clip_003.avi", relativePath: "veo/clip_003.avi", size: 5 },
  ]);
  assert.deepStrictEqual(plan.clips.map((clip) => clip.name),
    ["clip_001.mp4", "clip_002.MOV"]);
  assert.strictEqual(plan.skipped.notVideo, 3);
  assert.strictEqual(plan.error, null);
});

test("ignores macOS clutter and empty files", () => {
  assert.strictEqual(isIgnorablePath("veo/._clip_001.mp4"), true);
  assert.strictEqual(isIgnorablePath("__MACOSX/veo/clip_001.mp4"), true);
  assert.strictEqual(isIgnorablePath("veo/.DS_Store"), true);
  assert.strictEqual(isIgnorablePath("veo/clip_001.mp4"), false);
  const plan = planClipFiles([
    { name: "._clip_001.mp4", relativePath: "veo/._clip_001.mp4", size: 4 },
    { name: ".DS_Store", relativePath: "veo/.DS_Store", size: 6 },
    { name: "clip_002.mp4", relativePath: "veo/clip_002.mp4", size: 0 },
  ]);
  assert.strictEqual(plan.count, 0);
  assert.ok(plan.skipped.hidden >= 2);
  assert.strictEqual(plan.skipped.empty, 1);
});

test("orders clips naturally by their path inside the folder", () => {
  const files = ["shot_010", "shot_002", "shot_001", "shot_100"].map((name, index) => ({
    name: `${name}.mp4`,
    relativePath: `veo/${name}.mp4`,
    size: 100 + index,
  }));
  const plan = planClipFiles(files);
  assert.deepStrictEqual(plan.clips.map((clip) => clip.name),
    ["shot_001.mp4", "shot_002.mp4", "shot_010.mp4", "shot_100.mp4"]);
});

test("orders subfolders before recursing into the next one", () => {
  const files = [
    { name: "shot_002.mp4", relativePath: "b/shot_002.mp4", size: 4 },
    { name: "shot_001.mp4", relativePath: "a/shot_001.mp4", size: 1 },
    { name: "shot_010.mp4", relativePath: "a/shot_010.mp4", size: 2 },
    { name: "shot_001.mp4", relativePath: "b/shot_001.mp4", size: 3 },
  ];
  const plan = planClipFiles(files);
  assert.deepStrictEqual(plan.clips.map((clip) => clip.relativePath),
    ["a/shot_001.mp4", "a/shot_010.mp4", "b/shot_001.mp4", "b/shot_002.mp4"]);
});

test("naturalCompare sorts digits numerically", () => {
  assert.strictEqual(naturalCompare("shot_9.mp4", "shot_10.mp4"), -1);
  assert.strictEqual(naturalCompare("shot_10.mp4", "shot_9.mp4"), 1);
  assert.strictEqual(naturalCompare("a/shot_1.mp4", "b/shot_1.mp4"), -1);
});

test("duplicate names in subfolders are disambiguated with the parent folder", () => {
  const plan = planClipFiles([
    { name: "clip_001.mp4", relativePath: "batch-a/clip_001.mp4", size: 10 },
    { name: "clip_001.mp4", relativePath: "batch-b/clip_001.mp4", size: 20 },
  ]);
  assert.deepStrictEqual(plan.clips.map((clip) => clip.name),
    ["clip_001.mp4", "batch-b_clip_001.mp4"]);
  assert.strictEqual(plan.renamed, 1);
  assert.ok(plan.warnings.some((line) => /renamed/.test(line)));
});

test("the same file picked twice is queued once", () => {
  const plan = planClipFiles([
    { name: "clip_001.mp4", relativePath: "veo/clip_001.mp4", size: 10 },
    { name: "clip_001.mp4", relativePath: "veo/clip_001.mp4", size: 10 },
  ]);
  assert.strictEqual(plan.count, 1);
  assert.strictEqual(plan.duplicates, 1);
});

test("clips already saved in the workspace are marked as skipped", () => {
  const plan = planClipFiles(
    [
      { name: "clip_001.mp4", relativePath: "veo/clip_001.mp4", size: 10 },
      { name: "clip_002.mp4", relativePath: "veo/clip_002.mp4", size: 20 },
    ],
    { existing: { "clip_001.mp4": 10, "clip_002.mp4": 999 } });
  assert.strictEqual(plan.reused, 1);
  assert.deepStrictEqual(plan.pending.map((clip) => clip.name), ["clip_002.mp4"]);
  // A same-named but different-sized clip must not overwrite the old one when
  // appending, so it gets its own name instead.
  assert.strictEqual(plan.clips[1].renamed, false);
});

test("reserved names push appended clips to a free filename", () => {
  const plan = planClipFiles(
    [{ name: "clip_002.mp4", relativePath: "veo/clip_002.mp4", size: 77 }],
    { existing: { "clip_002.mp4": 77 }, reservedNames: ["clip_002.mp4"] });
  assert.strictEqual(plan.clips[0].name, "clip_002.mp4");
  assert.strictEqual(plan.clips[0].alreadyInWorkspace, true);

  const fresh = planClipFiles(
    [{ name: "clip_002.mp4", relativePath: "veo/clip_002.mp4", size: 12 }],
    { existing: { "clip_002.mp4": 77 }, reservedNames: ["clip_002.mp4"] });
  assert.strictEqual(fresh.clips[0].name, "veo_clip_002.mp4");
});

test("a top-level metadata.json sidecar is queued with the clips", () => {
  const plan = planClipFiles([
    { name: "metadata.json", relativePath: "veo/metadata.json", size: 400 },
    { name: "clip_001.mp4", relativePath: "veo/clip_001.mp4", size: 10 },
    { name: "metadata.json", relativePath: "veo/nested/metadata.json", size: 400 },
    { name: "metadata.json", relativePath: "veo/big.json", size: 5_000_000 },
  ]);
  assert.ok(plan.metadata);
  assert.strictEqual(plan.metadata.name, "metadata.json");
  assert.strictEqual(plan.metadata.relativePath, "veo/metadata.json");
  assert.strictEqual(plan.count, 1);
});

test("unsafe characters are sanitised exactly like the dashboard stores them", () => {
  assert.strictEqual(sanitizeClipName("../../etc/passwd.mp4"), "passwd.mp4");
  assert.strictEqual(sanitizeClipName("Veo 3: shot #12!.mp4"), "Veo 3_ shot _12_.mp4");
  const long = sanitizeClipName(`${"a".repeat(400)}.mp4`);
  assert.ok(long.length <= 180);
  assert.ok(long.endsWith(".mp4"));
});

test("refuses an oversized queue and an empty folder", () => {
  const many = [];
  for (let index = 0; index < 12; index += 1) {
    many.push({ name: `clip_${index}.mp4`, relativePath: `veo/clip_${index}.mp4`, size: 10 });
  }
  assert.match(planClipFiles(many, { limit: 10 }).error, /up to 10 at a time/);
  assert.match(planClipFiles([]).error, /No clips selected yet/);
  assert.match(planClipFiles([{ name: "a.txt", relativePath: "veo/a.txt", size: 3 }]).error,
    /No supported video clips found/);
});

let failures = 0;
cases.forEach(([name, fn]) => {
  try {
    fn();
    console.log(`ok   - ${name}`);
  } catch (error) {
    failures += 1;
    console.error(`FAIL - ${name}\n${error.message}`);
  }
});

if (failures) {
  console.error(`${failures} clip folder check(s) failed.`);
  process.exit(1);
}
console.log(`${cases.length} clip folder checks passed.`);
