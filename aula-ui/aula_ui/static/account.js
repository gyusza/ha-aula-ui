"use strict";
let polling = false;
const errorBox = document.querySelector("#error");
function failure(error) { errorBox.hidden = false; errorBox.textContent = error.message; }
async function request(path, body) {
  const response = await fetch(path, body === undefined ? {cache: "no-store"} : {
    method: "POST", headers: {"Content-Type": "application/json", "X-Aula-Request": "settings"},
    body: JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `Request failed (${response.status}).`);
  return value;
}
async function loadAccount() {
  if (polling) return;
  polling = true;
  try {
    const account = await request("api/account");
    errorBox.hidden = true;
    document.querySelector("#account-status").textContent = account.message;
    document.querySelector("#sync-status").textContent = account.last_sync && !account.errors?.length
      ? `Last successful direct sync: ${new Date(account.last_sync).toLocaleString()}. You can remove the Aula and EasyIQ integrations if you no longer need their other entities.`
      : "Keep your integrations until the add-on reports a successful direct sync. Your existing cached feed remains readable.";
    document.querySelector("#children-panel").hidden = !account.connected;
    const username = document.querySelector("#connect-form").elements.username;
    if (account.connected) { username.value = account.username; username.readOnly = true; }
    const children = document.querySelector("#children");
    children.replaceChildren();
    for (const child of account.children) {
      const row = document.createElement("p");
      const planning = account.planning?.[child.slug];
      row.textContent = child.display_name + (planning ? ` — ${planning.message}` : "");
      children.append(row);
    }
    const flow = account.login;
    const running = flow.status === "running";
    document.querySelector("#connect-form button").disabled = running || account.mode !== "direct";
    document.querySelector("#login-progress").hidden = flow.status === "idle";
    document.querySelector("#login-status").textContent = flow.message;
    document.querySelector("#cancel").hidden = !running;
    const qr = document.querySelector("#qr");
    qr.hidden = !flow.qr;
    if (flow.qr) qr.src = flow.qr; else qr.removeAttribute("src");
    const identities = document.querySelector("#identities");
    const identityKey = JSON.stringify(flow.identities);
    if (identities.dataset.options !== identityKey) {
      identities.dataset.options = identityKey;
      identities.replaceChildren();
      flow.identities.forEach((name, index) => {
        const button = document.createElement("button");
        button.className = "action"; button.type = "button"; button.textContent = name;
        button.addEventListener("click", async () => {
          button.disabled = true;
          try { await request("api/auth/identity", {index: index + 1}); await loadAccount(); }
          catch (error) { failure(error); button.disabled = false; }
        });
        identities.append(button);
      });
    }
    if (account.errors?.length) {
      errorBox.hidden = false; errorBox.textContent = account.errors.join("\n");
    }
  } catch (error) { failure(error); }
  finally { polling = false; }
}
document.querySelector("#connect-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.target.querySelector("button"); button.disabled = true;
  try { await request("api/auth/start", {username: event.target.elements.username.value}); await loadAccount(); }
  catch (error) { failure(error); button.disabled = false; }
});
document.querySelector("#cancel").addEventListener("click", async () => {
  try { await request("api/auth/cancel", {}); await loadAccount(); } catch (error) { failure(error); }
});
loadAccount();
setInterval(() => { if (!document.hidden) loadAccount(); }, 1000);
