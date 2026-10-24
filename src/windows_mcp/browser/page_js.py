"""The in-page JavaScript the Browser tool evaluates (one function, dispatched by op).

Element numbers from ``read``/``search`` index ``window.__wmcp.els`` (1-based), so a later
``click(element=3)`` resolves to the exact node without a selector. A navigation drops the
array, which is reported as ``stale`` (call ``read`` again).
"""

from __future__ import annotations

import json

LIB = r"""
(function (op, a) {
  const W = window.__wmcp || (window.__wmcp = { els: [] });
  const clean = (s, n) => {
    s = (s || '').replace(/\s+/g, ' ').trim();
    return n && s.length > n ? s.slice(0, n - 1) + '…' : s;
  };
  const INTERACTIVE = 'a[href],button,input:not([type=hidden]),textarea,select,summary,' +
    '[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],' +
    '[role=menuitemcheckbox],[role=option],[role=switch],[role=combobox],[role=textbox],' +
    '[role=searchbox],[role=slider],[role=spinbutton],[contenteditable=""],' +
    '[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"])';
  const walk = (root, sel, out) => {
    const tw = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let n = tw.nextNode();
    for (; n; n = tw.nextNode()) {
      if (sel === null || n.matches(sel)) out.push(n);
      if (n.shadowRoot) walk(n.shadowRoot, sel, out);
    }
    return out;
  };
  const visible = (el) => {
    if (!el.isConnected) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    if (el.checkVisibility) return el.checkVisibility({ visibilityProperty: true });
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const role = (el) => {
    const r = el.getAttribute('role');
    if (r) return r.split(' ')[0].toLowerCase();
    const t = el.tagName;
    if (t === 'A') return 'link';
    if (t === 'BUTTON' || t === 'SUMMARY') return 'button';
    if (t === 'SELECT') return 'combobox';
    if (t === 'TEXTAREA') return 'textbox';
    if (t === 'INPUT') {
      const ty = (el.type || 'text').toLowerCase();
      if (['button', 'submit', 'reset', 'image'].includes(ty)) return 'button';
      if (ty === 'checkbox' || ty === 'radio') return ty;
      if (ty === 'range') return 'slider';
      if (ty === 'search') return 'searchbox';
      if (ty === 'number') return 'spinbutton';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return 'clickable';
  };
  const FIELD = ['textbox', 'searchbox', 'combobox', 'spinbutton'];
  const label = (el) => {
    let t = el.getAttribute('aria-label');
    if (!t) {
      const ids = el.getAttribute('aria-labelledby');
      if (ids) t = ids.split(/\s+/).map((i) => {
        const e = document.getElementById(i); return e ? e.innerText : '';
      }).join(' ');
    }
    if (!t && el.labels && el.labels.length) t = el.labels[0].innerText;
    const tag = el.tagName;
    if (!t && (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT')) {
      t = el.placeholder || el.title ||
        (['button', 'submit', 'reset'].includes(el.type) ? el.value : '') || el.name || '';
    }
    if (!t) t = el.innerText || '';
    if (!t) {
      const img = el.querySelector && el.querySelector('img[alt]');
      t = el.title || el.getAttribute('alt') || (img && img.alt) || el.name || '';
    }
    return clean(t, 80);
  };
  const isPw = (el) => el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'password';
  const describe = (el, n) => {
    const d = { n, role: role(el), label: label(el) };
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') {
      if (el.placeholder && clean(el.placeholder, 80) !== d.label) d.placeholder = clean(el.placeholder, 60);
      if (isPw(el)) d.password = true;
      else if (el.value && !['button', 'submit', 'reset', 'checkbox', 'radio'].includes(el.type))
        d.value = clean(el.value, 60);
      if (el.type === 'checkbox' || el.type === 'radio') d.checked = el.checked;
    }
    if (tag === 'SELECT' && el.selectedOptions && el.selectedOptions[0])
      d.value = clean(el.selectedOptions[0].text, 60);
    const ac = el.getAttribute('aria-checked');
    if (ac) d.checked = ac === 'true';
    const ae = el.getAttribute('aria-expanded');
    if (ae) d.expanded = ae === 'true';
    if (tag === 'A' && el.href) d.href = el.href;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') d.disabled = true;
    return d;
  };
  const hasPassword = () => !!document.querySelector('input[type=password]');
  const pageText = (max) => {
    if (!(max > 0) || !document.body) return ['', 0];
    const out = []; let prev = '';
    for (const raw of document.body.innerText.split('\n')) {
      const l = raw.replace(/\s+/g, ' ').trim();
      if (l && l !== prev) out.push(l);
      prev = l;
    }
    const text = out.join('\n');
    return [text.slice(0, max), text.length];
  };
  const reg = (el) => {
    let i = W.els.indexOf(el);
    if (i < 0) { W.els.push(el); i = W.els.length - 1; }
    return i + 1;
  };
  const elAt = (n) => {
    const el = W.els[n - 1];
    return el && el.isConnected ? el : null;
  };
  const score = (el, want) => {
    const l = label(el).toLowerCase();
    const p = (el.placeholder || '').toLowerCase();
    const v = (el.tagName === 'INPUT' && ['button', 'submit'].includes(el.type) ? el.value : '').toLowerCase();
    let best = 0;
    for (const s of [l, p, v]) {
      if (!s) continue;
      if (s === want) best = Math.max(best, 4);
      else if (s.startsWith(want)) best = Math.max(best, 3);
      else if (new RegExp('\\b' + want.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b').test(s)) best = Math.max(best, 2);
      else if (s.includes(want)) best = Math.max(best, 1);
    }
    return best;
  };

  if (op === 'read') {
    const all = walk(document, INTERACTIVE, []).filter(visible);
    let chosen = all;
    if (all.length > a.max) {
      const vh = innerHeight;
      const idx = new Map(all.map((el, i) => [el, i]));
      const near = all.filter((el) => {
        const r = el.getBoundingClientRect(); return r.bottom > -vh * 0.5 && r.top < vh * 2;
      }).slice(0, a.max);
      const set = new Set(near);
      for (const el of all) { if (near.length >= a.max) break; if (!set.has(el)) { near.push(el); set.add(el); } }
      chosen = near.sort((x, y) => idx.get(x) - idx.get(y));
    }
    W.els = chosen.slice();
    const [text, textLength] = pageText(a.maxText);
    return {
      url: location.href, title: document.title, text, textLength, total: all.length,
      elements: chosen.map((el, i) => describe(el, i + 1)), hasPassword: hasPassword(),
    };
  }
  if (op === 'state') {
    return { url: location.href, title: document.title, ready: document.readyState, hasPassword: hasPassword() };
  }
  if (op === 'results') {
    const sels = {
      bing: 'li.b_algo h2 a', google: 'a:has(h3)', duckduckgo: 'a.result__a, a[data-testid=result-title-a]',
    };
    let links = [];
    try { links = Array.from(document.querySelectorAll(sels[a.engine] || 'h2 a, h3 a, a:has(h3)')); } catch (e) {}
    links = links.filter(visible).slice(0, a.max);
    W.els = links.slice();
    return {
      url: location.href, title: document.title,
      results: links.map((el, i) => {
        const box = el.closest('li, .g, article, .result') || el.parentElement;
        const cite = box && box.querySelector('cite');
        const snip = box && box.querySelector('p, .b_caption p, [data-sncf], .VwiC3b, .result__snippet');
        return {
          n: i + 1, title: clean((el.querySelector('h3') || el).innerText, 120), href: el.href,
          site: cite ? clean(cite.innerText, 80) : '', snippet: snip ? clean(snip.innerText, 160) : '',
        };
      }),
    };
  }
  if (op === 'resolve') {
    let el = null; let pool = [];
    if (a.element != null) {
      el = elAt(a.element);
      if (!el) return { error: 'stale', message: 'element ' + a.element + ' is not on this page any more; call read again' };
    } else if (a.selector) {
      try { pool = walk(document, a.selector, []); } catch (e) { return { error: 'bad_selector', message: String(e.message || e) }; }
      const vis = pool.filter(visible);
      if (vis.length) pool = vis;
    } else {
      pool = walk(document, INTERACTIVE, []).filter(visible);
      if (a.kind === 'fill') pool = pool.filter((e) => FIELD.includes(role(e)));
      if (a.role) {
        const want = a.role.toLowerCase();
        const alias = { input: 'textbox', field: 'textbox', edit: 'textbox', textarea: 'textbox', hyperlink: 'link', select: 'combobox' };
        pool = pool.filter((e) => role(e) === (alias[want] || want));
      }
      const want = clean(a.name || a.target || '').toLowerCase();
      if (want) {
        const scored = pool.map((e, i) => [score(e, want), i, e]).filter((x) => x[0] > 0);
        scored.sort((x, y) => y[0] - x[0] || x[1] - y[1]);
        const hits = scored.map((x) => x[2]);
        if (!hits.length && a.kind !== 'fill' && !a.role) {
          // Any visible element whose text contains the words; keep the innermost.
          const txt = walk(document, null, []).filter((e) =>
            !['SCRIPT', 'STYLE', 'NOSCRIPT', 'HTML', 'BODY'].includes(e.tagName) &&
            (e.textContent || '').toLowerCase().includes(want));
          const inner = txt.filter((e) => !txt.some((o) => o !== e && e.contains(o) && visible(o)));
          pool = inner.filter(visible);
          pool.sort((x, y) => (clean(x.innerText).toLowerCase() === want ? 0 : 1) - (clean(y.innerText).toLowerCase() === want ? 0 : 1));
        } else {
          pool = hits;
        }
      }
    }
    if (!el) el = pool[(a.nth || 1) - 1];
    if (!el) {
      const near = walk(document, INTERACTIVE, []).filter(visible).slice(0, 400)
        .filter((e) => a.kind !== 'fill' || FIELD.includes(role(e)));
      return {
        error: 'not_found', url: location.href,
        message: 'no matching element' + (pool.length ? ' (only ' + pool.length + ' matches)' : ''),
        candidates: near.slice(0, 8).map((e) => role(e) + ' "' + label(e) + '"'),
      };
    }
    const n = reg(el);
    el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2; const y = r.top + r.height / 2;
    let top = document.elementFromPoint(x, y);
    while (top && top.shadowRoot) {
      const inner = top.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === top) break;
      top = inner;
    }
    const lbl = el.labels && el.labels.length ? el.labels[0] : null;
    const hit = !!top && (top === el || el.contains(top) || (lbl && (lbl === top || lbl.contains(top))));
    return {
      n, x, y, hit, desc: describe(el, n), tag: el.tagName, url: location.href, title: document.title,
      password: isPw(el), hasPassword: hasPassword(), field: FIELD.includes(role(el)),
      select: el.tagName === 'SELECT', check: el.tagName === 'INPUT' && ['checkbox', 'radio'].includes(el.type),
    };
  }
  if (op === 'jsclick') {
    const el = elAt(a.n); if (!el) return { error: 'stale' };
    el.click(); return { ok: true };
  }
  if (op === 'focus') {
    const el = elAt(a.n); if (!el) return { error: 'stale' };
    el.focus({ preventScroll: true });
    const tag = el.tagName;
    if (a.clear) {
      if ((tag === 'INPUT' || tag === 'TEXTAREA') && el.select) el.select();
      else if (el.isContentEditable) {
        const rng = document.createRange(); rng.selectNodeContents(el);
        const s = getSelection(); s.removeAllRanges(); s.addRange(rng);
      }
    } else if ((tag === 'INPUT' || tag === 'TEXTAREA') && typeof el.value === 'string') {
      try { el.setSelectionRange(el.value.length, el.value.length); } catch (e) {}
    }
    const act = document.activeElement;
    return { focused: act === el || el.contains(act) || (el.shadowRoot && el.shadowRoot.activeElement) ? true : false };
  }
  if (op === 'select') {
    const el = elAt(a.n); if (!el) return { error: 'stale' };
    const want = clean(a.value).toLowerCase();
    const opts = Array.from(el.options);
    const o = opts.find((x) => clean(x.text).toLowerCase() === want || x.value.toLowerCase() === want) ||
      opts.find((x) => clean(x.text).toLowerCase().includes(want));
    if (!o) return { error: 'no_option', options: opts.slice(0, 20).map((x) => clean(x.text, 40)) };
    el.value = o.value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return { value: clean(o.text, 60) };
  }
  if (op === 'value') {
    const el = elAt(a.n); if (!el) return { value: null };
    if (isPw(el)) return { value: '(password)' };
    const v = 'value' in el && typeof el.value === 'string' ? el.value : el.innerText;
    return { value: clean(v, 200) };
  }
  if (op === 'quiet') {
    // Resolve once the DOM stops changing for a.quiet ms (client-side rendering after
    // DOMContentLoaded), or after a.max ms.
    return new Promise((res) => {
      const t0 = performance.now(); let last = t0;
      const obs = new MutationObserver(() => { last = performance.now(); });
      obs.observe(document, { subtree: true, childList: true });  // Nodes added = still rendering.
      const tick = () => {
        const now = performance.now();
        if (now - last >= a.quiet || now - t0 >= a.max) {
          obs.disconnect(); return res({ ms: Math.round(now - t0) });
        }
        setTimeout(tick, 15);
      };
      setTimeout(tick, 15);
    });
  }
  if (op === 'wait') {
    const want = a.text ? a.text.toLowerCase() : null;
    const check = () => {
      if (a.url && !location.href.includes(a.url)) return false;
      if (a.selector) {
        let found = null;
        try { found = walk(document, a.selector, []).find(visible); } catch (e) { return false; }
        if (a.gone ? found : !found) return false;
      }
      if (want) {
        const has = !!document.body && document.body.innerText.toLowerCase().includes(want);
        if (a.gone ? has : !has) return false;
      }
      if (a.load && document.readyState !== 'complete') return false;
      return true;
    };
    return new Promise((res) => {
      const t0 = performance.now();
      const tick = () => {
        let ok = false; try { ok = check(); } catch (e) {}
        if (ok) return res({ ok: true, url: location.href, title: document.title });
        if (performance.now() - t0 >= a.timeout) return res({ ok: false, url: location.href, title: document.title });
        setTimeout(tick, 40);
      };
      tick();
    });
  }
  return { error: 'unknown op ' + op };
})
"""


def call(op: str, args: dict | None = None) -> str:
    """The expression that runs *op* with *args* in the page."""
    return f"({LIB})({json.dumps(op)}, {json.dumps(args or {}, ensure_ascii=False)})"
