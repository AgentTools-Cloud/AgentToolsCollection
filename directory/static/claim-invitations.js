"use strict";

const form = document.getElementById("optout-form");
const button = document.getElementById("optout-button");
const input = document.getElementById("optout-token");
const status = document.getElementById("link-status");

function updateToken() {
  if (!form || !button || !input || !status) return;
  const token = window.location.hash.slice(1);
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(token)) {
    input.value = "";
    button.disabled = true;
    status.hidden = false;
    status.textContent = "This link is missing or invalid.";
    return;
  }
  input.value = token;
  button.disabled = false;
  status.textContent = "";
  status.hidden = true;
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
}

updateToken();
window.addEventListener("hashchange", updateToken);

if (form && button) {
  form.addEventListener("submit", () => {
    button.disabled = true;
    button.textContent = "Stopping...";
  });
}