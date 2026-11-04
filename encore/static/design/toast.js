// Toasts for apps without htmx/Alpine. toast("Saved") or toast("Can't reach the Pi", "error").
// Needs <div id="toasts" class="toasts"></div> at the end of <body>.
export function toast(message, kind = "ok") {
  const box = document.getElementById("toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " error" : "");
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, 4500);
}
