const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "../aula_ui/static/theme.js"), "utf8");

function theme(saved, light = false, blocked = false) {
  const events = {};
  const meta = {};
  const document = {
    addEventListener: (name, handler, options) => { events[name] = {handler, options}; },
    documentElement: {dataset: {}},
    querySelector: () => ({setAttribute: (key, value) => { meta[key] = value; }}),
  };
  const system = {matches: light, addEventListener: (name, handler) => { events.system = handler; }};
  const context = {
    document, console: {warn: () => {}},
    matchMedia: () => system,
    localStorage: {
      getItem: () => { if (blocked) throw new Error("blocked"); return saved; },
      setItem: (key, value) => { if (blocked) throw new Error("blocked"); saved = value; },
    },
    window: {addEventListener: (name, handler) => { events[name] = handler; }},
  };
  vm.runInNewContext(source, context);
  return {api: context.window.AulaTheme, document, system, events, meta};
}

test("appearance follows system by default and explicitly overrides either scheme", () => {
  const value = theme(null, true);
  assert.equal(value.api.get(), "system");
  assert.equal(value.document.documentElement.dataset.theme, "light");
  assert.equal(value.api.set("dark"), true);
  assert.equal(value.document.documentElement.dataset.theme, "dark");
  assert.equal(value.meta.content, "#0e1113");
  value.system.matches = false;
  value.events.system();
  assert.equal(value.document.documentElement.dataset.theme, "dark");
  value.api.set("light");
  assert.equal(value.document.documentElement.dataset.theme, "light");
  value.events.storage({key: "aula-theme", newValue: "dark"});
  assert.equal(value.api.get(), "dark");
  assert.throws(() => value.api.set("invalid"), /Invalid appearance/);
});

test("appearance restores saved preference and reports persistence errors", () => {
  assert.equal(theme("light", false).document.documentElement.dataset.theme, "light");
  const value = theme(null, false, true);
  assert.equal(value.api.set("light"), false);
  assert.equal(value.document.documentElement.dataset.theme, "light");
});

test("page pinch gestures are cancelled with non-passive listeners", () => {
  const value = theme(null);
  for (const name of ["gesturestart", "gesturechange"]) {
    let prevented = false;
    value.events[name].handler({preventDefault: () => { prevented = true; }});
    assert.equal(prevented, true);
    assert.equal(value.events[name].options.passive, false);
  }
  assert.equal(value.events.touchmove, undefined);
});
