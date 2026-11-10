/**
 * APEX Auto-Fill (Chrome MV3 extension) — honeypot detection and the DOM walk
 * that pierces shadow roots and same-origin frames.
 *
 * EXCERPT — original files: content/lib/dom.js (`visible`, `offscreen`,
 * `frameDocument`, `deepQueryAll`) and content/scan.js (`HONEYPOT_LABEL_RE`,
 * `clippedAway`, `labelVisible`, `honeypotReason`), plus the guard in
 * content/apply.js that refuses to write a trapped field.
 * Removed: the IIFE/namespace wrappers, the layout-availability memo used for
 * jsdom tests (here `hasLayout` is assumed true), the open-dialog exception for
 * aria-hidden wrappers, the full `effectiveType` helper (reduced to the type
 * attribute), and incident notes naming specific employer tenants.
 *
 * Why it matters: a bot trap is a real, required-looking input the page hides
 * from people. Writing into it identifies the filler as a script and can burn
 * the application. The same "hidden input" shape is ALSO how ATSes build
 * custom dropdowns (a transparent <input role=combobox> over styled markup),
 * so the rule has to separate the two: hidden AND nothing visible labels it.
 */
'use strict';

/**
 * Label copy that says a control is a bot trap. "robot"/"bot" only count as
 * "for robots" / "robots only" / "if you're a bot": the applicant applies for
 * AI roles, and "experience with chatbots" is a question, not a trap.
 */
const HONEYPOT_LABEL_RE =
  /\b(?:for|by) (?:robots?|bots?)\b|\b(?:robots?|bots?) only\b|\bhoneypot\b|leave (?:this |it |this field )?(?:blank|empty)|do not (?:fill|complete)\b|do not enter (?:this|anything|it|if)\b|don'?t (?:fill|complete)\b|if you(?:'re| are) (?:a )?(?:human|robot|bot)\b|not for humans?\b/i;

/** Smaller than this in both directions and a box cannot be seen. A live trap
 * measured 1px x 0.01px; a real control is never under 2px. */
const MIN_VISIBLE_PX = 2;

/** Whether an element is rendered and therefore a candidate for filling. */
function visible(el) {
  if (!el || !el.isConnected) return false;
  const view = el.ownerDocument.defaultView;
  const style = view && view.getComputedStyle ? view.getComputedStyle(el) : null;
  if (style) {
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (style.opacity === '0') return false;
  }
  const rect = el.getBoundingClientRect();
  if (rect.width > 0 || rect.height > 0) return true;
  // A radio hidden behind a styled label has a zero box but a visible label.
  return el.tagName === 'INPUT' && (el.type === 'radio' || el.type === 'checkbox');
}

/**
 * Whether an element has been parked outside the page (the classic
 * `left: -9999px` trap). Measured against the DOCUMENT, not the viewport:
 * a field the lane has scrolled past sits at negative viewport y, and an
 * earlier viewport-relative version skipped a whole form's identity fields
 * as "traps". Scrolled past is not parked.
 */
function offscreen(el, rect) {
  const view = el.ownerDocument.defaultView || globalThis;
  const width = (view && view.innerWidth) || 0;
  const scrollX = (view && (view.scrollX || view.pageXOffset)) || 0;
  const scrollY = (view && (view.scrollY || view.pageYOffset)) || 0;
  if (rect.right + scrollX <= 0 || rect.bottom + scrollY <= 0) return true;
  if (width && rect.left >= width) return true;
  return false;
}

/** Whether `clip` / `clip-path` leave nothing of the element to see. */
function clippedAway(style) {
  if (!style) return false;
  const clip = String(style.clip || '');
  const rect = clip.match(/rect\(\s*([\d.]+)px[ ,]+([\d.]+)px[ ,]+([\d.]+)px[ ,]+([\d.]+)px\s*\)/i);
  if (rect) {
    const [top, right, bottom, left] = rect.slice(1, 5).map(Number);
    if (bottom - top < MIN_VISIBLE_PX && right - left < MIN_VISIBLE_PX) return true;
  }
  const path = String(style.clipPath || style.getPropertyValue('clip-path') || '');
  if (/^inset\(\s*(50|100)%/i.test(path)) return true;
  const polygon = path.match(/^polygon\((.*)\)$/i);
  if (polygon) {
    const points = polygon[1].split(',').map((point) => point.trim());
    if (points.length && points.every((point) => point === points[0])) return true;
  }
  return false;
}

/** Whether a control has a <label> / aria-labelledby target a person can see. */
function labelVisible(el) {
  const doc = el.ownerDocument;
  const nodes = [];
  if (el.labels) Array.prototype.push.apply(nodes, el.labels);
  const wrap = el.closest && el.closest('label');
  if (wrap) nodes.push(wrap);
  const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
  ids.forEach((id) => { const node = doc.getElementById(id); if (node) nodes.push(node); });
  return nodes.some((node) => {
    if (!visible(node)) return false;
    const rect = node.getBoundingClientRect();
    return (rect.width >= MIN_VISIBLE_PX || rect.height >= MIN_VISIBLE_PX)
      && !offscreen(node, rect);
  });
}

/**
 * Why a control is a honeypot, or '' when it is a field to fill.
 *
 * Two families. A label that says the control is for robots is a trap
 * whatever it looks like. A control hidden from a person — aria-hidden, not
 * displayed, parked off-screen, clipped to nothing, too small to see, or taken
 * out of the tab order — is a trap when nothing visible labels it; with a
 * visible label it is the ordinary styled-input pattern and stays a field.
 *
 * @param {Element} el The control.
 * @param {{label: string, raw: string}} resolved Its resolved label text.
 * @returns {string} The reason it must never be written, or ''.
 */
function honeypotReason(el, resolved) {
  const copy = (resolved && (resolved.raw || resolved.label)) || '';
  if (HONEYPOT_LABEL_RE.test(copy)) return 'the label says it is for robots';
  if ((el.getAttribute('type') || '').toLowerCase() === 'file') return '';

  const hidden = [];
  if (el.closest && el.closest('[aria-hidden="true"]')) hidden.push('aria-hidden');
  const view = el.ownerDocument.defaultView;
  const style = view && view.getComputedStyle ? view.getComputedStyle(el) : null;
  if (style && style.display === 'none') hidden.push('display:none');
  if (style && style.visibility === 'hidden') hidden.push('visibility:hidden');
  if (clippedAway(style)) hidden.push('clipped to nothing');
  const rect = el.getBoundingClientRect();
  if (rect.width < MIN_VISIBLE_PX && rect.height < MIN_VISIBLE_PX) {
    hidden.push(`${Math.round(rect.width)}x${Math.round(rect.height)}px box`);
  } else if (offscreen(el, rect)) {
    hidden.push('parked off-screen');
  }
  if (el.getAttribute('tabindex') === '-1') hidden.push('tabindex=-1');
  if (!hidden.length || labelVisible(el)) return '';
  return `${hidden.join(', ')} with no visible label`;
}

/**
 * The document inside a frame, when this script may read it. A frame the user
 * cannot see is the honeypot shape one boundary out, so `visible` is applied
 * to the frame ELEMENT first. Cross-origin frames (reCAPTCHA, map embeds)
 * throw or return null; both mean "not ours to read".
 */
function frameDocument(el) {
  if (!visible(el)) return null;
  try {
    const doc = el.contentDocument;
    return doc && doc.body ? doc : null;
  } catch (err) {
    return null;
  }
}

/**
 * Every match for a selector, piercing open shadow roots and same-origin
 * frames. `querySelectorAll` stops at both boundaries: one ATS application
 * page measured 0 inputs in the light DOM and all of its fields inside
 * hundreds of shadow roots, and another serves its whole form in a
 * same-origin iframe. Closed shadow roots and cross-origin frames are simply
 * invisible, so this degrades rather than throws.
 *
 * @param {Document|Element|ShadowRoot} root Subtree to search.
 * @param {string} selector A CSS selector.
 * @param {Set} [seen] Roots already walked (a frame can point back at an ancestor).
 * @returns {Element[]} Outer tree first, then each shadow root and frame.
 */
function deepQueryAll(root, selector, seen) {
  if (!root || typeof root.querySelectorAll !== 'function') return [];
  const visited = seen || new Set();
  if (visited.has(root)) return [];
  visited.add(root);
  const found = Array.from(root.querySelectorAll(selector));
  root.querySelectorAll('*').forEach((el) => {
    if (el.shadowRoot) found.push(...deepQueryAll(el.shadowRoot, selector, visited));
  });
  root.querySelectorAll('iframe, frame').forEach((el) => {
    const doc = frameDocument(el);
    if (doc) found.push(...deepQueryAll(doc, selector, visited));
  });
  return found;
}

/*
 * From content/apply.js — the write loop re-checks every instruction, because
 * a stale server, a cached plan or a hand-built one can still say `set`, and
 * the write is the thing that marks the application as a bot's. The scanner's
 * own reason wins over the plan:
 *
 *   const trap = (entry && entry.honeypot) || (field && field.honeypot_reason)
 *     || (field && field.honeypot ? 'marked by the scanner' : '');
 *   if (trap) {
 *     rows.push(toRow(instruction,
 *       { status: 'skipped', note: `honeypot — ${trap}; never written` }, field));
 *     continue;
 *   }
 */

if (typeof module !== 'undefined') {
  module.exports = { HONEYPOT_LABEL_RE, honeypotReason, offscreen, clippedAway, deepQueryAll };
}
