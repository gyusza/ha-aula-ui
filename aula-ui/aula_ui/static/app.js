"use strict";

const paths = {
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 6a8 8 0 0 1 13 4M18 18A8 8 0 0 1 5 14"/>',
  home: '<path d="m3 10 9-7 9 7v11h-6v-7H9v7H3z"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 11h18M8 15h2M14 15h2"/>',
  saved: '<path d="M6 3h12v18l-6-4-6 4z"/>',
  read: '<path d="M4 4h16v13H8l-4 4z"/>',
  share: '<path d="m14 3 7 7-7 7v-4c-6 0-9 2-11 6 0-8 4-12 11-12z"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
};
function icon(name) {
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name]}</svg>`;
}
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function childClass(slug) {
  return state.children.find(child => child.slug === slug)?.institution_type === "daycare" ? "daycare" : "";
}
function childPhoto(slug, cls) {
  const version = state.children.find(child => child.slug === slug)?.avatar;
  if (!version) return null;
  const img = el("img", cls);
  img.src = `api/avatars/${encodeURIComponent(slug)}?v=${encodeURIComponent(version)}`;
  img.alt = "";
  img.loading = "lazy";
  img.decoding = "async";
  img.addEventListener("error", () => img.remove());
  return img;
}
function authorPhoto(name, cls) {
  const entry = name && state.authors[String(name).trim()];
  if (!entry) return null;
  const img = el("img", cls);
  img.src = `api/avatars/${encodeURIComponent(entry.key)}?v=${encodeURIComponent(entry.v)}`;
  img.alt = "";
  img.loading = "lazy";
  img.decoding = "async";
  img.addEventListener("error", () => img.remove());
  return img;
}
function appendInline(parent, text) {
  text.split(/(\*\*[^*\n]+\*\*|\*[^*\n]+\*)/g).forEach(part => {
    if (part.startsWith("**") && part.endsWith("**")) parent.append(el("strong", "", part.slice(2, -2)));
    else if (part.startsWith("*") && part.endsWith("*")) parent.append(el("em", "", part.slice(1, -1)));
    else parent.append(document.createTextNode(part));
  });
}
function summaryMarkup(text) {
  const lists = [];
  return String(text).replace(/<\/?(?:ul|ol|li|p|div|br|strong|b|em|i|h[1-6])\b[^>]*>/gi, tag => {
    const name = tag.match(/^<\/?([a-z0-9]+)/i)[1].toLowerCase();
    const closing = tag.startsWith("</");
    if (name === "ul" || name === "ol") {
      if (closing) lists.pop();
      else lists.push({name, count: 0});
      return "\n\n";
    }
    if (name === "li") {
      if (closing) return "";
      const list = lists.at(-1);
      return `\n${list?.name === "ol" ? `${++list.count}.` : "-"} `;
    }
    if (name === "strong" || name === "b") return "**";
    if (name === "em" || name === "i") return "*";
    if (/^h[1-6]$/.test(name)) return closing ? "\n\n" : "\n\n### ";
    return name === "br" ? "\n" : "\n\n";
  });
}
function chatParagraphs(text) {
  const value = String(text).replace(/\r\n?/g, "\n");
  if (value.length < 600 || typeof Intl.Segmenter !== "function") return value;
  const segmenter = new Intl.Segmenter("en", {granularity: "sentence"});
  return value.split("\n").map(line => {
    if (line.length < 600 || /^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s)/.test(line)) return line;
    const paragraphs = [];
    let paragraph = "";
    for (const {segment} of segmenter.segment(line)) {
      if (paragraph.length >= 300) {
        paragraphs.push(paragraph.trimEnd());
        paragraph = "";
      }
      paragraph += segment;
    }
    if (paragraph) paragraphs.push(paragraph.trimEnd());
    return paragraphs.join("\n\n");
  }).join("\n");
}
function plainText(text) {
  return summaryMarkup(text || "").replace(/^\s*#{1,6}\s+/gm, "").replace(/\*\*([^*\n]+)\*\*|\*([^*\n]+)\*/g, "$1$2")
    .replace(/\s+/g, " ").trim();
}
function richText(text, cls = "") {
  const container = el("div", `rich-text ${cls}`);
  let block = null;
  let list = null;
  summaryMarkup(text).replace(/\r\n?/g, "\n").split("\n").forEach(line => {
    if (!line.trim()) { block = null; list = null; return; }
    const heading = line.match(/^#{1,6}\s+(.+)$/);
    const bullet = line.match(/^\s*(?:[-*+]\s+|(\d+)[.)]\s+)(.+)$/);
    if (heading) {
      const node = el("h3");
      appendInline(node, heading[1]);
      container.append(node);
      block = null; list = null;
    } else if (bullet) {
      const tag = bullet[1] ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== tag) {
        list = el(tag);
        if (tag === "ol") list.start = Number(bullet[1]);
        container.append(list);
      }
      const node = el("li");
      appendInline(node, bullet[2]);
      list.append(node);
      block = null;
    } else {
      list = null;
      if (!block) { block = el("p"); container.append(block); }
      else block.append(el("br"));
      appendInline(block, line);
    }
  });
  return container;
}
const FEED_PAGE = 20;
const CALENDAR_PAGE = 30;
const state = {items: [], presence: [], children: [], authors: {}, child: "all", tab: "home", search: "", limit: FEED_PAGE, loaded: false};

// Deep links: paths (/post/<id>) when the server injects an absolute <base>, otherwise ?open=post/<id>.
const baseElement = document.querySelector("base");
const pathLinks = Boolean(baseElement && (baseElement.getAttribute("href") || "").startsWith("/"));
const appBase = new URL("./", pathLinks ? baseElement.href : location.href);
const pathRoutes = [["post/", "post/"], ["thread/", "thread/"], ["child/", "child/"], ["calendar-view", "calendar"]];
function keyFromLocation() {
  if (location.hash.length > 1) return location.hash.slice(1);
  let rel = "";
  if (pathLinks && location.pathname.startsWith(appBase.pathname)) rel = location.pathname.slice(appBase.pathname.length);
  if (!rel) rel = new URLSearchParams(location.search).get("open") || "";
  rel = rel.replace(/\/+$/, "");
  for (const [prefix, key] of pathRoutes) {
    if (prefix.endsWith("/") ? rel.startsWith(prefix) && rel.length > prefix.length : rel === prefix) {
      return prefix.endsWith("/") ? rel : key;
    }
  }
  return "";
}
function urlForKey(key) {
  if (!key) return appBase.href;
  const route = key === "calendar" ? "calendar-view" : key;
  if (/^(post|thread|child)\//.test(route) || route === "calendar-view") {
    return pathLinks ? appBase.href + route : `${appBase.href}?open=${route}`;
  }
  return `${appBase.href}#${key}`;
}
function navigate(key, replace = false) {
  const url = urlForKey(key);
  if (url !== location.href) {
    if (!replace && !document.querySelector("#timeline").hidden) {
      history.replaceState({
        ...history.state,
        aulaListReturn: {
          key: keyFromLocation(), tab: state.tab, scrollY: window.scrollY,
          limit: state.limit, child: state.child, search: state.search,
          searchValue: document.querySelector("#search").value,
          hiddenTypes: [...hiddenTypes], calendarHiddenTypes: [...calendarHiddenTypes],
          futureMode, calendarFocusDate,
        },
      }, "", location.href);
    }
    history[replace ? "replaceState" : "pushState"](replace ? history.state : {aulaDetailKey: key}, "", url);
  }
  route();
}
function shareUrl(item) {
  return urlForKey(`${item.thread_messages?.length ? "thread" : "post"}/${encodeURIComponent(item.id)}`);
}
document.addEventListener("click", event => {
  const link = event.target.closest?.("a[href^='#']");
  if (!link || event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey) return;
  if (link.dataset.detailBack === "true" && history.state?.aulaDetailKey === keyFromLocation()) {
    event.preventDefault();
    history.back();
    return;
  }
  event.preventDefault();
  navigate(link.getAttribute("href").slice(1));
});
window.addEventListener("popstate", () => { route(true); });
const conversationLimits = new Map();
const contentTypes = ["Post", "Message", "Album", "Calendar", "Weekplan", "Homework"];
const calendarTypes = new Set(["Calendar", "Weekplan", "Homework"]);
let calendarHiddenTypes = new Set();
let futureMode = "all";
let calendarFocusDate = localDateKey(new Date());
const calendarRangeDays = 14;
// Wide screens (iPad landscape, desktop) show the calendar beside the feed instead of as a tab.
const wideCalendar = window.matchMedia("(min-width: 900px)");
let sideLimit = 30;
function splitCalendar() { return wideCalendar.matches; }
let calendarCollapsed = false;
try { calendarCollapsed = localStorage.getItem("aula-calendar-collapsed") === "true"; }
catch (error) { console.warn("Calendar sidebar preference unavailable", error); }
function toggleCalendarSidebar() {
  calendarCollapsed = !calendarCollapsed;
  try { localStorage.setItem("aula-calendar-collapsed", String(calendarCollapsed)); }
  catch (error) { setNotice("Calendar visibility applies for this visit but could not be saved."); }
  render();
  document.querySelector("#calendar-toggle").focus();
}
document.querySelector("#calendar-toggle").addEventListener("click", toggleCalendarSidebar);
document.querySelector("#calendar-hide").addEventListener("click", toggleCalendarSidebar);
document.querySelector("#timeline").before(document.querySelector("#calendar-side"));
const calendarPastDays = 7;
try {
  const stored = JSON.parse(localStorage.getItem("aula-calendar-hidden-types") || "[]");
  if (!Array.isArray(stored) || !stored.every(type => typeof type === "string")) throw new Error("Invalid calendar filters");
  calendarHiddenTypes = new Set(stored);
  const preference = localStorage.getItem("aula-future-events");
  if (["all", "hide", "only"].includes(preference)) futureMode = preference;
} catch (error) { console.warn("Calendar filters unavailable", error); }
let hiddenTypes = new Set();
try {
  const stored = localStorage.getItem("aula-home-hidden-types") ?? localStorage.getItem("aula-hidden-types");
  if (stored !== null) {
    const parsed = JSON.parse(stored);
    if (!Array.isArray(parsed) || !parsed.every(type => typeof type === "string")) {
      throw new Error("Invalid content-type preferences");
    }
    hiddenTypes = new Set(parsed);
    // A legacy Calendar-only view should not leave the new Home page empty.
    if (localStorage.getItem("aula-home-hidden-types") === null &&
        ["Post", "Message"].every(type => hiddenTypes.has(type))) {
      hiddenTypes = new Set();
    }
    hiddenTypes.delete("Presence");
    localStorage.setItem("aula-home-hidden-types", JSON.stringify([...hiddenTypes]));
  }
} catch (error) { console.warn("Content-type preferences unavailable", error); }
let saved = new Set();
try { saved = new Set(JSON.parse(localStorage.getItem("aula-saved") || "[]")); }
catch (error) { console.warn("Saved posts unavailable", error); }
let collapsedPosts = new Set();
try {
  const stored = JSON.parse(localStorage.getItem("aula-collapsed-posts") || "[]");
  if (Array.isArray(stored)) collapsedPosts = new Set(stored.filter(id => typeof id === "string"));
} catch (error) { console.warn("Collapsed posts unavailable", error); }
function setPostCollapsed(article, id, collapsed) {
  article.classList.toggle("collapsed", collapsed);
  const toggle = article.querySelector(".collapse-toggle");
  toggle.setAttribute("aria-expanded", String(!collapsed));
  toggle.textContent = collapsed ? "+" : "–";
  collapsedPosts.delete(id);
  if (collapsed) collapsedPosts.add(id);
  // Keep the newest 1000 so long-term use cannot grow storage without bound.
  const stored = [...collapsedPosts].slice(-1000);
  collapsedPosts = new Set(stored);
  try { localStorage.setItem("aula-collapsed-posts", JSON.stringify(stored)); }
  catch (error) {
    console.warn("Could not persist collapsed posts", error);
    setNotice("Collapsed posts apply for this visit, but could not be saved on this device.");
  }
}
document.querySelector("#search-icon").innerHTML = icon("search");
document.querySelectorAll("[data-tab]").forEach(button => {
  button.querySelector(".nav-icon").innerHTML = icon(button.dataset.tab);
  button.addEventListener("click", () => {
    navigate(button.dataset.tab === "home" ? "" : button.dataset.tab === "calendar" ? "calendar" : button.dataset.tab);
  });
});
function selectTab(tab) {
  const changed = state.tab !== tab;
  state.tab = tab;
  activeRoute = "";
  routeGeneration++;
  document.querySelector("#page").hidden = true;
  document.querySelector("#timeline").hidden = false;
  if (changed) {
    state.limit = FEED_PAGE;
    window.scrollTo(0, 0);
  }
  document.querySelectorAll("[data-tab]").forEach(button => {
    const active = button.dataset.tab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active);
  });
  document.querySelector("#feed-title").textContent = {
    home: "Feed", calendar: "Calendar", saved: "Saved",
  }[tab];
  document.querySelector("#agenda-controls").hidden = tab !== "calendar" && !splitCalendar();
  document.querySelector("#future-filters").hidden = tab !== "calendar" && !splitCalendar();
  document.querySelector("#future-events").value = futureMode;
  filters();
  typeFilters();
}
function placeCalendarControls() {
  const controls = document.querySelector("#agenda-controls");
  const future = document.querySelector("#future-filters");
  if (splitCalendar()) {
    document.querySelector("#side-controls").append(controls);
    document.querySelector("#side-future").append(future);
  } else {
    document.querySelector("#timeline .type-filters").before(controls);
    document.querySelector("#timeline .type-filters summary").after(future);
  }
}
placeCalendarControls();
wideCalendar.addEventListener("change", () => {
  placeCalendarControls();
  if (!document.querySelector("#timeline").hidden) render();
});
document.querySelector("#future-events").addEventListener("change", event => {
  futureMode = event.target.value;
  try { localStorage.setItem("aula-future-events", futureMode); }
  catch (error) { setNotice("Future-event filter applies for this visit but could not be saved."); }
  state.limit = FEED_PAGE;
  sideLimit = 30;
  updateTypeSummary();
  render();
});
document.querySelector("#agenda-date").addEventListener("change", event => {
  if (!dateFromKey(event.target.value)) return;
  calendarFocusDate = event.target.value;
  state.limit = FEED_PAGE;
  sideLimit = 30;
  render();
});
document.querySelector("#agenda-today").addEventListener("click", () => {
  calendarFocusDate = localDateKey(new Date());
  state.limit = FEED_PAGE;
  sideLimit = 30;
  render();
});
document.querySelector("#agenda-previous").addEventListener("click", () => {
  calendarFocusDate = shiftDateKey(calendarFocusDate, -calendarRangeDays);
  state.limit = FEED_PAGE;
  sideLimit = 30;
  render();
});
document.querySelector("#agenda-next").addEventListener("click", () => {
  calendarFocusDate = shiftDateKey(calendarFocusDate, calendarRangeDays);
  state.limit = FEED_PAGE;
  sideLimit = 30;
  render();
});
document.querySelector("#search").addEventListener("input", event => {
  state.search = event.target.value.toLocaleLowerCase();
  document.querySelector("#search-clear").hidden = !event.target.value;
  state.limit = FEED_PAGE;
  render();
  window.scrollTo(0, 0);
});
document.querySelector("#search-clear").addEventListener("click", () => {
  const search = document.querySelector("#search");
  search.value = "";
  search.dispatchEvent(new Event("input", {bubbles: true}));
  search.focus({preventScroll: true});
});
function setNotice(text) {
  const notice = document.querySelector("#notice");
  notice.textContent = text;
  notice.hidden = !text;
}
function filters() {
  renderPresence();
  updateTypeSummary();
}
function childIsSelected(slug) {
  return state.child === "all" || (Array.isArray(state.child) ? state.child.includes(slug) : state.child === slug);
}
function toggleChildFilter(slug) {
  const selected = state.children.filter(child => childIsSelected(child.slug)).map(child => child.slug);
  state.child = selected.includes(slug) ? selected.filter(value => value !== slug) : [...selected, slug];
  if (state.children.every(child => state.child.includes(child.slug))) state.child = "all";
  state.limit = FEED_PAGE;
  if (keyFromLocation().startsWith("child/")) {
    navigate("", true);
    return;
  }
  filters();
  render();
  window.scrollTo(0, 0);
}
function typeFilters() {
  buildTypeFilters(state.tab, document.querySelector("#type-filters"));
  if (splitCalendar()) buildTypeFilters("calendar", document.querySelector("#side-type-filters"));
  updateTypeSummary();
}
function buildTypeFilters(tab, container) {
  container.replaceChildren();
  const types = [...new Set([...contentTypes, ...state.items.map(item => item.content_type)])]
    .filter(type => type && type !== "Presence" && (tab === "calendar" ? calendarTypes.has(type)
      : tab === "saved" || !calendarTypes.has(type)));
  if (tab === "calendar") types.push("Lessons / classroom bookings");
  const hidden = tab === "calendar" ? calendarHiddenTypes : hiddenTypes;
  types.forEach(type => {
    const label = el("label", "type-option");
    const checkbox = el("input");
    checkbox.type = "checkbox";
    checkbox.checked = !hidden.has(type);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) hidden.delete(type); else hidden.add(type);
      try {
        localStorage.setItem(tab === "calendar" ? "aula-calendar-hidden-types" : "aula-home-hidden-types",
          JSON.stringify([...hidden]));
      }
      catch (error) {
        console.warn("Could not persist content-type filters", error);
        setNotice("Filters apply for this visit, but could not be saved on this device.");
      }
      state.limit = FEED_PAGE;
      updateTypeSummary();
      render();
    });
    label.append(checkbox, el("span", "", type));
    container.append(label);
  });
}
function updateTypeSummary() {
  document.querySelector("#type-filter-summary").textContent = typeSummary(state.tab);
  document.querySelector("#side-filter-summary").textContent = typeSummary("calendar");
}
function typeSummary(tab) {
  const hidden = [...(tab === "calendar" ? calendarHiddenTypes : hiddenTypes)]
    .filter(type => tab === "calendar" || tab === "saved" || !calendarTypes.has(type));
  const selected = state.children.filter(child => childIsSelected(child.slug));
  const child = selected.length === state.children.length ? "Everyone"
    : selected.map(child => child.name).join(", ") || "No children";
  const dates = tab === "calendar" && futureMode !== "all"
    ? ` · ${futureMode === "hide" ? "Future hidden" : "Future only"}` : "";
  return `Filters · ${child}${dates}${hidden.length ? ` · ${hidden.join(", ")} hidden` : ""}`;
}
function presenceLabel(entry, now = Date.now()) {
  const fresh = entry.state !== "unknown" && Date.parse(entry.valid_until) > now;
  const times = [];
  if (fresh) {
    for (const [key, label] of [["check_in_time", "Arrived"], ["check_out_time", "Departed"]]) {
      const value = entry[key];
      if (!value) continue;
      let time = "";
      if (/^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$/.test(value)) time = value.slice(0, 5);
      else if (/[TZ+-]/.test(value) && Number.isFinite(Date.parse(value))) {
        time = new Date(value).toLocaleTimeString("en-GB", {
          hour: "2-digit", minute: "2-digit", timeZone: entry.timezone,
        });
      }
      if (time) times.push(`${label} ${time}`);
    }
    const clock = value => new Date(value).toLocaleTimeString("en-GB", {
      hour: "2-digit", minute: "2-digit", timeZone: entry.timezone,
    });
    for (const nap of entry.sleep_intervals || []) {
      const start = Date.parse(nap.start), end = Date.parse(nap.end);
      if (!Number.isFinite(start) || start > now) continue;
      if (!nap.end) {
        times.push(entry.status === "Napping"
          ? `Napping since ${clock(start)}`
          : `Nap started ${clock(start)} · wake-up not recorded`);
      } else if (Number.isFinite(end) && end >= start && end <= now) {
        const minutes = Math.floor((end - start) / 60000);
        const duration = minutes >= 60
          ? `${Math.floor(minutes / 60)}h${minutes % 60 ? ` ${minutes % 60}m` : ""}`
          : `${minutes}m`;
        times.push(`Slept ${duration} (${clock(start)}–${clock(end)})`);
      }
    }
    if (entry.status === "Napping" && !(entry.sleep_intervals || []).some(nap => !nap.end)) {
      times.push("Nap start not recorded");
    }
  }
  return {
    status: fresh ? entry.status : "Unknown",
    state: fresh ? entry.state : "unknown",
    detail: fresh ? times.join(" · ") : entry.reason || "Status out of date",
  };
}
function renderPresence() {
  const container = document.querySelector("#presence");
  container.hidden = !state.children.length;
  container.replaceChildren();
  if (container.hidden) return;
  for (const child of state.children) {
    const entry = state.presence.find(presence => presence.child_slug === child.slug)
      || {child_slug: child.slug, name: child.name, state: "unknown", status: "Unknown",
        reason: "No current presence status"};
    const label = presenceLabel(entry);
    const row = el("div", "presence-child");
    const filter = el("button", "presence-filter");
    filter.type = "button";
    row.classList.toggle("selected", childIsSelected(child.slug));
    filter.setAttribute("aria-pressed", String(childIsSelected(child.slug)));
    filter.setAttribute("aria-label", `Show ${child.name}'s items`);
    filter.addEventListener("click", () => toggleChildFilter(child.slug));
    const heading = el("div", "presence-heading");
    const who = el("span", "presence-who");
    const photo = childPhoto(entry.child_slug, "presence-photo");
    if (photo) who.append(photo);
    who.append(el("strong", "", child.name));
    heading.append(who);
    const check = el("span", "presence-check", "✓");
    check.setAttribute("aria-hidden", "true");
    filter.append(heading, check);
    row.append(filter);
    if (state.tab === "calendar" || (state.tab === "home" && splitCalendar())) {
      row.append(sicknessButton(child));
    }
    row.append(el("span", `presence-status ${label.state}`, label.status));
    if (label.detail) row.append(el("p", "presence-detail", label.detail));
    container.append(row);
  }
}
function aiProgressLabel(progress) {
  if (!progress || progress.status === "disabled" || progress.status === "idle"
      || !progress.total || progress.pending === 0 || progress.completed >= progress.total) return null;
  const count = `${progress.completed}/${progress.total}`;
  if (progress.status === "blocked") return {count: `Pending tasks ${count}`, detail: "Waiting for source"};
  let detail = "Estimating time…";
  if (progress.status === "retrying") {
    const minutes = Math.max(1, Math.ceil(progress.retry_seconds / 60));
    detail = `Retry in ~${minutes} min`;
  } else if (Number.isFinite(progress.eta_seconds) && progress.eta_seconds >= 0) {
    const minutes = Math.max(1, Math.ceil(progress.eta_seconds / 60));
    detail = minutes >= 60
      ? `~${Math.floor(minutes / 60)}h${minutes % 60 ? ` ${minutes % 60}m` : ""} left`
      : `~${minutes} min left`;
  }
  return {count: `Pending tasks ${count}`, detail};
}
function updateAIProgress(progress) {
  const container = document.querySelector("#ai-progress");
  const label = aiProgressLabel(progress);
  container.hidden = !label;
  if (!label) return;
  container.replaceChildren(el("strong", "", label.count), el("span", "", label.detail));
  container.title = `${progress.completed} of ${progress.total} tasks complete. ${label.detail}. Estimates change with provider response times and retries.`;
}
function isCalendarItem(item) {
  return Boolean(item.calendar_event) || calendarTypes.has(item.content_type);
}
function calendarFilterType(item) {
  return (item.calendar_event?.calendar_kind || item.calendar_kind) === "lesson"
    ? "Lessons / classroom bookings" : item.content_type;
}
function itemTitle(item) {
  return item.translated_title || item.title || "Untitled";
}
function calendarOrigin(event) {
  if (event.origin === "parent") return "Sick day · reported to Aula";
  if (event.origin === "aula" && event.calendar_kind === "lesson") return "Aula lesson";
  if (event.origin === "aula") return "Aula calendar";
  if (event.origin === "ai") return event.review_required ? "AI suggestion" : "AI approved";
  return event.review_required ? "Needs review" : "Calendar event";
}
function threadTranslationStatus(message, item = {}) {
  if (message.translation_status === "original" && message.is_translated !== false) {
    return {text: message.original_text || message.translated_text || "No message text provided.", translated: false, label: "Original message"};
  }
  if (message.is_translated === false && (message.translated_text || message.original_text)) {
    return {text: message.translated_text || message.original_text, translated: false, label: "English original"};
  }
  if (message.translated_text) return {
    text: message.translated_text,
    translated: message.is_translated === true,
    label: message.is_translated === true ? "English translation" : "Message",
  };
  const error = message.translation_error || item.translation_error;
  if (error) return {text: `Translation unavailable: ${error}`, translated: false};
  if (message.translation_status === "pending" || message.translation_pending || item.translation_pending) {
    return {text: "Translation pending…", translated: false};
  }
  return {text: "Translation unavailable.", translated: false};
}
function newestMessages(messages) {
  return messages.map((message, index) => ({message, index})).sort((a, b) => {
    const left = Date.parse(a.message.date);
    const right = Date.parse(b.message.date);
    if (!Number.isFinite(left) && !Number.isFinite(right)) return a.index - b.index;
    if (!Number.isFinite(left)) return 1;
    if (!Number.isFinite(right)) return -1;
    return right - left || a.index - b.index;
  }).map(entry => entry.message);
}
function sameMessageGroup(previous, message) {
  if (!previous || !message.sender || previous.sender !== message.sender) return false;
  const gap = Date.parse(previous.date) - Date.parse(message.date);
  return Number.isFinite(gap) && gap >= 0 && gap <= 30 * 60 * 1000;
}
function isFutureEvent(item, now = new Date()) {
  const event = item.calendar_event || {};
  const start = item.calendar_event ? event.start : item.date;
  const startDay = eventDateKey(start);
  if (!startDay) return false;
  const today = localDateKey(now);
  if (/^\d{4}-\d{2}-\d{2}$/.test(start)) {
    if (start > today) return true;
    const end = event.end && eventDateKey(event.end);
    return Boolean(event.all_day && end && end > today);
  }
  const startTime = Date.parse(start);
  if (Number.isFinite(startTime) && startTime > now.getTime()) return true;
  const endTime = Date.parse(event.end || "");
  return Number.isFinite(endTime) && endTime > now.getTime();
}
function localDateKey(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
function dateFromKey(key) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(key)) return null;
  const [year, month, day] = key.split("-").map(Number);
  const date = new Date(year, month - 1, day);
  return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day
    ? date : null;
}
function shiftDateKey(key, days) {
  const date = dateFromKey(key);
  if (!date) return null;
  date.setDate(date.getDate() + days);
  return localDateKey(date);
}
function eventInputValue(value, allDay, timeZone) {
  if (!value) return "";
  if (allDay) return dateFromKey(value.slice(0, 10)) ? value.slice(0, 10) : "";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).formatToParts(date).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`;
}
function eventInputDates(start, end, allDay, event, timeZone) {
  if (allDay) return {start, end: end ? shiftDateKey(end, 1) : ""};
  const withSeconds = value => value.length === 16 ? `${value}:00` : value;
  const original = name => !event.all_day
    && eventInputValue(event[name], false, timeZone) === withSeconds(name === "start" ? start : end);
  return {start: original("start") ? event.start : start, end: original("end") ? event.end || "" : end};
}
function eventDateKey(value) {
  if (typeof value !== "string" || !value) return null;
  const day = value.slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day) || !dateFromKey(day)) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return dateFromKey(day) ? day : null;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? localDateKey(date) : null;
}
function calendarDisplayDate(key, options = {}) {
  const date = dateFromKey(key);
  return date ? date.toLocaleDateString(undefined, options) : "Date unavailable";
}
function calendarRange(focus = calendarFocusDate) {
  const start = shiftDateKey(focus, -calendarPastDays);
  const end = shiftDateKey(focus, calendarRangeDays - calendarPastDays - 1);
  return {start, end};
}
function calendarEventDay(event, now = new Date()) {
  const start = eventDateKey(event.start);
  if (!start) return null;
  const today = localDateKey(now);
  if (start < today && event.end) {
    if (event.all_day) {
      const end = eventDateKey(event.end);
      if (end && end > today) return today;
    } else {
      const end = Date.parse(event.end);
      if (Number.isFinite(end) && end > now.getTime()) return today;
    }
  }
  return start;
}
function calendarTimeLabel(event, item = {}, groupDay = null) {
  const start = event.start || event.source_date || item.date;
  const startDay = eventDateKey(start);
  if (!startDay) return "Date unavailable";
  if (event.all_day) {
    const exclusiveEnd = eventDateKey(event.end);
    const lastDay = exclusiveEnd ? shiftDateKey(exclusiveEnd, -1) : startDay;
    if (groupDay && startDay < groupDay && lastDay === groupDay) return "All day · ongoing";
    return lastDay && lastDay > startDay
      ? `All day · through ${calendarDisplayDate(lastDay, {month: "short", day: "numeric"})}`
      : "All day";
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(start)) return "Date";
  const startTime = new Date(start);
  if (!Number.isFinite(startTime.getTime())) return "Date unavailable";
  const formatTime = date => date.toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit"});
  const end = event.end && new Date(event.end);
  if (!end || !Number.isFinite(end.getTime())) return formatTime(startTime);
  const endDay = localDateKey(end);
  if (groupDay && endDay === groupDay && localDateKey(startTime) < groupDay) {
    return `Started ${calendarDisplayDate(localDateKey(startTime), {month: "short", day: "numeric"})} · ends ${formatTime(end)}`;
  }
  return endDay === localDateKey(startTime)
    ? `${formatTime(startTime)}–${formatTime(end)}`
    : `${formatTime(startTime)}–${calendarDisplayDate(endDay, {month: "short", day: "numeric"})} ${formatTime(end)}`;
}
function calendarFullDateLabel(event, item = {}) {
  const start = event.start || event.source_date || item.date;
  const startDay = eventDateKey(start);
  if (!startDay) return "Date unavailable";
  const dateLabel = key => calendarDisplayDate(key, {month: "short", day: "numeric", year: "numeric"});
  if (event.all_day) {
    const exclusiveEnd = event.end && eventDateKey(event.end);
    const lastDay = exclusiveEnd ? shiftDateKey(exclusiveEnd, -1) : startDay;
    return `${dateLabel(startDay)}${lastDay && lastDay > startDay ? ` – ${dateLabel(lastDay)}` : ""} · All day`;
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(start)) return dateLabel(startDay);
  const startTime = new Date(start);
  if (!Number.isFinite(startTime.getTime())) return "Date unavailable";
  const formatTime = date => date.toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit"});
  const startDate = localDateKey(startTime);
  if (!event.end || !Number.isFinite(new Date(event.end).getTime())) {
    return `${dateLabel(startDate)} · ${formatTime(startTime)}`;
  }
  const endTime = new Date(event.end);
  const endDate = localDateKey(endTime);
  return startDate === endDate
    ? `${dateLabel(startDate)} · ${formatTime(startTime)}–${formatTime(endTime)}`
    : `${dateLabel(startDate)} ${formatTime(startTime)} – ${dateLabel(endDate)} ${formatTime(endTime)}`;
}
function calendarDayHeading(key, today = localDateKey(new Date())) {
  const yesterday = shiftDateKey(today, -1);
  const tomorrow = shiftDateKey(today, 1);
  const date = calendarDisplayDate(key, {weekday: "long", month: "long", day: "numeric",
    ...(key.slice(0, 4) !== today.slice(0, 4) ? {year: "numeric"} : {})});
  if (key === today) return `Today · ${date}`;
  if (key === tomorrow) return `Tomorrow · ${date}`;
  if (key === yesterday) return `Yesterday · ${date}`;
  return date;
}
function agendaSort(items) {
  return [...items].sort((left, right) => {
    const leftEvent = left.calendar_event || {start: left.date};
    const rightEvent = right.calendar_event || {start: right.date};
    const leftDay = calendarEventDay(leftEvent) || "9999-99-99";
    const rightDay = calendarEventDay(rightEvent) || "9999-99-99";
    if (leftDay !== rightDay) return leftDay.localeCompare(rightDay);
    if (Boolean(leftEvent.all_day) !== Boolean(rightEvent.all_day)) return leftEvent.all_day ? -1 : 1;
    const leftTime = Date.parse(leftEvent.start || "");
    const rightTime = Date.parse(rightEvent.start || "");
    if (Number.isFinite(leftTime) && Number.isFinite(rightTime)) return leftTime - rightTime;
    return String(left.title || "").localeCompare(String(right.title || ""));
  });
}
function timeLabel(date) {
  const value = new Date(date);
  if (!date || Number.isNaN(value.getTime())) return "Date unavailable";
  const options = {month: "short", day: "numeric", year: "numeric"};
  if (date.length > 10) Object.assign(options, {hour: "2-digit", minute: "2-digit"});
  return value.toLocaleString(undefined, options);
}
function action(name, label, callback, cls = "") {
  const button = el("button", `action ${cls}`);
  button.innerHTML = icon(name);
  button.append(el("span", "", label));
  button.addEventListener("click", callback);
  return button;
}
function saveAction(item) {
  const save = action("saved", saved.has(item.id) ? "Saved" : "Save", () => {
    const updated = new Set(saved);
    if (updated.has(item.id)) updated.delete(item.id); else updated.add(item.id);
    try { localStorage.setItem("aula-saved", JSON.stringify([...updated])); }
    catch (error) { setNotice("Could not save on this device. Browser storage is unavailable."); return; }
    saved = updated;
    save.setAttribute("aria-pressed", saved.has(item.id));
    save.querySelector("span").textContent = saved.has(item.id) ? "Saved" : "Save";
    if (state.tab === "saved") render();
  });
  save.setAttribute("aria-pressed", saved.has(item.id));
  return save;
}
function calendarEvidence(event, item) {
  const section = el("section", "calendar-evidence");
  section.append(el("strong", "calendar-origin", calendarOrigin(event)));
  if (event.origin === "parent") {
    section.append(el("p", "", event.description || event.evidence));
    return section;
  }
  const sourceTitle = event.source_title || "Original Aula item";
  const sourceDate = event.source_date || item?.date;
  section.append(el("p", "detail-source",
    `Found in: ${sourceTitle}${sourceDate ? ` · ${timeLabel(sourceDate)}` : ""}${event.source_author ? ` · ${event.source_author}` : ""}`));
  if (event.date_rationale) section.append(el("p", "calendar-rationale", event.date_rationale));
  const evidence = event.source_excerpt || event.evidence;
  if (evidence) section.append(el("blockquote", "calendar-quote", evidence));
  else section.append(el("p", "detail-source", "No verbatim source evidence is available."));
  const translation = item?.translated_text || item?.summary;
  if (translation) {
    const english = el("div", "calendar-translation");
    english.append(el("strong", "", item.translated_text ? "English translation" : "English summary"),
      richText(translation));
    section.append(english);
  }
  if (event.source_id) {
    const sourceLink = el("a", "page-back", "Open original source");
    sourceLink.href = `#post/${encodeURIComponent(event.source_id)}`;
    section.append(sourceLink);
  }
  return section;
}
function agendaEvent(item) {
  if (item.calendar_event) return item.calendar_event;
  return {
    title: item.title,
    start: item.date,
    all_day: /^\d{4}-\d{2}-\d{2}$/.test(item.date || ""),
    origin: item.source === "calendar" || item.source === "aula_api" ? "aula" : "",
  };
}
function agendaChildren(item, event) {
  if (item.children?.length) return item.children;
  const names = new Map(state.children.map(child => [child.slug, child.name]));
  return (event.child_slugs || item.child_slugs || []).map(slug => names.get(slug) || slug);
}
function agendaEntry(item, pending = false, groupDay = null) {
  const event = agendaEvent(item);
  const title = event.title || itemTitle(item);
  const children = agendaChildren(item, event);
  const origin = calendarOrigin(event);
  const metadata = [
    calendarTimeLabel(event, item, groupDay),
    event.location,
    children.length ? children.join(", ") : "Family",
    origin,
  ].filter(Boolean);
  const row = el("a", `calendar-entry${pending ? " needs-review-entry" : ""}`);
  row.href = event.uid ? `#event/${encodeURIComponent(event.uid)}` : `#post/${encodeURIComponent(item.id)}`;
  row.setAttribute("aria-label", `${pending ? "Review " : ""}${title}. ${metadata.join(" · ")}`);
  row.append(el("h3", "calendar-entry-title", title), el("p", "calendar-entry-meta", metadata.join(" · ")));
  return row;
}
function renderAgenda(items, container, {limit = state.limit, more = document.querySelector("#more")} = {}) {
  const today = localDateKey(new Date());
  const range = calendarRange();
  document.querySelector("#agenda-date").value = calendarFocusDate;
  document.querySelector("#agenda-range").textContent =
    `${calendarDisplayDate(range.start, {month: "short", day: "numeric"})} – ${calendarDisplayDate(range.end, {month: "short", day: "numeric", year: "numeric"})}`;
  const pending = items.filter(item => item.calendar_event?.review_required);
  const scheduled = agendaSort(items.filter(item => !item.calendar_event?.review_required))
    .filter(item => {
      const event = agendaEvent(item);
      const day = calendarEventDay(event);
      return day && day >= range.start && day <= range.end;
    });
  const grouped = new Map();
  scheduled.forEach(item => {
    const day = calendarEventDay(agendaEvent(item));
    if (!grouped.has(day)) grouped.set(day, []);
    grouped.get(day).push(item);
  });
  const visible = scheduled.slice(0, limit);
  const visibleDays = new Set(visible.map(item => calendarEventDay(agendaEvent(item))));
  const agenda = el("section", "calendar-agenda");
  agenda.setAttribute("aria-label", "Calendar agenda");
  agenda.append(el("p", "agenda-count",
    `${scheduled.length} scheduled event${scheduled.length === 1 ? "" : "s"} in range`));
  visibleDays.forEach(day => {
    const events = grouped.get(day);
    const group = el("section", "agenda-day");
    const heading = el("div", "agenda-day-heading");
    heading.append(el("h2", "", calendarDayHeading(day, today)),
      el("span", "agenda-day-count", `${events.length} event${events.length === 1 ? "" : "s"}`));
    group.append(heading);
    events.filter(item => visible.includes(item)).forEach(item => group.append(agendaEntry(item, false, day)));
    agenda.append(group);
  });
  if (!scheduled.length) {
    const empty = el("div", "empty agenda-empty");
    empty.append(el("h2", "", "No scheduled events in this date range"),
      el("p", "", "Use the date controls to browse another range. Your event and child filters still apply."));
    agenda.append(empty);
  }
  if (pending.length) {
    const review = el("details", "needs-review");
    review.append(el("summary", "", `Needs review · ${pending.length}`));
    const rows = el("div", "needs-review-list");
    pending.forEach(item => rows.append(agendaEntry(item, true)));
    review.append(rows);
    agenda.append(review);
  }
  container.replaceChildren(agenda);
  more.hidden = scheduled.length <= limit;
  more.textContent = `Show more events · ${Math.min(limit, scheduled.length)} of ${scheduled.length}`;
}
function card(item, {detail = false} = {}) {
  const article = el("article", "post");
  article.dataset.id = item.id;
  const header = el("div", "post-header");
  const toggle = el("button", "collapse-toggle");
  toggle.type = "button";
  toggle.setAttribute("aria-label", `Collapse or expand: ${item.title || "Untitled"}`);
  if (!detail) header.append(toggle);
  const slugs = item.child_slugs || [];
  const names = item.children || [];
  const family = slugs.length !== 1;
  const avatar = el("span", `avatar ${family ? "family" : childClass(slugs[0])}`.trim(),
    family ? "a" : String(names[0] || slugs[0] || "a").slice(0, 1));
  const photo = family ? null : childPhoto(slugs[0], "avatar-photo");
  if (photo) {
    const letter = avatar.textContent;
    avatar.textContent = ""; avatar.append(photo); avatar.classList.add("has-photo");
    photo.addEventListener("error", () => { avatar.classList.remove("has-photo"); avatar.textContent = letter; });
  }
  header.append(avatar);
  const meta = el("div", "meta");
  meta.append(el("span", "community", `a/${family ? "family" : slugs[0]}`), el("span", "separator", "·"));
  const time = el("time", "", timeLabel(item.date));
  if (item.date) time.dateTime = item.date;
  meta.append(time);
  if (item.unread) {
    const dot = el("span", "unread-dot");
    dot.title = "Unread in Aula";
    meta.append(dot);
  }
  header.append(meta);
  const more = el("button", "post-menu");
  more.innerHTML = icon("more");
  more.setAttribute("aria-label", `Show original: ${item.title || "Untitled"}`);
  header.append(more);
  article.append(header);
  if (!detail) {
    header.title = "Click to collapse or expand";
    header.addEventListener("click", event => {
      if (event.target.closest(".post-menu, a")) return;
      setPostCollapsed(article, item.id, !article.classList.contains("collapsed"));
    });
  }
  const flairs = el("div", "flairs");
  names.forEach((name, index) => flairs.append(el("span", `flair child ${childClass(slugs[index])}`.trim(), name)));
  if (!names.length) flairs.append(el("span", "flair", "Child unassigned"));
  flairs.append(el("span", "flair", item.content_type));
  if (item.event_type && item.event_type !== "other") flairs.append(el("span", "flair", item.event_type));
  if (item.calendar_event) flairs.append(el("span", "flair", calendarOrigin(item.calendar_event)));
  flairs.append(el("span", `flair ${item.is_translated ? "translation" : "pending"}`,
    item.is_translated ? "Translated summary" : item.source === "original" ? "Original · not translated" : "Not translated"));
  article.append(flairs);
  const contents = el("div", "content-indicators");
  const mediaEntries = new Map();
  for (const attachment of [...(item.attachments || []),
    ...(item.thread_messages || []).flatMap(message => message.attachments || [])]) {
    mediaEntries.set(`${attachment.kind}:${attachment.name}:${attachment.url || ""}`, attachment);
  }
  for (const [kind, label] of [["image", "image"], ["file", "attachment"], ["unknown", "attachment"],
    ["video", "video"], ["audio", "audio file"]]) {
    const count = [...mediaEntries.values()].filter(attachment => attachment.kind === kind).length;
    if (count) contents.append(el("span", `content-indicator ${kind}`,
      kind === "unknown" ? "Attachments" : `${count} ${label}${count === 1 ? "" : "s"}`));
  }
  const pendingEvents = (item.recognized_events || []).filter(event => event.review_required).length
    || (item.calendar_event?.review_required ? 1 : 0);
  if (pendingEvents) contents.append(el("span", "content-indicator event-pending",
    `${pendingEvents} pending event${pendingEvents === 1 ? "" : "s"}`));
  if (contents.childElementCount) article.append(contents);
  if (item.calendar_event) {
    const event = item.calendar_event;
    const title = event.origin === "aula" ? itemTitle(item) : event.title || itemTitle(item);
    article.append(el("h2", "calendar-event-title", title),
      el("p", "calendar-event-date", calendarFullDateLabel(event, item)));
    if (title !== itemTitle(item)) article.append(el("h2", "", itemTitle(item)));
  } else {
    article.append(el("h2", "", itemTitle(item)));
  }
  const body = item.summary || item.original_text;
  if (body) article.append(richText(body, "body-text"));
  if (item.content_type === "Album" && item.album?.thumbs) {
    const covers = el("div", "album-covers");
    for (let index = 0; index < Math.min(item.album.thumbs, 4); index++) {
      const image = el("img");
      image.alt = "";
      image.loading = "lazy";
      image.decoding = "async";
      image.src = `api/gallery/image?album=${encodeURIComponent(item.album.id)}&cover=${index}`;
      image.addEventListener("error", () => image.remove(), {once: true});
      covers.append(image);
    }
    article.append(covers);
  }
  if (item.analysis_error) article.append(el("p", "notice", `Summary unavailable: ${item.analysis_error}`));
  if (item.translation_pending) article.append(el("p", "profile-status", "English translation pending."));
  if (item.translation_error) article.append(el("p", "notice", `English translation unavailable: ${item.translation_error}`));
  if (item.calendar_event) article.append(calendarEvidence(item.calendar_event, item));
  if (item.sender) {
    const author = el("p", "author");
    const photo = authorPhoto(item.sender, "author-photo");
    if (photo) author.append(photo);
    author.append(document.createTextNode(`By ${item.sender}`));
    article.append(author);
  }
  const actions = el("div", "post-actions");
  const read = action("read", "Read", () => { navigate(`${item.thread_messages?.length ? "thread" : "post"}/${encodeURIComponent(item.id)}`); });
  more.addEventListener("click", () => read.click());
  const save = saveAction(item);
  const share = action("share", "Share", async () => {
    const plain = plainText(body);
    const summary = plain.length > 280 ? `${plain.slice(0, 277)}…` : plain;
    const meta = `${names.join(", ")} · ${item.content_type} · ${timeLabel(item.date)}`;
    const url = shareUrl(item);
    const text = [meta, summary].filter(Boolean).join("\n");
    try {
      if (navigator.share) await navigator.share({title: itemTitle(item), text, url});
      else if (navigator.clipboard) {
        await navigator.clipboard.writeText(`${itemTitle(item)}\n${text}\n${url}`);
        share.querySelector("span").textContent = "Copied";
        setTimeout(() => { share.querySelector("span").textContent = "Share"; }, 2000);
      } else setNotice("Sharing is not supported by this browser.");
    } catch (error) {
      if (error.name !== "AbortError") setNotice(`Could not share: ${error.message}`);
    }
  }, "share");
  actions.append(read, save, share);
  if (item.calendar_event) {
    actions.append(...calendarActions(item.calendar_event));
  }
  article.append(actions);
  // A collapsed feed card must not hide the summary and actions on its own detail page.
  const collapsed = !detail && collapsedPosts.has(item.id);
  article.classList.toggle("collapsed", collapsed);
  toggle.setAttribute("aria-expanded", String(!collapsed));
  toggle.textContent = collapsed ? "+" : "–";
  return article;
}
function attentionEvents() {
  const events = new Map();
  state.items.forEach(item => {
    [item.calendar_event, ...(item.recognized_events || [])].filter(Boolean).forEach(event => {
      if (event.review_required && event.uid) events.set(event.uid, event);
    });
  });
  return [...events.values()];
}
function updateAttention() {
  const count = attentionEvents().length;
  const badge = document.querySelector("#attention-count");
  badge.textContent = `${count} requires attention`;
  badge.hidden = !count;
}
function filteredItems(tab) {
  const hidden = tab === "calendar" ? calendarHiddenTypes : hiddenTypes;
  return state.items.filter(item => (
    item.content_type !== "Presence" &&
    !hidden.has(item.content_type) && !hidden.has(calendarFilterType(item)) &&
    (tab === "calendar" ? isCalendarItem(item) : tab === "saved" || !isCalendarItem(item)) &&
    (tab !== "calendar" || futureMode === "all"
      || (futureMode === "only" ? isFutureEvent(item) : !isFutureEvent(item))) &&
    (state.child === "all" || (item.child_slugs || []).some(slug => childIsSelected(slug))) &&
    (tab !== "saved" || saved.has(item.id)) &&
    (!state.search || [item.title, item.translated_title, item.original_text, item.summary, item.sender,
      item.content_type, item.calendar_event?.title, item.calendar_event?.location,
      item.calendar_event?.source_excerpt, ...(item.children || [])]
      .join(" ").toLocaleLowerCase().includes(state.search))
  ));
}
let appliedChildKey = "";
const sickness = {result: null, busy: false, error: "", message: "", checked: 0, timer: null};
function sicknessButton(child) {
  const result = sickness.result;
  const entry = result?.children.find(entry => entry.slug === child.slug);
  const fresh = result && Date.now() < Date.parse(result.valid_until);
  const sick = Boolean(fresh && entry?.sick);
  const button = el("button", "sickness-toggle");
  button.type = "button";
  button.dataset.child = child.slug;
  button.setAttribute("aria-label", `${child.name}: Sick today`);
  button.setAttribute("aria-pressed", String(sick));
  button.disabled = sickness.busy || Boolean(sickness.error) || !fresh || !entry?.can_report;
  button.title = sickness.error || entry?.reason || (!fresh ? "Checking today's Aula status…"
    : "Report or clear sickness for today only");
  const box = el("span", "sickness-box", sick ? "✓" : "");
  box.setAttribute("aria-hidden", "true");
  button.append(box, document.createTextNode("Sick today"));
  if (entry && !entry.can_report) button.title = `${child.name}: ${entry.reason}`;
  button.addEventListener("click", async () => {
    if (sickness.busy) return;
    if (Date.now() >= Date.parse(result.valid_until)) {
      await checkSickness();
      return;
    }
    const next = !sick;
    const prompt = next
      ? `Mark ${child.name} sick for ${result.date} only?\n\n${result.notice}`
      : `Clear ${child.name}'s sickness report for ${result.date}?\n\nThis removes today's sick-day calendar entry. It does not check your child in.`;
    if (!window.confirm(prompt)) return;
    sickness.busy = true;
    sickness.message = "Updating Aula…";
    renderPresence();
    renderSicknessStatus();
    try {
      const response = await fetch("api/sickness", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-Aula-Request": "settings"},
        body: JSON.stringify({child: child.slug, date: result.date, sick: next}),
      });
      const updated = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(updated.error || "Aula did not confirm the change");
      setSicknessResult(updated);
      sickness.message = next ? `${child.name} is reported sick today. Added to your calendar.`
        : `${child.name}'s sickness report cleared. Removed today's sick-day calendar entry.`;
      await load({force: true});
    } catch (error) {
      sickness.error = `Change not confirmed: ${error.message}. Check Aula status before retrying.`;
      sickness.message = "";
    } finally {
      sickness.busy = false;
      renderPresence();
      renderSicknessStatus();
    }
  });
  return button;
}
function setSicknessResult(result) {
  clearTimeout(sickness.timer);
  sickness.result = result;
  sickness.checked = Date.now();
  sickness.error = "";
  const remaining = Date.parse(result.valid_until) - Date.now();
  if (!(remaining > 0)) throw new Error("A new day has started. Check Aula status again.");
  sickness.timer = setTimeout(() => {
    sickness.result = null;
    sickness.message = "";
    renderPresence();
    checkSickness();
  }, remaining);
}
async function checkSickness() {
  if (sickness.busy) return;
  sickness.busy = true;
  sickness.message = "";
  sickness.checked = Date.now();
  renderPresence();
  try {
    const response = await fetch("api/sickness", {cache: "no-store"});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || "Could not check sickness reporting");
    setSicknessResult(result);
  } catch (error) {
    sickness.error = error.message;
  } finally {
    sickness.busy = false;
    renderPresence();
    renderSicknessStatus();
  }
}
function renderSicknessStatus() {
  const mount = document.querySelector("#sickness-status");
  mount.hidden = !(state.tab === "calendar" || (state.tab === "home" && splitCalendar()))
    || !(sickness.error || sickness.message);
  mount.replaceChildren(document.createTextNode(sickness.error || sickness.message));
  if (sickness.error) {
    const retry = el("button", "action", "Check Aula status");
    retry.type = "button";
    retry.disabled = sickness.busy;
    retry.addEventListener("click", checkSickness);
    mount.append(retry);
  }
}
function renderSicknessControls(side) {
  renderSicknessStatus();
  if ((state.tab === "calendar" || side) && Date.now() - sickness.checked >= 60000) checkSickness();
}
function render() {
  updateAttention();
  let key = keyFromLocation();
  if (key.startsWith("child/")) {
    const slug = decodeURIComponent(key.slice(6));
    if (appliedChildKey !== key && state.loaded) {
      appliedChildKey = key;
      if (state.children.some(child => child.slug === slug)) { state.child = [slug]; filters(); }
    }
    key = "";
  } else appliedChildKey = "";
  if (key && !["calendar", "saved"].includes(key)) {
    route();
    return;
  }
  if (key === "calendar" && splitCalendar()) key = "";
  selectTab(key || "home");
  renderPresence();
  const wideHome = splitCalendar() && state.tab === "home";
  const side = wideHome && !calendarCollapsed;
  const calendarToggle = document.querySelector("#calendar-toggle");
  calendarToggle.hidden = !wideHome;
  calendarToggle.textContent = side ? "Hide calendar" : "Show calendar";
  calendarToggle.setAttribute("aria-expanded", String(side));
  renderSicknessControls(side);
  document.querySelector("#calendar-side").hidden = !side;
  document.body.classList.toggle("split-calendar", side);
  document.body.classList.toggle("calendar-collapsed", wideHome && calendarCollapsed);
  if (side) {
    renderAgenda(filteredItems("calendar"), document.querySelector("#side-agenda"),
      {limit: sideLimit, more: document.querySelector("#side-more")});
  }
  const items = filteredItems(state.tab);
  const container = document.querySelector("#feed");
  const sync = document.querySelector("#sync");
  document.querySelector("#side-sync").textContent = sync.dataset.updated || "";
  const scrollY = window.scrollY;
  if (state.tab === "calendar") {
    renderAgenda(items, container, {limit: Math.ceil(state.limit / FEED_PAGE) * CALENDAR_PAGE});
    document.querySelector("#more").after(sync);
    if (sync.dataset.updated) sync.textContent = sync.dataset.updated;
    window.scrollTo(0, scrollY);
    return;
  }
  container.before(sync);
  const fragment = document.createDocumentFragment();
  items.slice(0, state.limit).forEach(item => {
    fragment.append(card(item));
  });
  if (!items.length) {
    const empty = el("div", "empty");
    empty.append(el("h2", "", "No matching posts"), el("p", "", "Try changing your content-type, child or search filters."));
    fragment.append(empty);
  }
  // Keep keyboard focus on the same control when a poll rebuilds the cards.
  const focused = document.activeElement?.closest?.("#feed article.post");
  const focusIndex = focused ? [...focused.querySelectorAll("button, a")].indexOf(document.activeElement) : -1;
  container.replaceChildren(fragment);
  if (focused && focusIndex >= 0) {
    const replacement = [...container.querySelectorAll("article.post")].find(post => post.dataset.id === focused.dataset.id);
    replacement?.querySelectorAll("button, a")[focusIndex]?.focus({preventScroll: true});
  }
  document.querySelector("#more").textContent = "Show more posts";
  window.scrollTo(0, scrollY);
  document.querySelector("#more").hidden = items.length <= state.limit;
  if (sync.dataset.updated) sync.textContent = `${items.length} items · ${sync.dataset.updated}`;
}
document.querySelector("#more").addEventListener("click", () => { state.limit += FEED_PAGE; render(); });
document.querySelector("#side-more").addEventListener("click", () => { sideLimit += 30; render(); });
// Polls, tab returns and actions can overlap: never let an older response replace newer data, and
// skip rebuilding the whole feed (losing focus and re-requesting images) when nothing changed.
const feedLoad = {seq: 0, applied: 0, pending: null, text: "", day: "", rendered: 0};
function load({poll = false, force = false} = {}) {
  if (poll && feedLoad.pending) return feedLoad.pending;
  const request = loadFeed(++feedLoad.seq, force);
  feedLoad.pending = request;
  request.finally(() => { if (feedLoad.pending === request) feedLoad.pending = null; });
  return request;
}
async function loadFeed(seq, force) {
  try {
    const response = await fetch("api/feed", {cache: "no-store"});
    if (!response.ok) throw new Error(`Feed request failed (${response.status})`);
    const text = await response.text();
    if (seq < feedLoad.applied) return;
    feedLoad.applied = seq;
    const day = localDateKey(new Date());
    if (!force && state.loaded && text === feedLoad.text && day === feedLoad.day
        && Date.now() - feedLoad.rendered < 300000) {
      if (!document.querySelector("#timeline").hidden) {
        renderPresence();
        renderSicknessControls(splitCalendar() && state.tab === "home");
      }
      refreshBell();
      return;
    }
    const payload = JSON.parse(text);
    Object.assign(feedLoad, {text, day, rendered: Date.now()});
    updateAIProgress(payload.ai_progress);
    state.items = payload.items.filter(item => item.content_type !== "Presence");
    state.presence = payload.presence || [];
    state.children = payload.children || [];
    state.authors = payload.authors || {};
    state.loaded = true;
    document.querySelector("#sync").dataset.updated =
      payload.updated_at ? `Updated ${timeLabel(payload.updated_at)}` : "First sync in progress";
    setNotice((payload.errors || []).join(" "));
    filters();
    typeFilters();
    render();
    refreshBell();
  } catch (error) {
    if (seq < feedLoad.applied) return;
    setNotice(`${error.message}. Showing the last loaded feed; retrying automatically.`);
    renderPresence();
  }
}
let bell = {items: [], unread: 0};
function paintBell() {
  const dot = document.querySelector(".bell-dot");
  dot.hidden = !bell.unread;
  const link = document.querySelector(".bell-link");
  const label = bell.unread ? `Notification history, ${bell.unread} unread` : "Notification history";
  link.setAttribute("aria-label", label);
  link.title = label;
}
async function refreshBell(request) {
  try {
    const response = await fetch(request ? "api/notifications/read" : "api/notifications/history", request || {cache: "no-store"});
    if (!response.ok) return;
    bell = await response.json();
    paintBell();
    if (activeRoute === "history") renderHistory();
  } catch (error) {
    console.warn("Notification history unavailable", error.message);
  }
}
function markNotificationOpened(itemId) {
  const pending = bell.items.filter(entry => entry.item_id === itemId && !entry.read);
  if (!pending.length) return;
  pending.forEach(entry => { entry.read = true; });
  refreshBell({method: "POST", headers: {"Content-Type": "application/json", "X-Aula-Request": "settings"},
    body: JSON.stringify({id: itemId})});
}
function renderHistory() {
  const page = document.querySelector("#page");
  page.querySelector(".history-list")?.remove();
  const list = el("ul", "history-list");
  if (!bell.items.length) list.append(el("li", "history-empty", "No notifications yet. Notifications sent from now on will be listed here."));
  for (const entry of bell.items) {
    const row = el("li", "history-row" + (entry.read ? "" : " unread"));
    const link = el(entry.available && entry.item_id ? "a" : "div", "history-link");
    if (entry.available && entry.item_id) {
      link.href = `#${entry.item_id.startsWith("thread:") ? "thread" : "post"}/${encodeURIComponent(entry.item_id)}`;
      link.addEventListener("click", () => markNotificationOpened(entry.item_id));
    }
    const head = el("div", "history-head");
    if (!entry.read) head.append(el("span", "unread-dot"));
    head.append(el("strong", "", entry.title), el("time", "history-time", timeLabel(entry.sent_at)));
    link.append(head, el("p", "history-text", entry.message));
    row.append(link);
    list.append(row);
  }
  page.append(list);
}
let whoami = null;
const whoamiRequest = fetch("api/whoami", {cache: "no-store"})
  .then(r => r.ok ? r.json() : null).then(data => { whoami = data?.user || ""; return whoami; })
  .catch(() => { whoami = ""; return ""; });
function accountCard() {
  const card = el("section", "account-card");
  card.setAttribute("aria-label", "Signed in and sync");
  const who = el("div", "account-who");
  const name = el("strong", "account-name", whoami || "…");
  const detail = el("span", "account-detail", "");
  const describe = user => {
    name.textContent = user === "HA" ? "Home Assistant" : user || "Signed in";
    detail.textContent = user === "HA" ? "Signed in via Home Assistant" : user ? "Signed in via Cloudflare Access" : "";
  };
  describe(whoami);
  if (whoami === null) whoamiRequest.then(describe);
  const sync = document.querySelector("#sync")?.dataset.updated || "";
  const status = el("span", "account-sync", sync);
  status.setAttribute("role", "status");
  who.append(name, detail, status);
  const button = el("button", "action account-refresh");
  button.type = "button";
  button.innerHTML = `${icon("refresh")}<span>Refresh</span>`;
  button.setAttribute("aria-label", "Refresh feed");
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const response = await fetch("api/refresh", {method: "POST", headers: {"X-Aula-Request": "refresh"}});
      if (!response.ok) throw new Error(`Refresh failed (${response.status})`);
      status.textContent = "Refresh queued · new items appear automatically";
      document.querySelector("#sync").textContent = "Refresh queued · New items and summaries appear automatically";
    } catch (error) {
      status.textContent = error.message;
      status.setAttribute("role", "alert");
    } finally { setTimeout(() => { button.disabled = false; }, 10000); }
  });
  card.append(who, button);
  return card;
}
typeFilters();
load();
setInterval(() => { if (!document.hidden) load({poll: true}); }, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) load({poll: true}); });

let routeGeneration = 0;
let activeRoute = "";
let detailSignature = "";
const mediaCache = new Map();
function openImageViewer(initial, gallery = [initial]) {
  let index = Math.max(0, gallery.indexOf(initial));
  let attachment = gallery[index] || initial;
  const viewer = el("dialog", "image-viewer");
  viewer.setAttribute("aria-label", attachment.name || "Image viewer");
  const toolbar = el("div", "image-viewer-toolbar");
  const actions = el("div", "image-viewer-actions");
  const bottom = el("div", "image-viewer-bottom");
  const tagSlot = el("div", "image-viewer-tags");
  const share = el("button", "image-viewer-share image-viewer-icon");
  share.type = "button";
  share.innerHTML = icon("share");
  const close = el("button", "image-viewer-close image-viewer-icon");
  close.type = "button";
  close.innerHTML = icon("close");
  close.setAttribute("aria-label", "Close");
  close.addEventListener("click", () => viewer.close());
  const previous = el("button", "image-viewer-nav", "‹");
  previous.type = "button";
  previous.setAttribute("aria-label", "Previous image");
  const next = el("button", "image-viewer-nav", "›");
  next.type = "button";
  next.setAttribute("aria-label", "Next image");
  const counter = el("span", "image-viewer-counter");
  if (gallery.length > 1) actions.append(previous, counter, next);
  toolbar.append(el("span"), actions, close);
  const image = el("img");
  image.referrerPolicy = "no-referrer";
  image.addEventListener("error", () => {
    image.replaceWith(el("p", "notice", "Image unavailable. View this post in Aula."));
  });
  const content = el("div", "image-viewer-content");
  const status = el("p", "image-viewer-share-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  bottom.append(tagSlot, status, share);
  let tagBusy = false;
  function paintTags() {
    const tags = attachment.tags;
    const focused = tagSlot.contains(document.activeElement) ? document.activeElement.dataset.child : null;
    tagSlot.replaceChildren(...(tags ? tags.choices.map(child => {
      const tagged = tags.photo.tagged_children.includes(child.slug);
      const button = el("button", "image-viewer-tag", `${tagged ? "✓" : "+"} ${child.name}`);
      button.type = "button";
      button.dataset.child = child.slug;
      button.setAttribute("aria-disabled", String(tagBusy));
      button.setAttribute("aria-pressed", String(tagged));
      button.setAttribute("aria-label", `${tagged ? "Remove tag for" : "Tag"} ${child.name}`);
      button.addEventListener("click", async () => {
        if (tagBusy) return;
        const current = attachment;
        tagBusy = true;
        paintTags();
        status.textContent = "Saving tag in Aula…";
        try {
          await tags.save(tags.photo, child, !tagged);
          if (current === attachment) status.textContent = tagged ? "Tag removed in Aula." : "Tagged in Aula.";
        } catch (error) {
          if (current === attachment) status.textContent = `Tag not confirmed: ${error.message}`;
        } finally {
          tagBusy = false;
          if (current === attachment) paintTags();
        }
      });
      return button;
    }) : []));
    if (focused) tagSlot.querySelector(`[data-child="${CSS.escape(focused)}"]`)?.focus();
  }
  let sharedFile = null;
  let shareByLink = false;
  let sharing = false;
  function show(newIndex) {
    index = (newIndex + gallery.length) % gallery.length;
    attachment = gallery[index];
    sharedFile = null;
    shareByLink = false;
    status.textContent = "";
    viewer.setAttribute("aria-label", attachment.name || "Image viewer");
    share.setAttribute("aria-label", `Share ${attachment.name || "image"}`);
    counter.textContent = `${index + 1} / ${gallery.length}`;
    paintTags();
    image.alt = attachment.name || "Aula image";
    image.src = attachment.url;
    content.replaceChildren(image);
    content.scrollTop = 0;
    for (const offset of [1, -1]) {
      const neighbour = gallery[(index + offset + gallery.length) % gallery.length];
      if (neighbour && neighbour !== attachment) new Image().src = neighbour.url;
    }
  }
  function step(delta) {
    if (gallery.length > 1 && !sharing && !tagBusy) show(index + delta);
  }
  previous.addEventListener("click", () => step(-1));
  next.addEventListener("click", () => step(1));
  viewer.addEventListener("keydown", event => {
    if (event.key === "ArrowLeft") { step(-1); event.preventDefault(); }
    else if (event.key === "ArrowRight") { step(1); event.preventDefault(); }
  });
  let touchStart = null;
  content.addEventListener("touchstart", event => {
    touchStart = event.touches.length === 1 ? {x: event.touches[0].clientX, y: event.touches[0].clientY} : null;
  }, {passive: true});
  content.addEventListener("touchend", event => {
    if (!touchStart) return;
    const dx = event.changedTouches[0].clientX - touchStart.x;
    const dy = event.changedTouches[0].clientY - touchStart.y;
    touchStart = null;
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) step(dx < 0 ? 1 : -1);
  }, {passive: true});
  share.addEventListener("click", async () => {
    if (sharing) return;
    sharing = true;
    share.disabled = true;
    status.textContent = "";
    try {
      if (shareByLink) {
        if (navigator.share) {
          await navigator.share({title: attachment.name || "Aula image", url: image.src});
          status.textContent = "Image link shared.";
        } else if (navigator.clipboard) {
          await navigator.clipboard.writeText(image.src);
          status.textContent = "Image link copied. Paste it into a message.";
        } else {
          status.textContent = "Image sharing is not supported on this device.";
        }
      } else {
        if (!sharedFile) {
        status.textContent = "Preparing image…";
        const response = await fetch(attachment.shareUrl || attachment.url, {cache: "no-store"});
        if (!response.ok) throw new Error("Image could not be loaded for sharing.");
        const blob = await response.blob();
        if (!blob.type.startsWith("image/")) throw new Error("This image is not available to share.");
        const safeName = String(attachment.name || "Aula image").replace(/[\\/:*?"<>|]+/g, "-").trim();
        const extension = blob.type.split("/")[1].replace("jpeg", "jpg").replace(/[^a-z0-9.+-]/gi, "");
        const name = /\.[a-z0-9]{2,5}$/i.test(safeName) ? safeName : `${safeName}.${extension}`;
        sharedFile = new File([blob], name, {type: blob.type});
        }
        if (navigator.share && navigator.canShare?.({files: [sharedFile]})) {
          await navigator.share({files: [sharedFile], title: sharedFile.name});
          status.textContent = "Image shared.";
        } else if (navigator.share) {
          await navigator.share({title: attachment.name || "Aula image", url: image.src});
          status.textContent = "Image link shared.";
        } else if (navigator.clipboard) {
          await navigator.clipboard.writeText(image.src);
          status.textContent = "Image link copied. Paste it into a message.";
        } else {
          status.textContent = "Image sharing is not supported on this device.";
        }
      }
    } catch (error) {
      if (error.name === "AbortError") return;
      if (error.name !== "NotAllowedError" && (!sharedFile || error.name === "TypeError")) {
        shareByLink = true;
        status.textContent = "Could not share the image file. Tap Share again to share its link.";
        return;
      }
      status.textContent = error.name === "NotAllowedError"
        ? "Tap Share again to open the share sheet."
        : "Could not share the image. Try again or open it in a browser.";
    } finally {
      sharing = false;
      share.disabled = false;
    }
  });
  viewer.append(toolbar, content, bottom);
  viewer.addEventListener("close", () => viewer.remove(), {once: true});
  show(index);
  document.body.append(viewer);
  viewer.showModal();
}
async function openAttachment(attachment, url) {
  const viewer = el("dialog", "image-viewer document-viewer");
  viewer.setAttribute("aria-label", attachment.name || "Attachment viewer");
  const toolbar = el("div", "document-toolbar");
  const close = el("button", "image-viewer-close", "Close");
  close.type = "button";
  close.addEventListener("click", () => viewer.close());
  toolbar.append(el("strong", "", attachment.name || "Attachment"), close);
  const content = el("div", "document-content");
  content.append(el("p", "profile-status", "Loading attachment…"));
  viewer.append(toolbar, content);
  let objectUrl;
  viewer.addEventListener("close", () => {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    viewer.remove();
  }, {once: true});
  document.body.append(viewer);
  viewer.showModal();
  try {
    const response = await fetch(url, {cache: "no-store"});
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || "Attachment unavailable");
    }
    const blob = await response.blob();
    if (!viewer.isConnected) return;
    objectUrl = URL.createObjectURL(blob);
    const download = el("a", "action", "Download");
    const downloadUrl = new URL(url, document.baseURI);
    downloadUrl.searchParams.set("download", "1");
    download.href = downloadUrl.href;
    download.download = attachment.name || "attachment";
    toolbar.insertBefore(download, close);
    content.replaceChildren();
    if (blob.type.startsWith("audio/") || blob.type.startsWith("video/")) {
      const player = el(blob.type.startsWith("audio/") ? "audio" : "video", "document-player");
      player.controls = true;
      player.src = objectUrl;
      content.append(player);
    } else if (blob.type.startsWith("image/")) {
      const image = el("img", "document-image");
      image.alt = attachment.name;
      image.src = objectUrl;
      content.append(image);
    } else {
      content.append(el("p", "notice", "This file format has no built-in preview. Use Download to open it in a compatible app."));
    }
  } catch (error) {
    if (viewer.isConnected) content.replaceChildren(el("p", "notice", error.message));
  }
}
const savedAttachments = new Map();
const installedApp = () => window.matchMedia?.("(display-mode: standalone)").matches || navigator.standalone === true;
const appleDevice = () => /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
async function deliverAttachment(file, label) {
  if (appleDevice() && navigator.canShare?.({files: [file]})) {
    try {
      await navigator.share({files: [file], title: file.name});
    } catch (error) {
      if (error.name === "NotAllowedError") {
        label.textContent = "Tap again to save";
        return;
      }
      if (error.name !== "AbortError") throw error;
    }
    label.textContent = "Download";
    return;
  }
  const objectUrl = URL.createObjectURL(file);
  const link = el("a");
  link.href = objectUrl;
  link.download = file.name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
  label.textContent = "Download";
}
async function saveAttachment(event, link, name) {
  // Installed apps (especially iOS) would navigate the app window to the file with no way back.
  if (!installedApp()) return;
  event.preventDefault();
  if (link.dataset.busy) return;
  const label = link.querySelector(".attachment-file-action");
  link.dataset.busy = "1";
  try {
    let file = savedAttachments.get(link.href);
    if (!file) {
      label.textContent = "Preparing…";
      const response = await fetch(link.href, {cache: "no-store"});
      if (!response.ok) throw new Error("Attachment unavailable");
      const blob = await response.blob();
      file = new File([blob], name, {type: blob.type || "application/octet-stream"});
      savedAttachments.set(link.href, file);
    }
    await deliverAttachment(file, label);
  } catch (error) {
    label.textContent = "Download failed — tap to retry";
  } finally {
    delete link.dataset.busy;
  }
}
function attachmentSection(attachments, item = null, message = null) {
  const section = el("section", "source-attachments");
  if (!attachments?.length) return section;
  section.append(el("h2", "", attachments.every(attachment => attachment.kind === "image") ? "Images" : "Attachments"));
  const photos = [];
  const photoGrid = el("div", "album-grid");
  const gallery = el("div", "attachment-gallery");
  attachments.forEach((attachment, index) => {
    const figure = el("figure", "attachment");
    const caption = el("figcaption", "", attachment.name || "Attachment");
    if (attachment.kind === "image" && /^https:\/\//i.test(attachment.url || "")) {
      const shareQuery = new URLSearchParams({id: item?.id || "", index: String(index)});
      if (message) shareQuery.set("message", message.id);
      const photo = {...attachment, shareUrl: `api/feed/attachment?${shareQuery}`};
      photos.push(photo);
      const open = el("button", "album-photo");
      open.type = "button";
      open.setAttribute("aria-label", `View ${attachment.name || "image"} full width`);
      open.addEventListener("click", () => openImageViewer(photo, photos));
      const image = el("img");
      image.alt = attachment.name || "Aula image";
      image.loading = "lazy";
      image.decoding = "async";
      image.referrerPolicy = "no-referrer";
      image.addEventListener("error", () => {
        open.remove();
        caption.textContent = `${attachment.name || "Image"} — Image unavailable here. Open this post in Aula to view it.`;
        figure.append(caption);
      }, {once: true});
      image.src = attachment.url;
      open.append(image);
      figure.append(open);
      figure.className = "album-photo-tile";
      photoGrid.append(figure);
      return;
    } else if (attachment.url && item) {
      const name = attachment.name || "attachment";
      const extension = (name.match(/\.([a-z0-9]{1,5})$/i)?.[1] || "file").toUpperCase();
      const query = new URLSearchParams({id: item.id, index: String(index)});
      if (message) query.set("message", message.id);
      const row = el("div", "attachment-file");
      const download = el("a", "attachment-file-download");
      download.href = `api/feed/attachment?${query}&download=1`;
      download.download = name;
      download.setAttribute("aria-label", `Download ${name}`);
      download.title = name;
      download.addEventListener("click", event => saveAttachment(event, download, name));
      download.append(el("span", "attachment-file-type", extension), el("span", "attachment-file-name", name),
        el("span", "attachment-file-action", "Download"));
      row.append(download);
      if (/^(MP3|M4A|WAV|OGG|AAC|MP4|MOV|WEBM|M4V)$/.test(extension)) {
        const play = el("button", "action attachment-file-play", "Play");
        play.type = "button";
        play.setAttribute("aria-label", `Play ${name}`);
        play.addEventListener("click", () => openAttachment(attachment, `api/feed/attachment?${query}`));
        row.append(play);
      }
      figure.append(row);
      gallery.append(figure);
      return;
    } else {
      caption.textContent += attachment.unavailable_reason ? ` — ${attachment.unavailable_reason}.` : attachment.kind === "image"
        ? " — Aula reports an image, but no displayable image link is available. View it in Aula."
        : " — Attachment present; view it in Aula.";
    }
    figure.append(caption);
    gallery.append(figure);
  });
  if (photos.length) {
    section.append(saveAllControl(photos, async photo => {
      const response = await fetch(photo.shareUrl, {cache: "no-store"});
      if (!response.ok) throw new Error("Photo unavailable");
      const blob = await response.blob();
      if (!blob.type.startsWith("image/")) throw new Error("Photo unavailable");
      const extension = blob.type.split("/")[1].replace("jpeg", "jpg").replace(/[^a-z0-9.+-]/gi, "");
      const name = photo.name || "Aula image";
      return new File([blob], /\.[a-z0-9]{2,5}$/i.test(name) ? name : `${name}.${extension}`, {type: blob.type});
    }), photoGrid);
  }
  if (gallery.childElementCount) section.append(gallery);
  return section;
}
async function postAttachments(item, container) {
  const cached = mediaCache.get(item.id);
  if (cached && Date.now() - cached.time < 300000) {
    container.replaceChildren(attachmentSection(cached.attachments, item));
    return;
  }
  try {
    const response = await fetch(`api/feed/media?id=${encodeURIComponent(item.id)}`, {cache: "no-store"});
    const data = await response.json().catch(() => ({}));
    container.replaceChildren(attachmentSection(data.attachments || item.attachments, item));
    if (!response.ok) throw new Error(data.error || `Attachments request failed (${response.status})`);
    if (Array.isArray(data.attachments)) mediaCache.set(item.id, {attachments: data.attachments, time: Date.now()});
  } catch (error) {
    container.append(el("p", "notice", error.message));
  }
}
function pageShell(title, parent = "#", detailBack = false) {
  const container = document.querySelector("#page");
  container.replaceChildren();
  const back = el("a", "page-back", "← Back");
  if (detailBack) back.dataset.detailBack = "true";
  back.href = parent;
  container.append(back, el("h1", "page-title", title));
  return container;
}
// Lookup results per item survive the periodic feed re-render, so links stay put and nothing is re-fetched.
const readingState = new Map();
const readingViews = new Map();
const readingBusy = new Map();
async function readingCall(id, url, options) {
  try {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Further reading unavailable");
    readingState.set(id, data);
  } catch (error) {
    readingState.set(id, {status: "failed-request", links: [], error: error.message});
  }
  readingViews.get(id)?.();
}
function readingTask(id, start) {
  // One in-flight operation per item; a finished lookup is answered by one held request (no polling).
  if (readingBusy.has(id)) return readingBusy.get(id);
  const task = (async () => {
    try {
      await start();
      while (readingState.get(id)?.status === "pending") {
        await readingCall(id, `api/feed/reading?id=${encodeURIComponent(id)}&wait=1`, {cache: "no-store"});
      }
    } finally { readingBusy.delete(id); }
  })();
  readingBusy.set(id, task);
  return task;
}
function relatedReading(item, mount) {
  const section = el("section", "detail-body related-reading");
  mount.append(section);
  const lookup = (label, regenerate) => {
    const button = el("button", "action", label);
    button.type = "button";
    button.addEventListener("click", () => {
      if (readingBusy.has(item.id)) return;
      readingState.set(item.id, {status: "pending", links: []});
      draw();
      readingTask(item.id, () => readingCall(item.id, "api/feed/reading", {method: "POST",
        headers: {"Content-Type": "application/json", "X-Aula-Request": "settings"},
        body: JSON.stringify(regenerate ? {id: item.id, regenerate: true} : {id: item.id})}));
    });
    return button;
  };
  function draw() {
    if (!section.isConnected) return;
    const data = readingState.get(item.id);
    section.replaceChildren(el("h2", "", "Learn more"));
    if (!data) { section.append(el("p", "profile-status", "Loading further reading…")); return; }
    if (data.status === "pending") {
      section.append(el("p", "profile-status", "Finding useful sources… this can take up to a minute."));
      return;
    }
    if (data.status === "failed-request") {
      section.append(el("p", "profile-status", data.error), lookup("Try again", true));
      return;
    }
    if (data.status === "idle") {
      section.append(lookup("Look up additional information", false));
      return;
    }
    const links = (data.links || []).filter(source => {
      try { return new URL(source.url).protocol === "https:"; } catch (error) { return false; }
    }).slice(0, 3);
    for (const source of links) {
      const link = el("a", "reading-link");
      link.href = new URL(source.url).href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.referrerPolicy = "no-referrer";
      link.append(el("strong", "", source.title), el("span", "", source.description || ""),
        el("small", "", `${source.publisher} ↗`));
      section.append(link);
    }
    if (data.status === "error") section.append(el("p", "profile-status", data.error));
    else if (!links.length) section.append(el("p", "profile-status", "No relevant additional sources found."));
    const meta = [];
    if (data.checked_at) meta.push(`Looked up ${timeLabel(data.checked_at)}`);
    if (data.outdated) meta.push("the post has changed since");
    if (meta.length) section.append(el("p", "profile-status reading-meta", meta.join(" · ")));
    section.append(lookup("Regenerate links", true));
  }
  readingViews.set(item.id, draw);
  draw();
  if (!readingState.has(item.id) && !readingBusy.has(item.id)) {
    readingTask(item.id, () => readingCall(item.id, `api/feed/reading?id=${encodeURIComponent(item.id)}`,
      {cache: "no-store"}));
  }
}
function eventList(events) {
  const list = el("ul", "event-list");
  events.forEach(event => {
    const row = el("li");
    if (event.uid) row.dataset.eventUid = event.uid;
    row.append(el("strong", "", event.title), el("div", "", calendarFullDateLabel(event)));
    if (event.location) row.append(el("div", "", event.location));
    if (event.review_required) {
      row.append(calendarEvidence(event, state.items.find(item => item.id === event.source_id)));
      const details = el("details", "review-context");
      details.append(el("summary", "", "Review details"),
        el("p", "", `${event.issue || "Check the date against the source."}${event.recommendation ? `\n${event.recommendation}` : ""}`));
      row.append(details);
    }
    if (event.uid) row.append(...calendarActions(event));
    list.append(row);
  });
  return list;
}
async function calendarRequest(path, value) {
  const response = await fetch(path, {
    method: "POST", headers: {"Content-Type": "application/json", "X-Aula-Request": "settings"},
    body: JSON.stringify(value),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || `Calendar update failed (${response.status})`);
}
function calendarActions(event, {edit = true} = {}) {
  const buttons = [];
  function control(label, path, value) {
    const button = action("saved", label, async () => {
      // Accept and Decline share one decision: lock both so a second tap cannot send the opposite one.
      if (buttons.some(other => other.dataset.busy)) return;
      const group = buttons.filter(other => other.dataset.decision);
      group.forEach(other => { other.disabled = true; other.dataset.busy = "true"; });
      button.setAttribute("aria-busy", "true");
      try {
        await calendarRequest(path, value);
        activeRoute = "";
        await load({force: true});
      } catch (error) {
        setNotice(error.message);
        group.forEach(other => { other.disabled = false; delete other.dataset.busy; });
      } finally { button.removeAttribute("aria-busy"); }
    });
    button.dataset.decision = value.action;
    buttons.push(button);
  }
  if (event.review_required) {
    control("Accept", "api/calendar/review", {uid: event.uid, action: "accept"});
    control("Decline", "api/calendar/review", {uid: event.uid, action: "decline"});
  }
  if (event.uid && edit) buttons.push(action("read", "Edit event", () => { navigate(`event/${encodeURIComponent(event.uid)}`); }));
  return buttons;
}
function renderListRoute(key, restoreFromHistory = false) {
  const tab = key === "calendar" ? "calendar" : key === "saved" ? "saved" : "home";
  const savedReturn = restoreFromHistory ? history.state?.aulaListReturn : null;
  const matches = savedReturn?.tab === tab && savedReturn.key === key;
  const scrollY = matches ? savedReturn.scrollY : null;
  if (matches) {
    selectTab(tab);
    Object.assign(state, {limit: savedReturn.limit, child: savedReturn.child, search: savedReturn.search});
    hiddenTypes = new Set(savedReturn.hiddenTypes);
    calendarHiddenTypes = new Set(savedReturn.calendarHiddenTypes);
    futureMode = savedReturn.futureMode;
    calendarFocusDate = savedReturn.calendarFocusDate;
    const search = document.querySelector("#search");
    search.value = savedReturn.searchValue;
    document.querySelector("#search-clear").hidden = !search.value;
  }
  render();
  if (scrollY !== null) requestAnimationFrame(() => window.scrollTo(0, scrollY));
}
const albumPhotos = new Map();
const albumImage = (album, photo, size, extra = "") =>
  `api/gallery/image?album=${encodeURIComponent(album)}&media=${encodeURIComponent(photo.id)}&size=${size}${extra}`;
async function fetchAlbumPhoto(album, photo) {
  const key = `${album}:${photo.id}`;
  if (albumPhotos.has(key)) return albumPhotos.get(key);
  const response = await fetch(albumImage(album, photo, "original"), {cache: "no-store"});
  if (!response.ok) throw new Error("Photo unavailable");
  const blob = await response.blob();
  const file = new File([blob], photo.name, {type: blob.type || "image/jpeg"});
  albumPhotos.set(key, file);
  return file;
}
function saveAllLabel(count) { return count === 1 ? "Save photo" : `Save all ${count} photos`; }
function savedTaggedLabel(count) {
  return count ? `Save ${count} tagged photo${count === 1 ? "" : "s"}` : "No tagged photos yet";
}
function saveAllControl(source, fetchPhoto, label = saveAllLabel) {
  const wrap = el("div", "album-save");
  const button = el("button", "action");
  button.type = "button";
  const note = el("p", "profile-status");
  wrap.append(button, note);
  let photos = [];
  let files = null;
  let busy = false;
  function reset() {
    photos = typeof source === "function" ? source() : source;
    files = null;
    button.textContent = label(photos.length);
    button.disabled = !photos.length;
    button.onclick = () => run(first);
  }
  wrap.refresh = () => { if (!busy) { reset(); note.textContent = ""; } };
  const batchSize = 30;
  async function share(start) {
    const batch = files.slice(start, start + batchSize);
    try {
      await navigator.share({files: batch});
    } catch (error) {
      if (error.name === "NotAllowedError") { button.textContent = "Tap again to save"; return; }
      if (error.name !== "AbortError") throw error;
    }
    const next = start + batchSize;
    if (next < files.length) {
      button.textContent = `Save photos ${next + 1}–${Math.min(next + batchSize, files.length)}`;
      button.onclick = () => run(() => share(next));
    } else {
      reset();
    }
  }
  async function run(task) {
    if (busy) return;
    busy = true;
    try { await task(); } catch (error) { note.textContent = `Could not save: ${error.message}`; }
    finally { busy = false; }
  }
  async function prepare() {
    const prepared = [];
    for (const photo of photos) {
      note.textContent = `Preparing ${prepared.length + 1} of ${photos.length}…`;
      prepared.push(await fetchPhoto(photo));
    }
    files = prepared;
    note.textContent = appleDevice()
      ? "In the share sheet choose Save Images, or a Shortcut that files them in an album."
      : "";
  }
  async function first() {
    if (!files) await prepare();
    if (appleDevice() && navigator.canShare?.({files: files.slice(0, batchSize)})) {
      button.textContent = "Tap to open share sheet";
      button.onclick = () => run(() => share(0));
      return;
    }
    for (const file of files) {
      const url = URL.createObjectURL(file);
      const link = el("a");
      link.href = url; link.download = file.name;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    note.textContent = `Downloaded ${files.length} photo${files.length === 1 ? "" : "s"}.`;
  }
  reset();
  return wrap;
}
async function saveAlbumTag(album, photo, child, tagged) {
  const response = await fetch("api/gallery/tag", {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Aula-Request": "settings"},
    body: JSON.stringify({album, media: photo.id, child: child.slug, tagged}),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || "Aula could not save this tag");
  Object.assign(photo, result);
  return photo;
}
async function albumSection(item, page) {
  const section = el("section", "detail-body album-photos");
  section.append(el("h2", "", "Photos"), el("p", "profile-status", "Loading photos…"));
  page.append(section);
  try {
    const response = await fetch(`api/gallery/album?id=${encodeURIComponent(item.album.id)}`, {cache: "no-store"});
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || "Photos unavailable");
    const album = await response.json();
    section.replaceChildren(el("h2", "", `${album.count} photo${album.count === 1 ? "" : "s"}`));
    const photos = album.media.filter(photo => photo.type === "image");
    const choices = album.tag_choices || [];
    const saveTagged = choices.length && photos.length ? saveAllControl(
      () => photos.filter(photo => photo.tagged_children.length),
      photo => fetchAlbumPhoto(item.album.id, photo), savedTaggedLabel) : null;
    if (saveTagged) section.append(saveTagged);
    if (choices.length && album.media.some(photo => photo.can_edit_tags)) {
      section.append(el("p", "profile-status",
        "Open a photo you have checked to tag your child. Tags are saved back to Aula."));
    } else if (choices.length) {
      section.append(el("p", "profile-status", "Aula does not allow your account to edit tags in this album."));
    }
    const tiles = new Map();
    function paintTile(photo) {
      const tile = tiles.get(photo);
      if (!tile) return;
      tile.querySelector(".album-tag-badges").replaceChildren(...choices
        .filter(child => photo.tagged_children.includes(child.slug))
        .map(child => el("span", "album-tag-badge", child.name)));
    }
    const saveTag = (photo, child, tagged) => saveAlbumTag(item.album.id, photo, child, tagged)
      .finally(() => { paintTile(photo); saveTagged?.refresh(); });
    const viewerImages = photos.map(photo => ({
      name: photo.name, url: albumImage(item.album.id, photo, "large"),
      tags: photo.can_edit_tags && choices.length ? {photo, choices, save: saveTag} : null,
    }));
    const grid = el("div", "album-grid");
    for (const photo of album.media) {
      const tile = el("div", "album-photo-tile");
      const open = el("button", "album-photo");
      open.type = "button";
      open.setAttribute("aria-label", photo.type === "video" ? `Open video ${photo.name}` : `View ${photo.name}`);
      const image = el("img");
      image.alt = photo.name;
      image.loading = "lazy";
      image.decoding = "async";
      image.src = albumImage(item.album.id, photo, "small");
      open.append(image);
      if (photo.type === "video") {
        open.append(el("span", "album-video-badge", "Video"));
        open.addEventListener("click", () => openAttachment({name: photo.name}, albumImage(item.album.id, photo, "original")));
      } else {
        open.addEventListener("click", () => openImageViewer(viewerImages[photos.indexOf(photo)], viewerImages));
      }
      tile.append(open);
      const badges = el("div", "album-tag-badges");
      tile.append(badges);
      tiles.set(photo, tile);
      paintTile(photo);
      grid.append(tile);
    }
    section.append(grid);
  } catch (error) {
    section.replaceChildren(el("h2", "", "Photos"), el("p", "notice", error.message));
  }
}
const readRequests = new Set();
async function markOpenedRead(item, page) {
  if (!item.unread || item.content_type !== "Message" || !item.id.startsWith("thread:") || readRequests.has(item.id)) return;
  readRequests.add(item.id);
  try {
    const response = await fetch("api/feed/read", {
      method: "POST", headers: {"Content-Type": "application/json", "X-Aula-Request": "settings"},
      body: JSON.stringify({id: item.id}),
    });
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || "Could not mark as read in Aula");
    for (const entry of state.items) if (entry.id === item.id) entry.unread = false;
    page.querySelectorAll(".unread-dot").forEach(dot => dot.remove());
  } catch (error) {
    console.warn("Mark read failed", error.message);
  }
}
async function route(restoreFromHistory = false) {
  const key = keyFromLocation();
  if (!key || key.startsWith("child/") || ["calendar", "saved"].includes(key)) {
    renderListRoute(key, restoreFromHistory);
    return;
  }
  document.querySelector("#page").hidden = false;
  document.querySelector("#timeline").hidden = true;
  document.querySelector("#calendar-side").hidden = true;
  document.body.classList.remove("split-calendar");
  document.body.classList.remove("calendar-collapsed");
  const changed = activeRoute !== key;
  activeRoute = key;
  if (changed) window.scrollTo(0, 0);
  if (key.startsWith("post/") || key.startsWith("thread/")) {
    let id;
    try { id = decodeURIComponent(key.slice(key.indexOf("/") + 1)); }
    catch (error) { pageShell("Invalid post link"); return; }
    const item = state.items.find(post => post.id === id);
    // Polls re-render the current route: keep an unchanged detail page (open originals, album
    // tagging, "Learn more" state, scroll) instead of rebuilding it and refetching its media.
    const signature = JSON.stringify([state.loaded, item ? {...item, unread: undefined} : null]);
    if (!changed && signature === detailSignature) return;
    detailSignature = signature;
    const openOriginals = new Set(changed ? [] : [...document.querySelectorAll("#page .thread-message")]
      .filter(entry => entry.querySelector(".thread-original[open]")).map(entry => entry.dataset.messageId));
    if (!changed) {
      const keepY = window.scrollY;
      requestAnimationFrame(() => window.scrollTo(0, keepY));
    }
    const page = pageShell(item ? itemTitle(item) : state.loaded ? "Not found" : "Loading…", isCalendarItem(item || {}) ? "#calendar" : "#", true);
    if (!item) {
      if (!state.loaded) { page.append(el("p", "profile-status", "Loading your feed…")); return; }
      page.append(el("p", "", "We couldn't find this post or conversation. It may be older than the feed history, or the link may be wrong."));
      const home = el("a", "action", "Open feed");
      home.href = "#";
      page.append(home);
      return;
    }
    const metadata = card(item, {detail: true});
    metadata.querySelector(".post-actions .action").remove();
    page.append(metadata);
    markOpenedRead(item, page);
    markNotificationOpened(item.id);
    if (item.content_type === "Album" && item.album) albumSection(item, page);
    relatedReading(item, page);
    if (item.content_type === "Post" || item.attachments?.length) {
      const media = el("div", "detail-media");
      media.append(attachmentSection(item.attachments, item));
      page.append(media);
      if (item.content_type === "Post") postAttachments(item, media);
    }
    if (item.recognized_events?.length) {
      const section = el("section", "detail-body related-calendar");
      section.append(el("h2", "", "Related calendar events"), eventList(item.recognized_events));
      page.append(section);
    }
    if (item.thread_messages?.length) {
      const thread = el("section", "detail-body thread");
      thread.append(el("h2", "", "Conversation"));
      const messages = newestMessages(item.thread_messages);
      const entries = el("div", "chat-messages");
      const more = el("button", "load-more conversation-more");
      more.type = "button";
      let rendered = 0;
      function appendMessages(limit) {
        messages.slice(rendered, limit).forEach((message, offset) => {
          const index = rendered + offset;
          const entry = el("article", `thread-message${sameMessageGroup(messages[index - 1], message) ? " consecutive" : ""}`);
          entry.dataset.messageId = message.id || String(index);
          const sender = message.sender || "Unknown sender";
          const initials = sender.split(/\s+/).filter(Boolean).slice(0, 2).map(word => word[0]).join("");
          const avatar = el("span", "chat-avatar", initials);
          avatar.setAttribute("aria-hidden", "true");
          const photo = authorPhoto(message.sender, "chat-photo");
          if (photo) {
            avatar.textContent = "";
            avatar.classList.add("has-photo");
            photo.addEventListener("error", () => { avatar.classList.remove("has-photo"); avatar.textContent = initials; });
            avatar.append(photo);
          }
          const bubble = el("div", "chat-bubble");
          const header = el("header", "chat-header");
          header.append(el("h3", "", sender));
          const time = el("time", "", timeLabel(message.date));
          if (message.date) time.dateTime = message.date;
          header.append(time);
          bubble.append(header);
          const translated = threadTranslationStatus(message, item);
          if (translated.label) bubble.append(el("span", "chat-language", translated.label));
          bubble.append(richText(chatParagraphs(translated.text),
            `thread-translation${translated.label ? "" : " unavailable"}`));
          if (message.attachments?.length) bubble.append(attachmentSection(message.attachments, item, message));
          const original = el("details", "thread-original");
          original.open = openOriginals.has(entry.dataset.messageId);
          original.append(el("summary", "", "Show original message"),
            richText(chatParagraphs(message.original_text || "No original message text provided."), "original"));
          bubble.append(original);
          entry.append(avatar, bubble);
          entries.append(entry);
        });
        rendered = Math.min(limit, messages.length);
        more.hidden = rendered >= messages.length;
        more.textContent = `Show ${Math.min(10, messages.length - rendered)} more messages`;
      }
      more.addEventListener("click", () => {
        const limit = rendered + 10;
        conversationLimits.set(item.id, limit);
        appendMessages(limit);
      });
      appendMessages(conversationLimits.get(item.id) || 10);
      thread.append(entries, more);
      if (item.translation_pending && !item.thread_messages.some(message => message.translated_text)) {
        thread.append(el("p", "profile-status", "Thread translation is still pending."));
      }
      if (item.translation_error) thread.append(el("p", "notice", `Thread translation unavailable: ${item.translation_error}`));
      page.append(thread);
    }
    if (item.content_type !== "Message") {
      const original = el("section", "detail-body");
      original.append(el("h2", "", "Original source"),
        el("h3", "", `Original title: ${item.title || "Untitled"}`),
        el("div", "original", item.original_text || "No additional text provided."),
        el("p", "detail-source", `${item.sender || "Unknown"} · ${timeLabel(item.date)}`));
      page.append(original);
    }
    if (item.calendar_event) {
      const event = item.calendar_event;
      const link = el("a", "page-back", "Read original source post");
      link.href = `#post/${encodeURIComponent(event.source_id)}`;
      if (event.source_id && event.source_id !== item.id && event.origin !== "parent") page.append(link);
    }
    return;
  }
  if (key === "history") {
    pageShell("Notification history");
    renderHistory();
    refreshBell();
    return;
  }
  if (key === "attention") {
    const page = pageShell("Requires attention");
    const events = attentionEvents();
    if (events.length) page.append(eventList(events));
    else page.append(el("p", "", "Nothing needs your attention."));
    return;
  }
  // Leave a settings form alone during feed polling so unsaved edits survive.
  if (!changed) return;
  const generation = ++routeGeneration;
  const page = pageShell(key === "profiles" || key.startsWith("profile/") ? "Settings" : "Calendar subscription");
  page.append(el("p", "profile-status", "Loading settings…"));
  try {
    const response = await fetch("api/profiles", {cache: "no-store"});
    if (!response.ok) throw new Error(`Settings request failed (${response.status})`);
    const data = await response.json();
    if (generation !== routeGeneration || keyFromLocation() !== key) return;
    if (key.startsWith("event/")) {
      const uid = decodeURIComponent(key.slice(6));
      const event = [...(data.events || []), ...(data.recommendations || [])].find(event => event.uid === uid);
      if (!event) { pageShell("Event unavailable", "#calendar"); return; }
      const container = pageShell(event.review_required ? "Review calendar suggestion" : "Calendar event details", "#calendar", true);
      if (event.review_required) {
        const details = el("details", "review-context");
        details.append(el("summary", "", "Review details"),
          el("p", "", `${event.issue || "Check the date against the source."}${event.recommendation ? `\n${event.recommendation}` : ""}`));
        container.append(details);
      }
      container.append(calendarEvidence(event, state.items.find(item => item.id === event.source_id)));
      const calendarItem = state.items.find(item => item.calendar_event?.uid === uid);
      if (calendarItem) container.append(saveAction(calendarItem));
      if (event.origin === "parent") {
        container.prepend(el("h2", "", event.title),
          el("p", "calendar-event-date", calendarFullDateLabel(event, calendarItem || {})));
        container.append(el("p", "profile-status",
          "This records the date reported to Aula. It does not mark your child sick on any other day."));
        return;
      }
      const form = el("form", "profile-form page-panel");
      const fields = {};
      const dateLabels = {};
      const timeZone = data.children.find(child => (event.child_slugs || []).includes(child.slug))?.timezone
        || "Europe/Copenhagen";
      const allDay = el("input"); allDay.type = "checkbox"; allDay.checked = event.all_day;
      const dayLabel = el("label", "check-label");
      dayLabel.append(allDay, el("span", "", "All-day event")); form.append(dayLabel);
      for (const [name, labelText] of [["title", "Event title"], ["start", "Starts"],
        ["end", "Ends (optional)"], ["location", "Location"]]) {
        const label = el("label");
        const caption = el("span", "", labelText);
        const input = el("input"); input.type = "text"; input.name = name;
        if (["start", "end"].includes(name)) {
          input.type = allDay.checked ? "date" : "datetime-local";
          input.step = "1";
          input.value = eventInputValue(event[name], allDay.checked, timeZone);
          if (name === "end" && allDay.checked && input.value) input.value = shiftDateKey(input.value, -1);
          dateLabels[name] = caption;
        } else input.value = event[name] || "";
        input.required = ["title", "start"].includes(name);
        label.append(caption, input); form.append(label); fields[name] = input;
      }
      const dateHelp = el("p", "profile-status");
      const updateDateLabels = () => {
        dateLabels.start.textContent = allDay.checked ? "First day" : "Starts";
        dateLabels.end.textContent = allDay.checked ? "Last day (optional)" : "Ends (optional)";
        dateHelp.textContent = allDay.checked
          ? "Include the last day of the event. Leave it blank for a one-day event."
          : `Choose a date and time. Times are shown in ${timeZone}.`;
        fields.end.min = fields.start.value;
      };
      const clocks = {start: "T09:00:00", end: "T10:00:00"};
      allDay.addEventListener("change", () => {
        for (const name of ["start", "end"]) {
          const input = fields[name];
          const day = input.value.slice(0, 10);
          if (allDay.checked && input.value) clocks[name] = input.value.slice(10);
          input.type = allDay.checked ? "date" : "datetime-local";
          input.step = "1";
          input.value = day ? day + (allDay.checked ? "" : clocks[name]) : "";
        }
        updateDateLabels();
      });
      fields.start.addEventListener("change", updateDateLabels);
      updateDateLabels();
      form.append(dateHelp);
      const descriptionLabel = el("label", "", "Event description / notes");
      const description = el("textarea");
      description.value = event.description || "";
      description.rows = 5;
      descriptionLabel.append(description); form.append(descriptionLabel);
      const status = el("p", "profile-status");
      const submit = el("button", "action", event.review_required ? "Accept corrected event" : "Save changes");
      submit.type = "submit"; form.append(submit, status);
      form.addEventListener("submit", async actionEvent => {
        actionEvent.preventDefault(); submit.disabled = true;
        try {
          await calendarRequest("api/calendar/review", {
            uid, action: "edit", changes: {
              title: fields.title.value,
              ...eventInputDates(fields.start.value, fields.end.value, allDay.checked, event, timeZone),
              location: fields.location.value, all_day: allDay.checked, description: description.value,
            },
          });
          navigate("calendar");
          activeRoute = "";
          await load({force: true});
        } catch (error) { status.textContent = error.message; }
        finally { submit.disabled = false; }
      });
      container.append(form);
      if (event.review_required) container.append(...calendarActions(event, {edit: false}));
      return;
    }
    if (key.startsWith("profile/")) {
      const profile = data.children.find(child => child.slug === key.slice(8));
      if (!profile) { pageShell("Unknown child", "#profiles"); return; }
      const container = pageShell(`${profile.display_name} · Settings`, "#profiles");
      container.append(el("p", "profile-status", `Aula identity: ${profile.aula_name}. Display settings never change source attribution.`));
      const form = el("form", "profile-form page-panel");
      function field(text, input) {
        const label = el("label", "", text);
        label.append(input);
        form.append(label);
      }
      const name = el("input"); name.type = "text"; name.required = true; name.maxLength = 80; name.value = profile.display_name;
      field("Display name", name);
      const institution = el("select");
      ["school", "daycare"].forEach(value => {
        const option = el("option", "", value === "school" ? "School" : "Daycare");
        option.value = value; institution.append(option);
      });
      institution.value = profile.institution_type;
      field("Institution type", institution);
      const enabled = el("input"); enabled.type = "checkbox"; enabled.checked = profile.calendar_enabled;
      const checkbox = el("label", "check-label");
      checkbox.append(enabled, el("span", "", "Include recognized events in calendar subscription"));
      form.append(checkbox);
      const status = el("p", "profile-status");
      const submit = el("button", "action", "Save settings"); submit.type = "submit";
      form.append(submit, status);
      form.addEventListener("submit", async event => {
        event.preventDefault(); submit.disabled = true;
        try {
          const result = await fetch(`api/profiles/${profile.slug}`, {
            method: "POST", headers: {"Content-Type": "application/json", "X-Aula-Request": "settings"},
            body: JSON.stringify({display_name: name.value, institution_type: institution.value,
              calendar_enabled: enabled.checked}),
          });
          const payload = await result.json().catch(() => ({}));
          if (!result.ok) throw new Error(payload.error || "Settings save failed");
          status.textContent = "Saved. Calendar subscription updated.";
          await load({force: true});
        } catch (error) { status.textContent = error.message; }
        finally { submit.disabled = false; }
      });
      container.append(form);
      const calendarLink = el("a", "profile-link", `${profile.display_name}'s calendar ›`);
      calendarLink.href = "#calendar";
      calendarLink.addEventListener("click", () => { state.child = [profile.slug]; });
      container.append(calendarLink);
      return;
    }
    if (key === "profiles") {
      const container = pageShell("Settings");
      container.append(accountCard());
      const appearance = el("section", "page-panel appearance-settings");
      const label = el("label", "appearance-label", "Appearance");
      const select = el("select", "appearance-select");
      select.setAttribute("aria-label", "Appearance");
      [["system", "Automatic"], ["light", "Light"], ["dark", "Dark"]].forEach(([value, text]) => {
        const option = el("option", "", text);
        option.value = value;
        select.append(option);
      });
      select.value = window.AulaTheme.get();
      const status = el("p", "profile-status");
      status.setAttribute("role", "status");
      select.addEventListener("change", () => {
        status.textContent = window.AulaTheme.set(select.value)
          ? "" : "Appearance changed, but could not be saved on this device.";
      });
      label.append(select);
      appearance.append(label, status);
      container.append(appearance);
      data.children.forEach(profile => {
        const link = el("a", "profile-link");
        const who = el("span", "profile-who");
        const photo = childPhoto(profile.slug, "profile-photo");
        if (photo) who.append(photo);
        who.append(document.createTextNode(`${profile.display_name} · ${profile.institution_type}`));
        link.append(who, document.createTextNode("›"));
        link.href = `#profile/${profile.slug}`; container.append(link);
      });
      const link = el("a", "profile-link", "Calendar subscription ›");
      link.href = "#calendar-settings"; container.append(link);
      const login = el("a", "profile-link", "Aula account login ›");
      login.href = "account";
      container.append(login);
      const aiLink = el("a", "profile-link", "AI ›");
      aiLink.href = "#ai-settings";
      container.append(aiLink);
      const notifyLink = el("a", "profile-link", "Notifications ›");
      notifyLink.href = "#notifications";
      container.append(notifyLink);
      const mcpLink = el("a", "profile-link", "MCP connector ›");
      mcpLink.href = "#mcp-settings";
      container.append(mcpLink);
      const syncLink = el("a", "profile-link", "Sync ›");
      syncLink.href = "#sync-settings";
      container.append(syncLink);
      container.append(el("p", "app-version", `Aula UI version ${window.AulaVersion?.current || "unknown"}`));
      return;
    }
    if (key === "sync-settings") {
      const container = pageShell("Sync", "#profiles");
      const mount = el("section", "page-panel");
      container.append(mount);
      window.AulaSyncSettings?.render(mount);
      return;
    }
    if (key === "mcp-settings") {
      const container = pageShell("MCP connector", "#profiles");
      const panel = el("section", "page-panel");
      panel.append(el("h2", "", "Connect an MCP client"));
      panel.append(el("p", "profile-status",
        "The MCP server runs inside this add-on and uses the same Aula session and cached feed. " +
        "Requests are protected by Home Assistant ingress or Cloudflare Access; no connector key is issued."));
      if (data.mcp_url) {
        const label = el("label", "", "Cloudflare Access endpoint");
        const endpoint = el("input");
        endpoint.type = "url";
        endpoint.readOnly = true;
        endpoint.value = data.mcp_url;
        endpoint.setAttribute("aria-label", "Cloudflare Access endpoint");
        const copy = el("button", "action", "Copy endpoint");
        copy.type = "button";
        const status = el("p", "profile-status");
        status.setAttribute("role", "status");
        copy.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(endpoint.value);
            status.textContent = "Endpoint copied.";
          } catch {
            endpoint.select();
            status.textContent = "Select and copy the endpoint.";
          }
        });
        label.append(endpoint);
        panel.append(label, copy, status);
        panel.append(el("p", "profile-status",
          "Configure your MCP client to connect to this URL and complete the Cloudflare Access sign-in. " +
          "Keep the whole endpoint behind the existing Access application; do not add a bypass rule."));
      } else {
        panel.append(el("p", "profile-status",
          "No public URL is configured. The endpoint is /mcp inside this add-on's Home Assistant ingress; " +
          "use a client that can access an authenticated Home Assistant ingress session. To connect a remote " +
          "MCP client, configure this add-on's existing Cloudflare Access custom domain in App settings."));
      }
      panel.append(el("h3", "", "Available tools"));
      panel.append(el("p", "profile-status",
        "Read cached feed items and full threads, check calendar and presence, inspect albums and attachments, " +
        "view sickness status, and queue a refresh. Reporting or withdrawing sickness is an explicit write; " +
        "the client must ask for your confirmation and the add-on checks Aula permissions and today's date."));
      container.append(panel);
      return;
    }
    if (key === "notifications") {
      const container = pageShell("Notifications", "#profiles");
      const mount = el("section", "page-panel");
      container.append(mount);
      window.AulaNotificationSettings?.render(mount);
      return;
    }
    if (key === "ai-settings") {
      const container = pageShell("AI", "#profiles");
      const aiMount = el("section", "page-panel");
      container.append(aiMount);
      window.AulaAISettings?.render(aiMount);
      return;
    }
    if (key !== "calendar-settings") { pageShell("Page not found"); return; }
    const container = pageShell("Calendar subscription", "#profiles");
    const calendar = el("section", "page-panel");
    const countText = el("p", "calendar-count", calendarCountText(data.calendar_count));
    calendar.append(el("h2", "", "Calendar subscription"), countText);
    if (!data.extraction_available) calendar.append(el("p", "notice", "Event recognition requires an available Home Assistant AI Task. Configure it under Settings → AI."));
    if (data.recognition_warnings?.length) {
      calendar.append(el("p", "notice", `${data.recognition_warnings.length} source(s) have date-recognition warnings. Review suggestions directly from Calendar; original source posts remain available.`));
      const omitted = el("ul", "event-list");
      data.recognition_warnings.forEach(item => {
        const row = el("li");
        const link = el("a", "page-back", item.title);
        link.href = `#post/${encodeURIComponent(item.id)}`;
        row.append(link);
        omitted.append(row);
      });
      calendar.append(omitted);
    }
    if (data.calendar_error) calendar.append(el("p", "notice", data.calendar_error));
    const url = data.calendar_url || new URL(data.calendar_path, location.origin).href;
    const input = el("input", "calendar-url"); input.type = "text"; input.readOnly = true; input.value = url; input.setAttribute("aria-label", "Private calendar subscription URL");
    calendar.append(input, el("p", "profile-status", "Anyone with this secret link can read the events. Keep it private. Use Subscribe by URL in your calendar app; importing the file once will not receive updates."));
    const copy = action("saved", "Copy subscription URL", async () => {
      try { await navigator.clipboard.writeText(url); copy.querySelector("span").textContent = "Copied"; }
      catch (error) { input.select(); setNotice("Could not copy automatically. Select and copy the subscription URL."); }
    });
    const download = el("a", "action", "Download ICS"); download.href = "calendar.ics"; download.download = "aula-family.ics";
    calendar.append(copy, download);
    const rotate = action("refresh", "Revoke & replace link", async () => {
      if (!confirm("Revoke the existing calendar subscription URL? You will need to update it in every calendar app.")) return;
      rotate.disabled = true;
      try {
        const response = await fetch("api/calendar/rotate", {method: "POST", headers: {"X-Aula-Request": "settings"}});
        if (!response.ok) throw new Error("Could not revoke calendar URL");
        activeRoute = "";
        await route();
      } catch (error) { setNotice(error.message); rotate.disabled = false; }
    });
    calendar.append(rotate);
    container.append(calendar, calendarCategoryPanel(data.calendar_categories || [], countText));
    const activeLink = el("a", "profile-link", "View calendar events ›");
    activeLink.href = "#calendar"; container.append(activeLink);
  } catch (error) {
    if (generation === routeGeneration) pageShell("Settings unavailable").append(el("p", "notice", error.message));
  }
}
function calendarCountText(count) {
  return `${count} event${count === 1 ? "" : "s"} in the subscription.`;
}
function calendarCategoryPanel(categories, countText) {
  const panel = el("section", "page-panel calendar-categories");
  panel.append(el("h2", "", "Event categories"),
    el("p", "profile-status", "Untick a category to leave it out of the subscription. Events stay visible in the Calendar tab."));
  const status = el("p", "profile-status");
  status.setAttribute("role", "status");
  if (!categories.length) {
    panel.append(el("p", "profile-status", "No calendar events yet."));
    return panel;
  }
  const options = el("div", "type-options");
  const boxes = categories.map(category => {
    const label = el("label", "type-option");
    const checkbox = el("input");
    checkbox.type = "checkbox";
    checkbox.value = category.key;
    checkbox.checked = !category.excluded;
    label.append(checkbox, el("span", "", `${category.label} (${category.count})`));
    options.append(label);
    return checkbox;
  });
  let saving = Promise.resolve();
  boxes.forEach(checkbox => checkbox.addEventListener("change", () => {
    const excluded = boxes.filter(box => !box.checked).map(box => box.value);
    status.textContent = "Saving…";
    saving = saving.then(async () => {
      try {
        const response = await fetch("api/calendar/categories", {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-Aula-Request": "settings"},
          body: JSON.stringify({excluded}),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.error || "Could not save calendar categories");
        countText.textContent = calendarCountText(result.calendar_count);
        status.textContent = excluded.length
          ? `Saved. ${excluded.length} categor${excluded.length === 1 ? "y" : "ies"} excluded. Calendar apps pick this up on their next refresh.`
          : "Saved. All categories are included.";
      } catch (error) {
        checkbox.checked = !checkbox.checked;
        status.textContent = "";
        setNotice(error.message);
      }
    });
  }));
  panel.append(options, status);
  return panel;
}
