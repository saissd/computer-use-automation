"""JavaScript injected into each frame to build the semantic view.

Why compute accessible names ourselves rather than lean entirely on
Playwright's `get_by_role`: we need the *same* name computation for two
different jobs — rendering the observation the model reasons over, and
capturing the descriptor we write into the artifact. If those two disagreed,
the model would act on a control described one way and we would record it
another way, and replay would drift from discovery for no visible reason.

The algorithm is a deliberately simplified ACCNAME: aria-label, then
aria-labelledby, then an associated <label>, then title, then placeholder,
then the value of a button, then text content. That ordering matches what
browsers do closely enough for the controls that matter, and it covers the
legacy patterns (`title=` on an input, label in an adjacent table cell) that
modern-only implementations miss.
"""

# Enumerate interactable controls and compute role/name for each.
SNAPSHOT_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();

  function accName(el) {
    const aria = el.getAttribute('aria-label');
    if (norm(aria)) return norm(aria);

    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const parts = lb.split(/\s+/)
        .map(id => { const t = document.getElementById(id); return t ? t.innerText : ''; })
        .filter(x => norm(x));
      if (parts.length) return norm(parts.join(' '));
    }

    if (el.id) {
      try {
        const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lab && norm(lab.innerText)) return norm(lab.innerText);
      } catch (e) { /* malformed id */ }
    }

    const wrap = el.closest ? el.closest('label') : null;
    if (wrap && norm(wrap.innerText)) return norm(wrap.innerText);

    if (norm(el.getAttribute('title'))) return norm(el.getAttribute('title'));
    if (norm(el.getAttribute('placeholder'))) return norm(el.getAttribute('placeholder'));

    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      if (ty === 'submit' || ty === 'button' || ty === 'reset') {
        return norm(el.getAttribute('value'));
      }
      return '';
    }
    if (tag === 'a' || tag === 'button') return norm(el.innerText);
    return '';
  }

  function roleOf(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'button') return 'button';
    if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      const map = {
        submit: 'button', button: 'button', reset: 'button', image: 'button',
        checkbox: 'checkbox', radio: 'radio', file: 'button',
        number: 'spinbutton', search: 'searchbox', range: 'slider',
        password: 'textbox', text: 'textbox', email: 'textbox',
        tel: 'textbox', url: 'textbox', date: 'textbox'
      };
      return map[ty] || 'textbox';
    }
    return 'generic';
  }

  function visible(el) {
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  // Refs are per-observation. Clear the last observation's so a stale ref can
  // never resolve to an element that is no longer numbered.
  document.querySelectorAll('[data-cua-ref]').forEach(e => e.removeAttribute('data-cua-ref'));

  const SEL = 'a[href], button, input, select, textarea, [role], [onclick]';
  const out = [];
  let i = 0;
  document.querySelectorAll(SEL).forEach(el => {
    const tag = el.tagName.toLowerCase();
    const ty = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && ty === 'hidden') return;
    if (!visible(el)) return;

    const r = el.getBoundingClientRect();
    el.setAttribute('data-cua-ref', String(i));

    // Never read back the value of a password field. Redaction starts at the
    // point of observation, not at the point of logging.
    let value = null;
    if (el.value !== undefined && ty !== 'password') value = String(el.value || '');

    out.push({
      ref: i,
      role: roleOf(el),
      name: accName(el),
      tag: tag,
      value: value,
      enabled: !el.disabled,
      bbox: { x: r.x, y: r.y, w: r.width, h: r.height },
      attrs: {
        id: el.id || '',
        name: el.getAttribute('name') || '',
        type: el.getAttribute('type') || ''
      }
    });
    i++;
  });

  // Read-only values: innermost table cells that hold text and no control.
  // Legacy detail screens put every value worth reading in a bare <td>, so
  // without this `extract` has nothing to address. They share the ref
  // sequence but carry role 'cell', which keeps them out of the control list.
  const rowText = (tr) => [...tr.children]
    .map(c => norm(c.innerText)).filter(Boolean).join(' | ');
  let cells = 0;
  document.querySelectorAll('td, th').forEach(cell => {
    if (cells >= 150) return;
    if (cell.querySelector('td, th, a[href], button, input, select, textarea')) return;
    const text = norm(cell.innerText);
    if (!text || !visible(cell)) return;

    const r = cell.getBoundingClientRect();
    const tr = cell.closest('tr');
    cell.setAttribute('data-cua-ref', String(i));
    out.push({
      ref: i,
      role: 'cell',
      name: '',
      tag: cell.tagName.toLowerCase(),
      value: text.slice(0, 120),
      enabled: true,
      bbox: { x: r.x, y: r.y, w: r.width, h: r.height },
      attrs: { id: cell.id || '', name: '', type: '', row: tr ? rowText(tr).slice(0, 160) : '' }
    });
    i++;
    cells++;
  });

  return {
    elements: out,
    text: document.body ? document.body.innerText : '',
    title: document.title || ''
  };
}
"""


# Mark cells positioned relative to an anchor cell, for the `text_near`
# strategy. This is what makes table-laid-out legacy forms and grids
# addressable when a control has no accessible name of its own.
NEAR_JS = r"""
(args) => {
  const { anchor, direction, offset } = args;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();

  document.querySelectorAll('[data-cua-near]')
          .forEach(e => e.removeAttribute('data-cua-near'));

  // Innermost cells only: an outer <td> wrapping a whole table also "contains"
  // the anchor text, and matching it would target the wrong thing entirely.
  const cells = [...document.querySelectorAll('td, th')].filter(c => {
    if (!norm(c.innerText).includes(anchor)) return false;
    return ![...c.querySelectorAll('td, th')]
      .some(inner => norm(inner.innerText).includes(anchor));
  });

  let count = 0;
  for (const c of cells) {
    const row = c.closest('tr');
    if (!row) continue;
    const kids = [...row.children];
    const idx = kids.indexOf(c);
    let target = null;

    if (direction === 'right')      target = kids[idx + offset];
    else if (direction === 'left')  target = kids[idx - offset];
    else if (direction === 'below') {
      let r = row;
      for (let n = 0; n < offset && r; n++) r = r.nextElementSibling;
      target = r ? r.children[idx] : null;
    } else if (direction === 'above') {
      let r = row;
      for (let n = 0; n < offset && r; n++) r = r.previousElementSibling;
      target = r ? r.children[idx] : null;
    }

    if (target) { target.setAttribute('data-cua-near', '1'); count++; }
  }
  return count;
}
"""


# Restrict a set of already-marked candidates to those inside a table row that
# also contains some other text. Used by Disambiguation.within_row_matching.
ROW_FILTER_JS = r"""
(args) => {
  const { text } = args;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  let kept = 0;
  document.querySelectorAll('[data-cua-near]').forEach(el => {
    const row = el.closest('tr');
    if (!row || !norm(row.innerText).includes(text)) {
      el.removeAttribute('data-cua-near');
    } else {
      kept++;
    }
  });
  return kept;
}
"""


# Given an element previously tagged with data-cua-ref, gather every signal we
# might use to describe it in an artifact. Run at *record* time only: this is
# how a discovery run turns "the thing the model clicked" into a ranked,
# replayable descriptor instead of a coordinate or a brittle selector.
DESCRIBE_JS = r"""
(args) => {
  const { ref } = args;
  const el = document.querySelector('[data-cua-ref="' + ref + '"]');
  if (!el) return null;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();

  // The label in the cell to the left, or the cell above — the two layouts
  // that legacy table-based forms actually use.
  let leftLabel = '', aboveLabel = '';
  let rowAnchor = '', rowAnchorOffset = 0;
  const cell = el.closest ? el.closest('td, th') : null;
  const row = cell ? cell.closest('tr') : null;
  if (row) {
    const kids = [...row.children];
    const idx = kids.indexOf(cell);
    if (idx > 0) leftLabel = norm(kids[idx - 1].innerText);
    const prevRow = row.previousElementSibling;
    if (prevRow && prevRow.children[idx]) {
      aboveLabel = norm(prevRow.children[idx].innerText);
    }

    // For reading a value: the nearest cell to the left that is label-like
    // (no digits — data has digits) and unique on the page (so a text_near
    // anchored on it cannot land in another row). The immediate neighbour is
    // often data itself, e.g. an account status of "Active" on every row.
    const innermostContaining = (t) => [...document.querySelectorAll('td, th')]
      .filter(c => norm(c.innerText).includes(t) &&
        ![...c.querySelectorAll('td, th')].some(x => norm(x.innerText).includes(t)))
      .length;
    for (let j = idx - 1; j >= 0; j--) {
      const t = norm(kids[j].innerText);
      if (!t || /\d/.test(t)) continue;
      if (innermostContaining(t) === 1) { rowAnchor = t; rowAnchorOffset = idx - j; break; }
    }
  }
  const withId = el.querySelector ? el.querySelector('[id]') : null;

  let labelFor = '';
  if (el.id) {
    try {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) labelFor = norm(lab.innerText);
    } catch (e) { /* ignore */ }
  }

  // How many controls share this element's own cell? If more than one, a
  // text_near descriptor pointing at that cell would be ambiguous.
  const siblingsInCell = cell
    ? cell.querySelectorAll('input, select, textarea, button, a').length
    : 0;

  return {
    id: el.id || '',
    nameAttr: el.getAttribute('name') || '',
    tag: el.tagName.toLowerCase(),
    type: (el.getAttribute('type') || '').toLowerCase(),
    title: norm(el.getAttribute('title')),
    placeholder: norm(el.getAttribute('placeholder')),
    ariaLabel: norm(el.getAttribute('aria-label')),
    labelFor: labelFor,
    leftLabel: leftLabel,
    aboveLabel: aboveLabel,
    rowAnchor: rowAnchor,
    rowAnchorOffset: rowAnchorOffset,
    innerId: withId ? withId.id : '',
    text: norm(el.innerText),
    value: el.type === 'password' ? '' : norm(el.value),
    siblingsInCell: siblingsInCell,
    outerHTMLHead: (el.outerHTML || '').slice(0, 160)
  };
}
"""
