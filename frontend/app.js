// ============================================================
// T2G Dashboard — logique front-end (vanilla JS, sans build step)
// Modifiez API_BASE si votre backend Flask tourne ailleurs.
// ============================================================
const API_BASE = window.T2G_API_BASE || "http://localhost:5000/api";
document.getElementById("api-url-hint").textContent = API_BASE;

let TOKEN = localStorage.getItem("t2g_token") || null;
let CURRENT_USER = null;
let SELECTED_SESSION = null;
let V2G_ENABLED = true;  // activé par défaut pour afficher gains V2G
let EDITING_VEHICLE_ID = null;
let charts = {};
let AUTO_REFRESH = false;
let AUTO_REFRESH_INTERVAL = null;
let SETTINGS = {
  health_weight: 0.01,
  max_cycles: 3,
  auto_refresh: false,
  v2g_notifications: false
};

function fmtTND(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString("fr-TN", { minimumFractionDigits: 2, maximumFractionDigits: 3 }) + " TND";
}

async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (TOKEN) headers["Authorization"] = "Bearer " + TOKEN;
  const res = await fetch(API_BASE + path, Object.assign({}, opts, { headers }));
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `Erreur API (${res.status})`);
  }
  return res.json();
}

const LOGIN_ACCOUNTS = {
  user:  { username: "demo",  password: "demo1234" },
  admin: { username: "admin", password: "admin1234" },
};
let LOGIN_MODE = "user";

function setLoginMode(mode) {
  LOGIN_MODE = mode;
  document.getElementById("tab-user").classList.toggle("active", mode === "user");
  document.getElementById("tab-admin").classList.toggle("active", mode === "admin");
  const acc = LOGIN_ACCOUNTS[mode];
  document.getElementById("login-username").value = acc.username;
  document.getElementById("login-password").value = acc.password;
}

async function login() {
  const username = document.getElementById("login-username").value.trim();
  const password = document.getElementById("login-password").value;
  const errEl = document.getElementById("login-error");
  errEl.textContent = "";
  try {
    const data = await api("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
    TOKEN = data.token;
    CURRENT_USER = data.user;
    localStorage.setItem("t2g_token", TOKEN);
    showApp();
  } catch (e) {
    errEl.textContent = "Connexion impossible : " + e.message + " (avez-vous lancé `python seed.py` ?)";
  }
}

function logout() {
  TOKEN = null;
  localStorage.removeItem("t2g_token");
  document.getElementById("app-view").classList.add("hidden");
  document.getElementById("login-view").classList.remove("hidden");
}

async function showApp() {
  document.getElementById("login-view").classList.add("hidden");
  document.getElementById("app-view").classList.remove("hidden");
  document.getElementById("me-username").textContent = CURRENT_USER.username;
  document.getElementById("me-balance").textContent = CURRENT_USER.v2g_balance_tnd.toFixed(3);
  // Afficher le menu admin si rôle admin
  const adminNav = document.getElementById("admin-nav");
  if (adminNav) adminNav.classList.toggle("hidden", (CURRENT_USER.role || "user") !== "admin");
  // Afficher l'état V2G (ON par défaut)
  document.getElementById("v2g-switch").classList.toggle("on", V2G_ENABLED);
  
  await loadStations();
  showView('dashboard');
}

async function refreshAll() {
  await Promise.all([loadStats(), loadStations(), loadSessions(1)]);
}

async function loadStats() {
  const s = await api("/stats/dashboard");
  
  // Statistiques globales
  document.getElementById("kpi-energy").innerHTML = s.total_energy_kwh.toLocaleString("fr-TN") + " <small>kWh</small>";
  document.getElementById("kpi-sessions").textContent = s.total_sessions.toLocaleString("fr-TN") + " sessions";
  document.getElementById("kpi-cost").textContent = fmtTND(s.total_cost_tnd);
  document.getElementById("kpi-gain").textContent = fmtTND(s.total_v2g_gain_tnd);
  document.getElementById("kpi-soc").innerHTML = `${s.avg_soc_start_pct}% <small>→</small> ${s.avg_soc_end_pct}%`;
  // Impact environnemental
  const env = s.environment || {};
  document.getElementById("kpi-co2").innerHTML = `${(env.co2_avoided_kg || 0).toLocaleString("fr-TN")} <small>kg CO₂</small>`;
  document.getElementById("kpi-co2-delta").textContent = `≈ ${env.trees_equivalent || 0} arbres / an`;

  // Statistiques personnelles si disponibles
  if (s.user_stats) {
    document.getElementById("me-balance").textContent = s.user_stats.v2g_balance_tnd.toFixed(3);
  }

  renderHourChart(s.by_hour);
  renderCityChart(s.by_city);
}

function renderHourChart(byHour) {
  const labels = Array.from({ length: 24 }, (_, h) => h + "h");
  const values = Array(24).fill(0);
  byHour.forEach((row) => { values[row.hour] = row.sessions; });
  const ctx = document.getElementById("chart-hour");
  if (charts.hour) charts.hour.destroy();
  charts.hour = new Chart(ctx, {
    type: "bar",
    data: { labels, datasets: [{ data: values, backgroundColor: "#16A34A", borderRadius: 3, barPercentage: 0.6 }] },
    options: baseChartOptions(false),
  });
}

function renderCityChart(byCity) {
  const sorted = [...byCity].sort((a, b) => b.cost_tnd - a.cost_tnd).slice(0, 8);
  const ctx = document.getElementById("chart-city");
  if (charts.city) charts.city.destroy();
  charts.city = new Chart(ctx, {
    type: "bar",
    data: {
      labels: sorted.map((r) => r.city),
      datasets: [{ data: sorted.map((r) => r.cost_tnd), backgroundColor: "#0F766E", borderRadius: 3 }],
    },
    options: Object.assign(baseChartOptions(true), {
      indexAxis: "y",
    }),
  });
}

function baseChartOptions(horizontal) {
  return {
    responsive: true,
    plugins: { legend: { display: false } },
    scales: {
      x: { grid: { color: "#E4EFE7" }, ticks: { color: "#5B7264", font: { size: 11 } } },
      y: { grid: { color: "#E4EFE7" }, ticks: { color: "#5B7264", font: { size: 11 } } },
    },
  };
}

async function loadStations() {
  const stations = await api("/stations");
  const cities = [...new Set(stations.map((s) => s.city))].sort();
  const sel = document.getElementById("city-filter");
  sel.innerHTML = '<option value="">Toutes les villes</option>' + cities.map((c) => `<option value="${c}">${c}</option>`).join("");
}

async function loadSessions(page) {
  const city = document.getElementById("city-filter").value;
  const qs = new URLSearchParams({ page, per_page: 12 });
  if (city) qs.set("city", city);
  const data = await api("/sessions?" + qs.toString());
  document.getElementById("sessions-count-tag").textContent = `${data.total} au total`;
  const tbody = document.getElementById("sessions-tbody");
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td class="empty" colspan="8">Aucune session de recharge trouvée.</td></tr>';
    renderPagination("sessions-pagination", data.page || page, data.pages || 1, "loadSessions");
    return;
  }
  tbody.innerHTML = data.items.map((s) => `
    <tr>
      <td>${s.session_code}</td>
      <td>${s.vehicle ? s.vehicle.model : "—"}</td>
      <td>${s.station ? s.station.city : "—"}</td>
      <td>${new Date(s.start_time).toLocaleString("fr-TN")}</td>
      <td>${s.energy_kwh} kWh</td>
      <td>${fmtTND(s.cost_tnd)}</td>
      <td><span class="badge ${s.mode}">${s.mode}</span></td>
      <td><a href="#" onclick="selectForOptim(${s.id}, '${s.session_code}'); return false;">Optimiser →</a></td>
    </tr>`).join("");
  renderPagination("sessions-pagination", data.page || page, data.pages || 1, "loadSessions");
}

function renderPagination(containerId, page, pages, fnName) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!pages || pages <= 1) {
    el.innerHTML = "";
    return;
  }
  const prev = Math.max(1, page - 1);
  const next = Math.min(pages, page + 1);
  el.innerHTML = `
    <div style="display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:10px;font-size:13px;">
      <button class="ghost" ${page <= 1 ? "disabled" : ""} onclick="${fnName}(${prev})">← Préc.</button>
      <span style="color:var(--muted)">Page ${page} / ${pages}</span>
      <button class="ghost" ${page >= pages ? "disabled" : ""} onclick="${fnName}(${next})">Suiv. →</button>
    </div>`;
}

function selectForOptim(id, code) {
  SELECTED_SESSION = id;
  document.getElementById("optim-session-label").textContent = code;
  showView('dashboard', null);
  setTimeout(() => {
    document.getElementById("optim-panel").scrollIntoView({ behavior: "smooth" });
  }, 100);
}

function toggleV2G() {
  V2G_ENABLED = !V2G_ENABLED;
  document.getElementById("v2g-switch").classList.toggle("on", V2G_ENABLED);
}

async function runOptimization() {
  if (!SELECTED_SESSION) {
    alert("Choisissez d'abord une session dans le tableau (bouton “Optimiser →”).");
    return;
  }
  const result = await api(`/optimize/${SELECTED_SESSION}`, {
    method: "POST",
    body: JSON.stringify({ 
      v2g_enabled: V2G_ENABLED,
      health_weight: SETTINGS.health_weight,
      max_cycles: SETTINGS.max_cycles
    }),
  });
  document.getElementById("opt-baseline").textContent = fmtTND(result.baseline_cost_tnd);
  document.getElementById("opt-net").textContent = fmtTND(result.optimized_net_cost_tnd);
  document.getElementById("opt-savings").textContent =
    `Économie : ${fmtTND(result.savings_tnd)} (${result.savings_pct}%)`;
  
  // Afficher les nouvelles métriques IA
  if (result.battery_health_cost_tnd > 0) {
    document.getElementById("opt-savings").textContent += 
      ` | Santé batterie: ${fmtTND(result.battery_health_cost_tnd)}`;
  }
  if (result.cycles_used !== undefined) {
    document.getElementById("opt-savings").textContent += 
      ` | Cycles: ${result.cycles_used}`;
  }
  
  renderScheduleChart(result.schedule);
  if (V2G_ENABLED && result.v2g_gain_tnd > 0) {
    notifyV2GOpportunity(`Gain V2G de ${fmtTND(result.v2g_gain_tnd)} sur cette session.`);
  }
  // Rafraîchir solde
  try {
    CURRENT_USER = await api("/auth/me");
    document.getElementById("me-balance").textContent = CURRENT_USER.v2g_balance_tnd.toFixed(3);
  } catch (_) {}
  await loadSessions(1);
}

// Formate une heure flottante (ex. 23.75) en "HH:MM"
function fmtHourLabel(h) {
  const totalMin = Math.round(((h % 24) + 24) % 24 * 60) % 1440;
  const hh = String(Math.floor(totalMin / 60)).padStart(2, "0");
  const mm = String(totalMin % 60).padStart(2, "0");
  return `${hh}:${mm}`;
}

function renderScheduleChart(schedule) {
  const ctx = document.getElementById("chart-schedule");
  if (charts.schedule) charts.schedule.destroy();
  charts.schedule = new Chart(ctx, {
    type: "line",
    data: {
      labels: schedule.map((s) => fmtHourLabel(s.hour)),
      datasets: [
        { label: "Charge (kW)", data: schedule.map((s) => s.charge_kw), borderColor: "#16A34A", backgroundColor: "#16A34A33", fill: true, tension: 0, stepped: true, pointRadius: 0, yAxisID: "y" },
        { label: "Décharge V2G (kW)", data: schedule.map((s) => -s.discharge_kw), borderColor: "#65A30D", backgroundColor: "#65A30D33", fill: true, tension: 0, stepped: true, pointRadius: 0, yAxisID: "y" },
        { label: "SoC (%)", data: schedule.map((s) => s.soc_pct), borderColor: "#0F766E", borderDash: [4, 3], tension: 0, stepped: true, pointRadius: 0, yAxisID: "y1" },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: "#E4EFE7" }, ticks: { color: "#5B7264", maxTicksLimit: 12, font: { size: 10 } }, title: { display: true, text: "Heure", color: "#5B7264" } },
        y: { position: "left", grid: { color: "#E4EFE7" }, ticks: { color: "#5B7264", font: { size: 10 } }, title: { display: true, text: "kW", color: "#5B7264" } },
        y1: { position: "right", grid: { display: false }, min: 0, max: 100, ticks: { color: "#5B7264", font: { size: 10 } }, title: { display: true, text: "SoC %", color: "#5B7264" } },
      },
    },
  });
}

// ============================================================
// Navigation dynamique entre les vues
// ============================================================
function showView(viewName, event = null) {
  // Masquer toutes les vues
  document.querySelectorAll('.view').forEach(view => {
    view.classList.remove('active');
  });
  
  // Afficher la vue demandée
  document.getElementById(viewName + '-view').classList.add('active');
  
  // Mettre à jour la navigation si appelé depuis un clic
  if (event && event.target) {
    document.querySelectorAll('.nav-item').forEach(item => {
      item.classList.remove('active');
    });
    event.target.classList.add('active');
  } else {
    // Mise à jour manuelle de la navigation
    document.querySelectorAll('.nav-item').forEach(item => {
      item.classList.remove('active');
      const text = item.textContent.toLowerCase();
      if (text.includes(viewName.toLowerCase()) || 
          (viewName === 'dashboard' && text.includes('tableau')) ||
          (viewName === 'sessions' && text.includes('sessions')) ||
          (viewName === 'vehicles' && text.includes('véhicules')) ||
          (viewName === 'recommendations' && text.includes('conseils')) ||
          (viewName === 'analytics' && text.includes('analyse')) ||
          (viewName === 'profile' && text.includes('profil')) ||
          (viewName === 'settings' && text.includes('paramètres')) ||
          (viewName === 'bookings' && text.includes('réservations')) ||
          (viewName === 'map' && text.includes('carte')) ||
          (viewName === 'admin' && text.includes('administration'))) {
        item.classList.add('active');
      }
    });
  }
  
  // Charger les données spécifiques à la vue
  switch(viewName) {
    case 'dashboard':
      refreshAll();
      break;
    case 'sessions':
      loadSessionsView(1);
      break;
    case 'vehicles':
      loadVehicles();
      break;
    case 'recommendations':
      loadRecommendations();
      break;
    case 'analytics':
      loadAnalytics();
      break;
    case 'profile':
      loadProfile();
      break;
    case 'settings':
      loadSettings();
      break;
    case 'bookings':
      loadBookings();
      break;
    case 'map':
      loadMap();
      break;
    case 'admin':
      loadAdmin();
      break;
  }
}

// ============================================================
// Gestion des véhicules
// ============================================================
async function loadVehicles() {
  try {
    const vehicles = await api("/vehicles");
    const container = document.getElementById("vehicles-container");
    
    if (vehicles.length === 0) {
      container.innerHTML = '<div class="empty">Aucun véhicule enregistré. Ajoutez votre premier véhicule !</div>';
      return;
    }
    
    container.innerHTML = vehicles.map(v => {
      const eff = (v.battery_efficiency != null ? v.battery_efficiency : 0.92);
      return `
      <div class="card">
        <div class="card-title">
          <span>${v.model}</span>
          <span class="badge ${v.v2g_eligible ? 'v2g' : 'classique'}">${v.v2g_eligible ? 'V2G' : 'Standard'}</span>
        </div>
        <div style="font-size:13px; color:var(--muted);">
          <div>Batterie: ${v.battery_capacity_kwh} kWh | Rendement: ${(eff * 100).toFixed(0)}%</div>
          <div>Charge: ${v.max_charge_power_kw} kW | Décharge: ${v.max_discharge_power_kw} kW</div>
          <div>Âge: ${v.vehicle_age_years} an(s)</div>
        </div>
        <div class="card-actions" style="margin-top:12px;">
          <button class="ghost" onclick="editVehicle(${v.id})">Modifier</button>
          <button class="ghost" style="color:var(--danger)" onclick="deleteVehicle(${v.id})">Supprimer</button>
        </div>
      </div>
    `;
    }).join('');
  } catch (e) {
    console.error("Erreur chargement véhicules:", e);
  }
}

function showVehicleModal() {
  EDITING_VEHICLE_ID = null;
  const title = document.getElementById('vehicle-modal-title');
  if (title) title.textContent = 'Ajouter un véhicule';
  document.getElementById('vehicle-modal').classList.add('active');
}

function hideVehicleModal() {
  EDITING_VEHICLE_ID = null;
  document.getElementById('vehicle-modal').classList.remove('active');
}

async function editVehicle(id) {
  try {
    const vehicles = await api("/vehicles");
    const v = vehicles.find(x => x.id === id);
    if (!v) {
      alert("Véhicule introuvable");
      return;
    }
    EDITING_VEHICLE_ID = id;
    const title = document.getElementById('vehicle-modal-title');
    if (title) title.textContent = 'Modifier le véhicule';
    document.getElementById('vehicle-model').value = v.model;
    document.getElementById('vehicle-battery').value = v.battery_capacity_kwh;
    document.getElementById('vehicle-age').value = v.vehicle_age_years;
    document.getElementById('vehicle-charge-power').value = v.max_charge_power_kw;
    document.getElementById('vehicle-discharge-power').value = v.max_discharge_power_kw;
    document.getElementById('vehicle-efficiency').value = v.battery_efficiency != null ? v.battery_efficiency : 0.92;
    document.getElementById('vehicle-v2g').checked = !!v.v2g_eligible;
    document.getElementById('vehicle-modal').classList.add('active');
  } catch (e) {
    alert("Impossible de charger le véhicule: " + e.message);
  }
}

async function saveVehicle() {
  const vehicleData = {
    model: document.getElementById('vehicle-model').value,
    battery_capacity_kwh: parseFloat(document.getElementById('vehicle-battery').value),
    vehicle_age_years: parseFloat(document.getElementById('vehicle-age').value),
    max_charge_power_kw: parseFloat(document.getElementById('vehicle-charge-power').value),
    max_discharge_power_kw: parseFloat(document.getElementById('vehicle-discharge-power').value),
    battery_efficiency: parseFloat(document.getElementById('vehicle-efficiency').value),
    v2g_eligible: document.getElementById('vehicle-v2g').checked
  };
  
  try {
    if (EDITING_VEHICLE_ID) {
      await api(`/vehicles/${EDITING_VEHICLE_ID}`, { method: "PUT", body: JSON.stringify(vehicleData) });
    } else {
      await api("/vehicles", { method: "POST", body: JSON.stringify(vehicleData) });
    }
    hideVehicleModal();
    loadVehicles();
  } catch (e) {
    alert("Erreur lors de l'enregistrement: " + e.message);
  }
}

async function deleteVehicle(id) {
  if (!confirm("Êtes-vous sûr de vouloir supprimer ce véhicule ?")) return;
  
  try {
    await api(`/vehicles/${id}`, { method: "DELETE" });
    loadVehicles();
  } catch (e) {
    alert("Erreur lors de la suppression: " + e.message);
  }
}

// ============================================================
// Vue sessions dédiée
// ============================================================
async function loadSessionsView(page) {
  // Charger les villes si pas encore fait
  const cityFilter = document.getElementById("sessions-city-filter");
  if (cityFilter.options.length === 1) {
    try {
      const stations = await api("/stations");
      const cities = [...new Set(stations.map((s) => s.city))].sort();
      cityFilter.innerHTML = '<option value="">Toutes les villes</option>' + cities.map((c) => `<option value="${c}">${c}</option>`).join("");
    } catch (e) {
      console.error("Erreur chargement villes:", e);
    }
  }
  
  const city = cityFilter.value;
  const mode = document.getElementById("sessions-mode-filter").value;
  const qs = new URLSearchParams({ page, per_page: 20 });
  if (city) qs.set("city", city);
  if (mode) qs.set("mode", mode);
  
  try {
    const data = await api("/sessions?" + qs.toString());
    const tbody = document.getElementById("sessions-view-tbody");
    
    if (!data.items.length) {
      tbody.innerHTML = '<tr><td class="empty" colspan="9">Aucune session trouvée pour ce filtre. Essayez « Tous les modes » ou un autre mode (classique / v1g / v2g).</td></tr>';
      renderPagination("sessions-view-pagination", data.page || page, data.pages || 1, "loadSessionsView");
      return;
    }
    
    tbody.innerHTML = data.items.map((s) => `
      <tr>
        <td>${s.session_code}</td>
        <td>${s.vehicle ? s.vehicle.model : "—"}</td>
        <td>${s.station ? s.station.city : "—"}</td>
        <td>${new Date(s.start_time).toLocaleString("fr-TN")}</td>
        <td>${s.energy_kwh} kWh</td>
        <td>${fmtTND(s.cost_tnd)}</td>
        <td><span class="badge ${s.mode}">${s.mode}</span></td>
        <td>${s.soc_start_pct}% → ${s.soc_end_pct}%</td>
        <td><a href="#" onclick="selectForOptim(${s.id}, '${s.session_code}'); return false;">Optimiser →</a></td>
      </tr>
    `).join("");
    renderPagination("sessions-view-pagination", data.page || page, data.pages || 1, "loadSessionsView");
  } catch (e) {
    console.error("Erreur chargement sessions:", e);
    const tbody = document.getElementById("sessions-view-tbody");
    if (tbody) tbody.innerHTML = `<tr><td class="empty" colspan="9">Erreur: ${e.message}</td></tr>`;
  }
}

// ============================================================
// Analytics avancés
// ============================================================
async function loadAnalytics() {
  try {
    const stats = await api("/stats/analytics");
    
    // Mettre à jour les KPIs mensuels
    if (stats.monthly_stats.length > 0) {
      const latest = stats.monthly_stats[stats.monthly_stats.length - 1];
      document.getElementById("analytics-monthly-sessions").textContent = latest.sessions;
      document.getElementById("analytics-monthly-energy").textContent = latest.energy_kwh + " kWh";
      document.getElementById("analytics-monthly-cost").textContent = fmtTND(latest.cost_tnd);
      document.getElementById("analytics-monthly-co2").textContent = (latest.co2_kg || 0).toLocaleString("fr-TN") + " kg";
    }
    
    // Graphique mensuel
    renderMonthlyChart(stats.monthly_stats);
    
    // Graphique véhicules
    renderVehicleChart(stats.vehicle_stats);

    // Graphique CO2 mensuel
    renderCo2Chart(stats.monthly_stats);
  } catch (e) {
    console.error("Erreur chargement analytics:", e);
  }
}

function renderMonthlyChart(monthlyStats) {
  const ctx = document.getElementById("chart-monthly");
  if (charts.monthly) charts.monthly.destroy();
  
  charts.monthly = new Chart(ctx, {
    type: "line",
    data: {
      labels: monthlyStats.map(m => m.month),
      datasets: [
        {
          label: "Énergie (kWh)",
          data: monthlyStats.map(m => m.energy_kwh),
          borderColor: "#16A34A",
          backgroundColor: "#16A34A33",
          fill: true,
          tension: 0.25,
          yAxisID: "y"
        },
        {
          label: "Coût (TND)",
          data: monthlyStats.map(m => m.cost_tnd),
          borderColor: "#65A30D",
          backgroundColor: "#65A30D33",
          fill: true,
          tension: 0.25,
          yAxisID: "y1"
        }
      ]
    },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: true } },
      scales: {
        x: { grid: { color: "#E4EFE7" }, ticks: { color: "#5B7264" } },
        y: { position: "left", grid: { color: "#E4EFE7" }, ticks: { color: "#5B7264" } },
        y1: { position: "right", grid: { display: false }, ticks: { color: "#5B7264" } }
      }
    }
  });
}

function renderCo2Chart(monthlyStats) {
  const ctx = document.getElementById("chart-co2");
  if (!ctx) return;
  if (charts.co2) charts.co2.destroy();
  charts.co2 = new Chart(ctx, {
    type: "bar",
    data: {
      labels: monthlyStats.map(m => m.month),
      datasets: [{
        label: "CO₂ évité (kg)",
        data: monthlyStats.map(m => m.co2_kg || 0),
        backgroundColor: "#15803D",
        borderRadius: 4,
      }],
    },
    options: Object.assign(baseChartOptions(false), {
      plugins: { legend: { display: false } },
    }),
  });
}

function renderVehicleChart(vehicleStats) {
  const ctx = document.getElementById("chart-vehicles");
  if (charts.vehicles) charts.vehicles.destroy();
  
  charts.vehicles = new Chart(ctx, {
    type: "bar",
    data: {
      labels: vehicleStats.map(v => v.model),
      datasets: [
        {
          label: "Sessions",
          data: vehicleStats.map(v => v.sessions),
          backgroundColor: "#16A34A",
          borderRadius: 3
        },
        {
          label: "Énergie totale (kWh)",
          data: vehicleStats.map(v => v.total_energy_kwh),
          backgroundColor: "#0F766E",
          borderRadius: 3
        }
      ]
    },
    options: Object.assign(baseChartOptions(false), {
      plugins: { legend: { display: true } }
    })
  });
}

// ============================================================
// Profil utilisateur
// ============================================================
async function loadProfile() {
  try {
    const user = await api("/auth/me");
    document.getElementById("profile-username").value = user.username;
    document.getElementById("profile-email").value = user.email;
    document.getElementById("profile-user-type").value = user.user_type;
    document.getElementById("profile-v2g-balance").value = user.v2g_balance_tnd.toFixed(3) + " TND";
  } catch (e) {
    console.error("Erreur chargement profil:", e);
  }
}

async function updateProfile() {
  const profileData = {
    username: document.getElementById("profile-username").value,
    email: document.getElementById("profile-email").value,
    user_type: document.getElementById("profile-user-type").value
  };
  
  try {
    await api("/auth/me", { method: "PUT", body: JSON.stringify(profileData) });
    alert("Profil mis à jour avec succès !");
    CURRENT_USER = await api("/auth/me");
    document.getElementById("me-username").textContent = CURRENT_USER.username;
  } catch (e) {
    alert("Erreur lors de la mise à jour: " + e.message);
  }
}

async function changePassword() {
  const current = document.getElementById("current-password").value;
  const newPass = document.getElementById("new-password").value;
  const confirm = document.getElementById("confirm-password").value;
  
  if (newPass !== confirm) {
    alert("Les mots de passe ne correspondent pas");
    return;
  }
  
  try {
    await api("/auth/change-password", { 
      method: "POST", 
      body: JSON.stringify({ current_password: current, new_password: newPass }) 
    });
    alert("Mot de passe changé avec succès !");
    document.getElementById("current-password").value = "";
    document.getElementById("new-password").value = "";
    document.getElementById("confirm-password").value = "";
  } catch (e) {
    alert("Erreur lors du changement de mot de passe: " + e.message);
  }
}

// ============================================================
// Paramètres
// ============================================================
function loadSettings() {
  document.getElementById("setting-health-weight").value = SETTINGS.health_weight;
  document.getElementById("setting-max-cycles").value = SETTINGS.max_cycles;
  document.getElementById("auto-refresh-switch").classList.toggle("on", SETTINGS.auto_refresh);
  document.getElementById("v2g-notif-switch").classList.toggle("on", SETTINGS.v2g_notifications);
}

function saveSettings() {
  SETTINGS.health_weight = parseFloat(document.getElementById("setting-health-weight").value);
  SETTINGS.max_cycles = parseInt(document.getElementById("setting-max-cycles").value);
  localStorage.setItem("t2g_settings", JSON.stringify(SETTINGS));
}

function toggleAutoRefresh() {
  SETTINGS.auto_refresh = !SETTINGS.auto_refresh;
  document.getElementById("auto-refresh-switch").classList.toggle("on", SETTINGS.auto_refresh);
  
  if (SETTINGS.auto_refresh) {
    AUTO_REFRESH_INTERVAL = setInterval(refreshAll, 60000); // Rafraîchir chaque minute
  } else {
    clearInterval(AUTO_REFRESH_INTERVAL);
  }
  
  localStorage.setItem("t2g_settings", JSON.stringify(SETTINGS));
}

function toggleV2GNotifications() {
  SETTINGS.v2g_notifications = !SETTINGS.v2g_notifications;
  document.getElementById("v2g-notif-switch").classList.toggle("on", SETTINGS.v2g_notifications);
  localStorage.setItem("t2g_settings", JSON.stringify(SETTINGS));
  if (SETTINGS.v2g_notifications) {
    notifyV2GOpportunity("Les alertes V2G sont activées. Optimisez une session en pointe pour maximiser vos gains.");
  }
}

// ============================================================
// Fonctions de prédiction
// ============================================================
async function predictCost() {
  const battery = parseFloat(document.getElementById('pred-battery').value);
  const socStart = parseInt(document.getElementById('pred-soc-start').value);
  const socTarget = parseInt(document.getElementById('pred-soc-target').value);
  const power = parseFloat(document.getElementById('pred-power').value);
  const startTime = document.getElementById('pred-start-time').value;
  const temperature = parseFloat(document.getElementById('pred-temperature').value);
  
  if (!startTime) {
    alert("Veuillez spécifier l'heure de début");
    return;
  }
  
  // Convertir datetime-local en format compatible avec le backend
  const isoStartTime = startTime.replace('T', ' ') + ":00";
  
  try {
    const result = await api("/predict/cost", {
      method: "POST",
      body: JSON.stringify({
        battery_capacity: battery,
        soc_start: socStart,
        soc_target: socTarget,
        charging_power: power,
        start_time: isoStartTime,
        temperature: temperature
      })
    });
    
    const predictionResult = document.getElementById('prediction-result');
    if (predictionResult) {
      predictionResult.style.display = 'block';
      document.getElementById('pred-cost').textContent = fmtTND(result.predicted_cost_tnd);
      
      if (result.confidence_interval) {
        document.getElementById('pred-confidence').textContent = 
          `Intervalle: ${fmtTND(result.confidence_interval.min)} - ${fmtTND(result.confidence_interval.max)}`;
      } else {
        document.getElementById('pred-confidence').textContent = `Méthode: ${result.method}`;
      }
      
      if (result.estimated_duration_h) {
        document.getElementById('pred-duration').textContent = result.estimated_duration_h + " h";
      } else {
        document.getElementById('pred-duration').textContent = "—";
      }
    }
    
  } catch (e) {
    alert("Erreur lors de la prédiction: " + e.message);
  }
}

async function predictOptimalTime() {
  const flexibility = parseInt(document.getElementById('optimal-flexibility').value);
  
  try {
    const result = await api("/predict/optimal-time", {
      method: "POST",
      body: JSON.stringify({
        current_soc: parseInt(document.getElementById('pred-soc-start').value),
        target_soc: parseInt(document.getElementById('pred-soc-target').value),
        battery_capacity: parseFloat(document.getElementById('pred-battery').value),
        max_power: parseFloat(document.getElementById('pred-power').value),
        flexibility_hours: flexibility
      })
    });
    
    const container = document.getElementById('optimal-times-result');
    container.innerHTML = result.optimal_times.map((time, index) => `
      <div class="card" style="margin-bottom:8px;">
        <div class="card-title">
          <span>${index + 1}. ${time.start_time}</span>
          <span class="badge ${time.tariff_type === 'Heures creuses' ? 'v1g' : time.tariff_type === 'Heures de pointe' ? 'v2g' : 'classique'}">${time.tariff_type}</span>
        </div>
        <div style="font-size:13px; color:var(--muted);">
          <div>Coût estimé: ${fmtTND(time.estimated_cost_tnd)} | Durée: ${time.estimated_duration_h}h</div>
          <div>${time.recommendation}</div>
        </div>
      </div>
    `).join('');
    
  } catch (e) {
    alert("Erreur lors de la prédiction: " + e.message);
  }
}

async function loadRecommendations() {
  try {
    const result = await api("/advice/personalized");
    const container = document.getElementById('advice-container');
    
    if (!container) {
      console.error("Container 'advice-container' not found");
      return;
    }

    const profile = result.user_profile || {};
    const meta = document.getElementById('advice-meta');
    if (meta) {
      meta.textContent = `${profile.total_sessions || 0} sessions · coût moyen ${profile.avg_cost || 0} TND · ML ${profile.ml_trained ? "entraîné" : "tarifaire"}`;
    }
    
    if (!result.advice || result.advice.length === 0) {
      container.innerHTML = '<div class="empty">Aucun conseil disponible pour le moment.</div>';
      return;
    }
    
    container.innerHTML = result.advice.map(advice => `
      <div class="card" style="margin-bottom:12px; border-left: 3px solid ${advice.priority === 'high' ? 'var(--danger)' : advice.priority === 'medium' ? 'var(--discharge)' : 'var(--charge)'};">
        <div class="card-title">
          <span>${advice.category} - ${advice.title}</span>
          <span class="badge ${advice.priority === 'high' ? 'v2g' : advice.priority === 'medium' ? 'v1g' : 'classique'}">${advice.priority}</span>
        </div>
        <div style="font-size:13px; color:var(--text); margin-bottom:8px;">
          ${advice.advice}
        </div>
        <div style="font-size:12px; color:var(--muted);">
          <strong>Action:</strong> ${advice.action}<br>
          <strong>Impact:</strong> ${advice.potential_savings}
        </div>
      </div>
    `).join('');
    
  } catch (e) {
    console.error("Erreur chargement conseils:", e);
    const container = document.getElementById('advice-container');
    if (container) {
      container.innerHTML = '<div class="empty">Erreur lors du chargement des conseils: ' + e.message + '</div>';
    }
  }
}

async function trainMlModel() {
  try {
    const result = await api("/ml/train", { method: "POST", body: JSON.stringify({}) });
    alert(result.message + (result.training_samples ? ` (${result.training_samples} échantillons)` : ""));
    loadRecommendations();
  } catch (e) {
    alert("Entraînement ML: " + e.message);
  }
}

function notifyV2GOpportunity(message) {
  if (!SETTINGS.v2g_notifications) return;
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification("T2G — Opportunité V2G", { body: message });
  } else if ("Notification" in window && Notification.permission !== "denied") {
    Notification.requestPermission().then(p => {
      if (p === "granted") new Notification("T2G — Opportunité V2G", { body: message });
    });
  } else {
    console.info("[V2G]", message);
  }
}

// ============================================================
// Administration (rôle admin)
// ============================================================
async function loadAdmin() {
  try {
    const [stats, users, audit] = await Promise.all([
      api("/admin/stats"),
      api("/admin/users"),
      api("/admin/audit").catch(() => []),
    ]);

    // KPIs globaux
    document.getElementById("admin-kpis").innerHTML = [
      ["Utilisateurs", stats.users.total, `${stats.users.admins} admin(s)`],
      ["Sessions totales", stats.sessions.total, "toutes périodes"],
      ["Énergie livrée", stats.energy_kwh.toLocaleString("fr-TN") + " kWh", ""],
      ["Coût total", fmtTND(stats.cost_tnd), ""],
      ["Gains V2G", fmtTND(stats.v2g_gain_tnd), ""],
      ["CO₂ évité", stats.co2_avoided_kg.toLocaleString("fr-TN") + " kg", "estimation réseau"],
      ["Stations", stats.stations.total, ""],
      ["Véhicules", stats.vehicles.total, ""],
    ].map(([label, value, delta]) => `
      <div class="analytics-card">
        <div class="analytics-label">${label}</div>
        <div class="analytics-value" style="font-size:20px;">${value}</div>
        <div style="font-size:11px;color:var(--muted-2);margin-top:4px;">${delta}</div>
      </div>`).join("");

    // Graphiques
    const topCities = [...stats.by_city].sort((a, b) => b.sessions - a.sessions).slice(0, 8);
    if (charts.adminCity) charts.adminCity.destroy();
    charts.adminCity = new Chart(document.getElementById("chart-admin-city"), {
      type: "bar",
      data: {
        labels: topCities.map(c => c.city),
        datasets: [{ data: topCities.map(c => c.sessions), backgroundColor: "#16A34A", borderRadius: 3 }],
      },
      options: Object.assign(baseChartOptions(true), { indexAxis: "y" }),
    });

    const modeColors = { classique: "#8AA094", v1g: "#16A34A", v2g: "#65A30D" };
    if (charts.adminMode) charts.adminMode.destroy();
    charts.adminMode = new Chart(document.getElementById("chart-admin-mode"), {
      type: "doughnut",
      data: {
        labels: stats.by_mode.map(m => m.mode),
        datasets: [{ data: stats.by_mode.map(m => m.sessions), backgroundColor: stats.by_mode.map(m => modeColors[m.mode] || "#0F766E") }],
      },
      options: { responsive: true, plugins: { legend: { position: "bottom", labels: { color: "#5B7264", font: { size: 11 } } } } },
    });

    // Tableau utilisateurs
    document.getElementById("admin-users-count").textContent = `${users.length} comptes`;
    document.getElementById("admin-users-tbody").innerHTML = users.map(u => `
      <tr>
        <td>${u.user_code}</td>
        <td><b>${u.username}</b></td>
        <td>${u.email}</td>
        <td><span class="badge ${u.role === 'admin' ? 'v2g' : 'classique'}">${u.role}</span></td>
        <td>${u.user_type}</td>
        <td><span class="badge ${u.is_active ? 'v1g' : 'classique'}">${u.is_active ? 'actif' : 'suspendu'}</span></td>
        <td>${u.sessions}</td>
        <td>${u.energy_kwh} kWh</td>
        <td>${fmtTND(u.cost_tnd)}</td>
        <td>${u.v2g_balance_tnd.toFixed(3)} TND</td>
        <td style="white-space:nowrap;">
          ${u.is_active
            ? `<button class="ghost" style="padding:4px 8px;font-size:11px;" onclick="adminSuspend(${u.id}, '${u.username}')">Suspendre</button>`
            : `<button class="ghost" style="padding:4px 8px;font-size:11px;" onclick="adminActivate(${u.id}, '${u.username}')">Activer</button>`}
          <button class="ghost" style="padding:4px 8px;font-size:11px;" onclick="adminResetPwd(${u.id}, '${u.username}')">Reset MDP</button>
        </td>
      </tr>`).join("");

    // Journal d'audit
    document.getElementById("admin-audit-tbody").innerHTML = (audit || []).length
      ? audit.map(l => `
        <tr>
          <td>${l.created_at ? new Date(l.created_at).toLocaleString("fr-TN") : "—"}</td>
          <td>${l.admin || "—"}</td>
          <td><code>${l.action}</code></td>
          <td>${l.target_type || ""} ${l.target_id || ""}</td>
          <td>${l.details || ""}</td>
        </tr>`).join("")
      : '<tr><td class="empty" colspan="5">Aucune action enregistrée</td></tr>';

    // Comptes récents
    document.getElementById("admin-recent-users").innerHTML = stats.recent_users.map(u => `
      <div class="card" style="display:flex;justify-content:space-between;align-items:center;padding:10px 14px;">
        <span><b>${u.username}</b> <span style="color:var(--muted);font-size:12px;">(${u.email})</span></span>
        <span class="badge ${u.role === 'admin' ? 'v2g' : 'classique'}">${u.role}</span>
      </div>`).join("");
  } catch (e) {
    document.getElementById("admin-kpis").innerHTML = `<div class="empty">Accès refusé : ${e.message}</div>`;
  }
}

// ============================================================
// Actions admin sur les utilisateurs
// ============================================================
async function adminSuspend(id, username) {
  if (!confirm(`Suspendre le compte "${username}" ? Il ne pourra plus se connecter.`)) return;
  try {
    await api(`/admin/users/${id}/suspend`, { method: "POST", body: JSON.stringify({}) });
    loadAdmin();
  } catch (e) { alert("Erreur : " + e.message); }
}

async function adminActivate(id, username) {
  try {
    await api(`/admin/users/${id}/activate`, { method: "POST", body: JSON.stringify({}) });
    loadAdmin();
  } catch (e) { alert("Erreur : " + e.message); }
}

async function adminResetPwd(id, username) {
  const pw = prompt(`Nouveau mot de passe pour "${username}" (8 caractères min., lettres + chiffres) :`);
  if (!pw) return;
  try {
    await api(`/admin/users/${id}/reset-password`, { method: "POST", body: JSON.stringify({ password: pw }) });
    alert(`Mot de passe de ${username} réinitialisé.`);
    loadAdmin();
  } catch (e) { alert("Erreur : " + e.message); }
}

async function runV2GStudy() {
  const btn = document.getElementById("v2g-study-btn");
  const box = document.getElementById("v2g-study-result");
  btn.disabled = true;
  btn.textContent = "Calcul en cours…";
  try {
    const r = await api("/admin/v2g-study?sample=60", { timeoutMs: 120000 });
    const card = (label, value, sub) => `
      <div class="analytics-card">
        <div class="analytics-label">${label}</div>
        <div class="analytics-value" style="font-size:20px;">${value}</div>
        <div style="font-size:11px;color:var(--muted-2);margin-top:4px;">${sub || ""}</div>
      </div>`;
    document.getElementById("v2g-study-kpis").innerHTML = [
      card("Sessions dataset", r.dataset_sessions, ""),
      card("V1G — économies", fmtTND(r.v1g.savings_tnd), `${r.v1g.savings_pct} % sur ${r.v1g.sessions} sessions (estimation)`),
      card("V2G — économies nettes", fmtTND(r.v2g_sample.savings_tnd), `${r.v2g_sample.savings_pct} % sur ${r.v2g_sample.sample_size} sessions (LP réel)`),
      card("V2G — revenus revente", fmtTND(r.v2g_sample.gain_tnd), "échantillon optimisé"),
    ].join("");
    document.getElementById("v2g-study-note").textContent = r.note || "";
    box.style.display = "block";
  } catch (e) {
    alert("Erreur étude : " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Lancer l'étude (~30 s)";
  }
}

// ============================================================
// Réservations
// ============================================================
let ALL_STATIONS = [];
let USER_VEHICLES = [];

async function loadBookings() {
  try {
    const [bookings, stations, vehicles] = await Promise.all([
      api("/bookings"),
      api("/stations"),
      api("/vehicles"),
    ]);
    ALL_STATIONS = stations;
    USER_VEHICLES = vehicles;

    const stSel = document.getElementById("booking-station");
    stSel.innerHTML = stations.map(s =>
      `<option value="${s.id}">${s.city} — ${s.area || s.station_code} (${s.charger_type || "?"})</option>`).join("");
    const vSel = document.getElementById("booking-vehicle");
    vSel.innerHTML = vehicles.length
      ? vehicles.map(v => `<option value="${v.id}">${v.model} (${v.battery_capacity_kwh} kWh)</option>`).join("")
      : '<option value="">— ajoutez d\'abord un véhicule —</option>';

    const tbody = document.getElementById("bookings-tbody");
    if (!bookings.length) {
      tbody.innerHTML = '<tr><td class="empty" colspan="8">Aucune réservation. Créez-en une ci-dessus.</td></tr>';
      return;
    }
    tbody.innerHTML = bookings.map(b => `
      <tr>
        <td>${b.station ? b.station.station_code : "—"}</td>
        <td>${b.station ? b.station.city : "—"}</td>
        <td>${b.vehicle ? b.vehicle.model : "—"}</td>
        <td>${b.start_time ? new Date(b.start_time).toLocaleString("fr-TN") : "—"}</td>
        <td>${b.end_time ? new Date(b.end_time).toLocaleString("fr-TN") : "—"}</td>
        <td><span class="badge ${b.status === 'confirmée' ? 'v1g' : 'classique'}">${b.status}</span></td>
        <td>${b.notes || "—"}</td>
        <td>
          ${b.status === 'confirmée' ? `<button class="ghost" style="padding:4px 8px;font-size:11px;" onclick="cancelBooking(${b.id})">Annuler</button>` : ""}
          <button class="ghost" style="padding:4px 8px;font-size:11px;color:var(--danger);" onclick="deleteBooking(${b.id})">✕</button>
        </td>
      </tr>`).join("");
  } catch (e) {
    console.error("Erreur réservations:", e);
  }
}

async function createBooking() {
  const err = document.getElementById("booking-error");
  err.textContent = "";
  const station_id = parseInt(document.getElementById("booking-station").value);
  const vehicle_id = parseInt(document.getElementById("booking-vehicle").value);
  const start = document.getElementById("booking-start").value;
  const end = document.getElementById("booking-end").value;
  const notes = document.getElementById("booking-notes").value;
  if (!station_id || !vehicle_id || !start || !end) {
    err.textContent = "Station, véhicule, début et fin sont requis.";
    return;
  }
  try {
    await api("/bookings", {
      method: "POST",
      body: JSON.stringify({ station_id, vehicle_id, start_time: start.replace("T", " "), end_time: end.replace("T", " "), notes }),
    });
    document.getElementById("booking-notes").value = "";
    loadBookings();
  } catch (e) { err.textContent = e.message; }
}

async function cancelBooking(id) {
  try {
    await api(`/bookings/${id}`, { method: "PUT", body: JSON.stringify({ status: "annulée" }) });
    loadBookings();
  } catch (e) { alert("Erreur : " + e.message); }
}

async function deleteBooking(id) {
  if (!confirm("Supprimer définitivement cette réservation ?")) return;
  try {
    await api(`/bookings/${id}`, { method: "DELETE" });
    loadBookings();
  } catch (e) { alert("Erreur : " + e.message); }
}

// ============================================================
// Carte des stations (Leaflet)
// ============================================================
let stationsMap = null;
let stationsMarkers = [];

async function loadMap() {
  const container = document.getElementById("stations-map");
  try {
    const stations = await api("/stations");
    ALL_STATIONS = stations;
    const cities = [...new Set(stations.map(s => s.city))].sort();
    const sel = document.getElementById("map-city-filter");
    if (sel.options.length === 1) {
      sel.innerHTML = '<option value="">Toutes les villes</option>' + cities.map(c => `<option value="${c}">${c}</option>`).join("");
    }
    if (typeof L === "undefined") {
      container.innerHTML = '<div class="empty" style="padding:40px;">La bibliothèque cartographique (Leaflet) n\'a pas pu être chargée. Vérifiez votre connexion ou désactivez votre bloqueur de contenu.</div>';
      return;
    }
    // Correction classique : icônes par défaut de Leaflet cassées en CDN
    if (!L.Icon.Default._iconFix) {
      L.Icon.Default.mergeOptions({
        iconRetinaUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png",
        iconUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png",
        shadowUrl: "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png",
      });
      L.Icon.Default._iconFix = true;
    }
    if (!stationsMap) {
      stationsMap = L.map("stations-map").setView([34.0, 9.5], 7);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: "© OpenStreetMap",
        maxZoom: 18,
      }).addTo(stationsMap);
    }
    renderMapMarkers();
    setTimeout(() => stationsMap.invalidateSize(), 150);
  } catch (e) {
    console.error("Erreur carte:", e);
    container.innerHTML = '<div class="empty" style="padding:40px;">Erreur de chargement : ' + escapeHtml(e.message) + '</div>';
  }
}

function renderMapMarkers() {
  if (!stationsMap) return;
  const city = document.getElementById("map-city-filter").value;
  stationsMarkers.forEach(m => stationsMap.removeLayer(m));
  stationsMarkers = [];
  const withCoords = ALL_STATIONS.filter(s => s.latitude != null && s.longitude != null
    && (!city || s.city === city));
  // Marqueur CSS intégré (pas d'image externe à télécharger)
  const pinIcon = L.divIcon({
    className: "station-pin",
    html: '<div style="width:22px;height:22px;border-radius:50% 50% 50% 0;background:#16A34A;border:2px solid #fff;transform:rotate(-45deg);box-shadow:0 2px 6px rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;"><div style="transform:rotate(45deg);color:#fff;font-size:11px;font-weight:700;">⚡</div></div>',
    iconSize: [22, 22],
    iconAnchor: [11, 22],
    popupAnchor: [0, -20],
  });
  withCoords.forEach(s => {
    const marker = L.marker([s.latitude, s.longitude], { icon: pinIcon }).addTo(stationsMap);
    marker.bindPopup(
      `<b>${s.city} — ${s.area || ""}</b><br>` +
      `Type : ${s.charger_type || "—"} (${s.charger_power_kw || "?"} kW)<br>` +
      `Code : ${s.station_code}`
    );
    stationsMarkers.push(marker);
  });
  if (withCoords.length) {
    const group = L.featureGroup(stationsMarkers);
    stationsMap.fitBounds(group.getBounds().pad(0.15));
  }
}

// ============================================================
// Export CSV
// ============================================================
async function exportSessionsCSV() {
  try {
    const res = await fetch(API_BASE + "/export/sessions.csv", {
      headers: { "Authorization": "Bearer " + TOKEN },
    });
    if (!res.ok) throw new Error("Export impossible (" + res.status + ")");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "t2g_sessions.csv";
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) { alert("Erreur export : " + e.message); }
}

// ============================================================
// Chatbot / Agent T2G
// ============================================================
let CHAT_OPEN = false;
let CHAT_HISTORY = [];

function toggleChat() {
  CHAT_OPEN = !CHAT_OPEN;
  document.getElementById("chat-panel").classList.toggle("open", CHAT_OPEN);
  if (CHAT_OPEN && !document.getElementById("chat-messages").children.length) {
    addChatMsg("bot", "Bonjour ! Je suis l'assistant T2G. Posez-moi une question ou demandez-moi d'agir (ex. « optimise ma dernière session »).");
    renderChatSuggestions(["Conseils personnalisés", "Tarifs TND/kWh", "Explique le V2G", "Mon bilan", "Impact CO₂", "Optimise ma dernière session"]);
  }
  if (CHAT_OPEN) document.getElementById("chat-input").focus();
}

function renderChatSuggestions(suggestions) {
  document.getElementById("chat-suggestions").innerHTML = suggestions.map(s =>
    `<span class="chip" onclick="sendChatSuggestion(decodeURIComponent('${encodeURIComponent(s)}'))">${escapeHtml(s)}</span>`
  ).join("");
}

function sendChatSuggestion(text) {
  document.getElementById("chat-input").value = text;
  sendChat();
}

function addChatMsg(role, text, actionsHtml) {
  const box = document.getElementById("chat-messages");
  const div = document.createElement("div");
  div.className = "msg " + role;
  const formatted = escapeHtml(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  div.innerHTML = formatted + (actionsHtml ? `<div class="msg-actions">${actionsHtml}</div>` : "");
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderChatActions(actions) {
  if (!actions || !actions.length) return "";
  return actions.map(a => {
    if (a.type === "optimal_times" && a.data) {
      return `<table class="opt-table"><tr><th>Créneau</th><th>Tarif</th><th>Coût</th></tr>` +
        a.data.slice(0, 3).map(t => `<tr><td>${t.start_time}</td><td>${t.tariff_type}</td><td>${t.estimated_cost_tnd} TND</td></tr>`).join("") + `</table>`;
    }
    if (a.type === "cost_prediction" && a.data) {
      const p = a.data;
      return `<div style="font-size:12px;margin-top:4px;">Coût prédit : <b>${p.predicted_cost_tnd} TND</b>${p.estimated_duration_h ? ` · ${p.estimated_duration_h} h` : ""}</div>`;
    }
    if (a.type === "optimization" && a.data) {
      const o = a.data;
      return `<div style="font-size:12px;margin-top:4px;">💰 Économie : <b>${o.savings_tnd} TND (${o.savings_pct} %)</b>${o.v2g_gain_tnd ? ` · gain V2G ${o.v2g_gain_tnd} TND` : ""}</div>`;
    }
    return "";
  }).join("");
}

async function sendChat() {
  const input = document.getElementById("chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  addChatMsg("user", text);
  CHAT_HISTORY.push({ role: "user", content: text });

  const thinking = addChatMsg("bot", "…");
  try {
    const res = await api("/chat", {
      method: "POST",
      body: JSON.stringify({ message: text, history: CHAT_HISTORY.slice(-10) }),
    });
    thinking.remove();
    addChatMsg("bot", res.reply || "…", renderChatActions(res.actions));
    CHAT_HISTORY.push({ role: "assistant", content: res.reply });
    renderChatSuggestions(res.suggestions || []);
  } catch (e) {
    thinking.remove();
    addChatMsg("bot", "Erreur : " + e.message);
  }
}

// ============================================================
// Initialisation
// ============================================================
(function init() {
  // Charger les paramètres sauvegardés
  const savedSettings = localStorage.getItem("t2g_settings");
  if (savedSettings) {
    SETTINGS = JSON.parse(savedSettings);
  }
  
  // Initialiser le datetime-local avec l'heure actuelle
  const now = new Date();
  const localIsoString = new Date(now.getTime() - (now.getTimezoneOffset() * 60000)).toISOString().slice(0, 16);
  const timeInput = document.getElementById('pred-start-time');
  if (timeInput) {
    timeInput.value = localIsoString;
  }
  
  // Auto-login si token déjà en cache
  if (TOKEN) {
    (async function() {
      try {
        CURRENT_USER = await api("/auth/me");
        showApp();
      } catch (e) {
        logout();
      }
    })();
  }
})();
