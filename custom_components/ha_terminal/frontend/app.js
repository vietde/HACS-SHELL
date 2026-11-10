/* HA Terminal frontend: xterm.js terminal + /config file manager.
 * Same origin as Home Assistant. The access token comes from, in order:
 *   1. a ?token= query parameter,
 *   2. a postMessage from the sidebar panel element (panel.js), which gets
 *      the token from the `hass` object — this is the path that works in
 *      the HA mobile app, where localStorage holds no token,
 *   3. localStorage ("hassTokens") — the desktop-browser path.
 * The terminal websocket takes the token as ?token=; the file API calls
 * send it as a Bearer header. The server validates the token and
 * requires an admin user. */
(function () {
  "use strict";

  var WS_BASE = (location.protocol === "https:" ? "wss://" : "ws://") + location.host;

  var haToken = null; // token posted by the sidebar panel element
  var tokenWaitedMs = 0; // ms spent waiting for a token at boot
  var tokenRefreshTried = false;

  function requestTokenFromParent() {
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ type: "ha-terminal-need-token" }, location.origin);
      }
    } catch (e) { /* ignore: no parent to ask */ }
  }

  function getToken() {
    try {
      var m = /[?&]token=([^&]+)/.exec(location.search);
      if (m) return decodeURIComponent(m[1]);
    } catch (e) { /* ignore */ }
    if (haToken) return haToken;
    try {
      var raw = localStorage.getItem("hassTokens");
      if (raw) return JSON.parse(raw).access_token || null;
    } catch (e) {
      return null;
    }
    return null;
  }

  // Tell the server where the page got to (loaded, token received,
  // websocket open/closed). Used to diagnose a blank screen on a phone,
  // where there is no dev console to look at. Sends only a short stage
  // string — never the token. Failures are silent on purpose.
  var reportedStages = {};
  function report(stage) {
    if (reportedStages[stage]) return;
    reportedStages[stage] = true;
    try {
      fetch("/api/ha_terminal/client-log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stage: stage }),
        keepalive: true,
      }).catch(function () {});
    } catch (e) { /* ignore */ }
  }

  window.addEventListener("message", function (ev) {
    if (ev.origin !== location.origin) return;
    var d = ev.data || {};
    if (d.type === "ha-terminal-token" && d.token) {
      haToken = d.token;
      report("token-parent");
      if (!termWs || termWs.readyState !== WebSocket.OPEN) connectTerminal();
    }
  });

  function showOverlay(text) {
    document.getElementById("overlay-text").textContent = text;
    document.getElementById("term-overlay").classList.remove("hidden");
  }
  function hideOverlay() {
    document.getElementById("term-overlay").classList.add("hidden");
  }
  function setStatus(connected, text) {
    var el = document.getElementById("status");
    el.textContent = text;
    el.className = "status " + (connected ? "connected" : "disconnected");
  }

  /* ---------------- Terminal ---------------- */
  var term = new Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: 'ui-monospace, "Cascadia Mono", Consolas, monospace',
    theme: { background: "#0b0e14", foreground: "#d6deeb" },
    allowProposedApi: true,
  });
  var fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(document.getElementById("terminal"));

  var termWs = null;
  var termWantOpen = true;
  // Auto-reconnect: the network between the browser and HA drops
  // websocket frames (HA's own log shows "No PONG received" on the main
  // connections too), so a dropped terminal socket is usually a
  // transient network hiccup, not a dead shell. Reconnect a few times
  // before giving up and showing the overlay.
  var reconnectAttempts = 0;
  var reconnectTimer = null;
  var keepaliveTimer = null;

  function stopKeepalive() {
    if (keepaliveTimer) { clearInterval(keepaliveTimer); keepaliveTimer = null; }
  }
  function startKeepalive() {
    stopKeepalive();
    // Application-level ping every 5 s. The server answers {"type":"pong"}.
    // This keeps data flowing through middleboxes that kill idle
    // websockets after ~10 s (the "Terminal closed" after 10-15 s of
    // silence), and it keeps the server's 120 s receive timeout from
    // ever firing on a live connection.
    keepaliveTimer = setInterval(function () {
      if (termWs && termWs.readyState === WebSocket.OPEN) {
        try { termWs.send(JSON.stringify({ type: "ping" })); } catch (e) { /* ignore */ }
      }
    }, 5000);
  }

  function scheduleReconnect() {
    if (!termWantOpen) return;
    if (reconnectAttempts >= 5) {
      showOverlay("Terminal closed. The shell may have exited.");
      return;
    }
    reconnectAttempts += 1;
    setStatus(false, "Connection lost — reconnecting…");
    reconnectTimer = setTimeout(function () {
      term.reset();
      connectTerminal();
    }, 1000);
  }

  function termSendResize() {
    if (termWs && termWs.readyState === WebSocket.OPEN) {
      termWs.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    }
  }

  function connectTerminal() {
    var token = getToken();
    if (!token) {
      // No token yet — the sidebar panel posts one (the HA mobile app
      // keeps tokens out of localStorage). Ask for it and wait a little
      // before giving up, instead of failing on the first paint.
      requestTokenFromParent();
      if (tokenWaitedMs < 10000) {
        tokenWaitedMs += 500;
        setStatus(false, "Connecting…");
        setTimeout(connectTerminal, 500);
        return;
      }
      setStatus(false, "No HA session — open Home Assistant first");
      report("token-timeout");
      showOverlay("No Home Assistant session found. Open this page from inside Home Assistant (sidebar → Terminal) while logged in.");
      return;
    }
    tokenWaitedMs = 0;
    if (!haToken) report("token-storage");
    setStatus(false, "Connecting…");
    hideOverlay();
    termWs = new WebSocket(WS_BASE + "/api/ha_terminal/ws?token=" + encodeURIComponent(token));
    termWs.binaryType = "arraybuffer";
    termWs.onopen = function () {
      tokenRefreshTried = false;
      reconnectAttempts = 0;
      report("ws-open");
      setStatus(true, "Connected");
      startKeepalive();
      fitAddon.fit();
      termSendResize();
      term.focus();
    };
    termWs.onmessage = function (ev) {
      if (typeof ev.data === "string") {
        try {
          var msg = JSON.parse(ev.data);
          if (msg.type === "error") term.writeln("\r\n" + msg.message);
        } catch (e) { /* ignore */ }
        return;
      }
      term.write(new Uint8Array(ev.data));
    };
    termWs.onclose = function (ev) {
      setStatus(false, "Disconnected");
      stopKeepalive();
      report(ev.code === 4401 ? "ws-close-4401" : "ws-close-" + ev.code);
      if (!termWantOpen) return;
      if (ev.code === 4401 && !tokenRefreshTried && window.parent && window.parent !== window) {
        // Token expired — ask the panel for a fresh one and retry once
        // before telling the user the session is gone.
        tokenRefreshTried = true;
        setStatus(false, "Session expired — refreshing…");
        requestTokenFromParent();
        setTimeout(connectTerminal, 500);
        return;
      }
      if (ev.code === 4401) {
        showOverlay("Session expired or not an admin. Reload Home Assistant and try again.");
        return;
      }
      // Any other close (usually 1006, the network dropping the socket):
      // reconnect automatically a few times instead of leaving a dead
      // overlay. A reconnect starts a fresh shell — the old one is gone
      // with the socket — so the terminal is reset first.
      scheduleReconnect();
    };
    termWs.onerror = function () {
      report("ws-error");
      setStatus(false, "Connection error");
    };
  }

  term.onData(function (data) {
    if (termWs && termWs.readyState === WebSocket.OPEN) termWs.send(new TextEncoder().encode(data));
  });

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      try { fitAddon.fit(); } catch (e) { /* ignore */ }
      termSendResize();
    }, 100);
  });

  document.getElementById("btn-reconnect").addEventListener("click", function () {
    termWantOpen = true;
    reconnectAttempts = 0;
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (termWs) termWs.close();
    term.reset();
    connectTerminal();
  });
  document.getElementById("btn-overlay-reconnect").addEventListener("click", function () {
    termWantOpen = true;
    reconnectAttempts = 0;
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    term.reset();
    connectTerminal();
  });
  document.getElementById("btn-clear").addEventListener("click", function () { term.clear(); });

  /* ---------------- Files (/config) ---------------- */
  var currentPath = "";
  var filesList = document.getElementById("files-list");
  var filesPath = document.getElementById("files-path");
  var filesStatus = document.getElementById("files-status");

  function fmtSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function joinPath(dir, name) {
    return dir ? dir + "/" + name : name;
  }

  function parentPath(path) {
    var i = path.lastIndexOf("/");
    return i === -1 ? "" : path.slice(0, i);
  }

  function setFilesStatus(text) {
    filesStatus.textContent = text;
    if (text) setTimeout(function () { filesStatus.textContent = ""; }, 4000);
  }

  function loadFiles(path) {
    var token = getToken();
    if (!token) {
      filesList.innerHTML = '<div class="files-empty">No Home Assistant session — log into Home Assistant first.</div>';
      return;
    }
    fetch("/api/ha_terminal/files/list?path=" + encodeURIComponent(path), {
      headers: { Authorization: "Bearer " + token },
    })
      .then(function (r) {
        if (!r.ok) throw new Error("list failed (" + r.status + ")");
        return r.json();
      })
      .then(function (data) {
        currentPath = data.path;
        filesPath.textContent = "/config" + (currentPath ? "/" + currentPath : "");
        var html = "";
        data.dirs.forEach(function (d) {
          var p = joinPath(currentPath, d.name);
          html += '<div class="file-row dir" data-path="' + p.replace(/"/g, "&quot;") + '">' +
            '<span class="file-icon">📁</span><span class="file-name">' + d.name + '</span>' +
            '<span class="file-meta"></span></div>';
        });
        data.files.forEach(function (f) {
          var p = joinPath(currentPath, f.name);
          html += '<div class="file-row" data-path="' + p.replace(/"/g, "&quot;") + '">' +
            '<span class="file-icon">📄</span><span class="file-name">' + f.name + '</span>' +
            '<span class="file-meta">' + fmtSize(f.size) + "</span></div>";
        });
        if (!data.dirs.length && !data.files.length) {
          html = '<div class="files-empty">Empty folder.</div>';
        }
        filesList.innerHTML = html;
        filesList.querySelectorAll(".file-row").forEach(function (row) {
          row.addEventListener("click", function () {
            var p = row.getAttribute("data-path");
            if (row.classList.contains("dir")) {
              loadFiles(p);
            } else {
              downloadFile(p);
            }
          });
        });
      })
      .catch(function (err) {
        filesList.innerHTML = '<div class="files-empty">' + err.message + "</div>";
      });
  }

  function downloadFile(path) {
    var token = getToken();
    if (!token) return;
    fetch("/api/ha_terminal/files/download?path=" + encodeURIComponent(path), {
      headers: { Authorization: "Bearer " + token },
    })
      .then(function (r) { if (!r.ok) throw new Error("download failed (" + r.status + ")"); return r.blob(); })
      .then(function (blob) {
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = path.split("/").pop();
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
      })
      .catch(function (err) { setFilesStatus(err.message); });
  }

  document.getElementById("btn-files-up").addEventListener("click", function () {
    loadFiles(parentPath(currentPath));
  });
  document.getElementById("btn-files-refresh").addEventListener("click", function () {
    loadFiles(currentPath);
  });
  document.getElementById("files-upload").addEventListener("change", function (ev) {
    var token = getToken();
    var input = ev.target;
    if (!token || !input.files.length) return;
    var form = new FormData();
    for (var i = 0; i < input.files.length; i++) form.append("file", input.files[i]);
    setFilesStatus("Uploading…");
    fetch("/api/ha_terminal/files/upload?path=" + encodeURIComponent(currentPath), {
      method: "POST",
      headers: { Authorization: "Bearer " + token },
      body: form,
    })
      .then(function (r) {
        if (!r.ok) throw new Error("upload failed (" + r.status + ")");
        return r.json();
      })
      .then(function (data) {
        setFilesStatus("Uploaded: " + data.uploaded.join(", "));
        loadFiles(currentPath);
      })
      .catch(function (err) { setFilesStatus(err.message); })
      .finally(function () { input.value = ""; });
  });

  /* ---------------- SSH keys (/config/.ssh/authorized_keys) ---------------- */
  var keysList = document.getElementById("keys-list");
  var keysInput = document.getElementById("keys-input");
  var keysStatus = document.getElementById("keys-status");

  function setKeysStatus(text) {
    keysStatus.textContent = text;
    if (text) setTimeout(function () { keysStatus.textContent = ""; }, 5000);
  }

  function escHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderKeys(keys) {
    if (!keys.length) {
      keysList.innerHTML = '<div class="keys-empty">No SSH keys yet — paste one above to enable passwordless login.</div>';
      return;
    }
    var html = "";
    keys.forEach(function (k) {
      var label = escHtml(k.key_type) + (k.comment ? " · " + escHtml(k.comment) : "");
      var fp = escHtml(k.fingerprint || "");
      html += '<div class="key-row">' +
        '<span class="key-icon">🔑</span>' +
        '<span class="key-info"><span class="key-label">' + label + '</span>' +
        '<span class="key-fp">' + fp + '</span></span>' +
        '<button class="btn key-remove" data-fp="' + fp + '">Remove</button></div>';
    });
    keysList.innerHTML = html;
    keysList.querySelectorAll(".key-remove").forEach(function (btn) {
      btn.addEventListener("click", function () {
        removeKey(btn.getAttribute("data-fp"));
      });
    });
  }

  function loadKeys() {
    var token = getToken();
    if (!token) {
      keysList.innerHTML = '<div class="keys-empty">No Home Assistant session — log into Home Assistant first.</div>';
      return;
    }
    fetch("/api/ha_terminal/files/authorized_keys", {
      headers: { Authorization: "Bearer " + token },
    })
      .then(function (r) {
        if (!r.ok) throw new Error("keys list failed (" + r.status + ")");
        return r.json();
      })
      .then(function (data) { renderKeys(data.keys || []); })
      .catch(function (err) {
        keysList.innerHTML = '<div class="keys-empty">' + escHtml(err.message) + "</div>";
      });
  }

  function addKey() {
    var token = getToken();
    var line = keysInput.value.trim();
    if (!token || !line) return;
    setKeysStatus("Adding…");
    fetch("/api/ha_terminal/files/authorized_keys", {
      method: "POST",
      headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      body: JSON.stringify({ key: line }),
    })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.message || "add failed (" + r.status + ")"); return d; }); })
      .then(function (data) {
        keysInput.value = "";
        setKeysStatus(data.message || "Key added.");
        renderKeys(data.keys || []);
      })
      .catch(function (err) { setKeysStatus(err.message); });
  }

  function removeKey(fingerprint) {
    var token = getToken();
    if (!token || !fingerprint) return;
    fetch("/api/ha_terminal/files/authorized_keys", {
      method: "DELETE",
      headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      body: JSON.stringify({ fingerprint: fingerprint }),
    })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.message || "remove failed (" + r.status + ")"); return d; }); })
      .then(function (data) {
        setKeysStatus(data.message || "Key removed.");
        renderKeys(data.keys || []);
      })
      .catch(function (err) { setKeysStatus(err.message); });
  }

  document.getElementById("btn-keys-add").addEventListener("click", addKey);

  /* ---------------- Tabs ---------------- */
  function showTab(which) {
    var isTerm = which === "terminal";
    document.getElementById("view-terminal").classList.toggle("hidden", !isTerm);
    document.getElementById("view-files").classList.toggle("hidden", isTerm);
    document.getElementById("tab-terminal").classList.toggle("active", isTerm);
    document.getElementById("tab-files").classList.toggle("active", !isTerm);
    if (isTerm) {
      try { fitAddon.fit(); } catch (e) { /* ignore */ }
      termSendResize();
      term.focus();
    } else {
      loadFiles(currentPath);
      loadKeys();
    }
  }
  document.getElementById("tab-terminal").addEventListener("click", function () { showTab("terminal"); });
  document.getElementById("tab-files").addEventListener("click", function () { showTab("files"); });

  /* ---------------- Boot ---------------- */
  report("page-loaded");
  fitAddon.fit();
  connectTerminal();
})();
