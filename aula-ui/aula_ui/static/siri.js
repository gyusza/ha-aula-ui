"use strict";
(() => {
  const node = (tag, text = "") => {
    const element = document.createElement(tag);
    element.textContent = text;
    return element;
  };
  async function render(mount) {
    mount.replaceChildren(node("h2", "Siri & Shortcuts"));
    mount.append(node("p", "Use personal Apple Shortcuts named after your voice commands. Siri runs the shortcut; Aula UI handles the action and returns text to speak."));
    const status = node("p");
    status.setAttribute("role", "status");
    mount.append(status);
    try {
      const response = await fetch("api/siri/settings", {cache: "no-store"});
      if (!response.ok) throw new Error("Could not load Siri settings");
      const settings = await response.json();
      const endpoint = node("input");
      endpoint.readOnly = true;
      endpoint.value = settings.endpoint;
      endpoint.setAttribute("aria-label", "Siri endpoint");
      mount.append(node("h3", "Connector URL"), endpoint);
      mount.append(node("p", "Calls must use HTTPS and an Authorization header: Bearer YOUR_KEY. The key can read your children's post summaries and report sickness today. Never put it in a URL or share a shortcut containing it."));
      mount.append(node("p", "If the URL redirects to Cloudflare sign-in, a path-specific Access bypass is required for /api/siri/command only. The connector still requires its own key; do not bypass the whole app."));
      const state = node("p", settings.enabled ? "Connector enabled." : "Connector disabled.");
      const keyMount = node("div");
      const generate = node("button", settings.enabled ? "Replace Siri key" : "Create Siri key");
      const revoke = node("button", "Disable Siri connector");
      generate.type = revoke.type = "button";
      generate.className = revoke.className = "action";
      revoke.disabled = !settings.enabled;
      async function change(action) {
        if (action === "generate" && settings.enabled
            && !window.confirm("Replace the Siri key? Existing shortcuts will stop working.")) return;
        generate.disabled = revoke.disabled = true;
        status.textContent = "Saving…";
        try {
          const response = await fetch("api/siri/settings", {
            method: "POST",
            headers: {"Content-Type": "application/json", "X-Aula-Request": "settings"},
            body: JSON.stringify({action}),
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || "Could not save Siri settings");
          settings.enabled = result.enabled;
          state.textContent = result.enabled ? "Connector enabled." : "Connector disabled. Previous keys no longer work.";
          generate.textContent = result.enabled ? "Replace Siri key" : "Create Siri key";
          keyMount.replaceChildren();
          if (result.key) {
            const key = node("input");
            key.type = "password";
            key.readOnly = true;
            key.value = result.key;
            key.autocomplete = "off";
            key.setAttribute("aria-label", "New Siri key (shown once)");
            const copy = node("button", "Copy key");
            copy.className = "action"; copy.type = "button";
            copy.addEventListener("click", async () => {
              try {
                await navigator.clipboard.writeText(key.value);
                status.textContent = "Key copied. Paste it into your personal shortcuts.";
              } catch (error) {
                key.type = "text"; key.select();
                status.textContent = "Clipboard unavailable. Copy the selected key manually.";
              }
            });
            keyMount.append(node("p", "Copy this key now. It cannot be retrieved after leaving this page."), key, copy);
          }
          status.textContent = "Saved.";
        } catch (error) { status.textContent = error.message; }
        finally { generate.disabled = false; revoke.disabled = !settings.enabled; }
      }
      generate.addEventListener("click", () => change("generate"));
      revoke.addEventListener("click", () => change("revoke"));
      mount.append(state, generate, revoke, keyMount);
      const child = settings.children[0];
      mount.append(node("h3", "Build the shortcuts"), node("p",
        "In Apple Shortcuts, create a shortcut and name it the phrase you want Siri to recognise. Add Get Contents of URL using the connector URL, POST, and the Authorization header above. Choose JSON for the request body."));
      mount.append(node("p", "Available child IDs: " + settings.children.map(c => `${c.name}: ${c.id}`).join("; ")));
      const recap = node("details");
      recap.append(node("summary", "Recap the latest posts"),
        node("p", `Example shortcut name: Recap the latest posts for ${child?.name || "Emma"}`),
        node("pre", JSON.stringify({action: "recap", child: child?.id || "emma", count: 3}, null, 2)),
        node("p", "After Get Contents of URL, add Get Dictionary Value → speech, then Speak Text. This reads the latest saved posts, not messages or albums, and makes no new Aula or AI requests."));
      const sick = node("details");
      sick.append(node("summary", "Mark a child sick today"),
        node("p", `Example shortcut name: Mark ${child?.name || "Emma"} sick for the day`),
        node("p", "First POST this JSON to the same URL with the same Authorization header:"),
        node("pre", JSON.stringify({action: "sick_status", child: child?.id || "emma"}, null, 2)),
        node("p", "Read the response's date and can_report values. If can_report is false, speak the reason and stop. If sick is true, speak speech and stop. Otherwise add Choose from Menu: Report [child] sick for [date]? Include notice in the prompt. Add Confirm and Cancel choices; Cancel must stop the shortcut."),
        node("p", "Only in Confirm, make another POST. Use the date variable returned above and a Boolean true for confirmed, not the text \"true\":"),
        node("pre", JSON.stringify({action: "sick_today", child: child?.id || "emma",
          date: "DATE_FROM_FIRST_RESPONSE", confirmed: true}, null, 2)),
        node("p", "Read speech from the response and Speak Text. Never cache or hardcode the date. Sickness applies to that day only and adds a calendar entry. This informs daycare/SFO staff, not necessarily the school teacher."));
      mount.append(recap, sick, node("p", "If a request fails, speak its error and stop. Siri may require the iPhone to be unlocked. These are personal shortcuts, not a native iOS App Intents extension."));
    } catch (error) { status.textContent = error.message; }
  }
  window.AulaSiriSettings = {render};
})();
