/* A catalog-backed binding layer for the existing imperative renderer.
 * Only known interface copy is localized. Never rewrite application state,
 * form values, contenteditable nodes, identifiers or the user's brief.
 */
(() => {
  const STORAGE_KEY = "video-control-loop-language";
  const normalize = text => String(text).replace(/\s+/g, " ").trim();
  const exact = new Map();
  const patterns = [];
  const escapeRegExp = text => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  let language = "zh";
  try { if (localStorage.getItem(STORAGE_KEY) === "en") language = "en"; } catch (_) { /* Private mode may deny storage. */ }

  for (const pair of window.I18N_CATALOG) {
    pair.forEach(source => {
      const key = normalize(source);
      if (!/§\d+§/.test(key)) {
        exact.set(key.toLowerCase(), pair);
        return;
      }
      const ids = [];
      const parts = key.split(/(§\d+§)/g).map(part => {
        if (/^§\d+§$/.test(part)) { ids.push(part); return "(.*?)"; }
        return escapeRegExp(part);
      });
      patterns.push({regex: new RegExp("^" + parts.join("") + "$", "i"), ids, pair,
        specificity: key.replace(/§\d+§/g, "").length});
    });
  }
  patterns.sort((a, b) => b.specificity - a.specificity);

  function translate(source, split = true) {
    const key = normalize(source);
    if (!key) return source;
    const pair = exact.get(key.toLowerCase());
    let translated;
    if (pair) translated = pair[language === "en" ? 0 : 1];
    else {
      for (const pattern of patterns) {
        const match = key.match(pattern.regex);
        if (!match) continue;
        const isLabelPattern = /^(Agent decided:|Agent is |Video tool |Audio presence |§0§: policy axis)/.test(pattern.pair[0]);
        const captures = Object.fromEntries(pattern.ids.map((id, i) => {
          const value = match[i + 1];
          const knownMessage = pattern.pair[0].startsWith("§0§ Evidence sampling will focus on") && id === "§0§";
          const label = (isLabelPattern || knownMessage) && exact.get(normalize(value).toLowerCase());
          return [id, label ? label[language === "en" ? 0 : 1] : value];
        }));
        translated = pattern.pair[language === "en" ? 0 : 1].replace(/§\d+§/g, id => captures[id] ?? "");
        break;
      }
    }
    if (translated === undefined && split) {
      const sentences = key.match(/[^。！？]+[。！？]|[^。！？]+$/g) || [];
      if (sentences.length > 1) {
        const localized = sentences.map(sentence => translate(sentence, false));
        if (localized.some((sentence, index) => sentence !== sentences[index])) translated = localized.join(language === "en" ? " " : "");
      }
    }
    if (translated === undefined) return source;
    const leading = String(source).match(/^\s*/)[0];
    const trailing = String(source).match(/\s*$/)[0];
    return leading + translated + trailing;
  }

  const textSources = new WeakMap();
  const attributeSources = new WeakMap();
  const attributes = ["aria-label", "title", "alt", "placeholder", "data-placeholder", "data-tooltip"];
  const protectedSelector = 'script, style, textarea, input, [contenteditable="true"], [data-i18n-ignore], [translate="no"]';
  function protectedNode(element) { return element?.closest(protectedSelector); }

  function localizeText(node) {
    if (protectedNode(node.parentElement)) return;
    const current = node.nodeValue;
    if (!current.trim()) return;
    const prior = textSources.get(node);
    const source = prior && current === prior.last ? prior.source : current;
    const last = translate(source);
    textSources.set(node, {source, last});
    if (current !== last) node.nodeValue = last;
  }

  function localizeAttributes(element) {
    // Inputs retain their value, but their accessible names/placeholders switch.
    if (element.closest('[data-i18n-ignore], [translate="no"], script, style')) return;
    const records = attributeSources.get(element) || {};
    for (const name of attributes) {
      if (!element.hasAttribute(name)) continue;
      const current = element.getAttribute(name);
      const prior = records[name];
      const source = prior && current === prior.last ? prior.source : current;
      const last = translate(source);
      records[name] = {source, last};
      if (current !== last) element.setAttribute(name, last);
    }
    attributeSources.set(element, records);
  }

  function localize(root = document.body) {
    if (!root) return;
    if (root.nodeType === Node.TEXT_NODE) { localizeText(root); return; }
    if (root.nodeType !== Node.ELEMENT_NODE) return;
    localizeAttributes(root);
    if (protectedNode(root)) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.nodeType === Node.TEXT_NODE) localizeText(node);
      else localizeAttributes(node);
    }
  }

  function setLanguage(next, persist = true) {
    if (next !== "zh" && next !== "en") return;
    language = next;
    document.documentElement.lang = next === "zh" ? "zh-CN" : "en";
    document.querySelectorAll("[data-language]").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.language === next));
    });
    if (persist) { try { localStorage.setItem(STORAGE_KEY, next); } catch (_) { /* Keep the in-memory preference. */ } }
    localize();
    window.dispatchEvent(new CustomEvent("languagechange", {detail: {language: next}}));
  }

  window.I18n = {
    text: translate, localize, setLanguage,
    get language() { return language; },
    alert(message) { window.alert(String(message).split("\n").map(line => translate(line)).join("\n")); },
  };
  document.querySelectorAll("[data-language]").forEach(button => {
    button.addEventListener("click", () => setLanguage(button.dataset.language));
  });
  setLanguage(language, false);
  new MutationObserver(records => {
    const roots = new Set();
    for (const record of records) {
      if (record.type === "childList") record.addedNodes.forEach(node => roots.add(node));
      else roots.add(record.target);
    }
    roots.forEach(root => { if (root.isConnected) localize(root); });
  }).observe(document.body, {subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: attributes});
})();
