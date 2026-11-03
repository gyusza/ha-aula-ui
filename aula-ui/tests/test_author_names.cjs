const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const {test} = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../aula_ui/static/app.js"), "utf8");
const start = source.indexOf("function authorName(");
const end = source.indexOf("function appendInline(", start);
const context = vm.createContext({});
vm.runInContext(source.slice(start, end), context);

test("parent names include Aula child metadata without changing the sender", () => {
  const item = {sender: "Erik Jensen", sender_metadata: "Noah, Alma"};
  assert.equal(context.authorName(item), "Erik Jensen (Noah, Alma)");
  assert.equal(item.sender, "Erik Jensen");
});

test("missing and already included metadata do not add empty or duplicate suffixes", () => {
  assert.equal(context.authorName({sender: "Teacher"}), "Teacher");
  assert.equal(context.authorName({sender: "Teacher", sender_metadata: " "}), "Teacher");
  assert.equal(context.authorName({sender: "Erik Jensen (Noah)", sender_metadata: "Noah"}),
    "Erik Jensen (Noah)");
  assert.equal(context.authorName({}), "Unknown");
  assert.equal(context.authorName({}, "Unknown sender"), "Unknown sender");
});
