/* DecisionTree — Context Graph.
 *
 * The force layout in layout() is ported from the design's graph() and kept
 * deliberately faithful: the same virtual root/cluster hierarchy, the same
 * spring weights and ideal lengths, the same 460 cooling iterations. Change
 * those numbers and it stops looking like the design.
 *
 * Fields the vault does not carry yet (cluster, source, author, ref) are
 * hidden rather than faked — a filter over data that is always null is worse
 * than no filter.
 */

const PALETTE = ['#9E6046', '#3F7A5C', '#5A6FA8', '#90588C', '#7A7233', '#2C7C88'];
const UNCLUSTERED = 'Unclustered';

const STATUS = {
  active:     { label: 'Active',     color: '#3F7A5C' },
  superseded: { label: 'Superseded', color: '#A9A198' },
  retired:    { label: 'Retired',    color: '#B5ADA3' },
};

const SOURCE_LABEL = { chat: 'CHAT', code: 'CODE', pr: 'PR', doc: 'DOC' };

const NW = 214, NH = 96;

const state = {
  projects: [], project: null, decisions: [], edges: [],
  clusters: [], sources: [],
  query: '', statusOff: {}, sourceOff: {}, clusterOff: {},
  selected: null, panelOpen: false,
  moved: {},          // id -> {x, y}: positions the user dragged, in world space
  tx: 40, ty: 20, scale: 0.82,
  loading: true, error: null, user: null,
};

/* ---------------------------------------------------------------- data --- */

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

async function loadProjects() {
  const data = await getJSON('/api/projects');
  // Empty vaults are real projects but nothing to look at; keep them out of
  // the rail rather than filling it with zeroes.
  state.projects = (data.projects || []).filter(p => p.decisions > 0);
}

async function loadProject(name) {
  state.loading = true; render();
  const data = await getJSON(`/api/projects/${encodeURIComponent(name)}/decisions`);
  state.project = name;
  state.decisions = data.decisions || [];
  state.edges = data.edges || [];
  state.clusters = data.clusters || [];
  state.sources = data.sources || [];
  state.selected = null; state.panelOpen = false;
  state.statusOff = {}; state.sourceOff = {}; state.clusterOff = {};
  state.moved = loadMoved(name);
  state.loading = false;
  layoutCache.key = null;
  render();
  queueFit();
}

/* ---------------------------------------------------------- arrangement --- */

/* A dragged arrangement is the user's own work — losing it on a filter change
 * or a page reload would make dragging pointless, so it is kept per project. */

const movedKey = name => `decisiontree:moved:${name}`;

function loadMoved(name) {
  try {
    return JSON.parse(localStorage.getItem(movedKey(name)) || '{}');
  } catch {
    return {};
  }
}

function saveMoved() {
  if (!state.project) return;
  try {
    if (Object.keys(state.moved).length) {
      localStorage.setItem(movedKey(state.project), JSON.stringify(state.moved));
    } else {
      localStorage.removeItem(movedKey(state.project));
    }
  } catch {
    /* storage disabled or full — the arrangement just won't outlive the tab */
  }
}

/** Where a node actually sits: the dragged position if there is one, else the
 *  position the force layout computed. */
const nodePos = (id, pos) => state.moved[id] || pos['d:' + id];

/* -------------------------------------------------------------- helpers --- */

const clusterOf = d => d.cluster || UNCLUSTERED;

function clusterColours() {
  const names = [...new Set(state.decisions.map(clusterOf))].sort();
  const map = {};
  names.forEach((name, i) => { map[name] = PALETTE[i % PALETTE.length]; });
  return map;
}

function shortDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? iso.slice(0, 10)
    : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
}

function visible() {
  const q = state.query.trim().toLowerCase();
  return state.decisions.filter(d => {
    if (state.statusOff[d.status]) return false;
    if (d.source && state.sourceOff[d.source]) return false;
    if (state.clusterOff[clusterOf(d)]) return false;
    if (!q) return true;
    return [d.summary, d.reasoning, d.excerpt, d.ref, d.author, d.cluster]
      .filter(Boolean).some(v => v.toLowerCase().includes(q));
  });
}

/* --------------------------------------------------------------- layout --- */

const layoutCache = { key: null, value: null };

function layout(list) {
  const key = state.project + '|' + list.map(d => d.id).join(',');
  if (layoutCache.key === key) return layoutCache.value;

  const clusters = [...new Set(list.map(clusterOf))].sort();
  const ids = ['__root'].concat(clusters.map(c => 'cl:' + c), list.map(d => 'd:' + d.id));
  const idx = {}; ids.forEach((id, i) => { idx[id] = i; });

  const links = [];
  clusters.forEach(c => links.push([idx['__root'], idx['cl:' + c], 1.3, 430]));
  list.forEach(d => links.push([idx['cl:' + clusterOf(d)], idx['d:' + d.id], 4.0, 50]));

  const present = new Set(list.map(d => d.id));
  list.forEach(d => {
    if (d.derives_from && present.has(d.derives_from)) {
      links.push([idx['d:' + d.derives_from], idx['d:' + d.id], 1.5, 58, 'derives']);
    }
  });
  state.edges.filter(e => e.kind === 'supersedes').forEach(e => {
    if (present.has(e.from) && present.has(e.to)) {
      links.push([idx['d:' + e.to], idx['d:' + e.from], 0.4, 120, 'supersedes']);
    }
  });

  const n = ids.length, K = 44;
  const px = new Float64Array(n), py = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const a = i * 2.399963, r = 40 + 70 * Math.sqrt(i);
    px[i] = Math.cos(a) * r; py[i] = Math.sin(a) * r;
  }
  px[0] = 0; py[0] = 0;

  const dx = new Float64Array(n), dy = new Float64Array(n);
  for (let it = 0; it < 460; it++) {
    const t = 70 * (1 - it / 460) + 1.5;
    dx.fill(0); dy.fill(0);
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      let ax = px[i] - px[j], ay = py[i] - py[j];
      let d2 = ax * ax + ay * ay;
      if (d2 < 1) { d2 = 1; ax = (i % 3) - 1 || 0.7; ay = (j % 3) - 1 || 0.5; }
      if (d2 > 25600) continue;
      const dist = Math.sqrt(d2), f = (K * K) / dist / dist;
      dx[i] += ax * f; dy[i] += ay * f; dx[j] -= ax * f; dy[j] -= ay * f;
    }
    links.forEach(l => {
      const i = l[0], j = l[1], w = l[2], ideal = l[3];
      const ax = px[j] - px[i], ay = py[j] - py[i];
      const dist = Math.max(1, Math.sqrt(ax * ax + ay * ay));
      const f = ((dist - ideal) / dist) * 0.12 * w;
      dx[i] += ax * f; dy[i] += ay * f; dx[j] -= ax * f; dy[j] -= ay * f;
    });
    for (let i = 0; i < n; i++) {
      dx[i] -= px[i] * 0.006; dy[i] -= py[i] * 0.006;
      const m = Math.sqrt(dx[i] * dx[i] + dy[i] * dy[i]) || 1;
      const s = Math.min(m, t) / m;
      px[i] += dx[i] * s; py[i] += dy[i] * s;
    }
    px[0] *= 0.9; py[0] *= 0.9;
  }

  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (let i = 0; i < n; i++) {
    if (px[i] < minx) minx = px[i];
    if (py[i] < miny) miny = py[i];
    if (px[i] > maxx) maxx = px[i];
    if (py[i] > maxy) maxy = py[i];
  }
  const sc = Math.min(1250 / Math.max(1, maxx - minx), 860 / Math.max(1, maxy - miny));
  const pos = {};
  ids.forEach((id, i) => { pos[id] = { x: (px[i] - minx) * sc + 170, y: (py[i] - miny) * sc + 130 }; });

  layoutCache.key = key;
  layoutCache.value = {
    pos, clusters,
    links: links.map(l => ({ a: ids[l[0]], b: ids[l[1]], kind: l[4] })),
  };
  return layoutCache.value;
}

function bbox(list, pos, clusters = []) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const cover = (x, y, w, h) => {
    x0 = Math.min(x0, x); y0 = Math.min(y0, y);
    x1 = Math.max(x1, x + w); y1 = Math.max(y1, y + h);
  };
  list.forEach(d => {
    const p = nodePos(d.id, pos); if (!p) return;
    cover(p.x, p.y, NW, NH);
  });
  // Cluster labels are drawn 120px wide, centred on the cluster's own node,
  // and can sit outside every card — leave them out and they get clipped.
  clusters.forEach(name => {
    const p = pos['cl:' + name]; if (!p) return;
    cover(p.x - 60, p.y - 10, 120, 20);
  });
  if (x0 === Infinity) return { x0: 0, y0: 0, x1: 1, y1: 1 };
  return { x0, y0, x1, y1 };
}

function fit() {
  const canvas = document.getElementById('canvas');
  const list = visible();
  if (!canvas || !list.length) return;
  const { pos } = layout(list);
  const b = bbox(list, pos);
  const rect = canvas.getBoundingClientRect(), pad = 40;
  const panel = state.panelOpen && state.selected ? 414 : 0;
  const availW = Math.max(180, rect.width - panel - pad * 2);
  const availH = Math.max(180, rect.height - pad * 2);
  const bw = Math.max(1, b.x1 - b.x0), bh = Math.max(1, b.y1 - b.y0);
  const s = Math.max(0.2, Math.min(1.1, Math.min(availW / bw, availH / bh)));
  const cw = bw * s, ch = bh * s;
  state.scale = s;
  state.tx = cw > availW ? pad - b.x0 * s : pad + (availW - cw) / 2 - b.x0 * s;
  state.ty = ch > availH ? pad - b.y0 * s : pad + (availH - ch) / 2 - b.y0 * s;
  applyTransform(true);
}

function queueFit(tries = 0) {
  requestAnimationFrame(() => {
    const canvas = document.getElementById('canvas');
    if (!canvas || canvas.getBoundingClientRect().width < 40) {
      if (tries < 20) queueFit(tries + 1);
      return;
    }
    fit();
  });
}

function applyTransform(animate = false) {
  const world = document.getElementById('world');
  if (!world) return;
  // Eased only for button and fit moves. Wheel and drag must track the input
  // exactly; a transition there feels like lag, not smoothness.
  world.style.transition = animate ? 'transform .18s ease' : 'none';
  world.style.transform =
    `translate(${state.tx}px, ${state.ty}px) scale(${state.scale})`;
}

/* --------------------------------------------------------------- render --- */

const el = (tag, props = {}, children = []) => {
  const node = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (v === undefined || v === null) return;
    if (k === 'style') node.style.cssText = v;
    else if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2).toLowerCase(), v);
    else node.setAttribute(k, v);
  });
  (Array.isArray(children) ? children : [children]).forEach(c => {
    if (c === null || c === undefined || c === false) return;
    node.appendChild(typeof c === 'string' || typeof c === 'number'
      ? document.createTextNode(String(c)) : c);
  });
  return node;
};

function counted(list, pick) {
  const counts = {};
  list.forEach(d => { const k = pick(d); if (k) counts[k] = (counts[k] || 0) + 1; });
  return counts;
}

function renderRail(colours) {
  const shown = visible();
  const statusCounts = counted(state.decisions, d => d.status);
  const sourceCounts = counted(state.decisions, d => d.source);
  const clusterCounts = counted(state.decisions, clusterOf);

  const head = el('div', { class: 'rail-head' }, [
    el('div', { class: 'brand' }, 'DecisionTree'),
    el('div', { class: 'kicker' }, 'Context ledger'),
  ]);

  const search = el('div', { style: 'padding:14px 16px 12px;border-bottom:1px solid #E2DACD' }, [
    el('input', {
      class: 'search', placeholder: 'Search decisions…', value: state.query,
      onInput: e => { state.query = e.target.value; layoutCache.key = null; render(); },
    }),
  ]);

  const projects = el('div', {
    style: 'padding:6px 10px 14px;display:flex;flex-direction:column;gap:2px;border-bottom:1px solid #E2DACD',
  }, state.projects.map(p => {
    const on = p.name === state.project;
    return el('div', {
      class: 'proj',
      style: `background:${on ? '#F1E9DC' : 'transparent'};border-left-color:${on ? '#A85C3A' : 'transparent'}`,
      onClick: () => loadProject(p.name),
    }, [
      el('span', { class: 'proj-name', style: `color:${on ? '#1C1917' : '#4A443D'}` }, p.name),
      el('span', { class: 'proj-count' }, String(p.active)),
    ]);
  }));

  const body = el('div', { class: 'om-scroll', style: 'flex:1;overflow-y:auto;padding:16px' }, []);

  body.appendChild(el('div', { class: 'sec', style: 'margin-bottom:9px' }, 'Status'));
  body.appendChild(el('div', { style: 'display:flex;flex-wrap:wrap;gap:5px;margin-bottom:20px' },
    Object.keys(STATUS).filter(k => statusCounts[k]).map(k => {
      const off = state.statusOff[k];
      return el('div', {
        class: 'chip',
        style: `border:1px solid ${off ? '#E7E0D4' : '#DED5C6'};background:${off ? 'transparent' : '#FFFDF9'};color:${off ? '#B5ADA3' : '#4A443D'}`,
        onClick: () => { state.statusOff[k] = !off; layoutCache.key = null; render(); },
      }, [
        el('span', { class: 'dot', style: `background:${STATUS[k].color}` }),
        STATUS[k].label,
        el('span', { style: 'color:#A9A198;font-size:9px' }, String(statusCounts[k])),
      ]);
    })));

  // Surface and cluster filters only exist once the data carries them.
  if (state.sources.length) {
    body.appendChild(el('div', { class: 'sec', style: 'margin-bottom:9px' }, 'Surface'));
    body.appendChild(el('div', { style: 'display:flex;flex-wrap:wrap;gap:5px;margin-bottom:20px' },
      state.sources.map(s => {
        const off = state.sourceOff[s];
        return el('div', {
          class: 'chip',
          style: `border:1px solid ${off ? '#E7E0D4' : '#DED5C6'};background:${off ? 'transparent' : '#FFFDF9'};color:${off ? '#B5ADA3' : '#4A443D'};letter-spacing:.1em`,
          onClick: () => { state.sourceOff[s] = !off; layoutCache.key = null; render(); },
        }, [SOURCE_LABEL[s] || s.toUpperCase(),
            el('span', { style: 'color:#A9A198;font-size:9px' }, String(sourceCounts[s] || 0))]);
      })));
  }

  const clusterNames = Object.keys(clusterCounts).sort();
  if (clusterNames.length && !(clusterNames.length === 1 && clusterNames[0] === UNCLUSTERED)) {
    body.appendChild(el('div', { class: 'sec', style: 'margin-bottom:9px' }, 'Clusters'));
    body.appendChild(el('div', { style: 'display:flex;flex-direction:column;gap:1px' },
      clusterNames.map(name => {
        const off = state.clusterOff[name];
        return el('div', {
          class: 'cluster-row', style: `opacity:${off ? 0.45 : 1}`,
          onClick: () => { state.clusterOff[name] = !off; layoutCache.key = null; render(); },
        }, [
          el('span', { class: 'swatch', style: `background:${colours[name]}` }),
          el('span', { style: 'flex:1;font-size:11px;color:#4A443D' }, name),
          el('span', { style: 'font-size:9.5px;color:#A9A198' }, String(clusterCounts[name])),
        ]);
      })));
  }

  const project = state.projects.find(p => p.name === state.project);
  const foot = el('div', {
    style: 'padding:11px 16px 9px;border-top:1px solid #E2DACD;font-size:9.5px;color:#9A9288;line-height:1.6',
  }, [
    `${shown.length} of ${state.decisions.length} shown`,
    el('br'),
    el('span', { style: 'color:#B5ADA3' },
      project && project.last_activity ? `Last logged ${shortDate(project.last_activity)}` : ''),
  ]);

  const account = el('div', {
    style: 'padding:10px 16px 14px;border-top:1px solid #E2DACD;display:flex;align-items:center;gap:8px',
  }, [
    el('span', { style: 'flex:1;font-size:10px;color:#9A9288;overflow:hidden;text-overflow:ellipsis' },
      state.user ? state.user.email || '' : ''),
    el('a', { class: 'link', href: '/logout', style: 'font-size:10px' }, 'Sign out'),
  ]);

  return el('aside', {}, [head, search,
    el('div', { class: 'sec', style: 'padding:16px 16px 4px' }, 'Projects'),
    projects, body, foot, account]);
}

/* Live references into the rendered canvas so a drag can move one node and its
 * edges without re-rendering the whole graph on every mouse move. */
const refs = { nodes: {}, lines: [] };

function endpoint(id, pos) {
  if (id.startsWith('d:')) {
    const p = nodePos(Number(id.slice(2)), pos);
    return p && { x: p.x + NW / 2, y: p.y + NH / 2 };
  }
  return pos[id];
}

function placeLine(entry, pos) {
  const a = endpoint(entry.a, pos), b = endpoint(entry.b, pos);
  if (!a || !b) return false;
  entry.el.setAttribute('x1', a.x); entry.el.setAttribute('y1', a.y);
  entry.el.setAttribute('x2', b.x); entry.el.setAttribute('y2', b.y);
  return true;
}

function renderCanvas(colours) {
  const list = visible();
  const canvas = el('div', {
    id: 'canvas',
    style: 'position:relative;overflow:hidden;cursor:grab;background:#F4F0E8',
  });

  if (state.loading) {
    canvas.appendChild(el('div', {
      style: 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:11px;color:#A0988E',
    }, 'Loading…'));
    return canvas;
  }
  if (state.error) {
    canvas.appendChild(el('div', {
      style: 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:11px;color:#A85C3A',
    }, state.error));
    return canvas;
  }
  if (!list.length) {
    canvas.appendChild(el('div', {
      style: 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:11px;color:#A0988E',
    }, state.decisions.length ? 'Nothing matches those filters.' : 'No decisions in this project yet.'));
    return canvas;
  }

  const { pos, links } = layout(list);
  const world = el('div', {
    id: 'world',
    style: 'position:absolute;left:0;top:0;transform-origin:0 0;will-change:transform',
  });

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('style', 'position:absolute;left:0;top:0;overflow:visible;pointer-events:none');
  svg.setAttribute('width', '1');
  svg.setAttribute('height', '1');
  refs.lines = [];
  links.forEach(l => {
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('stroke', l.kind === 'supersedes' ? '#C9B79E' : l.kind ? '#CBBFAC' : '#E2DACD');
    line.setAttribute('stroke-width', l.kind === 'derives' ? 1.2 : 1);
    if (l.kind === 'supersedes') line.setAttribute('stroke-dasharray', '3 3');
    const entry = { el: line, a: l.a, b: l.b };
    if (!placeLine(entry, pos)) return;
    refs.lines.push(entry);
    svg.appendChild(line);
  });
  world.appendChild(svg);

  // Cluster labels sit on their virtual node, behind the cards.
  layout(list).clusters.forEach(name => {
    const p = pos['cl:' + name];
    if (!p || name === UNCLUSTERED) return;
    world.appendChild(el('div', {
      style: `position:absolute;left:${p.x - 60}px;top:${p.y - 10}px;width:120px;text-align:center;` +
             `font-size:9px;letter-spacing:.16em;text-transform:uppercase;color:${colours[name]};opacity:.75;pointer-events:none`,
    }, name));
  });

  refs.nodes = {};
  list.forEach(d => {
    const p = nodePos(d.id, pos);
    if (!p) return;
    const chosen = state.selected && state.selected.id === d.id;
    const dim = d.status === 'superseded';
    const colour = colours[clusterOf(d)];
    const card = el('div', {
      class: 'node',
      style: `left:${p.x}px;top:${p.y}px;border:1px solid ${chosen ? '#A85C3A' : '#E2DACD'};` +
             `opacity:${dim ? 0.55 : 1};box-shadow:${chosen ? '0 8px 22px rgba(28,25,23,.13)' : 'none'}`,
    }, [
      el('div', { class: 'node-meta' }, [
        d.source ? el('span', { class: 'tag' }, SOURCE_LABEL[d.source] || d.source.toUpperCase()) : null,
        el('span', { class: 'dot', style: `background:${(STATUS[d.status] || {}).color || '#A9A198'}` }),
        el('span', { style: 'font-size:8.5px;letter-spacing:.08em;color:#A9A198;text-transform:uppercase' },
          (STATUS[d.status] || {}).label || d.status),
        el('span', { style: 'margin-left:auto;font-size:9px;color:#B0A89E' }, shortDate(d.created_at)),
      ]),
      el('div', { class: 'node-title' }, d.summary),
      d.cluster ? el('div', { class: 'node-cluster' }, [
        el('span', { style: `width:5px;height:5px;background:${colour}` }),
        el('span', { style: 'font-size:8.5px;letter-spacing:.13em;text-transform:uppercase;color:#A0988E' }, d.cluster),
      ]) : null,
    ]);
    card.addEventListener('mousedown', e => startNodeDrag(e, d, p));
    refs.nodes[d.id] = card;
    world.appendChild(card);
  });

  canvas.appendChild(world);
  canvas.appendChild(el('div', {
    style: 'position:absolute;left:14px;bottom:14px;display:flex;gap:6px;z-index:4',
  }, [
    el('div', { class: 'zoom-btn', title: 'Zoom out', onClick: () => zoomBy(1 / 1.25) }, '−'),
    el('div', { class: 'zoom-btn', title: 'Zoom in', onClick: () => zoomBy(1.25) }, '+'),
    el('div', { class: 'zoom-btn', title: 'Fit', style: 'width:auto;padding:0 8px;font-size:9px;letter-spacing:.1em', onClick: () => fit() }, 'FIT'),
    Object.keys(state.moved).length ? el('div', {
      class: 'zoom-btn',
      title: 'Discard your arrangement and return to the computed layout',
      style: 'width:auto;padding:0 8px;font-size:9px;letter-spacing:.1em',
      onClick: () => { state.moved = {}; saveMoved(); render(); queueFit(); },
    }, 'RESET') : null,
  ]));

  if (state.panelOpen && state.selected) canvas.appendChild(renderPanel(colours));
  return canvas;
}

function relatedRow(label, ids) {
  if (!ids.length) return null;
  return el('div', { style: 'margin-bottom:8px' }, [
    el('span', { style: 'font-size:10px;color:#A0988E' }, label + ' '),
    ...ids.map(id => el('span', {
      class: 'link',
      onClick: () => {
        const target = state.decisions.find(d => d.id === id);
        if (target) select(target);
      },
    }, `#${id} `)),
  ]);
}

function renderPanel(colours) {
  const d = state.selected;
  const derived = state.decisions.filter(x => x.derives_from === d.id).map(x => x.id);
  const supersededBy = d.superseded_by ? [d.superseded_by] : [];

  const rows = [
    el('div', { style: 'display:flex;align-items:center;gap:8px;margin-bottom:14px' }, [
      el('span', { class: 'dot', style: `background:${(STATUS[d.status] || {}).color}` }),
      el('span', { style: 'font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:#A9A198' },
        (STATUS[d.status] || {}).label || d.status),
      el('span', { style: 'margin-left:auto;font-size:10px;color:#B0A89E' }, shortDate(d.created_at)),
      el('span', {
        style: 'cursor:pointer;font-size:15px;color:#A0988E;line-height:1',
        onClick: () => { state.panelOpen = false; state.selected = null; render(); },
      }, '×'),
    ]),
    el('div', { class: 'panel-sec' }, 'Statement'),
    el('div', {
      style: 'font-family:Newsreader,serif;font-size:19px;line-height:1.32;margin-bottom:18px',
    }, d.summary),
    el('div', { class: 'panel-sec' }, 'Reasoning'),
    el('div', { class: 'prose', style: 'margin-bottom:18px' }, d.reasoning),
    el('div', { class: 'panel-sec' }, 'Citation'),
    el('div', { class: 'quote', style: 'margin-bottom:10px' }, d.excerpt),
  ];

  if (d.ref) rows.push(el('div', {
    style: 'font-size:9.5px;color:#A0988E;word-break:break-all;margin-bottom:18px',
  }, d.ref));

  const related = [
    relatedRow('Derives from', d.derives_from ? [d.derives_from] : []),
    relatedRow('Derived by', derived),
    relatedRow('Supersedes', d.supersedes ? [d.supersedes] : []),
    relatedRow('Superseded by', supersededBy),
  ].filter(Boolean);
  if (related.length) {
    rows.push(el('div', { class: 'panel-sec', style: 'margin-top:8px' }, 'Connections'));
    rows.push(...related);
  }

  const meta = [];
  if (d.cluster) meta.push(`Cluster ${d.cluster}`);
  if (d.author) meta.push(`Logged by ${d.author}`);
  if (meta.length) {
    rows.push(el('div', {
      style: 'margin-top:16px;padding-top:12px;border-top:1px solid #E7E0D4;font-size:10px;color:#A0988E;line-height:1.7',
    }, meta.join(' · ')));
  }

  return el('div', { class: 'panel om-scroll', style: 'padding:22px 24px 40px' }, rows);
}

function select(d) {
  state.selected = d;
  state.panelOpen = true;
  render();
  reveal(d.id);
}

function reveal(id) {
  // The panel covers the right 414px, which is exactly where a node the user
  // just clicked often sits. Slide it clear rather than hiding what they asked
  // to look at.
  const canvas = document.getElementById('canvas');
  const cached = layoutCache.value;
  if (!canvas || !cached || !state.panelOpen) return;
  const p = cached.pos['d:' + id];
  if (!p) return;
  const rect = canvas.getBoundingClientRect();
  const right = state.tx + (p.x + NW) * state.scale;
  const limit = rect.width - 414 - 24;
  if (right > limit) {
    state.tx -= right - limit;
    applyTransform();
  }
}

const ZOOM_MIN = 0.25, ZOOM_MAX = 1.8;

/** Zoom about a point, keeping whatever is under it fixed. */
function zoomAt(factor, mx, my, animate = false) {
  const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, state.scale * factor));
  if (next === state.scale) return;
  state.tx = mx - (mx - state.tx) * (next / state.scale);
  state.ty = my - (my - state.ty) * (next / state.scale);
  state.scale = next;
  applyTransform(animate);
}

function zoomBy(factor) {
  const canvas = document.getElementById('canvas');
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  zoomAt(factor, rect.width / 2, rect.height / 2, true);
}

/** How far one wheel event should zoom.
 *
 * A fixed step per event is what made this unusable: a single trackpad flick
 * emits dozens of events, so a constant 0.9 compounded to nothing. Scaling by
 * the reported delta makes a small movement small. The exponential keeps it
 * symmetric — scrolling back up exactly undoes scrolling down.
 */
function wheelFactor(event) {
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1;
  // Clamp so one oversized event (some mice report 100+ per notch) cannot
  // swing the whole range at once.
  const delta = Math.max(-60, Math.min(60, event.deltaY * unit));
  // A trackpad pinch arrives as ctrl+wheel and is deliberately coarser.
  const sensitivity = event.ctrlKey ? 0.005 : 0.0022;
  return Math.exp(-delta * sensitivity);
}

/* ------------------------------------------------------------ interaction - */

let drag = null;      // panning the canvas
let nodeDrag = null;  // repositioning one node

// Below this many pixels of movement a mousedown/up is a click, not a drag.
// Without it, the tiny wobble in a real click swallows every selection.
const DRAG_THRESHOLD = 4;

function startNodeDrag(event, decision, position) {
  if (event.button !== 0) return;
  event.preventDefault();
  event.stopPropagation();
  nodeDrag = {
    decision,
    startX: event.clientX, startY: event.clientY,
    originX: position.x, originY: position.y,
    dragged: false,
  };
}

function moveNode(event) {
  const dx = (event.clientX - nodeDrag.startX) / state.scale;
  const dy = (event.clientY - nodeDrag.startY) / state.scale;
  if (!nodeDrag.dragged &&
      Math.hypot(event.clientX - nodeDrag.startX, event.clientY - nodeDrag.startY) < DRAG_THRESHOLD) {
    return;
  }
  if (!nodeDrag.dragged) {
    nodeDrag.dragged = true;
    document.body.style.cursor = 'grabbing';
    const card = refs.nodes[nodeDrag.decision.id];
    if (card) card.classList.add('dragging');
  }

  const id = nodeDrag.decision.id;
  const next = { x: nodeDrag.originX + dx, y: nodeDrag.originY + dy };
  state.moved[id] = next;

  // Move the card and its edges directly. A full re-render per mouse move
  // would rebuild the DOM — and re-running the force layout would fight the
  // drag by pulling the node back.
  const card = refs.nodes[id];
  if (card) { card.style.left = `${next.x}px`; card.style.top = `${next.y}px`; }
  const { pos } = layout(visible());
  const key = 'd:' + id;
  refs.lines.forEach(entry => {
    if (entry.a === key || entry.b === key) placeLine(entry, pos);
  });
}

function endNodeDrag() {
  const finished = nodeDrag;
  nodeDrag = null;
  if (!finished) return;
  document.body.style.cursor = '';
  if (finished.dragged) {
    saveMoved();
    render();          // repaint once, so the panel and rail see the new state
  } else {
    select(finished.decision);
  }
}

function wireCanvas() {
  const canvas = document.getElementById('canvas');
  if (!canvas) return;

  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    zoomAt(wheelFactor(e), e.clientX - rect.left, e.clientY - rect.top);
  }, { passive: false });

  canvas.addEventListener('mousedown', e => {
    if (e.target.closest('.node') || e.target.closest('.panel') || e.target.closest('.zoom-btn')) return;
    drag = { x: e.clientX - state.tx, y: e.clientY - state.ty };
    canvas.style.cursor = 'grabbing';
  });
}

window.addEventListener('mousemove', e => {
  if (nodeDrag) { moveNode(e); return; }
  if (!drag) return;
  state.tx = e.clientX - drag.x;
  state.ty = e.clientY - drag.y;
  applyTransform();
});

window.addEventListener('mouseup', () => {
  if (nodeDrag) { endNodeDrag(); return; }
  drag = null;
  const canvas = document.getElementById('canvas');
  if (canvas) canvas.style.cursor = 'grab';
});

window.addEventListener('keydown', e => {
  if (e.key === 'Escape' && state.panelOpen) {
    state.panelOpen = false; state.selected = null; render();
  }
});

window.addEventListener('resize', () => queueFit());

/* ----------------------------------------------------------------- boot --- */

function render() {
  const colours = clusterColours();
  const app = document.getElementById('app');
  app.replaceChildren(renderRail(colours), renderCanvas(colours));
  applyTransform();
  wireCanvas();
}

(async () => {
  render();
  try {
    state.user = await getJSON('/whoami').catch(() => null);
    await loadProjects();
    if (!state.projects.length) {
      state.loading = false;
      state.error = 'No projects with decisions yet.';
      render();
      return;
    }
    // Open whichever project was touched most recently.
    const newest = [...state.projects].sort(
      (a, b) => String(b.last_activity || '').localeCompare(String(a.last_activity || ''))
    )[0];
    await loadProject(newest.name);
  } catch (err) {
    state.loading = false;
    state.error = `Could not load decisions: ${err.message}`;
    render();
  }
})();
