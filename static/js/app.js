(() => {
  const logEl = document.getElementById("log");
  const progress = document.getElementById("progress");
  const bar = progress.querySelector("span");
  const licStatus = document.getElementById("licStatus");
  let jobId = null;
  let pollTimer = null;

  const ui = JSON.parse(document.getElementById("ui-copy").textContent);
  const statLine = document.getElementById("statLine");
  const presets = {
    gmail: { host: "imap.gmail.com", port: 993, hint: "gmail" },
    outlook: { host: "outlook.office365.com", port: 993, hint: "outlook" },
    yandex: { host: "imap.yandex.com", port: 993, hint: "default" },
    zoho: { host: "imap.zoho.com", port: 993, hint: "default" },
    cpanel: { host: "", port: 993, hint: "default" },
  };

  function showHint(selectId, hintId) {
    const key = presets[document.getElementById(selectId).value]?.hint;
    const el = document.getElementById(hintId);
    if (!key) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    el.textContent = ui[key] || ui.default;
  }

  function log(msg) {
    logEl.textContent += msg + "\n";
    logEl.scrollTop = logEl.scrollHeight;
  }

  function bindPreset(selectId, hostId, portId) {
    document.getElementById(selectId).addEventListener("change", (e) => {
      const p = presets[e.target.value];
      if (!p) return;
      if (p.host) document.getElementById(hostId).value = p.host;
      document.getElementById(portId).value = p.port;
      showHint(selectId, selectId.startsWith("src") ? "src_hint" : "dst_hint");
    });
  }
  bindPreset("src_preset", "src_host", "src_port");
  bindPreset("dst_preset", "dst_host", "dst_port");

  document.querySelectorAll("[data-toggle]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = document.getElementById(btn.getAttribute("data-toggle"));
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      const label = btn.querySelector(".btn-label");
      (label || btn).textContent = show ? ui.hide : ui.show;
    });
  });

  document.getElementById("btnSwap").addEventListener("click", () => {
    const pairs = ["preset", "host", "port", "user", "password"];
    pairs.forEach((name) => {
      const a = document.getElementById("src_" + name);
      const b = document.getElementById("dst_" + name);
      const tmp = a.value;
      a.value = b.value;
      b.value = tmp;
    });
    showHint("src_preset", "src_hint");
    showHint("dst_preset", "dst_hint");
  });

  const savedLic = sessionStorage.getItem("pa_license");
  if (savedLic) document.getElementById("license").value = savedLic;

  function payload() {
    return {
      src_preset: document.getElementById("src_preset").value,
      src_host: document.getElementById("src_host").value,
      src_port: document.getElementById("src_port").value,
      src_user: document.getElementById("src_user").value,
      src_password: document.getElementById("src_password").value,
      src_ssl: document.getElementById("opt_ssl").checked,
      dst_preset: document.getElementById("dst_preset").value,
      dst_host: document.getElementById("dst_host").value,
      dst_port: document.getElementById("dst_port").value,
      dst_user: document.getElementById("dst_user").value,
      dst_password: document.getElementById("dst_password").value,
      dst_ssl: document.getElementById("opt_ssl").checked,
      dry_run: document.getElementById("opt_dry").checked,
      delete: document.getElementById("opt_delete").checked,
      license: document.getElementById("license").value.trim(),
    };
  }

  function rememberLicense() {
    sessionStorage.setItem("pa_license", document.getElementById("license").value.trim());
  }

  async function api(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || data.message || ("HTTP " + res.status));
    return data;
  }

  document.getElementById("btnLicense").addEventListener("click", async () => {
    try {
      const data = await api("/api/license", { license: document.getElementById("license").value.trim() });
      licStatus.textContent = data.message || "lisans geçerli";
      licStatus.className = "status-pill " + (data.ok ? "ok" : "err");
      log(data.message || JSON.stringify(data));
    } catch (e) {
      licStatus.textContent = "geçersiz";
      licStatus.className = "status-pill err";
      log("Lisans: " + e.message);
    }
  });

  document.getElementById("btnTest").addEventListener("click", async () => {
    log("Bağlantı testi başlıyor...");
    try {
      const data = await api("/api/test", payload());
      log(data.message || "Test tamam.");
      if (data.logs) data.logs.forEach((l) => log(l));
    } catch (e) {
      log("Hata: " + e.message);
    }
  });

  function setRunning(on) {
    document.getElementById("btnStart").disabled = on;
    document.getElementById("btnStop").disabled = !on;
    progress.classList.toggle("active", on);
  }

  async function poll() {
    if (!jobId) return;
    try {
      const res = await fetch("/api/job/" + jobId);
      const data = await res.json();
      if (data.logs) {
        for (const line of data.logs) log(line);
      }
      if (typeof data.progress === "number") {
        const pct = Math.max(2, Math.min(100, data.progress));
        bar.style.width = pct + "%";
        statLine.hidden = false;
        statLine.textContent = ui.progress + " " + Math.round(pct) + "% · " + (data.status || "");
      }
      if (data.status === "done" || data.status === "error" || data.status === "stopped") {
        clearInterval(pollTimer);
        pollTimer = null;
        sessionStorage.removeItem("pa_job");
        setRunning(false);
        log("Durum: " + data.status + (data.message ? " — " + data.message : ""));
        bar.style.width = data.status === "done" ? "100%" : bar.style.width;
      }
    } catch (e) {
      log("Poll hatası: " + e.message);
    }
  }

  function watchJob(id) {
    jobId = id;
    sessionStorage.setItem("pa_job", id);
    progress.classList.add("active");
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(poll, 1200);
    poll();
  }

  document.getElementById("btnStart").addEventListener("click", async () => {
    rememberLicense();
    log("Taşıma başlatılıyor...");
    setRunning(true);
    bar.style.width = "2%";
    try {
      const data = await api("/api/start", payload());
      log("İş ID: " + data.job_id);
      watchJob(data.job_id);
    } catch (e) {
      setRunning(false);
      log("Hata: " + e.message);
    }
  });

  document.getElementById("btnStop").addEventListener("click", async () => {
    if (!jobId) return;
    try {
      await api("/api/stop", { job_id: jobId });
      log("Durdurma istendi.");
    } catch (e) {
      log("Durdurma hatası: " + e.message);
    }
  });

  const pending = sessionStorage.getItem("pa_job");
  if (pending) {
    setRunning(true);
    log("Süren işe bağlanılıyor: " + pending);
    watchJob(pending);
  }
})();
