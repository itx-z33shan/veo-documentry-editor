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
  };

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
        "clips-file": "Choose one or more clips",
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

  function selectedUploads() {
    const definition = workflowDefinition();
    const uploads = [];
    const addFiles = (field, input) => {
      Array.from(input.files || []).forEach((file) => uploads.push({ field, file, replaceClips: false }));
    };
    if (definition.engine === "master") addFiles("master", $("#master-file"));
    else addFiles("clips", $("#clips-file"));
    addFiles("narration", $("#narration-file"));
    if (state.workflow === "master-rebuild" || state.workflow === "clips-music") addFiles("music", $("#music-file"));
    addFiles("transcript", $("#transcript-file"));

    const firstClip = uploads.find((entry) => entry.field === "clips");
    if (firstClip && $("#replace-clips").checked) firstClip.replaceClips = true;
    return uploads;
  }

  function rawUpload(entry, final, completedBytes, totalBytes, updateProgress) {
    return new Promise((resolve, reject) => {
      const query = new URLSearchParams({
        field: entry.field,
        name: entry.file.name,
        replaceClips: entry.replaceClips ? "true" : "false",
        final: final ? "true" : "false",
      });
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/upload?${query.toString()}`);
      xhr.responseType = "json";
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.addEventListener("progress", (event) => {
        if (!event.lengthComputable) return;
        updateProgress(completedBytes + event.loaded, totalBytes);
      });
      xhr.addEventListener("load", () => {
        if (xhr.status < 200 || xhr.status >= 300 || !xhr.response || !xhr.response.ok) {
          reject(new Error((xhr.response && xhr.response.error) || `Upload failed for ${entry.file.name}.`));
          return;
        }
        resolve(xhr.response);
      });
      xhr.addEventListener("error", () => reject(new Error(`Upload connection failed for ${entry.file.name}.`)));
      xhr.send(entry.file);
    });
  }

  async function uploadSelectedFiles(event) {
    event.preventDefault();
    const form = $("#upload-form");
    const uploads = selectedUploads();
    if (!uploads.length) {
      showFlash("Choose at least one file for the selected workflow.");
      return;
    }

    const button = $("#upload-button");
    const progress = $("#upload-progress");
    const bar = $("#upload-progress-bar");
    const label = $("#upload-progress-label");
    const value = $("#upload-progress-value");
    const totalBytes = uploads.reduce((sum, entry) => sum + entry.file.size, 0);
    let completedBytes = 0;
    let finalResponse = null;
    const updateProgress = (loaded, total) => {
      const percent = total ? Math.max(0, Math.min(100, Math.round(loaded / total * 100))) : 0;
      bar.style.width = `${percent}%`;
      value.textContent = `${percent}%`;
    };

    setBusy(button, true, "Uploading…");
    progress.hidden = false;
    updateProgress(0, totalBytes);
    try {
      for (let index = 0; index < uploads.length; index += 1) {
        const entry = uploads[index];
        label.textContent = `Uploading ${index + 1} of ${uploads.length}: ${entry.file.name}`;
        finalResponse = await rawUpload(entry, index === uploads.length - 1, completedBytes, totalBytes, updateProgress);
        completedBytes += entry.file.size;
        updateProgress(completedBytes, totalBytes);
      }
      value.textContent = "Saved";
      label.textContent = "Assets saved locally and inspected.";
      if (finalResponse && finalResponse.media) renderMedia(finalResponse.media);
      else await refreshMedia(true);
      form.reset();
      $$("input[type=file]", form).forEach(updateFileChoice);
      showFlash("Assets are in the local workspace. Review the inspection below.", "success");
    } catch (error) {
      showFlash(error.message);
    } finally {
      setBusy(button, false);
    }
  }

  function currentSettings() {
    return {
      loudnessTarget: Number($("#loudness-target").value),
      truePeak: Number($("#true-peak").value),
      aacBitrate: Number($("#aac-bitrate").value),
      masterFade: Number($("#master-fade").value),
      subtitles: $("#subtitles").checked,
      keepClipAudio: $("#keep-clip-audio").checked,
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
      embedded: "Quiet embedded clip audio under narration",
      music: "Separate music ducked under narration",
    }[definition.audioMode];
    const transition = definition.engine === "clips" ? (settings.keepClipAudio && settings.transition === "crossfade" ? "Hard cut (embedded-audio protection)" : settings.transition === "cut" ? "Hard cuts" : "Crossfade") : `${settings.masterFade.toFixed(2)} sec opening/closing fade`;
    $("#review-summary").innerHTML = `<div class="review-lines">
      <div class="review-line"><span>Workflow</span><b>${escapeHtml(definition.title)}</b></div>
      <div class="review-line"><span>Audio route</span><b>${escapeHtml(audioPlan)}</b></div>
      <div class="review-line"><span>Delivery</span><b>YouTube + Facebook / 1080p</b></div>
      <div class="review-line"><span>Final target</span><b>${settings.loudnessTarget} LUFS · ${settings.truePeak} dBTP</b></div>
      <div class="review-line"><span>Transitions / fade</span><b>${escapeHtml(transition)}</b></div>
      <div class="review-line"><span>Captions</span><b>${settings.subtitles ? "SRT sidecar" : "No captions"}</b></div>
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
    $("#job-stage").textContent = status === "running" ? "FFmpeg / editor working locally" : status === "succeeded" ? "Finished" : job.error || "No process active";
    if (status === "running") led.classList.add("is-running");
    if (status === "succeeded") led.classList.add("is-good");
    if (status === "failed" || status === "cancelled") led.classList.add("is-bad");
    $("#cancel-button").hidden = status !== "running";
    if (status !== "running" && job.action === "dry-run") setBusy($("#dry-run-button"), false);
    if (status !== "running" && job.action === "render") setBusy($("#render-button"), false);

    if (status === "succeeded" && job.action === "dry-run") {
      state.dryRunSucceeded = true;
      $("#render-button").disabled = false;
      showFlash("Dry run passed. Review the warnings and start the final render when ready.", "success");
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
  }

  async function refreshResults() {
    try {
      const response = await api("/api/results");
      renderResults(response.files || []);
    } catch (error) {
      showFlash(error.message);
    }
  }

  function bindEvents() {
    $$(".workflow-card").forEach((card) => card.addEventListener("click", () => selectWorkflow(card.dataset.workflow)));
    $$('[data-next-step]').forEach((button) => button.addEventListener("click", () => goStep(button.dataset.nextStep)));
    $$('[data-prev-step]').forEach((button) => button.addEventListener("click", () => goStep(button.dataset.prevStep)));
    $$('[data-go-step]').forEach((button) => button.addEventListener("click", () => goStep(button.dataset.goStep)));
    $$("input[type=file]").forEach((input) => input.addEventListener("change", () => updateFileChoice(input)));
    $$(".upload-card").forEach((card) => {
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
    $("#upload-form").addEventListener("submit", uploadSelectedFiles);
    $("#refresh-media").addEventListener("click", () => refreshMedia());
    $("#refresh-results").addEventListener("click", refreshResults);
    $("#master-fade").addEventListener("input", (event) => {
      $("#fade-output").textContent = `${Number(event.target.value).toFixed(2)} sec`;
      if (state.step === 4) renderReview();
    });
    ["#loudness-target", "#true-peak", "#aac-bitrate", "#keep-clip-audio", "#clip-audio-volume", "#transition", "#music-volume", "#ducking", "#subtitles"].forEach((selector) => {
      $(selector).addEventListener("input", () => { updateTransitionHint(); if (state.step === 4) renderReview(); });
      $(selector).addEventListener("change", () => { updateTransitionHint(); if (state.step === 4) renderReview(); });
    });
    $("#dry-run-button").addEventListener("click", () => startJob("dry-run"));
    $("#render-button").addEventListener("click", () => startJob("render"));
    $("#cancel-button").addEventListener("click", cancelJob);
  }

  async function initialize() {
    bindEvents();
    updateWorkflowUI();
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
