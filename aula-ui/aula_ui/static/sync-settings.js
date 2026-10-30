"use strict";
(function () {
  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  const NUMBERS = [
    ["full_sync_minutes", "Full sync", "minutes",
      "Re-reads everything due below and retries failed items. Messages and check-ins do not wait for this."],
    ["posts_minutes", "Posts", "minutes", ""],
    ["calendar_minutes", "Aula calendar and EasyIQ planning", "minutes", ""],
    ["albums_minutes", "Photo albums", "minutes", ""],
  ];

  function numberField(key, label, unit, help, data) {
    const wrap = node("label", "", `${label} (${unit})`);
    const input = node("input");
    input.type = "number";
    input.inputMode = "numeric";
    input.name = key;
    input.min = data.limits[key].min;
    input.max = data.limits[key].max;
    input.step = 1;
    input.value = data.values[key];
    input.required = true;
    wrap.append(input);
    if (help) wrap.append(node("span", "profile-status", help));
    return wrap;
  }

  function hourSelect(key, data, ends) {
    const select = node("select");
    select.name = key;
    const {min, max} = data.limits[key];
    for (let hour = min; hour <= max; hour += 1) {
      const option = node("option", "", ends && hour === 24 ? "24:00 (midnight)" : `${String(hour).padStart(2, "0")}:00`);
      option.value = String(hour);
      select.append(option);
    }
    select.value = String(data.values[key]);
    return select;
  }

  function checkbox(key, label, data) {
    const wrap = node("label", "check-label");
    const input = node("input");
    input.type = "checkbox";
    input.name = key;
    input.checked = data.values[key];
    wrap.append(input, node("span", "", label));
    return wrap;
  }

  function windowRow(prefix, label, data) {
    const row = node("div", "sync-window");
    const from = hourSelect(`${prefix}_from_hour`, data, false);
    const to = hourSelect(`${prefix}_to_hour`, data, true);
    from.setAttribute("aria-label", `${label} from`);
    to.setAttribute("aria-label", `${label} until`);
    row.append(node("span", "", "Between"), from, node("span", "", "and"), to);
    return row;
  }

  function collect(form, data) {
    const value = {};
    for (const key of Object.keys(data.values)) {
      const input = form.elements[key];
      if (!input) continue;
      value[key] = input.type === "checkbox" ? input.checked : Number(input.value);
    }
    return value;
  }

  function fill(container, data) {
    const form = node("form", "profile-form sync-settings");
    form.append(node("h2", "", "Near real-time"),
      node("p", "profile-status",
        "Between full syncs, one small Aula request each checks for new messages and check-in, pick-up and nap changes. A new message starts a full sync straight away."));
    const quick = node("label", "", "Check every (seconds)");
    const quickInput = node("input");
    Object.assign(quickInput, {type: "number", name: "quick_check_seconds", step: 1, required: true,
      min: data.limits.quick_check_seconds.min, max: data.limits.quick_check_seconds.max,
      value: data.values.quick_check_seconds, inputMode: "numeric"});
    quick.append(quickInput);
    form.append(quick,
      checkbox("quick_messages", "Messages", data), windowRow("messages", "Messages", data),
      checkbox("quick_presence", "Check-in and naps", data), windowRow("presence", "Check-in and naps", data),
      checkbox("presence_weekdays_only", "Check-in and naps on weekdays only", data));
    form.append(node("h2", "", "Slower content"));
    for (const [key, label, unit, help] of NUMBERS) form.append(numberField(key, label, unit, help, data));
    const estimate = node("p", "profile-status sync-estimate");
    const showEstimate = result => {
      estimate.textContent = `About ${result.estimate} Aula requests per hour at peak; the add-on never exceeds ${result.budget}.`;
    };
    showEstimate(data);
    const save = node("button", "action", "Save sync settings");
    save.type = "submit";
    const reset = node("button", "action", "Restore defaults");
    reset.type = "button";
    const status = node("p", "profile-status");
    status.setAttribute("role", "status");
    form.append(estimate,
      node("p", "profile-status",
        "Shorter intervals make Aula more likely to limit requests. If Aula does, the add-on pauses automatically and keeps showing cached content."),
      save, reset, status);

    async function send(body) {
      for (const element of form.querySelectorAll("button, input, select")) element.disabled = true;
      status.textContent = "Saving…";
      status.setAttribute("role", "status");
      try {
        const response = await fetch("api/sync-settings", {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-Aula-Request": "settings"},
          body: JSON.stringify(body),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `Saving failed (${response.status})`);
        fill(container, result).textContent = "Saved. A sync starts now.";
      } catch (error) {
        for (const element of form.querySelectorAll("button, input, select")) element.disabled = false;
        status.textContent = error.message;
        status.setAttribute("role", "alert");
      }
    }
    form.addEventListener("submit", event => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      send(collect(form, data));
    });
    reset.addEventListener("click", () => send({...data.defaults}));
    container.replaceChildren(form);
    return status;
  }

  async function render(container) {
    container.replaceChildren(node("p", "profile-status", "Loading sync settings…"));
    try {
      const response = await fetch("api/sync-settings", {cache: "no-store"});
      if (!response.ok) throw new Error(`Sync settings request failed (${response.status})`);
      fill(container, await response.json());
    } catch (error) {
      const status = node("p", "profile-status", error.message);
      status.setAttribute("role", "alert");
      container.replaceChildren(status);
    }
  }

  window.AulaSyncSettings = {render};
})();
