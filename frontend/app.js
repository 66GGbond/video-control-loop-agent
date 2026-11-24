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
let generatedEvidenceError = null;
let autopilotAdvancePending = false;
let activeAudioReferencePreview = null;
let activeImageReferencePreview = null;

const human = value => String(value ?? "").replaceAll("_", " ").replace(/\b\w/g, char => char.toUpperCase());
const escapeHTML = value => String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);

const isImageReference = file => file?.type?.startsWith("image/");
const isSkillReference=file=>/\.(md|txt|json|ya?ml)$/i.test(file.name);
const isVideoReference = file => file?.type?.startsWith("video/");
const isAudioReference = file => ["audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav"].includes(file?.type);
const referenceFileKey = file => `${file.webkitRelativePath||file.name}:${file.size}:${file.lastModified}:${file.type}`;
const mentionLabel = file => `@${isSkillReference(file)?file.webkitRelativePath||file.name:file.name}`;
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
  const hint=document.createElement('p');hint.id='duration-validation-popover';hint.className='composer-validation';hint.setAttribute('role','alert');hint.textContent=I18n.text('Each video can be at most 30 seconds.');promptEditor().parentElement.append(hint);
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
    } else if(isSkillReference(file)){
      const badge=document.createElement('span');badge.textContent=/SKILL\.md$/i.test(file.name)?'Skill':'TXT';item.append(badge);item.title=file.webkitRelativePath||file.name;
    } else if (isVideoReference(file)) {
      const icon = document.createElement("span");icon.textContent = "▶";icon.className = "reference-video-icon";
      item.title = I18n.text('Local video preview only');item.append(icon);
      item.addEventListener('dblclick',event=>{event.preventDefault();const dialog=document.createElement('dialog');dialog.className='video-player-dialog';const video=document.createElement('video');video.controls=true;video.src=file.reference_url||URL.createObjectURL(file);const close=document.createElement('button');close.type='button';close.textContent=I18n.text('Close');close.onclick=()=>dialog.close();dialog.append(close,video);document.body.append(dialog);dialog.addEventListener('close',()=>{video.pause();URL.revokeObjectURL(video.src);dialog.remove();});dialog.showModal();});
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

function waitForVideoEvent(video, event, start) {
  return new Promise((resolve, reject) => {
    const onSuccess = () => { cleanup(); resolve(); };
    const onError = () => { cleanup(); reject(new Error("This browser could not read the selected video.")); };
    const timer = window.setTimeout(() => {
      cleanup();
      reject(new Error("The browser timed out reading a video frame."));
    }, 10000);
    const cleanup = () => {
      window.clearTimeout(timer);
      video.removeEventListener(event, onSuccess);
      video.removeEventListener("error", onError);
    };
    video.addEventListener(event, onSuccess, { once: true });
    video.addEventListener("error", onError, { once: true });
    try {
      start?.();
    } catch (error) {
      cleanup();
      reject(error);
    }
  });
}

function evidenceWindows(duration, policy = {}) {
  const scale = duration / (Number(policy.planned_duration_seconds) || duration);
  const hints = new Map((policy.shot_density || []).map(hint => [hint.shot_id, hint]));
  const focus = new Set(policy.focus_shot_ids || []);
  const windows = (policy.shot_windows || []).flatMap(window => {
    if (!/^S[1-6]$/.test(window.shot_id) || !(window.end_seconds > window.start_seconds)) return [];
    const hint = hints.get(window.shot_id);
    return [{
      shotId: window.shot_id,
      start: window.start_seconds * scale,
      end: Math.min(duration, window.end_seconds * scale),
      interval: Number(hint?.target_interval_seconds) || (focus.has(window.shot_id) ? 2 : 4),
      reason: hint?.reason || (focus.has(window.shot_id) ? "Planned visual risk" : "Baseline coverage"),
    }];
  });
  return windows.length ? windows : [{shotId: "S1", start: 0, end: duration, interval: 4, reason: "Baseline coverage"}];
}

function baselineEvidencePlan(windows, limit) {
  const counts = windows.map(() => 1);
  const baselineLimit = Math.max(windows.length, limit - 5);
  while (counts.reduce((total, count) => total + count, 0) < baselineLimit) {
    const demand = windows.map((window, index) => (window.end - window.start) / (window.interval * counts[index]));
    const choice = demand.indexOf(Math.max(...demand));
    if (Math.max(...demand) <= 1.05 && counts.reduce((total, count) => total + count, 0) >= 3) break;
    counts[choice] += 1;
  }
  return windows.flatMap((window, index) => Array.from({length: counts[index]}, (_, frame) => ({
    timestamp: window.start + (window.end - window.start) * (frame + 0.5) / counts[index],
    expected_shot_id: window.shotId,
    sampling_reason: `Baseline · ${window.reason}`,
  })));
}

function imageDifference(a, b) {
  let total = 0;
  for (let index = 0; index < a.length; index += 4) {
    total += Math.abs(a[index] - b[index]) + Math.abs(a[index + 1] - b[index + 1]) + Math.abs(a[index + 2] - b[index + 2]);
  }
  return total / (a.length * 0.75 * 255);
}

function refinementProbes(windows, baseline, missingShots, extraSlots) {
  const probes = [];
  for (const [windowIndex, window] of windows.entries()) {
    const frames = baseline.filter(frame => frame.expected_shot_id === window.shotId);
    const before = baseline.findLast(frame => frame.expected_shot_id === windows[windowIndex - 1]?.shotId);
    const after = baseline.find(frame => frame.expected_shot_id === windows[windowIndex + 1]?.shotId);
    const edges = [{timestamp: window.start}, ...frames, {timestamp: window.end}];
    for (let index = 0; index < edges.length - 1; index++) {
      const left = edges[index], right = edges[index + 1];
      const gap = right.timestamp - left.timestamp;
      if (gap < Math.max(0.16, Math.min(0.4, window.interval / 3))) continue;
      const nearby = [left, right].filter(frame => frame.preview);
      if (!nearby.length) continue;
      const neighbor = index === 0 ? before : index === edges.length - 2 ? after : null;
      const change = nearby.length === 2 ? imageDifference(left.preview, right.preview)
        : neighbor ? imageDifference(nearby[0].preview, neighbor.preview) : 0;
      const missing = missingShots.has(window.shotId);
      const risk = gap / window.interval;
      probes.push({
        timestamp: (left.timestamp + right.timestamp) / 2,
        expected_shot_id: window.shotId,
        nearby, missing,
        priority: (missing ? 2 : 0) + 4 * change + Math.min(2, risk),
      });
    }
  }
  return probes.sort((a, b) => b.priority - a.priority).slice(0, Math.min(12, extraSlots * 2));
}

function additionalEvidence(probe, frame) {
  const novelty = Math.min(...probe.nearby.map(other => imageDifference(frame.preview, other.preview)));
  // Drop probes that merely repeat their neighbors. Prior visual gaps can
  // warrant a smaller change, such as a brief gesture or a prop moving.
  const threshold = probe.missing ? 0.014 : 0.035;
  if (novelty < threshold) return null;
  return {
    ...frame,
    sampling_reason: probe.missing ? "Refined · prior visual evidence gap" : "Refined · visible change between samples",
    rank: probe.priority + novelty * 4,
  };
}

function missingVisualShots(state) {
  const criteria = new Map((state.creative_spec.acceptance_criteria || []).map(item => [item.requirement_id, item]));
  return new Set((state.latest_report?.atomic_checks || []).flatMap(check => {
    const criterion = criteria.get(check.requirement_id);
    if (check.status !== "insufficient_evidence" || !criterion) return [];
    const observed = String(check.observed || "");
    const audioLimit = /(?:cannot|could not|unable to|can't)\s+(?:verify|confirm|hear|assess)[^.;]*(?:audio|spoken|dialogue|lip[ -]?sync|English line)|(?:无法|不能)[^。；]*(?:听清|音频|音轨|台词|对白|口型)/i;
    return audioLimit.test(observed) ? [] : [criterion.shot_id];
  }));
}

function evidencePolicy(spec) {
  const contract = spec.evaluation_contract?.evidence_plan?.sampling_policy;
  return contract?.shot_density ? contract : spec.evidence_sampling || contract || {};
}

async function extractEvaluationFrames(file, evidenceSampling, missingShots = new Set()) {
  const url = URL.createObjectURL(file);
  const video = document.createElement("video");
  video.preload = "auto";
  video.muted = true;
  video.playsInline = true;
  try {
    await waitForVideoEvent(video, "loadedmetadata", () => { video.src = url; });
    if (!Number.isFinite(video.duration) || video.duration <= 0) throw new Error("The video duration could not be read.");
    const windows = evidenceWindows(video.duration, evidenceSampling);
    const limit = Math.max(windows.length + 1, Math.min(18, Number(evidenceSampling?.max_frames) || 18));
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d", { alpha: false });
    const probeCanvas = document.createElement("canvas");
    probeCanvas.width = 24;
    probeCanvas.height = 16;
    const probeContext = probeCanvas.getContext("2d", { alpha: false, willReadFrequently: true });
    const scale = Math.min(1, 512 / Math.max(video.videoWidth || 512, video.videoHeight || 512));
    canvas.width = Math.max(1, Math.round((video.videoWidth || 320) * scale));
    canvas.height = Math.max(1, Math.round((video.videoHeight || 180) * scale));

    async function readFrame(sample) {
      const timestamp = Math.max(0, Math.min(video.duration - 0.02, sample.timestamp));
      if (Math.abs(video.currentTime - timestamp) > 0.001) {
        await waitForVideoEvent(video, "seeked", () => { video.currentTime = timestamp; });
      }
      if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
        await waitForVideoEvent(video, "loadeddata");
      }
      probeContext.drawImage(video, 0, 0, probeCanvas.width, probeCanvas.height);
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
      return {
        timestamp: Number(video.currentTime.toFixed(2)),
        expected_shot_id: sample.expected_shot_id,
        sampling_reason: sample.sampling_reason,
        preview: probeContext.getImageData(0, 0, probeCanvas.width, probeCanvas.height).data,
        data_url: canvas.toDataURL("image/jpeg", 0.68),
      };
    }

    const baseline = [];
    for (const sample of baselineEvidencePlan(windows, limit)) baseline.push(await readFrame(sample));
    const candidates = [];
    const extraSlots = limit - baseline.length;
    for (const probe of refinementProbes(windows, baseline, missingShots, extraSlots)) {
      try {
        const addition = additionalEvidence(probe, await readFrame(probe));
        if (addition) candidates.push(addition);
      } catch (_) {
        // A supplemental seek must not discard the usable baseline.
        break;
      }
    }
    candidates.sort((a, b) => b.rank - a.rank);
    return [...baseline, ...candidates.slice(0, extraSlots)]
      .sort((a, b) => a.timestamp - b.timestamp)
      .map(({preview, rank, ...frame}, index) => ({...frame, frame_id: `F${index + 1}`}));
  } finally {
    video.removeAttribute("src");
    video.load();
    URL.revokeObjectURL(url);
  }
}

function projectStage(status) {
  if(['skill_preparation_required','skill_preparation_failed','planning_failed','awaiting_skill_clarification'].includes(status))return 'preparation';
  if (status === "ready_for_brief") return "brief";
  if (status === "awaiting_context_confirmation") return "context";
  if (["planning_ready", "needs_segmentation"].includes(status)) return "plan";
  if (["awaiting_first_video", "generating_video", "generated_video_ready", "video_complete"].includes(status)) return "version";
  return "quality";
}

function renderProjectStatus(state) {
  const characterPlan=state.creative_spec.character_reference_plan;
  const stage = ['planning_ready','awaiting_first_video'].includes(state.status)&&characterPlan?.required ? 'characters' : projectStage(state.status);
  const labels = {
    preparation: [I18n.language==='zh'?'创作规划尚未完成':'Creative planning incomplete',state.creative_spec.planning_notice,'Skill',I18n.language==='zh'?'在对话中继续':'Continue in conversation'],
    brief: ["Ready for a creative brief", "Describe the video to the Creative Agent to begin the controlled production loop.", "Brief", "Open Creative Agent"],
    context: ["Creative Agent needs one decision", "A missing context field could change a high-impact generation constraint.", "Context", "Answer the Agent"],
    plan: state.status === "needs_segmentation"
      ? ["Multi-segment plan required", "The Agent determined this brief should not be compressed into one 30-second clip. Revise the brief or plan separate segments.", "Segment plan", "Revise brief"]
      : ["Creative plan ready", "The creative plan and complete video prompt are ready. Continue in the Agent.", "Plan ready", "Generate video"],
    characters: ["Character reference stage", "Prepare three views of the important characters before video generation.", "Character references", "Generate three views"],
    version: state.status === "video_complete"
      ? [I18n.language === "zh" ? "视频已保存" : "Video saved", I18n.language === "zh" ? "已按要求结束流程，跳过质检和修复。" : "The workflow ended after generation.", "Version", "Watch video"]
      : ["Video version in production", "The approved plan is being rendered or prepared as evidence for evaluation.", "Version", "Wait for video evidence"],
    quality: ["Quality control loop active", "Review the selected version, its evidence-backed quality result, and the Agent's next decision.", "Quality", "Review version result"],
  };
  if(characterPlan?.status==='ready')labels.characters=["Creative plan ready","Character references attached to video","Plan ready","Generate V1"];
  const [title, copy, badge, next] = labels[stage];
  document.querySelector("#project-status-title").textContent = title;
  document.querySelector("#project-status-copy").textContent = copy;
  document.querySelector("#project-status-badge").textContent = badge;
  document.querySelector("#project-current-version").textContent = state.current_version_id === "none" ? "No version yet" : state.current_version_id.toUpperCase();
  const latestAsset = state.current_video_asset || (state.creative_spec || {}).reference_asset;
  document.querySelector("#project-latest-asset").textContent = latestAsset?.name || "Awaiting input";
  document.querySelector("#project-next-action").textContent = next;
  const sequence = ["brief", "context", "plan", "characters", "version", "quality"];
  const stageIndex = stage==='preparation'?1:sequence.indexOf(stage);
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
  if(context.skill_application?.length)entries.push({kind:'skill',title:'Uploaded Skills applied',detail:(context.uploaded_skills||[]).map(s=>s.path).join(' · ')});
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
  if (binding?.capability_profile?.profile_id&&!['skill_preparation_required','skill_preparation_failed','planning_failed','awaiting_skill_clarification'].includes(state.status)) {
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
      detail: I18n.text(releaseLabel(report.release_decision)),
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
let chatOutbox=[];
function paintOutbox(state,log){
  for(const item of chatOutbox){
    if((state.conversation||[]).slice(item.base).some(m=>m.role==='user'&&m.text.trim()===item.text.trim()))continue;
    const bubble=document.createElement('article');bubble.className='agent-message agent-message-user';bubble.dataset.messageId=item.id;
    const text=document.createElement('p');text.textContent=item.text;text.translate=false;bubble.append(text);
    const status=document.createElement('small');status.className='chat-send-status';status.textContent=I18n.text(item.failed?'Not sent':'Sending…');bubble.append(status);
    if(item.failed){const retry=document.createElement('button');retry.type='button';retry.className='chat-retry';retry.textContent=I18n.text('Retry');retry.addEventListener('click',async()=>{
      if(document.querySelector('#create-plan').disabled)return;
      retry.disabled=true;
      try{const latest=await endpoint('/api/state');render(latest);
        if((latest.conversation||[]).slice(item.base).some(m=>m.role==='user'&&m.text.trim()===item.text.trim())){chatOutbox=chatOutbox.filter(m=>m!==item);renderConversation(latest);return;}
        await submitChat({preventDefault(){},currentTarget:document.querySelector('#project-form'),retry:item});
      }catch(_){retry.disabled=false;}
    });bubble.append(retry);}
    log.append(bubble);
    if(!item.failed){const working=document.createElement('article');working.className='agent-message agent-message-system chat-working';working.setAttribute('role','status');working.textContent=I18n.text('Agent is thinking…');log.append(working);}
  }
}
function renderConversation(state) {
  const log=document.querySelector("#agent-conversation"), feed=document.querySelector("#creative-agent-feed");
  const messages=state.conversation || [], signature=I18n.language+state.status+state.execution_mode+state.autopilot_paused+JSON.stringify(messages)+JSON.stringify(chatOutbox.map(m=>[m.id,m.failed]));
  if(signature===renderedConversation)return;
  const follow=feed.scrollHeight-feed.scrollTop-feed.clientHeight<48, scroll=feed.scrollTop;
  const first=renderedConversation==="";
  renderedConversation=signature;
  log.replaceChildren();
  for(const message of messages) {
    if(message.kind==='creative_clarification'){
      const index=messages.indexOf(message),previousUser=messages.slice(0,index).findLastIndex(m=>m.role==='user');
      if(messages.slice(previousUser+1,index).some(m=>m.kind==='creative_clarification'))continue;
    }
    const article=document.createElement("article"),text=document.createElement("p");
    article.className="agent-message agent-message-"+(message.role==="user"?"user":"system");
    text.setAttribute("translate","no");text.textContent=message.role==="assistant"&&message.kind?I18n.text(message.text):message.text;
    article.append(text);log.append(article);
  }
  const progress=state.status === 'generating_video' ? '视频正在生成，完成后会在主界面展示。' : '';
  if(progress&&!autopilotIsRunning(state)){const item=document.createElement('article');item.className='agent-message agent-message-system';const text=document.createElement('p');text.textContent=I18n.text(progress);item.append(text);log.append(item);}
  paintOutbox(state,log);
  log.hidden=!messages.length&&!progress&&!chatOutbox.length;
  requestAnimationFrame(()=>{feed.scrollTop=follow||first?feed.scrollHeight:scroll;});
}

function renderPlanLanguage(spec) {
  const cadence = new Map((evidencePolicy(spec).shot_density || []).map(shot => [shot.shot_id, shot.target_interval_seconds]));
  document.querySelector("#shots").innerHTML=spec.shots.map(shot=>{
    const interval = cadence.get(shot.shot_id);
    const sampling = interval ? (I18n.language === "zh" ? ` · 预设抽帧间隔 ${interval} 秒` : ` · Target sampling interval ${interval}s`) : "";
    const timing=Number.isFinite(Number(shot.start_seconds))&&Number.isFinite(Number(shot.end_seconds))
      ? `<small class="shot-timing">${escapeHTML(shot.start_seconds)}–${escapeHTML(shot.end_seconds)}s · ${escapeHTML(I18n.text(shot.critical_moment || "planned visual beat"))}${escapeHTML(sampling)}</small>`:"";
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
  question.hidden = true;
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
  composer.dataset.placeholder=I18n.text("Message the Agent · Enter to send, Shift+Enter for a new line");
  attachments.hidden = !selectedReferenceItems.length;
  submit.textContent = isInitialBrief ? "Generate control plan" : "Send update";
  const budgetControl = document.querySelector("#repair-budget-control");
  budgetControl.hidden = !hasPlan;
  {
    const autopilot = state.execution_mode === "autopilot";
    const paused = Boolean(state.autopilot_paused);
    const button = document.querySelector("#autopilot-toggle");
    button.hidden = false;
    button.innerHTML='<svg class="ui-icon" aria-hidden="true"><use href="#i-agent"/></svg>';
    button.setAttribute("aria-pressed",String(autopilot&&!paused));
    const tooltip = I18n.text(autopilot&&!paused ? '暂停 Agent 自主代理' : '开启 Agent 自主代理');
    button.dataset.tooltip = tooltip;
    button.setAttribute("aria-label", tooltip);
    button.disabled = false;
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

function releaseLabel(decision) {
  if (decision === "pass") return "Quality gate passed";
  if (decision === "fail") return "Repair required";
  return "More evidence required";
}

function renderQuality(scores, reports, report) {
  const overall = diagnosticScore(report);
  const audioCheck = (report.atomic_checks || []).find(item => item.evidence_mode === "audio_probe");
  const audioGate = audioCheck
    ? `<span><i class="${audioCheck.status === "pass" ? "good" : ""}"></i>Audio presence ${human(audioCheck.status)}</span>`
    : `<span><i></i>Audio gate not required</span>`;
  const decision = report.release_decision || "insufficient_evidence";
  const releaseText = releaseLabel(decision);
  const coverage = report.evidence_coverage || {};
  const criticalCoverage = `${coverage.critical_requirements_evidenced || 0} / ${coverage.critical_requirements_total || 0}`;
  const profile = report.rubric_profile || {};
  const profileLabel = I18n.text(profile.label || "Controlled video");
  const profileReason = profile.selection_reason || "The Agent selected this rubric from the approved brief and control plan.";
  const labels = profile.dimension_labels || {};
  const dimensionLabel = name => I18n.text(labels[name] || human(name));
  const configuredDimensions = (profile.release_dimensions || []).filter(name => name in scores);
  const dimensions = (configuredDimensions.length ? configuredDimensions : Object.keys(scores))
    .filter(name => name !== "overall" && name !== "audio_visual_alignment")
    .map(name => [name, Number(scores[name])])
    .filter(([, value]) => Number.isFinite(value));
  const inactive = (profile.inactive_dimensions || [])
    .map(dimensionLabel);
  const evidenceByDimension = report.dimension_evidence || {};
  const dimensionEvidenceText = dimensions.map(([name]) => {
    const item = evidenceByDimension[name];
    if (!item || !item.requirements_total) return I18n.text(`${dimensionLabel(name)}: policy axis`);
    return I18n.text(`${dimensionLabel(name)}: ${item.requirements_evidenced || 0}/${item.requirements_total} evidenced`);
  });
  const displayedDimensionCount = dimensions.length || 5;
  const overallFloor = Number(profile.overall_floor || 3);
  return `<div class="quality-topline">
      <div class="overall-card"><span>Diagnostic quality</span><strong>${overall.toFixed(1)}<em>/5</em></strong></div>
      <div class="threshold-card"><span class="label">Release policy</span><strong>${releaseText}</strong><div class="threshold-list"><span><i class="${overall >= overallFloor ? "good" : ""}"></i>Diagnostic quality ≥ ${overallFloor.toFixed(1)}</span><span><i class="${decision === "pass" ? "good" : ""}"></i>Critical evidence ${criticalCoverage}</span><span><i class="${decision === "pass" ? "good" : ""}"></i>Active dimensions (${displayedDimensionCount}) ≥ ${Number(profile.dimension_floor || 3).toFixed(1)}</span>${audioGate}</div></div>
    </div>
    <div class="rubric-profile"><span>Selected rubric · ${escapeHTML(profileLabel)}</span><small>${escapeHTML(profileReason)}</small></div>
    <div class="trend-frame"><div class="trend-head"><strong>${reports.length > 1 ? "Quality trajectory" : "Initial quality baseline"}</strong><span>Publish ≥ ${overallFloor.toFixed(1)} · evidence-gated</span></div><canvas id="trend-canvas" class="trend-canvas" role="img" aria-label="Quality score trend"></canvas></div>
    <div class="bar-chart">${dimensions.map(([name, value]) => `<div class="bar-row ${scoreClass(value)}"><span class="bar-label">${escapeHTML(dimensionLabel(name))}</span><span class="bar-track"><i class="bar-fill" style="width:${value / 5 * 100}%"></i></span><span class="bar-value">${value.toFixed(1)}</span></div>`).join("")}</div><div class="rubric-profile"><small>Score evidence — ${escapeHTML(dimensionEvidenceText.join(" · "))}</small></div>${inactive.length ? `<div class="rubric-profile"><small>Excluded for this plan — not part of the diagnostic score: ${escapeHTML(inactive.join(" · "))}</small></div>` : ""}`;
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
  if (review.status === "generation_complete") return I18n.language === "zh" ? "已生成" : "Generated";
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
  const failureDetail = active?.observation_error || "";
  const copyrightRejected = failedGeneration
    && failureDetail.includes("OutputVideoSensitiveContentDetected.PolicyViolation")
    && /copyright/i.test(failureDetail);
  generationFailure.hidden = !failedGeneration;
  generationFailureDetail.textContent = copyrightRejected
    ? (I18n.language === "zh" ? "Seedance 拒绝了这次输出，原因是可能涉及版权限制。没有返回成片。" : "Seedance rejected the output because it may be subject to copyright restrictions. No video was returned.")
    : failedGeneration ? (failureDetail || "The video provider did not return a completed video.") : "";
  document.querySelector("#generation-failure-hint").hidden = copyrightRejected;
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

const reviewTranslations = new Map();
let displayedReviewKey = "";
let displayedOriginalReviewKey = "";

function renderEvidenceChecks(report, translateText) {
  const checks = report.atomic_checks || [];
  if (!checks.length) return '<div class="empty">No planned acceptance checks were returned.</div>';
  const statusLabels = {pass: "Pass", fail: "Fail", insufficient_evidence: "Insufficient evidence"};
  const field = (label, value) => value ? `<p><strong>${label}</strong> <span translate="no">${escapeHTML(translateText(value))}</span></p>` : "";
  return checks.map(item => {
    const status = Object.hasOwn(statusLabels, item.status) ? item.status : "insufficient_evidence";
    const evidence = (item.evidence || []).map(frame => {
      const timestamp = frame.source === "audio_probe"
        ? I18n.text(`Audio interval · ${Number(frame.start_seconds || 0).toFixed(2)}–${Number(frame.end_seconds || 0).toFixed(2)}s`)
        : `${frame.frame_id} · ${Number(frame.timestamp).toFixed(2)}${I18n.language === "zh" ? " 秒" : "s"}`;
      return `${escapeHTML(timestamp)} <span translate="no">${escapeHTML(translateText(frame.description))}</span>`;
    }).join("<br>");
    return `<div class="case ${status}"><div class="case-top"><h3>${escapeHTML(item.shot_id)} · <span translate="no">${escapeHTML(translateText(item.requirement))}</span></h3><span class="severity ${status}">${statusLabels[status]}</span></div>
      ${field("Expected:", item.expected)}${field("Observed:", item.observed)}
      <p><strong>Evidence:</strong> ${evidence || `<span>${item.evidence_mode === "audio_probe" ? "No valid audio evidence returned." : "No valid frame evidence returned."}</span>`}</p>
      ${field("Repair hint:", item.repair_hint)}</div>`;
  }).join("");
}

function renderReviewText(state, review, retry = false) {
  const report = review?.report;
  const language = I18n.language;
  const summary = document.querySelector("#summary");
  const evidence = document.querySelector("#bad-cases");
  const status = document.querySelector("#review-translation-status");
  const retryButton = document.querySelector("#retry-review-translation");
  const original = document.querySelector("#original-review");
  const originalContent = document.querySelector("#original-review-content");
  const originalKey = JSON.stringify([state.project_id, review?.version_id, report, review?.observation_error]);
  const key = report ? language + originalKey : "";
  if (displayedReviewKey !== key || displayedOriginalReviewKey !== originalKey) {
    if (displayedOriginalReviewKey !== originalKey) original.open = false;
    displayedOriginalReviewKey = originalKey;
    original.hidden = !report && !review?.observation_error;
    originalContent.innerHTML = report
      ? `<p class="summary" translate="no">${escapeHTML(report.summary)}</p>${renderEvidenceChecks(report, value => value)}`
      : `<p translate="no">${escapeHTML(review?.observation_error)}</p>`;
  }
  displayedReviewKey = key;
  if (!report) {
    summary.hidden = status.hidden = retryButton.hidden = true;
    evidence.innerHTML = `<div class="empty">${review?.observation_error ? "Observation unavailable" : "No observation yet."}</div>`;
    return;
  }

  if (retry) reviewTranslations.delete(key);
  if (!reviewTranslations.has(key)) {
    reviewTranslations.set(key, {pending: true});
    endpoint("/api/review-translation", {
      method: "POST", body: JSON.stringify({version_id: review.version_id, language}),
    }).then(({translations}) => {
      reviewTranslations.set(key, {translations});
    }).catch(() => {
      reviewTranslations.set(key, {failed: true});
    }).finally(() => {
      if (displayedReviewKey === key && !document.querySelector("#evaluation-section").hidden) {
        renderReviewText(renderedState, selectedReview(renderedState));
      }
    });
  }
  const result = reviewTranslations.get(key);
  status.hidden = !result.pending && !result.failed;
  status.textContent = result.failed ? "Report translation is unavailable. You can still view the original." : "Translating this review…";
  retryButton.hidden = !result.failed;
  summary.hidden = !result.translations || !report.summary;
  summary.textContent = result.translations?.[report.summary] || "";
  evidence.innerHTML = result.translations
    ? renderEvidenceChecks(report, value => result.translations[value] || "")
    : "";
}

document.querySelector("#retry-review-translation").addEventListener("click", () => {
  if (renderedState) renderReviewText(renderedState, selectedReview(renderedState), true);
});

function renderEvaluation(state) {
  const review = selectedReview(state);
  const report = review?.report || null;
  const observationError = review?.observation_error || "";
  const source = document.querySelector("#observation-source");
  source.textContent = observationError ? "Observation unavailable" : !report ? "Awaiting observation" : report.observation_source === "doubao_seed_2_1_pro" ? "Doubao Seed 2.1 Pro" : report.observation_source === "fixture" ? "Demo fixture" : "Archived model result";
  const scores = document.querySelector("#scores");
  const emptyGate = document.querySelector("#gate-empty");
  if (report) {
    const reports = (state.version_reviews || []).map(item => item.report).filter(Boolean);
    emptyGate.hidden = true;
    scores.innerHTML = renderQuality(report.scores, reports, report);
    const canvas = scores.querySelector("#trend-canvas");
    requestAnimationFrame(() => { if (canvas.isConnected) drawTrend(canvas, reports, Number(report.rubric_profile?.overall_floor || 3)); });
  } else {
    emptyGate.hidden = false;
    emptyGate.textContent = observationError ? "Observation unavailable" : "Run the quality loop to evaluate this version.";
    scores.innerHTML = "";
  }
  renderReviewText(state, review);

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
  document.querySelector("#reference-detail").textContent = referenceMedia.filter(a=>!a.local_only).length
    ? "Reference materials attached to video generation."
    : "The Agent will preserve constraints extracted from your brief.";
  for(const asset of referenceVideos.filter(a=>a.local_only)){
    const button=document.createElement('button');button.type='button';button.className='local-video-preview';button.textContent='▶ '+asset.name+' · '+I18n.text('Local preview');
    button.addEventListener('click',()=>{const dialog=document.createElement('dialog');dialog.className='video-player-dialog';const video=document.createElement('video');video.controls=true;video.src='/api/local-reference-video?project='+encodeURIComponent(renderedState.project_id)+'&index='+referenceMedia.indexOf(asset);const close=document.createElement('button');close.type='button';close.textContent=I18n.text('Close');close.onclick=()=>dialog.close();dialog.append(close,video);document.body.append(dialog);dialog.onclose=()=>{video.pause();dialog.remove();};dialog.showModal();});
    document.querySelector('#reference-detail').append(button);
  }
  const skills=spec.enriched_context?.uploaded_skills||[];
  if(skills.length){const tag=document.createElement('span');tag.className='imported-skills';tag.textContent='Skill · '+[...new Set(skills.map(s=>s.path))].join('、');document.querySelector('#reference-detail').append(tag);}
  const referenceVisual = document.querySelector("#reference-visual");
  const referencePreview = document.querySelector("#reference-preview");
  referenceVisual.hidden = !(reference && reference.preview_data_url);
  if (!referenceVisual.hidden) referencePreview.src = reference.preview_data_url;

}

function render(state) {
  renderedState = state;
  if(state.conversation_only){renderCreativeAgent(state,state.creative_spec,state.creative_spec.enriched_context||{});return;}
  const context=state.creative_spec.enriched_context||{};
  const skillResult=document.querySelector('#skill-execution-result');
  skillResult.hidden=!context.video_prompt;
  document.querySelector('#skill-execution-copy').textContent=(context.skill_application||[]).map(step=>typeof step==='string'?step:JSON.stringify(step)).join('\n\n')+'\n\n'+(context.video_prompt||'');
  renderModelSelection(state);
  renderCharacterReferences(state);
  scheduleProductionDefaults(state);
  if (state.current_version_id !== lastCurrentVersionId) {
    selectedReviewVersionId = state.current_version_id;
    lastCurrentVersionId = state.current_version_id;
  }
  const spec = state.creative_spec;
  const hasPlan = Boolean(spec.goal && spec.shots.length)&&!['skill_preparation_required','skill_preparation_failed','planning_failed','awaiting_skill_clarification'].includes(state.status);
  const enrichedContext = spec.enriched_context || {};
  const promptInput = document.querySelector("#creative-prompt");
  renderProjectStatus(state);
  renderCreativeAgent(state, spec, enrichedContext);

  document.querySelector("#plan-section").hidden = !hasPlan;
  document.querySelector("#evaluation-section").hidden = !EVALUATION_STATES.has(state.status);
  if (!hasPlan) {
    document.querySelector("#agent-video-action").hidden = true;
    document.querySelector("#version-review-bar").hidden = true;
    window.dispatchEvent(new CustomEvent("vclstatechange", {detail:state}));
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
    : `修复预算：当前最多 ${budget} 轮。调整预算会暂停 Agent 自主代理，等待你开启 Agent 自主代理继续。`;
  budgetControl.removeAttribute("title");delete budgetControl.dataset.tooltip;
  budgetDecrease.disabled = !canAdjustBudget || budget <= state.iterations_used;
  budgetIncrease.disabled = !canAdjustBudget || budget >= 5;
  const videoStage = document.querySelector("#agent-video-action");
  const firstVideoStates = new Set(["planning_ready", "awaiting_first_video", "generating_video", "generated_video_ready", "video_complete", "ready_for_evaluation", "awaiting_repair_upload", "repair_uploaded", "published", "stopped_budget_exhausted"]);
  videoStage.hidden = !firstVideoStates.has(state.status);
  const generationControls = document.querySelector("#video-generation-controls");
  const readyCard = document.querySelector("#video-ready-card");
  const runLoop = document.querySelector("#run-loop");
  const generateButton = document.querySelector("#generate-video");
  const videoAsset = state.current_video_asset;
  const canRunQuality = state.status === "ready_for_evaluation" || state.status === "repair_uploaded";
  const videoStatus = document.querySelector("#agent-video-status");
  const evidenceFailed = state.status === "generated_video_ready"
    && generatedEvidenceError?.projectId === state.project_id
    && generatedEvidenceError?.versionId === state.current_version_id;
  const retryEvidence = document.querySelector("#retry-generated-evidence");
  retryEvidence.hidden = !evidenceFailed;
  retryEvidence.textContent = I18n.language === "zh" ? "重试取样" : "Retry frame sampling";
  runLoop.hidden = evidenceFailed;
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
    document.querySelector("#video-stage-copy").textContent = evidenceFailed
      ? (I18n.language === "zh" ? "成片已保存。取样未完成，可用现有视频重试。" : "The video is saved. Retry sampling frames from this version.")
      : "Generation completed. The Agent is sampling risk-aware visual evidence before quality evaluation.";
    generationControls.hidden = true;
    readyCard.hidden = true;
    videoStatus.textContent = evidenceFailed
      ? (I18n.language === "zh" ? "取样失败" : "Frame sampling failed")
      : "Preparing evidence";
    runLoop.disabled = true;
    runLoop.textContent = "Preparing evidence…";
  } else if (state.status === "video_complete") {
    generationControls.hidden = true;
    readyCard.hidden = true;
    document.querySelector("#video-stage-copy").textContent = I18n.language === "zh" ? "视频已保存，可在版本区播放。" : "The saved video is available in the version panel.";
    videoStatus.textContent = I18n.language === "zh" ? "已生成" : "Generated";
    runLoop.disabled = true;
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

function importReferenceFiles(event) {
  const incoming = Array.from(event.target.files || []);
  const existing = new Set(selectedReferenceItems.map(referenceFileKey));
  // `@filename` is the user-facing binding syntax.  A second asset with the
  // same filename would make that binding ambiguous, even when its bytes are
  // different, so keep filenames unique inside one creative brief.
  const existingNames = new Set(selectedReferenceItems.map(file => (isSkillReference(file)?file.webkitRelativePath||file.name:file.name).toLowerCase()));
  let imageCount = selectedReferenceItems.filter(isImageReference).length;
  let audioCount = selectedReferenceItems.filter(isAudioReference).length;
  const rejected = [];
  incoming.forEach(file => {
    if(file.webkitRelativePath&&file.webkitRelativePath.split("/").some(p=>p.startsWith(".")||["node_modules","__pycache__"].includes(p)))return;
    if (!isImageReference(file) && !isAudioReference(file) && !isVideoReference(file) && !isSkillReference(file)) {
      rejected.push(`${file.name}: 不支持此文件格式`);
      return;
    }
    if (existingNames.has((isSkillReference(file)?file.webkitRelativePath||file.name:file.name).toLowerCase())) {
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
    if(isVideoReference(file)&&(file.size>32*1024*1024||selectedReferenceItems.filter(isVideoReference).length>=10)){rejected.push(file.name+': 最多10个视频，每个不超过32MB');return;}
    if(isSkillReference(file)&&(file.size>32000||selectedReferenceItems.filter(isSkillReference).length>=50)){rejected.push(file.name+': 文本文件过大或数量超过50个');return;}
    selectedReferenceItems.push(file);
    existing.add(referenceFileKey(file));
    existingNames.add((isSkillReference(file)?file.webkitRelativePath||file.name:file.name).toLowerCase());
    if (isImageReference(file)) imageCount += 1;
    else if(isAudioReference(file)) audioCount += 1;
  });
  event.target.value = "";
  renderReferenceAttachments();
  updateMentionMenu();
  document.querySelector('#brief-attachments').hidden=!selectedReferenceItems.length;
  if (rejected.length) I18n.alert(rejected.join("\n"));
}
document.querySelector('#reference-media').addEventListener('change',importReferenceFiles);
async function readLocalVideo(file){
  if(file.reference_url)return {name:file.name,mime_type:file.type,url:file.reference_url,duration_seconds:file.duration_seconds,size:0};
  const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(file);});
  const duration=await new Promise((resolve,reject)=>{const video=document.createElement('video');const url=URL.createObjectURL(file);video.preload='metadata';video.onloadedmetadata=()=>{URL.revokeObjectURL(url);resolve(video.duration);};video.onerror=()=>{URL.revokeObjectURL(url);reject(new Error('无法读取视频，请使用 MP4/MOV 文件。'));};video.src=url;});
  return {name:file.name,mime_type:file.type||'video/mp4',data_url:data,duration_seconds:duration,size:file.size,local_only:true};
}
async function serializeImports(prompt,files=selectedReferenceItems){
  const skill_files=await Promise.all(files.filter(isSkillReference).map(async file=>({path:file.webkitRelativePath||file.name,text:await file.text()})));
  const multimodal_assets=await Promise.all(files.filter(f=>!isSkillReference(f)).map(async(file,index)=>{
    const asset=isImageReference(file)?await readReferenceFile(file):isVideoReference(file)?await readLocalVideo(file):await readAudioReferenceFile(file);
    return {...asset,role:isImageReference(file)?'Visual reference':isVideoReference(file)?'reference_video':'Audio reference',input_order:index+1,prompt_mentions:promptMentionsFor(file,prompt)};
  }));return {skill_files,multimodal_assets};
}
document.querySelector("#creative-prompt").addEventListener("input", () => {
  // Typing after an over-limit warning replaces the temporary visual marker.
  // The next submission evaluates the edited content again.
  promptEditor().querySelectorAll(".duration-overlimit").forEach(marker => marker.replaceWith(document.createTextNode(marker.textContent || "")));
  hideDurationValidationPopover();
  updateMentionMenu();
});
document.querySelector("#creative-prompt").addEventListener("keyup", updateMentionMenu);
document.querySelector("#creative-prompt").addEventListener("keydown", event => {
  if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(!document.querySelector('#create-plan').disabled)document.querySelector('#project-form').requestSubmit();return;}
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
async function submitChat(event) {
  event.preventDefault();
  const prompt = event.retry?.text||editorText().trim();
  const submit = event.currentTarget.querySelector("button[type=submit]");
  if (!prompt || submit.disabled || !renderedState) return;
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
  const files=event.retry?.files||[...selectedReferenceItems];
  const outgoing=event.retry||{id:crypto.randomUUID(),text:prompt,files,base:renderedState?.conversation?.length||0};
  outgoing.failed=false;if(!event.retry)chatOutbox.push(outgoing);
  if(!event.retry){setEditorText('');clearReferenceAttachments();}
  renderConversation(renderedState);
  const feed=document.querySelector('#creative-agent-feed');feed.scrollTop=feed.scrollHeight;
  let accepted=false;
  try {
    if(!isInitialBrief&&files.length&&!outgoing.imported){render(await endpoint('/api/import-references',{method:'POST',body:JSON.stringify(await serializeImports(prompt,files))}));outgoing.imported=true;}
    const imports=isInitialBrief?await serializeImports(prompt,files):{};
    render(await endpoint('/api/chat',{method:'POST',body:JSON.stringify({message:prompt,...imports,language:I18n.language})}));
    accepted=true;chatOutbox=chatOutbox.filter(item=>item!==outgoing);renderConversation(renderedState);
    if(renderedState.chat_action==='generate')await generateVideo(null,true);
    else if(renderedState.chat_action==='quality')await runQualityLoop();
    else if(renderedState.chat_action==='recheck_quality')await runQualityLoop(true);
    else if(renderedState.chat_action==='retry_evidence')await prepareGeneratedEvidence(renderedState);
    accepted=true;
    if(!renderedState.conversation_only&&autopilotIsRunning(renderedState))await advanceAutopilot(renderedState);

  } catch (error) {
    outgoing.failed=true;
  } finally {
    if(accepted)chatOutbox=chatOutbox.filter(item=>item!==outgoing);
    renderConversation(renderedState);
    submit.disabled = false;
    submit.textContent = renderedState?.status === "ready_for_brief" ? "Generate control plan" : "Send update";
  }
}
document.querySelector("#project-form").addEventListener("submit",submitChat);
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
async function runQualityLoop(recheck = false) {
  if (qualityLoopPending) return;
  const current = renderedState;
  startQualityProgress();
  try {
    const options = { method: "POST" };
    if (recheck) {
      const url = current.current_video_asset?.local_video_url;
      if (!url) throw new Error("The saved video is unavailable for rechecking.");
      const video = await fetch(url, {cache: "no-store", signal: AbortSignal.timeout(20000)});
      if (!video.ok) throw new Error("The saved video could not be read.");
      const spec = current.creative_spec;
      const frames = await extractEvaluationFrames(await video.blob(), evidencePolicy(spec), missingVisualShots(current));
      if (renderedState?.project_id !== current.project_id) throw new Error("The project changed before the recheck.");
      options.body = JSON.stringify({reobserve: true, version_id: current.current_version_id, frames});
    }
    const state = await endpoint("/api/run-loop", options);
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
  generatedEvidenceError = null;
  render(state);
  const sameVersion = () => renderedState?.project_id === state.project_id
    && renderedState?.current_version_id === state.current_version_id;
  try {
    const response = await fetch(videoUrl, { cache: "no-store", signal: AbortSignal.timeout(20000) });
    if (!response.ok) throw new Error("The generated video could not be retrieved for evaluation.");
    const frames = await extractEvaluationFrames(await response.blob(), evidencePolicy(state.creative_spec));
    if (!sameVersion() || renderedState.status !== "generated_video_ready") return;
    const prepared = await endpoint("/api/register-generated-video", {
      method: "POST",
      body: JSON.stringify({ frames }),
      signal: AbortSignal.timeout(15000),
    });
    if (!sameVersion()) return;
    render(prepared);
    if (autopilotIsRunning(prepared)) {
      await runQualityLoop();
      await advanceAutopilot(renderedState);
    }
  } catch (error) {
    if (!sameVersion() || renderedState?.status !== "generated_video_ready") return;
    // Another tab may have registered the same version while we were sampling.
    try {
      const latest = await endpoint("/api/state", { signal: AbortSignal.timeout(5000) });
      if (!sameVersion() || latest.project_id !== state.project_id) return;
      if (latest.status !== "generated_video_ready") { render(latest); return; }
    } catch (_) { /* The retained video can be sampled again. */ }
    if (!sameVersion() || renderedState?.status !== "generated_video_ready") return;
    generatedEvidenceError = {projectId: state.project_id, versionId: state.current_version_id};
    render(renderedState);
    setCreativeAgentOpen(true);
  } finally {
    generatedEvidencePending = false;
  }
}
document.querySelector("#retry-generated-evidence").addEventListener("click", () => prepareGeneratedEvidence(renderedState));
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
let videoStartPending=false;
async function generateVideo(event, authorized=false) {
  if(videoStartPending)return;videoStartPending=true;
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
      if((authorized||autopilotIsRunning(renderedState))&&characters.status==='planned'){
        render(await endpoint('/api/generate-character-references',{method:'POST',body:JSON.stringify({plan_id:characters.id,model_id:characters.selected_model||characters.model_choice?.recommended_model})}));
      }
      button.disabled=false;return;
    }
    await recommendCurrentVideoModel();
    await saveGenerationSettings();
    await approveCurrentPlan();
    const state = await endpoint("/api/generate-video", { method: "POST" });
    render(state);
    pollGeneration();
  } catch (error) {
    button.disabled = false;
    try{render(await endpoint('/api/state'));}catch(_){}
    const notice=document.createElement('article');notice.className='agent-message agent-message-system';notice.textContent=I18n.text(error.message);document.querySelector('#agent-conversation').append(notice);
  } finally {videoStartPending=false;}
}
function autopilotIsRunning(state) {
  return state?.execution_mode === "autopilot" && !state.autopilot_paused;
}

async function advanceAutopilot(state) {
  if(location.hash==='#history'||document.body.dataset.workspace==='history')return;
  if (!autopilotIsRunning(state) || autopilotAdvancePending) return;
  const preparation=['skill_preparation_required','skill_preparation_failed','planning_failed','awaiting_skill_clarification'];
  if (![...preparation,"awaiting_first_video", "awaiting_repair_upload", "ready_for_evaluation", "repair_uploaded"].includes(state.status)) return;
  autopilotAdvancePending = true;
  try {
    if(preparation.includes(state.status)){
      const prepared=await endpoint('/api/retry-planning',{method:'POST',body:'{}'});render(prepared);
      if(autopilotIsRunning(prepared)&&prepared.status==='awaiting_first_video')await generateVideo();
    } else if (["awaiting_first_video", "awaiting_repair_upload"].includes(state.status)) {
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
document.querySelector("#run-loop").addEventListener("click", () => runQualityLoop(
  Boolean(renderedState?.latest_report && renderedState?.current_video_asset?.local_video_url)
));
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
window.openNewProject=()=>{
  document.querySelector('#new-project-name').value='';
  document.querySelector('#new-project-dialog').showModal();document.querySelector('#new-project-name').focus();
};
document.querySelector('#reset').addEventListener('click',()=>window.openNewProject('vcl'));
document.querySelector('#new-project-cancel').addEventListener('click',()=>document.querySelector('#new-project-dialog').close());
document.querySelector('#new-project-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;
  try{
    const title=document.querySelector('#new-project-name').value.trim();
    await endpoint('/api/reset',{method:'POST',body:JSON.stringify({title})});
    location.assign('/?new='+Date.now()+'#vcl');
  }catch(error){I18n.alert(error.message);button.disabled=false;}
});

refresh();

window.addEventListener("languagechange", () => {
  if (!renderedState) return;
  renderConversation(renderedState);
  renderExecutionTrace(renderedState);
  if (renderedState.creative_spec.shots?.length) {
    renderPlanLanguage(renderedState.creative_spec);
    renderReferenceMedia(renderedState.creative_spec);
  }
  if (!document.querySelector("#evaluation-section").hidden) renderEvaluation(renderedState);
});

// Shared project state for navigation and history.
window.VCLBridge={state:()=>renderedState,open:()=>setCreativeAgentOpen(true,true)};
