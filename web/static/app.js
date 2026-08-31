(() => {
  "use strict";

  const workflowInfo = {
    "master-preserve": {
      title: "Finished CapCut master",
      engine: "master",
      audioMode: "preserve",
      safetyTitle: "No duplicate narration",
      safetyCopy: "Preserve mode keeps embedded CapCut audio and uses your separate AAC only as a duration and sync reference.",
    },
    "master-replace": {
      title: "Master with narration only",
      engine: "master",
      audioMode: "replace",
      safetyTitle: "Embedded master audio is removed",
      safetyCopy: "Replace mode prevents doubled voice, but it also removes baked music and ambience from the CapCut export.",
    },
    "master-rebuild": {
      title: "Rebuild from clean stems",
      engine: "master",
      audioMode: "rebuild",
      safetyTitle: "Only clean stems are mixed",
      safetyCopy: "The master audio is removed. Clean narration and separate music are ducked and mastered together.",
    },
    "clips-embedded": {
      title: "Raw clips + embedded audio",
      engine: "clips",
      audioMode: "embedded",
      safetyTitle: "Embedded audio stays deliberately quiet",
      safetyCopy: "Hard cuts are used by default so generated Veo music and ambience are not disrupted by global crossfades.",
    },
    "clips-music": {
      title: "Raw clips + separate music",
      engine: "clips",
      audioMode: "music",
      safetyTitle: "Narration remains the priority",
      safetyCopy: "A separate music stem is ducked beneath the voice before final loudness processing.",
    },
  };

  const stepTitles = {
    1: "Choose the safest route",
    2: "Add the approved assets",
    3: "Set a platform-safe finish",
    4: "Review before you render",
    5: "Deliver with confidence",
  };

  const state = {
    step: 1,
    workflow: "master-preserve",
    media: null,
    logCursor: 0,
    pollTimer: null,
    dryRunSucceeded: false,
    finalRenderSucceeded: false,
    // Staged source clips: nothing is written to clips/ until the form is saved.
    clipQueue: [],
    clipFiles: new Map(),
    clipPlan: null,
    uploadAborted: false,
    activeXhr: null,
  };

  // The dashboard streams each file with its own request, so a folder of 70
  // clips is 70 uploads. These guards keep a recursive drop from scanning an
  // entire drive and mirror the server-side per-file limit.
  const DASHBOARD_UPLOAD_LIMIT = 12 * 1024 * 1024 * 1024;
  const CLIP_SCAN_LIMIT = 4000;
  const CLIP_SCAN_DEPTH = 8;

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      ...options,
    });
    let payload;
    try {
      payload = await response.json();
    } catch (_) {
      throw new Error("The dashboard returned an invalid response.");
    }
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || "The request could not be completed.");
    }
    return payload;
  }

  function fileUrl(name) {
    return "/api/files/" + name.split("/").map(encodeURIComponent).join("/");
  }

  function humanBytes(value) {
    if (value === undefined || value === null) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let amount = Number(value);
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) {
      amount /= 1024;
      index += 1;
    }
    return `${amount.toFixed(amount >= 100 || index === 0 ? 0 : 1)} ${units[index]}`;
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(Number(seconds))) return "";
    const value = Math.max(0, Number(seconds));
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const remainder = Math.floor(value % 60);
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}` : `${minutes}:${String(remainder).padStart(2, "0")}`;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    }[character]));
  }

  function showFlash(message, type = "error") {
    const flash = $("#flash");
    flash.textContent = message;
    flash.className = `flash is-visible${type === "success" ? " is-success" : ""}`;
    window.clearTimeout(showFlash.timer);
    showFlash.timer = window.setTimeout(() => {
      flash.className = "flash";
      flash.textContent = "";
    }, type === "success" ? 4200 : 6800);
  }

  function setBusy(button, busy, label) {
    if (!button) return;
    if (busy) {
      button.dataset.originalLabel = button.innerHTML;
      button.disabled = true;
      button.textContent = label || "Working…";
    } else {
      button.disabled = false;
      if (button.dataset.originalLabel) button.innerHTML = button.dataset.originalLabel;
    }
  }

  function goStep(step) {
    step = Number(step);
    if (!step || step < 1 || step > 5) return;
    state.step = step;
    $$(".step-panel").forEach((panel) => {
      const active = Number(panel.dataset.step) === step;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });
    $$(".step-link").forEach((link) => link.classList.toggle("is-active", Number(link.dataset.goStep) === step));
    $("#page-title").textContent = stepTitles[step];
    if (step === 4) renderReview();
    if (step === 5) refreshResults();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function workflowDefinition() {
    return workflowInfo[state.workflow] || workflowInfo["master-preserve"];
  }

  function updateWorkflowUI() {
    const definition = workflowDefinition();
    const isMaster = definition.engine === "master";
    const isMusic = state.workflow === "master-rebuild" || state.workflow === "clips-music";

    $$(".workflow-card").forEach((card) => card.classList.toggle("is-selected", card.dataset.workflow === state.workflow));
    $$(".workflow-master-field").forEach((element) => { element.hidden = !isMaster; });
    $$(".workflow-clips-field").forEach((element) => { element.hidden = isMaster; });
    $$(".workflow-music-field").forEach((element) => { element.hidden = !isMusic; });
    $$(".workflow-master-setting").forEach((element) => { element.hidden = !isMaster; });
    $$(".workflow-clips-setting").forEach((element) => { element.hidden = isMaster; });
    $$(".workflow-music-setting").forEach((element) => { element.hidden = !isMusic; });

    const narrationRequirement = $(".narration-requirement");
    if (narrationRequirement) {
      narrationRequirement.textContent = state.workflow === "master-preserve" ? "Reference" : "Required";
    }
    $("#review-workflow").textContent = definition.title;
    $("#safety-title").textContent = definition.safetyTitle;
    $("#safety-copy").textContent = definition.safetyCopy;

    const keepAudio = $("#keep-clip-audio");
    if (state.workflow === "clips-embedded") keepAudio.checked = true;
    if (state.workflow === "clips-music") keepAudio.checked = false;
    updateTransitionHint();
    renderClipQueue();
  }

  function updateTransitionHint() {
    const hint = $("#transition-help");
    const transition = $("#transition");
    const keepAudio = $("#keep-clip-audio");
    if (!hint || !transition || !keepAudio) return;
    if (keepAudio.checked && transition.value === "crossfade") {
      hint.textContent = "Crossfade will be changed to a hard cut at render time to protect embedded clip audio.";
      hint.style.color = "#efbf71";
    } else if (keepAudio.checked) {
      hint.textContent = "Hard cuts avoid breaking embedded clip audio.";
      hint.style.color = "";
    } else {
      hint.textContent = "A separate music stem is recommended when using crossfades.";
      hint.style.color = "";
    }
  }

  function updateCaptionControls() {
    const subtitles = $("#subtitles").checked;
    const autoTranscript = $("#auto-transcript");
    const option = $("#transcription-option");
    const details = $("#transcription-details");
    autoTranscript.disabled = !subtitles;
    option.classList.toggle("is-disabled", !subtitles);
    details.hidden = !subtitles || !autoTranscript.checked;
  }

  function selectWorkflow(workflow) {
    if (!workflowInfo[workflow]) return;
    state.workflow = workflow;
    state.dryRunSucceeded = false;
    state.finalRenderSucceeded = false;
    $("#render-button").disabled = true;
    $("#go-deliver-button").disabled = true;
    updateWorkflowUI();
    renderReview();
  }

  function setHealth(health) {
    const chip = $("#health-chip");
    const chipDot = $(".status-dot", chip);
    const sidebarDot = $("#sidebar-status-dot");
    const sidebarStatus = $("#sidebar-status");
    chip.classList.toggle("is-good", Boolean(health.ffmpeg_available));
    chip.classList.toggle("is-bad", !health.ffmpeg_available);
    chipDot.classList.toggle("is-good", Boolean(health.ffmpeg_available));
    chipDot.classList.toggle("is-bad", !health.ffmpeg_available);
    sidebarDot.classList.toggle("is-good", Boolean(health.ffmpeg_available));
    sidebarDot.classList.toggle("is-bad", !health.ffmpeg_available);
    $("span:last-child", chip).textContent = health.ffmpeg_available ? "FFmpeg ready locally" : "FFmpeg needs attention";
    sidebarStatus.textContent = health.ffmpeg_available ? "FFmpeg ready" : "FFmpeg unavailable";
    const whisperStatus = $("#whisper-status");
    if (whisperStatus && health.local_whisper_message) {
      whisperStatus.textContent = health.local_whisper_message;
      whisperStatus.style.color = health.local_whisper_available ? "" : "#efbf71";
    }
  }

  function mediaDetails(item, kind) {
    if (!item || !item.exists) return "Not added yet";
    const media = item.media || {};
    const pieces = [];
    if (kind === "video") {
      if (media.duration) pieces.push(formatDuration(media.duration));
      if (media.width && media.height) pieces.push(`${media.width}×${media.height}`);
      if (media.fps) pieces.push(`${Number(media.fps).toFixed(Number(media.fps) % 1 ? 1 : 0)} fps`);
      if (media.has_audio === true) pieces.push("audio included");
    } else if (kind === "audio") {
      if (media.duration) pieces.push(formatDuration(media.duration));
      if (media.sample_rate) pieces.push(`${media.sample_rate} Hz`);
      if (media.channels) pieces.push(`${media.channels} ch`);
    } else if (kind === "clips") {
      pieces.push(`${item.count || 0} clip${item.count === 1 ? "" : "s"}`);
      if (item.audio_clip_count) pieces.push(`${item.audio_clip_count} with audio`);
      if (item.metadata && item.metadata.exists) pieces.push("metadata.json sidecar");
    }
    if (item.size_bytes) pieces.push(humanBytes(item.size_bytes));
    if (!pieces.length && item.probe_error) pieces.push("Needs FFmpeg inspection");
    return pieces.join(" · ") || "Ready";
  }

  function inventoryItem(label, icon, item, kind) {
    const exists = Boolean(item && (item.exists || item.count));
    const name = kind === "clips" && exists
      ? `${item.count} source clip${item.count === 1 ? "" : "s"}`
      : exists ? item.name : "Not added";
    const details = mediaDetails(item, kind);
    return `<div class="inventory-item ${exists ? "is-ready" : ""}">
      <span class="inventory-icon">${icon}</span>
      <div class="inventory-copy"><b>${escapeHtml(label)}</b><span>${escapeHtml(name || "Not added")}</span><small>${escapeHtml(details)}</small></div>
    </div>`;
  }

  function renderMedia(media) {
    state.media = media;
    const health = media.health || {};
    setHealth(health);
    const inventory = $("#media-summary");
    inventory.innerHTML = [
      inventoryItem("Finished master", "▣", media.master, "video"),
      inventoryItem("Narration", "◌", media.narration, "audio"),
      inventoryItem("Transcript / captions", "¶", media.transcript, "text"),
      inventoryItem("Music stem", "≋", media.music, "audio"),
      inventoryItem("Source clips", "▦", media.clips, "clips"),
    ].join("");

    const recommendation = media.recommendation || {};
    $("#recommendation-title").textContent = recommendation.title || "Choose a safe workflow.";
    $("#recommendation-reason").textContent = recommendation.reason || "";
    if (state.clipQueue.length) replanClips();
    const warnings = recommendation.warnings || [];
    const note = $("#inspection-note");
    if (health.ffmpeg_available) {
      note.textContent = warnings.length ? warnings[0] : "Media inspected with FFmpeg.";
    } else {
      note.textContent = health.message || "Add FFmpeg to inspect media.";
    }
  }

  async function refreshMedia(silent = false) {
    try {
      const response = await api("/api/media");
      renderMedia(response.media);
      if (!silent) showFlash("Workspace inspection refreshed.", "success");
    } catch (error) {
      if (!silent) showFlash(error.message);
    }
  }

  function updateFileChoice(input) {
    const target = document.querySelector(`[data-for="${input.id}"]`);
    const card = input.closest(".upload-card");
    const files = Array.from(input.files || []);
    if (!target || !card) return;
    if (!files.length) {
      const defaults = {
        "master-file": "Choose a video file",
        "narration-file": "Choose narration audio",
        "music-file": "Choose music audio",
        "transcript-file": "Choose transcript or SRT",
      };
      target.textContent = defaults[input.id] || "Choose file";
      card.classList.remove("has-file");
      return;
    }
    target.textContent = files.length === 1 ? files[0].name : `${files.length} files selected`;
    card.classList.add("has-file");
  }

  /* ------------------------------------------------------------------
   * Source-clip queue.
   *
   * A picked folder (or a dropped one) is turned into a staging queue here,
   * while web/static/clips-folder.js owns the rules: which files count as
   * clips, the natural order they are edited in, and the exact filename
   * clips/ will store. Nothing reaches the workspace until the form is saved.
   * ------------------------------------------------------------------ */

  function clipHelper() {
    return window.VeoClipsFolder || null;
  }

  function folderPickerSupported() {
    return typeof HTMLInputElement !== "undefined"
      && "webkitdirectory" in HTMLInputElement.prototype;
  }

  function replaceClipsChecked() {
    const box = $("#replace-clips");
    return !box || box.checked;
  }

  /** Workspace clips by name, so already-saved files can be skipped. */
  function existingClipSizes() {
    const index = {};
    const files = ((state.media || {}).clips || {}).files || [];
    files.forEach((file) => {
      if (file && file.name) index[file.name] = Number(file.size_bytes) || 0;
    });
    return index;
  }

  function planClipQueue() {
    const helper = clipHelper();
    if (!helper) return null;
    const existing = replaceClipsChecked() ? {} : existingClipSizes();
    return helper.planClipFiles(state.clipQueue, {
      limit: helper.MAX_CLIPS_PER_QUEUE,
      existing,
      reservedNames: Object.keys(existing),
      maxFileBytes: DASHBOARD_UPLOAD_LIMIT,
    });
  }

  function queueRootLabel(clips) {
    const roots = new Set(clips.map((clip) => String(clip.relativePath || "").split("/")[0]));
    if (roots.size === 1) {
      const root = Array.from(roots)[0];
      if (root && root !== clips[0].originalName) return root;
    }
    return "";
  }

  function renderClipQueue() {
    const card = $("#clips-card");
    const choice = $("#clips-choice");
    const panel = $("#clips-queue");
    const clear = $("#clear-clips");
    const note = $("#intake-note");
    if (!card || !panel) return;
    const plan = state.clipPlan;
    const clips = plan ? plan.clips : [];
    if (clear) clear.hidden = clips.length === 0;
    card.classList.toggle("has-file", clips.length > 0);
    card.classList.toggle("has-error", Boolean(plan && plan.error && clips.length));

    if (!clips.length) {
      if (choice) {
        choice.textContent = folderPickerSupported()
          ? "Choose a folder, or drop one here"
          : "Choose one or more clips";
      }
      panel.hidden = true;
      panel.innerHTML = "";
      if (note) {
        note.textContent = workflowDefinition().engine === "master"
          ? "Not needed for a finished master."
          : "No clips queued.";
      }
      updateUploadButton();
      return;
    }

    const root = queueRootLabel(clips);
    const skipped = clips.length - plan.pending.length;
    const summary = `${clips.length} clip${clips.length === 1 ? "" : "s"} queued`
      + (root ? ` from “${root}”` : "")
      + ` · ${humanBytes(plan.totalBytes)}`
      + (skipped ? ` · ${skipped} already saved` : "");
    if (choice) choice.textContent = summary;

    const shown = clips.slice(0, 6).map((clip) => `<li><span>${escapeHtml(clip.name)}</span><small>${clip.alreadyInWorkspace
      ? "already in workspace"
      : escapeHtml(humanBytes(clip.size))}</small></li>`).join("");
    const rest = clips.length > 6 ? `<li class="queue-more">+${clips.length - 6} more</li>` : "";
    const notes = (plan.warnings || []).map((line) => `<li>${escapeHtml(line)}</li>`).join("");
    panel.innerHTML = `<ul class="queue-list">${shown}${rest}</ul>`
      + (notes ? `<ul class="queue-notes">${notes}</ul>` : "")
      + (plan.error ? `<p class="queue-error">${escapeHtml(plan.error)}</p>` : "");
    panel.hidden = !notes && !shown && !plan.error;
    if (note) {
      note.textContent = workflowDefinition().engine === "master"
        ? `${summary} · not used by the master route`
        : summary;
    }
    updateUploadButton();
  }

  function updateUploadButton() {
    const button = $("#upload-button");
    if (!button || button.disabled) return;
    const plan = state.clipPlan;
    const master = workflowDefinition().engine === "master";
    const pending = master ? 0 : (plan ? plan.pending.length : 0);
    button.innerHTML = pending > 1
      ? `Save &amp; inspect ${pending} clips <span>↑</span>`
      : "Save &amp; inspect media <span>↑</span>";
  }

  function replanClips() {
    state.clipPlan = planClipQueue();
    renderClipQueue();
    return state.clipPlan;
  }

  function queueClipEntries(items) {
    const helper = clipHelper();
    if (!helper) {
      showFlash("The clip queue script failed to load. Reload the dashboard.");
      return;
    }
    let added = 0;
    items.forEach((item) => {
      const file = item.file;
      if (!file) return;
      const relativePath = item.relativePath || file.name;
      const key = helper.queueKey(relativePath, file.size);
      if (state.clipFiles.has(key)) return;
      state.clipFiles.set(key, file);
      state.clipQueue.push({ key, relativePath, name: file.name, size: file.size });
      added += 1;
    });
    const plan = replanClips();
    if (plan && plan.error) {
      showFlash(plan.error);
      return;
    }
    if (!added) {
      showFlash("Those clips are already in the queue.");
      return;
    }
    const skipped = plan.clips.length - plan.pending.length;
    showFlash(`${added} clip${added === 1 ? "" : "s"} queued (${humanBytes(plan.totalBytes)} to save)`
      + (skipped ? `, ${skipped} already in the workspace` : "")
      + ". Save the form to copy them into clips/.", "success");
  }

  function clearClipQueue() {
    state.clipQueue = [];
    state.clipFiles = new Map();
    const folder = $("#clips-folder");
    const files = $("#clips-file");
    if (folder) folder.value = "";
    if (files) files.value = "";
    replanClips();
  }

  function entriesFromFileInput(input) {
    return Array.from(input.files || []).map((file) => ({
      file,
      relativePath: file.webkitRelativePath || file.name,
    }));
  }

  function fileFromEntry(entry, prefix) {
    return new Promise((resolve) => {
      const fail = () => resolve(null);
      try {
        entry.file((file) => resolve({
          file,
          relativePath: prefix ? `${prefix}/${entry.name}` : entry.name,
        }), fail);
      } catch (error) {
        fail();
      }
    });
  }

  function readDirectoryEntries(entry) {
    return new Promise((resolve) => {
      const reader = entry.createReader();
      const found = [];
      const step = () => {
        try {
          reader.readEntries((batch) => {
            if (!batch || !batch.length) {
              resolve(found);
              return;
            }
            found.push(...batch);
            if (found.length >= CLIP_SCAN_LIMIT) {
              resolve(found);
              return;
            }
            step();
          }, () => resolve(found));
        } catch (error) {
          resolve(found);
        }
      };
      step();
    });
  }

  /** Depth-limited walk so a dropped folder behaves like the folder picker. */
  async function entriesFromDataTransfer(dataTransfer) {
    const items = Array.from((dataTransfer && dataTransfer.items) || []);
    const supportsEntries = items.length > 0
      && typeof items[0].webkitGetAsEntry === "function";
    const roots = [];
    if (supportsEntries) {
      items.forEach((item) => {
        if (item.kind !== "file") return;
        const entry = item.webkitGetAsEntry();
        if (entry) roots.push(entry);
      });
    }
    if (!roots.length) {
      return Array.from((dataTransfer && dataTransfer.files) || [])
        .map((file) => ({ file, relativePath: file.name }));
    }
    const collected = [];
    const pending = roots.map((entry) => ({ entry, prefix: "", depth: 0 }));
    while (pending.length && collected.length < CLIP_SCAN_LIMIT) {
      const { entry, prefix, depth } = pending.shift();
      const path = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory) {
        if (depth >= CLIP_SCAN_DEPTH) continue;
        const children = await readDirectoryEntries(entry);
        children.forEach((child) => pending.push({ entry: child, prefix: path, depth: depth + 1 }));
        continue;
      }
      const item = await fileFromEntry(entry, prefix);
      if (item) collected.push(item);
    }
    return collected;
  }

  async function queueDroppedClips(card, dataTransfer) {
    card.classList.add("is-reading");
    const choice = $("#clips-choice");
    if (choice) choice.textContent = "Reading folder…";
    try {
      const items = await entriesFromDataTransfer(dataTransfer);
      queueClipEntries(items);
    } catch (error) {
      showFlash("That folder could not be read. Use Choose folder instead.");
      renderClipQueue();
    } finally {
      card.classList.remove("is-reading");
    }
  }

  function selectedUploads() {
    const definition = workflowDefinition();
    const uploads = [];
    const addFiles = (field, input) => {
      if (!input) return;
      Array.from(input.files || []).forEach((file) => {
        uploads.push({ field, file, name: file.name, replaceClips: false });
      });
    };
    if (definition.engine === "master") addFiles("master", $("#master-file"));
    else addQueuedClips(uploads);
    addFiles("narration", $("#narration-file"));
    if (state.workflow === "master-rebuild" || state.workflow === "clips-music") addFiles("music", $("#music-file"));
    addFiles("transcript", $("#transcript-file"));
    return uploads;
  }

  /** Clips are uploaded one request per file; only the first may replace. */
  function addQueuedClips(uploads) {
    const plan = state.clipPlan;
    if (!plan) return;
    const replace = replaceClipsChecked();
    plan.clips.forEach((clip) => {
      if (!replace && clip.alreadyInWorkspace) return;
      const file = state.clipFiles.get(clip.key);
      if (!file) return;
      uploads.push({ field: "clips", file, name: clip.name, replaceClips: false });
    });
    const firstClip = uploads.find((entry) => entry.field === "clips");
    if (firstClip && replace) firstClip.replaceClips = true;
    if (plan.metadata) {
      const file = state.clipFiles.get(plan.metadata.key);
      // Sent last so a replace of the clip folder cannot discard the sidecar.
      if (file) uploads.push({ field: "clips", file, name: "metadata.json", replaceClips: false });
    }
  }

  function uploadError(message, aborted = false) {
    const error = new Error(message);
    error.aborted = aborted;
    return error;
  }

  function rawUpload(entry, final, completedBytes, totalBytes, updateProgress) {
    return new Promise((resolve, reject) => {
      const query = new URLSearchParams({
        field: entry.field,
        name: entry.name || entry.file.name,
        replaceClips: entry.replaceClips ? "true" : "false",
        final: final ? "true" : "false",
      });
      const xhr = new XMLHttpRequest();
      const settle = (callback, value) => {
        state.activeXhr = null;
        callback(value);
      };
      state.activeXhr = xhr;
      xhr.open("POST", `/api/upload?${query.toString()}`);
      xhr.responseType = "json";
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.addEventListener("progress", (event) => {
        if (!event.lengthComputable) return;
        updateProgress(completedBytes + event.loaded, totalBytes);
      });
      xhr.addEventListener("load", () => {
        if (xhr.status < 200 || xhr.status >= 300 || !xhr.response || !xhr.response.ok) {
          settle(reject, uploadError((xhr.response && xhr.response.error) || `Upload failed for ${entry.file.name}.`));
          return;
        }
        settle(resolve, xhr.response);
      });
      xhr.addEventListener("error", () => settle(reject, uploadError(`Upload connection failed for ${entry.file.name}.`)));
      xhr.addEventListener("timeout", () => settle(reject, uploadError(`Upload timed out for ${entry.file.name}.`)));
      xhr.addEventListener("abort", () => settle(reject, uploadError(`Upload stopped during ${entry.file.name}.`, true)));
      xhr.send(entry.file);
    });
  }

  async function uploadSelectedFiles(event) {
    event.preventDefault();
    const form = $("#upload-form");
    const definition = workflowDefinition();
    const plan = state.clipPlan;
    if (plan && plan.error) {
      showFlash(plan.error);
      return;
    }
    const uploads = selectedUploads();
    if (!uploads.length) {
      const queued = plan ? plan.clips.length : 0;
      showFlash(queued
        ? "Every queued clip is already saved in the workspace. Turn on “Replace existing source clips” to send them again."
        : "Choose at least one file for the selected workflow.");
      return;
    }

    const button = $("#upload-button");
    const progress = $("#upload-progress");
    const bar = $("#upload-progress-bar");
    const label = $("#upload-progress-label");
    const value = $("#upload-progress-value");
    const cancel = $("#upload-cancel");
    const totalBytes = uploads.reduce((sum, entry) => sum + entry.file.size, 0);
    const many = uploads.length > 1;
    let completedBytes = 0;
    let saved = 0;
    let finalResponse = null;
    let replaced = false;
    const failures = [];
    const updateProgress = (loaded, total) => {
      const percent = total ? Math.max(0, Math.min(100, Math.round(loaded / total * 100))) : 0;
      bar.style.width = `${percent}%`;
      value.textContent = `${percent}%`;
    };

    state.uploadAborted = false;
    setBusy(button, true, "Uploading…");
    progress.hidden = false;
    if (cancel) cancel.hidden = !many;
    updateProgress(0, totalBytes);
    let stopped = false;
    try {
      for (let index = 0; index < uploads.length; index += 1) {
        if (state.uploadAborted) {
          stopped = true;
          break;
        }
        const entry = uploads[index];
        const size = humanBytes(entry.file.size);
        label.textContent = many
          ? `Uploading ${index + 1} of ${uploads.length} (${size}): ${entry.file.name}`
          : `Uploading ${entry.file.name} (${size})`;
        if (entry.replaceClips) replaced = true;
        try {
          const response = await rawUpload(entry, index === uploads.length - 1, completedBytes, totalBytes, updateProgress);
          if (response && response.media) finalResponse = response;
        } catch (error) {
          if (error.aborted) {
            stopped = true;
            break;
          }
          // A stray clip in a 70-file folder must not destroy the other 69.
          // A failed master/narration/music file, or a failed first clip while
          // replacing, stops the batch so the workspace never ends up mixed.
          if (index === 0 || entry.field !== "clips") throw error;
          failures.push(`${entry.file.name}: ${error.message}`);
          completedBytes += entry.file.size;
          updateProgress(completedBytes, totalBytes);
          continue;
        }
        saved += 1;
        completedBytes += entry.file.size;
        updateProgress(completedBytes, totalBytes);
      }

      if (finalResponse && finalResponse.media) renderMedia(finalResponse.media);
      else await refreshMedia(true);

      if (stopped && saved < uploads.length) {
        value.textContent = "Stopped";
        label.textContent = `Upload stopped after ${saved} of ${uploads.length} files. Saved files stay in the workspace; the rest are still queued.`;
        showFlash(`Upload stopped. Re-pick the folder to continue — saved clips are detected and skipped.`);
      } else if (failures.length) {
        value.textContent = "Partial";
        label.textContent = `${saved} of ${uploads.length} files saved.`;
        if (replaced) {
          const box = $("#replace-clips");
          if (box) box.checked = false;
          replanClips();
        }
        showFlash(`${failures.length} file(s) failed: ${failures.slice(0, 2).join("; ")}${failures.length > 2 ? " …" : ""}. The rest are saved; pick the folder again to send only what is missing.`);
      } else {
        value.textContent = "Saved";
        label.textContent = "Assets saved locally and inspected.";
        // A master workflow never consumes the queue, so keep the staged
        // clips for the next time the raw-clips route is selected.
        if (definition.engine === "clips") clearClipQueue();
        form.reset();
        $$("input[type=file]", form).filter((input) => !input.id.startsWith("clips-")).forEach(updateFileChoice);
        showFlash("Assets are in the local workspace. Review the inspection below.", "success");
      }
    } catch (error) {
      await refreshMedia(true);
      showFlash(error.message);
    } finally {
      setBusy(button, false);
      updateUploadButton();
      if (cancel) {
        cancel.hidden = true;
        cancel.textContent = "Stop after the current file";
      }
    }
  }

  function currentSettings() {
    return {
      loudnessTarget: Number($("#loudness-target").value),
      truePeak: Number($("#true-peak").value),
      aacBitrate: Number($("#aac-bitrate").value),
      masterFade: Number($("#master-fade").value),
      subtitles: $("#subtitles").checked,
      autoTranscript: $("#subtitles").checked && $("#auto-transcript").checked,
      transcriptionModel: $("#transcription-model").value,
      keepClipAudio: $("#keep-clip-audio").checked,
      useGeminiMatching: $("#use-gemini-matching").checked,
      clipAudioDucking: $("#clip-audio-ducking").checked,
      clipAudioVolume: Number($("#clip-audio-volume").value),
      transition: $("#transition").value,
      crossfadeSeconds: 0.3,
      musicVolume: Number($("#music-volume").value),
      ducking: $("#ducking").checked,
    };
  }

  function renderReview() {
    const definition = workflowDefinition();
    const settings = currentSettings();
    const audioPlan = {
      preserve: "Preserve CapCut mix; AAC stays a sync reference",
      replace: "Remove master audio; use narration only",
      rebuild: "Remove master audio; mix narration + separate music",
      embedded: !settings.keepClipAudio ? "Clip audio muted; narration only" : settings.clipAudioDucking ? "Veo clip music automatically ducked under narration" : "Quiet fixed embedded clip audio under narration",
      music: "Separate music ducked under narration",
    }[definition.audioMode];
    const transition = definition.engine === "clips" ? (settings.keepClipAudio && settings.transition === "crossfade" ? "Hard cut (embedded-audio protection)" : settings.transition === "cut" ? "Hard cuts" : "Crossfade") : `${settings.masterFade.toFixed(2)} sec opening/closing fade`;
    const matching = definition.engine !== "clips" ? "Existing CapCut visual edit preserved" : settings.useGeminiMatching ? "Gemini visual matching (local key / API quota)" : "Local deterministic clip matching";
    const uploadedCaptions = Boolean(state.media && state.media.transcript && state.media.transcript.exists);
    const captions = !settings.subtitles ? "No captions" : uploadedCaptions
      ? "Uploaded transcript / SRT sidecar"
      : settings.autoTranscript ? `Local Whisper ${settings.transcriptionModel} draft SRT` : "No caption source selected";
    $("#review-summary").innerHTML = `<div class="review-lines">
      <div class="review-line"><span>Workflow</span><b>${escapeHtml(definition.title)}</b></div>
      <div class="review-line"><span>Audio route</span><b>${escapeHtml(audioPlan)}</b></div>
      <div class="review-line"><span>Delivery</span><b>YouTube + Facebook / 1080p</b></div>
      <div class="review-line"><span>Final target</span><b>${settings.loudnessTarget} LUFS · ${settings.truePeak} dBTP</b></div>
      <div class="review-line"><span>Transitions / fade</span><b>${escapeHtml(transition)}</b></div>
      <div class="review-line"><span>Visual matching</span><b>${escapeHtml(matching)}</b></div>
      <div class="review-line"><span>Captions</span><b>${escapeHtml(captions)}</b></div>
    </div>`;
  }

  function resetConsole() {
    state.logCursor = 0;
    $("#job-log").textContent = "";
    $("#job-status").textContent = "Starting local editor…";
    $("#job-stage").textContent = "Preparing workflow";
  }

  function updateJobUI(job) {
    const log = $("#job-log");
    if (job.logs && job.logs.length) {
      const text = job.logs.map((entry) => entry.line).join("\n");
      log.textContent += (log.textContent ? "\n" : "") + text;
      log.scrollTop = log.scrollHeight;
    }
    state.logCursor = job.next_cursor || state.logCursor;
    const led = $("#console-led");
    led.className = "console-led";
    const status = job.status || "idle";
    const statusLabel = {
      idle: "Waiting for a dry run",
      running: job.action === "dry-run" ? "Dry run in progress" : "Final render in progress",
      succeeded: job.action === "dry-run" ? "Dry run completed" : "Final render completed",
      failed: "Render needs attention",
      cancelled: "Render cancelled",
    }[status] || status;
    $("#job-status").textContent = statusLabel;
    $("#job-stage").textContent = job.stage || (status === "running" ? "Working locally" : status === "succeeded" ? "Finished" : job.error || "No process active");
    if (status === "running") led.classList.add("is-running");
    if (status === "succeeded") led.classList.add("is-good");
    if (status === "failed" || status === "cancelled") led.classList.add("is-bad");
    $("#cancel-button").hidden = status !== "running";
    if (status !== "running" && job.action === "dry-run") setBusy($("#dry-run-button"), false);
    if (status !== "running" && job.action === "render") setBusy($("#render-button"), false);

    if (status === "succeeded" && job.action === "dry-run") {
      state.dryRunSucceeded = true;
      $("#render-button").disabled = false;
      refreshResults();
      showFlash("Dry run passed. Review the caption draft and warnings before starting the final render.", "success");
    }
    if (status === "succeeded" && job.action === "render") {
      state.finalRenderSucceeded = true;
      $("#go-deliver-button").disabled = false;
      refreshResults();
      showFlash("Final render finished. Your MP4, SRT, and report are ready.", "success");
    }
    if (status === "failed" && job.error) showFlash(job.error);
  }

  function stopPolling() {
    if (state.pollTimer) window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  async function pollJob() {
    try {
      const response = await api(`/api/job?cursor=${encodeURIComponent(state.logCursor)}`);
      updateJobUI(response.job);
      if (response.job.status !== "running") stopPolling();
    } catch (error) {
      stopPolling();
      showFlash(error.message);
    }
  }

  async function startJob(action) {
    if (action === "render" && !state.dryRunSucceeded) {
      showFlash("Run the dry check before starting the final render.");
      return;
    }
    const button = action === "dry-run" ? $("#dry-run-button") : $("#render-button");
    setBusy(button, true, action === "dry-run" ? "Checking…" : "Rendering…");
    if (action === "dry-run") {
      state.dryRunSucceeded = false;
      $("#render-button").disabled = true;
    }
    resetConsole();
    try {
      const response = await api("/api/job", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, workflow: state.workflow, settings: currentSettings() }),
      });
      updateJobUI(response.job);
      stopPolling();
      state.pollTimer = window.setInterval(pollJob, 900);
      window.setTimeout(pollJob, 250);
    } catch (error) {
      setBusy(button, false);
      showFlash(error.message);
    }
  }

  async function cancelJob() {
    try {
      const response = await api("/api/job/cancel", { method: "POST" });
      updateJobUI(response.job);
      showFlash("Cancellation requested. FFmpeg may take a moment to stop.", "success");
    } catch (error) {
      showFlash(error.message);
    }
  }

  function renderResults(files) {
    const list = $("#deliverables-list");
    const videos = files.filter((file) => file.kind === "video");
    const report = files.find((file) => file.kind === "report" && /report\.json$/i.test(file.name));
    const subtitle = files.find((file) => file.kind === "subtitle" && /subtitles\.srt$/i.test(file.name));
    const preferredVideo = videos.find((file) => /final_master\.mp4$/i.test(file.name)) || videos.find((file) => /final_documentary\.mp4$/i.test(file.name)) || videos[0];

    if (preferredVideo) {
      $("#video-result").innerHTML = `<video controls preload="metadata" src="${escapeHtml(fileUrl(preferredVideo.name))}">Your browser cannot preview this MP4.</video>`;
    } else {
      $("#video-result").innerHTML = `<div class="video-empty"><span>▶</span><strong>Final video preview</strong><p>Complete a render to preview the delivery master here.</p></div>`;
    }
    if (!files.length) {
      list.innerHTML = '<p class="muted">No render outputs yet.</p>';
    } else {
      list.innerHTML = files.map((file) => {
        const label = file.kind === "video" ? "MP4" : file.kind === "subtitle" ? "SRT" : file.kind === "report" ? "JSON" : "FILE";
        return `<div class="deliverable"><span class="deliverable-icon">${label}</span><div class="deliverable-copy"><b>${escapeHtml(file.name)}</b><small>${escapeHtml(humanBytes(file.size_bytes))}</small></div><a href="${escapeHtml(fileUrl(file.name))}" download>Download</a></div>`;
      }).join("");
    }
    const reportLink = $("#open-report");
    if (report) {
      reportLink.hidden = false;
      reportLink.href = fileUrl(report.name);
    } else {
      reportLink.hidden = true;
      reportLink.href = "#";
    }
    const reviewSrt = $("#review-srt-button");
    if (subtitle) {
      reviewSrt.hidden = false;
      reviewSrt.href = fileUrl(subtitle.name);
    } else {
      reviewSrt.hidden = true;
      reviewSrt.href = "#";
    }
  }

  async function refreshResults() {
    try {
      const response = await api("/api/results");
      renderResults(response.files || []);
    } catch (error) {
      showFlash(error.message);
    }
  }

  function bindClipQueueControls() {
    const card = $("#clips-card");
    const folderInput = $("#clips-folder");
    const filesInput = $("#clips-file");
    if (!card || !folderInput || !filesInput) return;

    if (!folderPickerSupported()) {
      const button = $("#pick-clips-folder");
      if (button) button.hidden = true;
    }

    // Both pickers fill the same queue, so a project can be assembled from
    // several folders (Veo exports, CapCut picks) before anything is saved.
    folderInput.addEventListener("change", () => queueClipEntries(entriesFromFileInput(folderInput)));
    filesInput.addEventListener("change", () => queueClipEntries(entriesFromFileInput(filesInput)));

    const stop = (event) => {
      event.preventDefault();
      event.stopPropagation();
    };
    $("#pick-clips-folder").addEventListener("click", (event) => {
      stop(event);
      folderInput.value = "";
      folderInput.click();
    });
    $("#pick-clip-files").addEventListener("click", (event) => {
      stop(event);
      filesInput.click();
    });
    $("#clear-clips").addEventListener("click", (event) => {
      stop(event);
      clearClipQueue();
      showFlash("Clip queue cleared. Files already saved in the workspace are untouched.", "success");
    });
    card.addEventListener("click", (event) => {
      if (event.target.closest("button") || event.target === folderInput || event.target === filesInput) return;
      if (!folderPickerSupported()) {
        filesInput.click();
        return;
      }
      folderInput.value = "";
      folderInput.click();
    });

    ["dragenter", "dragover"].forEach((name) => card.addEventListener(name, (event) => {
      event.preventDefault();
      card.classList.add("is-dragging");
    }));
    ["dragleave", "drop"].forEach((name) => card.addEventListener(name, (event) => {
      event.preventDefault();
      card.classList.remove("is-dragging");
    }));
    card.addEventListener("drop", (event) => {
      const dataTransfer = event.dataTransfer;
      if (!dataTransfer) return;
      queueDroppedClips(card, dataTransfer);
    });

    const replace = $("#replace-clips");
    if (replace) replace.addEventListener("change", () => replanClips());

    const cancel = $("#upload-cancel");
    if (cancel) cancel.addEventListener("click", () => {
      state.uploadAborted = true;
      cancel.textContent = "Stopping…";
      if (state.activeXhr) state.activeXhr.abort();
    });
  }

  function bindEvents() {
    $$(".workflow-card").forEach((card) => card.addEventListener("click", () => selectWorkflow(card.dataset.workflow)));
    $$('[data-next-step]').forEach((button) => button.addEventListener("click", () => goStep(button.dataset.nextStep)));
    $$('[data-prev-step]').forEach((button) => button.addEventListener("click", () => goStep(button.dataset.prevStep)));
    $$('[data-go-step]').forEach((button) => button.addEventListener("click", () => goStep(button.dataset.goStep)));
    $$("input[type=file]").filter((input) => !input.id.startsWith("clips-"))
      .forEach((input) => input.addEventListener("change", () => updateFileChoice(input)));
    $$(".upload-card").filter((card) => card.id !== "clips-card").forEach((card) => {
      const input = $("input[type=file]", card);
      ["dragenter", "dragover"].forEach((eventName) => card.addEventListener(eventName, (event) => {
        event.preventDefault();
        card.classList.add("is-dragging");
      }));
      ["dragleave", "drop"].forEach((eventName) => card.addEventListener(eventName, (event) => {
        event.preventDefault();
        card.classList.remove("is-dragging");
      }));
      card.addEventListener("drop", (event) => {
        const files = Array.from(event.dataTransfer.files || []);
        if (!files.length) return;
        const transfer = new DataTransfer();
        (input.multiple ? files : files.slice(0, 1)).forEach((file) => transfer.items.add(file));
        input.files = transfer.files;
        updateFileChoice(input);
      });
    });
    bindClipQueueControls();
    // Dropping a folder anywhere else on the page would otherwise navigate
    // the browser away from the wizard and lose the queue.
    ["dragover", "drop"].forEach((eventName) => document.addEventListener(eventName, (event) => {
      if (event.target.closest(".upload-card")) return;
      event.preventDefault();
    }));
    $("#upload-form").addEventListener("submit", uploadSelectedFiles);
    $("#refresh-media").addEventListener("click", () => refreshMedia());
    $("#refresh-results").addEventListener("click", refreshResults);
    $("#master-fade").addEventListener("input", (event) => {
      $("#fade-output").textContent = `${Number(event.target.value).toFixed(2)} sec`;
      if (state.step === 4) renderReview();
    });
    ["#loudness-target", "#true-peak", "#aac-bitrate", "#keep-clip-audio", "#use-gemini-matching", "#clip-audio-ducking", "#clip-audio-volume", "#transition", "#music-volume", "#ducking", "#subtitles", "#auto-transcript", "#transcription-model"].forEach((selector) => {
      $(selector).addEventListener("input", () => { updateTransitionHint(); updateCaptionControls(); if (state.step === 4) renderReview(); });
      $(selector).addEventListener("change", () => { updateTransitionHint(); updateCaptionControls(); if (state.step === 4) renderReview(); });
    });
    $("#dry-run-button").addEventListener("click", () => startJob("dry-run"));
    $("#render-button").addEventListener("click", () => startJob("render"));
    $("#cancel-button").addEventListener("click", cancelJob);
  }

  async function initialize() {
    bindEvents();
    updateWorkflowUI();
    replanClips();
    updateCaptionControls();
    renderReview();
    await refreshMedia(true);
    await refreshResults();
    try {
      const response = await api("/api/job?cursor=0");
      if (response.job.status === "running") {
        resetConsole();
        updateJobUI(response.job);
        state.pollTimer = window.setInterval(pollJob, 900);
      }
    } catch (_) {
      // Health/media call already reports a useful message if the server is unavailable.
    }
  }

  window.addEventListener("beforeunload", stopPolling);
  document.addEventListener("DOMContentLoaded", initialize);
})();
