const endpoint = async (path, options = {}) => {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(renderedState?.project_id?{"X-Project-ID":renderedState.project_id}:{}), ...(options.headers || {}) },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Request failed. Please try again later.");
  return body;
};

const EVALUATION_STATES = new Set(["ready_for_evaluation", "awaiting_repair_upload", "repair_uploaded", "published", "stopped_budget_exhausted"]);
const MAX_VIDEO_DURATION_SECONDS = 30;
const DURATION_PATTERN = /(?<!\d)(\d+(?:\.\d+)?)\s*(?:-\s*)?(seconds?|secs?|s|秒|minutes?|mins?|分钟|分)(?![a-z])/gi;
let selectedReferenceItems = [];
let qualityLoopPending = false;
let qualityProgressTimer = null;
let qualityProgressVisible = false;
let selectedReviewVersionId = null;
let lastCurrentVersionId = null;
let renderedState = null;
let generationPollTimer = null;
let generatedEvidencePending = false;
let autopilotAdvancePending = false;
let activeAudioReferencePreview = null;
let activeImageReferencePreview = null;

const human = value => String(value ?? "").replaceAll("_", " ").replace(/\b\w/g, char => char.toUpperCase());
const escapeHTML = value => String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);

const isImageReference = file => file?.type?.startsWith("image/");
const isVideoReference = file => file?.type?.startsWith("video/");
const isAudioReference = file => ["audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav"].includes(file?.type);
const referenceFileKey = file => `${file.name}:${file.size}:${file.lastModified}:${file.type}`;
const mentionLabel = file => `@${file.name}`;
const promptEditor = () => document.querySelector("#creative-prompt");

function serializeEditorNode(node) {
  if (node.nodeType === Node.TEXT_NODE) return node.nodeValue || "";
  if (node.nodeType !== Node.ELEMENT_NODE) return "";
  if (node.classList.contains("inline-reference-token")) return node.dataset.mention || "";
  const children = Array.from(node.childNodes).map(serializeEditorNode).join("");
  return node.tagName === "DIV" || node.tagName === "P" ? `${children}\n` : children;
}

function editorText() {
  return Array.from(promptEditor().childNodes).map(serializeEditorNode).join("").replace(/\u00a0/g, " ");
}

function setEditorText(value = "") {
  promptEditor().textContent = value;
}

function requestedDurations(text) {
  DURATION_PATTERN.lastIndex = 0;
  return Array.from(text.matchAll(DURATION_PATTERN), match => {
    const amount = Number(match[1]);
    const unit = match[2].toLowerCase();
    const seconds = Math.max(1, Math.round((unit.startsWith("m") || unit === "分钟" || unit === "分") ? amount * 60 : amount));
    return { start: match.index, end: match.index + match[0].length, raw: match[0], seconds };
  });
}

function overLimitDurations(text) {
  return requestedDurations(text).filter(item => item.seconds > MAX_VIDEO_DURATION_SECONDS);
}

function hideDurationValidationPopover() {
  document.querySelector("#duration-validation-popover")?.remove();
}

function showDurationValidationPopover(marker) {
  hideDurationValidationPopover();
  if (!marker) return;
  const rect = marker.getBoundingClientRect();
  const popover = document.createElement("div");
  popover.id = "duration-validation-popover";
  popover.className = "duration-validation-popover";
  popover.setAttribute("role", "alert");
  popover.innerHTML = `<span>单条视频最长 ${MAX_VIDEO_DURATION_SECONDS} 秒，请修改此时长。</span><button type="button" aria-label="关闭时长提示">×</button>`;
  document.body.append(popover);
  const popoverRect = popover.getBoundingClientRect();
  const left = Math.max(12, Math.min(rect.left, window.innerWidth - popoverRect.width - 12));
  const top = rect.bottom + popoverRect.height + 12 > window.innerHeight
    ? Math.max(12, rect.top - popoverRect.height - 10)
    : rect.bottom + 10;
  popover.style.left = `${left}px`;
  popover.style.top = `${top}px`;
  popover.querySelector("button").addEventListener("click", hideDurationValidationPopover);
}

function highlightOverLimitDurations() {
  const editor = promptEditor();
  // Restore visual @ tokens first; only plain text portions are decorated.
  // The submitted serialised brief remains byte-for-byte unchanged.
  renderEditorWithMentionTokens(editorText());
  const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let firstMarker = null;
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach(node => {
    const matches = overLimitDurations(node.nodeValue || "");
    matches.reverse().forEach(match => {
      const range = document.createRange();
      range.setStart(node, match.start);
      range.setEnd(node, match.end);
      const marker = document.createElement("mark");
      marker.className = "duration-overlimit";
      marker.title = `Video duration cannot exceed ${MAX_VIDEO_DURATION_SECONDS} seconds`;
      range.surroundContents(marker);
      firstMarker ||= marker;
    });
  });
  return firstMarker;
}

function editorCursorOffset() {
  const editor = promptEditor();
  const selection = window.getSelection();
  if (!selection?.rangeCount || !editor.contains(selection.anchorNode)) return editorText().length;
  const range = selection.getRangeAt(0).cloneRange();
  range.selectNodeContents(editor);
  range.setEnd(selection.anchorNode, selection.anchorOffset);
  const fragment = range.cloneContents();
  return Array.from(fragment.childNodes).map(serializeEditorNode).join("").length;
}

function setEditorCursorAfter(node) {
  const range = document.createRange();
  range.setStartAfter(node);
  range.collapse(true);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
}

function makeMentionToken(file) {
  const token = document.createElement("span");
  token.className = `inline-reference-token ${isAudioReference(file) ? "is-audio" : "is-image"}`;
  token.contentEditable = "false";
  token.dataset.mention = mentionLabel(file);
  token.title = mentionLabel(file);
  if (isImageReference(file)) {
    const preview = document.createElement("img");
    preview.src = URL.createObjectURL(file);
    preview.alt = mentionLabel(file);
    preview.onload = () => URL.revokeObjectURL(preview.src);
    token.append(preview);
  } else {
    token.textContent = "♫";
  }
  return token;
}

function removeInlineMentionToken(token) {
  if (!(token instanceof Element) || !token.classList.contains("inline-reference-token")) return;
  const editor = promptEditor();
  const cursor = document.createTextNode("");
  token.before(cursor);
  const following = token.nextSibling;
  // Inserted mentions add a trailing space for readable prose. Remove only
  // that one separator when unlinking, never the user's actual text.
  if (following?.nodeType === Node.TEXT_NODE && following.nodeValue?.startsWith(" ")) {
    following.nodeValue = following.nodeValue.slice(1);
  }
  token.remove();
  editor.focus();
  setEditorCursorAfter(cursor);
  updateMentionMenu();
}

function removeAllMentionsForReference(file) {
  const label = mentionLabel(file);
  const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // Serialisation makes every visual token become its @filename marker. Once
  // the asset is deleted, remove every such marker before tokens are rebuilt.
  const nextText = editorText().replace(new RegExp(`${escaped}(?:[ \\t]+)?`, "g"), "");
  renderEditorWithMentionTokens(nextText);
}

function adjacentMentionToken(direction) {
  const editor = promptEditor();
  const selection = window.getSelection();
  if (!selection?.rangeCount || !selection.isCollapsed || !editor.contains(selection.anchorNode)) return null;
  const node = selection.anchorNode;
  const offset = selection.anchorOffset;
  let candidate = null;
  if (node.nodeType === Node.TEXT_NODE) {
    if (direction === "backward" && offset === 0) candidate = node.previousSibling;
    if (direction === "forward" && offset === (node.nodeValue || "").length) candidate = node.nextSibling;
  } else if (node === editor) {
    candidate = direction === "backward" ? editor.childNodes[offset - 1] : editor.childNodes[offset];
  }
  return candidate instanceof Element && candidate.classList.contains("inline-reference-token") ? candidate : null;
}

function renderEditorWithMentionTokens(text, targetMention = "") {
  const editor = promptEditor();
  const references = [...selectedReferenceItems].sort((a, b) => mentionLabel(b).length - mentionLabel(a).length);
  if (!references.length) {
    editor.textContent = text;
    return null;
  }
  const labels = references.map(file => mentionLabel(file).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pattern = new RegExp(labels.join("|"), "g");
  const fragment = document.createDocumentFragment();
  let lastIndex = 0;
  let target = null;
  for (const match of text.matchAll(pattern)) {
    fragment.append(document.createTextNode(text.slice(lastIndex, match.index)));
    const file = references.find(item => mentionLabel(item) === match[0]);
    const token = makeMentionToken(file);
    if (!target && match[0] === targetMention) target = token;
    fragment.append(token);
    lastIndex = match.index + match[0].length;
  }
  fragment.append(document.createTextNode(text.slice(lastIndex)));
  editor.replaceChildren(fragment);
  return target;
}

function insertMentionAtCursor(file, replaceMention = false) {
  const text = editorText();
  const cursor = editorCursorOffset();
  const mention = replaceMention ? activeMentionQuery() : null;
  const start = mention ? mention.start : cursor;
  const prefix = !mention && start > 0 && !/\s/.test(text.charAt(start - 1)) ? " " : "";
  const label = mentionLabel(file);
  const nextText = `${text.slice(0, start)}${prefix}${label} ${text.slice(cursor)}`;
  const token = renderEditorWithMentionTokens(nextText, label);
  promptEditor().focus();
  if (token) setEditorCursorAfter(token.nextSibling);
  hideMentionMenu();
}

function insertEditorText(text) {
  const editor = promptEditor();
  const selection = window.getSelection();
  const range = selection?.rangeCount ? selection.getRangeAt(0) : null;
  if (!range || !editor.contains(range.commonAncestorContainer)) {
    editor.append(document.createTextNode(text));
    setEditorCursorAfter(editor.lastChild);
    return;
  }
  range.deleteContents();
  const textNode = document.createTextNode(text);
  range.insertNode(textNode);
  setEditorCursorAfter(textNode);
}

function activeMentionQuery() {
  const beforeCursor = editorText().slice(0, editorCursorOffset());
  // Chinese prompts commonly attach a reference directly to a noun (for
  // example, "人物@角色图"). The active mention is therefore the last @ token
  // before the cursor, not only an @ preceded by whitespace.
  const match = beforeCursor.match(/@([^\s@]*)$/);
  return match ? { query: match[1].toLowerCase(), start: beforeCursor.length - match[1].length - 1 } : null;
}

function hideMentionMenu() {
  const menu = document.querySelector("#reference-mention-menu");
  menu.hidden = true;
  menu.innerHTML = "";
}

function chooseMention(file) {
  if (!activeMentionQuery()) return hideMentionMenu();
  insertMentionAtCursor(file, true);
}

function updateMentionMenu() {
  const menu = document.querySelector("#reference-mention-menu");
  const mention = activeMentionQuery();
  if (!mention || !selectedReferenceItems.length) return hideMentionMenu();
  const matches = selectedReferenceItems.filter(file => file.name.toLowerCase().includes(mention.query));
  if (!matches.length) return hideMentionMenu();
  menu.innerHTML = "";
  matches.forEach(file => {
    const option = document.createElement("button");
    option.type = "button";
    option.className = `reference-mention-option ${isAudioReference(file) ? "is-audio" : "is-image"}`;
    option.setAttribute("role", "option");
    option.title = mentionLabel(file);
    if (isImageReference(file)) {
      const preview = document.createElement("img");
      preview.src = URL.createObjectURL(file);
      preview.alt = mentionLabel(file);
      preview.onload = () => URL.revokeObjectURL(preview.src);
      option.append(preview);
    } else {
      const icon = document.createElement("span");
      icon.className = "mention-option-audio";
      icon.setAttribute("aria-label", `Audio reference ${mentionLabel(file)}`);
      icon.textContent = "♫";
      option.append(icon);
    }
    option.addEventListener("mousedown", event => event.preventDefault());
    option.addEventListener("click", () => chooseMention(file));
    menu.append(option);
  });
  menu.hidden = false;
}

function insertMentionFromAttachment(file) {
  insertMentionAtCursor(file);
}

function promptMentionsFor(file, prompt) {
  const escaped = file.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`@${escaped}(?=\\s|$|[,.!?，。！？])`, "i").test(prompt) ? [mentionLabel(file)] : [];
}

function stopAudioReferencePreview() {
  if (!activeAudioReferencePreview) return;
  activeAudioReferencePreview.audio.pause();
  activeAudioReferencePreview.audio.currentTime = 0;
  activeAudioReferencePreview.card.classList.remove("is-previewing");
  activeAudioReferencePreview = null;
}

function previewAudioReference(audio, card) {
  if (activeAudioReferencePreview?.audio === audio) return;
  stopAudioReferencePreview();
  activeAudioReferencePreview = { audio, card };
  card.classList.add("is-previewing");
  audio.currentTime = 0;
  audio.play().catch(() => {
    // Some browsers require an earlier interaction before audible hover
    // preview. Keep the card honest rather than pretending it is playing.
    if (activeAudioReferencePreview?.audio !== audio) return;
    card.classList.remove("is-previewing");
    card.classList.add("preview-needs-gesture");
    activeAudioReferencePreview = null;
  });
}

function hideImageReferencePreview() {
  if (!activeImageReferencePreview) return;
  const { panel, objectUrl } = activeImageReferencePreview;
  panel.remove();
  URL.revokeObjectURL(objectUrl);
  activeImageReferencePreview = null;
}

function positionImageReferencePreview(panel, card) {
  if (!panel.isConnected) return;
  const cardRect = card.getBoundingClientRect();
  const panelRect = panel.getBoundingClientRect();
  const gap = 8;
  const left = Math.max(8, Math.min(
    cardRect.left + (cardRect.width - panelRect.width) / 2,
    window.innerWidth - panelRect.width - 8,
  ));
  panel.style.left = `${left}px`;
  panel.style.top = `${Math.max(8, cardRect.top - panelRect.height - gap)}px`;
}

function previewImageReference(file, card) {
  hideImageReferencePreview();
  const panel = document.createElement("div");
  panel.className = "reference-image-preview";
  panel.setAttribute("aria-hidden", "true");
  const image = document.createElement("img");
  const objectUrl = URL.createObjectURL(file);
  image.src = objectUrl;
  image.alt = "";
  panel.append(image);
  document.body.append(panel);
  activeImageReferencePreview = { panel, objectUrl };
  positionImageReferencePreview(panel, card);
  image.addEventListener("load", () => {
    if (activeImageReferencePreview?.panel === panel) positionImageReferencePreview(panel, card);
  });
}

function renderReferenceAttachments() {
  const list = document.querySelector("#reference-attachment-list");
  stopAudioReferencePreview();
  hideImageReferencePreview();
  list.innerHTML = "";
  selectedReferenceItems.forEach((file, index) => {
    const item = document.createElement("article");
    item.className = `reference-attachment ${isAudioReference(file) ? "is-audio" : "is-image"}`;
    if (!isImageReference(file)) item.title = file.name;
    item.tabIndex = 0;
    item.setAttribute("role", "button");
    item.setAttribute("aria-label", `Insert ${mentionLabel(file)} into the brief`);
    item.addEventListener("click", event => {
      if (!event.target.closest(".reference-attachment-remove")) insertMentionFromAttachment(file);
    });
    item.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        insertMentionFromAttachment(file);
      }
    });
    if (isImageReference(file)) {
      const preview = document.createElement("img");
      preview.src = URL.createObjectURL(file);
      preview.alt = file.name;
      preview.onload = () => URL.revokeObjectURL(preview.src);
      item.append(preview);
      item.addEventListener("mouseenter", () => previewImageReference(file, item));
      item.addEventListener("mouseleave", hideImageReferencePreview);
    } else if (isVideoReference(file)) {
      const icon = document.createElement("span");icon.textContent = "▶";icon.className = "reference-video-icon";
      item.title = file.name;item.append(icon);
    } else {
      const icon = document.createElement("span");
      icon.className = "reference-audio-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = "♫";
      item.append(icon);
      const equalizer = document.createElement("span");
      equalizer.className = "reference-audio-equalizer";
      equalizer.setAttribute("aria-hidden", "true");
      equalizer.innerHTML = "<i></i><i></i><i></i>";
      item.append(equalizer);
      const audio = document.createElement("audio");
      audio.preload = "metadata";
      audio.src = URL.createObjectURL(file);
      audio.addEventListener("ended", stopAudioReferencePreview);
      audio.addEventListener("error", () => {
        item.classList.remove("is-previewing");
        item.classList.add("preview-unavailable");
        if (activeAudioReferencePreview?.audio === audio) activeAudioReferencePreview = null;
      });
      item.append(audio);
      item.title = `${file.name} · Hover to preview audio · Click to insert ${mentionLabel(file)}`;
      item.addEventListener("pointerenter", () => previewAudioReference(audio, item));
      item.addEventListener("pointerleave", stopAudioReferencePreview);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "reference-attachment-remove";
    remove.setAttribute("aria-label", `Remove ${file.name}`);
    remove.title = "Remove reference";
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      if (activeAudioReferencePreview?.card === item) stopAudioReferencePreview();
      removeAllMentionsForReference(file);
      selectedReferenceItems.splice(index, 1);
      renderReferenceAttachments();
      updateMentionMenu();
    });
    item.append(remove);
    list.append(item);
  });
}

function clearReferenceAttachments() {
  selectedReferenceItems = [];
  document.querySelector("#reference-media").value = "";
  renderReferenceAttachments();
  hideMentionMenu();
}

// The strip stays visually clean: a vertical mouse wheel over it pans the
// horizontal reference sequence instead of exposing a permanent scrollbar.
document.addEventListener("wheel", event => {
  const strip = event.target.closest?.("#reference-attachment-list");
  if (!strip || Math.abs(event.deltaY) <= Math.abs(event.deltaX) || strip.scrollWidth <= strip.clientWidth) return;
  strip.scrollLeft += event.deltaY;
  event.preventDefault();
}, { passive: false });

function readReferenceFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const image = new Image();
        image.src = reader.result;
        await new Promise((imageResolve, imageReject) => {
          image.onload = imageResolve;
          image.onerror = () => imageReject(new Error("Unable to prepare the reference image"));
        });
        const canvas = document.createElement("canvas");
        const context = canvas.getContext("2d", { alpha: false });
        let dataUrl = "";
        for (const [maxDimension, quality] of [[1024, 0.8], [768, 0.72], [512, 0.64]]) {
          const scale = Math.min(1, maxDimension / Math.max(image.naturalWidth, image.naturalHeight));
          canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
          canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
          context.fillStyle = "#ffffff";
          context.fillRect(0, 0, canvas.width, canvas.height);
          context.drawImage(image, 0, 0, canvas.width, canvas.height);
          dataUrl = canvas.toDataURL("image/jpeg", quality);
          if (dataUrl.length <= 1_200_000) break;
        }
        resolve({ name: file.name, mime_type: "image/jpeg", size: Math.round(dataUrl.length * 0.75), data_url: dataUrl });
      } catch (error) {
        reject(error);
      }
    };
    reader.onerror = () => reject(new Error("Unable to read the reference image"));
    reader.readAsDataURL(file);
  });
}

function readAudioReferenceFile(file) {
  return new Promise(async (resolve, reject) => {
    if (!file || !["audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav"].includes(file.type)) {
      reject(new Error("Use an MP3 or WAV audio reference."));
      return;
    }
    if (file.size > 9 * 1024 * 1024) {
      reject(new Error("Each audio reference must be under 9 MB."));
      return;
    }
    let durationSeconds;
    try {
      durationSeconds = await readAudioDuration(file);
    } catch (error) {
      reject(error);
      return;
    }
    if (durationSeconds < 2 || durationSeconds > 30) {
      reject(new Error(`Audio references must be 2–30 seconds. ${file.name} is ${durationSeconds.toFixed(1)} seconds.`));
      return;
    }
    const reader = new FileReader();
    reader.onload = () => resolve({
      name: file.name,
      mime_type: file.type,
      size: file.size,
      data_url: reader.result,
      role: "Audio reference",
      duration_seconds: Math.round(durationSeconds * 100) / 100,
    });
    reader.onerror = () => reject(new Error("Unable to read the audio reference."));
    reader.readAsDataURL(file);
  });
}

function readAudioDuration(file) {
  return new Promise((resolve, reject) => {
    const audio = document.createElement("audio");
    const url = URL.createObjectURL(file);
    const cleanUp = () => {
      audio.removeAttribute("src");
      audio.load();
      URL.revokeObjectURL(url);
    };
    audio.preload = "metadata";
    audio.onloadedmetadata = () => {
      const duration = Number(audio.duration);
      cleanUp();
      if (!Number.isFinite(duration) || duration <= 0) {
        reject(new Error(`Unable to read the duration of ${file.name}. Choose a valid MP3 or WAV file.`));
        return;
      }
      resolve(duration);
    };
    audio.onerror = () => {
      cleanUp();
      reject(new Error(`Unable to read the duration of ${file.name}. Choose a valid MP3 or WAV file.`));
    };
    audio.src = url;
  });
}

function waitForVideoEvent(video, event) {
  return new Promise((resolve, reject) => {
    const onSuccess = () => { cleanup(); resolve(); };
    const onError = () => { cleanup(); reject(new Error("This browser could not read the selected video.")); };
    const cleanup = () => {
      video.removeEventListener(event, onSuccess);
      video.removeEventListener("error", onError);
    };
    video.addEventListener(event, onSuccess, { once: true });
    video.addEventListener("error", onError, { once: true });
  });
}

function adaptiveEvidencePlan(duration, policy = {}) {
  const windows = Array.isArray(policy.shot_windows) ? policy.shot_windows : [];
  const storyboardIds = windows
    .filter(item => item && /^S[1-6]$/.test(String(item.shot_id))
      && Number.isFinite(Number(item.start_seconds)) && Number.isFinite(Number(item.end_seconds))
      && Number(item.end_seconds) > Number(item.start_seconds))
    .map(item => String(item.shot_id));
  const validShotIds = new Set(storyboardIds.length ? storyboardIds : ["S1"]);
  const requestedFocus = Array.isArray(policy.focus_shot_ids)
    ? policy.focus_shot_ids.filter(shotId => validShotIds.has(shotId))
    : [];
  const focusShots = requestedFocus.length ? requestedFocus : [storyboardIds.at(-1) || "S1"];
  const criticalMoments = Array.isArray(policy.critical_moments) ? policy.critical_moments : [];
  const reasons = new Map(criticalMoments.map(item => [item.shot_id, item.reason]).filter(([shotId, reason]) => validShotIds.has(shotId) && typeof reason === "string"));
  const windowByShot = new Map(windows
    .filter(item => item && validShotIds.has(item.shot_id)
      && Number.isFinite(Number(item.start_seconds)) && Number.isFinite(Number(item.end_seconds))
      && Number(item.end_seconds) > Number(item.start_seconds))
    .map(item => [item.shot_id, { start: Number(item.start_seconds), end: Number(item.end_seconds) }]));
  const plannedDuration = Number(policy.planned_duration_seconds);
  const durationBudget = duration <= 4 ? 4 : duration <= 12 ? 5 : 6;
  const frameBudget = Math.min(6, Math.max(durationBudget, storyboardIds.length || 1, 3 + Math.min(3, criticalMoments.length)));
  // A missing policy is a degraded single-beat fallback, never a hidden
  // three-beat assumption. Normally every ID comes from the approved plan.
  const baselineShotIds = storyboardIds.length ? storyboardIds : ["S1"];
  const counts = Object.fromEntries(baselineShotIds.map(shotId => [shotId, 1]));
  for (let index = 0; index < frameBudget - baselineShotIds.length; index += 1) {
    const shotId = focusShots[index % focusShots.length] || baselineShotIds.at(-1);
    counts[shotId] += 1;
  }
  const anchors = {
    1: [0.5],
    2: [0.28, 0.72],
    3: [0.2, 0.5, 0.8],
  };
  // A planned window is in the Agent's intended timeline. Scale it to the
  // actual generated duration so a provider's small duration drift does not
  // make evidence extraction fail or silently revert to global percentages.
  if (windowByShot.size === baselineShotIds.length && Number.isFinite(plannedDuration) && plannedDuration > 0) {
    return baselineShotIds.flatMap(shotId => {
      const window = windowByShot.get(shotId);
      const positions = anchors[counts[shotId]] || anchors[3];
      return positions.map(position => {
        const plannedTime = window.start + (window.end - window.start) * position;
        return {
          ratio: Math.min(0.99, Math.max(0.01, plannedTime / plannedDuration)),
          expected_shot_id: shotId,
          sampling_reason: reasons.has(shotId)
            ? `Timeline evidence for ${shotId} (${window.start}–${window.end}s): ${reasons.get(shotId)}`
            : `Timeline baseline for ${shotId} (${window.start}–${window.end}s).`,
        };
      });
    }).sort((left, right) => left.ratio - right.ratio);
  }
  return baselineShotIds.flatMap((shotId, index) => {
    const count = counts[shotId];
    // Evenly distribute evidence within each planned beat when timing windows
    // are unavailable. This handles any 1–6 beat plan, including S4–S6.
    const perBeat = Array.from({ length: count }, (_, sampleIndex) =>
      Math.min(.94, Math.max(.06, (index + (sampleIndex + 1) / (count + 1)) / baselineShotIds.length))
    );
    return perBeat.map(ratio => ({
    ratio,
    expected_shot_id: shotId,
    sampling_reason: reasons.has(shotId)
      ? `Risk-focused evidence: ${reasons.get(shotId)}`
      : `Baseline evidence for ${shotId}.`,
  })); }).sort((left, right) => left.ratio - right.ratio);
}

async function extractEvaluationFrames(file, evidenceSampling) {
  const url = URL.createObjectURL(file);
  const video = document.createElement("video");
  video.preload = "metadata";
  video.muted = true;
  video.playsInline = true;
  video.src = url;
  try {
    await waitForVideoEvent(video, "loadedmetadata");
    if (!Number.isFinite(video.duration) || video.duration <= 0) throw new Error("The video duration could not be read.");
    const evidencePlan = adaptiveEvidencePlan(video.duration, evidenceSampling);
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d", { alpha: false });
    const frames = [];
    for (let index = 0; index < evidencePlan.length; index += 1) {
      const sample = evidencePlan[index];
      const timestamp = Math.min(video.duration - 0.02, Math.max(0, video.duration * sample.ratio));
      video.currentTime = timestamp;
      await waitForVideoEvent(video, "seeked");
      // 512px preserves product/identity evidence far better than the old 320px
      // while remaining small enough for a short interview-demo evaluation call.
      const scale = Math.min(1, 512 / Math.max(video.videoWidth || 512, video.videoHeight || 512));
      canvas.width = Math.max(1, Math.round((video.videoWidth || 320) * scale));
      canvas.height = Math.max(1, Math.round((video.videoHeight || 180) * scale));
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
      frames.push({
        frame_id: `F${index + 1}`,
        timestamp: Number(timestamp.toFixed(2)),
        expected_shot_id: sample.expected_shot_id,
        sampling_reason: sample.sampling_reason,
        data_url: canvas.toDataURL("image/jpeg", 0.68),
      });
    }
    return frames;
  } finally {
    video.removeAttribute("src");
    video.load();
    URL.revokeObjectURL(url);
  }
}

function projectStage(status) {
  if (status === "ready_for_brief") return "brief";
  if (status === "awaiting_context_confirmation") return "context";
  if (["planning_ready", "needs_segmentation"].includes(status)) return "plan";
  if (["awaiting_first_video", "generating_video", "generated_video_ready"].includes(status)) return "version";
  return "quality";
}

function renderProjectStatus(state) {
  const characterPlan=state.creative_spec.character_reference_plan;
  const stage = ['planning_ready','awaiting_first_video'].includes(state.status)&&characterPlan?.required ? 'characters' : projectStage(state.status);
  const labels = {
    brief: ["Ready for a creative brief", "Describe the video to the Creative Agent to begin the controlled production loop.", "Brief", "Open Creative Agent"],
    context: ["Creative Agent needs one decision", "A missing context field could change a high-impact generation constraint.", "Context", "Answer the Agent"],
    plan: state.status === "needs_segmentation"
      ? ["Multi-segment plan required", "The Agent determined this brief should not be compressed into one 30-second clip. Revise the brief or plan separate segments.", "Segment plan", "Revise brief"]
      : ["Control plan ready for approval", "Review the Agent's planned shots, constraints, risks, and bounded repair budget.", "Plan ready", "Approve control plan"],
    characters: ["Character reference stage", "Prepare three views of the important characters before video generation.", "Character references", "Generate three views"],
    version: ["Video version in production", "The approved plan is being rendered or prepared as evidence for evaluation.", "Version", "Wait for video evidence"],
    quality: ["Quality control loop active", "Review the selected version, its evidence-backed quality result, and the Agent's next decision.", "Quality", "Review version result"],
  };
  if(characterPlan?.status==='ready')labels.characters=["Control plan ready for approval","Character references attached to video","Plan ready","Generate V1"];
  const [title, copy, badge, next] = labels[stage];
  document.querySelector("#project-status-title").textContent = title;
  document.querySelector("#project-status-copy").textContent = copy;
  document.querySelector("#project-status-badge").textContent = badge;
  document.querySelector("#project-current-version").textContent = state.current_version_id === "none" ? "No version yet" : state.current_version_id.toUpperCase();
  const latestAsset = state.current_video_asset || (state.creative_spec || {}).reference_asset;
  document.querySelector("#project-latest-asset").textContent = latestAsset?.name || "Awaiting input";
  document.querySelector("#project-next-action").textContent = next;
  const sequence = ["brief", "context", "plan", "characters", "version", "quality"];
  const stageIndex = sequence.indexOf(stage);
  document.querySelectorAll(".project-steps li").forEach((item, index) => {
    item.classList.toggle("active", item.dataset.stage === stage);
    item.classList.toggle("done", index < stageIndex);
  });
  renderExecutionTrace(state);
}

function executionTraceEntries(state) {
  const spec = state.creative_spec || {};
  const context = spec.enriched_context || {};
  const entries = [];
  if (spec.source_prompt || spec.goal) {
    entries.push({
      kind: "brief",
      title: "Brief accepted",
      detail: "Creative constraints were retained as the source for the control plan.",
    });
  }
  const completedTools = (context.tool_observations || []).filter(item => item?.status === "completed");
  if (completedTools.length) {
    entries.push({
      kind: "tool",
      title: "Context tools checked",
      detail: `Executed ${completedTools.map(item => human(item.tool)).join(", ")}. Only completed tool observations can affect planning.`,
    });
  }
  const binding = spec.generation_contract?.provider_binding;
  if (binding?.capability_profile?.profile_id) {
    const reference = spec.generation_contract?.reference_strategy || {};
    const mode = human(binding.intended_mode || "unavailable");
    const referenceNote = reference.mode === "reference_retained_not_applied"
      ? " The reference is retained for planning and evaluation, not sent to this adapter."
      : "";
    entries.push({
      kind: "plan",
      title: "Control plan compiled",
      detail: `${mode} · ${binding.tool_name || "No provider tool"} · ${binding.capability_profile.profile_id}.${referenceNote}`,
    });
  }
  const job = (state.generation_jobs || []).at(-1);
  if (job) {
    entries.push({
      kind: "act",
      title: `Video tool ${job.status || "queued"}`,
      detail: `${human(job.request_mode || job.tool_name || "video_generation")} via ${job.model_id || "configured adapter"}. ${job.selection_reason || ""}`.trim(),
    });
  }
  const evidenceEvent = [...(state.history || [])].reverse().find(item => item?.event === "generated_video_evidence_ready");
  if (evidenceEvent) {
    entries.push({
      kind: "observe",
      title: "Risk-aware evidence sampled",
      detail: evidenceEvent.payload?.message || "Keyframes were sampled according to the compiled evidence policy.",
    });
  }
  const report = state.latest_report;
  if (report) {
    entries.push({
      kind: "evaluate",
      title: "Quality result validated",
      detail: `${human(report.release_decision || "insufficient_evidence")} · ${report.rubric_profile?.label || "selected rubric"}. ${report.summary || ""}`,
    });
  }
  const decision = state.latest_decision;
  if (decision) {
    entries.push({
      kind: "decide",
      title: `Agent decided: ${human(decision.action)}`,
      detail: `${decision.reason || "Evidence-backed next action recorded."} Tool: ${human(decision.tool_name || "not specified")}.`,
    });
  }
  return entries;
}

function renderExecutionTrace(state) {
  const entries = executionTraceEntries(state);
  const summary = document.querySelector("#agent-execution-trace-summary");
  const list = document.querySelector("#agent-execution-trace-list");
  summary.textContent = entries.length ? `${entries.length} verified Agent step${entries.length === 1 ? "" : "s"}` : "No Agent actions yet";
  list.innerHTML = entries.length
    ? entries.map((entry, index) => `<li class="trace-${escapeHTML(entry.kind)}"><span>${String(index + 1).padStart(2, "0")}</span><div><strong>${escapeHTML(entry.title)}</strong><p>${escapeHTML(entry.detail)}</p></div></li>`).join("")
    : `<li class="trace-empty"><div><strong>Awaiting a creative brief</strong><p>The execution trace begins only after the Agent receives a brief.</p></div></li>`;
}

let renderedConversation = "";
function renderConversation(state) {
  const log=document.querySelector("#agent-conversation"), feed=document.querySelector("#creative-agent-feed");
  const messages=state.conversation || [], signature=I18n.language+JSON.stringify(messages);
  if(signature===renderedConversation)return;
  const follow=feed.scrollHeight-feed.scrollTop-feed.clientHeight<48, scroll=feed.scrollTop;
  const first=renderedConversation==="";
  renderedConversation=signature;
  log.replaceChildren();
  for(const message of messages) {
    if(state.status==="awaiting_context_confirmation"&&message===messages.at(-1)&&message.kind==="context_question")continue;
    const article=document.createElement("article"),text=document.createElement("p");
    article.className="agent-message agent-message-"+(message.role==="user"?"user":"system");
    text.setAttribute("translate","no");text.textContent=message.role==="assistant"&&message.kind?I18n.text(message.text):message.text;
    article.append(text);log.append(article);
  }
  log.hidden=!messages.length;
  requestAnimationFrame(()=>{feed.scrollTop=follow||first?feed.scrollHeight:scroll;});
}

function renderPlanLanguage(spec) {
  document.querySelector("#shots").innerHTML=spec.shots.map(shot=>{
    const timing=Number.isFinite(Number(shot.start_seconds))&&Number.isFinite(Number(shot.end_seconds))
      ? `<small class="shot-timing">${escapeHTML(shot.start_seconds)}–${escapeHTML(shot.end_seconds)}s · ${escapeHTML(I18n.text(shot.critical_moment || "planned visual beat"))}</small>`:"";
    return `<li><strong>${escapeHTML(shot.shot_id)}</strong> — ${escapeHTML(I18n.text(shot.intent))}${timing}</li>`;
  }).join("");
  document.querySelector("#constraint-list").innerHTML=(spec.immutable_character_attributes||[]).map(item=>`<span class="tag">${escapeHTML(I18n.text(item))}</span>`).join("");

}

function renderCreativeAgent(state, spec, enrichedContext) {
  renderConversation(state);
  const prompt = spec.source_prompt || spec.goal || "";
  const hasBrief = Boolean(prompt);
  const hasPlan = Boolean(spec.shots?.length);
  const question = document.querySelector("#context-confirmation");
  question.hidden = state.status !== "awaiting_context_confirmation";
  document.querySelector("#context-question-detail").textContent = "Which country or region is this video for?";
  document.querySelector("#agent-intro").parentElement.hidden = hasBrief || Boolean(state.conversation?.length);
  const isInitialBrief = state.status === "ready_for_brief";
  const composerLabel = document.querySelector("#creative-prompt-label");
  const composer = document.querySelector("#creative-prompt");
  const attachments = document.querySelector("#brief-attachments");
  const submit = document.querySelector("#create-plan");
  composerLabel.textContent = isInitialBrief ? "Creative brief" : "Message Creative Agent";
  composer.dataset.placeholder = isInitialBrief
    ? "Describe the subject, key action, setting, style, and non-negotiable constraints."
    : state.status === "awaiting_context_confirmation"
      ? "Add a constraint or clarification while you answer the required question above."
      : "Add a constraint or request a revision for the next eligible version.";
  if(spec.character_reference_plan?.status==='awaiting_clarification')composer.dataset.placeholder=I18n.text("Tell the Agent your preferred visual style or clarify its question.");
  attachments.hidden = !isInitialBrief;
  submit.textContent = isInitialBrief ? "Generate control plan" : "Send update";
  const budgetControl = document.querySelector("#repair-budget-control");
  budgetControl.hidden = !hasPlan;
  if (hasPlan) {
    const autopilot = state.execution_mode === "autopilot";
    const paused = Boolean(state.autopilot_paused);
    const supported = new Set(["planning_ready", "awaiting_first_video", "ready_for_evaluation", "repair_uploaded", "awaiting_repair_upload", "generating_video", "generated_video_ready"]);
    const button = document.querySelector("#autopilot-toggle");
    button.hidden = false;
    button.textContent = autopilot && !paused ? "Ⅱ" : "▶";
    const tooltip = autopilot
      ? (paused ? "继续 Agent 自主代理" : "暂停 Agent 自主代理：当前步骤完成后停止下一步")
      : "Agent 自主代理：在推荐预算内自动生成、评估和修复";
    button.dataset.tooltip = tooltip;
    button.setAttribute("aria-label", tooltip);
    button.disabled = !autopilot && !supported.has(state.status);
  } else {
    document.querySelector("#autopilot-toggle").hidden = true;
  }
}

function clampBudget(value) {
  return Math.min(5, Math.max(0, Number.isFinite(value) ? Math.round(value) : 0));
}

function scoreClass(value) {
  return value >= 4 ? "high" : value >= 3 ? "mid" : "low";
}

function diagnosticScore(report) {
  const calibrated = Number(report?.diagnostic_overall);
  if (Number.isFinite(calibrated) && calibrated > 0) return calibrated;
  const explicit = Number(report?.scores?.overall);
  if (Number.isFinite(explicit)) return explicit;
  const values = Object.entries(report?.scores || {})
    .filter(([name, value]) => name !== "overall" && name !== "audio_visual_alignment" && Number.isFinite(Number(value)))
    .map(([, value]) => Number(value));
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
}

function drawTrend(canvas, reports, releaseFloor = 3) {
  const scale = window.devicePixelRatio || 1;
  const width = Math.max(280, Math.floor(canvas.getBoundingClientRect().width * scale));
  const height = Math.floor(102 * scale);
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  ctx.scale(scale, scale);
  const w = width / scale;
  const h = height / scale;
  const pad = { left: 31, right: 16, top: 13, bottom: 20 };
  // One point is rendered for every immutable version review with an actual
  // evaluation. The chart deliberately has no assumed final version number.
  const points = reports
    .map(report => ({ label: String(report.version_id || "V").toUpperCase(), score: diagnosticScore(report) }))
    .filter(point => Number.isFinite(point.score));
  const y = score => pad.top + (5 - score) / 4 * (h - pad.top - pad.bottom);
  ctx.clearRect(0, 0, w, h);
  ctx.font = "10px Inter, system-ui, sans-serif";
  [5, 4, 3, 2, 1].forEach(value => {
    const yy = y(value);
    ctx.strokeStyle = "rgba(155,148,179,.15)";
    ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(w - pad.right, yy); ctx.stroke();
    ctx.fillStyle = "#716a83";
    ctx.fillText(String(value), 0, yy + 3);
  });
  const gateY = y(releaseFloor);
  ctx.strokeStyle = "rgba(157,116,255,.62)";
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(pad.left, gateY); ctx.lineTo(w - pad.right, gateY); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#a984ff";
  ctx.font = "600 10px Inter, system-ui, sans-serif";
  ctx.fillText(I18n.text(`${releaseFloor.toFixed(1)} gate`), pad.left + 4, gateY - 5);
  const x = index => points.length === 1
    ? w / 2
    : pad.left + (index / (points.length - 1)) * (w - pad.left - pad.right);
  if (points.length > 1) {
    ctx.strokeStyle = "#9d74ff";
    ctx.lineWidth = 2;
    ctx.beginPath();
    points.forEach((point, index) => {
      if (index === 0) ctx.moveTo(x(index), y(point.score));
      else ctx.lineTo(x(index), y(point.score));
    });
    ctx.stroke();
  }
  points.forEach((point, index) => {
    const px = x(index); const py = y(point.score);
    ctx.fillStyle = "rgba(157,116,255,.17)"; ctx.beginPath(); ctx.arc(px, py, 9, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#9d74ff"; ctx.beginPath(); ctx.arc(px, py, 4, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#d9d3e8"; ctx.font = "600 10px Inter, system-ui, sans-serif"; ctx.fillText(`${point.score.toFixed(1)}`, px - 8, py - 13);
    ctx.fillStyle = "#817b93"; ctx.font = "10px Inter, system-ui, sans-serif"; ctx.fillText(point.label, px - 6, h - 5);
  });
}

function renderQuality(scores, reports, report) {
  const overall = diagnosticScore(report);
  const audioCheck = (report.atomic_checks || []).find(item => item.evidence_mode === "audio_probe");
  const audioGate = audioCheck
    ? `<span><i class="${audioCheck.status === "pass" ? "good" : ""}"></i>Audio presence ${human(audioCheck.status)}</span>`
    : `<span><i></i>Audio gate not required</span>`;
  const decision = report.release_decision || "insufficient_evidence";
  const releaseText = decision === "pass" ? "Quality gate passed" : decision === "insufficient_evidence" ? "More evidence required" : "Repair required";
  const coverage = report.evidence_coverage || {};
  const criticalCoverage = `${coverage.critical_requirements_evidenced || 0} / ${coverage.critical_requirements_total || 0}`;
  const profile = report.rubric_profile || {};
  const profileLabel = profile.label || "Controlled video";
  const profileReason = profile.selection_reason || "The Agent selected this rubric from the approved brief and control plan.";
  const labels = profile.dimension_labels || {};
  const configuredDimensions = (profile.release_dimensions || []).filter(name => name in scores);
  const dimensions = (configuredDimensions.length ? configuredDimensions : Object.keys(scores))
    .filter(name => name !== "overall" && name !== "audio_visual_alignment")
    .map(name => [name, Number(scores[name])])
    .filter(([, value]) => Number.isFinite(value));
  const inactive = (profile.inactive_dimensions || [])
    .map(name => labels[name] || human(name));
  const evidenceByDimension = report.dimension_evidence || {};
  const dimensionEvidenceText = dimensions.map(([name]) => {
    const item = evidenceByDimension[name];
    if (!item || !item.requirements_total) return `${labels[name] || human(name)}: policy axis`;
    return `${labels[name] || human(name)}: ${item.requirements_evidenced || 0}/${item.requirements_total} evidenced`;
  });
  const displayedDimensionCount = dimensions.length || 5;
  const overallFloor = Number(profile.overall_floor || 3);
  return `<div class="quality-topline">
      <div class="overall-card"><span>Diagnostic quality</span><strong>${overall.toFixed(1)}<em>/5</em></strong></div>
      <div class="threshold-card"><span class="label">Release policy</span><strong>${releaseText}</strong><div class="threshold-list"><span><i class="${overall >= overallFloor ? "good" : ""}"></i>Diagnostic quality ≥ ${overallFloor.toFixed(1)}</span><span><i class="${decision === "pass" ? "good" : ""}"></i>Critical evidence ${criticalCoverage}</span><span><i class="${decision === "pass" ? "good" : ""}"></i>Active dimensions (${displayedDimensionCount}) ≥ ${Number(profile.dimension_floor || 3).toFixed(1)}</span>${audioGate}</div></div>
    </div>
    <div class="rubric-profile"><span>Selected rubric · ${profileLabel}</span><small>${profileReason}</small></div>
    <div class="trend-frame"><div class="trend-head"><strong>${reports.length > 1 ? "Quality trajectory" : "Initial quality baseline"}</strong><span>Publish ≥ ${overallFloor.toFixed(1)} · evidence-gated</span></div><canvas id="trend-canvas" class="trend-canvas" role="img" aria-label="Quality score trend"></canvas></div>
    <div class="bar-chart">${dimensions.map(([name, value]) => `<div class="bar-row ${scoreClass(value)}"><span class="bar-label">${labels[name] || human(name)}</span><span class="bar-track"><i class="bar-fill" style="width:${value / 5 * 100}%"></i></span><span class="bar-value">${value.toFixed(1)}</span></div>`).join("")}</div><div class="rubric-profile"><small>Score evidence — ${dimensionEvidenceText.join(" · ")}</small></div>${inactive.length ? `<div class="rubric-profile"><small>Excluded for this plan — not part of the diagnostic score: ${inactive.join(" · ")}</small></div>` : ""}`;
}

function reviewForVersion(state, versionId) {
  return (state.version_reviews || []).find(review => review.version_id === versionId) || null;
}

function selectedReview(state) {
  return reviewForVersion(state, selectedReviewVersionId)
    || reviewForVersion(state, state.current_version_id)
    // A failed first generation intentionally does not promote V1 to the
    // current playable version. It still needs to be selectable so its
    // provider error and retry path remain visible to the user.
    || (state.version_reviews || []).at(-1)
    || null;
}

function versionStateLabel(review) {
  if (review.status === "generating") return "Generating";
  if (review.status === "sampling_evidence") return "Preparing evidence";
  if (review.status === "generation_failed") return "Generation failed";
  if (review.status === "published") return "Passed";
  if (review.status === "repair_required") return "Repair required";
  if (review.status === "stopped_budget_exhausted") return "Approval required";
  if (review.status === "observation_unavailable") return "Observation unavailable";
  return review.report ? "Reviewed" : "Awaiting evaluation";
}

function nextVersionLabel(versionId) {
  if (versionId === "none") return "V1";
  const number = Number(String(versionId || "").replace(/^v/i, ""));
  return Number.isInteger(number) && number > 0 ? `V${number + 1}` : "next version";
}

function renderVersionNavigator(state) {
  const reviews = state.version_reviews || [];
  const bar = document.querySelector("#version-review-bar");
  const navigator = document.querySelector("#version-navigator");
  const tabs = document.querySelector("#version-tabs");
  const videoTrigger = document.querySelector("#review-video-trigger");
  const videoThumb = document.querySelector("#review-video-thumb-image");
  const videoFallback = document.querySelector("#review-video-thumb-fallback");
  const videoMeta = document.querySelector("#review-video-meta");
  const generateRepair = document.querySelector("#generate-repair");
  const generationFailure = document.querySelector("#generation-failure");
  const generationFailureDetail = document.querySelector("#generation-failure-detail");
  bar.hidden = !reviews.length && !qualityProgressVisible;
  if (!reviews.length) {
    generateRepair.hidden = true;
    generationFailure.hidden = true;
    return;
  }
  if (!reviewForVersion(state, selectedReviewVersionId)) {
    const currentReview = reviewForVersion(state, state.current_version_id);
    selectedReviewVersionId = (currentReview || reviews.at(-1))?.version_id || "";
  }
  navigator.hidden = qualityProgressVisible;
  if (qualityProgressVisible) return;
  const active = selectedReview(state);
  const failedGeneration = active?.status === "generation_failed";
  generationFailure.hidden = !failedGeneration;
  generationFailureDetail.textContent = failedGeneration
    ? (active.observation_error || "The video provider did not return a completed video.")
    : "";
  tabs.innerHTML = reviews.map(review => {
    const selected = review.version_id === active?.version_id;
    const score = review.report ? `${diagnosticScore(review.report).toFixed(1)} / 5` : "Not evaluated";
    const stateLabel = versionStateLabel(review);
    return `<button class="version-tab ${selected ? "active" : ""} ${review.status === "published" ? "published" : ""}" type="button" role="tab" aria-selected="${selected}" data-version-id="${review.version_id}"><strong>${review.version_id.toUpperCase()}</strong><small>${score}</small><small class="version-state">${stateLabel}</small></button>`;
  }).join("");
  // Older in-memory runs may predate the UI-safe URL field. Agent-generated
  // videos still have a stable per-version playback route, so restore it here.
  const playableUrl = active?.asset?.local_video_url
    || (active?.asset?.source === "agent_generated" && active?.version_id ? `/api/generated-video/${active.version_id}` : "");
  videoTrigger.hidden = !playableUrl;
  videoTrigger.dataset.videoUrl = playableUrl || "";
  if (playableUrl) {
    const poster = active.asset?.poster_data_url;
    videoThumb.hidden = !poster;
    videoFallback.hidden = Boolean(poster);
    if (poster) videoThumb.src = poster;
    videoMeta.textContent = `${active.version_id.toUpperCase()} · play video`;
  } else {
    videoThumb.removeAttribute("src");
    videoTrigger.dataset.videoUrl = "";
  }
  const isBudgetEscalation = active && active.version_id === state.current_version_id && state.status === "stopped_budget_exhausted";
  const hasActiveRepair = active && active.version_id === state.current_version_id && state.status === "awaiting_repair_upload" && Boolean(active.repair_task);
  const isPublishedCurrent = active && active.version_id === state.current_version_id && state.status === "published";
  const remainingRepairBudget = Math.max(0, Number(state.creative_spec?.max_iterations || 0) - Number(state.iterations_used || 0));
  const optionalIterationNeedsApproval = isPublishedCurrent && remainingRepairBudget === 0;
  const canStartOptionalIteration = isPublishedCurrent && remainingRepairBudget > 0;
  generateRepair.hidden = !hasActiveRepair && !isBudgetEscalation && !optionalIterationNeedsApproval && !canStartOptionalIteration;
  if (isBudgetEscalation) {
    generateRepair.disabled = false;
    generateRepair.dataset.mode = "request_budget";
    generateRepair.textContent = "Approve additional repair";
    generateRepair.title = "Quality is still below the release gate. Approve an additional repair round to continue.";
  } else if (optionalIterationNeedsApproval) {
    generateRepair.disabled = false;
    generateRepair.dataset.mode = "request_budget";
    generateRepair.textContent = "Approve optional iteration";
    generateRepair.title = "This version passed. Approve another iteration only if you want to improve it further.";
  } else if (canStartOptionalIteration) {
    generateRepair.disabled = false;
    generateRepair.dataset.mode = "optional_iteration";
    generateRepair.textContent = "Iterate further";
    generateRepair.title = "Create an optional next-version optimisation while preserving this passed version.";
  } else if (hasActiveRepair) {
    const nextLabel = nextVersionLabel(active.version_id);
    const optionalIteration = active.decision?.priority === "optional";
    generateRepair.disabled = false;
    generateRepair.dataset.mode = "generate";
    generateRepair.textContent = `${optionalIteration ? "Regenerate optimized" : "Regenerate full"} ${nextLabel}`;
    generateRepair.title = "Creates a new complete clip. The Agent uses the failed beat as a repair priority; it does not locally edit a single shot.";
  } else {
    generateRepair.dataset.mode = "generate";
    generateRepair.title = "";
  }
}

function renderEvaluation(state) {
  const review = selectedReview(state);
  const report = review?.report || null;
  const observationError = review?.observation_error || "";
  const source = document.querySelector("#observation-source");
  source.textContent = observationError ? "Observation unavailable" : !report ? "Awaiting observation" : report.observation_source === "doubao_seed_2_1_pro" ? "Doubao Seed 2.1 Pro" : report.observation_source === "fixture" ? "Demo fixture" : "Archived model result";
  const scores = document.querySelector("#scores");
  const emptyGate = document.querySelector("#gate-empty");
  const summary = document.querySelector("#summary");
  if (report) {
    const reports = (state.version_reviews || []).map(item => item.report).filter(Boolean);
    emptyGate.hidden = true;
    scores.innerHTML = renderQuality(report.scores, reports, report);
    requestAnimationFrame(() => drawTrend(document.querySelector("#trend-canvas"), reports, Number(report.rubric_profile?.overall_floor || 3)));
    summary.textContent = report.summary;
  } else {
    emptyGate.hidden = false;
    emptyGate.textContent = observationError || "Run the quality loop to evaluate this version.";
    scores.innerHTML = "";
    summary.textContent = "";
  }

  const badCases = document.querySelector("#bad-cases");
  const checks = report?.atomic_checks || [];
  badCases.innerHTML = report
    ? (checks.length ? checks.map(item => {
      const evidence = item.evidence?.length
        ? item.evidence.map(frame => frame.source === "audio_probe"
          ? `Audio probe · ${Number(frame.start_seconds || 0).toFixed(2)}–${Number(frame.end_seconds || 0).toFixed(2)}s — ${frame.description}`
          : `${frame.frame_id} · ${Number(frame.timestamp).toFixed(2)}s — ${frame.description}`).join("<br>")
        : (item.evidence_mode === "audio_probe" ? "No valid audio evidence returned." : "No valid frame evidence returned.");
      return `<div class="case ${item.status}"><div class="case-top"><h3>${item.shot_id} · ${item.requirement}</h3><span class="severity ${item.status}">${human(item.status)}</span></div><p><strong>Expected:</strong> ${item.expected}</p><p><strong>Observed:</strong> ${item.observed}</p><p><strong>Evidence:</strong> ${evidence}</p>${item.repair_hint ? `<p><strong>Repair hint:</strong> ${item.repair_hint}</p>` : ""}</div>`;
    }).join("") : `<div class="empty">No planned acceptance checks were returned.</div>`)
    : `<div class="empty">${observationError || "No observation yet."}</div>`;

  const decision = document.querySelector("#decision");
  if (review?.decision) {
    const d = review.decision;
    const task = review.repair_task;
    const expected = Array.isArray(d.expected_improvement) ? d.expected_improvement : [String(d.expected_improvement || "—")];
    const rejected = Array.isArray(d.rejected_options) ? d.rejected_options : [String(d.rejected_options || "—")];
    const actionLabel = d.action === "regenerate_shot" || d.action === "split_shot" ? "Targeted full-video regeneration" : human(d.action);
    const scopeNote = task?.execution_scope === "whole_clip_regeneration"
      ? `<p><strong>Scope:</strong> ${task.scope_notice || "A new complete clip will be generated; the target beat is the evidence-backed priority."}</p>` : "";
    decision.innerHTML = `<div class="decision-card"><h3>${actionLabel} · ${d.target_shot || "project"}</h3><p>${d.reason}</p><ul><li><strong>Expected:</strong> ${expected.map(human).join(", ")}</li><li><strong>Rejected:</strong> ${rejected.join("; ")}</li><li><strong>Tool:</strong> ${d.tool_name}</li></ul></div>${task ? `<div class="repair-task"><strong>Tool output · ${task.task_id}</strong>${scopeNote}<p>${task.human_handoff}</p><p>${task.prompt}</p></div>` : ""}`;
  } else if (review?.status === "published") {
    decision.innerHTML = `<div class="decision-card"><h3>Stop: publish</h3><p>Policy stopped further actions because all release gates pass.</p></div>`;
  } else if (report?.release_decision === "insufficient_evidence") {
    decision.innerHTML = `<div class="empty">The Agent will not create a repair task until every critical requirement has valid frame-level evidence. Re-run the quality loop with better evidence.</div>`;
  } else if (observationError) {
    decision.innerHTML = `<div class="empty">No repair decision was created because the Agent has no evidence-backed visual observation.</div>`;
  } else {
    decision.innerHTML = `<div class="empty">Run the quality loop to see the Agent decision.</div>`;
  }
  const log = document.querySelector("#log");
  log.innerHTML = state.history.length ? [...state.history].reverse().map(item => {
    const text = item.payload.reason || item.payload.message || item.payload.human_handoff || JSON.stringify(item.payload);
    return `<div class="log-row"><span class="phase">${human(item.phase)}</span><span class="event">${human(item.event)}</span><span class="log-detail">${text}</span></div>`;
  }).join("") : `<div class="empty">The log records Observe, Reason and Act as the agent works.</div>`;
}

function startQualityProgress() {
  qualityLoopPending = true;
  qualityProgressVisible = true;
  const progress = document.querySelector("#quality-progress");
  document.querySelector("#version-review-bar").hidden = false;
  document.querySelector("#version-navigator").hidden = true;
  const fill = document.querySelector("#quality-progress-fill");
  const button = document.querySelector("#run-loop");
  progress.hidden = false;
  fill.style.width = "0%";
  button.disabled = true;
  button.textContent = "Analyzing video…";
  let value = 0;
  qualityProgressTimer = window.setInterval(() => {
    if (value < 90) value = Math.min(90, value + Math.max(2, (90 - value) * 0.2));
    else if (value < 99) value = Math.min(99, value + Math.max(0.06, (99 - value) * 0.025));
    fill.style.width = `${value}%`;
  }, 180);
}

function finishQualityProgress(success) {
  if (qualityProgressTimer) window.clearInterval(qualityProgressTimer);
  qualityProgressTimer = null;
  const progress = document.querySelector("#quality-progress");
  const fill = document.querySelector("#quality-progress-fill");
  qualityLoopPending = false;
  if (success) {
    fill.style.width = "100%";
    window.setTimeout(() => {
      qualityProgressVisible = false;
      progress.hidden = true;
      if (renderedState) renderVersionNavigator(renderedState);
    }, 420);
  } else {
    qualityProgressVisible = false;
    progress.hidden = true;
    if (renderedState) renderVersionNavigator(renderedState);
  }
}

function renderReferenceMedia(spec) {
  const referenceMedia = Array.isArray(spec.multimodal_assets) ? spec.multimodal_assets : [];
  const referenceImages = referenceMedia.filter(asset => asset && asset.media_type === "image");
  const referenceAudio = referenceMedia.filter(asset => asset && asset.media_type === "audio");
  const referenceVideos = referenceMedia.filter(asset => asset && asset.media_type === "video");
  const reference = spec.reference_asset || referenceImages[0];
  const referenceLabel = [
    referenceImages.length ? I18n.text(`${referenceImages.length} images`) : "",
    referenceAudio.length ? I18n.text(`${referenceAudio.length} audio files`) : "",
    referenceVideos.length ? I18n.text(`${referenceVideos.length} video reference(s)`) : "",
  ].filter(Boolean).join(" + ");
  document.querySelector("#reference-role-label").textContent = referenceLabel || I18n.text("Prompt-derived constraints");
  document.querySelector("#reference-role-label").translate=false;
  document.querySelector("#reference-title").translate=false;
  document.querySelector("#reference-title").textContent = reference
    ? (referenceMedia.length > 1 ? `${I18n.text("Attached")} · ${referenceLabel}` : reference.name)
    : (referenceMedia.length ? `${I18n.text("Attached")} · ${referenceLabel}` : I18n.text("No reference media"));
  document.querySelector("#reference-detail").textContent = referenceMedia.length
    ? "Reference materials attached to video generation."
    : "The Agent will preserve constraints extracted from your brief.";
  const referenceVisual = document.querySelector("#reference-visual");
  const referencePreview = document.querySelector("#reference-preview");
  referenceVisual.hidden = !(reference && reference.preview_data_url);
  if (!referenceVisual.hidden) referencePreview.src = reference.preview_data_url;

}

function render(state) {
  renderedState = state;
  renderModelSelection(state);
  renderCharacterReferences(state);
  scheduleProductionDefaults(state);
  if (state.current_version_id !== lastCurrentVersionId) {
    selectedReviewVersionId = state.current_version_id;
    lastCurrentVersionId = state.current_version_id;
  }
  const spec = state.creative_spec;
  const hasPlan = Boolean(spec.goal && spec.shots.length);
  const enrichedContext = spec.enriched_context || {};
  const promptInput = document.querySelector("#creative-prompt");
  if (state.status === "ready_for_brief" && document.activeElement !== promptInput) setEditorText("");
  renderProjectStatus(state);
  renderCreativeAgent(state, spec, enrichedContext);

  document.querySelector("#plan-section").hidden = !hasPlan;
  document.querySelector("#evaluation-section").hidden = !EVALUATION_STATES.has(state.status);
  if (!hasPlan) {
    document.querySelector("#agent-video-action").hidden = true;
    document.querySelector("#version-review-bar").hidden = true;
    return;
  }

  const usedLivePlanner = spec.planning_source === "doubao_seed_2_1_pro";
  document.querySelector("#planning-source").textContent = usedLivePlanner ? "Doubao Seed 2.1 Pro plan" : ["fixture", "fallback"].includes(spec.planning_source) ? "Deterministic fallback" : "Archived model plan";
  const samplingFocus = (spec.evidence_sampling?.focus_shot_ids || []).join(" · ");
  const planningSummary = spec.planning_notice || "Agent analyzed the brief, built a scene-specific control plan, and identified controllability risks.";
  document.querySelector("#plan-summary-text").textContent = samplingFocus
    ? `${planningSummary} Evidence sampling will focus on ${samplingFocus}.`
    : planningSummary;
  renderPlanLanguage(spec);
  const direction = spec.production_direction || {};
  const directionItems = [
    ["Camera", direction.camera_language],
    ["Visual treatment", direction.visual_treatment],
    ["Continuity", direction.motion_continuity],
    ["Sound", direction.sound_design],
  ].filter(([, value]) => typeof value === "string" && value.trim());
  document.querySelector("#production-direction").innerHTML = directionItems
    .map(([label, value]) => `<div class="direction-item"><b>${label}</b><span>${value}</span></div>`).join("");
  document.querySelector("#acceptance-criteria").innerHTML = (spec.acceptance_criteria || []).map(item => `<div class="criterion"><span class="criterion-shot">${item.shot_id}</span><span>${item.requirement}</span>${item.critical ? `<b>critical</b>` : ""}</div>`).join("");
  document.querySelector("#risk-list").innerHTML = (spec.risk_forecast || []).map(item => `<span class="risk">${item}</span>`).join("");
  const autopilot = state.execution_mode === "autopilot";

  renderReferenceMedia(spec);

  const budgetControl = document.querySelector("#repair-budget-control");
  const budgetValue = document.querySelector("#budget-value");
  const budgetDecrease = document.querySelector("#budget-decrease");
  const budgetIncrease = document.querySelector("#budget-increase");
  const canAdjustBudget = hasPlan;
  const budget = clampBudget(Number(spec.max_iterations));
  budgetControl.hidden = false;
  budgetControl.classList.toggle("needs-approval", state.status === "stopped_budget_exhausted");
  budgetControl.dataset.minimum = String(state.iterations_used);
  budgetValue.textContent = String(budget);
  budgetControl.title = state.status === "stopped_budget_exhausted"
    ? `修复预算已耗尽：当前为 ${budget} 轮。增加次数即可授权 Agent 继续修复。`
    : `修复预算：当前最多 ${budget} 轮。调整预算会暂停 Agent 自主代理，等待你点击 ▶ 继续。`;
  budgetControl.dataset.tooltip = budgetControl.title;
  budgetDecrease.disabled = !canAdjustBudget || budget <= state.iterations_used;
  budgetIncrease.disabled = !canAdjustBudget || budget >= 5;
  const videoStage = document.querySelector("#agent-video-action");
  const firstVideoStates = new Set(["planning_ready", "awaiting_first_video", "generating_video", "generated_video_ready", "ready_for_evaluation", "awaiting_repair_upload", "repair_uploaded", "published", "stopped_budget_exhausted"]);
  videoStage.hidden = !firstVideoStates.has(state.status);
  const generationControls = document.querySelector("#video-generation-controls");
  const readyCard = document.querySelector("#video-ready-card");
  const runLoop = document.querySelector("#run-loop");
  const generateButton = document.querySelector("#generate-video");
  const videoAsset = state.current_video_asset;
  const canRunQuality = state.status === "ready_for_evaluation" || state.status === "repair_uploaded";
  const videoStatus = document.querySelector("#agent-video-status");
  if (state.status === "planning_ready" || state.status === "awaiting_first_video") {
    const upcomingVersion = nextVersionLabel(state.current_version_id);
    document.querySelector("#video-stage-copy").textContent = state.status === "planning_ready"
      ? `Your selected repair maximum will be approved when you generate ${upcomingVersion}.`
      : `Start the Agent to generate ${upcomingVersion} from the approved control plan.`;
    videoStatus.textContent = `Ready for ${upcomingVersion}`;
    generationControls.hidden = false;
    generateButton.disabled = false;
    generateButton.textContent = `Generate ${upcomingVersion}`;
    readyCard.hidden = true;
    runLoop.disabled = true;
    runLoop.textContent = "Run quality loop";
  } else if (state.status === "generating_video") {
    const job = (state.generation_jobs || []).at(-1);
    const statusUnavailable = Boolean(job?.error);
    document.querySelector("#video-stage-copy").textContent = statusUnavailable
      ? "The provider task is retained, but its latest status could not be verified. The Agent has paused autonomy and will not submit a duplicate generation."
      : `Agent is ${job?.status || "queued"} ${job?.version_id?.toUpperCase() || "the next version"} with ${human(job?.tool_name || "video generation tool")}.`;
    generationControls.hidden = true;
    readyCard.hidden = true;
    videoStatus.textContent = statusUnavailable ? "Status check needed" : "Generating";
    runLoop.disabled = true;
    runLoop.textContent = "Generating video…";
  } else if (state.status === "generated_video_ready") {
    document.querySelector("#video-stage-copy").textContent = "Generation completed. The Agent is sampling risk-aware visual evidence before quality evaluation.";
    generationControls.hidden = true;
    readyCard.hidden = true;
    videoStatus.textContent = "Preparing evidence";
    runLoop.disabled = true;
    runLoop.textContent = "Preparing evidence…";
  } else if (videoAsset) {
    generationControls.hidden = true;
    readyCard.hidden = false;
    document.querySelector("#video-stage-copy").textContent = state.status === "awaiting_repair_upload"
        ? state.latest_decision?.priority === "optional"
          ? "The Agent has prepared an optional next-version optimisation. Continue it from this Agent card, then it will be verified automatically."
          : "The Agent has prepared a targeted repair. Continue it from this Agent card, then it will be verified automatically."
        : canRunQuality
          ? "Generated V1 is ready for a quality evaluation."
        : "The video evidence is retained with this Agent run for an auditable quality decision.";
    videoStatus.textContent = canRunQuality ? "Ready to evaluate" : state.status === "published" ? "Quality passed" : "Decision ready";
    document.querySelector("#video-ready-name").textContent = videoAsset.name || "V1 video";
    const frameCount = Number(videoAsset.frame_count || 0);
    document.querySelector("#video-ready-meta").textContent = frameCount ? `${frameCount} risk-aware evidence frames · ready for evaluation` : "Prepared version · ready for evaluation";
    const poster = document.querySelector("#video-poster-image");
    const fallback = document.querySelector("#video-poster-fallback");
    poster.hidden = !videoAsset.poster_data_url;
    fallback.hidden = Boolean(videoAsset.poster_data_url);
    if (videoAsset.poster_data_url) poster.src = videoAsset.poster_data_url;
    runLoop.disabled = qualityLoopPending || !canRunQuality;
    if (!qualityLoopPending) runLoop.textContent = canRunQuality ? "Run quality loop" : "Quality assessed";
  }
  document.querySelector('#generate-repair').disabled=spec.character_reference_plan?.status==='awaiting_clarification';
  if(spec.character_reference_plan?.status==='awaiting_clarification'){
    generateButton.disabled=true;
    videoStatus.textContent='Awaiting creative clarification';
    document.querySelector('#video-stage-copy').textContent='Confirm the visual style in the Creative Agent conversation';
  }
  renderVersionNavigator(state);
  if (EVALUATION_STATES.has(state.status)) renderEvaluation(state);
  window.dispatchEvent(new CustomEvent("vclstatechange", {detail:state}));
}

async function refresh() {
  const state = await endpoint("/api/state");
  render(state);
  if (state.status === "generating_video") pollGeneration();
  if (state.status === "generated_video_ready") prepareGeneratedEvidence(state);
  if (autopilotIsRunning(state)) window.setTimeout(() => advanceAutopilot(state), 0);
}

document.querySelector("#version-tabs").addEventListener("click", event => {
  const tab = event.target.closest("button[data-version-id]");
  if (!tab || !renderedState) return;
  selectedReviewVersionId = tab.dataset.versionId;
  render(renderedState);
});
function closeReviewPlayer() {
  const dialog = document.querySelector("#video-player-dialog");
  const player = document.querySelector("#review-video-player");
  player.pause();
  player.removeAttribute("src");
  player.load();
  if (dialog.open) dialog.close();
}

document.querySelector("#review-video-trigger").addEventListener("click", () => {
  const trigger = document.querySelector("#review-video-trigger");
  const url = trigger.dataset.videoUrl;
  if (!url) return;
  const active = selectedReview(renderedState || {});
  const dialog = document.querySelector("#video-player-dialog");
  const player = document.querySelector("#review-video-player");
  document.querySelector("#video-player-title").textContent = `${active?.version_id?.toUpperCase() || "Selected"} video`;
  player.src = url;
  dialog.showModal();
  player.play().catch(() => {});
});

document.querySelector("#close-video-player").addEventListener("click", closeReviewPlayer);
document.querySelector("#video-player-dialog").addEventListener("click", event => {
  if (event.target === event.currentTarget) closeReviewPlayer();
});
document.querySelector("#video-player-dialog").addEventListener("close", () => {
  const player = document.querySelector("#review-video-player");
  player.pause();
  player.removeAttribute("src");
});

document.querySelector("#reference-media").addEventListener("change", event => {
  const incoming = Array.from(event.target.files || []);
  const existing = new Set(selectedReferenceItems.map(referenceFileKey));
  // `@filename` is the user-facing binding syntax.  A second asset with the
  // same filename would make that binding ambiguous, even when its bytes are
  // different, so keep filenames unique inside one creative brief.
  const existingNames = new Set(selectedReferenceItems.map(file => file.name.toLowerCase()));
  let imageCount = selectedReferenceItems.filter(isImageReference).length;
  let audioCount = selectedReferenceItems.filter(isAudioReference).length;
  const rejected = [];
  incoming.forEach(file => {
    if (!isImageReference(file) && !isAudioReference(file)) {
      rejected.push(`${file.name}: use an image, MP3, or WAV file`);
      return;
    }
    if (existingNames.has(file.name.toLowerCase())) {
      rejected.push(`${file.name}: filenames must be unique for @ reference binding`);
      return;
    }
    if (existing.has(referenceFileKey(file))) return;
    if (isImageReference(file) && imageCount >= 30) {
      rejected.push(`${file.name}: maximum 30 images`);
      return;
    }
    if (isAudioReference(file) && audioCount >= 10) {
      rejected.push(`${file.name}: maximum 10 audio files`);
      return;
    }
    selectedReferenceItems.push(file);
    existing.add(referenceFileKey(file));
    existingNames.add(file.name.toLowerCase());
    if (isImageReference(file)) imageCount += 1;
    else audioCount += 1;
  });
  event.target.value = "";
  renderReferenceAttachments();
  updateMentionMenu();
  if (rejected.length) I18n.alert(rejected.join("\n"));
});
document.querySelector("#creative-prompt").addEventListener("input", () => {
  // Typing after an over-limit warning replaces the temporary visual marker.
  // The next submission evaluates the edited content again.
  promptEditor().querySelectorAll(".duration-overlimit").forEach(marker => marker.replaceWith(document.createTextNode(marker.textContent || "")));
  hideDurationValidationPopover();
  updateMentionMenu();
});
document.querySelector("#creative-prompt").addEventListener("keyup", updateMentionMenu);
document.querySelector("#creative-prompt").addEventListener("keydown", event => {
  if (event.key === "Backspace" || event.key === "Delete") {
    const token = adjacentMentionToken(event.key === "Backspace" ? "backward" : "forward");
    if (token) {
      event.preventDefault();
      removeInlineMentionToken(token);
      return;
    }
  }
  if (event.key === "@" && selectedReferenceItems.length) {
    window.requestAnimationFrame(updateMentionMenu);
  }
  if (event.key === "Escape") hideMentionMenu();
});
document.querySelector("#creative-prompt").addEventListener("click", updateMentionMenu);
document.querySelector("#creative-prompt").addEventListener("blur", () => window.setTimeout(hideMentionMenu, 120));
document.querySelector("#project-form").addEventListener("submit", async event => {
  event.preventDefault();
  const prompt = editorText().trim();
  const submit = event.currentTarget.querySelector("button[type=submit]");
  if (!prompt) return;
  const invalidDurations = overLimitDurations(prompt);
  if (invalidDurations.length) {
    const marker = highlightOverLimitDurations();
    promptEditor().focus();
    // A native alert blocks paint, which made the duration marker appear to
    // fail.  Position the product-owned hint beside the marked text instead.
    window.requestAnimationFrame(() => showDurationValidationPopover(marker));
    return;
  }
  const isInitialBrief = renderedState?.status === "ready_for_brief";
  submit.disabled = true;
  submit.textContent = isInitialBrief ? "Analyzing brief…" : "Updating Agent…";
  try {
    if (isInitialBrief) {
      // Promise.all preserves the order of this array.  It is deliberately
      // built from the visual attachment strip rather than from media-type
      // buckets, so `image → audio → image` remains that exact order all the
      // way into the provider request.
      const multimodalAssets = await Promise.all(selectedReferenceItems.map(async (file, inputOrder) => {
        const asset = isImageReference(file)
          ? await readReferenceFile(file)
          : isVideoReference(file) ? {name:file.name,mime_type:file.type,url:file.reference_url,duration_seconds:file.duration_seconds,size:0} : await readAudioReferenceFile(file);
        asset.role = isImageReference(file) ? "Visual reference" : isVideoReference(file) ? "reference_video" : "Audio reference";
        asset.input_order = inputOrder + 1;
        asset.prompt_mentions = promptMentionsFor(file, prompt);
        return asset;
      }));
      render(await endpoint("/api/create-project", { method: "POST", body: JSON.stringify({ prompt, multimodal_assets: multimodalAssets, language:I18n.language }) }));
    } else {
      render(await endpoint("/api/revise-brief", { method: "POST", body: JSON.stringify({ instruction: prompt, language:I18n.language }) }));
    }
    setEditorText("");
    clearReferenceAttachments();
    if (["planning_ready", "awaiting_first_video", "awaiting_repair_upload"].includes(renderedState.status)) await recommendCurrentVideoModel();
  } catch (error) {
    I18n.alert(error.message);
  } finally {
    submit.disabled = false;
    submit.textContent = renderedState?.status === "ready_for_brief" ? "Generate control plan" : "Send update";
  }
});
document.querySelector("#confirm-context").addEventListener("click", async () => {
  const button = document.querySelector("#confirm-context");
  const market = document.querySelector("#target-market").value.trim();
  if (!market) {
    document.querySelector("#target-market").focus();
    return;
  }
  button.disabled = true;
  button.textContent = "Continuing…";
  try {
    render(await endpoint("/api/confirm-context", { method: "POST", body: JSON.stringify({ target_market: market, language:I18n.language }) }));
    if (renderedState.status === "planning_ready") await recommendCurrentVideoModel();
  } catch (error) {
    I18n.alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Continue planning";
  }
});
function setCreativeAgentOpen(open, focus = false) {
  const panel=document.querySelector("#creative-agent"), capsule=document.querySelector("#creative-agent-toggle");
  if (panel.classList.contains("collapsed") === open) panel.classList.toggle("collapsed", !open);
  panel.inert=!open;
  document.body.classList.toggle("agent-open", open);
  capsule.hidden=open;
  document.querySelector('.canvas-header-actions').inert=open;
  capsule.setAttribute("aria-expanded", String(open));
  capsule.setAttribute("aria-label", open ? "Minimize Creative Agent" : "Open Creative Agent");
  if (focus && open) requestAnimationFrame(()=>promptEditor().focus());
}
document.querySelector("#creative-agent-minimize").addEventListener("click",()=>{
  setCreativeAgentOpen(false);
  document.querySelector("#creative-agent-toggle").focus();
});
document.querySelector("#creative-agent-toggle").addEventListener("click",()=>setCreativeAgentOpen(document.querySelector("#creative-agent").classList.contains("collapsed"),true));
new MutationObserver(()=>setCreativeAgentOpen(!document.querySelector("#creative-agent").classList.contains("collapsed"))).observe(document.querySelector("#creative-agent"),{attributes:true,attributeFilter:["class"]});
setCreativeAgentOpen(false);
function currentBudgetValue() {
  return clampBudget(Number(document.querySelector("#budget-value").textContent));
}

async function changeRepairBudget(delta) {
  if (!renderedState) return;
  const control = document.querySelector("#repair-budget-control");
  const minimum = Number(control.dataset.minimum || 0);
  const next = Math.min(5, Math.max(minimum, currentBudgetValue() + delta));
  if (next === currentBudgetValue()) return;
  document.querySelector("#budget-value").textContent = String(next);
  try {
    render(await endpoint("/api/update-budget", { method: "POST", body: JSON.stringify({ max_iterations: next }) }));
  } catch (error) {
    I18n.alert(error.message);
    render(await endpoint("/api/state"));
  }
}

document.querySelector("#budget-decrease").addEventListener("click", () => changeRepairBudget(-1));
document.querySelector("#budget-increase").addEventListener("click", () => changeRepairBudget(1));
async function approveCurrentPlan() {
  if (renderedState?.status !== "planning_ready") return renderedState;
  const state = await endpoint("/api/approve-plan", {
    method: "POST",
    body: JSON.stringify({ max_iterations: currentBudgetValue() }),
  });
  render(state);
  return state;
}
async function runQualityLoop() {
  startQualityProgress();
  try {
    const state = await endpoint("/api/run-loop", { method: "POST" });
    finishQualityProgress(true);
    selectedReviewVersionId = state.current_version_id;
    render(state);
  } catch (error) {
    finishQualityProgress(false);
    document.querySelector("#run-loop").disabled = false;
    I18n.alert(error.message);
  }
}
async function prepareGeneratedEvidence(state) {
  if (generatedEvidencePending || state.status !== "generated_video_ready") return;
  const videoUrl = state.current_video_asset?.local_video_url;
  if (!videoUrl) return;
  generatedEvidencePending = true;
  try {
    const response = await fetch(videoUrl, { cache: "no-store" });
    if (!response.ok) throw new Error("The generated video could not be retrieved for evaluation.");
    const contractSampling = state.creative_spec?.evaluation_contract?.evidence_plan?.sampling_policy;
    const frames = await extractEvaluationFrames(await response.blob(), contractSampling || state.creative_spec?.evidence_sampling);
    const prepared = await endpoint("/api/register-generated-video", { method: "POST", body: JSON.stringify({ frames }) });
    render(prepared);
    if (prepared.execution_mode !== "autopilot" || !prepared.autopilot_paused) {
      await runQualityLoop();
      await advanceAutopilot(renderedState);
    }
  } catch (error) {
    I18n.alert(error.message);
  } finally {
    generatedEvidencePending = false;
  }
}
async function pollGeneration() {
  if (generationPollTimer) window.clearTimeout(generationPollTimer);
  try {
    const state = await endpoint("/api/refresh-generation", { method: "POST" });
    render(state);
    if (state.status === "generating_video") {
      generationPollTimer = window.setTimeout(pollGeneration, 4000);
    } else if (state.status === "generated_video_ready") {
      await prepareGeneratedEvidence(state);
    }
  } catch (error) {
    I18n.alert(error.message);
  }
}
async function generateVideo(event) {
  const button = event?.currentTarget || document.querySelector("#generate-video");
  button.disabled = true;
  try {
    await prepareCharacterReferences();
    if (renderedState.creative_spec.character_reference_plan?.status==='generated') await attachCharacterReferences();
    const characters = renderedState.creative_spec.character_reference_plan;
    if (characters?.status==='awaiting_clarification') {
      if(autopilotIsRunning(renderedState))render(await endpoint('/api/autopilot',{method:'POST',body:JSON.stringify({action:'pause'})}));
      setCreativeAgentOpen(true,true);button.disabled=false;return;
    }
    if (characters?.required && (characters.status !== 'ready' || !characters.attached)) {
      if (autopilotIsRunning(renderedState)) render(await endpoint('/api/autopilot', {method:'POST',body:JSON.stringify({action:'pause'})}));
      location.hash = 'vcl';
      window.setTimeout(()=>document.querySelector('#character-references').scrollIntoView({behavior:'smooth',block:'center'}),0);
      button.disabled = false;
      return;
    }
    await saveGenerationSettings();
    await approveCurrentPlan();
    const state = await endpoint("/api/generate-video", { method: "POST" });
    render(state);
    pollGeneration();
  } catch (error) {
    button.disabled = false;
    I18n.alert(error.message);
  }
}
function autopilotIsRunning(state) {
  return state?.execution_mode === "autopilot" && !state.autopilot_paused;
}

async function advanceAutopilot(state) {
  if(location.hash==='#history'||document.body.dataset.workspace==='history')return;
  if (!autopilotIsRunning(state) || autopilotAdvancePending) return;
  if (!["awaiting_first_video", "awaiting_repair_upload", "ready_for_evaluation", "repair_uploaded"].includes(state.status)) return;
  autopilotAdvancePending = true;
  try {
    if (["awaiting_first_video", "awaiting_repair_upload"].includes(state.status)) {
      await generateVideo();
    } else {
      await runQualityLoop();
      if (autopilotIsRunning(renderedState) && ["awaiting_first_video", "awaiting_repair_upload"].includes(renderedState.status)) {
        await generateVideo();
      }
    }
  } finally {
    autopilotAdvancePending = false;
  }
}

document.querySelector("#autopilot-toggle").addEventListener("click", async event => {
  const button = event.currentTarget;
  const pausing = renderedState?.execution_mode === "autopilot" && !renderedState.autopilot_paused;
  button.disabled = true;
  try {
    const state = await endpoint("/api/autopilot", { method: "POST", body: JSON.stringify({ action: pausing ? "pause" : "start" }) });
    render(state);
    await advanceAutopilot(state);
  } catch (error) {
    I18n.alert(error.message);
    button.disabled = false;
  }
});
document.querySelector("#run-loop").addEventListener("click", runQualityLoop);
document.querySelector("#generate-video").addEventListener("click", generateVideo);
document.querySelector("#generate-repair").addEventListener("click", event => {
  if (event.currentTarget.dataset.mode === "request_budget") {
    document.querySelector("#creative-agent").classList.remove("collapsed");

    render(renderedState);
    requestAnimationFrame(() => {
      document.querySelector("#repair-budget-control").classList.add("needs-approval");
      document.querySelector("#budget-increase").focus();
    });
    return;
  }
  if (event.currentTarget.dataset.mode === "optional_iteration") {
    event.currentTarget.disabled = true;
    endpoint("/api/continue-optimization", { method: "POST" })
      .then(state => render(state))
      .catch(error => {
        event.currentTarget.disabled = false;
        I18n.alert(error.message);
      });
    return;
  }
  generateVideo(event);
});
window.openNewProject=(kind='vcl',choose=false)=>{
  document.querySelector('#new-project-kind').value=kind;
  document.querySelector('#new-project-kind').hidden=!choose;
  document.querySelector('#new-project-kind-label').hidden=!choose;
  document.querySelector('#new-project-name').value='';
  document.querySelector('#new-project-dialog').showModal();document.querySelector('#new-project-name').focus();
};
document.querySelector('#reset').addEventListener('click',()=>window.openNewProject('vcl'));
document.querySelector('#new-project-cancel').addEventListener('click',()=>document.querySelector('#new-project-dialog').close());
document.querySelector('#new-project-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;
  try{
    await window.CanvasImages.flush();
    const kind=document.querySelector('#new-project-kind').value,title=document.querySelector('#new-project-name').value.trim();
    await endpoint(kind==='canvas'?'/api/new-canvas':'/api/reset',{method:'POST',body:JSON.stringify({title})});
    location.assign('/?new='+Date.now()+'#'+kind);
  }catch(error){I18n.alert(error.message);button.disabled=false;}
});

refresh();

window.addEventListener("languagechange", () => {
  if(renderedState){renderConversation(renderedState);if(renderedState.creative_spec.shots?.length){renderPlanLanguage(renderedState.creative_spec);renderReferenceMedia(renderedState.creative_spec);}}
  const report = renderedState && selectedReview(renderedState)?.report;
  if (!report) return;
  const reports = renderedState.version_reviews.map(item => item.report).filter(Boolean);
  drawTrend(document.querySelector("#trend-canvas"), reports, Number(report.rubric_profile?.overall_floor || 3));
});

// Canvas hands a reviewable draft to the same composer; it never calls a paid provider.
window.VCLBridge = {
  state:()=>renderedState,
  open:()=>setCreativeAgentOpen(true,true),
  readMedia:async file=>isImageReference(file)?readReferenceFile(file):readAudioReferenceFile(file),
  async stage({text,assets}) {
    if(renderedState?.status==='generating_video') throw new Error('Wait for the current video before changing references.');
    if(renderedState?.status!=='ready_for_brief' && assets.length) throw new Error('This project already has references. Start a new project to use different canvas references.');
    const names=new Set(selectedReferenceItems.map(file=>file.name.toLowerCase()));
    const additions=[];
    for(const asset of assets) {
      if(names.has(asset.name.toLowerCase())) throw new Error('Reference filenames must be unique.');
      names.add(asset.name.toLowerCase());
      if(asset.media_type==='video') additions.push({name:asset.name,type:asset.mime_type,size:0,lastModified:0,reference_url:asset.url,duration_seconds:asset.duration_seconds});
      else {
        const value=asset.data_url||asset.preview_data_url;
        if(!value?.startsWith(asset.media_type==='image'?'data:image/':'data:audio/')) throw new Error('Reference media is unavailable.');
        const blob=await (await fetch(value)).blob();
        additions.push(new File([blob],asset.name,{type:asset.mime_type,lastModified:0}));
      }
    }
    const all=[...selectedReferenceItems,...additions];
    if(all.filter(isImageReference).length>30 || all.filter(isAudioReference).length>10 || all.filter(isVideoReference).length>10) throw new Error('Too many references for one brief.');
    selectedReferenceItems=all;
    const existing=editorText().trim();
    setEditorText(existing?existing+'\n\n'+text:text);
    renderReferenceAttachments();
    setCreativeAgentOpen(true,true);
  }
};
