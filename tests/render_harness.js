// Fake DOM + page script loader for test_dashboard_render.py (not shipped).
const fs = require("fs"), vm = require("vm");
const IDS = JSON.parse(process.env.IDS), dir = process.env.DIR;
function mkEl(tag) {
  const e = { tag, children: [], className: "", _text: "", width: 360, height: 360, arcs: 0, texts: [] };
  Object.defineProperty(e, "textContent", {
    get() { return e.children.length ? e.children.map(c => c.textContent).join("") : e._text; },
    set(v) { e._text = String(v); e.children = []; },
  });
  Object.defineProperty(e, "firstChild", { get() { return e.children[0] || null; } });
  e.appendChild = (c) => { e.children.push(c); return c; };
  e.removeChild = (c) => { e.children.splice(e.children.indexOf(c), 1); };
  e.tBodies = [e._body = { children: [], appendChild(c) { this.children.push(c); },
    removeChild(c) { this.children.splice(this.children.indexOf(c), 1); },
    get firstChild() { return this.children[0] || null; } }];
  e.getContext = () => new Proxy({}, { get(_, k) {
    if (k === "arc") return () => { e.arcs++; };
    if (k === "fillText") return (t) => { e.texts.push(String(t)); };
    return () => {};
  }, set() { return true; } });
  return e;
}
const els = {}; IDS.forEach(id => { els[id] = mkEl(id); });
const handlers = {};
const context = {
  document: { getElementById: (id) => { if (!els[id]) throw new Error("no element " + id); return els[id]; },
              createElement: mkEl },
  EventSource: function () { this.addEventListener = (n, f) => { handlers[n] = f; }; this.close = () => {}; },
  Date, Math, JSON, isFinite, String, Infinity,
};
context.window = context;
context.window.setTimeout = () => 1; context.window.clearTimeout = () => {};
context.window.setInterval = () => 1;
context.globalThis = context;
vm.createContext(context);
for (const f of ["logic.js", "app.js"]) vm.runInContext(fs.readFileSync(dir + "/" + f, "utf8"), context);
function send(name, data) { handlers[name]({ data: JSON.stringify(data) }); }
function dl(id) { const c = els[id].children; const o = []; for (let i = 0; i < c.length; i += 2) o.push([c[i].textContent, c[i + 1].textContent]); return o; }
function dlCls(id) { const c = els[id].children; const o = {}; for (let i = 0; i < c.length; i += 2) o[c[i].textContent] = c[i + 1].className || ""; return o; }
function rows(id) { return els[id]._body.children.map(tr => tr.children.map(td => td.textContent)); }
function collect() {
  const out = { pnl: dl("pnl"), counts: dl("counts"), decision: dl("decision"), brier: rows("brier"),
    arcs: {}, notes: {}, texts: {},
    kill: [els["chip-kill"].textContent, els["chip-kill"].className],
    maker: dl("maker"), makerCls: dlCls("maker"), makerHidden: els["maker-card"].hidden === true,
    midTexts: els["mid-chart"].texts };
  IDS.filter(i => i.startsWith("rel-chart")).forEach(i => {
    out.arcs[i] = els[i].arcs; out.texts[i] = els[i].texts;
  });
  IDS.filter(i => i.startsWith("rel-note")).forEach(i => { out.notes[i] = els[i].textContent; });
  return out;
}
