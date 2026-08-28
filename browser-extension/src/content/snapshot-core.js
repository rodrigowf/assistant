/**
 * Snapshot + interaction core — all DOM logic, zero extension APIs.
 *
 * Deliberately free of any `chrome.*` dependency so it can be loaded into a
 * plain page for testing (see ../../test-fixtures/). `content-script.js` is a
 * thin messaging shim over this file; both are listed in the manifest's
 * content_scripts and therefore share one isolated world, so this file's
 * global is visible to the shim but never to the page.
 *
 * Coordinate contract (SPEC 2.1 / PLAN 0.1): all geometry is expressed as
 * proportions of the viewport in [0,1], which is also the screenshot's frame.
 * That makes devicePixelRatio and zoom cancel out.
 *
 * Not a module: manifest content scripts don't support ES module syntax.
 */

(() => {
  if (globalThis.__archieCore) return;

  // --- configuration --------------------------------------------------

  const DEFAULT_MAX_CHARS = 40_000;
  const MIN_IMAGE_PX = 32;
  const MAX_DATA_URI = 1024;

  const SKIP_TAGS = new Set([
    "SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "META", "LINK", "TITLE",
    "SVG", "CANVAS",
  ]);

  const BLOCK_TAGS = new Set([
    "ADDRESS", "ARTICLE", "ASIDE", "BLOCKQUOTE", "DD", "DETAILS", "DIALOG",
    "DIV", "DL", "DT", "FIELDSET", "FIGCAPTION", "FIGURE", "FOOTER", "FORM",
    "H1", "H2", "H3", "H4", "H5", "H6", "HEADER", "HGROUP", "HR", "LI", "MAIN",
    "NAV", "OL", "P", "PRE", "SECTION", "TABLE", "TBODY", "TD", "TFOOT", "TH",
    "THEAD", "TR", "UL", "VIDEO",
  ]);

  const INTERACTIVE_SELECTOR = [
    "a[href]", "button", "input", "select", "textarea", "summary",
    '[role="button"]', '[role="link"]', '[role="menuitem"]', '[role="tab"]',
    '[role="checkbox"]', '[role="radio"]', '[role="switch"]', '[role="option"]',
    "[contenteditable='']", '[contenteditable="true"]',
  ].join(",");

  // Attributes stable enough to build a selector from. Class names are
  // deliberately absent here — Tailwind / CSS-in-JS hashes change per build.
  const STABLE_ATTRS = [
    "data-testid", "data-test-id", "data-test", "data-qa", "name",
    "aria-label", "placeholder", "type",
  ];

  // --- snapshot state -------------------------------------------------

  const state = {
    generation: 0,
    refs: new Map(),          // ref string -> Element
    scroll: { x: 0, y: 0 },   // scroll position at snapshot time
  };

  // --- visibility -----------------------------------------------------

  function isVisible(el) {
    if (!el || el.nodeType !== Node.ELEMENT_NODE) return false;
    if (el.getAttribute && el.getAttribute("aria-hidden") === "true") return false;

    const style = getComputedStyle(el);
    if (style.display === "none") return false;
    if (style.visibility === "hidden" || style.visibility === "collapse") return false;
    if (parseFloat(style.opacity) === 0) return false;

    const rect = el.getBoundingClientRect();
    // Zero-size elements carry no text a user could read. Inputs are the
    // exception worth keeping only if they have real geometry, so no special
    // case here.
    if (rect.width === 0 && rect.height === 0) return false;
    return true;
  }

  // --- selector generation --------------------------------------------

  function isUnique(selector) {
    try {
      return document.querySelectorAll(selector).length === 1;
    } catch {
      return false;  // Malformed selector — treat as unusable.
    }
  }

  function quoteAttr(value) {
    return value.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  }

  /**
   * Build a CSS selector that resolves to exactly this element.
   *
   * Returns null for elements inside a shadow root: `document.querySelector`
   * cannot pierce shadow boundaries, so any selector we generated would be a
   * lie. Those elements are reachable by `ref` only.
   */
  function selectorFor(el) {
    if (el.getRootNode() !== document) return null;

    if (el.id) {
      const s = `#${CSS.escape(el.id)}`;
      if (isUnique(s)) return s;
    }

    const tag = el.tagName.toLowerCase();

    for (const attr of STABLE_ATTRS) {
      const v = el.getAttribute(attr);
      if (v) {
        const s = `${tag}[${attr}="${quoteAttr(v)}"]`;
        if (isUnique(s)) return s;
      }
    }

    // Class combination, only if it happens to be unique.
    if (el.classList.length) {
      const classes = [...el.classList].slice(0, 3).map((c) => `.${CSS.escape(c)}`).join("");
      const s = `${tag}${classes}`;
      if (isUnique(s)) return s;
    }

    // Structural path — always works, but breaks on any reordering.
    return structuralPath(el);
  }

  function structuralPath(el) {
    const parts = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE && node !== document.documentElement) {
      const parent = node.parentElement;
      if (!parent) break;
      const index = [...parent.children].indexOf(node) + 1;
      parts.unshift(`${node.tagName.toLowerCase()}:nth-child(${index})`);
      const candidate = parts.join(" > ");
      if (isUnique(candidate)) return candidate;
      node = parent;
    }
    return parts.length ? parts.join(" > ") : null;
  }

  // --- element description --------------------------------------------

  function boxOf(el) {
    const r = el.getBoundingClientRect();
    const vw = window.innerWidth || 1;
    const vh = window.innerHeight || 1;
    const round = (n) => Math.round(n * 10000) / 10000;
    return {
      x: round(r.left / vw), y: round(r.top / vh),
      w: round(r.width / vw), h: round(r.height / vh),
    };
  }

  function accessibleName(el) {
    const aria = el.getAttribute && el.getAttribute("aria-label");
    if (aria) return aria.trim();

    if (el.tagName === "IMG") return (el.getAttribute("alt") || "").trim();
    if (el.tagName === "INPUT") {
      const label = el.labels && el.labels[0] ? el.labels[0].textContent : "";
      return (label || el.getAttribute("placeholder") ||
              el.getAttribute("aria-labelledby") || el.getAttribute("name") || "").trim();
    }
    const text = (el.textContent || "").replace(/\s+/g, " ").trim();
    if (text) return text.slice(0, 200);
    return (el.getAttribute("title") || el.getAttribute("name") || "").trim();
  }

  function roleOf(el) {
    const explicit = el.getAttribute && el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a") return "link";
    if (tag === "input") return `input:${el.type || "text"}`;
    if (tag === "textarea") return "input:textarea";
    if (tag === "select") return "select";
    if (tag === "button" || tag === "summary") return "button";
    if (el.isContentEditable) return "input:contenteditable";
    return tag;
  }

  function stateOf(el) {
    const s = {};
    if (el.disabled) s.disabled = true;
    if (el.checked !== undefined && (el.type === "checkbox" || el.type === "radio")) {
      s.checked = Boolean(el.checked);
    }
    const expanded = el.getAttribute && el.getAttribute("aria-expanded");
    if (expanded !== null && expanded !== undefined) s.expanded = expanded === "true";
    if (el.tagName === "SELECT") {
      s.selected = el.selectedOptions?.[0]?.textContent?.trim() || null;
      s.options = [...el.options].slice(0, 50).map((o) => o.textContent.trim());
    }
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      // The current value matters to an agent deciding whether to fill.
      // Password values are reported as a length, not content.
      s.value = el.type === "password" ? `<${el.value.length} chars>` : el.value;
    }
    return s;
  }

  function isInteractive(el) {
    try {
      return el.matches(INTERACTIVE_SELECTOR);
    } catch {
      return false;
    }
  }

  function shouldIncludeImage(el) {
    const rect = el.getBoundingClientRect();
    if (rect.width < MIN_IMAGE_PX && rect.height < MIN_IMAGE_PX) return false;
    const src = el.currentSrc || el.src || "";
    if (src.startsWith("data:") && src.length > MAX_DATA_URI) return false;
    return Boolean(src);
  }

  // --- snapshot --------------------------------------------------------

  function elideUrl(url) {
    if (url.startsWith("data:")) {
      const comma = url.indexOf(",");
      const prefix = comma > 0 ? url.slice(0, comma) : "data:";
      return `${prefix},<${url.length} bytes>`;
    }
    return url.length > 100 ? `${url.slice(0, 100)}…` : url;
  }

  // Monotonic across snapshots, deliberately NOT reset per snapshot. If refs
  // restarted at e1 each time, a ref held from an earlier snapshot would
  // silently resolve to whatever element now occupies that slot — the same
  // hazard as a stale position, but without the loud failure. Never reusing a
  // ref means a stale one is simply absent from the current map and fails with
  // unknown_ref instead of acting on the wrong element.
  let refCounter = 0;

  function makeRegistrar(elements) {
    const seen = new Map();  // Element -> entry

    return function register(el) {
      const existing = seen.get(el);
      if (existing) return existing;

      refCounter += 1;
      const ref = `e${refCounter}`;
      const entry = {
        ref,
        selector: selectorFor(el),
        role: roleOf(el),
        name: accessibleName(el),
        tag: el.tagName.toLowerCase(),
        box: boxOf(el),
        state: stateOf(el),
      };
      if (el.tagName === "A" && el.href) entry.href = el.href;
      if (el.tagName === "IMG") entry.src = el.currentSrc || el.src || null;

      seen.set(el, entry);
      state.refs.set(ref, el);
      elements.push(entry);
      return entry;
    };
  }

  function* childNodesOf(node) {
    if (node.shadowRoot) yield* node.shadowRoot.childNodes;
    yield* node.childNodes;
  }

  function annotate(el, register) {
    const entry = register(el);
    const bits = [`ref=${entry.ref}`];
    if (entry.selector) bits.push(`selector=${entry.selector}`);
    if (entry.state.disabled) bits.push("disabled");
    if (entry.state.checked !== undefined) bits.push(`checked=${entry.state.checked}`);
    if (entry.state.expanded !== undefined) bits.push(`expanded=${entry.state.expanded}`);
    if (entry.state.value) bits.push(`value=${JSON.stringify(entry.state.value)}`);
    // The markdown is meant to be read; a base64 blob or a 400-char tracking
    // URL drowns it. The untruncated src stays in the elements array.
    if (entry.src) bits.push(`src=${elideUrl(entry.src)}`);

    const label = entry.name || "";
    return `[${entry.role}] ${label} {${bits.join(", ")}}`.replace(/\s+/g, " ").trim();
  }

  /** Flatten an element's inline content, annotating interactive descendants. */
  function inlineOf(el, register) {
    const parts = [];
    for (const node of childNodesOf(el)) {
      if (node.nodeType === Node.TEXT_NODE) {
        const text = node.textContent.replace(/\s+/g, " ");
        if (text.trim()) parts.push(text.trim());
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        if (SKIP_TAGS.has(node.tagName) || !isVisible(node)) continue;
        if (node.tagName === "IMG") {
          if (shouldIncludeImage(node)) parts.push(annotate(node, register));
        } else if (isInteractive(node)) {
          parts.push(annotate(node, register));
        } else {
          const inner = inlineOf(node, register);
          if (inner) parts.push(inner);
        }
      }
    }
    return parts.join(" ").replace(/\s+/g, " ").trim();
  }

  function isBlock(el) {
    if (BLOCK_TAGS.has(el.tagName)) return true;
    const d = getComputedStyle(el).display;
    return d === "block" || d === "flex" || d === "grid" ||
           d === "list-item" || d.startsWith("table");
  }

  function hasBlockChild(el) {
    for (const node of childNodesOf(el)) {
      if (node.nodeType !== Node.ELEMENT_NODE) continue;
      if (SKIP_TAGS.has(node.tagName)) continue;
      if (!isVisible(node)) continue;
      if (isBlock(node)) return true;
    }
    return false;
  }

  function walk(el, lines, register) {
    if (SKIP_TAGS.has(el.tagName) || !isVisible(el)) return;

    if (el.tagName === "IMG") {
      if (shouldIncludeImage(el)) lines.push(annotate(el, register));
      return;
    }

    // An interactive element is a leaf regardless of its internals — a button
    // wrapping spans is one actionable thing, not three.
    if (isInteractive(el)) {
      lines.push(annotate(el, register));
      return;
    }

    const heading = /^H([1-6])$/.exec(el.tagName);
    if (heading && !hasBlockChild(el)) {
      const text = inlineOf(el, register);
      if (text) lines.push(`${"#".repeat(Number(heading[1]))} ${text}`);
      return;
    }

    if (el.tagName === "LI" && !hasBlockChild(el)) {
      const text = inlineOf(el, register);
      if (text) lines.push(`- ${text}`);
      return;
    }

    if (hasBlockChild(el)) {
      for (const node of childNodesOf(el)) {
        if (node.nodeType === Node.ELEMENT_NODE) walk(node, lines, register);
      }
      return;
    }

    const text = inlineOf(el, register);
    if (text) lines.push(text);
  }

  function viewportInfo() {
    return {
      cssWidth: window.innerWidth,
      cssHeight: window.innerHeight,
      devicePixelRatio: window.devicePixelRatio || 1,
      scrollX: Math.round(window.scrollX),
      scrollY: Math.round(window.scrollY),
      documentHeight: document.documentElement.scrollHeight,
    };
  }

  function snapshot(params) {
    const maxChars = Number(params.maxChars) || DEFAULT_MAX_CHARS;

    state.generation += 1;
    state.refs = new Map();
    state.scroll = { x: Math.round(window.scrollX), y: Math.round(window.scrollY) };

    const elements = [];
    const register = makeRegistrar(elements);
    const lines = [];

    if (document.title) lines.push(`# ${document.title}`, "");
    if (document.body) walk(document.body, lines, register);

    // Collapse runs of blank lines the walk may have produced.
    let markdown = lines.filter((l, i) => l !== "" || lines[i - 1] !== "").join("\n");

    let truncated = false;
    if (markdown.length > maxChars) {
      markdown = `${markdown.slice(0, maxChars)}\n\n[... truncated at ${maxChars} chars ...]`;
      truncated = true;
    }

    return {
      generation: state.generation,
      url: location.href,
      title: document.title,
      markdown,
      truncated,
      elements,
      viewport: viewportInfo(),
    };
  }

  // --- target resolution ----------------------------------------------

  function assertViewportFresh(params) {
    if (state.generation === 0) {
      throw new Error(
        "no_snapshot: take a snapshot before using position targeting"
      );
    }
    if (params.generation !== undefined && params.generation !== state.generation) {
      throw new Error(
        `stale_viewport: position refers to snapshot generation ${params.generation}, ` +
        `current is ${state.generation}; re-snapshot first`
      );
    }
    const x = Math.round(window.scrollX);
    const y = Math.round(window.scrollY);
    if (x !== state.scroll.x || y !== state.scroll.y) {
      throw new Error(
        `stale_viewport: page scrolled from (${state.scroll.x},${state.scroll.y}) ` +
        `to (${x},${y}) since the snapshot; re-snapshot before position targeting`
      );
    }
  }

  function resolveTarget(params) {
    if (params.ref) {
      const el = state.refs.get(params.ref);
      if (!el) {
        throw new Error(`unknown_ref: ${params.ref} — take a snapshot first`);
      }
      if (!el.isConnected) {
        throw new Error(`stale_ref: ${params.ref} is no longer in the document`);
      }
      return el;
    }

    if (params.selector) {
      const el = document.querySelector(params.selector);
      if (!el) throw new Error(`no_match: no element matches ${params.selector}`);
      return el;
    }

    if (params.position) {
      assertViewportFresh(params);
      const { x, y } = params.position;
      if (typeof x !== "number" || typeof y !== "number") {
        throw new Error("position requires numeric x and y proportions in [0,1]");
      }
      const cssX = x * window.innerWidth;
      const cssY = y * window.innerHeight;
      const el = document.elementFromPoint(cssX, cssY);
      if (!el) throw new Error(`no_element_at_position: (${x}, ${y})`);
      return el;
    }

    throw new Error("target required: one of ref, selector, or position");
  }

  function describe(el) {
    return {
      tag: el.tagName.toLowerCase(),
      role: roleOf(el),
      name: accessibleName(el),
      selector: selectorFor(el),
    };
  }

  // --- interaction ------------------------------------------------------

  function click(params) {
    const el = resolveTarget(params);
    el.scrollIntoView({ block: "center", inline: "center" });

    if (el.disabled) throw new Error("element_disabled: refusing to click");
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) {
      throw new Error("element_not_visible: zero-size bounding box");
    }

    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const base = {
      bubbles: true, cancelable: true, view: window,
      clientX: cx, clientY: cy, button: 0, buttons: 1,
    };

    // Full pointer sequence — frameworks commonly listen on pointer/mouse
    // events rather than click alone.
    el.dispatchEvent(new PointerEvent("pointerdown", { ...base, pointerId: 1, isPrimary: true }));
    el.dispatchEvent(new MouseEvent("mousedown", base));
    if (typeof el.focus === "function") el.focus();
    el.dispatchEvent(new PointerEvent("pointerup", { ...base, pointerId: 1, isPrimary: true, buttons: 0 }));
    el.dispatchEvent(new MouseEvent("mouseup", { ...base, buttons: 0 }));
    const notPrevented = el.dispatchEvent(new MouseEvent("click", { ...base, buttons: 0 }));

    return { clicked: describe(el), defaultPrevented: !notPrevented };
  }

  /**
   * Assign an input's value through the native setter.
   *
   * React (and Vue) install their own value setter on the element instance and
   * track the last value they wrote. A plain `el.value = x` bypasses that
   * bookkeeping, so the framework never sees the change and the component's
   * state silently stays stale — the classic reason naive form-filling
   * "works" in the DOM but not in the app.
   */
  function setNativeValue(el, value) {
    const proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
  }

  function fire(el, type) {
    el.dispatchEvent(new Event(type, { bubbles: true }));
  }

  function fill(params) {
    const el = resolveTarget(params);
    el.scrollIntoView({ block: "center", inline: "center" });
    if (el.disabled) throw new Error("element_disabled: refusing to fill");
    if (typeof el.focus === "function") el.focus();

    const value = params.value ?? "";

    if (el.tagName === "SELECT") {
      const options = [...el.options];
      const match = options.find((o) => o.value === value) ||
                    options.find((o) => o.textContent.trim() === String(value).trim());
      if (!match) {
        throw new Error(
          `no_option: ${JSON.stringify(value)} not among ` +
          JSON.stringify(options.map((o) => o.textContent.trim()).slice(0, 20))
        );
      }
      el.value = match.value;
      fire(el, "input");
      fire(el, "change");
      return { filled: describe(el), value: match.value };
    }

    if (el.type === "checkbox" || el.type === "radio") {
      const desired = params.checked !== undefined ? Boolean(params.checked) : true;
      if (el.checked !== desired) el.click();  // click keeps framework state in sync
      return { filled: describe(el), checked: el.checked };
    }

    if (el.isContentEditable) {
      el.textContent = value;
      fire(el, "input");
      fire(el, "change");
      return { filled: describe(el), value };
    }

    if (el.tagName !== "INPUT" && el.tagName !== "TEXTAREA") {
      throw new Error(`not_fillable: <${el.tagName.toLowerCase()}> is not a text field`);
    }

    setNativeValue(el, value);
    fire(el, "input");
    fire(el, "change");
    return { filled: describe(el), value: el.value };
  }

  function scroll(params) {
    let result;

    if (params.ref || params.selector) {
      const el = resolveTarget(params);
      el.scrollIntoView({ block: params.block || "center", inline: "nearest" });
      result = { scrolledTo: describe(el) };
    } else if (params.to === "top") {
      window.scrollTo(0, 0);
      result = { scrolledTo: "top" };
    } else if (params.to === "bottom") {
      window.scrollTo(0, document.documentElement.scrollHeight);
      result = { scrolledTo: "bottom" };
    } else if (params.pages !== undefined) {
      window.scrollBy(0, Number(params.pages) * window.innerHeight);
      result = { scrolledBy: `${params.pages} page(s)` };
    } else if (params.by) {
      window.scrollBy(Number(params.by.x) || 0, Number(params.by.y) || 0);
      result = { scrolledBy: params.by };
    } else {
      throw new Error("scroll requires one of: ref, selector, to, pages, by");
    }

    // Scrolling invalidates every proportional coordinate from the last
    // snapshot. Bumping the generation without updating state.scroll makes
    // any subsequent position target fail loudly instead of landing blind.
    state.generation += 1;

    return { ...result, generation: state.generation, viewport: viewportInfo() };
  }

  globalThis.__archieCore = {
    ping: () => ({ pong: true, url: location.href, title: document.title }),
    get_viewport: viewportInfo,
    snapshot,
    click,
    fill,
    scroll,
    // Exposed for tests and debugging only.
    _internals: { selectorFor, isVisible, isInteractive, state },
  };
})();
