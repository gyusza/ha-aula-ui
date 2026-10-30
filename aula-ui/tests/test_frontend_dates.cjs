const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const {test} = require("node:test");
const vm = require("node:vm");

process.env.TZ = "Europe/Copenhagen";
const source = readFileSync(path.join(__dirname, "../aula_ui/static/app.js"), "utf8");
const context = vm.createContext({
  calendarTypes: new Set(["Calendar", "Weekplan", "Homework"]),
  calendarRangeDays: 14,
  calendarPastDays: 7,
});
const start = source.indexOf("function presenceLabel(");
const end = source.indexOf("function timeLabel(", start);
assert.ok(start >= 0 && end > start);
vm.runInContext(source.slice(start, end), context);
vm.runInContext(source.slice(source.indexOf("function chatParagraphs("), source.indexOf("function richText(")), context);
vm.runInContext(source.slice(source.indexOf("function summaryMarkup("), source.indexOf("function chatParagraphs(")), context);

test("HTML summary formatting becomes safe Markdown, never active HTML", () => {
  const result = context.summaryMarkup('Activities: <ul class="items"><li>Cars</li><li><b>Lego</b></li></ul><p><em>Tomorrow</em></p><ol><li>Lunch</li><li>Boots</li></ol>');
  assert.match(result, /- Cars\n- \*\*Lego\*\*/);
  assert.match(result, /\*Tomorrow\*/);
  assert.match(result, /1\. Lunch\n2\. Boots/);
  assert.equal(context.summaryMarkup("Keep **Markdown** and 2 < 3."), "Keep **Markdown** and 2 < 3.");
  assert.equal(context.summaryMarkup('<img src=x onerror=alert(1)><script>alert(1)</script>'),
    '<img src=x onerror=alert(1)><script>alert(1)</script>');
});

test("long chat messages gain readable paragraphs without losing words", () => {
  const input = Array.from({length: 16}, (_, n) => `Sentence ${n} keeps every important detail about the children's day.`).join(" ");
  const result = context.chatParagraphs(input);
  assert.ok(result.split("\n\n").length >= 3);
  assert.equal(result.replace(/\s+/g, " "), input);
  const structured = "## Next week\n\n- Bring lunch\n- Arrive at 09:30\n\nThank you.";
  assert.equal(context.chatParagraphs(structured), structured);
  assert.equal(context.chatParagraphs("Short reply."), "Short reply.");
});

test("presence labels show current times and expire cached status without inventing dates", () => {
  const now = Date.parse("2026-10-26T08:00:00Z");
  const entry = {
    state: "present", status: "Present", timezone: "Europe/Copenhagen",
    valid_until: "2026-10-26T08:30:00Z", check_in_time: "08:30", check_out_time: "",
  };
  assert.deepEqual({...context.presenceLabel(entry, now)}, {
    state: "present", status: "Present", detail: "Arrived 08:30",
  });
  assert.equal(context.presenceLabel({...entry, check_in_time: ""}, now).detail, "");
  assert.equal(context.presenceLabel({...entry, check_in_time: "2026-10-26T07:30:00Z"}, now).detail, "Arrived 08:30");
  assert.equal(context.presenceLabel({...entry, check_in_time: "invalid"}, now).detail, "");
  assert.deepEqual({...context.presenceLabel(entry, now + 1800000)}, {
    state: "unknown", status: "Unknown", detail: "Status out of date",
  });
  assert.equal(context.presenceLabel({...entry, state: "unknown", reason: "Refresh failed"}, now).detail, "Refresh failed");
  assert.equal(context.presenceLabel({...entry, valid_until: null}, now).status, "Unknown");
});

test("AI progress labels show counts and honest estimates or retry state", () => {
  assert.equal(context.aiProgressLabel(null), null);
  assert.equal(context.aiProgressLabel({status: "disabled", total: 4}), null);
  assert.equal(context.aiProgressLabel({status: "idle", total: 0}), null);
  const progress = {completed: 5, total: 20, status: "processing", eta_seconds: null};
  assert.deepEqual({...context.aiProgressLabel(progress)}, {count: "Pending tasks 5/20", detail: "Estimating time…"});
  assert.equal(context.aiProgressLabel({...progress, eta_seconds: 90}).detail, "~2 min left");
  assert.equal(context.aiProgressLabel({...progress, eta_seconds: 7200}).detail, "~2h left");
  assert.equal(context.aiProgressLabel({...progress, status: "retrying", retry_seconds: 61}).detail, "Retry in ~2 min");
  assert.equal(context.aiProgressLabel({...progress, status: "blocked"}).detail, "Waiting for source");
  assert.equal(context.aiProgressLabel({...progress, status: "idle"}), null);
  assert.equal(context.aiProgressLabel({...progress, pending: 0}), null);
  assert.equal(context.aiProgressLabel({...progress, completed: 20}), null);
});

test("presence shows recorded nap start and completed duration without last checked", () => {
  const now = Date.parse("2026-10-26T14:00:00Z");
  const entry = {
    state: "present", status: "Napping", timezone: "Europe/Copenhagen",
    valid_until: "2026-10-26T14:30:00Z",
    sleep_intervals: [{start: "2026-10-26T12:05:00+01:00", end: null}],
  };
  assert.equal(context.presenceLabel(entry, now).detail, "Napping since 12:05");
  const completed = {...entry, status: "Present", sleep_intervals: [
    {start: "2026-10-26T12:05:00+01:00", end: "2026-10-26T13:25:00+01:00"},
  ]};
  assert.equal(context.presenceLabel(completed, now).detail, "Slept 1h 20m (12:05–13:25)");
  assert.equal(context.presenceLabel({...entry, sleep_intervals: []}, now).detail, "Nap start not recorded");
  assert.equal(context.presenceLabel({...entry, status: "Present"}, now).detail,
    "Nap started 12:05 · wake-up not recorded");
  assert.equal(context.presenceLabel(entry, now + 3600000).status, "Unknown");
  assert.ok(!source.includes("Last checked"));
});
test("calendar pages recognise dated content without moving source posts", () => {
  for (const content_type of ["Calendar", "Weekplan", "Homework"]) {
    assert.equal(context.isCalendarItem({content_type}), true);
  }
  assert.equal(context.isCalendarItem({calendar_event: {start: ""}}), true);
  assert.equal(context.isCalendarItem({content_type: "Post", recognized_events: [{}]}), false);
  assert.equal(context.isCalendarItem({content_type: "Message"}), false);
  assert.equal(context.isCalendarItem({content_type: "Presence"}), false);
});

test("all-day future events compare device-local calendar dates, not UTC midnight", () => {
  const now = new Date("2026-10-09T00:30:00+02:00");
  for (const [start, expected] of [["2026-10-08", false], ["2026-10-09", false], ["2026-10-10", true]]) {
    assert.equal(context.isFutureEvent({calendar_event: {start, all_day: true}}, now), expected);
  }
  assert.equal(context.isFutureEvent({
    calendar_event: {start: "2026-10-08", end: "2026-10-10", all_day: true},
  }, now), true);
});

test("timed events use absolute starts and handle timezone offsets", () => {
  const now = new Date("2026-10-09T12:00:00+02:00");
  for (const [start, expected] of [
    ["2026-10-09T12:00:00+02:00", false],
    ["2026-10-09T12:00:01+02:00", true],
    ["2026-10-09T13:00:00+05:00", false],
    ["2026-10-09T11:00:00Z", true],
  ]) {
    assert.equal(context.isFutureEvent({calendar_event: {start}}, now), expected);
    assert.equal(context.isFutureEvent({content_type: "Weekplan", date: start}, now), expected);
  }
});

test("undated or invalid recommendations are not future based on their source date", () => {
  const now = new Date("2026-10-09T12:00:00+02:00");
  for (const start of [undefined, "", "unknown", "2099-99-99", "2099-02-30", "2099-11-31", "2099-10-09T99:00:00Z"]) {
    assert.equal(context.isFutureEvent({date: "2099-12-31", calendar_event: {start, review_required: true}}, now), false);
  }
});

test("translated titles are preferred without replacing original titles", () => {
  assert.equal(context.itemTitle({title: "Original title", translated_title: "English title"}), "English title");
  assert.equal(context.itemTitle({title: "Original title"}), "Original title");
  assert.equal(context.itemTitle({}), "Untitled");
});

test("calendar origin labels distinguish native, pending AI, and approved AI events", () => {
  assert.equal(context.calendarOrigin({origin: "aula", review_required: false}), "Aula calendar");
  assert.equal(context.calendarOrigin({origin: "ai", review_required: true}), "AI suggestion");
  assert.equal(context.calendarOrigin({origin: "ai", review_required: false}), "AI approved");
});

test("lesson filter uses structured metadata without guessing from a title or room", () => {
  assert.equal(context.calendarFilterType({
    content_type: "Calendar", title: "BHK", location: "H021",
  }), "Calendar");
  for (const content_type of ["Calendar", "Weekplan"]) {
    assert.equal(context.calendarFilterType({
      content_type, calendar_event: {calendar_kind: "lesson"},
    }), "Lessons / classroom bookings");
  }
  assert.equal(context.calendarOrigin({origin: "aula", calendar_kind: "lesson"}), "Aula lesson");
});

test("event pickers show the child's timezone rather than the device timezone", () => {
  assert.equal(context.eventInputValue("2026-10-26T07:00:00Z", false, "Europe/Copenhagen"),
    "2026-10-26T08:00:00");
  assert.equal(context.eventInputValue("2026-07-26T07:00:00Z", false, "Europe/Copenhagen"),
    "2026-07-26T09:00:00");
  assert.equal(context.eventInputValue("2026-10-26T07:00:00Z", false, "America/New_York"),
    "2026-10-26T03:00:00");
  assert.equal(context.eventInputValue("2026-10-26", true, "America/New_York"), "2026-10-26");
  assert.equal(context.eventInputValue("", false, "Europe/Copenhagen"), "");
  assert.equal(context.eventInputValue("invalid", false, "Europe/Copenhagen"), "");
});

test("all-day pickers accept inclusive last days and preserve exclusive API ends", () => {
  assert.deepEqual({...context.eventInputDates("2026-10-26", "2026-10-26", true, {}, "Europe/Copenhagen")},
    {start: "2026-10-26", end: "2026-10-27"});
  assert.deepEqual({...context.eventInputDates("2026-12-31", "2027-01-02", true, {}, "Europe/Copenhagen")},
    {start: "2026-12-31", end: "2027-01-03"});
  assert.equal(context.eventInputDates("2026-10-26", "", true, {}, "Europe/Copenhagen").end, "");
});

test("unchanged picker values retain exact instants, including repeated DST hours", () => {
  const zone = "Europe/Copenhagen";
  for (const start of ["2026-10-25T00:30:00Z", "2026-10-25T01:30:00Z"]) {
    const event = {start, end: "", all_day: false};
    const value = context.eventInputValue(start, false, zone);
    assert.equal(context.eventInputDates(value, "", false, event, zone).start, start);
    assert.equal(context.eventInputDates(value.slice(0, 16), "", false, event, zone).start, start);
    assert.equal(context.eventInputDates("2026-10-26T10:15", "", false, event, zone).start,
      "2026-10-26T10:15");
  }
});

test("agenda date keys preserve all-day calendar dates and convert timed events locally", () => {
  assert.equal(context.eventDateKey("2026-10-09"), "2026-10-09");
  assert.equal(context.eventDateKey("2026-02-31"), null);
  assert.equal(context.eventDateKey("2026-10-09T23:30:00Z"), "2026-10-10");
});

test("all-day agenda labels treat source end as exclusive", () => {
  assert.equal(context.calendarTimeLabel({
    start: "2026-10-09", end: "2026-10-10", all_day: true,
  }), "All day");
  assert.equal(context.calendarTimeLabel({
    start: "2026-10-09", end: "2026-10-12", all_day: true,
  }), "All day · through Oct 11");
  assert.equal(context.calendarFullDateLabel({
    start: "2026-10-09", end: "2026-10-12", all_day: true,
  }), "Oct 9, 2026 – Oct 11, 2026 · All day");
  assert.equal(context.calendarFullDateLabel({
    start: "2026-10-09", end: "2026-10-10", all_day: true,
  }), "Oct 9, 2026 · All day");
});

test("ongoing multi-day events appear under today once, without shifting their end date", () => {
  const now = new Date("2026-10-09T12:00:00+02:00");
  const event = {start: "2026-10-08", end: "2026-10-10", all_day: true};
  assert.equal(context.calendarEventDay(event, now), "2026-10-09");
  assert.equal(context.calendarTimeLabel(event), "All day · through Oct 9");
  assert.equal(context.calendarTimeLabel(event, {}, "2026-10-09"), "All day · ongoing");
});

test("agenda ranges sort all dates chronologically, including past dates", () => {
  assert.deepEqual({...context.calendarRange("2026-10-09")},
    {start: "2026-10-02", end: "2026-10-15"});
  const items = [
    {id: "older", date: "2026-10-07"},
    {id: "tomorrow", date: "2026-10-10"},
    {id: "today", date: "2026-10-09"},
    {id: "yesterday", date: "2026-10-08"},
  ];
  assert.deepEqual(Array.from(context.agendaSort(items), item => item.id),
    ["older", "yesterday", "today", "tomorrow"]);
});

test("agenda sorts all-day entries before timed events and times by instant", () => {
  const items = [
    {id: "late", calendar_event: {start: "2026-10-28T12:00:00+01:00"}},
    {id: "all-day", calendar_event: {start: "2026-10-28", all_day: true}},
    {id: "early", calendar_event: {start: "2026-10-28T10:15:00+01:00"}},
    {id: "invalid", calendar_event: {start: "invalid"}},
  ];
  assert.deepEqual(Array.from(context.agendaSort(items), item => item.id),
    ["all-day", "early", "late", "invalid"]);
  assert.equal(items[0].id, "late");
});

test("thread translation helper gives explicit translated, pending, and error states", () => {
  assert.deepEqual({...context.threadTranslationStatus({translated_text: "Translated", is_translated: true})},
    {text: "Translated", translated: true, label: "English translation"});
  assert.deepEqual({...context.threadTranslationStatus({translation_pending: true})},
    {text: "Translation pending…", translated: false});
  assert.deepEqual({...context.threadTranslationStatus({translation_error: "Provider unavailable"})},
    {text: "Translation unavailable: Provider unavailable", translated: false});
  assert.deepEqual({...context.threadTranslationStatus({}, {translation_pending: true})},
    {text: "Translation pending…", translated: false});
  assert.deepEqual({...context.threadTranslationStatus({})},
    {text: "Translation unavailable.", translated: false});
});

test("English originals rely on explicit backend status and never require a provider", () => {
  assert.deepEqual({...context.threadTranslationStatus({
    original_text: "Please bring lunch.", is_translated: false,
  }, {translation_error: "No provider"})},
  {text: "Please bring lunch.", translated: false, label: "English original"});
  assert.deepEqual({...context.threadTranslationStatus({
    original_text: "Original", translated_text: "Please bring lunch.", is_translated: false,
  })}, {text: "Please bring lunch.", translated: false, label: "English original"});
  assert.equal(context.threadTranslationStatus({translated_text: "Unclassified"}).label, "Message");
  assert.deepEqual({...context.threadTranslationStatus({
    original_text: "The office closes at four.", is_translated: null, translation_status: "original",
  }, {translation_error: "No provider"})},
  {text: "The office closes at four.", translated: false, label: "Original message"});
  assert.deepEqual({...context.threadTranslationStatus({translation_status: "pending"})},
    {text: "Translation pending…", translated: false});
});

test("chat messages sort latest first without mutating backend arrays", () => {
  const messages = [
    {id: "latest", date: "2026-10-25T10:10:00Z"},
    {id: "unknown", date: "invalid"},
    {id: "earliest", date: "2026-10-25T10:00:00Z"},
    {id: "middle", date: "2026-10-25T12:05:00+02:00"},
  ];
  assert.deepEqual(Array.from(context.newestMessages(messages), message => message.id),
    ["latest", "middle", "earliest", "unknown"]);
  assert.equal(messages[0].id, "latest");
});

test("only consecutive messages from the same known author within 30 minutes group", () => {
  const first = {sender: "Teacher", date: "2026-10-25T10:10:00Z"};
  assert.equal(context.sameMessageGroup(first, {sender: "Teacher", date: "2026-10-25T10:05:00Z"}), true);
  assert.equal(context.sameMessageGroup(first, {sender: "Parent", date: "2026-10-25T10:05:00Z"}), false);
  assert.equal(context.sameMessageGroup(first, {sender: "Teacher", date: "2026-10-26T10:05:00Z"}), false);
  assert.equal(context.sameMessageGroup(null, first), false);
  assert.equal(context.sameMessageGroup({}, {}), false);
});
