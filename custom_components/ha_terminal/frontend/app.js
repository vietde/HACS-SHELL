/* HA Terminal frontend: xterm.js terminal + live log tail.
 * Same origin as Home Assistant, so the HA access token can be read from
 * localStorage ("hassTokens") and sent as ?token= on the websockets.
 * The server validates the token and requires an admin user. */
(function () {
  "use strict";

  var WS_BASE = (location.protocol === "https:" ? "wss://" : "ws://") + location.host;

  function getToken() {
    try {
      var raw = localStorage.getItem("hassTokens");
      if (!raw) return null;
      return JSON.parse(raw).access_token || null;
    } catch (e) {
      return null;
    }
  }

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

  function termSendResize() {
    if (termWs && termWs.readyState === WebSocket.OPEN) {
      termWs.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    }
  }

  function connectTerminal() {
    var token = getToken();
    if (!token) {
      setStatus(false, "No HA session — open Home Assistant first");
      showOverlay("No Home Assistant session found. Open this page from inside Home Assistant (sidebar → Terminal) while logged in.");
      return;
    }
    setStatus(false, "Connecting…");
    hideOverlay();
    termWs = new WebSocket(WS_BASE + "/api/ha_terminal/ws?token=" + encodeURIComponent(token));
    termWs.binaryType = "arraybuffer";
    termWs.onopen = function () {
      setStatus(true, "Connected");
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
      if (termWantOpen) {
        showOverlay(ev.code === 4401
          ? "Session expired or not an admin. Reload Home Assistant and try again."
          : "Terminal closed. The shell may have exited.");
      }
    };
    termWs.onerror = function () {
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
    if (termWs) termWs.close();
    term.reset();
    connectTerminal();
  });
  document.getElementById("btn-overlay-reconnect").addEventListener("click", function () {
    termWantOpen = true;
    term.reset();
    connectTerminal();
  });
  document.getElementById("btn-clear").addEventListener("click", function () { term.clear(); });

  /* ---------------- Logs ---------------- */
  var logWs = null;
  var logLines = [];
  var MAX_LOG_LINES = 2000;

  var logOut = document.getElementById("log-output");
  var logSearch = document.getElementById("log-search");
  var logLevel = document.getElementById("log-level");
  var logFollow = document.getElementById("log-follow");
  var logCount = document.getElementById("log-count");

  function levelOf(line) {
    var m = line.match(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b/);
    return m ? m[1] : null;
  }

  function renderLogs() {
    var needle = logSearch.value.trim().toLowerCase();
    var html = "";
    var shown = 0;
    var currentLevel = "INFO";
    for (var i = 0; i < logLines.length; i++) {
      var line = logLines[i];
      var lvl = levelOf(line);
      if (lvl) currentLevel = lvl;
      if (needle && line.toLowerCase().indexOf(needle) === -1) continue;
      shown++;
      var cls = "lvl-" + currentLevel;
      var esc = line.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      if (needle) {
        var idx = esc.toLowerCase().indexOf(needle);
        if (idx !== -1) {
          esc = esc.slice(0, idx) + "<mark>" + esc.slice(idx, idx + needle.length) + "</mark>" + esc.slice(idx + needle.length);
        }
      }
      html += '<span class="' + cls + '">' + esc + "</span>\n";
    }
    logOut.innerHTML = html;
    logCount.textContent = shown + " / " + logLines.length + " lines";
    if (logFollow.checked) logOut.scrollTop = logOut.scrollHeight;
  }

  function connectLogs() {
    var token = getToken();
    if (!token || (logWs && logWs.readyState <= 1)) return;
    logWs = new WebSocket(WS_BASE + "/api/ha_terminal/logs/ws?token=" + encodeURIComponent(token));
    logWs.onmessage = function (ev) {
      try {
        var msg = JSON.parse(ev.data);
        if (msg.type === "lines") {
          for (var i = 0; i < msg.lines.length; i++) logLines.push(msg.lines[i]);
          if (logLines.length > MAX_LOG_LINES) logLines.splice(0, logLines.length - MAX_LOG_LINES);
          renderLogs();
        } else if (msg.type === "error") {
          logLines.push("ERROR: " + msg.message);
          renderLogs();
        }
      } catch (e) { /* ignore */ }
    };
    logWs.onclose = function () {
      setTimeout(function () {
        if (!document.getElementById("view-logs").classList.contains("hidden")) connectLogs();
      }, 3000);
    };
  }

  logLevel.addEventListener("change", function () {
    if (logWs && logWs.readyState === WebSocket.OPEN) {
      logWs.send(JSON.stringify({ type: "set_level", level: logLevel.value }));
    }
  });
  logSearch.addEventListener("input", renderLogs);
  document.getElementById("btn-log-clear").addEventListener("click", function () {
    logLines = [];
    renderLogs();
  });
  document.getElementById("btn-download").addEventListener("click", function () {
    var token = getToken();
    if (!token) return;
    fetch("/api/ha_terminal/logs/download", { headers: { Authorization: "Bearer " + token } })
      .then(function (r) { if (!r.ok) throw new Error("download failed"); return r.blob(); })
      .then(function (blob) {
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "home-assistant.log";
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
      })
      .catch(function () { /* ignore */ });
  });

  /* ---------------- Tabs ---------------- */
  function showTab(which) {
    var isTerm = which === "terminal";
    document.getElementById("view-terminal").classList.toggle("hidden", !isTerm);
    document.getElementById("view-logs").classList.toggle("hidden", isTerm);
    document.getElementById("tab-terminal").classList.toggle("active", isTerm);
    document.getElementById("tab-logs").classList.toggle("active", !isTerm);
    if (isTerm) {
      try { fitAddon.fit(); } catch (e) { /* ignore */ }
      termSendResize();
      term.focus();
    } else {
      connectLogs();
    }
  }
  document.getElementById("tab-terminal").addEventListener("click", function () { showTab("terminal"); });
  document.getElementById("tab-logs").addEventListener("click", function () { showTab("logs"); });

  /* ---------------- Boot ---------------- */
  fitAddon.fit();
  connectTerminal();
})();
