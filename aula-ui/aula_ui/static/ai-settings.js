"use strict";
(function () {
  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  async function post(body) {
    const response = await fetch("api/ai-settings", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Aula-Request": "settings"},
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Saving failed (${response.status})`);
    return result;
  }

  function credentialForm(container, data, source, selected, onDone) {
    const form = node("form", "ai-credential-form");
    const fields = {};
    const add = (name, label, type, value, placeholder) => {
      const wrap = node("label", "", label);
      const input = node("input");
      input.type = type;
      input.value = value || "";
      input.placeholder = placeholder || "";
      input.autocomplete = "off";
      input.setAttribute("aria-label", `${source.label} ${label}`);
      fields[name] = input;
      wrap.append(input);
      form.append(wrap);
    };
    if (source.id === "cloudflare") add("account_id", "Account ID", "text", source.account_id);
    add("api_key", source.id === "cloudflare" ? "API token" : "API key", "password", "",
      source.configured ? "Saved — leave blank to keep" : "");
    add("model", "Model", "text", source.model);
    const save = node("button", "action", "Save key");
    save.type = "submit";
    const remove = node("button", "action", "Remove key");
    remove.type = "button";
    remove.hidden = !source.stored;
    const status = node("p", "profile-status ai-credential-status");
    status.setAttribute("role", "status");
    form.append(save, remove, status);
    const send = async body => {
      for (const element of form.querySelectorAll("button, input")) element.disabled = true;
      status.textContent = "Saving…";
      try {
        onDone(await post({ai_credential: {source: source.id, ...body}}));
      } catch (error) {
        for (const element of form.querySelectorAll("button, input")) element.disabled = false;
        status.textContent = error.message;
        status.setAttribute("role", "alert");
      }
    };
    form.addEventListener("submit", event => {
      event.preventDefault();
      event.stopPropagation();
      const body = {model: fields.model.value};
      if (fields.api_key.value) body.api_key = fields.api_key.value;
      if (fields.account_id) body.account_id = fields.account_id.value;
      send(body);
    });
    remove.addEventListener("click", () => send({clear: true}));
    return form;
  }

  function fill(container, data, draft) {
    container.replaceChildren();
    container.append(node("h2", "", "AI providers"));
    container.append(node("p", "profile-status",
      "One ordered list of everything that can summarise and translate. Add Home Assistant AI tasks, " +
      "your own Anthropic or Google key, or Cloudflare Workers AI. Each request starts at the top; " +
      "if it fails, the next provider is tried. Content is sent to the provider that handles it."));
    let selected = [...(draft || data.providers || data.ai_task_entities || [])];
    const sources = data.sources || [];
    const sourceById = id => sources.find(value => value.id === id);
    const open = new Set(selected.filter(id => sourceById(id) && !sourceById(id).configured));
    const form = node("form", "profile-form");
    const list = node("ol", "ai-priority-list");
    const label = node("label", "", "Add provider");
    const select = node("select", "ai-settings-select");
    select.setAttribute("aria-label", "Add provider");
    const add = node("button", "action", "Add");
    add.type = "button";
    const defaultNote = node("p", "profile-status");
    const status = node("p", "profile-status ai-settings-status", data.error || "");
    status.setAttribute("role", data.error ? "alert" : "status");
    const save = node("button", "action", "Save providers");
    save.type = "submit";
    function describe(id) {
      const source = sourceById(id);
      if (source) {
        return {title: `${source.label}${source.configured ? "" : " — key needed"}`,
          detail: source.configured ? source.model : "Set up the key below", source};
      }
      const entity = (data.entities || []).find(value => value.entity_id === id);
      return {title: entity ? `${entity.name}${entity.available ? "" : " — unavailable"}` : `${id} — missing`,
        detail: id};
    }
    function draw() {
      list.replaceChildren();
      selected.forEach((id, index) => {
        const info = describe(id);
        const row = node("li", "ai-priority-item");
        const name = node("div", "ai-priority-name");
        name.append(node("strong", "", `${index + 1}. ${info.title}`), node("small", "", info.detail),
          node("small", "", index === 0 ? "Primary" : `Fallback ${index}`));
        row.append(name);
        const controls = [
          ["↑", "Move up", index === 0, () => {
            [selected[index - 1], selected[index]] = [selected[index], selected[index - 1]];
          }],
          ["↓", "Move down", index === selected.length - 1, () => {
            [selected[index + 1], selected[index]] = [selected[index], selected[index + 1]];
          }],
          ["Remove", "Remove", false, () => selected.splice(index, 1)],
        ];
        if (info.source) {
          controls.push([open.has(id) ? "Hide key" : "Key", "Key settings for", false,
            () => { open.has(id) ? open.delete(id) : open.add(id); }]);
        }
        for (const [text, action, disabled, change] of controls) {
          const button = node("button", "action", text);
          button.type = "button";
          button.disabled = disabled;
          button.setAttribute("aria-label", `${action} ${id}`);
          button.addEventListener("click", () => { change(); draw(); });
          row.append(button);
        }
        if (info.source && open.has(id)) {
          row.append(credentialForm(container, data, info.source, selected, result => fill(container, result, selected)));
        }
        list.append(row);
      });
      select.replaceChildren();
      const placeholder = node("option", "", "Choose a provider…");
      placeholder.value = "";
      select.append(placeholder);
      for (const source of sources) {
        if (selected.includes(source.id)) continue;
        const option = node("option", "", `${source.label}${source.configured ? "" : " — key needed"}`);
        option.value = source.id;
        select.append(option);
      }
      for (const entity of data.entities || []) {
        if (selected.includes(entity.entity_id)) continue;
        const option = node("option", "", `Home Assistant: ${entity.name} (${entity.entity_id})${entity.available ? "" : " — unavailable"}`);
        option.value = entity.entity_id;
        select.append(option);
      }
      add.disabled = select.options.length <= 1;
      defaultNote.textContent = selected.length
        ? "Changes apply after saving. Existing summaries and translations are retained."
        : "No explicit list: follows Home Assistant's default AI task, without additional fallbacks.";
    }
    add.addEventListener("click", () => {
      if (select.value && !selected.includes(select.value)) {
        selected.push(select.value);
        if (sourceById(select.value) && !sourceById(select.value).configured) open.add(select.value);
        draw();
      }
    });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const providers = [...selected];
      for (const element of form.querySelectorAll(":scope > button, :scope > label select")) element.disabled = true;
      status.textContent = "Saving…";
      status.setAttribute("role", "status");
      try {
        const result = await post({ai_providers: providers});
        fill(container, result);
        if (!result.error) container.querySelector(".ai-settings-status").textContent = "Saved. New AI requests follow this order.";
      } catch (error) {
        for (const element of form.querySelectorAll(":scope > button, :scope > label select")) element.disabled = false;
        draw();
        status.textContent = error.message;
        status.setAttribute("role", "alert");
      }
    });
    label.append(select);
    form.append(list, defaultNote, label, add, save, status);
    container.append(form, readingForm(container, data));
    draw();
  }

  function readingForm(container, data) {
    const form = node("form", "profile-form reading-search-form");
    form.append(node("h2", "", "Learn more web search"), node("p", "profile-status",
      "Choose an AI task with web search enabled in Home Assistant (for example Claude or OpenAI with “Web search” turned on). " +
      "Every returned link is checked before it is shown. Built-in search is often blocked by search engines."));
    const label = node("label", "", "AI task for Learn more");
    const select = node("select", "reading-entity-select");
    const builtin = node("option", "", "Built-in search (limited)");
    builtin.value = "";
    select.append(builtin);
    for (const entity of data.entities || []) {
      const option = node("option", "", `${entity.name} (${entity.entity_id})${entity.available ? "" : " — unavailable"}`);
      option.value = entity.entity_id;
      select.append(option);
    }
    select.value = data.reading_entity || "";
    const save = node("button", "action", "Save");
    save.type = "submit";
    const status = node("p", "profile-status reading-entity-status");
    status.setAttribute("role", "status");
    form.addEventListener("submit", async event => {
      event.preventDefault();
      select.disabled = save.disabled = true;
      status.textContent = "Saving…";
      try {
        const response = await fetch("api/ai-settings", {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-Aula-Request": "settings"},
          body: JSON.stringify({reading_entity: select.value}),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `Saving failed (${response.status})`);
        fill(container, result);
        container.querySelector(".reading-entity-status").textContent = "Saved. New lookups use this choice.";
      } catch (error) {
        select.disabled = save.disabled = false;
        status.textContent = error.message;
        status.setAttribute("role", "alert");
      }
    });
    label.append(select);
    form.append(label, save, status);
    return form;
  }

  async function render(container) {
    container.replaceChildren(node("p", "profile-status", "Loading AI settings…"));
    try {
      const response = await fetch("api/ai-settings", {cache: "no-store"});
      if (!response.ok) throw new Error(`AI settings request failed (${response.status})`);
      fill(container, await response.json());
    } catch (error) {
      const status = node("p", "profile-status ai-settings-status", error.message);
      status.setAttribute("role", "alert");
      container.replaceChildren(status);
    }
  }

  window.AulaAISettings = {render};
})();
