/* HA Terminal sidebar panel (custom element).

Home Assistant sets the `hass` object on this element. The terminal page
runs inside an iframe below, and it needs the current access token to
open its websocket and call the file API. It cannot read the token from
localStorage ("hassTokens"): the HA mobile app keeps tokens in native
storage, so localStorage is empty there and the page showed "No Home
Assistant session found". Instead this element hands the token to the
iframe with postMessage — on load, and again whenever the token changes
(HA refreshes it about every 30 minutes).

The iframe page falls back to localStorage when there is no parent panel
(e.g. opening /api/ha_terminal/ directly in a desktop browser).
*/

class HATerminalPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._token = null;
    this._iframe = null;
    this._onMessage = this._onMessage.bind(this);
    this.style.display = "block";
    this.style.height = "100%";
  }

  set hass(hass) {
    this._hass = hass;
    const token =
      hass && hass.auth && hass.auth.data && hass.auth.data.access_token;
    if (token && token !== this._token) {
      this._token = token;
      this._postToken();
    }
  }

  connectedCallback() {
    const iframe = document.createElement("iframe");
    iframe.src = "/api/ha_terminal/";
    iframe.title = "HA Terminal";
    iframe.style.cssText = "border:0;width:100%;height:100%;display:block;";
    this.appendChild(iframe);
    this._iframe = iframe;
    iframe.addEventListener("load", () => this._postToken());
    window.addEventListener("message", this._onMessage);
  }

  disconnectedCallback() {
    window.removeEventListener("message", this._onMessage);
    this._iframe = null;
  }

  _postToken() {
    if (this._iframe && this._iframe.contentWindow && this._token) {
      this._iframe.contentWindow.postMessage(
        { type: "ha-terminal-token", token: this._token },
        window.location.origin
      );
    }
  }

  _onMessage(ev) {
    if (ev.origin !== window.location.origin) return;
    if (ev.data && ev.data.type === "ha-terminal-need-token") {
      this._postToken();
    }
  }
}

customElements.define("ha-terminal-panel", HATerminalPanel);
