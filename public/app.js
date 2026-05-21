const state = {
  config: null,
  allAircraft: [],
  aircraft: [],
  selectedIcao: null,
  markers: new Map(),
  selectedTrack: null,
  map: null,
  rings: [],
  mapSized: false,
};

const els = {
  refreshButton: document.querySelector("#refreshButton"),
  locationLabel: document.querySelector("#locationLabel"),
  statusLine: document.querySelector("#statusLine"),
  credentialBadge: document.querySelector("#credentialBadge"),
  visibleCount: document.querySelector("#visibleCount"),
  overheadCount: document.querySelector("#overheadCount"),
  closestDistance: document.querySelector("#closestDistance"),
  aircraftList: document.querySelector("#aircraftList"),
  detailsPanel: document.querySelector("#detailsPanel"),
  clearSelection: document.querySelector("#clearSelection"),
  lastUpdated: document.querySelector("#lastUpdated"),
  historyRange: document.querySelector("#historyRange"),
  historyAircraft: document.querySelector("#historyAircraft"),
  historyObservations: document.querySelector("#historyObservations"),
  hourlyChart: document.querySelector("#hourlyChart"),
  closestPasses: document.querySelector("#closestPasses"),
  notificationStatus: document.querySelector("#notificationStatus"),
  notificationRules: document.querySelector("#notificationRules"),
  testAlertButton: document.querySelector("#testAlertButton"),
  filterSummary: document.querySelector("#filterSummary"),
};

const DEFAULT_FAST_HIGH_FILTER = {
  minSpeedMph: 300,
  minAltitudeFt: 10000,
};

function fmtNumber(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function fmtUnit(value, unit, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return `${fmtNumber(value, digits)} ${unit}`;
}

function fmtTime(epochSeconds) {
  if (!epochSeconds) return "--";
  return new Date(epochSeconds * 1000).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtDateTime(epochSeconds) {
  if (!epochSeconds) return "--";
  return new Date(epochSeconds * 1000).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function aircraftLabel(aircraft) {
  return aircraft.callsign || aircraft.icao24.toUpperCase();
}

function routeLabel(aircraft) {
  if (aircraft.origin && aircraft.destination) {
    return `${aircraft.origin} to ${aircraft.destination}`;
  }
  if (aircraft.destination) return `To ${aircraft.destination}`;
  return "Route unavailable";
}

function airlineLabel(aircraft) {
  return (
    aircraft.airline?.name ||
    aircraft.aircraft?.owner ||
    "Private / unknown operator"
  );
}

function aircraftTypeLabel(aircraft) {
  const type = [aircraft.aircraft?.manufacturer, aircraft.aircraft?.type]
    .filter(Boolean)
    .join(" ");
  if (type) return type;
  if (aircraft.aircraft?.icao_type) return aircraft.aircraft.icao_type;
  return aircraft.category_label || "Aircraft type unavailable";
}

function registrationLabel(aircraft) {
  return aircraft.aircraft?.registration || aircraft.icao24.toUpperCase();
}

function statusPill(aircraft) {
  if (aircraft.overhead) return "Overhead";
  if (aircraft.destination || aircraft.airline) return "Route";
  return aircraft.bearing_cardinal || "Nearby";
}

function flightInfoScore(aircraft) {
  const speed = aircraft.speed_mph || 0;
  const distance = aircraft.distance_miles || 99;
  let score = 0;
  if (aircraft.overhead) score += 1000;
  if (aircraft.destination || aircraft.origin || aircraft.airline) score += 500;
  if (speed >= 250) score += 120;
  score += Math.min(speed / 10, 70);
  score -= distance;
  return score;
}

function sortAircraftForDisplay(aircraft) {
  return [...aircraft].sort((a, b) => {
    const scoreDelta = flightInfoScore(b) - flightInfoScore(a);
    if (Math.abs(scoreDelta) > 0.001) return scoreDelta;
    return (a.distance_miles || 99) - (b.distance_miles || 99);
  });
}

function fastHighFilter() {
  const collectionFilter = state.config?.collection_filter;
  if (collectionFilter) {
    return {
      minSpeedMph:
        collectionFilter.min_speed_mph || DEFAULT_FAST_HIGH_FILTER.minSpeedMph,
      minAltitudeFt:
        collectionFilter.min_altitude_ft || DEFAULT_FAST_HIGH_FILTER.minAltitudeFt,
    };
  }
  const rule = state.config?.notifications?.rules?.high_fast_approach;
  return {
    minSpeedMph: rule?.min_speed_mph || DEFAULT_FAST_HIGH_FILTER.minSpeedMph,
    minAltitudeFt: rule?.min_altitude_ft || DEFAULT_FAST_HIGH_FILTER.minAltitudeFt,
  };
}

function isFastHigh(aircraft) {
  const filter = fastHighFilter();
  return (
    !aircraft.on_ground &&
    Number(aircraft.speed_mph || 0) >= filter.minSpeedMph &&
    Number(aircraft.altitude_ft || 0) >= filter.minAltitudeFt
  );
}

function filterAircraftForDisplay(aircraft) {
  return sortAircraftForDisplay(aircraft.filter(isFastHigh));
}

function renderFilterSummary() {
  const filter = fastHighFilter();
  const total = state.allAircraft.length;
  const shown = state.aircraft.length;
  els.filterSummary.textContent = `${fmtNumber(filter.minSpeedMph)}+ mph and ${fmtNumber(filter.minAltitudeFt)}+ ft (${shown} of ${total})`;
}

function fmtHeading(aircraft) {
  if (aircraft.track_deg === null || aircraft.track_deg === undefined) return "--";
  return `${fmtNumber(aircraft.track_deg)}° ${aircraft.track_cardinal || ""}`.trim();
}

function fmtVertical(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  if (Math.abs(value) < 100) return "Level";
  const direction = value > 0 ? "Climb" : "Descend";
  return `${direction} ${fmtNumber(Math.abs(value))} ft/min`;
}

function initMap(config) {
  if (state.map) return;

  state.map = L.map("map", {
    zoomControl: false,
  }).setView([config.home.lat, config.home.lon], 9);

  L.control.zoom({ position: "bottomleft" }).addTo(state.map);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(state.map);

  const homeIcon = L.divIcon({
    className: "",
    html: '<div class="home-marker"></div>',
    iconSize: [20, 20],
    iconAnchor: [10, 10],
  });

  L.marker([config.home.lat, config.home.lon], { icon: homeIcon })
    .addTo(state.map)
    .bindPopup(`<strong>${config.label}</strong>`);

  state.rings.push(
    L.circle([config.home.lat, config.home.lon], {
      radius: config.overhead_radius_miles * 1609.344,
      color: "#c98316",
      fillOpacity: 0.03,
      opacity: 0.8,
      weight: 2,
    }).addTo(state.map),
  );

  state.rings.push(
    L.circle([config.home.lat, config.home.lon], {
      radius: config.search_radius_miles * 1609.344,
      color: "#2473a6",
      fillOpacity: 0.02,
      opacity: 0.55,
      weight: 2,
    }).addTo(state.map),
  );

  resizeMapSoon();
}

function resizeMapSoon() {
  if (!state.map) return;
  requestAnimationFrame(() => {
    state.map.invalidateSize({ pan: false });
  });
  window.setTimeout(() => {
    state.map?.invalidateSize({ pan: false });
  }, 250);
}

function fitMapOnce() {
  if (!state.map || state.mapSized || !state.aircraft.length) return;
  const points = [
    [state.config.home.lat, state.config.home.lon],
    ...state.aircraft
      .filter((aircraft) => aircraft.lat !== null && aircraft.lon !== null)
      .map((aircraft) => [aircraft.lat, aircraft.lon]),
  ];
  if (points.length < 2) return;
  state.map.fitBounds(L.latLngBounds(points).pad(0.18), {
    animate: false,
    maxZoom: 10,
  });
  state.mapSized = true;
}

function markerIcon(aircraft) {
  const selected = state.selectedIcao === aircraft.icao24;
  const classes = [
    "plane-marker",
    aircraft.overhead ? "overhead" : "",
    selected ? "selected" : "",
  ]
    .filter(Boolean)
    .join(" ");
  const rotation = aircraft.track_deg ?? 0;
  return L.divIcon({
    className: "",
    html: `<div class="${classes}"><div class="plane-arrow" style="transform: rotate(${rotation}deg)"></div></div>`,
    iconSize: [30, 30],
    iconAnchor: [15, 15],
    popupAnchor: [0, -14],
  });
}

function popupHtml(aircraft) {
  return `
    <div class="popup-title">${escapeHtml(aircraftLabel(aircraft))}</div>
    <div class="popup-body">
      <strong>${escapeHtml(airlineLabel(aircraft))}</strong><br />
      ${escapeHtml(routeLabel(aircraft))}<br />
      ${fmtUnit(aircraft.altitude_ft, "ft")} · ${fmtUnit(aircraft.speed_mph, "mph")} · ${escapeHtml(fmtHeading(aircraft))}<br />
      ${fmtUnit(aircraft.distance_miles, "mi", 1)} ${escapeHtml(aircraft.bearing_cardinal || "")}
    </div>
  `;
}

function renderMarkers() {
  const seen = new Set();
  for (const aircraft of state.aircraft) {
    seen.add(aircraft.icao24);
    const position = [aircraft.lat, aircraft.lon];
    let marker = state.markers.get(aircraft.icao24);
    if (!marker) {
      marker = L.marker(position, { icon: markerIcon(aircraft) })
        .addTo(state.map)
        .on("click", () => selectAircraft(aircraft.icao24));
      state.markers.set(aircraft.icao24, marker);
    } else {
      marker.setLatLng(position);
      marker.setIcon(markerIcon(aircraft));
    }
    marker.bindPopup(popupHtml(aircraft));
  }

  for (const [icao24, marker] of state.markers) {
    if (!seen.has(icao24)) {
      marker.remove();
      state.markers.delete(icao24);
    }
  }
}

function renderAircraftList() {
  if (!state.aircraft.length) {
    els.aircraftList.innerHTML =
      '<div class="empty-state">No recent aircraft are both fast and high inside the search radius.</div>';
    return;
  }

  els.aircraftList.innerHTML = state.aircraft
    .map((aircraft) => {
      const active = state.selectedIcao === aircraft.icao24 ? "active" : "";
      const route = routeLabel(aircraft);
      const airline = airlineLabel(aircraft);
      const equipment = aircraftTypeLabel(aircraft);
      return `
        <article class="aircraft-card ${active} ${aircraft.overhead ? "overhead" : ""}" data-icao="${aircraft.icao24}">
          <div class="aircraft-topline">
            <div>
              <div class="callsign">${escapeHtml(aircraftLabel(aircraft))}</div>
              <div class="flight-subtitle">${escapeHtml(airline)}</div>
            </div>
            <span class="pill ${aircraft.overhead ? "hot" : ""}">${escapeHtml(statusPill(aircraft))}</span>
          </div>
          <div class="route-line">${escapeHtml(route)}</div>
          <div class="data-row dense">
            <div class="data-point">
              <span>Altitude</span>
              <strong>${fmtUnit(aircraft.altitude_ft, "ft")}</strong>
            </div>
            <div class="data-point">
              <span>Speed</span>
              <strong>${fmtUnit(aircraft.speed_mph, "mph")}</strong>
            </div>
            <div class="data-point">
              <span>Heading</span>
              <strong>${escapeHtml(fmtHeading(aircraft))}</strong>
            </div>
            <div class="data-point">
              <span>Vertical</span>
              <strong>${escapeHtml(fmtVertical(aircraft.vertical_rate_fpm))}</strong>
            </div>
            <div class="data-point">
              <span>Distance</span>
              <strong>${fmtUnit(aircraft.distance_miles, "mi", 1)}</strong>
            </div>
            <div class="data-point">
              <span>Aircraft</span>
              <strong>${escapeHtml(equipment)}</strong>
            </div>
          </div>
          <div class="aircraft-meta">
            <span>${escapeHtml(registrationLabel(aircraft))}</span>
            <span>Seen ${fmtTime(aircraft.observed_at)}</span>
          </div>
        </article>
      `;
    })
    .join("");

  els.aircraftList.querySelectorAll("[data-icao]").forEach((node) => {
    node.addEventListener("click", () => selectAircraft(node.dataset.icao));
  });
}

function renderSummary() {
  const overhead = state.aircraft.filter((aircraft) => aircraft.overhead).length;
  const closest = state.aircraft[0]?.distance_miles;
  els.visibleCount.textContent = state.aircraft.length;
  els.overheadCount.textContent = overhead;
  els.closestDistance.textContent =
    closest === undefined ? "--" : `${fmtNumber(closest, 1)} mi`;
  renderFilterSummary();
}

function renderDetails() {
  const aircraft =
    state.aircraft.find((item) => item.icao24 === state.selectedIcao) ||
    state.aircraft[0];
  if (!aircraft) {
    els.detailsPanel.className = "details-panel empty-state";
    els.detailsPanel.textContent = "Select an aircraft on the map or list.";
    return;
  }

  els.detailsPanel.className = "details-panel";
  els.detailsPanel.innerHTML = `
    <div class="details-heading">
      <div>
        <strong>${escapeHtml(aircraftLabel(aircraft))}</strong>
        <span class="icao">${escapeHtml(registrationLabel(aircraft))}</span>
      </div>
      <span class="pill ${aircraft.overhead ? "hot" : ""}">${escapeHtml(statusPill(aircraft))}</span>
    </div>
    ${aircraft.aircraft?.photo_thumbnail ? `<img class="aircraft-photo" src="${escapeHtml(aircraft.aircraft.photo_thumbnail)}" alt="${escapeHtml(aircraftLabel(aircraft))} aircraft photo" loading="lazy" />` : ""}
    <div class="route-summary">
      <span>Route</span>
      <strong>${escapeHtml(routeLabel(aircraft))}</strong>
    </div>
    <div class="details-grid">
      <div class="detail-item"><span>Airline / Operator</span><strong>${escapeHtml(airlineLabel(aircraft))}</strong></div>
      <div class="detail-item"><span>Aircraft</span><strong>${escapeHtml(aircraftTypeLabel(aircraft))}</strong></div>
      <div class="detail-item"><span>Altitude</span><strong>${fmtUnit(aircraft.altitude_ft, "ft")}</strong></div>
      <div class="detail-item"><span>Speed</span><strong>${fmtUnit(aircraft.speed_mph, "mph")}</strong></div>
      <div class="detail-item"><span>Heading</span><strong>${escapeHtml(fmtHeading(aircraft))}</strong></div>
      <div class="detail-item"><span>Vertical</span><strong>${escapeHtml(fmtVertical(aircraft.vertical_rate_fpm))}</strong></div>
      <div class="detail-item"><span>Distance</span><strong>${fmtUnit(aircraft.distance_miles, "mi", 1)} ${escapeHtml(aircraft.bearing_cardinal || "")}</strong></div>
      <div class="detail-item"><span>Origin</span><strong>${escapeHtml(aircraft.origin || "Unavailable")}</strong></div>
      <div class="detail-item"><span>Destination</span><strong>${escapeHtml(aircraft.destination || "Unavailable")}</strong></div>
      <div class="detail-item"><span>Squawk</span><strong>${escapeHtml(aircraft.squawk || "--")}</strong></div>
      <div class="detail-item"><span>Last contact</span><strong>${fmtTime(aircraft.last_contact)}</strong></div>
      <div class="detail-item"><span>Observed</span><strong>${fmtTime(aircraft.observed_at)}</strong></div>
      <div class="detail-item"><span>ICAO hex</span><strong>${escapeHtml(aircraft.icao24.toUpperCase())}</strong></div>
    </div>
    <p class="route-note">${escapeHtml(aircraft.destination_note)}</p>
  `;
}

async function renderTrack(icao24) {
  if (state.selectedTrack) {
    state.selectedTrack.remove();
    state.selectedTrack = null;
  }
  if (!icao24) return;

  const response = await fetch(`/api/aircraft/${icao24}/history?hours=12`);
  const history = await response.json();
  const points = history.points
    .filter((point) => point.lat !== null && point.lon !== null)
    .map((point) => [point.lat, point.lon]);
  if (points.length < 2) return;

  state.selectedTrack = L.polyline(points, {
    color: "#0f5e62",
    opacity: 0.85,
    weight: 4,
  }).addTo(state.map);
}

function selectAircraft(icao24) {
  state.selectedIcao = icao24;
  renderMarkers();
  renderAircraftList();
  renderDetails();
  renderTrack(icao24).catch(console.error);

  const aircraft = state.aircraft.find((item) => item.icao24 === icao24);
  if (aircraft) {
    state.map.panTo([aircraft.lat, aircraft.lon], { animate: true, duration: 0.4 });
    state.markers.get(icao24)?.openPopup();
  }
}

function renderStatus(payload) {
  const status = payload.status || {};
  const ok = status.ok === true;
  const failed = status.ok === false;
  els.statusLine.textContent = `${status.message || "Waiting for OpenSky."} Last poll: ${fmtTime(status.at)}.`;
  els.lastUpdated.textContent = fmtTime(status.at);

  if (payload.config?.has_opensky_credentials) {
    els.credentialBadge.textContent = "OAuth";
    els.credentialBadge.className = `badge ${failed ? "error" : ""}`;
  } else {
    els.credentialBadge.textContent = "Anonymous";
    els.credentialBadge.className = `badge ${failed ? "error" : "warning"}`;
  }
}

function renderNotificationConfig(config) {
  const notifications = config?.notifications || config;
  if (!notifications?.enabled) {
    els.notificationStatus.textContent = "Off";
    els.notificationRules.textContent = "Set NOTIFY_ENABLED=1 and NTFY_TOPIC in .env.";
    return;
  }
  if (!notifications.configured) {
    els.notificationStatus.textContent = "Needs topic";
    els.notificationRules.textContent = "Set a private NTFY_TOPIC before alerts can send.";
    return;
  }

  const rule = notifications.rules.high_fast_approach;
  els.notificationStatus.textContent = "Ready";
  els.notificationRules.textContent = `${fmtNumber(rule.min_speed_mph)}+ mph, ${fmtNumber(rule.min_altitude_ft)}+ ft, within ${fmtNumber(rule.radius_miles)} mi and tracking toward home.`;
}

async function refreshAircraft(force = false) {
  els.refreshButton.disabled = true;
  try {
    const response = await fetch(`/api/aircraft${force ? "?refresh=1" : ""}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Aircraft request failed");

    state.config = payload.config;
    state.allAircraft = payload.aircraft || [];
    state.aircraft = filterAircraftForDisplay(state.allAircraft);
    els.locationLabel.textContent = payload.config.label;
    initMap(payload.config);
    renderStatus(payload);
    renderNotificationConfig(payload.config);
    renderSummary();
    if (
      state.selectedIcao &&
      !state.aircraft.some((a) => a.icao24 === state.selectedIcao)
    ) {
      state.selectedIcao = null;
      if (state.selectedTrack) {
        state.selectedTrack.remove();
        state.selectedTrack = null;
      }
    }
    if (!state.selectedIcao && state.aircraft.length) {
      state.selectedIcao = state.aircraft[0].icao24;
    }
    renderMarkers();
    fitMapOnce();
    resizeMapSoon();
    renderAircraftList();
    renderDetails();
  } catch (error) {
    els.statusLine.textContent = error.message;
    els.credentialBadge.className = "badge error";
  } finally {
    els.refreshButton.disabled = false;
    if (window.lucide) window.lucide.createIcons();
  }
}

function renderHourlyChart(rows) {
  if (!rows.length) {
    els.hourlyChart.innerHTML = '<div class="empty-state">No history yet.</div>';
    return;
  }
  const maxValue = Math.max(...rows.map((row) => row.aircraft || 0), 1);
  els.hourlyChart.innerHTML = rows
    .map((row) => {
      const height = Math.max(4, ((row.aircraft || 0) / maxValue) * 92);
      const hasOverhead = Number(row.overhead || 0) > 0;
      const label = `${fmtDateTime(row.bucket)}: ${row.aircraft} aircraft, ${row.observations} observations`;
      return `<div class="bar ${hasOverhead ? "overhead" : ""}" style="height:${height}px" title="${label}"></div>`;
    })
    .join("");
}

function renderClosestPasses(rows) {
  if (!rows.length) {
    els.closestPasses.innerHTML = "";
    return;
  }
  els.closestPasses.innerHTML = rows
    .slice(0, 5)
    .map(
      (aircraft) => `
        <div class="compact-row">
          <strong>${aircraftLabel(aircraft)}</strong>
          <span>${fmtUnit(aircraft.distance_miles, "mi", 1)} · ${fmtDateTime(aircraft.observed_at)}</span>
        </div>
      `,
    )
    .join("");
}

async function refreshHistory() {
  const hours = els.historyRange.value;
  const response = await fetch(`/api/history?hours=${hours}`);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "History request failed");

  els.historyAircraft.textContent = fmtNumber(payload.totals.aircraft);
  els.historyObservations.textContent = fmtNumber(payload.totals.observations);
  renderHourlyChart(payload.hourly || []);
  renderClosestPasses(payload.closest_passes || []);
}

els.refreshButton.addEventListener("click", () => {
  refreshAircraft(true).then(refreshHistory).catch(console.error);
});

els.clearSelection.addEventListener("click", () => {
  state.selectedIcao = state.aircraft[0]?.icao24 || null;
  if (state.selectedTrack) {
    state.selectedTrack.remove();
    state.selectedTrack = null;
  }
  renderMarkers();
  renderAircraftList();
  renderDetails();
  if (state.selectedIcao) {
    renderTrack(state.selectedIcao).catch(console.error);
  }
});

els.historyRange.addEventListener("change", () => {
  refreshHistory().catch(console.error);
});

window.addEventListener("resize", resizeMapSoon);

els.testAlertButton.addEventListener("click", async () => {
  els.testAlertButton.disabled = true;
  try {
    const response = await fetch("/api/notifications/test");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Test notification failed");
    renderNotificationConfig(payload.config);
    els.notificationStatus.textContent = payload.sent_ok ? "Test sent" : "Test failed";
    if (payload.error) els.notificationRules.textContent = payload.error;
  } catch (error) {
    els.notificationStatus.textContent = "Test failed";
    els.notificationRules.textContent = error.message;
  } finally {
    els.testAlertButton.disabled = false;
  }
});

async function boot() {
  await refreshAircraft(true);
  await refreshHistory();
  window.setInterval(() => refreshAircraft(false).catch(console.error), 30_000);
  window.setInterval(() => refreshHistory().catch(console.error), 120_000);
  if (window.lucide) window.lucide.createIcons();
}

boot().catch((error) => {
  els.statusLine.textContent = error.message;
});
