const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const {test} = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../aula_ui/static/app.js"), "utf8");

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.classes = new Set();
    this.classList = {
      toggle: (name, active) => active ? this.classes.add(name) : this.classes.delete(name),
    };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  set textContent(value) { this.text = value; }
  get textContent() {
    return (this.text || "") + this.children.map(child => child.textContent || "").join("");
  }
}

function childContext() {
  const presence = new Element("section");
  const state = {
    child: "all",
    limit: 60,
    children: [{slug: "noah", name: "Noah"}, {slug: "alma", name: "Alma"}],
    presence: [{child_slug: "noah", name: "Noah", state: "present", status: "Present"}],
  };
  const context = vm.createContext({
    state,
    FEED_PAGE: 20,
    splitCalendar: () => false,
    document: {
      querySelector: selector => selector === "#presence" ? presence : null,
      createElement: tag => new Element(tag),
      createTextNode: text => ({textContent: text}),
    },
    el: (tag, cls, text) => {
      const element = new Element(tag);
      if (cls) cls.split(/\s+/).forEach(name => element.classes.add(name));
      if (text !== undefined) element.textContent = text;
      return element;
    },
    childPhoto: () => null,
    presenceLabel: entry => ({state: entry.state || "unknown", status: entry.status || "Unknown", detail: entry.reason || ""}),
    keyFromLocation: () => "",
    navigate() {},
    filters() {},
    render() {},
    updateTypeSummary() {},
    window: {scrollTo() {}},
  });
  const helperStart = source.indexOf("function filters()");
  const helperEnd = source.indexOf("function typeFilters()", helperStart);
  const cardsStart = source.indexOf("function renderPresence()");
  const cardsEnd = source.indexOf("function aiProgressLabel(", cardsStart);
  vm.runInContext(source.slice(helperStart, helperEnd), context);
  vm.runInContext(source.slice(cardsStart, cardsEnd), context);
  return {context, presence, state};
}

test("child cards independently toggle without hiding other cards", () => {
  const {context, presence, state} = childContext();
  context.renderPresence();
  assert.equal(presence.children.length, 2);
  assert.deepEqual(presence.children.map(card => card.children[0].attributes["aria-pressed"]), ["true", "true"]);
  assert.equal(presence.children[1].textContent.includes("No current presence status"), true);

  presence.children[0].children[0].listeners.click();
  assert.equal(presence.children.length, 2);
  assert.deepEqual(presence.children.map(card => card.children[0].attributes["aria-pressed"]), ["false", "true"]);
  presence.children[0].children[0].listeners.click();
  assert.deepEqual(presence.children.map(card => card.children[0].attributes["aria-pressed"]), ["true", "true"]);
});

test("children support both, either, neither and independently reselecting each", () => {
  const {context, state} = childContext();
  context.toggleChildFilter("noah");
  assert.deepEqual(Array.from(state.child), ["alma"]);
  assert.equal(state.limit, 20);
  context.toggleChildFilter("alma");
  assert.deepEqual(Array.from(state.child), []);
  context.toggleChildFilter("noah");
  assert.deepEqual(Array.from(state.child), ["noah"]);
  context.toggleChildFilter("alma");
  assert.equal(state.child, "all");
});

test("returning to the same list restores scroll without resetting pagination", () => {
  let rendered = 0;
  let restoredY = null;
  const snapshot = {
    key: "saved", tab: "saved", scrollY: 742, limit: 90, search: "trip", searchValue: "Trip",
    child: "noah", hiddenTypes: ["Post"], calendarHiddenTypes: [], futureMode: "all",
    calendarFocusDate: "2026-10-26",
  };
  const search = {value: ""};
  const clear = {hidden: true};
  const context = vm.createContext({
    history: {state: {aulaListReturn: snapshot}},
    state: {limit: 30, search: "", child: "all"},
    selectTab() {},
    document: {querySelector: selector => selector === "#search" ? search : clear},
    render: () => { rendered += 1; },
    requestAnimationFrame: callback => callback(),
    window: {scrollTo: (x, y) => { restoredY = y; }},
  });
  const start = source.indexOf("function renderListRoute(");
  const end = source.indexOf("async function route(", start);
  assert.ok(start >= 0 && end > start);
  vm.runInContext(source.slice(start, end), context);
  context.renderListRoute("saved", true);
  assert.equal(rendered, 1);
  assert.equal(restoredY, 742);
  assert.equal(context.state.limit, 90);
  assert.equal(context.state.search, "trip");
  assert.equal(context.state.child, "noah");
  assert.equal(search.value, "Trip");
  assert.equal(clear.hidden, false);
  restoredY = null;
  context.renderListRoute("calendar", true);
  assert.equal(restoredY, null);
  context.renderListRoute("saved");
  assert.equal(restoredY, null);
});

test("only an explicit item Back marker for the current history entry uses browser back", () => {
  let click;
  let backCalls = 0;
  const navigations = [];
  const context = vm.createContext({
    document: {addEventListener: (name, callback) => { click = callback; }},
    window: {addEventListener() {}},
    history: {state: {aulaDetailKey: "post/one"}, back: () => { backCalls += 1; }},
    keyFromLocation: () => "post/one",
    navigate: key => navigations.push(key),
  });
  const start = source.indexOf('document.addEventListener("click", event => {');
  const end = source.indexOf("const conversationLimits", start);
  vm.runInContext(source.slice(start, end), context);
  const invoke = (href, dataset = {}) => click({
    target: {closest: () => ({dataset, getAttribute: () => href})},
    preventDefault() {},
  });
  invoke("#calendar-settings");
  invoke("#post/source");
  invoke("#profiles");
  assert.deepEqual(navigations, ["calendar-settings", "post/source", "profiles"]);
  assert.equal(backCalls, 0);
  invoke("#", {detailBack: "true"});
  assert.equal(backCalls, 1);
  context.history.state = {aulaDetailKey: "post/other"};
  invoke("#", {detailBack: "true"});
  assert.equal(backCalls, 1);
  assert.equal(navigations.at(-1), "");
});
