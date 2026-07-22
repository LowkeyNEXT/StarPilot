import { html, reactive } from "/assets/vendor/arrow-core.js"
import { Modal } from "/assets/components/modal.js";

const DEFAULT_PLAY_STORE_URL = "https://play.google.com/store/apps/details?id=com.embaucha.galaxynav&hl=en-US&ah=9FldHJ99kxL8oNbSlO5F4sQqwC4"

export function NavKeys() {
  const state = reactive({
    initialMapboxComplete: false,
    showMapboxHelp: false,
    visible: false,

    imageVersion: 0,

    error: "",
    lastGroup: "",
    message: "",

    amap1Key: "", amap2Key: "",
    editA1: false, editA2: false,
    savedA1: false, savedA2: false,

    publicKey: "", secretKey: "",
    editPublic: false, editSecret: false,
    savedPublic: false, savedSecret: false,

    galaxyCookieName: "galaxy_session",
    galaxySessionToken: "",
    galaxyAppUrl: DEFAULT_PLAY_STORE_URL,
    galaxyPaired: false,
    galaxySessionVisible: false,

    externalPairingQrData: "",
    externalPairingQrImage: "",
    externalPairingCode: "",
    externalPairingLoading: false,

    telemetrySupported: false,
    telemetryInfoVisible: false,
    telemetryMode: "off",
    telemetryPushUrl: "",
    telemetryPushToken: "",
    telemetryBatteryCapacity: "",
    telemetryVinAvailable: false,
    telemetrySaving: false,

    showDeleteModal: false,
    keyToDelete: null,
  })

  const bumpImageVersion = () => state.imageVersion++

  let clearTimer = null
  let fadeTimer = null

  function showMessage(type, text, group) {
    clearTimer && clearTimeout(clearTimer)
    fadeTimer && clearTimeout(fadeTimer)

    state.error = type === "error" ? text : ""
    state.message = type === "message" ? text : ""

    state.lastGroup = group

    state.visible = true

    clearTimer = setTimeout(() => { state.message = "", state.error = "" }, 5000)
    fadeTimer = setTimeout(() => state.visible = false, 5000)
  }

  const util = {
    prefix: (key, prefix) => key.startsWith(prefix) ? key : prefix ? prefix + key : key,

    mask: (key) => {
      if (!key) {
        return ""
      }

      const prefix = ["pk.", "sk."].find(p => key.startsWith(p)) || ""
      return prefix + "x".repeat(key.length - prefix.length)
    },

    req: async (url, opts) => {
      const response = await fetch(url, opts)
      return { ok: response.ok, data: await response.json().catch(() => ({})) }
    },

    copyText: async (text) => {
      if (!text) {
        throw new Error("Nothing to copy")
      }

      if (navigator.clipboard?.writeText && window.isSecureContext) {
        await navigator.clipboard.writeText(text)
        return
      }

      const textarea = document.createElement("textarea")
      textarea.value = text
      textarea.setAttribute("readonly", "")
      textarea.style.position = "fixed"
      textarea.style.left = "-9999px"
      textarea.style.opacity = "0"
      document.body.appendChild(textarea)
      textarea.select()

      try {
        if (!document.execCommand("copy")) {
          throw new Error("Copy command failed")
        }
      } finally {
        textarea.remove()
      }
    }
  }

  const meta = {
    amap1:  { prop: "amap1Key",  saved: "savedA1",     edit: "editA1",     prefix: "",    body: "amap1", minLength: 39  },
    amap2:  { prop: "amap2Key",  saved: "savedA2",     edit: "editA2",     prefix: "",    body: "amap2", minLength: 39  },
    public: { prop: "publicKey", saved: "savedPublic", edit: "editPublic", prefix: "pk.", body: "public", minLength: 80 },
    secret: { prop: "secretKey", saved: "savedSecret", edit: "editSecret", prefix: "sk.", body: "secret", minLength: 80 }
  }

  const canSave = (kind) => {
    const keyMeta = meta[kind];
    if (!keyMeta) return false;

    const value = state[keyMeta.prop]?.trim() || "";
    if (!value) return false;

    if (!state[keyMeta.saved]) {
      const fullValue = util.prefix(value, keyMeta.prefix);
      return fullValue.length >= keyMeta.minLength;
    }
    return false;
  }

  const getDeleteLabel = (kind) => {
    switch (kind) {
      case "amap1": return "AMap / Gaode 1"
      case "amap2": return "AMap / Gaode 2"
      case "public": return "Public Mapbox"
      case "secret": return "Secret Mapbox"
      default: return kind
    }
  }

  const api = {
    path: {
      galaxy: "/api/galaxy/session",
      externalPairing: "/api/external-app/pairing",
      telemetryConfig: "/api/vehicle/telemetry/config",
      key: "/api/navigation_key",
      nav: "/api/navigation"
    },

    load: async () => {
      const { ok, data } = await util.req(api.path.nav)
      if (!ok) {
        showMessage("error", "Failed to load keys...", "")
        await api.loadGalaxySession()
        return
      }

      state.amap1Key = data.amap1Key ?? ""
      state.amap2Key = data.amap2Key ?? ""
      state.savedA1 = !!state.amap1Key
      state.savedA2 = !!state.amap2Key

      state.publicKey = data.mapboxPublic ?? ""
      state.secretKey = data.mapboxSecret ?? ""
      state.savedPublic = !!state.publicKey
      state.savedSecret = !!state.secretKey

      state.initialMapboxComplete = state.savedPublic && state.savedSecret

      bumpImageVersion()
      await api.loadGalaxySession()
    },

    loadGalaxySession: async () => {
      const { ok, data } = await util.req(api.path.galaxy)
      if (!ok) {
        return showMessage("error", "Failed to load Galaxy session...", "app")
      }

      state.galaxyAppUrl = data.appUrl || DEFAULT_PLAY_STORE_URL
      state.galaxyCookieName = data.cookieName || "galaxy_session"
      state.galaxyPaired = !!data.paired
      state.galaxySessionToken = data.sessionToken || ""
      state.galaxySessionVisible = false
      state.telemetrySupported = !!data.vehicleTelemetrySupported
      if (state.telemetrySupported) {
        await api.loadTelemetry()
      }
    },

    copyGalaxySession: async () => {
      try {
        await util.copyText(state.galaxySessionToken)
        showMessage("message", "Session token copied!", "app")
      } catch (e) {
        showMessage("error", "Copy failed...", "app")
      }
    },

    loadTelemetry: async () => {
      const { ok, data } = await util.req(api.path.telemetryConfig)
      if (!ok) return
      state.telemetryMode = data.config?.mode || "off"
      state.telemetryPushUrl = data.config?.push?.url || ""
      state.telemetryBatteryCapacity = data.config?.push?.maximumBatteryCapacityKilowattHours ?? ""
      state.telemetryVinAvailable = !!data.vinAvailable
    },

    saveTelemetry: async () => {
      state.telemetrySaving = true
      const payload = {
        mode: state.telemetryMode,
        pushToken: state.telemetryPushToken,
        push: {
          url: state.telemetryPushUrl,
          maximumBatteryCapacityKilowattHours: state.telemetryBatteryCapacity || null,
        },
      }
      const { ok, data } = await util.req(api.path.telemetryConfig, {
        body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      })
      state.telemetrySaving = false
      if (!ok) {
        return showMessage("error", data.error || "Could not save EV Vehicle Telemetry...", "telemetry")
      }
      state.telemetryPushToken = ""
      state.telemetryMode = data.config?.mode || "off"
      state.telemetryPushUrl = data.config?.push?.url || ""
      state.telemetryBatteryCapacity = data.config?.push?.maximumBatteryCapacityKilowattHours ?? ""
      state.telemetryVinAvailable = !!data.vinAvailable
      showMessage("message", "EV Vehicle Telemetry saved.", "telemetry")
    },

    createExternalPairing: async () => {
      state.externalPairingLoading = true
      const { ok, data } = await util.req(api.path.externalPairing, { method: "POST" })
      state.externalPairingLoading = false
      if (!ok) {
        return showMessage("error", data.error || "Could not create pairing...", "app")
      }
      state.externalPairingQrData = data.qrData || ""
      state.externalPairingQrImage = data.qrImageDataURL || ""
      state.externalPairingCode = data.pairingCode || ""
      showMessage("message", "One-time pairing is ready for 10 minutes.", "app")
    },

    copyExternalPairing: async () => {
      try {
        await util.copyText(state.externalPairingQrData)
        showMessage("message", "One-time pairing code copied!", "app")
      } catch (e) {
        showMessage("error", "Copy failed...", "app")
      }
    },

    save: (kind) => async () => {
      const group = kind.startsWith("amap") ? "amap" : "mapbox"
      const keyMeta = meta[kind]
      const value = util.prefix(state[keyMeta.prop].trim(), keyMeta.prefix)

      const { ok, data } = await util.req(api.path.key, {
        body: JSON.stringify({ [keyMeta.body]: value }),
        headers: { "Content-Type": "application/json" },
        method: "POST"
      })

      if (!ok) {
        const input = document.getElementById(`${kind}-key`)
        if (input) {
          input.value = ""
          state[keyMeta.edit] = true
          state[keyMeta.saved] = false
          state[keyMeta.prop] = ""
          input.focus()
        }
        return showMessage("error", data.error || "Save failed...", group)
      }

      Object.assign(state, {
        [keyMeta.edit]: false,
        [keyMeta.saved]: true,
        [keyMeta.prop]: value
      })

      const input = document.getElementById(`${kind}-key`)
      if (input) {
        input.blur()
        input.value = ""
        requestAnimationFrame(() => { input.value = util.mask(state[keyMeta.prop]) })
      }

      if (group === "mapbox") {
        bumpImageVersion()
      }

      showMessage("message", data.message || "Saved!", group)
    },

    confirmDelete: (kind) => {
      state.keyToDelete = kind;
      state.showDeleteModal = true;
    },

    delete: async () => {
      const kind = state.keyToDelete;
      if (!kind) return;

      const group = kind.startsWith("amap") ? "amap" : "mapbox"
      const keyMeta = meta[kind]

      const { ok, data } = await util.req(`${api.path.key}?type=${kind}`, {
        method: "DELETE"
      })

      state.showDeleteModal = false;

      if (!ok) {
        return showMessage("error", data.error || "Delete failed...", group)
      }

      Object.assign(state, {
        [keyMeta.saved]: false,
        [keyMeta.prop]: ""
      })

      if (group === "mapbox") {
        state.initialMapboxComplete = false
        bumpImageVersion()
      }

      showMessage("message", data.message || "Deleted!", group)
    }
  }

  queueMicrotask(api.load)

  function renderGroup(title, kinds) {
    const isMapbox = title === "Mapbox Keys"
    const isAMap = title === "AMap / Gaode Keys"

    return html`
      <div class="navkeys-group">
        <div class="navkeys-title">
          ${title}
          ${isMapbox ? html`
            <span class="navkeys-help-icon" @click="${() => state.showMapboxHelp = !state.showMapboxHelp}">
              <i class="bi bi-question-circle-fill"></i>
            </span>
          ` : ""}
        </div>
        ${isAMap ? html`<div class="navkeys-subtitle">AMap is the Gaode provider, not Google Maps.</div>` : ""}

        ${kinds.map(kind => {
          const keyMeta = meta[kind]
          const label = kind[0].toUpperCase() + kind.slice(1).replace(/[0-9]/, d => " " + d)

          return html`
            <label class="navkeys-label" for="${kind}-key">${label} Key</label>
            <div class="navkeys-row">
              <input
                autocomplete="off"
                class="navkeys-input"
                id="${kind}-key"
                placeholder="${keyMeta.prefix || ""}xxxxxx..."
                value="${() => state[keyMeta.saved] ? util.mask(state[keyMeta.prop]) : state[keyMeta.prop]}"
                @keydown="${(e) => {
                  if (state[keyMeta.saved] && !state[keyMeta.edit]) {
                    state[keyMeta.edit] = true
                    state[keyMeta.saved] = false
                    state[keyMeta.prop] = ""
                    e.target.value = ""
                  }
                }}"
                @input="${(e) => state[keyMeta.prop] = e.target.value}"
              />
              <button
                class="${() => `navkeys-btn ${state[keyMeta.saved] ? "delete" : ""}`}"
                @click="${() => state[keyMeta.saved] ? api.confirmDelete(kind) : api.save(kind)()}"
                disabled="${() => !state[keyMeta.saved] && !canSave(kind)}">
                ${() => state[keyMeta.saved] ? "🗑️" : "💾"}
              </button>
            </div>
          `
        })}

        ${() => {
          if (isMapbox && state.showMapboxHelp) {
            return html`
              <div class="navkeys-help-img">
                <img
                  alt="Mapbox key setup guide"
                  src="${() => {
                    const bothKeysSet = state.savedPublic && state.savedSecret

                    let imageSource = "/mapbox-help/no_keys_set.png"
                    if (bothKeysSet) {
                      imageSource = state.initialMapboxComplete ? "/mapbox-help/setup_completed.png" : "/mapbox-help/both_keys_set.png"
                    } else if (state.savedPublic) {
                      imageSource = "/mapbox-help/public_key_set.png"
                    }
                    return `${imageSource}?v=${state.imageVersion}`
                  }}"
                />
              </div>
            `
          }
          return ""
        }}
      </div>
    `
  }

  function renderStatus(group) {
    return html`
      <div class="navkeys-status">
        <div
          class="navkeys-message"
          style="${() => state.lastGroup === group && state.message ? `opacity: ${state.visible ? 1 : 0}` : "opacity: 0"}">
          ${() => state.message}
        </div>
        <div
          class="navkeys-error"
          style="${() => state.lastGroup === group && state.error ? `opacity: ${state.visible ? 1 : 0}` : "opacity: 0"}">
          ${() => state.error}
        </div>
      </div>
    `
  }

  function renderAppKeys() {
    return html`
      <div class="navkeys-title">App Keys</div>

      <div class="navkeys-app-actions">
        <a
          class="navkeys-btn navkeys-link-btn"
          href="${() => state.galaxyAppUrl}"
          rel="noopener noreferrer"
          target="_blank">
          <i class="bi bi-google-play"></i>
          <span>Install The App</span>
        </a>
      </div>

      <label class="navkeys-label" for="galaxy-cookie-name">Cookie Name</label>
      <div class="navkeys-row">
        <input
          class="navkeys-input navkeys-token-input"
          id="galaxy-cookie-name"
          readonly
          value="${() => state.galaxyCookieName}"
        />
      </div>

      <label class="navkeys-label" for="galaxy-session-token">Session Token</label>
      <div class="navkeys-row">
        <input
          class="navkeys-input navkeys-token-input"
          id="galaxy-session-token"
          placeholder="${() => state.galaxyPaired ? "Session token unavailable..." : "Pair Galaxy to create a session token..."}"
          readonly
          type="${() => state.galaxySessionVisible ? "text" : "password"}"
          value="${() => state.galaxySessionToken}"
        />
        <button
          aria-label="${() => state.galaxySessionVisible ? "Hide session token" : "Show session token"}"
          class="navkeys-btn navkeys-icon-btn"
          @click="${() => { state.galaxySessionVisible = !state.galaxySessionVisible }}"
          disabled="${() => !state.galaxySessionToken}"
          title="${() => state.galaxySessionVisible ? "Hide session token" : "Show session token"}">
          <i class="${() => `bi ${state.galaxySessionVisible ? "bi-eye-slash" : "bi-eye"}`}"></i>
        </button>
        <button
          class="navkeys-btn navkeys-copy-btn"
          @click="${api.copyGalaxySession}"
          disabled="${() => !state.galaxySessionToken}">
          <i class="bi bi-copy"></i>
          <span>Copy</span>
        </button>
      </div>

      ${() => state.telemetrySupported ? html`
        <div class="navkeys-app-actions navkeys-pair-actions">
          <button
            class="navkeys-btn navkeys-copy-btn"
            @click="${api.createExternalPairing}"
            disabled="${() => state.externalPairingLoading || state.telemetryMode !== "galaxy"}">
            <i class="bi bi-qr-code"></i>
            <span>${() => state.externalPairingLoading ? "Creating..." : "Create Pairing QR"}</span>
          </button>
        </div>
      ` : ""}

      ${() => state.externalPairingQrData ? html`
        <div class="navkeys-pairing-card">
          ${state.externalPairingQrImage ? html`
            <img class="navkeys-pairing-qr" alt="One-time external app pairing QR code" src="${state.externalPairingQrImage}" />
          ` : ""}
          <div class="navkeys-pairing-details">
            <div class="navkeys-label">One-time connection package</div>
            <div class="navkeys-pairing-code" aria-label="Six digit pairing code">${state.externalPairingCode}</div>
            <button class="navkeys-btn navkeys-copy-btn" @click="${api.copyExternalPairing}">
              <i class="bi bi-copy"></i><span>Copy Pairing Code</span>
            </button>
          </div>
        </div>
      ` : ""}
    `
  }

  function telemetryInput(label, property, { type = "text", placeholder = "", secret = false } = {}) {
    return html`
      <label class="navkeys-label">${label}</label>
      <input
        autocomplete="${secret ? "new-password" : "off"}"
        class="navkeys-input ${secret ? "navkeys-token-input" : ""}"
        type="${type}"
        placeholder="${placeholder}"
        value="${() => state[property]}"
        @input="${(event) => state[property] = event.target.value}" />
    `
  }

  function renderTelemetryConfig() {
    return html`
      <div class="navkeys-title">
        EV Vehicle Telemetry
        <button
          aria-expanded="${() => state.telemetryInfoVisible}"
          aria-label="About EV Vehicle Telemetry"
          class="navkeys-help-icon navkeys-info-button"
          @click="${() => state.telemetryInfoVisible = !state.telemetryInfoVisible}">
          <i class="bi bi-question-circle-fill"></i>
        </button>
      </div>

      ${() => state.telemetryInfoVisible ? html`
        <div class="navkeys-telemetry-info">
          Shares read-only battery, range, charging, plug, and charge-time data with Galaxy or one custom HTTPS endpoint. The VIN is supplied automatically. Off stops collection and removes saved telemetry; custom uploads run only while driving or charging.
        </div>
      ` : ""}

      <label class="navkeys-label">Operating mode</label>
      <select
        class="navkeys-input navkeys-select"
        value="${() => state.telemetryMode}"
        @change="${(event) => state.telemetryMode = event.target.value}">
        <option value="off">Off</option>
        <option value="galaxy">Galaxy portal</option>
        <option value="send">Custom HTTPS backend</option>
      </select>

      ${() => state.telemetryMode === "send" ? html`
        <div class="navkeys-telemetry-grid">
          <div>${telemetryInput("Backend URL", "telemetryPushUrl", { placeholder: "https://telemetry.example/ingest" })}</div>
          <div>${telemetryInput("Bearer token (leave blank to keep)", "telemetryPushToken", { secret: true, placeholder: "••••••••" })}</div>
          <div>${telemetryInput("Battery capacity (kWh, optional)", "telemetryBatteryCapacity", { type: "number" })}</div>
        </div>
        <div class="navkeys-telemetry-status">
          ${() => state.telemetryVinAvailable ? "VIN will be supplied automatically." : "VIN will be supplied automatically when the vehicle identifies itself."}
        </div>
      ` : ""}

      <div class="navkeys-app-actions navkeys-telemetry-actions">
        <button class="navkeys-btn navkeys-copy-btn" @click="${api.saveTelemetry}" disabled="${() => state.telemetrySaving}">
          <i class="bi bi-floppy-fill"></i>
          <span>${() => state.telemetrySaving ? "Saving..." : "Save Telemetry"}</span>
        </button>
      </div>
    `
  }

  return html`
    <div class="navkeys-wrapper navkeys-offset-top">
      <div class="navkeys-container">
          ${renderGroup("AMap / Gaode Keys", ["amap1", "amap2"])}
        ${renderStatus("amap")}
      </div>
      <div class="navkeys-container">
        ${renderGroup("Mapbox Keys", ["public", "secret"])}
        ${renderStatus("mapbox")}
      </div>
      <div class="navkeys-container navkeys-app-container">
        ${renderAppKeys()}
        ${renderStatus("app")}
      </div>
      ${() => state.telemetrySupported ? html`
        <div class="navkeys-container navkeys-app-container">
          ${renderTelemetryConfig()}
          ${renderStatus("telemetry")}
        </div>
      ` : ""}
    </div>
    ${() => state.showDeleteModal ? Modal({
      title: "Confirm Delete",
      message: `Are you sure you want to delete your <strong>${getDeleteLabel(state.keyToDelete)}</strong> key?`,
      onConfirm: api.delete,
      onCancel: () => { state.showDeleteModal = false },
      confirmText: "Yes, Delete"
    }) : ""}
  `
}
