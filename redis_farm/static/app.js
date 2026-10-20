/* Redis Farm frontend. No CDN, no localStorage, no persisted command history. */
"use strict";
const tr = window.RFI18n.t;

const $ = (id) => document.getElementById(id);
const state = {
  authMode: document.body.dataset.initialProfile || "admin", profileSelection: {admin:null,default:null}, accessServiceId:null, lastAddition:null,
  panelLogin: false, csrf: "", unlocked: false, cliAvailable: true, customDns: false, services: [], selected: null, snapshot: null, topology: null,
  picked: {redis: null, sentinel: null}, kind: "redis", topic: "acl",
  terminals: {redis: null, sentinel: null}, drafts: {redis:"", sentinel:""}, snippets: [], preview: null,
  detailCache: new Map(), busySnapshots: new Set(), selectEpoch: 0, detailEpoch: 0,
  discoveryId: null, discoveryMode: "node", discoveryRunning: false, discoveryCancelRequested: false, refreshSeconds: 10, sessionLive: true, history: [], historyPos: 0
};
const uiText = {"tagline": tr("Весь редис на одной грядке"), "defaultTab": "default", "passwords": tr("Пароли сервиса"), "defaultHint": tr("Выберите сервис в каталоге или добавьте новую ноду. Пароль admin не нужен."), "authService": tr("default · пароль выбранного сервиса / инстанса"), "credentialTitle": tr("Доступ default"), "credentialScopeLabel": tr("Область пароля"), "serviceScope": tr("Весь выбранный сервис (его master и replicas)"), "endpointScope": tr("Только инстанс"), "passwordLabel": tr("Пароль default"), "passwordPlaceholder": tr("Пароль"), "usePassword": tr("Использовать пароль"), "forgetScope": tr("Забыть пароль"), "deleteOverride": tr("Удалить переопределение"), "defaultSeedOption": "default", "defaultSeedLabel": tr("Пароль (необязательно)"), "passwordPortLabel": tr("Redis-порт для этого пароля"), "seedPasswordHelp": tr("При сканировании пароль применяется только к указанному Redis-порту и связанным участникам его сервиса. Другие инстансы добавятся без пароля — его можно ввести позже. Оставьте поле пустым для обнаружения без пароля."), "savedTitle": tr("Добавление завершено"), "gotoCatalog": tr("Перейти в каталог"), "addedButton": tr("Добавлено"), "seedFooter": tr("Sentinel — без пароля"), "chooseService": tr("Выберите сервис"), "noPassword": tr("Пароль не введён"), "authorized": tr("Авторизован"), "defaultRequired": tr("Введите пароль default для выбранного сервиса или инстанса."), "defaultReady": tr("Пароль принят."), "scopeForgotten": tr("Пароль удалён из сессии."), "adminForgotten": tr("Пароль admin удалён. Пароли default не изменены."), "defaultForgotten": tr("Пароли default выбранного сервиса удалены."), "enterDefault": tr("Ввести пароль default"), "scanDone": tr("Сканирование завершено, но сервисы не добавлены."), "openAccess": tr("Пароль не требуется"), "partialAccess": tr("Доступ к части инстансов"), "authModeLabel": tr("Подключение"), "passwordPending": tr("требуется пароль default"), "currentServiceLabel": tr("Сервис"), "defaultSummary": tr("default · без логина"), "allDefaultHint": tr("Выбранные здесь сервисы не используют общий пароль admin."), "confirmSaved": tr("В каталоге сохранено"), "credentialCancel": tr("Закрыть")};
const profileTerminals = {admin:{redis:null,sentinel:null}, default:{redis:null,sentinel:null}};
state.terminals = profileTerminals[state.authMode];
const modes = {standalone:"Standalone", replication:"Replication", sentinel:"Sentinel", cluster:"Redis Cluster"};
const commandNames = {
  server:tr("Сведения о сервере"), replication:tr("Репликация"), role:tr("Текущая роль"), memory:tr("Память"),
  persistence:"RDB / AOF", statistics:tr("Статистика"), clients:tr("Клиентские соединения"),
  users:tr("Список пользователей"), acl:tr("ACL в памяти инстанса"), acl_user:tr("Пользователь admin"),
  aclfile:tr("Путь ACL-файла"), acl_save:tr("Сохранить ACL-файл"), config_memory:tr("Лимит памяти"),
  config_rewrite:tr("Записать текущую конфигурацию"), slowlog:tr("Последние медленные команды"),
  latency:tr("События задержек"), keyspace:tr("Статистика баз"), masters:tr("Все наблюдаемые группы"),
  master:tr("Выбранная master-группа"), replicas:tr("Реплики группы"), sentinels:tr("Другие Sentinel"),
  master_addr:tr("Адрес master по данным Sentinel"), quorum:tr("Проверка кворума по запросу"),
  failover:tr("Переключить master"), info:tr("Сведения Sentinel"), flushconfig:tr("Сохранить конфигурацию Sentinel"),
  cluster_info:tr("Состояние Redis Cluster"), cluster_nodes:tr("Участники и слоты"), cluster_shards:tr("Шарды (Redis 7+)")
};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function replaceChildren(id, ...nodes) { $(id).replaceChildren(...nodes.filter(Boolean)); }
function shown(value) { return value === undefined || value === null || value === "" ? "—" : String(value); }
function formatNumber(value) { return value === undefined || value === null ? "—" : Number(value).toLocaleString(window.RFI18n.locale); }
function bytes(value) {
  if (value === undefined || value === null || value === "") return "—";
  let n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let index = 0;
  while (Math.abs(n) >= 1024 && index < units.length - 1) { n /= 1024; index++; }
  return `${n.toLocaleString(window.RFI18n.locale, {maximumFractionDigits:index ? 2 : 0})} ${units[index]}`;
}
function dateTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString(window.RFI18n.locale, {day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit"}) : tr("Ещё не опрошено");
}
function statusClass(value) { return value === true ? "up" : value === false ? "down" : ""; }
function authMode(item) { return item?.auth_mode || item?.endpoints?.find(e=>e.kind === "redis")?.auth || "admin"; }
function profile(item) { return ["admin", "user"].includes(authMode(item)) ? "admin" : "default"; }
function profileServices() { return state.services.filter(s=>profile(s) === state.authMode); }
function endpointUnlocked(ep, item=service()) {
  if (!ep || ep.auth === "none") return true;
  return ep.kind === "redis" && ep.auth === "admin" ? state.unlocked : !!item?.credentials?.unlocked_endpoints?.includes(ep.id);
}
function service() { return state.services.find(s => s.id === state.selected); }
function nodes() {
  if (state.snapshot?.service_id === state.selected) return state.snapshot.nodes;
  if (state.topology?.service_id === state.selected) return state.topology.nodes;
  return [];
}
function rowHasEndpoint(row, id) {
  return row.endpoint.id === id || row.endpoint_ids?.includes(id);
}
function selectedEndpoint() {
  const id = state.picked[state.kind], row = nodes().find(n => rowHasEndpoint(n, id));
  return row?.endpoint_options?.find(e => e.id === id) ||
    service()?.endpoints.find(e => e.id === id) || row?.endpoint || null;
}
function selectedNode() { return nodes().find(n => rowHasEndpoint(n, state.picked[state.kind])); }
function kv(entries) {
  const list = el("dl", "kv");
  for (const [key, value] of entries) list.append(el("dt", "", key), el("dd", "", shown(value)));
  return list;
}

let toastTimer;
function notify(message, error=false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast${error ? " error" : ""}`;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, error ? 11000 : 5500);
}
function errorMessage(err) {
  const messages = {
    default_password_required:uiText.defaultRequired,
    password_required:tr("Введите пароль."),
    session_expired:tr("Локальная сессия завершена. Перезагрузите страницу и повторно введите пароль Redis."),
    cli_unavailable:tr("redis-cli недоступен. Установите redis-tools и перезапустите приложение."),
    terminal_closed:tr("Процесс redis-cli закрыт. Откройте новую консоль; команды автоматически не повторяются."),
    cancelled:tr("Операция остановлена."),
    csrf:tr("Токен локальной сессии устарел. Перезагрузите страницу.")
  };
  return messages[err.code] || window.RFI18n.error(err.message || String(err), err.code);
}
async function api(path, options={}) {
  const headers = {"Accept":"application/json", ...(options.headers || {})};
  if (options.method && !["GET", "HEAD"].includes(options.method)) {
    headers["X-RF-CSRF"] = state.csrf;
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {credentials:"same-origin", cache:"no-store", ...options, headers});
  let data;
  try { data = await response.json(); }
  catch { throw new Error(tr`Неверный ответ HTTP ${response.status}`); }
  if (!response.ok) {
    const error = new Error(data.error || `HTTP ${response.status}`);
    error.code = data.code;
    if (data.code === "login_required" || (data.code === "session_expired" && state.panelLogin)) {
      state.sessionLive = false; window.location.replace("/login");
    }
    if (data.code === "csrf" && state.panelLogin) {
      // Another tab may have changed the authenticated session. Reload the UI;
      // never retry a modifying request or a Redis command automatically.
      state.sessionLive = false;
      window.location.replace("/");
    }
    if (data.code === "session_expired") {
      state.sessionLive = false; state.unlocked = false;
      for (const terminal of Object.values(state.terminals)) if (terminal) terminal.alive = false;
      renderCredential(); renderConsole();
    }
    throw error;
  }
  return data;
}
const post = (path, body={}) => api(path, {method:"POST", body:JSON.stringify(body)});
const delay = (ms) => new Promise(resolve => setTimeout(resolve, ms));
async function job(path, body={}, onStart, onProgress) {
  const start = await post(path, body);
  if (onStart) await onStart(start.job_id);
  for (;;) {
    const result = await api(`/api/jobs/${start.job_id}`);
    if (onProgress && result.progress) onProgress(result.progress);
    if (result.state === "done") return result.result;
    if (result.state === "error" || result.state === "cancelled") {
      const error = new Error(result.error || tr("Операция остановлена"));
      error.code = result.code || "cancelled";
      throw error;
    }
    await delay(500);
  }
}

function chooseConsoleKind(kind) {
  if (kind === state.kind) return;
  state.drafts[state.kind] = $("commandInput").value;
  state.kind = kind;
  $("commandInput").value = state.drafts[kind] || "";
}

function usesGlobal() { return state.authMode === "admin" && (!service() || authMode(service()) === "admin"); }
function accessLabel(ep) {
  if (!ep || ep.auth === "none") return tr("No authentication");
  if (ep.auth === "user") return ep.username;
  return ep.auth === "admin" ? document.body.dataset.adminUser || "admin" : "default";
}
function renderCredential() {
  const shared = usesGlobal(), item = service();
  for (const b of $("authTabs").querySelectorAll("button")) b.classList.toggle("active", b.dataset.auth === state.authMode);
  $("credentialForm").hidden = !shared;
  $("defaultCredentialControls").hidden = shared;
  const redis = item?.endpoints.filter(e=>e.kind === "redis") || [];
  const ready = shared ? state.unlocked : redis.length > 0 && redis.every(e=>endpointUnlocked(e,item));
  const status = $("credentialStatus");
  status.className = `credential-status${ready ? " ready" : ""}`;
  let label = ready ? uiText.authorized : uiText.noPassword;
  if (!shared && !item) label = uiText.chooseService;
  else if (item && authMode(item) === "none") label = uiText.openAccess;
  status.replaceChildren(el("i", "dot"), document.createTextNode(label));
  $("lockButton").disabled = shared ? !state.unlocked : !item || !redis.some(e=>e.auth !== "none" && endpointUnlocked(e,item));
  $("defaultCredentialTarget").textContent = item?.name || uiText.chooseService;
  $("defaultAccessButton").disabled = !item;
  $("defaultAccessButton").textContent = tr("Connection settings");
  $("connectionAccessButton").disabled = !item;
}
async function switchAuth(mode, select=true) {
  if (mode === state.authMode) return;
  state.profileSelection[state.authMode] = state.selected;
  profileTerminals[state.authMode] = state.terminals;
  state.authMode = mode; state.terminals = profileTerminals[mode];
  state.selected = null; state.snapshot = null; state.selectEpoch++; state.detailEpoch++;
  state.picked = {redis:null,sentinel:null}; state.drafts = {redis:"",sentinel:""};
  state.history = []; state.historyPos = 0; $("commandInput").value = "";
  $("serviceWorkspace").hidden = true; $("welcome").hidden = false;
  renderCredential(); renderCatalog(); renderConsole();
  const selected = profileServices().find(s=>s.id === state.profileSelection[mode]) ||
    (mode === "admin" ? profileServices()[0] : null);
  // Restoring a view is not a request for credentials.
  if (selected && select) await selectService(selected.id, undefined, false, false);
}
function serviceConnection(item,kind) {
  return item?.[kind+"_connection"] || item?.endpoints.find(e=>e.kind===kind) || {auth:kind==="redis"?"admin":"none"};
}
function selectedAccessConfig() {
  const item = state.services.find(s=>s.id===state.accessServiceId), kind=$("accessKind").value;
  return $("defaultAccessScope").value === "service" ? serviceConnection(item,kind) : item?.endpoints.find(e=>e.id===$("defaultAccessScope").value) || {};
}
function updateAccessFields() {
  const auth=$("accessAuth").value;
  $("accessUsernameRow").hidden=auth!=="user"; $("accessUsername").required=auth==="user";
  $("accessPasswordRow").hidden=!["user","default"].includes(auth);
  $("accessTlsFields").disabled=!$("accessTls").checked;
  $("defaultAccessPassword").required=false;
  $("accessPasswordState").textContent=auth==="admin"?tr("Uses the shared Redis password entered in this session."):auth==="none"?tr("No password is sent."):tr("Leave blank to keep the current session password, or enter a new one.");
}
function loadAccessScope() {
  const kind=$("accessKind").value, config=selectedAccessConfig();
  const options = kind==="redis" ? [["admin",tr("Global Redis user ({0})",document.body.dataset.adminUser||"admin")]] : [];
  options.push(["user",tr("Username and password")],["default",tr("Password")],["none",tr("No authentication")]);
  $("accessAuth").replaceChildren(...options.map(([v,l])=>new Option(l,v)));
  $("accessAuth").value=config.auth || (kind==="redis"?"admin":"none");
  $("accessUsername").value=config.username || "";
  $("accessTls").checked=!!config.tls;
  for (const [id,key] of [["accessCa","cacert"],["accessSni","sni"],["accessCert","certfile"],["accessKey","keyfile"]]) $(id).value=config[key] || "";
  $("defaultAccessPassword").value="";
  updateAccessFields();
}
function populateAccessScopes(endpointId=null) {
  const item=state.services.find(s=>s.id===state.accessServiceId); if(!item)return;
  const kind=$("accessKind").value, scope=$("defaultAccessScope");
  scope.replaceChildren(new Option(tr("Entire service"),"service"));
  for(const e of item.endpoints.filter(e=>e.kind===kind)) scope.append(new Option(e.address,e.id));
  scope.value=endpointId && [...scope.options].some(o=>o.value===endpointId)?endpointId:"service";
  $("connectionAccessTitle").textContent=kind==="redis"?tr("Redis access"):tr("Sentinel access");
  loadAccessScope();
}
function showAccess(kind="redis",endpointId=null) {
  const item=service();if(!item)return;
  state.accessServiceId=item.id;
  $("defaultAccessName").textContent=`${item.group?item.group+" / ":""}${item.name}`;
  $("accessKind").value=kind;
  $("defaultAccessError").hidden=true;
  populateAccessScopes(endpointId);
  if(!$("defaultAccessDialog").open) $("defaultAccessDialog").showModal();
  if(!$("accessPasswordRow").hidden) $("defaultAccessPassword").focus();
}
function showDefaultAccess(endpointId=null) { showAccess("redis",endpointId); }
function requestEndpointAccess(ep) {
  if (ep?.kind === "redis" && ep.auth === "admin") {
    $("redisPassword").focus(); notify(tr("Enter the password.")); return;
  }
  showAccess(ep?.kind || "redis",ep?.id);
}
function resetViewAfterCredentialChange() {
  state.selectEpoch++; state.detailEpoch++; state.busySnapshots.clear(); state.detailCache.clear(); state.snapshot=null;
  state.history=[]; state.historyPos=0; state.drafts={redis:"",sentinel:""}; $("commandInput").value="";
  // Only affected consoles have been closed on the backend; sync descriptors below.
}
async function syncTerminals() {
  const data = await api("/api/terminals"), live = new Set(data.terminals.map(t=>t.id));
  for (const consoles of Object.values(profileTerminals)) for (const t of Object.values(consoles)) if (t && !live.has(t.id)) t.alive=false;
}
async function saveDefaultCredential(remove=false) {
  const sid=state.accessServiceId, scope=$("defaultAccessScope").value;
  if(!sid)return;
  const payload={kind:$("accessKind").value, scope:scope==="service"?"service":"endpoint",endpoint_id:scope};
  if(!remove) {
    payload.connection={auth:$("accessAuth").value,username:$("accessUsername").value.trim(),tls:$("accessTls").checked,
      cacert:$("accessCa").value.trim(),sni:$("accessSni").value.trim(),certfile:$("accessCert").value.trim(),keyfile:$("accessKey").value.trim()};
    if(["default","user"].includes(payload.connection.auth)) payload.password=$("defaultAccessPassword").value;
  }
  $("saveDefaultAccess").disabled=true;
  try {
    await api(`/api/services/${sid}/${remove?"credentials":"connection"}`,{method:remove?"DELETE":"POST",body:JSON.stringify(payload)});
    $("defaultAccessPassword").value="";$("defaultAccessDialog").close();
    resetViewAfterCredentialChange();await syncTerminals();await loadCatalog();
    const item=state.services.find(s=>s.id===sid);
    if(item && profile(item)!==state.authMode) await switchAuth(profile(item),false);
    await selectService(sid,undefined,false,false);
    notify(remove?tr("Password forgotten for this session."):tr("Connection settings updated."));
  } catch(err) {$("defaultAccessError").hidden=false;$("defaultAccessError").textContent=errorMessage(err);}
  finally {delete payload.password;$("saveDefaultAccess").disabled=false;}
}
// Group expansion is page-local: new visits and reloads start collapsed.
const catalogExpansion = new Map();
let catalogQuery = "", searchExpansion = new Map();
function revealCatalogGroup(group) {
  catalogExpansion.set(group || "", true);
  searchExpansion.set(group || "", true);
}
function catalogItem(item) {
  const button = el("button", `catalog-item${item.id === state.selected ? " selected" : ""}`);
  button.type = "button";
  let cls = "";
  if (item.last_summary) {
    const s = item.last_summary;
    const all = s.redis_total + s.sentinel_total, up = s.redis_up + s.sentinel_up;
    cls = up === all && up ? "up" : up ? "partial" : "down";
  }
  button.append(el("i", `dot ${cls}`));
  const content = el("div");
  content.append(el("strong", "", item.name), el("span", "mode-tag", modes[item.mode] || item.mode));
  const n = item.last_summary?.redis_total ?? item.endpoints.filter(e => e.kind === "redis").length;
  const m = item.last_summary?.sentinel_total ?? item.endpoints.filter(e => e.kind === "sentinel").length;
  const ports = [...new Set(item.endpoints.filter(e => e.kind === "redis").map(e => e.port))].sort((a,b)=>a-b);
  content.append(el("small", "instance-ports", ports.length ? `:${ports.join(" / :")}` : "Sentinel"));
  content.append(el("small", "", `${n} Redis${m ? ` \u00b7 ${m} Sentinel` : ""}`));
  button.append(content);
  button.title = `${item.name}\n${item.endpoints.map(e => e.address).join("\n")}\n${dateTime(item.last_sampled_at)}`;
  button.addEventListener("click", () => selectService(item.id));
  return button;
}
function syncCatalogToggle() {
  const groups = [...$("catalog").querySelectorAll(".catalog-group-toggle")];
  const anyOpen = groups.some(button => button.getAttribute("aria-expanded") === "true");
  $("toggleAllGroups").disabled = groups.length === 0;
  $("toggleAllGroups").textContent = anyOpen ? tr("Свернуть всё") : tr("Развернуть всё");
  $("editButton").disabled = !service();
}
function toggleAllCatalogGroups() {
  const groups = [...$("catalog").querySelectorAll(".catalog-group-toggle")];
  const open = !groups.some(button => button.getAttribute("aria-expanded") === "true");
  const expansion = $("catalogSearch").value.trim() ? searchExpansion : catalogExpansion;
  for (const button of groups) expansion.set(button.dataset.group, open);
  renderCatalog();
}
function renderCatalog() {
  $("catalogCount").textContent = String(profileServices().length);
  const root = $("catalog"), scroll = root.scrollTop;
  const focus = root.contains(document.activeElement) ? document.activeElement.dataset.group : undefined;
  root.replaceChildren();
  const query = $("catalogSearch").value.trim().toLowerCase();
  if (query !== catalogQuery) { catalogQuery = query; searchExpansion = new Map(); }
  const matched = profileServices().filter(s => [s.name, s.group, s.mode, ...s.endpoints.map(e => e.address)].join(" ").toLowerCase().includes(query));
  if (!matched.length) { syncCatalogToggle(); root.append(el("p", "empty-note", profileServices().length ? tr("Совпадений нет") : tr("Каталог пока пуст"))); return; }
  const groups = new Map();
  for (const item of matched) {
    const key = item.group || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  let index = 0;
  for (const [key, items] of groups) {
    const cluster = el("section", "catalog-cluster"), button = el("button", "catalog-group-toggle");
    cluster.dataset.group = key; button.dataset.group = key; button.type = "button";
    button.classList.toggle("contains-selected", items.some(s => s.id === state.selected));
    const expansion = query ? searchExpansion : catalogExpansion;
    let open = expansion.get(key) ?? Boolean(query);
    const arrow = el("span", "catalog-arrow", "\u203a"); arrow.setAttribute("aria-hidden", "true");
    const label = el("span", "catalog-group-name", key || tr("Без группы"));
    const total = profileServices().filter(s => (s.group || "") === key).length;
    const count = el("span", "count", items.length === total ? total : `${items.length}/${total}`);
    count.title = tr("Инстансов в группе");
    button.append(arrow, label, count);
    button.title = key || tr("Без группы");
    const children = el("div", "catalog-children"); children.id = `catalog-children-${index++}`;
    children.setAttribute("role", "group"); children.setAttribute("aria-label", button.title);
    button.setAttribute("aria-controls", children.id);
    function applyOpen() { children.hidden = !open; button.setAttribute("aria-expanded", String(open)); }
    applyOpen();
    button.addEventListener("click", () => {
      open = !open; expansion.set(key, open); applyOpen(); syncCatalogToggle();
    });
    items.forEach(item => children.append(catalogItem(item)));
    cluster.append(button, children); root.append(cluster);
    if (focus !== undefined && key === focus) button.focus({preventScroll:true});
  }
  root.scrollTop = scroll; syncCatalogToggle();
}
async function loadCatalog() {
  const result = await api("/api/services");
  state.services = result.services;
  renderCatalog(); renderCredential();
}
function chooseDefaults(preferred) {
  for (const kind of ["redis", "sentinel"]) {
    const list = nodes().filter(n => n.endpoint.kind === kind);
    if (preferred && list.some(n => rowHasEndpoint(n, preferred))) state.picked[kind] = preferred;
    if (!list.some(n => rowHasEndpoint(n, state.picked[kind]))) {
      const first = list.find(n => n.reachable && n.role === "master") || list[0];
      state.picked[kind] = first?.endpoint_options?.find(e => endpointUnlocked(e))?.id || first?.endpoint.id || null;
    }
  }
  if (!state.picked[state.kind]) chooseConsoleKind(state.picked.redis ? "redis" : "sentinel");
}
async function selectService(id, preferred, reveal=true, requestAccess=true) {
  state.selectEpoch++; state.detailEpoch++;
  state.selected = id; state.snapshot = null; state.topology = null; state.picked = {redis:null, sentinel:null};
  const item = service();
  if (!item) return;
  if (profile(item) !== state.authMode) { await switchAuth(profile(item), false); state.selected=id; }
  if (reveal) revealCatalogGroup(item.group);
  chooseConsoleKind(item.endpoints.some(e => e.kind === "redis") ? "redis" : "sentinel");
  // Wait for the prepared view before selecting defaults; an empty loading
  // view must not change the requested Redis tab to Sentinel.
  $("welcome").hidden = true; $("serviceWorkspace").hidden = false;
  renderWorkspace(); renderCatalog(); loadSnippets();
  replaceChildren("detailContent", el("div", "empty-note", tr("Получение состояния инстанса…")));
  const topologyEpoch = state.selectEpoch;
  try {
    const view = await api(`/api/services/${encodeURIComponent(id)}/topology`);
    if (state.selected !== id || topologyEpoch !== state.selectEpoch) return;
    state.topology = view;
  } catch (err) {
    if (state.selected !== id || topologyEpoch !== state.selectEpoch) return;
    state.topology = {service_id:id, nodes:[], error:errorMessage(err)};
  }
  chooseDefaults(preferred); renderWorkspace(); loadSnippets();
  const pickedRedis = item.endpoints.find(e=>e.id === state.picked.redis);
  if (requestAccess && ["default","user"].includes(authMode(item)) && pickedRedis && !endpointUnlocked(pickedRedis, item)) {
    showDefaultAccess(preferred === pickedRedis.id ? preferred : null);
  }
  await refreshSnapshot();
  await loadDetail();
}
async function refreshSnapshot() {
  const sid = state.selected;
  if (!sid || state.busySnapshots.has(sid) || !state.sessionLive || !state.cliAvailable || state.topology?.service_id !== sid) return;
  const epoch = state.selectEpoch;
  state.busySnapshots.add(sid);
  $("refreshButton").disabled = true;
  $("sampleStatus").textContent = tr("Получение INFO и топологии…");
  try {
    const result = await job(`/api/services/${sid}/snapshot`);
    const item = state.services.find(s => s.id === sid);
    if (item) { item.last_summary = result.summary; item.last_sampled_at = result.sampled_at; item.endpoints = result.endpoints; if(result.credentials)item.credentials=result.credentials; }
    if (state.selected === sid && epoch === state.selectEpoch) {
      state.snapshot = result; chooseDefaults(); renderWorkspace();
      if (state.topic === "events") renderEvents();
    }
    renderCatalog();
  } catch (err) {
    if (state.selected === sid && epoch === state.selectEpoch) {
      $("sampleStatus").textContent = tr("Не удалось обновить · ") + errorMessage(err);
      notify(errorMessage(err), true);
    }
  } finally {
    state.busySnapshots.delete(sid);
    if (state.selected === sid) $("refreshButton").disabled = false;
  }
}
function renderWorkspace() {
  const item = service();
  if (!item) return;
  $("serviceBreadcrumb").textContent = `${item.group || tr("КАТАЛОГ")} / ${modes[item.mode] || item.mode}`;
  $("serviceTitle").textContent = item.name;
  $("serviceSubtitle").textContent = item.master_name ? `Sentinel master-name: ${item.master_name}` : tr("Независимый сервис Redis");
  renderCredential(); renderSummary(); renderTopology(); renderStats(); renderConsole();
  $("sampleStatus").textContent = state.snapshot ? tr`Последний опрос: ${dateTime(state.snapshot.sampled_at)}` : tr("Ещё не опрошено");
}
function renderSummary() {
  const s = state.snapshot?.summary;
  const cards = [
    ["Redis", s ? `${s.redis_up} / ${s.redis_total}` : "—", tr("доступны из панели")],
    ["Sentinel", s ? `${s.sentinel_up} / ${s.sentinel_total}` : "—", s?.sentinel_agree === false ? tr("разные адреса master") : tr("доступны из панели")],
    ["Master", s ? s.masters.map(displayAddress).join(", ") || tr("не подтверждён") : "—", tr("роль из INFO Redis"), true],
    [tr("Клиенты"), s ? formatNumber(s.clients) : "—", tr("соединения на всех Redis")],
    [tr("Память Redis"), s ? bytes(s.memory_all) : "—", tr("все процессы, включая replicas")]
  ];
  $("summary").replaceChildren(...cards.map(([label, value, caption, small]) => {
    const card = el("div", "summary-card");
    card.append(el("div", "summary-label", label), el("div", `summary-value${small ? " small-value" : ""}`, value), el("div", "summary-caption", caption));
    return card;
  }));
}
function displayAddress(raw) {
  const row = nodes().find(n => n.endpoint.address === raw || n.connection?.address === raw || n.aliases?.includes(raw));
  return row?.display?.address || raw;
}
function renderTopology() {
  const root = $("topology"); root.replaceChildren();
  if (!nodes().length) {
    root.append(el("p", "empty-note", state.topology?.error || tr("Подготовка топологии…")));
    $("topologyNote").hidden = true;
    return;
  }
  const groups = new Map();
  for (const row of nodes()) {
    const key = row.display?.host_key || row.connection?.connect_host || row.endpoint.host;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  for (const [key, list] of groups) {
    const row0 = list[0], host = row0.display?.host || row0.endpoint.host;
    const card = el("article", "host-card"); card.dataset.hostKey = key;
    const shownIp = row0.display?.ip && row0.display.ip !== host ? row0.display.ip : "";
    const hostLabel = shownIp ? `${host}  —  ${shownIp}` : host;
    const head = el("div", "host-header"), name = el("span", "host-name", hostLabel);
    head.append(el("span", "host-symbol", "\u25a4"), name); card.append(head);
    if (row0.display?.note) card.append(el("div", "host-dns-note", row0.display.note));
    name.title = shownIp ? `${host} — ${shownIp}` : host;
    list.sort((a,b) => a.endpoint.kind.localeCompare(b.endpoint.kind) || a.endpoint.port-b.endpoint.port);
    for (const row of list) {
      const chosen = state.picked[row.endpoint.kind];
      const e = row.endpoint_options?.find(option => option.id === chosen) ||
        row.endpoint_options?.find(option => endpointUnlocked(option)) || row.endpoint;
      const button = el("button", `endpoint-button${rowHasEndpoint(row, state.picked[state.kind]) ? " active" : ""}`);
      button.append(el("i", `dot ${row.transport_reachable === true && !row.reachable ? "access-warning" : statusClass(row.reachable)}`), el("span", "port", e.port));
      const accessStates = {password_required:tr("Password required"),default_password_required:tr("Password required"),sentinel_password_required:tr("Password required"),authentication_required:tr("Authentication required"),authentication_failed:tr("Authentication failed"),permission_denied:tr("Permission denied"),unsupported_command:tr("Unsupported command")};
      const accessState=accessStates[row.error_code || row.access_state];
      button.append(el("span", "endpoint-status"+(accessState?" access-warning":""), accessState || (row.reachable===null?tr("Not sampled"):row.reachable?tr("Available"):tr("No response"))));
      button.append(el("span", `endpoint-role ${row.role}`, row.role === "unknown" ? e.kind : row.role));
      button.title = `${row.display?.address || e.address}\n${e.address}\n${row.error ? window.RFI18n.error(row.error, row.error_code || row.code) : ""}${row.aliases?.length ? tr("\nДругие адреса: ")+row.aliases.join(", ") : ""}`;
      button.addEventListener("click", () => {
        state.picked[e.kind] = e.id; chooseConsoleKind(e.kind);
        renderTopology(); renderStats(); renderConsole(); loadSnippets(); loadDetail();
        if (e.auth !== "none" && !endpointUnlocked(e)) requestEndpointAccess(e);
      });
      card.append(button);
    }
    const aliases = new Set([key, host, ...list.map(r => r.endpoint.host)]);
    const other = state.services.flatMap(s => s.id === state.selected ? [] : s.endpoints.filter(e => aliases.has(e.host) && e.kind === "redis").map(e => ({s,e})));
    if (other.length) {
      const box = el("div", "host-other", tr("Другие известные инстансы"));
      const ports = el("div", "other-ports"), seen = new Set();
      for (const {s,e} of other.sort((a,b)=>a.e.port-b.e.port)) {
        const pair = `${s.id}:${e.port}`; if (seen.has(pair)) continue; seen.add(pair);
        const button = el("button", "", e.port);
        button.title = `${s.name} \u00b7 ${e.address}`;
        button.addEventListener("click", () => selectService(s.id, e.id)); ports.append(button);
      }
      box.append(ports); card.append(box);
    }
    root.append(card);
  }
  const s = state.snapshot?.summary;
  const notes = [];
  if (s?.replication_down.length) notes.push(tr`\u0421\u0432\u044f\u0437\u044c replica \u2192 master \u043d\u0435 up: ${s.replication_down.map(displayAddress).join(", ")}`);
  if (s?.sentinel_agree === false) notes.push(tr`Sentinel \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u044e\u0442 \u0440\u0430\u0437\u043d\u044b\u0435 master: ${s.sentinel_views.join(", ")}`);
  if (s && s.sentinel_up < s.sentinel_total) notes.push(tr("Недоступность Sentinel из панели не означает, что его не видят другие участники."));
  $("topologyNote").hidden = !notes.length; $("topologyNote").textContent = notes.join(" \u00b7 ");
}
function renderStats() {
  const row = selectedNode();
  $("selectedRole").textContent = row?.role && row.role !== "unknown" ? row.role : "—";
  $("selectedAddress").textContent = row?.connection?.address && row.connection.address !== (row.display?.address || row.endpoint.address)
    ? `${row.display?.address || row.endpoint.address} → ${row.connection.address}` : row?.display?.address || row?.endpoint.address || "";
  if (!row || !row.reachable) {
    const message = row?.error ? window.RFI18n.error(row.error, row.error_code || row.code) : tr("Статистика появится после успешного опроса.");
    replaceChildren("replicationStats", el("div", row?.error ? "error-note" : "empty-note", message));
    replaceChildren("resourceStats", el("div", "empty-note", tr("Нет подтверждённых данных этого инстанса.")));
    return;
  }
  const d = row.info;
  const common = [[tr("Версия Redis"), d.redis_version], ["Uptime", d.uptime_in_seconds ? `${formatNumber(d.uptime_in_seconds)} s` : "—"], [tr("Конфигурация"), d.config_file]];
  if (row.endpoint.kind === "sentinel") {
    const v = row.sentinel || {};
    replaceChildren("replicationStats", kv([[tr("Группа"), v.name || service()?.master_name], [tr("Master по Sentinel"), v.ip ? `${v.ip}:${v.port}` : "—"], [tr("Флаги"), v.flags], ["Quorum", v.quorum], ["Config epoch", v["config-epoch"]], [tr("Известные реплики"), v["num-slaves"]], [tr("Другие Sentinel"), v["num-other-sentinels"]]]), row.sentinel_error ? el("div","error-note",window.RFI18n.error(row.sentinel_error)) : null);
    replaceChildren("resourceStats", kv([...common, [tr("Время опроса CLI"), `${row.sample_ms} ms`], [tr("Память"), bytes(d.used_memory)]]));
    return;
  }
  const left = [[tr("Роль"), row.role], ["Upstream", d.master_host ? `${d.master_host}:${d.master_port}` : "—"], [tr("Связь с master"), d.master_link_status || (row.role === "master" ? "master" : "—")], [tr("Реплик подключено"), d.connected_slaves], ["Master repl offset", d.master_repl_offset], ["Replica repl offset", d.slave_repl_offset], [tr("Синхронизация"), d.master_sync_in_progress], [tr("ACL-пользователей"), row.acl?.state === "available" ? row.acl.users.length : tr("не определено")]];
  if (row.cluster) left.push(["Cluster state", row.cluster.cluster_state], [tr("Назначено слотов"), row.cluster.cluster_slots_assigned], [tr("Слотов fail"), row.cluster.cluster_slots_fail]);
  replaceChildren("replicationStats", kv(left));
  const right = [...common, [tr("Память / RSS"), `${bytes(d.used_memory)} / ${bytes(d.used_memory_rss)}`], [tr("Лимит / eviction"), `${Number(d.maxmemory) ? bytes(d.maxmemory) : tr("без лимита")} / ${shown(d.maxmemory_policy)}`], ["Operations/sec", formatNumber(d.instantaneous_ops_per_sec)], [tr("Клиенты / blocked"), `${shown(d.connected_clients)} / ${shown(d.blocked_clients)}`], ["RDB last status", d.rdb_last_bgsave_status], ["AOF / last write", `${shown(d.aof_enabled)} / ${shown(d.aof_last_write_status)}`], [tr("Время опроса CLI"), `${row.sample_ms} ms`]];
  const progress = el("progress", "memory-progress");
  progress.max = Number(d.maxmemory) || 1; progress.value = Number(d.used_memory) || 0;
  progress.hidden = !Number(d.maxmemory); progress.title = "used_memory / maxmemory";
  replaceChildren("resourceStats", kv(right), progress);
}

function renderConsole() {
  const terminal = state.terminals[state.kind], ep = selectedEndpoint();
  for (const button of $("consoleTabs").querySelectorAll("button")) button.classList.toggle("active", button.dataset.kind === state.kind);
  const select = $("consoleEndpoint");
  select.replaceChildren();
  const list = nodes().filter(n => n.endpoint.kind === state.kind);
  if (!list.length) select.append(new Option(tr("Нет известных инстансов"), ""));
  for (const row of list) {
    const e = rowHasEndpoint(row, ep?.id) ? ep : row.endpoint; select.append(new Option(`${row.display?.address || e.address} · ${row.role}`, e.id));
  }
  select.value = ep?.id || "";
  $("dbControl").hidden = state.kind !== "redis";
  $("databaseNumber").disabled=service()?.mode==="cluster";
  if(service()?.mode==="cluster") $("databaseNumber").value=0;
  $("consoleAccessButton").disabled=!service();
  $("redirectControl").hidden = state.kind !== "redis" || service()?.mode !== "cluster";
  $("connectButton").disabled = !ep || !state.sessionLive || !state.cliAvailable;
  $("connectButton").textContent = terminal ? tr("Переоткрыть") : tr("Открыть");
  $("disconnectButton").disabled = !terminal?.id;
  $("commandInput").disabled = !ep && !terminal;
  $("sendButton").disabled = !terminal?.alive || !!terminal?.sending;
  $("helpKind").textContent = state.kind === "redis" ? "Redis" : "Sentinel";
  $("commandInput").placeholder = tr`Команда ${state.kind === "redis" ? "Redis" : "Sentinel"}… Enter — выполнить, Shift+Enter — новая строка`;
  const mismatch = terminal && ep && (terminal.endpoint.id !== ep.id || terminal.service_id !== state.selected);
  $("consoleMismatch").hidden = !mismatch;
  if (mismatch) $("consoleMismatch").textContent = tr`Открытая консоль остаётся на ${terminal.endpoint.address}. Выбрано ${ep.address}; кнопка «Переоткрыть» создаст новое соединение.`;
  if (terminal) {
    $("consoleContext").textContent = tr`Адрес запуска: ${terminal.endpoint.address}${terminal.connection?.source === "dns" ? ` → ${terminal.connection.address} (DNS ${terminal.connection.dns_servers.join(", ")})` : terminal.connection?.source === "system_fallback" ? tr` → ${terminal.connection.address} (DNS ОС)` : ""} · ${accessLabel(terminal.endpoint)} · DB при открытии: ${terminal.initial_db}${terminal.cluster_redirect ? tr(" · -c: следование редиректам") : ""} · ${terminal.alive ? tr("процесс CLI запущен") : tr("процесс CLI завершён")}`;
    $("terminalLog").textContent = terminal.buffer || "";
  } else {
    $("consoleContext").textContent = tr("Соединение не открыто");
    $("terminalLog").textContent = tr("Откройте консоль для выбранного инстанса.\n");
  }
}
async function loadSnippets() {
  const sid = state.selected, kind = state.kind;
  if (!sid) return;
  try {
    const data = await api(`/api/snippets/${sid}/${kind}`);
    if (state.selected !== sid || state.kind !== kind) return;
    state.snippets = data.snippets; renderSnippets();
  } catch (err) { notify(errorMessage(err), true); }
}
function renderSnippets() {
  const query = $("snippetSearch").value.toLowerCase();
  $("snippetList").replaceChildren();
  for (const snippet of state.snippets) {
    const label = commandNames[snippet.label] || snippet.label;
    if (!(snippet.command+" "+label).toLowerCase().includes(query)) continue;
    const button = el("button", "snippet");
    button.append(el("span", "", label), el("code", "", snippet.command));
    button.addEventListener("click", () => { $("commandInput").value = snippet.command; $("commandInput").focus(); });
    $("snippetList").append(button);
  }
}
function appendOutput(terminal, text) {
  if (!text) return;
  const clean = text.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g,"").replace(/\r/g,"");
  terminal.buffer = (terminal.buffer || "") + clean;
  if (terminal.buffer.length > 650000) terminal.buffer = tr("[Старый вывод удалён из буфера]\n") + terminal.buffer.slice(-600000);
  if (state.terminals[state.kind] === terminal) {
    const node = $("terminalLog");
    const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 90;
    node.textContent = terminal.buffer;
    if (atBottom) node.scrollTop = node.scrollHeight;
  }
}
async function openConsole() {
  if (selectedEndpoint() && !endpointUnlocked(selectedEndpoint())) { requestEndpointAccess(selectedEndpoint()); return; }
  const ep = selectedEndpoint(), kind = state.kind, sid = state.selected;
  if (!ep) return;
  $("connectButton").disabled = true;
  try {
    const old = state.terminals[kind];
    if (old?.id) {
      try { await api(`/api/terminals/${old.id}`, {method:"DELETE"}); }
      catch (err) { if (err.code !== "not_found") throw err; }
      state.terminals[kind] = null;
    }
    const data = await post("/api/terminals", {service_id:sid, endpoint_id:ep.id,
      db:kind === "redis" ? Number($("databaseNumber").value) : 0,
      cluster_redirect:kind === "redis" && service()?.mode === "cluster" && $("clusterRedirect").checked});
    const terminal = {...data.terminal, service_id:sid, buffer:"", polling:false};
    state.terminals[kind] = terminal;
    appendOutput(terminal, tr`# redis-cli открыт для ${ep.address}\n`);
    appendOutput(terminal, terminal.chunks.join(""));
    renderConsole(); renderCredential(); $("commandInput").focus();
  } catch (err) { notify(errorMessage(err), true); if (err.code === "password_required") $("redisPassword").focus(); }
  finally { renderConsole(); }
}
async function sendCommand() {
  const terminal = state.terminals[state.kind], text = $("commandInput").value;
  if (!terminal?.alive || terminal.sending || !text.trim()) return;
  terminal.sending = true;
  $("sendButton").disabled = true;
  appendOutput(terminal, `\n${terminal.endpoint.address}> ${text}\n`);
  try {
    await post(`/api/terminals/${terminal.id}/input`, {text});
    // Only browser memory. Credential-bearing commands are omitted from the recall list.
    if (!/^\s*(AUTH\b|ACL\s+SETUSER\b)/i.test(text)) {
      state.history.push(text); if (state.history.length > 100) state.history.shift();
    }
    state.historyPos = state.history.length;
    if ($("commandInput").value === text) $("commandInput").value = "";
  } catch (err) { notify(errorMessage(err), true); }
  finally { terminal.sending = false; $("sendButton").disabled = !state.terminals[state.kind]?.alive; }
}
async function pollTerminals() {
  try {
    if (state.sessionLive) await Promise.all(Object.values(state.terminals).filter(t => t?.id && t.alive && !t.polling).map(async terminal => {
      terminal.polling = true;
      try {
        const data = await api(`/api/terminals/${terminal.id}?after=${terminal.cursor || 0}`);
        if (data.truncated) appendOutput(terminal, tr("\n[Старые фрагменты вышли за пределы серверного буфера]\n"));
        terminal.cursor = data.cursor;
        appendOutput(terminal, data.chunks.join(""));
        if (!data.alive) {
          terminal.alive = false; appendOutput(terminal, tr`\n[redis-cli завершён, код ${data.exit_code}]\n`);
          renderConsole();
        }
      } catch (err) {
        if (err.code === "not_found" || err.code === "session_expired") {
          terminal.alive = false; terminal.id = null;
          appendOutput(terminal, tr("\n[Консоль завершена или сессия истекла]\n")); renderConsole();
        }
      } finally { terminal.polling = false; }
    }));
  } finally { setTimeout(pollTerminals, document.hidden ? 2200 : 500); }
}

function renderTable(headers, rows, renderCell) {
  const table = el("table", "data-table"), head = el("thead"), hrow = el("tr"), body = el("tbody");
  headers.forEach(h => hrow.append(el("th","",h))); head.append(hrow);
  for (const row of rows) {
    const tr = el("tr");
    row.forEach((cell, index) => {
      const td = el("td");
      const value = renderCell ? renderCell(cell, index) : null;
      if (value instanceof Node) td.append(value); else td.textContent = shown(cell);
      tr.append(td);
    });
    body.append(tr);
  }
  table.append(head, body);
  return table;
}
function renderEvents() {
  $("detailStatus").textContent = tr("События наблюдения в памяти текущей сессии");
  const events = state.snapshot?.events || [];
  if (!events.length) { replaceChildren("detailContent", el("div","empty-note",tr("Изменений пока не зафиксировано. История начинается с первого опроса, не с запуска Redis."))); return; }
  const fields = {role:tr("Роль"), reachable:tr("Доступность из панели"), discovered:tr("Новый участник")};
  replaceChildren("detailContent", renderTable([tr("Время"),tr("Инстанс"),tr("Изменение"),tr("Было"),tr("Стало")], events.map(e => [
    dateTime(e.time), e.endpoint, fields[e.field] || e.field,
    typeof e.before === "boolean" ? e.before ? tr("доступен") : tr("нет ответа") : e.before,
    typeof e.after === "boolean" ? e.after ? tr("доступен") : tr("нет ответа") : e.after
  ])));
}
async function loadDetail(force=false) {
  const epoch = ++state.detailEpoch, ep = selectedEndpoint(), sid = state.selected, topic = state.topic;
  for (const button of $("detailTabs").querySelectorAll("button")) button.classList.toggle("active", button.dataset.topic === topic);
  $("detailRefresh").disabled = false;
  if (topic === "events") { renderEvents(); filterDetails(); return; }
  if (!state.cliAvailable) { replaceChildren("detailContent", el("div","empty-note",tr("Для получения данных установите redis-cli и перезапустите панель."))); return; }
  if (!ep) { replaceChildren("detailContent", el("div","empty-note",tr("Выберите инстанс."))); return; }
  if (ep.auth !== "none" && !endpointUnlocked(ep)) {
    const button=el("button", "secondary", tr("Enter the password.")); button.addEventListener("click",()=>requestEndpointAccess(ep));
    replaceChildren("detailContent", el("div","empty-note",ep.kind === "sentinel" ? tr("Enter the Sentinel password.") : tr("Enter the password.")),button);
    $("detailStatus").textContent=ep.address; return;
  }
  const key = `${sid}:${ep.id}:${topic}`, cached = state.detailCache.get(key);
  if (!force && cached && Date.now() - cached.saved < 60000) { renderDetail(cached.data); return; }
  $("detailStatus").textContent = tr`${ep.address} · загрузка…`;
  $("detailRefresh").disabled = true;
  replaceChildren("detailContent", el("div","empty-note",tr("Получение данных…")));
  try {
    const data = await job("/api/details", {service_id:sid, endpoint_id:ep.id, topic});
    if (epoch !== state.detailEpoch) return;
    state.detailCache.set(key, {saved:Date.now(), data});
    if (state.detailCache.size > 48) state.detailCache.delete(state.detailCache.keys().next().value);
    if (epoch === state.detailEpoch) renderDetail(data);
  } catch (err) {
    if (epoch === state.detailEpoch) {
      replaceChildren("detailContent", el("div","error-note",errorMessage(err)));
      $("detailStatus").textContent = tr`${ep.address} · данные не получены`;
    }
  } finally { if (epoch === state.detailEpoch) $("detailRefresh").disabled = false; }
}
function renderDetail(data) {
  $("detailStatus").textContent = `${data.endpoint.address} · ${dateTime(data.sampled_at)}`;
  const root = $("detailContent"), p = data.payload;
  root.replaceChildren();
  if (data.topic === "acl") {
    const path = p.file_state === "external" ? tr`ACL-файл: ${p.aclfile}` : p.file_state === "not_configured" ? tr("Внешний ACL-файл не настроен. Ниже — ACL в памяти Redis.") : tr`Путь ACL-файла не определён: ${p.file_error}`;
    root.append(el("div","info-line", path));
    root.append(renderTable([tr("Пользователь"),tr("Статус"),tr("Назначение"),tr("Правила (хеши скрыты)")], p.rows.map(r => [r.name, r.enabled ? "on" : "off", r.category === "system" ? tr("системный") : tr("прикладной"), r.rules]), (cell, i) => {
      if (i === 3 || i === 0) return el("code","",cell);
      if (i === 1) return el("span",cell === "on" ? "active-state" : "disabled-state",cell);
      return null;
    }));
  } else if (data.topic === "clients") {
    root.append(el("div","info-line", tr`Соединений в ответе: ${p.total}${p.limited ? tr(" · отображаются первые 1000") : ""}`));
    root.append(renderTable(["ID",tr("Пользователь"),tr("Адрес"),tr("Имя"),"DB","Idle, s",tr("Команда"),tr("Флаги")], p.rows.map(r => [r.id,r.user,r.addr,r.name,r.db,r.idle,r.cmd,r.flags])));
  } else if (data.topic === "config") {
    root.append(renderTable([tr("Параметр"),tr("Значение")], Object.entries(p.values), (v,i) => i === 1 ? el("code","",v) : null));
    if (Object.keys(p.errors).length) root.append(el("pre","error-note",Object.entries(p.errors).map(([k,v]) => `${k}: ${v}`).join("\n")));
  } else if (data.topic === "slowlog") {
    root.append(renderTable(["ID",tr("Время"),tr("Выполнение, µs"),tr("Команда"),tr("Клиент")], (p || []).map(r => [r[0],dateTime(r[1]),r[2],Array.isArray(r[3]) ? r[3].join(" ") : r[3],r[4]]), (v,i) => i === 3 ? el("code","",v) : null));
    if (!p?.length) root.append(el("div","empty-note",tr("Slowlog пуст.")));
  } else if (data.topic === "latency") {
    root.append(renderTable([tr("Событие"),tr("Время"),tr("Последняя, ms"),tr("Максимум, ms")], (p || []).map(r => [r[0],dateTime(r[1]),r[2],r[3]])));
    if (!p?.length) root.append(el("div","empty-note",tr("Событий LATENCY LATEST нет.")));
  } else { root.append(el("pre","raw-detail", typeof p === "string" ? p : JSON.stringify(p,null,2))); }
  filterDetails();
}
function filterDetails() {
  const filter = $("detailFilter").value.toLowerCase();
  for (const row of $("detailContent").querySelectorAll("tbody tr")) row.hidden = !row.textContent.toLowerCase().includes(filter);
}

function resetAddition() {
  if (state.discoveryRunning) return;
  state.lastAddition=null; $("additionSuccess").hidden=true;
  $("discoverButton").disabled=!state.cliAvailable;
  updateScanSummary();
}
function updateSeedAuth() {
  const mode=$("seedAuth").value, own=["user","default"].includes(mode);
  $("defaultSeedFields").hidden=!own;$("seedDefaultPassword").disabled=!own;
  $("seedPasswordPort").disabled=!own;$("seedUsernameRow").hidden=mode!=="user";
  $("seedUsername").required=mode==="user";$("seedDefaultPassword").value="";
  updateSentinelFields();
}
function updateSentinelFields() {
  const mode=$("seedSentinelAuth").value, own=mode!=="none";
  $("seedSentinelUsernameRow").hidden=mode!=="user";$("seedSentinelUsername").required=mode==="user";
  $("sentinelPasswordFields").hidden=!own;
  $("seedSentinelPassword").disabled=!own;
  $("seedSentinelPasswordPort").disabled=!own;
  $("seedSentinelTlsFields").disabled=!$("seedSentinelTls").checked;
  $("sentinelConnectionSummary").textContent=(mode==="none"?tr("No authentication"):mode==="user"?tr("Username and password"):tr("Password"))+($("seedSentinelTls").checked?" / TLS":"");
}
function sentinelEndpointList() {
  const text=$("seedSentinelEndpoints").value.trim();if(!text)return [];
  return text.split(/\r?\n/).map(line=>line.trim()).filter(Boolean).map(line=>{
    const match=line.match(/^\[([^\]]+)\]:(\d+)$/) || line.match(/^([^:\s]+):(\d+)$/);
    if(!match) throw new Error(tr("Use one host:port per line."));
    return {host:match[1],port:Number(match[2])};
  });
}
function completeAddition(saved, addition) {
  if (!saved?.length) return;
  state.lastAddition={services:saved};
  $("additionSuccess").hidden=false; $("additionSuccess").focus(); $("addDialog").scrollTop=0;
  $("discoverButton").textContent=uiText.addedButton; $("discoverButton").disabled=true;
  notify(uiText.savedTitle);
}
function showAdd() {
  // Clear only the new target, not the administrator's connection/scan settings.
  // Do not change a submitted target while an existing job is still running.
  if (!$('addDialog').open && !state.discoveryRunning) {
    $('seedHost').value = '';
    $('seedGroup').value = '';
  }
  resetAddition(); $("seedAuth").value=state.authMode; updateSeedAuth();
  if (!state.discoveryRunning) { $("discoveryResults").hidden=true; $("discoveryProgress").hidden=true; $("saveDiscoveryRow").hidden=true; state.preview=null; }
  if (!$("addDialog").open) $("addDialog").showModal();
  $("seedHost").focus();
}
function updateScanSummary() {
  $("scanRangeSummary").textContent = `Redis ${$("scanRedisStart").value}–${$("scanRedisEnd").value} · Sentinel ${$("scanSentinelStart").value}–${$("scanSentinelEnd").value}`;
  $("discoverButton").textContent = $("autoAddDiscovered").checked ? tr("Сканировать и добавить") : tr("Обнаружить");
}
function setDiscoveryMode(mode, setAutoAdd=true) {
  state.discoveryMode = mode;
  const node = mode === "node";
  $("nodeScanFields").hidden = !node; $("explicitPortFields").hidden = node;
  for (const button of $("discoveryModes").querySelectorAll("button")) button.classList.toggle("active", button.dataset.mode === mode);
  for (const input of $("nodeScanFields").querySelectorAll("input")) input.disabled = !node;
  for (const input of $("explicitPortFields").querySelectorAll("input,select")) input.disabled = node;
  $("seedPorts").required = !node;
  if (setAutoAdd) $("autoAddDiscovered").checked = node;
  updateScanSummary();
}
function renderScanProgress(p) {
  if (!p.phase) return;
  $("discoveryProgress").hidden = false;
  const topology = p.phase === "topology";
  $("scanProgressLabel").textContent = p.phase === "done" ? tr("Поиск завершён") : topology ? tr("Сбор участников кластера") : tr("Сканирование портов ноды");
  const current = topology ? p.topology_completed : p.completed, total = topology ? p.topology_total : p.total;
  $("scanProgressBar").max = Math.max(1,total || 0); $("scanProgressBar").value = current || 0;
  $("scanProgressCount").textContent = `${current || 0} / ${total || 0}`;
  $("scanProgressDetails").textContent = tr`Ответили на исходной ноде: Redis ${p.redis_found || 0}, Sentinel ${p.sentinel_found || 0}. Сервисов: ${p.services_found || 0}${p.current ? " · " + p.current : ""}`;
}
async function discover(event) {
  event.preventDefault();
  if (state.discoveryRunning || state.lastAddition) return;
  state.preview = null; state.discoveryId = null; state.discoveryRunning = true; state.discoveryCancelRequested = false;
  $("saveDiscoveryRow").hidden = true; $("discoveryResults").hidden = false; $("discoveryProgress").hidden = true;
  replaceChildren("discoveryResults", el("div","empty-note",tr("Обнаружение нод и инстансов…")));
  const spec = {host:$("seedHost").value.trim(), discovery_mode:state.discoveryMode, auth:$("seedAuth").value,
    name:$("seedName").value.trim(), group:$("seedGroup").value.trim(), auto_add:$("autoAddDiscovered").checked,
    tls:$("seedTls").checked, cacert:$("seedCa").value.trim(), sni:$("seedSni").value.trim(),
    username:$("seedUsername").value.trim(),certfile:$("seedCert").value.trim(),keyfile:$("seedKey").value.trim(),
    sentinel_connection:{auth:$("seedSentinelAuth").value,username:$("seedSentinelUsername").value.trim(),tls:$("seedSentinelTls").checked,
      cacert:$("seedSentinelCa").value.trim(),sni:$("seedSentinelSni").value.trim(),certfile:$("seedSentinelCert").value.trim(),keyfile:$("seedSentinelKey").value.trim()},
    sentinel_master:$("seedMasterName").value.trim()};
  try {spec.sentinel_endpoints=sentinelEndpointList();} catch(err) {state.discoveryRunning=false;notify(errorMessage(err),true);return;}
  if (state.discoveryMode === "node") Object.assign(spec, {redis_start:Number($("scanRedisStart").value), redis_end:Number($("scanRedisEnd").value),sentinel_start:Number($("scanSentinelStart").value),sentinel_end:Number($("scanSentinelEnd").value)});
  else Object.assign(spec, {ports:$("seedPorts").value.replace(/[–—]/g,"-"),kind:$("seedKind").value,find_sentinel:$("findSentinel").checked,sentinel_port:$("seedSentinelPort").value || null});
  if (["default","user"].includes(spec.auth)) {spec.redis_password=$("seedDefaultPassword").value;spec.password_port=Number($("seedPasswordPort").value);}
  if (spec.sentinel_connection.auth!=="none") {spec.sentinel_password=$("seedSentinelPassword").value;spec.sentinel_password_port=Number($("seedSentinelPasswordPort").value);}
  $("seedSentinelPassword").value="";
  $("seedDefaultPassword").value="";
  for (const input of $("discoveryForm").querySelectorAll("input,select,button")) input.disabled = true;
  $("cancelDiscovery").hidden = false; $("cancelDiscovery").disabled = false;
  try {
    const result = await job("/api/discover", spec, async id => {
      delete spec.default_password;delete spec.redis_password;delete spec.sentinel_password;
      state.discoveryId = id;
      if (state.discoveryCancelRequested) await api(`/api/jobs/${id}`, {method:"DELETE"});
    }, renderScanProgress);
    state.preview = result; renderDiscovery(result);
    if (result.scan) renderScanProgress(result.scan);
    if (result.auto_added) {(result.saved_services || []).forEach(s => revealCatalogGroup(s.group));completeAddition(result.saved_services, result.addition);await loadCatalog();}
  } catch (err) {replaceChildren("discoveryResults", el("div","error-note",errorMessage(err)));}
  finally {
    delete spec.default_password;delete spec.redis_password;delete spec.sentinel_password;
    state.discoveryRunning = false; state.discoveryId = null;
    for (const input of $("discoveryForm").querySelectorAll("input,select,button")) input.disabled = false;
    $("cancelDiscovery").hidden = true; setDiscoveryMode(state.discoveryMode, false);
    $("discoverButton").disabled = !state.cliAvailable || !!state.lastAddition;
    if (state.lastAddition) $("discoverButton").textContent=uiText.addedButton;
    updateSeedAuth();
  }
}
function renderDiscovery(result) {
  const root = $("discoveryResults"); root.replaceChildren();
  result.services.forEach((preview, i) => {
    const box = el("div","discovery-result"), label = el("label");
    const checkbox = el("input"); checkbox.type = "checkbox"; checkbox.checked = true; checkbox.dataset.preview = String(i);
    label.append(checkbox, el("strong","",`${preview.service.name} · ${modes[preview.service.mode]}`)); box.append(label);
    for (const observation of preview.observations) {
      const row = el("div","endpoint-preview");
      row.append(el("i",`dot ${statusClass(observation.reachable)}`),
        document.createTextNode(`${observation.display?.address || observation.endpoint.address} · ${observation.reachable ? observation.role : tr("нет прямого доступа")}`));
      if (observation.error) row.title = observation.error;
      box.append(row);
    }
    if (result.auto_added) {
      checkbox.hidden = true;
      const saved = (result.saved_services || []).find(s => s.mode === preview.service.mode && s.master_name === preview.service.master_name && s.endpoints.some(e => preview.service.endpoints.some(v => v.id === e.id)));
      if (saved) {const open = el("button","secondary small",tr("Открыть сервис"));open.addEventListener("click",()=>{$("addDialog").close();selectService(saved.id);});box.append(open);}
    }
    root.append(box);
  });
  let errorRoot = root;
  if (result.errors.length && result.scan?.mode === "node") {errorRoot=el("details","discovery-notes scan-errors");errorRoot.append(el("summary","",tr`Нет ответа или данные не получены: ${result.errors.length} (показать)`));root.append(errorRoot);}
  for (const error of result.errors) errorRoot.append(el("div","error-note",`${error.address}: ${errorMessage({message:error.error,code:error.code})}`));
  if (result.notes.length) {
    const details = el("details","discovery-notes");
    details.append(el("summary","",tr`Сообщения обнаружения (${result.notes.length})`), el("pre","raw-detail",result.notes.join("\n")));
    root.append(details);
  }
  if (result.limited) root.append(el("div","error-note",tr("Достигнут лимит обнаружения. Добавьте оставшиеся участники отдельной записью.")));
  if (!result.services.length && !result.errors.length) root.append(el("div","empty-note",tr("Связанных сервисов не найдено.")));
  $("saveDiscoveryRow").hidden = !!result.auto_added || !result.services.length;
  $("discoveredCount").textContent = tr`Найдено сервисов: ${result.services.length}`;
  if (result.auto_added && result.services.length) root.prepend(el("div","info-line",tr`В каталоге: ${(result.saved_services || []).length} сервисов. Повторно найденные записи обновлены.`));
}
async function cancelDiscovery() {
  state.discoveryCancelRequested = true;
  if (!state.discoveryId) return;
  try { await api(`/api/jobs/${state.discoveryId}`, {method:"DELETE"}); }
  catch (err) { notify(errorMessage(err), true); }
}
async function saveDiscovery() {
  const indices = Array.from($("discoveryResults").querySelectorAll("input[data-preview]:checked")).map(e => Number(e.dataset.preview));
  if (!indices.length) { notify(tr("Выберите хотя бы один сервис."), true); return; }
  $("saveDiscovery").disabled = true;
  try {
    const selected = indices.map(i => ({...state.preview.services[i].service, group:$("seedGroup").value.trim()}));
    if (selected.length === 1 && $("seedName").value.trim()) selected[0].name = $("seedName").value.trim();
    const data = await post("/api/services", {services:selected, credential_ref:state.preview.credential_ref});
    completeAddition(data.services, data.addition); await loadCatalog(); $("saveDiscoveryRow").hidden=true;
    notify(tr`Добавлено или обновлено сервисов: ${data.services.length}`);
    if (data.services[0]) revealCatalogGroup(data.services[0].group);
  } catch (err) { notify(errorMessage(err), true); }
  finally { $("saveDiscovery").disabled = false; }
}

function bindEvents() {
  $("toggleAllGroups").addEventListener("click", toggleAllCatalogGroups);
  $("panelLogoutButton")?.addEventListener("click", async () => {
    $("panelLogoutButton").disabled = true;
    try { await post("/api/panel/logout"); window.location.replace("/login"); }
    catch (err) { notify(errorMessage(err), true); $("panelLogoutButton").disabled = false; }
  });
  $("authTabs").addEventListener("click",e=>{const b=e.target.closest("button[data-auth]");if(b)switchAuth(b.dataset.auth);});
  $("defaultAccessButton").addEventListener("click",()=>showAccess("redis"));
  $("connectionAccessButton").addEventListener("click",()=>showAccess(state.kind));
  $("consoleAccessButton").addEventListener("click",()=>showAccess(state.kind,state.picked[state.kind]));
  $("accessKind").addEventListener("change",()=>populateAccessScopes());
  $("defaultAccessScope").addEventListener("change",loadAccessScope);
  $("accessAuth").addEventListener("change",()=>{$("defaultAccessPassword").value="";updateAccessFields();});
  $("accessTls").addEventListener("change",updateAccessFields);
  $("seedSentinelAuth").addEventListener("change",()=>{$("seedSentinelPassword").value="";updateSentinelFields();});
  $("seedSentinelTls").addEventListener("change",updateSentinelFields);
  $("scanSentinelStart").addEventListener("input",()=>{$("seedSentinelPasswordPort").value=$("scanSentinelStart").value;});
  $("seedKind").addEventListener("change",()=>{if($("seedKind").value==="sentinel")$("seedSentinelPasswordPort").value=parseInt($("seedPorts").value,10)||26379;});
  $("closeDefaultAccess").addEventListener("click",()=>$("defaultAccessDialog").close());
  $("defaultAccessDialog").addEventListener("close",()=>{$("defaultAccessPassword").value="";});
  $("defaultAccessForm").addEventListener("submit",e=>{e.preventDefault();saveDefaultCredential();});
  $("forgetDefaultScope").addEventListener("click",()=>saveDefaultCredential(true));
  $("credentialForm").addEventListener("submit", async event => {
    event.preventDefault(); $("unlockButton").disabled=true;
    try {
      await post("/api/session/unlock",{password:$("redisPassword").value}); $("redisPassword").value="";
      state.unlocked=true; resetViewAfterCredentialChange(); await syncTerminals(); await loadCatalog(); renderCredential(); renderConsole();
      if(state.selected) {await refreshSnapshot();await loadDetail(true);}
    } catch(err) {notify(errorMessage(err),true);}
    finally {$("unlockButton").disabled=false;}
  });
  $("lockButton").addEventListener("click", async () => {
    try {
      if(usesGlobal()) {await post("/api/session/lock",{profile:"admin"});state.unlocked=false;}
      else if(service()) await api(`/api/services/${state.selected}/credentials`,{method:"DELETE",body:JSON.stringify({scope:"service"})});
      resetViewAfterCredentialChange(); await syncTerminals(); await loadCatalog();
      $("redisPassword").value="";renderCredential();renderConsole();if(state.selected)renderWorkspace();
      replaceChildren("detailContent",el("div","empty-note",uiText.scopeForgotten));
      notify(state.authMode === "admin" ? uiText.adminForgotten : uiText.defaultForgotten);
    } catch(err){notify(errorMessage(err),true);}
  });
  $("additionOpen").addEventListener("click",async()=>{
    const first=state.lastAddition?.services[0];$("addDialog").close();
    if(first) {await switchAuth(profile(first),false);await selectService(first.id, undefined, true, false);}
  });
  $("seedAuth").addEventListener("change",updateSeedAuth);
  $("seedPorts").addEventListener("input",()=>{
    const first=parseInt($("seedPorts").value,10);if(first>=1 && first<=65535)$("seedPasswordPort").value=first;
  });
  $("scanRedisStart").addEventListener("input",()=>{$("seedPasswordPort").value=$("scanRedisStart").value;});
  $("discoveryForm").addEventListener("input",resetAddition);
  $("discoveryForm").addEventListener("change",resetAddition);
  $("addDialog").addEventListener("close",()=>{$("seedDefaultPassword").value="";$("seedSentinelPassword").value="";});
  $("catalogSearch").addEventListener("input",renderCatalog);
  $("addButton").addEventListener("click",showAdd); $("welcomeAdd").addEventListener("click",showAdd);
  $("closeAdd").addEventListener("click",async () => { $("seedDefaultPassword").value=""; await cancelDiscovery(); $("addDialog").close(); });
  $("addDialog").addEventListener("cancel",cancelDiscovery);
  $("discoveryModes").addEventListener("click",event=>{const button=event.target.closest("button[data-mode]");if(button && !state.discoveryRunning) {resetAddition();setDiscoveryMode(button.dataset.mode);}});
  for (const id of ["scanRedisStart","scanRedisEnd","scanSentinelStart","scanSentinelEnd","autoAddDiscovered"]) $(id).addEventListener("input",updateScanSummary);
  $("discoveryForm").addEventListener("submit",discover);
  $("cancelDiscovery").addEventListener("click",cancelDiscovery);
  $("saveDiscovery").addEventListener("click",saveDiscovery);
  $("refreshButton").addEventListener("click",refreshSnapshot);
  $("consoleTabs").addEventListener("click",event => {
    const button = event.target.closest("button[data-kind]"); if (!button) return;
    chooseConsoleKind(button.dataset.kind); renderConsole(); renderStats(); renderTopology(); loadSnippets(); loadDetail();
  });
  $("consoleEndpoint").addEventListener("change",() => {
    state.picked[state.kind] = $("consoleEndpoint").value; renderConsole(); renderStats(); renderTopology(); loadDetail();
  });
  $("connectButton").addEventListener("click",openConsole);
  $("disconnectButton").addEventListener("click",async () => {
    const terminal = state.terminals[state.kind]; if (!terminal?.id) return;
    try { await api(`/api/terminals/${terminal.id}`,{method:"DELETE"}); terminal.alive=false; terminal.id=null; appendOutput(terminal,tr("\n[Процесс CLI закрыт пользователем]\n")); renderConsole(); }
    catch (err) { notify(errorMessage(err), true); }
  });
  $("commandForm").addEventListener("submit",event => { event.preventDefault(); sendCommand(); });
  $("commandInput").addEventListener("keydown",event => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendCommand(); }
    else if (event.key === "ArrowUp" && !event.shiftKey && $("commandInput").selectionStart === 0 && state.history.length) {
      event.preventDefault(); state.historyPos=Math.max(0,state.historyPos-1); $("commandInput").value=state.history[state.historyPos];
    } else if (event.key === "ArrowDown" && !event.shiftKey && state.historyPos < state.history.length) {
      event.preventDefault(); state.historyPos++; $("commandInput").value=state.history[state.historyPos] || "";
    }
  });
  $("clearConsole").addEventListener("click",async () => {
    const terminal = state.terminals[state.kind]; if (!terminal) return;
    try {
      if (terminal.id) terminal.cursor = (await post(`/api/terminals/${terminal.id}/clear`)).cursor;
      terminal.buffer=""; $("terminalLog").textContent="";
    } catch (err) { notify(errorMessage(err),true); }
  });
  $("expandConsole").addEventListener("click",() => $("consolePanel").classList.toggle("maximized"));
  document.addEventListener("keydown",event => { if (event.key === "Escape") $("consolePanel").classList.remove("maximized"); });
  $("snippetSearch").addEventListener("input",renderSnippets);
  $("detailTabs").addEventListener("click",event => {
    const button=event.target.closest("button[data-topic]"); if (!button) return;
    state.topic=button.dataset.topic; $("detailFilter").value=""; loadDetail();
  });
  $("detailRefresh").addEventListener("click",() => loadDetail(true));
  $("detailFilter").addEventListener("input",filterDetails);
  $("editButton").addEventListener("click",() => {
    const item=service(); if(!item)return;
    $("editName").value=item.name; $("editGroup").value=item.group; $("editDialog").showModal();
  });
  $("closeEdit").addEventListener("click",() => $("editDialog").close());
  $("editForm").addEventListener("submit",async event => {
    event.preventDefault();
    try {
      await api(`/api/services/${state.selected}`,{method:"PATCH",body:JSON.stringify({name:$("editName").value,group:$("editGroup").value})});
      await loadCatalog(); renderWorkspace(); $("editDialog").close();
    } catch(err){notify(errorMessage(err),true);}
  });
  $("deleteService").addEventListener("click",async () => {
    if(!confirm(tr("Удалить запись только из локального каталога? Redis и Sentinel не изменяются.")))return;
    try {
      await api(`/api/services/${state.selected}`,{method:"DELETE"});
      state.selected=null;state.snapshot=null;state.selectEpoch++;state.detailEpoch++;
      $("editDialog").close();$("serviceWorkspace").hidden=true;$("welcome").hidden=false;
      await loadCatalog();
    }catch(err){notify(errorMessage(err),true);}
  });
  $("exportButton").addEventListener("click",async () => {
    try {
      const data=await api("/api/catalog/export");
      const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json"}));
      const link=el("a");link.href=url;link.download="redis-farm-inventory.json";link.click();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(err){notify(errorMessage(err),true);}
  });
  $("importButton").addEventListener("click",()=> $("importFile").click());
  $("importFile").addEventListener("change",async () => {
    const file=$("importFile").files[0];if(!file)return;
    try {
      if(file.size>2*1024*1024)throw new Error(tr("Файл каталога превышает 2 MiB."));
      const data=JSON.parse(await file.text());await post("/api/catalog/import",data);await loadCatalog();
      notify(tr("Каталог импортирован.")); if(!state.selected && profileServices()[0] && state.authMode === "admin")selectService(profileServices()[0].id, undefined, false, false);
    }catch(err){notify(errorMessage(err),true);}
    finally{$("importFile").value="";}
  });
  $("aboutButton").addEventListener("click",()=> $("aboutDialog").showModal());
  $("closeAbout").addEventListener("click",()=> $("aboutDialog").close());
  $("aboutDialog").addEventListener("click",event => {
    if (event.target !== $("aboutDialog")) return;
    const rect=$("aboutDialog").getBoundingClientRect();
    const outside=event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
    if (outside) $("aboutDialog").close();
  });
  let lastTouch=0;
  const touch=() => {
    if(!state.csrf || !state.sessionLive || Date.now()-lastTouch<30000)return;
    lastTouch=Date.now();post("/api/session/touch").catch(()=>{});
  };
  document.addEventListener("pointerdown",touch,{passive:true});document.addEventListener("keydown",touch);
}
async function init() {
  bindEvents();
  try {
    const data=await api("/api/bootstrap");
    state.panelLogin=!!data.panel_login;state.csrf=data.csrf;state.unlocked=data.redis_unlocked;state.refreshSeconds=data.refresh_seconds;state.cliAvailable=data.cli.available;
    const defaults=data.discovery_defaults || {};
    for (const [id,key] of [["scanRedisStart","redis_start"],["scanRedisEnd","redis_end"],["scanSentinelStart","sentinel_start"],["scanSentinelEnd","sentinel_end"]]) if(defaults[key]) $(id).value=defaults[key];
    setDiscoveryMode("node");
    $("discoverButton").disabled=!data.cli.available;
    $("cliBadge").textContent=data.cli.available?data.cli.version:tr("redis-cli не найден / не поддерживается");
    $("cliBadge").classList.toggle("error",!data.cli.available);
    $("cliBadge").title=data.cli.error||data.cli.path;
    const dns=data.cli.dns;
    state.customDns=dns?.mode === "custom";
    const dnsLabel=dns?.mode === "custom" ? dns.servers.join(", ") + (dns.system_fallback ? tr(" → DNS ОС при ошибке") : "") : tr("системный");
    replaceChildren("aboutContent",kv([[tr("Версия"),data.version],["DNS",dnsLabel],["redis-cli",data.cli.version||tr("недоступен")],[tr("Бинарник"),data.cli.path],[tr("Данные"),data.data_dir],[tr("Обновление"),`${data.refresh_seconds} s`]]),data.cli.error?el("div","error-note",window.RFI18n.error(data.cli.error)):null);
    renderCredential();await loadCatalog();
    if(!data.cli.available)notify(tr("Установите redis-tools, затем перезапустите панель. Каталог остаётся доступным."),true);
    // Restore process handles after a page reload, without restarting Redis connections.
    try {
      const existing=await api("/api/terminals");
      for(const descriptor of existing.terminals) {
        const kind=descriptor.endpoint.kind;
        const sid=descriptor.service_id || state.services.find(s=>s.endpoints.some(e=>e.id===descriptor.endpoint.id))?.id;
        const tab=(descriptor.profile || descriptor.endpoint.auth) === "admin" ? "admin" : "default";
        profileTerminals[tab][kind]={...descriptor,service_id:sid,cursor:0,buffer:"",polling:false};
      }
    } catch (_) { /* An empty first session has nothing to restore. */ }
    if(!profileServices().length && state.services.length)await switchAuth("default",false);
    if(profileServices()[0] && state.authMode === "admin")await selectService(profileServices()[0].id, undefined, false, false);
    else if(state.authMode === "admin") $("redisPassword").focus();
  }catch(err){notify(errorMessage(err),true);}
  pollTerminals();
  setInterval(() => {
    if(state.selected && state.sessionLive && $("autoRefresh").checked && !document.hidden && !$("addDialog").open) refreshSnapshot();
  },state.refreshSeconds*1000);
}
document.addEventListener("DOMContentLoaded",init);
window.addEventListener("pageshow", event => {
  // A restored page must revalidate its web session before displaying old data.
  if (event.persisted) window.location.reload();
});
