"use strict";
(function () {
  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function checkboxGroup(legend, help, options, checked) {
    const fieldset = node("fieldset", "notification-group");
    fieldset.append(node("legend", "", legend));
    if (help) fieldset.append(node("p", "profile-status", help));
    const list = node("div", "type-options");
    for (const option of options) {
      const label = node("label", "type-option");
      const input = node("input");
      input.type = "checkbox";
      input.value = option.value;
      input.checked = checked.includes(option.value);
      input.disabled = Boolean(option.disabled && !input.checked);
      label.append(input, node("span", "", option.label));
      list.append(label);
    }
    fieldset.append(list);
    return fieldset;
  }

  function categoryOptions(data) {
    const labels = {post: "Posts", message: "Messages", calendar: "Aula events",
      lesson: "Lessons", homework: "Homework", weekplan: "Week plans", suggestion: "AI suggestions", nap: "Naps"};
    return (data.available_categories || []).map(category => ({
      value: category.key, label: labels[category.key] || category.label,
    }));
  }

  function values(group) {
    return [...group.querySelectorAll("input:checked")].map(input => input.value);
  }

  async function postJSON(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Aula-Request": "settings"},
      body: JSON.stringify(body),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || result.errors?.join("; ") || `Request failed (${response.status})`);
    return result;
  }

  const standalone = () => window.matchMedia?.("(display-mode: standalone)").matches || navigator.standalone === true;
  const pushSupported = () => window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

  function deviceName() {
    const ua = navigator.userAgent;
    if (/iPhone/.test(ua)) return "iPhone";
    if (/iPad/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) return "iPad";
    if (/Android/.test(ua)) return "Android";
    if (/Mac/.test(ua)) return "Mac";
    if (/Windows/.test(ua)) return "Windows PC";
    return "This device";
  }

  async function deviceId(endpoint) {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint));
    return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("").slice(0, 16);
  }

  function keyBytes(base64) {
    const raw = atob((base64 + "=".repeat((4 - base64.length % 4) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, char => char.charCodeAt(0));
  }

  async function registration() {
    const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error("The app's service worker is not active yet; reload and try again.")), 8000));
    return Promise.race([navigator.serviceWorker.ready, timeout]);
  }

  async function fillPush(mount, data, message) {
    const push = data.push || {devices: []};
    mount.replaceChildren(node("h3", "", "This device (app notifications)"));
    const status = node("p", "profile-status push-status", message || "");
    status.setAttribute("role", "status");
    const fail = text => { status.textContent = text; status.setAttribute("role", "alert"); };
    const refresh = (snapshot, text) => fillPush(mount, {...data, push: snapshot}, text);
    let current = null;
    let subscription = null;
    if (!pushSupported()) {
      const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
      mount.append(node("p", "profile-status", ios && !standalone()
        ? "Add Aula to your Home Screen (Share → Add to Home Screen) and open it from there to get notifications directly on this device."
        : "This browser does not support app notifications. Use Home Assistant notifications below."));
    } else if (!push.public_key) {
      mount.append(node("p", "profile-status", "App notifications are unavailable; check the add-on log."));
    } else {
      try {
        subscription = await (await registration()).pushManager.getSubscription();
        const id = subscription ? await deviceId(subscription.endpoint) : "";
        current = push.devices.find(device => device.id === id) || null;
      } catch (error) {
        fail(error.message);
      }
      if (!standalone() && !current) mount.append(node("p", "profile-status", "Works best when Aula is installed to your home screen."));
      if (Notification.permission === "denied") {
        mount.append(node("p", "profile-status", "Notifications are blocked for Aula in this device's settings. Allow them there, then reload."));
      }
      const form = node("form", "profile-form push-form");
      const nameLabel = node("label", "push-name");
      const name = node("input");
      name.type = "text";
      name.maxLength = 60;
      name.value = current?.name || deviceName();
      nameLabel.append(node("span", "", "Device name"), name);
      const categories = checkboxGroup("Notify about", "",
        categoryOptions(data),
        current?.categories || data.categories || []);
      categories.classList.add("push-categories");
      const children = checkboxGroup("Children (all if none selected)", "",
        (data.available_children || []).map(child => ({value: child.slug, label: child.name})),
        current?.children || []);
      children.classList.add("push-children");
      const buttons = node("div", "post-actions");
      const save = node("button", "action", current ? "Save for this device" : "Turn on for this device");
      save.type = "submit";
      buttons.append(save);
      let test = null;
      let off = null;
      if (current) {
        test = node("button", "action", "Send test");
        test.type = "button";
        off = node("button", "action", "Turn off");
        off.type = "button";
        buttons.append(test, off);
      }
      form.append(nameLabel, categories, children, buttons);
      if (current?.last_error) form.append(node("p", "profile-status", `Last error: ${current.last_error}`));
      mount.append(form);
      const busy = async (text, job) => {
        for (const element of form.querySelectorAll("button, input")) element.disabled = true;
        status.textContent = text;
        status.setAttribute("role", "status");
        try { return await job(); } catch (error) { fail(error.message); return null; } finally {
          for (const element of form.querySelectorAll("button, input")) element.disabled = false;
        }
      };
      form.addEventListener("submit", async event => {
        event.preventDefault();
        const body = {name: name.value.trim() || deviceName(), categories: values(categories), children: values(children)};
        if (current) {
          const result = await busy("Saving…", () => postJSON("api/push/device", {id: current.id, ...body}));
          if (result) refresh(result, "Saved for this device.");
          return;
        }
        // Ask for permission first, straight from the tap, as iOS requires.
        const permission = await Notification.requestPermission();
        if (permission !== "granted") {
          fail("Notifications were not allowed. Allow them for Aula in this device's settings to continue.");
          return;
        }
        const result = await busy("Turning on…", async () => {
          const reg = await registration();
          let sub = await reg.pushManager.getSubscription();
          const key = keyBytes(push.public_key);
          const sameKey = sub?.options?.applicationServerKey
            && new Uint8Array(sub.options.applicationServerKey).join() === key.join();
          if (sub && !sameKey) { await sub.unsubscribe(); sub = null; }
          sub = sub || await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
          return postJSON("api/push/subscribe", {subscription: sub.toJSON(), ...body});
        });
        if (result) refresh(result, "Turned on. New Aula items will appear as notifications on this device.");
      });
      test?.addEventListener("click", async () => {
        const result = await busy("Sending test…", () => postJSON("api/push/test", {id: current.id}));
        if (result) { status.textContent = "Test sent — it should appear in a few seconds."; }
      });
      off?.addEventListener("click", async () => {
        const result = await busy("Turning off…", async () => {
          const snapshot = await postJSON("api/push/remove", {id: current.id});
          await subscription?.unsubscribe().catch(() => {});
          return snapshot;
        });
        if (result) refresh(result, "Turned off for this device.");
      });
    }
    const others = push.devices.filter(device => device.id !== current?.id);
    if (others.length) {
      const list = node("div", "push-devices");
      list.append(node("h4", "", current ? "Other devices" : "Devices with app notifications"));
      for (const device of others) {
        const row = node("div", "push-device");
        const labels = (device.categories || []).map(key =>
          (data.available_categories || []).find(category => category.key === key)?.label || key);
        const sent = device.last_sent ? `last sent ${new Date(device.last_sent).toLocaleString()}` : "nothing sent yet";
        row.append(node("strong", "", device.name),
          node("p", "profile-status", `${labels.join(", ") || "No topics"} · ${sent}${device.last_error ? ` · ${device.last_error}` : ""}`));
        const remove = node("button", "action", "Remove");
        remove.type = "button";
        remove.addEventListener("click", async () => {
          remove.disabled = true;
          try { refresh(await postJSON("api/push/remove", {id: device.id}), `Removed ${device.name}.`); } catch (error) {
            remove.disabled = false;
            fail(error.message);
          }
        });
        row.append(remove);
        list.append(row);
      }
      mount.append(list);
    }
    mount.append(status);
  }

  function fill(container, data, message) {
    container.replaceChildren();
    container.append(node("h2", "", "Notifications"),
      node("p", "profile-status", "Only new items are sent; existing feed items are not."));
    const pushMount = node("section", "push-settings");
    container.append(pushMount);
    fillPush(pushMount, data);

    let selected = [...(data.targets || [])];
    const known = id => (data.available_targets || []).find(target => target.id === id);
    const form = node("form", "profile-form notification-form");
    form.append(node("h3", "", "Home Assistant"));
    const list = node("ul", "notification-target-list");
    const addLabel = node("label", "notification-add", "Add notify target");
    const select = node("select", "notification-target-select");
    select.setAttribute("aria-label", "Add notify target");
    const add = node("button", "action", "Add");
    add.type = "button";
    const addRow = node("div", "notification-add-row");
    addLabel.append(select);
    addRow.append(addLabel, add);
    const categories = checkboxGroup("Notify about", "",
      categoryOptions(data),
      data.categories || []);
    categories.classList.add("notification-categories");
    const children = checkboxGroup("Children (all if none selected)", "",
      (data.available_children || []).map(child => ({value: child.slug, label: child.name})),
      data.children || []);
    children.classList.add("notification-children");
    const status = node("p", "profile-status notification-status", message || data.error || "");
    status.setAttribute("role", data.error && !message ? "alert" : "status");
    const last = data.status?.last_sent
      ? `Last sent ${new Date(data.status.last_sent).toLocaleString()}.` : "Nothing sent yet.";
    const history = node("p", "profile-status",
      `${last}${data.status?.last_error ? ` Last error: ${data.status.last_error}` : ""}`);
    const save = node("button", "action", "Save notifications");
    save.type = "submit";
    const test = node("button", "action", "Send test");
    test.type = "button";
    const buttons = node("div", "post-actions");
    buttons.append(save, test);
    form.append(list, addRow, categories, children, buttons, status, history);
    container.append(form);

    function draw() {
      list.replaceChildren();
      if (!selected.length) {
        list.append(node("li", "profile-status notification-empty", data.available_targets?.length
          ? "No Home Assistant targets selected."
          : "No notify targets found in Home Assistant."));
      }
      for (const id of selected) {
        const target = known(id);
        const row = node("li", "notification-target");
        const state = !target || target.missing ? "missing" : target.available ? "" : "unavailable";
        const name = node("div", "notification-target-name");
        name.append(node("strong", "", target ? `${target.name}${state ? " — " + state : ""}` : `${id} — missing`),
          node("small", "", target?.target || id));
        const remove = node("button", "action", "Remove");
        remove.type = "button";
        remove.setAttribute("aria-label", `Remove ${target?.name || id}`);
        remove.addEventListener("click", () => { selected = selected.filter(value => value !== id); draw(); });
        row.append(name, remove);
        list.append(row);
      }
      select.replaceChildren();
      const placeholder = node("option", "", "Choose a notify target…");
      placeholder.value = "";
      select.append(placeholder);
      for (const target of data.available_targets || []) {
        if (selected.includes(target.id) || !target.available) continue;
        const option = node("option", "", `${target.name} · ${target.target}`);
        option.value = target.id;
        select.append(option);
      }
      add.disabled = select.options.length <= 1;
    }
    add.addEventListener("click", () => {
      if (select.value && !selected.includes(select.value)) { selected.push(select.value); draw(); }
    });
    draw();

    async function send(url, body, busy) {
      const locked = [...form.querySelectorAll("button, input, select")].filter(element => !element.disabled);
      for (const element of locked) element.disabled = true;
      status.textContent = busy;
      status.setAttribute("role", "status");
      try {
        return await postJSON(url, body);
      } catch (error) {
        status.textContent = error.message;
        status.setAttribute("role", "alert");
        return null;
      } finally {
        for (const element of locked) if (element.isConnected) element.disabled = false;
        draw();
      }
    }
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const result = await send("api/notifications",
        {targets: [...selected], categories: values(categories), children: values(children)}, "Saving…");
      if (result) fill(container, result, result.targets.length
        ? "Saved. New items will be sent to the selected targets."
        : "Saved. Home Assistant notifications are off.");
    });
    test.addEventListener("click", async () => {
      if (!selected.length) {
        status.textContent = "Select at least one target to test.";
        status.setAttribute("role", "alert");
        return;
      }
      const result = await send("api/notifications/test", {targets: [...selected]}, "Sending test…");
      if (result) status.textContent = `Test sent to ${result.sent} target${result.sent === 1 ? "" : "s"}.`;
    });
  }

  async function render(container) {
    container.replaceChildren(node("p", "profile-status", "Loading notification settings…"));
    try {
      const response = await fetch("api/notifications", {cache: "no-store"});
      if (!response.ok) throw new Error(`Notification settings request failed (${response.status})`);
      fill(container, await response.json());
    } catch (error) {
      const status = node("p", "profile-status notification-status", error.message);
      status.setAttribute("role", "alert");
      container.replaceChildren(status);
    }
  }

  window.AulaNotificationSettings = {render};
})();
