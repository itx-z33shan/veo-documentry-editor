/* Queueing rules for "add a whole clips folder" in the finishing dashboard.
 *
 * This file deliberately touches no DOM APIs. The browser loads it as a plain
 * script (window.VeoClipsFolder) and tests/clips_folder_test.js requires it
 * through Node, so the folder rules — what counts as a clip, how clips are
 * ordered, and which filename the dashboard will actually store — stay
 * testable and identical to what src/dashboard.py writes to clips/.
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.VeoClipsFolder = api;
  }
}(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Keep aligned with SUPPORTED_VIDEO_EXTS in src/media.py.
  var VIDEO_EXTENSIONS = [".mp4", ".mov", ".mkv", ".webm", ".m4v"];
  // An optional clips/metadata.json sidecar drives semantic scene matching.
  var METADATA_NAME = "metadata.json";
  var MAX_METADATA_BYTES = 1024 * 1024;
  // Keep aligned with _safe_filename() in src/dashboard.py.
  var MAX_NAME_LENGTH = 180;
  var MAX_CLIPS_PER_QUEUE = 400;

  var IGNORED_NAMES = {
    ".ds_store": true,
    "thumbs.db": true,
    "ehthumbs.db": true,
    "desktop.ini": true,
    "picasa.ini": true,
    ".directory": true,
  };

  var IGNORED_DIRECTORIES = {
    "__macosx": true,
    "@eadir": true,
    ".spotlight-v100": true,
    ".trashes": true,
    ".thumbnails": true,
  };

  function normalizePath(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/\\/g, "/")
      .replace(/^\.\/+/, "")
      .replace(/^\/+|\/+$/g, "");
  }

  function pathParts(value) {
    return normalizePath(value).split("/").filter(function (part) { return part !== ""; });
  }

  function baseOf(value) {
    var parts = pathParts(value);
    return parts.length ? parts[parts.length - 1] : "";
  }

  function parentOf(value) {
    var parts = pathParts(value);
    parts.pop();
    return parts.length ? parts[parts.length - 1] : "";
  }

  function extensionOf(value, preserveCase) {
    var name = baseOf(value);
    var dot = name.lastIndexOf(".");
    if (dot <= 0) return "";
    var extension = name.slice(dot);
    return preserveCase ? extension : extension.toLowerCase();
  }

  function isVideoName(value) {
    return VIDEO_EXTENSIONS.indexOf(extensionOf(value)) !== -1;
  }

  /** True for macOS/Windows clutter that must never be queued as a clip. */
  function isIgnorablePath(value) {
    var parts = pathParts(value);
    if (!parts.length) return true;
    for (var index = 0; index < parts.length - 1; index += 1) {
      if (IGNORED_DIRECTORIES[parts[index].toLowerCase()]) return true;
    }
    var name = parts[parts.length - 1].toLowerCase();
    if (IGNORED_NAMES[name]) return true;
    // Dot files and AppleDouble "._clip_001.mp4" resource forks.
    return name.charAt(0) === "." || name.indexOf("._") === 0;
  }

  /** Mirror of dashboard filename sanitising, keeping the extension intact. */
  function sanitizeClipName(value) {
    var name = baseOf(value);
    // The stored name keeps the writer's own extension case; only the
    // membership tests below compare case-insensitively.
    var extension = extensionOf(name, true);
    var stem = extension && name.length > extension.length
      ? name.slice(0, name.length - extension.length)
      : name;
    stem = stem.replace(/[^A-Za-z0-9. -]+/g, "_").replace(/^[ .]+/g, "").replace(/[ .]+$/g, "");
    extension = extension.replace(/[^A-Za-z0-9.]+/g, "_");
    if (!stem) return "";
    var budget = MAX_NAME_LENGTH - extension.length;
    if (budget < 1) return "";
    if (stem.length > budget) stem = stem.slice(0, budget).replace(/[ .]+$/g, "");
    if (!stem) return "";
    return stem + extension;
  }

  function naturalSegments(value) {
    return String(value).toLowerCase().split(/(\d+)/).filter(function (part) {
      return part !== "";
    });
  }

  /** 001, 002 … 010 order (the same key src/scanner.py uses for clips/). */
  function naturalCompare(left, right) {
    var a = naturalSegments(left);
    var b = naturalSegments(right);
    var length = Math.max(a.length, b.length);
    for (var index = 0; index < length; index += 1) {
      if (a[index] === undefined) return -1;
      if (b[index] === undefined) return 1;
      var aNumber = /^\d+$/.test(a[index]);
      var bNumber = /^\d+$/.test(b[index]);
      if (aNumber && bNumber) {
        var leftValue = Number(a[index]);
        var rightValue = Number(b[index]);
        if (leftValue !== rightValue) return leftValue < rightValue ? -1 : 1;
        continue;
      }
      if (a[index] !== b[index]) return a[index] < b[index] ? -1 : 1;
    }
    return 0;
  }

  function queueKey(relativePath, size) {
    return normalizePath(relativePath) + "|" + (Number(size) || 0);
  }

  function bumpSuffix(name) {
    var extension = extensionOf(name, true);
    var stem = extension && name.length > extension.length
      ? name.slice(0, name.length - extension.length)
      : name;
    var match = /^(.*)_(\d+)$/.exec(stem);
    if (match) {
      return match[1] + "_" + (Number(match[2]) + 1) + extension;
    }
    return stem + "_2" + extension;
  }

  /**
   * Turn raw folder picks into the exact upload queue the dashboard needs.
   *
   * ``entries`` items only need ``name``/``relativePath``/``size``; DOM File
   * objects are kept by the caller because this module has to stay testable.
   * ``options.existing`` maps workspace filename -> byte size so clips that
   * are already saved can be skipped, and ``options.reservedNames`` carries the
   * names that are already taken when the queue is appended instead of
   * replacing.
   */
  function planClipFiles(entries, options) {
    var settings = options || {};
    var limit = settings.limit || MAX_CLIPS_PER_QUEUE;
    var existing = settings.existing || {};
    var taken = {};
    (settings.reservedNames || []).forEach(function (name) {
      if (name) taken[name] = true;
    });

    var queue = [];
    var seen = {};
    var metadata = null;
    var skipped = { notVideo: 0, hidden: 0, empty: 0, oversized: 0 };
    var duplicates = 0;
    var tooLarge = [];

    (entries || []).forEach(function (entry) {
      var relativePath = normalizePath((entry && (entry.relativePath || entry.name)) || "");
      var base = baseOf(relativePath);
      if (!base || isIgnorablePath(relativePath)) {
        skipped.hidden += 1;
        return;
      }
      var size = Number(entry && entry.size) || 0;
      if (size <= 0) {
        skipped.empty += 1;
        return;
      }
      if (base.toLowerCase() === METADATA_NAME) {
        // Only the sidecar at the top of a picked folder is meaningful.
        if (!metadata && pathParts(relativePath).length <= 2 && size <= MAX_METADATA_BYTES) {
          metadata = {
            key: queueKey(relativePath, size),
            relativePath: relativePath,
            name: METADATA_NAME,
            size: size,
          };
        }
        return;
      }
      if (!isVideoName(base)) {
        skipped.notVideo += 1;
        return;
      }
      if (settings.maxFileBytes && size > settings.maxFileBytes) {
        skipped.oversized += 1;
        tooLarge.push(base);
        return;
      }
      var key = queueKey(relativePath, size);
      if (seen[key]) {
        duplicates += 1;
        return;
      }
      seen[key] = true;
      queue.push({
        key: key,
        relativePath: relativePath,
        originalName: base,
        size: size,
      });
    });

    queue.sort(function (a, b) {
      var delta = naturalCompare(a.relativePath, b.relativePath);
      if (delta) return delta;
      // Mixed padding ("clip_1", "clip_001") is numerically equal, so fall
      // back to the raw path to keep the queue order deterministic.
      return a.relativePath < b.relativePath ? -1 : (a.relativePath > b.relativePath ? 1 : 0);
    });

    var renamed = 0;
    var reused = 0;
    queue.forEach(function (item) {
      var clean = sanitizeClipName(item.originalName);
      var sameOnDisk = Object.prototype.hasOwnProperty.call(existing, clean)
        && Number(existing[clean]) === item.size;
      var name = clean;
      if (!sameOnDisk && taken[name]) {
        // The same basename twice in one folder tree would silently overwrite
        // or shuffle clips, so the parent folder keeps them apart.
        var parent = sanitizeClipName(parentOf(item.relativePath));
        if (parent) name = sanitizeClipName(parent + "_" + clean);
      }
      var guard = 0;
      while (!sameOnDisk && taken[name] && guard < 999) {
        name = bumpSuffix(name);
        guard += 1;
      }
      if (!sameOnDisk && taken[name]) name = "";
      if (!name) {
        item.error = "Could not find a free filename for " + item.originalName + ".";
        return;
      }
      taken[name] = true;
      item.name = name;
      item.renamed = name !== clean;
      item.alreadyInWorkspace = sameOnDisk;
      if (item.renamed) renamed += 1;
      if (sameOnDisk) reused += 1;
    });

    var blocked = queue.filter(function (item) { return item.error; });
    var clips = queue.filter(function (item) { return !item.error; });
    var pending = clips.filter(function (item) { return !item.alreadyInWorkspace; });
    var totalBytes = pending.reduce(function (sum, item) { return sum + item.size; }, 0);

    var warnings = [];
    if (skipped.notVideo) {
      warnings.push(skipped.notVideo + " file" + (skipped.notVideo === 1 ? "" : "s")
        + " in the folder " + (skipped.notVideo === 1 ? "was" : "were")
        + " skipped because " + (skipped.notVideo === 1 ? "it is" : "they are")
        + " not a supported video file.");
    }
    if (skipped.hidden) {
      warnings.push(skipped.hidden + " hidden or system file" + (skipped.hidden === 1 ? "" : "s") + " ignored.");
    }
    if (skipped.empty) {
      warnings.push(skipped.empty + " empty file" + (skipped.empty === 1 ? "" : "s") + " ignored.");
    }
    if (tooLarge.length) {
      warnings.push(tooLarge.length + " file" + (tooLarge.length === 1 ? "" : "s")
        + " above the dashboard upload limit skipped: " + tooLarge.slice(0, 3).join(", ") + ".");
    }
    if (renamed) {
      warnings.push(renamed + " clip" + (renamed === 1 ? "" : "s")
        + " renamed with " + (renamed === 1 ? "its" : "their")
        + " parent folder so identical names in different subfolders stay distinct.");
    }
    if (duplicates) {
      warnings.push(duplicates + " already-queued file" + (duplicates === 1 ? "" : "s") + " ignored.");
    }
    if (reused) {
      warnings.push(reused + " clip" + (reused === 1 ? "" : "s")
        + " already in the workspace" + (reused === 1 ? " is" : " are") + " skipped on save.");
    }
    if (metadata) {
      warnings.push("metadata.json sidecar queued for scene matching."
        + (renamed ? " Renamed clips may need their metadata keys updated." : ""));
    }
    if (blocked.length) {
      warnings.push(blocked.length + " clip" + (blocked.length === 1 ? "" : "s")
        + " could not be given a unique filename and were dropped.");
    }

    var error = null;
    if (clips.length > limit) {
      error = "That folder has " + clips.length + " clips; the dashboard queues up to "
        + limit + " at a time. Split the folder into smaller batches.";
    } else if (!clips.length) {
      error = skipped.notVideo || skipped.hidden
        ? "No supported video clips found. The folder must contain MP4, MOV, MKV, WebM, or M4V files."
        : "No clips selected yet.";
    }

    return {
      clips: clips,
      pending: pending,
      metadata: metadata,
      skipped: skipped,
      duplicates: duplicates,
      renamed: renamed,
      reused: reused,
      count: clips.length,
      uploadCount: pending.length,
      totalBytes: totalBytes,
      warnings: warnings,
      error: error,
      limit: limit,
    };
  }

  return {
    VIDEO_EXTENSIONS: VIDEO_EXTENSIONS,
    METADATA_NAME: METADATA_NAME,
    MAX_CLIPS_PER_QUEUE: MAX_CLIPS_PER_QUEUE,
    MAX_METADATA_BYTES: MAX_METADATA_BYTES,
    baseOf: baseOf,
    extensionOf: extensionOf,
    isVideoName: isVideoName,
    isIgnorablePath: isIgnorablePath,
    naturalCompare: naturalCompare,
    queueKey: queueKey,
    sanitizeClipName: sanitizeClipName,
    planClipFiles: planClipFiles,
  };
}));
