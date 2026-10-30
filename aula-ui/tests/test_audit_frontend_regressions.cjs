const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const {test} = require("node:test");
const vm = require("node:vm");

const static_ = path.join(__dirname, "../aula_ui/static");
const source = readFileSync(path.join(static_, "app.js"), "utf8");
const slice = (from, to) => {
  const start = source.indexOf(from);
  const end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `${from} … ${to}`);
  return source.slice(start, end);
};

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.dataset = {};
    this.classes = new Set();
    this.classList = {
      add: name => this.classes.add(name),
      remove: name => this.classes.delete(name),
      toggle: (name, active) => (active ? this.classes.add(name) : this.classes.delete(name)),
      contains: name => this.classes.has(name),
    };
    this.disabled = false;
  }
  append(...children) { this.children.push(...children); }
  before() {}
  remove() {}
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  set innerHTML(value) { this.html = value; }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return (this.text || "") + this.children.map(child => child.textContent || "").join(""); }
  click() { if (!this.disabled) this.listeners.click?.(); }
  find(predicate) {
    for (const child of this.children) {
      if (!(child instanceof Element)) continue;
      if (predicate(child)) return child;
      const found = child.find(predicate);
      if (found) return found;
    }
    return null;
  }
}
const el = (tag, cls, text) => {
  const element = new Element(tag);
  if (cls) cls.split(/\s+/).filter(Boolean).forEach(name => element.classes.add(name));
  if (text !== undefined) element.textContent = text;
  return element;
};

test("share text and calendar evidence never show raw Markdown", () => {
  const context = vm.createContext({});
  vm.runInContext(slice("const paths =", "function el("), context);
  vm.runInContext(slice("function summaryMarkup(", "function chatParagraphs("), context);
  vm.runInContext(slice("function plainText(", "function richText("), context);
  assert.equal(context.plainText("### Trip\n**Summary** 0. Pack *warm clothes*\n\n- Boots"),
    "Trip Summary 0. Pack warm clothes - Boots");
  assert.equal(context.plainText("<p><b>Bold</b> text</p>"), "Bold text");
  assert.equal(context.plainText(undefined), "");
  const evidence = slice("function calendarEvidence(", "function agendaEvent(");
  assert.match(evidence, /richText\(translation\)/);
  assert.doesNotMatch(evidence, /English summary"\}: \$\{translation\}/);
});

test("a single album photo is not labelled 'Save all 1 photos'", () => {
  const context = vm.createContext({});
  vm.runInContext(slice("function saveAllLabel(", "function saveAllControl("), context);
  assert.equal(context.saveAllLabel(1), "Save photo");
  assert.equal(context.saveAllLabel(3), "Save all 3 photos");
});

function calendarContext() {
  const requests = [];
  let release;
  const context = vm.createContext({
    requests,
    activeRoute: "x",
    action: (name, label, callback) => {
      const button = el("button", "action", label);
      button.addEventListener("click", callback);
      return button;
    },
    calendarRequest: (url, value) => {
      requests.push(value.action);
      return new Promise((resolve, reject) => { release = {resolve, reject}; });
    },
    load: async () => {},
    navigate() {},
    notices: [],
  });
  context.setNotice = message => context.notices.push(message);
  vm.runInContext(slice("function calendarActions(", "function renderListRoute("), context);
  return {context, requests, settle: ok => (ok ? release.resolve() : release.reject(new Error("Aula said no")))};
}

test("Accept then Decline on an AI suggestion sends only one decision", async () => {
  const {context, requests, settle} = calendarContext();
  const [accept, decline, edit] = context.calendarActions({uid: "ev1", review_required: true});
  assert.equal(edit.textContent, "Edit event");
  accept.listeners.click();
  assert.equal(accept.disabled, true);
  assert.equal(decline.disabled, true);
  decline.listeners.click();
  assert.deepEqual(requests, ["accept"]);
  settle(false);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(context.notices, ["Aula said no"]);
  assert.equal(accept.disabled, false);
  assert.equal(decline.disabled, false);
  decline.listeners.click();
  assert.deepEqual(requests, ["accept", "decline"]);
});

test("the event page does not offer an Edit event link to itself", () => {
  const {context} = calendarContext();
  const labels = context.calendarActions({uid: "ev1", review_required: true}, {edit: false}).map(button => button.textContent);
  assert.equal(labels.length, 2);
  assert.deepEqual([...labels], ["Accept", "Decline"]);
});

function feedContext() {
  const responses = [];
  const counts = {render: 0, presence: 0, bell: 0};
  const sync = {dataset: {}};
  const timeline = {hidden: false};
  const context = vm.createContext({
    state: {items: [], loaded: false, tab: "home"},
    Date, JSON, Object,
    fetch: () => new Promise(resolve => responses.push(resolve)),
    document: {querySelector: selector => (selector === "#sync" ? sync : timeline)},
    localDateKey: () => "2026-10-26",
    timeLabel: value => value,
    updateAIProgress() {},
    setNotice() {},
    filters() {},
    typeFilters() {},
    render: () => { counts.render += 1; },
    refreshBell: () => { counts.bell += 1; },
    renderPresence: () => { counts.presence += 1; },
    renderSicknessControls() {},
    splitCalendar: () => false,
  });
  vm.runInContext(slice("const feedLoad =", "let bell = "), context);
  const reply = (index, payload) => responses[index]({ok: true, text: async () => JSON.stringify(payload)});
  return {context, counts, reply, responses};
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test("an older feed response never overwrites a newer one", async () => {
  const {context, counts, reply} = feedContext();
  const first = context.load();
  const second = context.load({force: true});
  reply(1, {items: [{id: "new"}], updated_at: "b"});
  await second;
  reply(0, {items: [{id: "old"}], updated_at: "a"});
  await first;
  assert.deepEqual([...context.state.items.map(item => item.id)], ["new"]);
  assert.equal(counts.render, 1);
});

test("polls do not stack and identical payloads skip the full feed rebuild", async () => {
  const {context, counts, reply, responses} = feedContext();
  const payload = {items: [{id: "a"}], updated_at: "x"};
  const first = context.load();
  assert.equal(context.load({poll: true}), vm.runInContext("feedLoad.pending", context));
  assert.equal(responses.length, 1);
  reply(0, payload);
  await first;
  await flush();
  assert.equal(counts.render, 1);
  const poll = context.load({poll: true});
  reply(1, payload);
  await poll;
  assert.equal(counts.render, 1, "unchanged poll must not rebuild the feed");
  assert.equal(counts.presence, 1, "presence times still refresh");
  const forced = context.load({force: true});
  reply(2, payload);
  await forced;
  assert.equal(counts.render, 2, "actions force a fresh render");
  const changed = context.load({poll: true});
  reply(3, {...payload, updated_at: "y"});
  await changed;
  assert.equal(counts.render, 3);
});

function cardContext() {
  const context = vm.createContext({
    el, document: {createTextNode: text => ({textContent: text})},
    icon: () => "", childPhoto: () => null, childClass: () => "", authorPhoto: () => null, timeLabel: () => "today",
    calendarOrigin: () => "", itemTitle: item => item.title, richText: text => el("div", "rich-text", text),
    calendarFullDateLabel: () => "", calendarEvidence: () => el("section"), calendarActions: () => [],
    action: (name, label, callback) => { const button = el("button", "action", label); button.addEventListener("click", callback); return button; },
    saveAction: () => el("button", "action", "Save"), shareUrl: () => "", navigate() {}, setNotice() {},
    setPostCollapsed() {}, collapsedPosts: new Set(["post:1"]),
  });
  vm.runInContext(slice("function card(", "function attentionEvents("), context);
  return context;
}

test("a card for a child without a resolved name still renders", () => {
  const context = cardContext();
  const article = context.card({id: "post:2", title: "Hi", child_slugs: ["maja"], children: [], content_type: "Post"});
  assert.equal(article.find(node => node.classes.has("avatar")).textContent, "m");
});

test("a post collapsed in the feed is fully expanded on its detail page", () => {
  const context = cardContext();
  const item = {id: "post:1", title: "Trip", summary: "Body", child_slugs: ["maja"], children: ["Maja"], content_type: "Post"};
  const feedCard = context.card(item);
  assert.equal(feedCard.classes.has("collapsed"), true);
  const detail = context.card(item, {detail: true});
  assert.equal(detail.classes.has("collapsed"), false);
  assert.equal(detail.find(node => node.classes.has("collapse-toggle")), null);
});

test("detail pages are not rebuilt by polls when nothing changed", () => {
  const route = slice("async function route(", 'if (key === "history")');
  assert.match(route, /if \(!changed && signature === detailSignature\) return;/);
  assert.match(route, /original\.open = openOriginals\.has/);
});

async function serviceWorker(online) {
  const listeners = {};
  const store = new Map();
  const cache = {
    put: async (request, response) => { store.set(request.url, response); },
    match: async request => store.get(request.url),
  };
  const network = {ok: true, body: "fresh", clone() { return {body: "fresh"}; }};
  const context = vm.createContext({
    self: {registration: {scope: "https://ha.test/api/hassio_ingress/tok/"}, addEventListener: (name, callback) => { listeners[name] = callback; }},
    location: {origin: "https://ha.test"},
    URL,
    caches: {open: async () => cache},
    fetch: async () => { if (!online) throw new TypeError("offline"); return network; },
  });
  vm.runInContext(readFileSync(path.join(static_, "sw.js"), "utf8"), context);
  store.set("https://ha.test/api/hassio_ingress/tok/app.js", {body: "stale"});
  let responded;
  listeners.fetch({
    request: {method: "GET", mode: "no-cors", url: "https://ha.test/api/hassio_ingress/tok/app.js"},
    respondWith: promise => { responded = promise; },
    waitUntil() {},
  });
  return (await responded).body;
}

test("service worker serves fresh shell files and uses the cache only offline", async () => {
  assert.equal(await serviceWorker(true), "fresh");
  assert.equal(await serviceWorker(false), "stale");
});

// The backend decodes Aula HTML entities, so "&lt;script&gt;" arrives as a literal "<script>".
const HOSTILE = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>';

test("no static script parses data as HTML: innerHTML only ever receives fixed SVG icons", () => {
  const {readdirSync} = require("node:fs");
  const files = readdirSync(static_).filter(name => /\.(js|html)$/.test(name));
  assert.ok(files.includes("app.js") && files.includes("notifications.js"));
  for (const name of files) {
    const text = readFileSync(path.join(static_, name), "utf8");
    assert.doesNotMatch(text, /insertAdjacentHTML|outerHTML|document\.write|srcdoc|createContextualFragment|DOMParser|new Function|\beval\(/, name);
    for (const match of text.matchAll(/\.innerHTML\s*=\s*([^;]+);/g)) {
      const value = match[1].trim();
      assert.match(value, /^(icon\("[a-z]+"\)|icon\([a-z.]+\)|`\$\{icon\("[a-z]+"\)\}<span>[A-Za-z ]+<\/span>`)$/, `${name}: ${value}`);
    }
  }
  const icons = vm.createContext({});
  vm.runInContext(slice("const paths =", "function el("), icons);
  assert.doesNotMatch(icons.icon("more"), /undefined/);
});

test("Aula and AI strings with literal markup render as inert text", () => {
  const created = [];
  const context = cardContext();
  context.el = (tag, cls, text) => { created.push(tag); return el(tag, cls, text); };
  vm.runInContext(slice("function appendInline(", "function chatParagraphs("), context);
  vm.runInContext(slice("const paths =", "function el("), context);
  vm.runInContext(slice("function summaryMarkup(", "function chatParagraphs("), context);
  vm.runInContext(slice("function plainText(", "const state ="), context);
  vm.runInContext(slice("function calendarEvidence(", "function agendaEvent("), context);
  vm.runInContext(slice("function card(", "function attentionEvents("), context);
  const item = {
    id: "post:x", title: HOSTILE, summary: `**${HOSTILE}**\n- ${HOSTILE}`, sender: HOSTILE,
    child_slugs: ["maja"], children: [HOSTILE], content_type: "Post", date: "2026-10-26",
    calendar_event: {origin: "ai", title: HOSTILE, source_title: HOSTILE, source_excerpt: HOSTILE, source_author: HOSTILE},
  };
  const article = context.card(item);
  assert.ok(article.textContent.includes(HOSTILE));
  assert.equal(created.includes("script"), false);
  const htmlSinks = [];
  (function walk(node) {
    if (!(node instanceof Element)) return;
    if (node.html !== undefined) htmlSinks.push(node.html);
    node.children.forEach(walk);
  })(article);
  assert.ok(htmlSinks.length >= 1);
  assert.ok(htmlSinks.every(html => html.startsWith("<svg") && !html.includes("onerror")));
  assert.equal(context.plainText(HOSTILE), HOSTILE);
});

test("data-provided links are restricted to https and internal routes are URL-encoded", () => {
  assert.match(source, /attachment\.kind === "image" && \/\^https:\\\/\\\/\/i\.test\(attachment\.url/);
  assert.match(source, /new URL\(source\.url\)\.protocol === "https:"/);
  for (const match of source.matchAll(/\.href = `#(?:post|thread|event)\/\$\{([^}]+)\}`/g)) {
    assert.match(match[1], /^encodeURIComponent\(/, match[0]);
  }
});
