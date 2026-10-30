const $ = (id) => document.getElementById(id);
async function refresh() {
  const s = await chrome.runtime.sendMessage({ type: "status" });
  $("status").textContent = !s?.paired ? "Not paired." : s.connected ? `Connected to the daemon on port ${s.port} (${s.tabs} tab${s.tabs === 1 ? "" : "s"} attached).` : `Paired (port ${s.port}); waiting for the daemon to run.`;
}
$("pair").addEventListener("click", async () => {
  const r = await chrome.runtime.sendMessage({ type: "pair", value: $("code").value });
  $("status").textContent = r?.ok ? "Paired. Connecting…" : r?.error || "Pairing failed.";
  $("code").value = "";
  setTimeout(refresh, 800);
});
refresh(); setInterval(refresh, 2000);
