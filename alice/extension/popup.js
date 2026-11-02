chrome.runtime.sendMessage("state", (s) => {
  const el = document.getElementById("st");
  el.textContent = s === "bağlı" ? "bağlı" : "bağlı değil (ALICE çalışıyor mu?)";
  if (s === "bağlı") el.className = "ok";
});
