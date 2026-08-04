/* DecisionTree — Context Graph.
 *
 * The force constants are ported from the design's graph() and kept
 * deliberately faithful: the same virtual root/cluster hierarchy, the same
 * spring weights and ideal lengths. Change those numbers and it stops looking
 * like the design — which is why the graph was grown by scaling the solved
 * coordinates (WORLD) rather than by loosening the forces. What did change is
 * that they run a frame at a time against a decaying alpha instead of 460
 * iterations up front, so the springs are still live once the graph settles.
 *
 * Fields the vault does not carry yet (cluster, source, author, ref) are
 * hidden rather than faked — a filter over data that is always null is worse
 * than no filter.
 */

/* Theme follows the system until the user says otherwise, and that choice
 * sticks. Applied to <html> before first paint so the page never flashes the
 * wrong background. */
const THEME_KEY = 'decisiontree:theme';

function preferredTheme() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === 'light' || saved === 'dark') return saved;
  } catch { /* storage disabled */ }
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
    ? 'dark' : 'light';
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  state.theme = theme;
}

function toggleTheme() {
  const next = state.theme === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  try { localStorage.setItem(THEME_KEY, next); } catch { /* storage disabled */ }
  // Orb, hull and edge colours are set as attributes at render time, so a
  // repaint is what actually moves them; CSS alone only covers the chrome.
  render();
}

/* Read from CSS so a theme change moves every colour at once — a second
 * palette hardcoded here would drift the moment either side is edited. */
const cssVar = name =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const palette = () => [0, 1, 2, 3, 4, 5].map(i => cssVar(`--cluster-${i}`));
const UNCLUSTERED = 'Unclustered';

const STATUS = {
  active:     { label: 'Active',     color: 'var(--ok)' },
  superseded: { label: 'Superseded', color: 'var(--muted-2)' },
  retired:    { label: 'Retired',    color: 'var(--muted-3)' },
};

const SOURCE_LABEL = { chat: 'CHAT', code: 'CODE', pr: 'PR', doc: 'DOC' };

const NW = 214, NH = 96;

const state = {
  projects: [], project: null, decisions: [], edges: [],
  clusters: [], sources: [],
  query: '', statusOff: {}, sourceOff: {}, clusterOff: {},
  selected: null, panelOpen: false,
  layout: {},         // id -> {x, y}: where the graph last settled
  pinned: {},         // id -> true: nodes the user placed, which the sim leaves alone
  hovered: null,      // decision id under the cursor, for neighbourhood dimming
  tx: 40, ty: 20, scale: 0.82,
  loading: true, error: null, user: null,
  theme: 'light',
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
  const restored = loadLayout(name);
  state.layout = restored.positions;
  state.pinned = restored.pinned;
  state.loading = false;
  sim.key = null;
  render();
  queueFit();
}

/* ---------------------------------------------------------- arrangement --- */

/* Where the graph settled is the user's own view of the project, so it is kept
 * per project. Nodes the user dragged are pinned and the simulation leaves
 * them alone; everything else is free to re-settle around them. */

const layoutKeyFor = name => `decisiontree:layout:${name}`;

function loadLayout(name) {
  try {
    const raw = JSON.parse(localStorage.getItem(layoutKeyFor(name)) || '{}');
    return { positions: raw.positions || {}, pinned: raw.pinned || {} };
  } catch {
    return { positions: {}, pinned: {} };
  }
}

function saveLayout() {
  if (!state.project) return;
  const positions = {};
  sim.nodes.forEach(n => { positions[n.id] = { x: Math.round(n.x), y: Math.round(n.y) }; });
  try {
    localStorage.setItem(layoutKeyFor(state.project),
      JSON.stringify({ positions, pinned: state.pinned }));
  } catch { /* storage disabled — the arrangement just won't outlive the tab */ }
}

function clearLayout() {
  state.layout = {}; state.pinned = {};
  try { localStorage.removeItem(layoutKeyFor(state.project)); } catch { /* ignore */ }
  sim.key = null;
  render();
  reheat(1);
}

const centreOf = id => sim.index['d:' + id];

/* -------------------------------------------------------------- helpers --- */

const clusterOf = d => d.cluster || UNCLUSTERED;

function clusterColours() {
  const names = [...new Set(state.decisions.map(clusterOf))].sort();
  const map = {};
  const colours = palette();
  names.forEach((name, i) => { map[name] = colours[i % colours.length]; });
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

/* ------------------------------------------------------------ simulation --- */

/* The forces are the ones the batch layout used — the shape they produce is
 * the shape that was signed off. What changed is that they run every frame
 * instead of 460 times up front: a graph that is still solving reads as alive,
 * and dragging a node can pull its neighbours because the springs are live
 * rather than already resolved.
 */
/* The batch layout solved with these constants and then scaled the solved
 * result to fit 1250x860. Growing the graph by changing the constants changed
 * its shape too — weakening the centring force strung the clusters out into a
 * diagonal chain. So the forces are exactly the originals, and the coordinates
 * they produce are multiplied at render time, which is what the batch version
 * effectively did. The simulation thinks in solved units; the DOM is in world
 * units; WORLD is the only bridge between them.
 */
const WORLD = 2.2;
const FORCE = {
  repel: 44 * 44,     // K² from the original layout
  cutoff2: 25600,     // beyond ~160 units repulsion is not worth computing
  spring: 0.045,
  centre: 0.006,
  damping: 0.72,
  decay: 0.028,       // ~3s to settle from cold at 60fps
  floor: 0.004,       // below this the graph is at rest and the loop stops
};

const sim = {
  key: null, nodes: [], index: {}, links: [], clusters: [],
  alpha: 0, frame: null,
  // A graph that is still solving grows well past the frame it was fitted to,
  // so a cold one is re-fitted when it comes to rest. Any pan, zoom or drag
  // hands the view to the user and cancels that — re-framing under someone who
  // has just positioned the view is worse than an imperfect fit.
  fitOnRest: false,
};

const layoutKey = list => state.project + '|' + list.map(d => d.id).join(',');

/** Deterministic starting spread, so a cold graph never begins degenerate. */
function seedPosition(i) {
  const angle = i * 2.399963, radius = 40 + 70 * Math.sqrt(i);
  return { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
}

function buildSim(list) {
  const clusters = [...new Set(list.map(clusterOf))].sort();
  const ids = ['__root'].concat(clusters.map(c => 'cl:' + c), list.map(d => 'd:' + d.id));

  const saved = state.layout || {};
  const previous = sim.index;
  const nodes = ids.map((id, i) => {
    // Carry a node across a filter change so the graph glides to its new shape
    // rather than restarting; fall back to a saved position, then to the seed.
    // The root is a layout anchor rather than a datum, and it is nailed to the
    // origin. Left free it drifts, and since the centring force pulls towards
    // the origin regardless, the clusters end up orbiting a centre that is not
    // where they are being pulled — which is what strung them into a diagonal.
    if (id === '__root') return { id, x: 0, y: 0, vx: 0, vy: 0, pinned: true };
    const from = previous[id] || saved[id] || seedPosition(i);
    return {
      id, x: from.x, y: from.y, vx: 0, vy: 0,
      pinned: !!(state.pinned && state.pinned[id]),
    };
  });

  const index = {};
  nodes.forEach(n => { index[n.id] = n; });

  const links = [];
  clusters.forEach(c => links.push({ a: '__root', b: 'cl:' + c, w: 1.3, ideal: 430 }));
  list.forEach(d => links.push({ a: 'cl:' + clusterOf(d), b: 'd:' + d.id, w: 4.0, ideal: 50 }));

  const present = new Set(list.map(d => d.id));
  list.forEach(d => {
    if (d.derives_from && present.has(d.derives_from)) {
      links.push({ a: 'd:' + d.derives_from, b: 'd:' + d.id, w: 1.5, ideal: 58, kind: 'derives' });
    }
  });
  state.edges.filter(e => e.kind === 'supersedes').forEach(e => {
    if (present.has(e.from) && present.has(e.to)) {
      links.push({ a: 'd:' + e.to, b: 'd:' + e.from, w: 0.4, ideal: 120, kind: 'supersedes' });
    }
  });

  const hadPositions = ids.every(id => previous[id] || saved[id]);
  Object.assign(sim, {
    key: layoutKey(list), nodes, index, links, clusters,
    // A graph restored from saved positions is already at rest; a fresh one
    // should be seen to arrive.
    alpha: hadPositions ? 0 : 1,
    fitOnRest: sim.fitOnRest || !hadPositions,
  });
}

function ensureSim(list) {
  if (sim.key !== layoutKey(list)) buildSim(list);
  return sim;
}

function stepSim() {
  const nodes = sim.nodes;
  sim.alpha += (0 - sim.alpha) * FORCE.decay;
  const a = sim.alpha;

  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const p = nodes[i], q = nodes[j];
      let dx = p.x - q.x, dy = p.y - q.y;
      let d2 = dx * dx + dy * dy;
      if (d2 > FORCE.cutoff2) continue;
      if (d2 < 1) { d2 = 1; dx = (i % 3) - 1 || 0.7; dy = (j % 3) - 1 || 0.5; }
      const d = Math.sqrt(d2);
      const f = (FORCE.repel / d2) * a;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      p.vx += fx; p.vy += fy; q.vx -= fx; q.vy -= fy;
    }
  }

  for (const l of sim.links) {
    const p = sim.index[l.a], q = sim.index[l.b];
    if (!p || !q) continue;
    const dx = q.x - p.x, dy = q.y - p.y;
    const d = Math.max(1, Math.hypot(dx, dy));
    const f = (d - l.ideal) * FORCE.spring * l.w * a;
    const fx = (dx / d) * f, fy = (dy / d) * f;
    p.vx += fx; p.vy += fy; q.vx -= fx; q.vy -= fy;
  }

  for (const p of nodes) {
    p.vx -= p.x * FORCE.centre * a;
    p.vy -= p.y * FORCE.centre * a;
    if (p.pinned) { p.vx = 0; p.vy = 0; continue; }
    p.vx *= FORCE.damping; p.vy *= FORCE.damping;
    p.x += p.vx; p.y += p.vy;
  }
}

function runSim() {
  if (sim.frame !== null) return;
  const tick = () => {
    stepSim();
    paint();
    if (sim.alpha > FORCE.floor) {
      sim.frame = requestAnimationFrame(tick);
    } else {
      sim.frame = null;
      saveLayout();      // at rest: remember where everything landed
      if (sim.fitOnRest) { sim.fitOnRest = false; fit(); }
    }
  };
  sim.frame = requestAnimationFrame(tick);
}

/** Warm the simulation back up. Any interaction should make it re-settle
 *  rather than snap, which is the whole difference in feel. */
function reheat(target = 0.35) {
  sim.alpha = Math.max(sim.alpha, target);
  runSim();
}

/** The bounding box of what is actually on screen, in world units.
 *
 * Guessing extents per tier does not work: an orb carries a 180px label, sits
 * in a hull padded 54px, and a cluster title floats 92px above its topmost
 * member. Measuring the rendered result is exact and survives changes to any
 * of those. World children are positioned in world units and scaled by the
 * transform, so offset* and SVG getBBox() are already the coordinates we want.
 */
/* The bounding box of the graph, derived from the simulation rather than from
 * the DOM.
 *
 * Measuring the DOM made the fit depend on the detail tier: labels are real
 * elements, so they widened the box, which lowered the scale, which dropped
 * the tier, which removed the labels — and the next fit read the narrower box
 * and put them back. A graph whose natural fit sat near a threshold flipped
 * between two scales indefinitely. The simulation *is* the graph; labels are
 * annotations hung off it and are allowed to overflow.
 */
const NODE_MARGIN = 46;   // clears the largest orb and the largest cluster ring

function layoutBBox() {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of sim.nodes) {
    if (n.id === '__root') continue;          // no visual form, so not in frame
    const x = n.x * WORLD, y = n.y * WORLD;
    if (!isFinite(x) || !isFinite(y)) continue;
    x0 = Math.min(x0, x); y0 = Math.min(y0, y);
    x1 = Math.max(x1, x); y1 = Math.max(y1, y);
  }
  if (x0 === Infinity) return null;
  return { x0: x0 - NODE_MARGIN, y0: y0 - NODE_MARGIN,
           x1: x1 + NODE_MARGIN, y1: y1 + NODE_MARGIN };
}

function fit() {
  const canvas = document.getElementById('canvas');
  const list = visible();
  if (!canvas || !list.length) return;
  const b = layoutBBox();
  if (!b) return;
  const tierBefore = tierFor(state.scale);
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
  // Fitting can cross a detail threshold, which changes what is drawn. It no
  // longer changes what is measured, so redrawing is all that is needed — no
  // second fit, and no scale to converge on.
  if (tierFor(state.scale) !== tierBefore) render();
}

function queueFit(tries = 0) {
  // The box comes from the simulation, so the only thing worth waiting for is
  // the canvas having a width to fit into. Two frames still, because a canvas
  // that render() has only just produced has not been laid out yet, and
  // fitting into a zero-width box silently leaves the view unfitted.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const canvas = document.getElementById('canvas');
    const ready = canvas
      && canvas.getBoundingClientRect().width >= 40
      && sim.nodes.length;
    if (!ready) {
      if (tries < 20) queueFit(tries + 1);
      return;
    }
    fit();
  }));
}

function applyTransform(animate = false) {
  const world = document.getElementById('world');
  if (!world) return;
  // Eased only for button and fit moves. Wheel and drag must track the input
  // exactly; a transition there feels like lag, not smoothness.
  if (animate) {
    // Commit the current transform before arming the transition. render()
    // creates a fresh #world every time, and a transition declared on an
    // element that has never had its transform committed swallows the change
    // entirely — fit() appeared to do nothing at all.
    world.style.transition = 'none';
    void world.offsetWidth;
    world.style.transition = 'transform .18s ease';
  } else {
    world.style.transition = 'none';
  }
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

  const search = el('div', { style: 'padding:14px 16px 12px;border-bottom:1px solid var(--line)' }, [
    el('input', {
      class: 'search', placeholder: 'Search decisions…', value: state.query,
      onInput: e => { state.query = e.target.value; sim.key = null; render(); reheat(0.5); },
    }),
  ]);

  const projects = el('div', {
    style: 'padding:6px 10px 14px;display:flex;flex-direction:column;gap:2px;border-bottom:1px solid var(--line)',
  }, state.projects.map(p => {
    const on = p.name === state.project;
    return el('div', {
      class: 'proj',
      style: `background:${on ? 'var(--selected)' : 'transparent'};border-left-color:${on ? 'var(--accent)' : 'transparent'}`,
      onClick: () => loadProject(p.name),
    }, [
      el('span', { class: 'proj-name', style: `color:${on ? 'var(--ink)' : 'var(--ink-2)'}` }, p.name),
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
        style: `border:1px solid ${off ? 'var(--line-soft)' : 'var(--line-strong)'};background:${off ? 'transparent' : 'var(--surface)'};color:${off ? 'var(--muted-3)' : 'var(--ink-2)'}`,
        onClick: () => { state.statusOff[k] = !off; sim.key = null; render(); reheat(0.5); },
      }, [
        el('span', { class: 'dot', style: `background:${STATUS[k].color}` }),
        STATUS[k].label,
        el('span', { style: 'color:var(--muted-2);font-size:9px' }, String(statusCounts[k])),
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
          style: `border:1px solid ${off ? 'var(--line-soft)' : 'var(--line-strong)'};background:${off ? 'transparent' : 'var(--surface)'};color:${off ? 'var(--muted-3)' : 'var(--ink-2)'};letter-spacing:.1em`,
          onClick: () => { state.sourceOff[s] = !off; sim.key = null; render(); reheat(0.5); },
        }, [SOURCE_LABEL[s] || s.toUpperCase(),
            el('span', { style: 'color:var(--muted-2);font-size:9px' }, String(sourceCounts[s] || 0))]);
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
          onClick: () => { state.clusterOff[name] = !off; sim.key = null; render(); reheat(0.5); },
        }, [
          el('span', { class: 'swatch', style: `background:${colours[name]}` }),
          el('span', { style: 'flex:1;font-size:11px;color:var(--ink-2)' }, name),
          el('span', { style: 'font-size:9.5px;color:var(--muted-2)' }, String(clusterCounts[name])),
        ]);
      })));
  }

  const project = state.projects.find(p => p.name === state.project);
  const foot = el('div', {
    style: 'padding:11px 16px 9px;border-top:1px solid var(--line);font-size:9.5px;color:var(--muted);line-height:1.6',
  }, [
    `${shown.length} of ${state.decisions.length} shown`,
    el('br'),
    el('span', { style: 'color:var(--muted-3)' },
      project && project.last_activity ? `Last logged ${shortDate(project.last_activity)}` : ''),
  ]);

  const account = el('div', {
    style: 'padding:10px 16px 14px;border-top:1px solid var(--line);display:flex;align-items:center;gap:8px',
  }, [
    el('span', { style: 'flex:1;font-size:10px;color:var(--muted);overflow:hidden;text-overflow:ellipsis' },
      state.user ? state.user.email || '' : ''),
    el('span', {
      class: 'theme-toggle',
      title: state.theme === 'dark' ? 'Switch to light' : 'Switch to dark',
      onClick: toggleTheme,
    }, state.theme === 'dark' ? '☾' : '☀'),
    el('a', { class: 'link', href: '/logout', style: 'font-size:10px' }, 'Sign out'),
  ]);

  return el('aside', {}, [head, search,
    el('div', { class: 'sec', style: 'padding:16px 16px 4px' }, 'Projects'),
    projects, body, foot, account]);
}

/* Live references into the rendered canvas so a drag can move one node and its
 * edges without re-rendering the whole graph on every mouse move. */
const refs = { nodes: {}, lines: [], labels: {}, hubs: {} };

const SVG = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs = {}) => {
  const node = document.createElementNS(SVG, tag);
  Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
  return node;
};

/* Detail rises with zoom rather than everything shouting at once. Cards cover
 * roughly half the canvas at 28 decisions, which buries the edges the layout
 * exists to show — so they only appear once you are close enough to be reading
 * a few, not scanning all of them. */
/* Labels are drawn inside the transformed world, so a label and the gap
 * between two orbs scale together — zooming never separates colliding labels.
 * The threshold therefore has to sit clear of the fit scale (~0.56 on a full
 * project) rather than next to it, or the overview lands on whichever side of
 * the boundary the graph happened to settle. Overview is orbs; labels are for
 * when the user has deliberately zoomed in to read. */
const TIER_LABEL = 0.8, TIER_CARD = 1.35;
const tierFor = scale => (scale < TIER_LABEL ? 'orb' : scale < TIER_CARD ? 'label' : 'card');

/** Edges to other decisions. Cluster membership is structure, not connection. */
function degrees(list) {
  const present = new Set(list.map(d => d.id));
  const count = {};
  const bump = id => { if (present.has(id)) count[id] = (count[id] || 0) + 1; };
  list.forEach(d => {
    if (d.derives_from && present.has(d.derives_from)) { bump(d.id); bump(d.derives_from); }
    if (d.supersedes && present.has(d.supersedes)) { bump(d.id); bump(d.supersedes); }
  });
  return count;
}

const orbRadius = degree => 5.5 + 3.2 * Math.sqrt(degree || 0);

function placeLine(entry) {
  const a = sim.index[entry.a], b = sim.index[entry.b];
  if (!a || !b) return false;
  entry.el.setAttribute('x1', a.x * WORLD); entry.el.setAttribute('y1', a.y * WORLD);
  entry.el.setAttribute('x2', b.x * WORLD); entry.el.setAttribute('y2', b.y * WORLD);
  return true;
}

/** Write the simulation's positions onto the DOM. Runs every frame while the
 *  graph is warm, so it touches only style properties and never rebuilds. */
function paint() {
  for (const id in refs.nodes) {
    const node = sim.index['d:' + id], elm = refs.nodes[id];
    if (!node || !elm) continue;
    const x = node.x * WORLD, y = node.y * WORLD;
    if (elm._r !== undefined) {
      elm.style.left = `${x - elm._r}px`;
      elm.style.top = `${y - elm._r}px`;
      const label = refs.labels[id];
      if (label) {
        label.style.left = `${x - 90}px`;
        label.style.top = `${y + elm._r + 5}px`;
      }
    } else {
      elm.style.left = `${x - NW / 2}px`;
      elm.style.top = `${y - NH / 2}px`;
    }
  }
  for (const name in refs.hubs) {
    const node = sim.index['cl:' + name], hub = refs.hubs[name];
    if (!node) continue;
    const x = node.x * WORLD, y = node.y * WORLD;
    hub.ring.style.left = `${x - hub.r}px`;
    hub.ring.style.top = `${y - hub.r}px`;
    hub.label.style.left = `${x - 110}px`;
    hub.label.style.top = `${y + hub.r + 6}px`;
  }
  refs.lines.forEach(placeLine);
}

/** Fade everything not connected to the node under the cursor. Obsidian does
 *  this and it is half of why its graph feels responsive to attention. */
function setHovered(id) {
  if (state.hovered === id) return;
  state.hovered = id;

  const near = new Set();
  if (id !== null) {
    near.add(String(id));
    sim.links.forEach(l => {
      if (l.a === 'd:' + id && l.b.startsWith('d:')) near.add(l.b.slice(2));
      if (l.b === 'd:' + id && l.a.startsWith('d:')) near.add(l.a.slice(2));
    });
  }
  for (const nid in refs.nodes) {
    const faded = id !== null && !near.has(nid);
    refs.nodes[nid].classList.toggle('faded', faded);
    if (refs.labels[nid]) refs.labels[nid].classList.toggle('faded', faded);
  }
  refs.lines.forEach(entry => {
    const touches = id !== null && (entry.a === 'd:' + id || entry.b === 'd:' + id);
    entry.el.classList.toggle('faded', id !== null && !touches);
  });
}

let hoverPop = null;

function hidePop() {
  if (hoverPop) { hoverPop.remove(); hoverPop = null; }
}

function showPop(decision, colour, canvas) {
  hidePop();
  const orb = refs.nodes[decision.id];
  if (!orb) return;
  const box = orb.getBoundingClientRect();
  const area = canvas.getBoundingClientRect();

  const pop = el('div', { class: 'pop' }, [
    el('div', { class: 'pop-meta' }, [
      decision.cluster
        ? el('span', { style: `color:${colour};letter-spacing:.13em` }, decision.cluster.toUpperCase())
        : null,
      el('span', { style: 'margin-left:auto;color:var(--muted-2)' }, shortDate(decision.created_at)),
    ]),
    el('div', { class: 'pop-title' }, decision.summary),
  ]);

  // Place above the orb, flipping below when there is no room, and clamped so
  // it never leaves the canvas.
  pop.style.visibility = 'hidden';
  canvas.appendChild(pop);
  const w = pop.offsetWidth, h = pop.offsetHeight;
  let left = box.left - area.left + box.width / 2 - w / 2;
  let top = box.top - area.top - h - 12;
  if (top < 8) top = box.bottom - area.top + 12;
  left = Math.max(8, Math.min(area.width - w - 8, left));
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
  pop.style.visibility = '';
  hoverPop = pop;
}

function renderCanvas(colours) {
  const list = visible();
  const canvas = el('div', {
    id: 'canvas',
    style: 'position:relative;overflow:hidden;cursor:grab;background:var(--canvas)',
  });

  if (state.loading || state.error || !list.length) {
    const message = state.error
      ? state.error
      : state.loading
        ? 'Loading…'
        : state.decisions.length
          ? 'Nothing matches those filters.'
          : 'No decisions in this project yet.';
    canvas.appendChild(el('div', {
      style: 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;'
           + `font-size:11px;color:${state.error ? 'var(--accent)' : 'var(--muted)'}`,
    }, message));
    return canvas;
  }

  ensureSim(list);
  const { index: pos, links, clusters } = sim;
  const tier = tierFor(state.scale);
  const degree = degrees(list);

  const world = el('div', {
    id: 'world',
    style: 'position:absolute;left:0;top:0;transform-origin:0 0;will-change:transform',
  });

  const svg = svgEl('svg', {
    style: 'position:absolute;left:0;top:0;overflow:visible;pointer-events:none',
    width: 1, height: 1,
  });

  // 1. Every edge, including the structural cluster spokes. Drawing only the
  // typed ones left any decision without a derives_from as an isolated dot —
  // in this graph a node's cluster *is* a connection, so it gets a line.
  refs.lines = [];
  links.forEach(l => {
    // The root has no visual form, so its spokes would be long lines running
    // into empty space. It still does its work as a force.
    if (l.a === '__root' || l.b === '__root') return;
    const structural = !l.kind;
    const line = svgEl('line', {
      stroke: l.kind === 'supersedes' ? 'var(--edge-strong)' : 'var(--edge)',
      'stroke-width': l.kind === 'derives' ? 1.5 : 1,
      'stroke-opacity': structural ? 0.5 : 0.9,
    });
    if (l.kind === 'supersedes') line.setAttribute('stroke-dasharray', '3 3');
    const entry = { el: line, a: l.a, b: l.b };
    if (!placeLine(entry)) return;
    refs.lines.push(entry);
    svg.appendChild(line);
  });
  world.appendChild(svg);

  refs.hubs = {};
  // 2. The cluster itself as a node. The spokes have to terminate somewhere,
  // and a labelled hub identifies the category without tinting the background.
  clusters.forEach(name => {
    const solved = pos['cl:' + name];
    const members = list.filter(d => clusterOf(d) === name);
    if (!solved || !members.length || name === UNCLUSTERED) return;
    const hub = { x: solved.x * WORLD, y: solved.y * WORLD };
    const r = 7 + 1.6 * Math.sqrt(members.length);
    const ring = el('div', {
      class: 'hub',
      style: `left:${hub.x - r}px;top:${hub.y - r}px;width:${r * 2}px;height:${r * 2}px;`
           + `border-color:${colours[name]}`,
    });
    const label = el('div', {
      class: 'cluster-label',
      style: `left:${hub.x - 110}px;top:${hub.y + r + 6}px;color:${colours[name]}`,
    }, [
      el('span', {}, name),
      el('span', { class: 'cluster-count' }, String(members.length)),
    ]);
    refs.hubs[name] = { ring, label, r };
    world.appendChild(ring);
    world.appendChild(label);
  });

  // 3. Decisions.
  refs.nodes = {}; refs.labels = {};
  list.forEach(d => {
    const solved = centreOf(d.id);
    if (!solved) return;
    const c = { x: solved.x * WORLD, y: solved.y * WORLD };
    const chosen = state.selected && state.selected.id === d.id;
    const dim = d.status === 'superseded';
    const colour = colours[clusterOf(d)];

    if (tier === 'card') {
      const p = { x: c.x - NW / 2, y: c.y - NH / 2 };
      const card = el('div', {
        class: 'node' + (chosen ? ' selected' : ''),
        style: `left:${p.x}px;top:${p.y}px;border:1px solid ${chosen ? 'var(--accent)' : 'var(--line)'};`
             + `opacity:${dim ? 0.55 : 1}`,
      }, [
        el('div', { class: 'node-meta' }, [
          d.source ? el('span', { class: 'tag' }, SOURCE_LABEL[d.source] || d.source.toUpperCase()) : null,
          el('span', { class: 'dot', style: `background:${(STATUS[d.status] || {}).color || 'var(--muted-2)'}` }),
          el('span', { style: 'font-size:8.5px;letter-spacing:.08em;color:var(--muted-2);text-transform:uppercase' },
            (STATUS[d.status] || {}).label || d.status),
          el('span', { style: 'margin-left:auto;font-size:9px;color:var(--muted-2)' }, shortDate(d.created_at)),
        ]),
        el('div', { class: 'node-title' }, d.summary),
        d.cluster ? el('div', { class: 'node-cluster' }, [
          el('span', { style: `width:5px;height:5px;background:${colour}` }),
          el('span', { style: 'font-size:8.5px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted)' }, d.cluster),
        ]) : null,
      ]);
      card.addEventListener('mousedown', e => startNodeDrag(e, d));
      refs.nodes[d.id] = card;
      world.appendChild(card);
      return;
    }

    const r = orbRadius(degree[d.id]);
    const orb = el('div', {
      class: 'orb' + (chosen ? ' selected' : '') + (dim ? ' dim' : ''),
      style: `left:${c.x - r}px;top:${c.y - r}px;width:${r * 2}px;height:${r * 2}px;`
           + `background:${colour};--orb:${colour}`,
      title: '',
    });
    orb.addEventListener('mousedown', e => startNodeDrag(e, d));
    orb.addEventListener('mouseenter', () => {
      if (nodeDrag) return;
      showPop(d, colour, canvas);
      setHovered(d.id);
    });
    orb.addEventListener('mouseleave', () => { hidePop(); setHovered(null); });
    orb._r = r;
    refs.nodes[d.id] = orb;
    world.appendChild(orb);

    if (tier === 'label') {
      const label = el('div', {
        class: 'orb-label',
        style: `left:${c.x - 90}px;top:${c.y + r + 5}px;opacity:${dim ? 0.5 : 1}`,
      }, d.summary);
      refs.labels[d.id] = label;
      world.appendChild(label);
    }
  });

  canvas.appendChild(world);
  canvas.appendChild(el('div', {
    style: 'position:absolute;left:14px;bottom:14px;display:flex;gap:6px;z-index:4',
  }, [
    el('div', { class: 'zoom-btn', title: 'Zoom out', onClick: () => zoomBy(1 / 1.25) }, '−'),
    el('div', { class: 'zoom-btn', title: 'Zoom in', onClick: () => zoomBy(1.25) }, '+'),
    el('div', { class: 'zoom-btn', title: 'Fit', style: 'width:auto;padding:0 8px;font-size:9px;letter-spacing:.1em', onClick: () => fit() }, 'FIT'),
    Object.keys(state.pinned || {}).length ? el('div', {
      class: 'zoom-btn',
      title: 'Unpin everything and let the graph settle again',
      style: 'width:auto;padding:0 8px;font-size:9px;letter-spacing:.1em',
      onClick: clearLayout,
    }, 'RESET') : null,
  ]));

  if (state.panelOpen && state.selected) canvas.appendChild(renderPanel(colours));
  return canvas;
}

function relatedRow(label, ids) {
  if (!ids.length) return null;
  return el('div', { style: 'margin-bottom:8px' }, [
    el('span', { style: 'font-size:10px;color:var(--muted)' }, label + ' '),
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
      el('span', { style: 'font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted-2)' },
        (STATUS[d.status] || {}).label || d.status),
      el('span', { style: 'margin-left:auto;font-size:10px;color:var(--muted-2)' }, shortDate(d.created_at)),
      el('span', {
        style: 'cursor:pointer;font-size:15px;color:var(--muted);line-height:1',
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
    style: 'font-size:9.5px;color:var(--muted);word-break:break-all;margin-bottom:18px',
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
      style: 'margin-top:16px;padding-top:12px;border-top:1px solid var(--line-soft);font-size:10px;color:var(--muted);line-height:1.7',
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
  if (!canvas || !state.panelOpen) return;
  const centre = centreOf(id);
  if (!centre) return;
  const p = { x: centre.x * WORLD - NW / 2, y: centre.y * WORLD - NH / 2 };
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
  sim.fitOnRest = false;
  const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, state.scale * factor));
  if (next === state.scale) return;
  const wasTier = tierFor(state.scale);
  state.tx = mx - (mx - state.tx) * (next / state.scale);
  state.ty = my - (my - state.ty) * (next / state.scale);
  state.scale = next;
  applyTransform(animate);
  // Crossing a detail threshold changes what is drawn, not just its size.
  if (tierFor(next) !== wasTier) { hidePop(); render(); }
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

function startNodeDrag(event, decision) {
  if (event.button !== 0) return;
  const node = centreOf(decision.id);
  if (!node) return;
  event.preventDefault();
  event.stopPropagation();
  nodeDrag = {
    decision, node,
    startX: event.clientX, startY: event.clientY,
    originX: node.x, originY: node.y,
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
    hidePop();
    document.body.style.cursor = 'grabbing';
    const card = refs.nodes[nodeDrag.decision.id];
    if (card) card.classList.add('dragging');
  }

  // Move the node itself and let the simulation carry the consequences: the
  // springs stretch, the neighbours follow, and the graph re-settles. Pinning
  // during the drag stops the forces fighting the cursor.
  const node = nodeDrag.node;
  node.pinned = true;
  sim.fitOnRest = false;
  node.x = nodeDrag.originX + dx / WORLD;
  node.y = nodeDrag.originY + dy / WORLD;
  node.vx = 0; node.vy = 0;
  reheat(0.45);
}

function endNodeDrag() {
  const finished = nodeDrag;
  nodeDrag = null;
  if (!finished) return;
  document.body.style.cursor = '';
  if (finished.dragged) {
    // Stays where it was put: pinned nodes are excluded from integration.
    state.pinned['d:' + finished.decision.id] = true;
    saveLayout();
    render();          // repaint once, so RESET appears and the rail updates
    reheat(0.2);
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
    sim.fitOnRest = false;
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
  // A repaint replaces the DOM the loop was writing to, so hand it the new
  // elements; if the graph is still warm it carries on from where it was.
  if (sim.nodes.length) { paint(); runSim(); }
}

applyTheme(preferredTheme());

// Follow the system if the user has never chosen explicitly.
if (window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e => {
    let chosen = null;
    try { chosen = localStorage.getItem(THEME_KEY); } catch { /* ignore */ }
    if (!chosen) { applyTheme(e.matches ? 'dark' : 'light'); render(); }
  });
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
