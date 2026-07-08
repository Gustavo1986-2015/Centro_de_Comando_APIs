        function formatLatency(totalSeconds, decimals = 1) {
            if (totalSeconds === null || totalSeconds === undefined) return 'N/A';
            const val = parseFloat(totalSeconds);
            if (val < 60) return `${val.toFixed(decimals)}s`;
            const h = Math.floor(val / 3600);
            const m = Math.floor((val % 3600) / 60);
            const s = Math.round(val % 60);
            if (h > 0) return `${h}h ${m}m ${s}s`;
            return `${m}m ${s}s`;
        }

        const _metricState = {};
        const SPARK_HISTORY = {
            'spark-pending':  new Array(60).fill(0),
            'spark-sent':     new Array(60).fill(0),
            'spark-failed':   new Array(60).fill(0),
            'spark-retries':  new Array(60).fill(0),
            'spark-push':     new Array(60).fill(0),
        };

        function updateSparkline(sparkId, metricId) {
            const history = SPARK_HISTORY[sparkId];
            history.push(_metricState[metricId] || 0);
            history.shift();

            const svg = document.getElementById(sparkId);
            if (!svg) return;

            const max = Math.max(...history, 1);
            const W = 120, H = 28;
            const pts = history.map((v, i) => {
                const x = (i / (history.length - 1)) * W;
                const y = H - (v / max) * H;
                return `${x.toFixed(1)},${y.toFixed(1)}`;
            }).join(' ');

            let poly = svg.querySelector('polyline');
            if (!poly) {
                poly = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
                svg.appendChild(poly);
            }
            poly.setAttribute('points', pts);
        }

        const _providerEventCounts = {};
        const _seenEventIds = new Set();
        const SEEN_IDS_MAX = 2000;
        const RATE_WINDOW_MS = 60_000;

        function updateProviderBar(recentEvents) {
            const now = Date.now();
            recentEvents.forEach(ev => {
                const uniqueId = `${ev.provider}-${ev.env}-${ev.id}-${ev.time}`;
                if (!ev.id || _seenEventIds.has(uniqueId)) return;  // ya contado, ignorar

                // Silenciar eventos históricos que tienen más de 60 segundos
                if (ev.time) {
                    const eventTimestamp = new Date(ev.time.replace(' (UTC)', 'Z')).getTime();
                    if (!isNaN(eventTimestamp) && now - eventTimestamp > RATE_WINDOW_MS) return;
                }

                // Agregar al Set con límite de tamaño (evita memory leak en sesiones largas)
                _seenEventIds.add(uniqueId);
                if (_seenEventIds.size > SEEN_IDS_MAX) {
                    // Eliminar el primer elemento insertado (el más antiguo)
                    _seenEventIds.delete(_seenEventIds.values().next().value);
                }

                const key = ev.provider.toLowerCase();
                if (!_providerEventCounts[key]) _providerEventCounts[key] = [];
                _providerEventCounts[key].push(now);
            });

            Object.keys(_providerEventCounts).forEach(key => {
                _providerEventCounts[key] = _providerEventCounts[key]
                    .filter(t => now - t < RATE_WINDOW_MS);
            });

            const bar = document.getElementById('provider-status-bar');
            if (!bar) return;

            const allProviders = Object.keys(_providerEventCounts);
            if (allProviders.length === 0) {
                bar.innerHTML = '<span style="color:var(--color-gray-label);font-size:0.78rem;">Sin proveedores activos</span>';
                return;
            }

            const failedProviders = new Set(
                recentEvents.filter(e => e.status === 'failed').map(e => e.provider.toLowerCase())
            );

            bar.innerHTML = allProviders.map(key => {
                const rate      = _providerEventCounts[key].length;
                const hasFailed = failedProviders.has(key);
                const isActive  = rate > 0;
                const cls       = hasFailed ? 'has-failed' : (isActive ? 'active' : 'inactive');
                const label     = isActive ? `${rate} ev/min` : 'inactivo';
                return `<div class="provider-pill ${cls}" title="${key.toUpperCase()} — ${label}">
                    <span class="pill-dot"></span>
                    ${key.toUpperCase()}
                    <span class="pill-rate">${label}</span>
                </div>`;
            }).join('');
        }

        function injectSkeletonRows(tbodyId, cols, rowCount = 5) {
            const tbody = document.getElementById(tbodyId);
            if (!tbody || tbody.children.length > 0) return;
            tbody.innerHTML = Array(rowCount).fill(null).map(() =>
                `<tr class="skeleton-row">${Array(cols).fill('<td>&nbsp;</td>').join('')}</tr>`
            ).join('');
        }

        let _reconnectTimer    = null;
        let _reconnectDelay    = 5;
        let _countdownInterval = null;

        function showDisconnectBanner(message = 'Conexión perdida con el servidor') {
            const banner = document.getElementById('disconnect-banner');
            const msg    = document.getElementById('disconnect-message');
            const cdEl   = document.getElementById('disconnect-countdown');
            if (!banner) return;
            msg.textContent    = message;
            banner.style.display = 'flex';

            let remaining = _reconnectDelay;
            clearInterval(_countdownInterval);
            cdEl.textContent = `— reintentando en ${remaining}s`;
            _countdownInterval = setInterval(() => {
                remaining--;
                cdEl.textContent = remaining > 0
                    ? `— reintentando en ${remaining}s` : '— conectando...';
                if (remaining <= 0) clearInterval(_countdownInterval);
            }, 1000);

            clearTimeout(_reconnectTimer);
            _reconnectTimer = setTimeout(manualReconnect, _reconnectDelay * 1000);
            _reconnectDelay = Math.min(_reconnectDelay * 2, 60);
        }

        function hideDisconnectBanner() {
            const banner = document.getElementById('disconnect-banner');
            if (banner) banner.style.display = 'none';
            clearTimeout(_reconnectTimer);
            clearInterval(_countdownInterval);
            _reconnectDelay = 5;
        }

        function manualReconnect() {
            clearTimeout(_reconnectTimer);
            clearInterval(_countdownInterval);
            if (window.evtSource) {
                window.evtSource.close();
            }
            
            const timestamp = Date.now();
            if (currentProviderFilter === 'all' && currentStatusFilter === 'all') {
                window.evtSource = new EventSource('/api/stats/stream?t=' + timestamp);
            } else {
                window.evtSource = new EventSource(`/api/stats/stream?provider=${currentProviderFilter}&status=${currentStatusFilter}&t=${timestamp}`);
            }

            window.evtSource.onmessage = e => renderStats(JSON.parse(e.data));
            window.evtSource.onerror = () => {
                document.getElementById('sync-status').textContent = 'Pérdida de Conexión';
                document.querySelector('.pulse').style.backgroundColor = '#EF4444';
                showDisconnectBanner('Conexión SSE interrumpida — reconectando...');
            };
        }

        let currentConfigs = [];
        
        // Variables globales para filtros
        let currentStatusFilter = 'all';
        let currentProviderFilter = 'all';
        let currentLatencyFilter = 'all';
        let allRecentEvents = [];
        let expandedRows = new Set();
        
        // Carga eventos filtrados desde el backend cuando hay filtros activos
        async function fetchFilteredEvents() {
            const hasFilter = currentStatusFilter !== 'all' || currentProviderFilter !== 'all';
            if (!hasFilter) {
                // Sin filtros: la grilla se alimenta del SSE normalmente
                renderRecentTable();
                return;
            }
            try {
                const params = new URLSearchParams();
                if (currentStatusFilter !== 'all') params.set('status', currentStatusFilter);
                if (currentProviderFilter !== 'all') params.set('provider', currentProviderFilter);
                const res = await fetch(`/api/stats?${params.toString()}`, { cache: 'no-store' });
                const data = await res.json();
                allRecentEvents = data.recent || [];
                renderRecentTable();
            } catch(e) {
                console.error('Error al cargar eventos filtrados:', e);
            }
        }

        function setFilterStatus(status) {
            currentStatusFilter = status;
            fetchFilteredEvents();
        }
        
        function setFilterProvider(provider) {
            currentProviderFilter = provider.toLowerCase();
            fetchFilteredEvents();
        }

        function setFilterLatency(latency) {
            currentLatencyFilter = latency;
            renderRecentTable();
        }
        
        function resetFilters() {
            currentStatusFilter = 'all';
            currentProviderFilter = 'all';
            currentLatencyFilter = 'all';
            const statusDropdown = document.getElementById('filter-status');
            if(statusDropdown) statusDropdown.value = 'all';
            document.getElementById('filter-provider').value = 'all';
            document.getElementById('filter-latency').value = 'all';
            renderRecentTable();
        }

        function downloadExcel() {
            let filtered = allRecentEvents;
            
            if (currentStatusFilter !== 'all') {
                filtered = filtered.filter(ev => ev.status === currentStatusFilter);
            }
            if (currentProviderFilter !== 'all') {
                filtered = filtered.filter(ev => ev.provider.toLowerCase() === currentProviderFilter);
            }
            if (currentLatencyFilter !== 'all') {
                filtered = filtered.filter(ev => {
                    const lat = ev.rc_latency_sec !== null && ev.rc_latency_sec !== undefined ? ev.rc_latency_sec : ev.latency_sec;
                    if (lat === null || lat === undefined) return false;
                    if (currentLatencyFilter === 'low') return lat <= 2;
                    if (currentLatencyFilter === 'medium') return lat > 2 && lat <= 9;
                    if (currentLatencyFilter === 'high') return lat >= 10;
                    return true;
                });
            }
            
            if (filtered.length === 0) {
                alert("No hay datos en pantalla para exportar.");
                return;
            }
            
            const headers = [
                "Proveedor", "Entorno", "Activo / Patente", "Estado", "Última Actualización",
                "Fecha GPS", "Latencia Transmisión", "Coordenadas", "Dirección", "Altitud",
                "Velocidad", "Ignición", "Batería", "Temperatura", "Odómetro", "Código EV",
                "Job ID", "Recepcionado Assistcargo", "Enviado a RC", "Latencia Hub AC",
                "Recepcionado RC", "Latencia RC", "Respuesta RC (Error)"
            ];
            
            let csvRows = [];
            csvRows.push(headers.join(";"));
            
            filtered.forEach(ev => {
                let statusText = 'En Cola';
                if (ev.status === 'sent') statusText = 'Enviado';
                else if (ev.status === 'failed') statusText = 'Error';
                else if (ev.status === 'pending' && ev.retry_count > 0) statusText = `Reintento ${ev.retry_count}/4`;
                
                let transSec = ev.transmission_latency_sec;
                let displayTrans = 'N/A';
                if (transSec !== null && transSec !== undefined) {
                    displayTrans = formatLatency(transSec, 1);
                }
                
                let lat = ev.latency_sec;
                let displayHub = 'N/A';
                if (lat !== null && lat !== undefined) {
                    displayHub = formatLatency(lat, 2);
                }

                let rcLat = ev.rc_latency_sec;
                let displayRc = 'N/A';
                if (rcLat !== null && rcLat !== undefined) {
                    displayRc = `${rcLat.toFixed(3)}s`;
                }
                
                let timeSentText = ev.time_sent || 'N/A';
                let timeReceivedRcText = ev.time_received_rc || 'N/A';
                
                const row = [
                    ev.provider.toUpperCase(),
                    ev.env.toUpperCase(),
                    ev.chassis,
                    statusText.toUpperCase(),
                    ev.time || 'N/A',
                    ev.device_date || 'N/A',
                    displayTrans,
                    ev.coords || 'Sin GPS',
                    ev.course !== null ? ev.course + '°' : 'N/A',
                    ev.altitude !== null ? ev.altitude + 'm' : 'N/A',
                    ev.speed + ' km/h',
                    ev.ignition,
                    ev.battery !== null ? ev.battery + '%' : 'N/A',
                    ev.temperature !== null ? ev.temperature + '°' : 'N/A',
                    ev.odometer !== null ? ev.odometer : 'N/A',
                    ev.code || 'N/A',
                    ev.job_id || 'N/A',
                    ev.time_received || 'N/A',
                    timeSentText,
                    displayHub,
                    timeReceivedRcText,
                    displayRc,
                    ev.rc_response ? ev.rc_response.replace(/[\n\r;]/g, " ") : ""
                ];
                
                const sanitizedRow = row.map(val => {
                    let s = String(val);
                    if (s.includes(";") || s.includes('"') || s.includes("\n")) {
                        s = '"' + s.replace(/"/g, '""') + '"';
                    }
                    return s;
                });
                
                csvRows.push(sanitizedRow.join(";"));
            });
            
            const csvString = "\uFEFF" + csvRows.join("\n");
            const blob = new Blob([csvString], { type: "text/csv;charset=utf-8;" });
            
            const link = document.createElement("a");
            const url = URL.createObjectURL(blob);
            link.setAttribute("href", url);
            
            const cleanDate = new Date().toISOString().slice(0, 10);
            link.setAttribute("download", `reporte_actividad_comando_${cleanDate}.csv`);
            link.style.visibility = 'hidden';
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }

        // Vistas
        function switchView(view) {
            document.getElementById('view-dashboard').style.display = view === 'dashboard' ? 'flex' : 'none';
            document.getElementById('view-config').style.display = view === 'config' ? 'flex' : 'none';
            document.getElementById('view-simulator').style.display = view === 'simulator' ? 'flex' : 'none';
            document.getElementById('view-history').style.display = view === 'history' ? 'flex' : 'none';
            document.getElementById('view-db-viewer').style.display = view === 'db-viewer' ? 'flex' : 'none';
            document.getElementById('view-monitor').style.display = view === 'monitor' ? 'flex' : 'none';
            if(document.getElementById('view-integrations')) {
                document.getElementById('view-integrations').style.display = view === 'integrations' ? 'flex' : 'none';
            }
            
            document.getElementById('tab-dashboard').classList.toggle('active-tab', view === 'dashboard');
            document.getElementById('tab-config').classList.toggle('active-tab', view === 'config');
            document.getElementById('tab-simulator').classList.toggle('active-tab', view === 'simulator');
            document.getElementById('tab-history').classList.toggle('active-tab', view === 'history');
            document.getElementById('tab-db-viewer').classList.toggle('active-tab', view === 'db-viewer');
            document.getElementById('tab-monitor').classList.toggle('active-tab', view === 'monitor');
            if(document.getElementById('tab-integrations')) {
                document.getElementById('tab-integrations').classList.toggle('active-tab', view === 'integrations');
            }
            
            toggleMenu(); // Cierra el menú al elegir
 
            if(window.evtSource) { window.evtSource.close(); window.evtSource = null; }
            
            if (view === 'config') {
                loadConfig();
                loadRetentionConfig();
            } else if (view === 'simulator') {
                loadSimulator();
            } else if (view === 'history') {
                loadHistory();
            } else if (view === 'db-viewer') {
                loadDatabases();
            } else if (view === 'monitor') {
                loadMonitor();
                // Inicializar el buscador de vehículos con la fecha de hoy
                const dateInput = document.getElementById('veh-date-filter');
                if (dateInput && !dateInput.value) {
                    dateInput.value = new Date().toISOString().slice(0, 10);
                }
                initVehicleProviderDropdown();
                loadVehicles();
            } else if (view === 'integrations') {
                loadIntegrationStudio();
            } else {
                
                initSSE();
            }
        }

        // Monitor Interno
        async function loadMonitor() {
            try {
                const res = await fetch('/api/stats', { cache: 'no-store' });
                const data = await res.json();
                document.getElementById('monitor-stats').value = JSON.stringify(data, null, 2);
                
                // Renderizar Throughput
                const tpContainer = document.getElementById('monitor-throughput');
                if (data.throughput && Object.keys(data.throughput).length > 0) {
                    let tpHtml = '';
                    for (const [provider, count] of Object.entries(data.throughput)) {
                        let color = count > 0 ? 'var(--color-green-bright)' : 'var(--color-gray)';
                        let shadow = count > 0 ? `text-shadow: 0 0 15px ${color}80;` : '';
                        tpHtml += `
                            <div style="background: rgba(20, 23, 30, 0.5); padding: 1.2rem 1.5rem; border-radius: 12px; border: 1px solid rgba(255,255,255,0.03); border-left: 4px solid ${color}; display: flex; flex-direction: column; min-width: 180px; box-shadow: 0 4px 15px rgba(0,0,0,0.2);">
                                <span style="color: var(--color-gray); font-weight: 600; font-size: 0.8rem; letter-spacing: 1.5px; text-transform: uppercase;">${provider.replace('_', ' ')}</span>
                                <span style="color: ${color}; font-size: 2.5rem; font-weight: 700; margin: 10px 0; line-height: 1; ${shadow}">${count}</span>
                                <span style="color: #6B7280; font-size: 0.75rem; letter-spacing: 1px;">EVENTOS / 30S</span>
                            </div>
                        `;
                    }
                    tpContainer.innerHTML = tpHtml;
                } else {
                    tpContainer.innerHTML = '<span style="color: var(--color-gray); font-size: 0.85rem;">Sin actividad reciente.</span>';
                }
                
                // Fetch health
                const healthRes = await fetch('/health', { cache: 'no-store' });
                if (healthRes.ok) {
                    const healthData = await healthRes.json();
                    document.getElementById('monitor-health').value = JSON.stringify(healthData, null, 2);
                } else {
                    document.getElementById('monitor-health').value = "Error fetching /health: " + healthRes.status;
                }
            } catch (err) {
                console.error("Error cargando monitor interno:", err);
                document.getElementById('monitor-stats').value = "Network Error";
                document.getElementById('monitor-health').value = "Network Error";
            }
        }

        // --- Buscador de Vehículos Únicos ---
        let _vehDebounceTimer = null;
        let _lastVehicleData = {};

        function debounceVehicleSearch() {
            clearTimeout(_vehDebounceTimer);
            _vehDebounceTimer = setTimeout(loadVehicles, 500);
        }

        async function copyAllVisibleVehicles() {
            if (!_lastVehicleData || Object.keys(_lastVehicleData).length === 0) {
                alert("No hay vehículos para copiar.");
                return;
            }
            const dateVal = document.getElementById('veh-date-filter')?.value || new Date().toISOString().split('T')[0];
            let outputDict = { 
                fecha: dateVal, 
                proveedores: {} 
            };
            let totalCopied = 0;

            for (const [key, info] of Object.entries(_lastVehicleData)) {
                if (info.vehicles && info.vehicles.length > 0) {
                    const providerName = `${info.provider} ${info.env}`;
                    outputDict.proveedores[providerName] = info.vehicles;
                    totalCopied += info.vehicles.length;
                }
            }

            if (totalCopied === 0) {
                alert("No hay vehículos para copiar.");
                return;
            }
            try {
                await navigator.clipboard.writeText(JSON.stringify(outputDict, null, 2));
                alert(`Copiadas ${totalCopied} patentes al portapapeles en formato estructurado.`);
            } catch (err) {
                alert("Error al copiar al portapapeles.");
            }
        }

        async function downloadVehicleData(provider, env, chassis) {
            const dateVal = document.getElementById('veh-date-filter')?.value || '';
            try {
                const res = await fetch(`/api/vehicles/data?provider=${provider}&env=${env}&chassis=${encodeURIComponent(chassis)}&date=${dateVal}`, { cache: 'no-store' });
                const data = await res.json();
                const jsonStr = JSON.stringify(data, null, 2);
                
                // Copiar al portapapeles
                await navigator.clipboard.writeText(jsonStr);
                
                // Descargar archivo JSON
                const blob = new Blob([jsonStr], { type: 'application/json' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `${chassis}_${provider}_${env}_${dateVal || 'hoy'}.json`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
                
                // Mostrar confirmación
                alert(`Datos de ${chassis} copiados y descargados.`);
            } catch (err) {
                alert(`Error obteniendo datos de ${chassis}: ` + err.message);
            }
        }

        async function loadVehicles() {
            const dateVal     = document.getElementById('veh-date-filter')?.value || '';
            const providerVal = document.getElementById('veh-provider-filter')?.value || '';
            const searchVal   = document.getElementById('veh-search-input')?.value?.trim() || '';
            const resultsEl   = document.getElementById('veh-results');
            if (!resultsEl) return;

            resultsEl.innerHTML = '<span style="color:var(--color-gray-label); font-size:0.83rem;">Consultando...</span>';

            const params = new URLSearchParams();
            if (dateVal)     params.set('date',     dateVal);
            if (providerVal) params.set('provider', providerVal);
            if (searchVal)   params.set('search',   searchVal);

            try {
                const res  = await fetch(`/api/vehicles/unique?${params.toString()}`, { cache: 'no-store' });
                const data = await res.json();
                _lastVehicleData = data;
                const entries = Object.entries(data);

                if (entries.length === 0) {
                    resultsEl.innerHTML = '<span style="color:var(--color-gray-label);font-size:0.83rem;">No hay proveedores configurados.</span>';
                    return;
                }

                // Total global entre todos los proveedores visibles
                const totalGlobal = entries.reduce((sum, [, v]) => sum + (v.total || 0), 0);
                let html = '';

                // Tarjeta resumen global (solo si hay más de un proveedor)
                if (entries.length > 1) {
                    html += `
                        <div style="background:rgba(124,58,237,0.12);border:1px solid rgba(124,58,237,0.3);border-radius:8px;padding:0.6rem 1rem;min-width:120px;display:flex;flex-direction:column;align-items:center;gap:2px;">
                            <span style="font-size:0.65rem;color:#c4b5fd;text-transform:uppercase;letter-spacing:0.07em;font-weight:600;">Total Global</span>
                            <span style="font-size:2rem;font-weight:800;color:#a78bfa;line-height:1.1;">${totalGlobal}</span>
                            <span style="font-size:0.65rem;color:var(--color-gray-label);">vehículos únicos</span>
                        </div>`;
                }

                // Tarjeta por proveedor (más compactas)
                for (const [key, info] of entries) {
                    const hasVehicles = info.total > 0;
                    const envColor    = info.env === 'PROD' ? '#10b981' : '#f59e0b';
                    const pillsHtml   = hasVehicles
                        ? info.vehicles.map(v => {
                            const esc = v.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                            const highlighted = searchVal
                                ? v.replace(new RegExp(`(${esc})`, 'gi'), '<mark style="background:#7c3aed44;color:#c4b5fd;border-radius:3px;padding:0 2px;">$1</mark>')
                                : v;
                            return `<span onclick="downloadVehicleData('${info.provider.toLowerCase()}', '${info.env.toLowerCase()}', '${v}')" title="Clic para descargar JSON de ${v}" style="background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:5px;padding:3px 8px;font-size:0.75rem;color:#e2e8f0;font-family:monospace;cursor:pointer;transition:background 0.2s;" onmouseover="this.style.background='rgba(124,58,237,0.3)'" onmouseout="this.style.background='rgba(255,255,255,0.06)'">${highlighted}</span>`;
                          }).join('')
                        : '<span style="color:var(--color-gray-label);font-size:0.75rem;font-style:italic;">Sin actividad en este período</span>';

                    html += `
                        <div style="flex:1;min-width:200px;background:rgba(15,17,21,0.6);border:1px solid rgba(255,255,255,0.06);border-radius:8px;padding:0.75rem 1rem;display:flex;flex-direction:column;gap:0.5rem;">
                            <div style="display:flex;align-items:center;gap:6px;">
                                <span style="font-weight:700;font-size:0.85rem;color:white;">${info.provider}</span>
                                <span style="background:${envColor}22;color:${envColor};border:1px solid ${envColor}55;border-radius:4px;padding:1px 6px;font-size:0.68rem;font-weight:700;">${info.env}</span>
                                <span style="margin-left:auto;font-size:1.4rem;font-weight:800;color:${hasVehicles ? '#a78bfa' : 'var(--color-gray)'};">${info.total}</span>
                                <span style="font-size:0.65rem;color:var(--color-gray-label);">veh.</span>
                            </div>
                            <div style="display:flex;gap:5px;flex-wrap:wrap;max-height:100px;overflow-y:auto;padding-right:4px;">
                                ${pillsHtml}
                            </div>
                            ${info.error ? `<span style="color:var(--color-red);font-size:0.7rem;">⚠ ${info.error}</span>` : ''}
                        </div>`;
                }

                resultsEl.innerHTML = html;
            } catch(e) {
                resultsEl.innerHTML = `<span style="color:var(--color-red);font-size:0.83rem;">Error al consultar: ${e.message}</span>`;
            }
        }

        // Poblamos el selector de proveedor del buscador al cargar la vista
        async function initVehicleProviderDropdown() {
            try {
                const res  = await fetch('/api/config/providers', { cache: 'no-store' });
                const list = await res.json();
                const sel  = document.getElementById('veh-provider-filter');
                if (!sel) return;
                const seen = new Set();
                list.forEach(p => {
                    const name = p.provider_name.toLowerCase();
                    if (!seen.has(name)) {
                        seen.add(name);
                        const opt = document.createElement('option');
                        opt.value     = name;
                        opt.innerText = name.toUpperCase();
                        sel.appendChild(opt);
                    }
                });
            } catch(_) {}
        }

        async function loadHistory() {
            try {
                const response = await fetch('/api/history', { cache: 'no-store' });
                const data = await response.json();
                const tbody = document.getElementById('history-table-body');
                tbody.innerHTML = '';
                
                if (data.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center; color: var(--color-gray)">No hay registros históricos consolidados aún.</td></tr>';
                    return;
                }
                
                data.forEach(row => {
                    const tr = document.createElement('tr');
                    const total = (row.sent_count || 0) + (row.failed_count || 0);
                    
                    let transText = 'N/A';
                    if (row.avg_transmission_latency_sec !== null && row.avg_transmission_latency_sec !== undefined) {
                        let t = row.avg_transmission_latency_sec;
                        transText = formatLatency(t, 1);
                    }
                    
                    let hubText = 'N/A';
                    if (row.avg_hub_latency_sec !== null && row.avg_hub_latency_sec !== undefined) {
                        let h = row.avg_hub_latency_sec;
                        hubText = formatLatency(h, 1);
                    }

                    let rcText = 'N/A';
                    if (row.avg_rc_latency_sec !== null && row.avg_rc_latency_sec !== undefined) {
                        let rc = row.avg_rc_latency_sec;
                        rcText = formatLatency(rc, 1);
                    }

                    tr.innerHTML = `
                        <td style="font-weight:bold; color:var(--color-white)">${row.date}</td>
                        <td style="font-weight:bold; color:var(--color-white)">${row.provider}</td>
                        <td><span class="badge ${row.env === 'PROD' ? 'sent' : 'pending'}">${row.env}</span></td>
                        <td style="color: var(--color-green-bright); font-weight: bold;">${row.sent_count}</td>
                        <td style="color: var(--color-red); font-weight: bold;">${row.failed_count}</td>
                        <td style="font-weight: bold; color: var(--color-white);">${total}</td>
                        <td style="color: var(--color-yellow); font-weight: bold;">${transText}</td>
                        <td style="color:#14b8a6; font-weight:bold;">${row.avg_push_latency_ms != null ? row.avg_push_latency_ms.toFixed(3) + 'ms' : 'N/A'}</td>
                        <td style="color: var(--color-green-bright); font-weight: bold;">${hubText}</td>
                        <td style="color: #3b82f6; font-weight: bold;">${rcText}</td>
                    `;
                    tbody.appendChild(tr);
                });
            } catch(e) {
                console.error("Error al cargar historial diario", e);
            }
        }

        // Dashboard Stats
        
        function initSSE() {
            if (!window.evtSource) {
                injectSkeletonRows('recent-table-body',  6, 6);
                injectSkeletonRows('history-table-body', 9, 4);
                window.evtSource = new EventSource('/api/stats/stream');
                window.evtSource.onmessage = e => renderStats(JSON.parse(e.data));
                window.evtSource.onerror = () => {
                    document.getElementById('sync-status').textContent = 'Pérdida de Conexión';
                    document.querySelector('.pulse').style.backgroundColor = '#EF4444';
                    showDisconnectBanner('Conexión SSE interrumpida — reconectando...');
                };
            }
        }

        function renderStats(data) {
            try {
                console.log("RENDER STATS RECEIVED:", data);
                hideDisconnectBanner();
                // If filters are active, we must compute the local counts from data.recent 
                // OR we just use global counts. We will use global counts for simplicity 
                // or compute from data.recent if filtered.
                let filteredRecent = data.recent;
                if (currentProviderFilter !== 'all') {
                    filteredRecent = filteredRecent.filter(ev => ev.provider.toLowerCase() === currentProviderFilter);
                }
                if (currentStatusFilter !== 'all') {
                    filteredRecent = filteredRecent.filter(ev => ev.status === currentStatusFilter);
                }
                
                // Update top boxes. If filtered, we show the global anyway or we could compute it.
                // The current backend get_stats_data doesn't receive filters via SSE, so data is global.
                updateValue('val-pending', data.pending);
                updateValue('val-sent', data.sent);
                updateValue('val-failed', data.failed);
                updateValue('val-retries', data.retries);
                
                // Filtrar push stats por proveedor activo en el selector
                const currentFilter = document.getElementById('filter-provider')?.value || 'all';
                const pushData = (currentFilter && currentFilter !== 'all' && data.push_per_provider)
                    ? (data.push_per_provider[currentFilter.toLowerCase()] || data.push_stats)
                    : (data.push_stats || {});

                updateValue('val-push-latency', ((pushData.avg_ms || 0) / 1000).toFixed(3) + 's');

                const sla = pushData.compliance_pct ?? 100;
                const slaEl = document.getElementById('push-sla-pct');
                if (slaEl) {
                    slaEl.textContent = sla.toFixed(1) + '%';
                    slaEl.style.color = sla >= 99 ? 'var(--color-green-bright)'
                                      : sla >= 95 ? 'var(--color-yellow)'
                                      : 'var(--color-red)';
                }
                const countEl = document.getElementById('push-count');
                if (countEl) countEl.textContent = (pushData.count || 0).toLocaleString();

                updateSparkline('spark-push', 'val-push-latency');
                updateValue('val-latency', (data.avg_latency_sec || 0).toFixed(3) + 's');
                updateValue('val-rc-latency', (data.avg_rc_latency_sec || 0).toFixed(3) + 's');

                updateSparkline('spark-pending',  'val-pending');
                updateSparkline('spark-sent',     'val-sent');
                updateSparkline('spark-failed',   'val-failed');
                updateSparkline('spark-retries',  'val-retries');

                allRecentEvents = data.recent;
                
                // Populate provider dropdown dynamically
                const dropdown = document.getElementById('filter-provider');
                const existingOptions = Array.from(dropdown.options).map(o => o.value);
                const uniqueProviders = data.all_providers || [...new Set(data.recent.map(e => e.provider.toLowerCase()))];
                uniqueProviders.forEach(p => {
                    const pLower = p.toLowerCase();
                    if (!existingOptions.includes(pLower)) {
                        const opt = document.createElement('option');
                        opt.value = pLower;
                        opt.innerText = pLower.toUpperCase();
                        opt.style.color = "black";
                        opt.style.background = "white";
                        dropdown.appendChild(opt);
                    }
                });

                renderRecentTable();
                updateProviderBar(data.recent || []);

                document.getElementById('sync-status').textContent = 'Conectado y Escuchando';
                document.querySelector('.pulse').style.backgroundColor = 'var(--color-green-bright)';

                // Estado del Circuit Breaker de RC
                const cbState = data.rc_circuit_state || 'CLOSED';
                const cbFailures = data.rc_failure_count || 0;
                const cbEl    = document.getElementById('rc-circuit-badge');
                const cbCont  = document.getElementById('rc-circuit-container');
                const cbPulse = document.getElementById('rc-circuit-pulse');
                const cbCuts  = document.getElementById('rc-circuit-microcuts');

                if (cbEl && cbCont) {
                    const labels = {
                        'CLOSED':    { text: 'RC Online',    color: 'var(--color-green-bright)', rgba: 'rgba(16, 185, 129,' },
                        'OPEN':      { text: 'RC Caído',     color: 'var(--color-red)',          rgba: 'rgba(239, 68, 68,' },
                        'HALF_OPEN': { text: 'RC Probando', color: 'var(--color-yellow)',       rgba: 'rgba(251, 191, 36,' },
                    };
                    const info = labels[cbState] || labels['CLOSED'];
                    cbEl.textContent   = info.text;
                    cbEl.style.color   = info.color;
                    cbCont.style.borderColor = info.rgba + ' 0.3)';
                    cbCont.style.background  = info.rgba + ' 0.1)';
                    cbCont.style.textShadow  = '0 0 10px ' + info.rgba + ' 0.5)';
                    
                    if (cbPulse) {
                        cbPulse.style.backgroundColor = info.color;
                    }
                    if (cbCuts) {
                        if (cbState === 'CLOSED' && cbFailures > 0) {
                            cbCuts.style.display = 'block';
                            cbCuts.textContent = `(Micro-cortes: ${cbFailures}/5)`;
                        } else if (cbState !== 'CLOSED') {
                            cbCuts.style.display = 'block';
                            cbCuts.textContent = `(Reconectando...)`;
                        } else {
                            cbCuts.style.display = 'none';
                        }
                    }
                }
            } catch (error) {
                console.error("Error procesando data SSE:", error);
                alert("JS Error en renderStats: " + error.message);
            }
        }


        function updateValue(elementId, newValue) {
            const el = document.getElementById(elementId);
            if (!el) return;

            const isNumeric = !isNaN(parseFloat(newValue)) && String(newValue).match(/^[\d.]+/);

            if (!isNumeric) {
                if (el.textContent !== String(newValue)) {
                    el.textContent = newValue;
                    el.classList.remove('value-update');
                    void el.offsetWidth;
                    el.classList.add('value-update');
                }
                return;
            }

            const targetNum = parseFloat(newValue);
            const suffix    = String(newValue).replace(/^[\d.]+/, '');
            const startNum  = _metricState[elementId] !== undefined
                              ? _metricState[elementId] : (parseFloat(el.textContent) || 0);
            _metricState[elementId] = targetNum;

            if (startNum === targetNum) {
                // Ensure the DOM actually shows the targetNum even if it didn't "change" according to state
                const isFloat = suffix !== '' || String(newValue).includes('.');
                const formatted = isFloat ? targetNum.toFixed(3) + suffix : Math.round(targetNum) + suffix;
                if (el.textContent !== formatted) {
                    el.textContent = formatted;
                }
                return;
            }

            const isFloat   = suffix !== '' || String(newValue).includes('.');
            const duration  = 400;
            const startTime = performance.now();

            function step(now) {
                const elapsed  = now - startTime;
                const progress = Math.min(elapsed / duration, 1);
                const eased    = 1 - (1 - progress) * (1 - progress);
                const current  = startNum + (targetNum - startNum) * eased;
                el.textContent = isFloat
                    ? current.toFixed(3) + suffix
                    : Math.round(current) + suffix;
                if (progress < 1) requestAnimationFrame(step);
            }

            requestAnimationFrame(step);
        }

        function renderRecentTable() {
            const tbody = document.getElementById('recent-table-body');
            tbody.innerHTML = '';
            
            let filtered = allRecentEvents;
            
            if (currentStatusFilter !== 'all') {
                filtered = filtered.filter(ev => ev.status === currentStatusFilter);
            }
            if (currentProviderFilter !== 'all') {
                filtered = filtered.filter(ev => ev.provider.toLowerCase() === currentProviderFilter);
            }
            if (currentLatencyFilter !== 'all') {
                filtered = filtered.filter(ev => {
                    const lat = ev.rc_latency_sec !== null && ev.rc_latency_sec !== undefined ? ev.rc_latency_sec : ev.latency_sec;
                    if (lat === null || lat === undefined) return false;
                    if (currentLatencyFilter === 'low') return lat <= 2;
                    if (currentLatencyFilter === 'medium') return lat > 2 && lat <= 9;
                    if (currentLatencyFilter === 'high') return lat >= 10;
                    return true;
                });
            }
            
            let labelText = "";
            if (currentStatusFilter !== 'all' || currentProviderFilter !== 'all' || currentLatencyFilter !== 'all') {
                let filters = [];
                if (currentStatusFilter !== 'all') filters.push(currentStatusFilter.toUpperCase());
                if (currentProviderFilter !== 'all') filters.push(currentProviderFilter.toUpperCase());
                if (currentLatencyFilter !== 'all') filters.push(`LATENCIA: ${currentLatencyFilter.toUpperCase()}`);
                labelText = `(Filtrado: ${filters.join(' | ')})`;
            }
            document.getElementById('current-filter-label').innerText = labelText;

            if (filtered.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color: var(--color-gray)">No hay eventos con estos filtros.</td></tr>';
                return;
            }

            filtered.forEach((ev, i) => {
                const tr = document.createElement('tr');
                
                const isRetrying = ev.status === 'pending' && ev.retry_count > 0;
                if (ev.status === 'failed')       tr.classList.add('row-failed');
                else if (isRetrying)              tr.classList.add('row-retrying');
                else if (ev.status === 'pending') tr.classList.add('row-pending');
                else if (ev.status === 'sent')    tr.classList.add('row-sent');
                
                let statusClass = 'pending';
                let statusText = 'En Cola';
                let badgeStyle = '';
                if (ev.status === 'sent') { statusClass = 'sent'; statusText = 'Enviado'; }
                else if (ev.status === 'failed') { statusClass = 'failed'; statusText = 'Error'; }
                else if (ev.status === 'pending' && ev.retry_count && ev.retry_count > 0) {
                    statusClass = 'pending';
                    statusText = `Reintento ${ev.retry_count}/4`;
                    badgeStyle = 'background: rgba(251, 191, 36, 0.15); color: var(--color-yellow); border: 1px solid rgba(251, 191, 36, 0.4); padding: 0.15rem 0.6rem; font-size: 0.75rem;';
                }

                let badgeClass = 'pending';
                if (ev.env.toLowerCase() === 'prod') badgeClass = 'sent';
                if (ev.env.toLowerCase() === 'test') badgeClass = 'pending';

                let rawHtml = `<pre style="font-size:0.7rem; background:rgba(0,0,0,0.5); padding:10px; border-radius:4px; max-height:150px; overflow-y:auto; color:var(--color-green-bright); margin-top:5px; white-space:pre-wrap;">${JSON.stringify(ev.rc_format, null, 2)}</pre>`;

                let transmissionHtml = '';
                if (ev.transmission_latency_sec !== null && ev.transmission_latency_sec !== undefined) {
                    let transSec = ev.transmission_latency_sec;
                    let displayTime = formatLatency(transSec, 1);
                    transmissionHtml = `<span style="background:rgba(251,191,36,0.1); color:var(--color-yellow); border:1px solid rgba(251,191,36,0.3); padding:2px 5px; border-radius:4px; font-size:0.7rem; margin-left:5px;" title="Tiempo de viaje desde el dispositivo hasta el Hub">📡 ${displayTime}</span>`;
                }

                let locHtml = `<div style="font-size:0.8rem; color:var(--color-gray); line-height: 1.4;">`;
                locHtml += `<div>Fecha GPS: <span style="color:var(--color-white); font-weight:bold;">${ev.device_date}</span></div>`;
                locHtml += `<div>Coords: <span style="color:var(--color-white)">${ev.coords}</span></div>`;
                locHtml += `<div>Dir: <span style="color:var(--color-white)">${ev.course !== null ? ev.course + '°' : 'N/A'}</span> | Alt: <span style="color:var(--color-white)">${ev.altitude !== null ? ev.altitude + 'm' : 'N/A'}</span></div>`;
                locHtml += `</div>`;

                let sensorHtml = `<div style="font-size:0.8rem; color:var(--color-gray); line-height: 1.4;">`;
                sensorHtml += `<div>Velocidad: <span style="color:var(--color-white)">${ev.speed} km/h</span></div>`;
                sensorHtml += `<div>Ignición: <span style="${ev.ignition==='ON'?'color:var(--color-green-bright); font-weight:bold;':'color:var(--color-red)'}">${ev.ignition}</span></div>`;
                sensorHtml += `<div>Batería: <span style="color:var(--color-white)">${ev.battery !== null ? ev.battery + '%' : 'N/A'}</span> | Temp: <span style="color:var(--color-white)">${ev.temperature !== null ? ev.temperature + '°' : 'N/A'}</span></div>`;
                sensorHtml += `<div>Odom: <span style="color:var(--color-white)">${ev.odometer !== null ? ev.odometer : 'N/A'}</span> | Código EV: <span style="color:var(--color-yellow)">${ev.code}</span></div>`;
                sensorHtml += `</div>`;

                const fallbackTime = ev.time_received || ev.device_date || "time";
                let rcHtml = `<div style="font-size:0.8rem; color:var(--color-gray); line-height: 1.4;">`;
                
                // Status Inlined
                let statusHtml = `<span class="badge ${statusClass}" style="${badgeStyle}">${statusText.toUpperCase()}</span>`;
                if (ev.status === 'pending' && ev.retry_count && ev.retry_count > 0 && ev.next_retry_in_sec !== undefined) {
                    statusHtml += `<br><span style="font-size:0.7rem; color:var(--color-yellow); display:inline-block; margin-top:2px;">⏳ Retrying in ${ev.next_retry_in_sec}s</span>`;
                }


                let jobStyle = 'color:var(--color-white)';
                if (ev.status === 'sent') {
                    jobStyle = 'color:var(--color-green-bright); font-weight:bold;';
                } else if (ev.status === 'failed') {
                    jobStyle = 'color:var(--color-red); font-weight:bold;';
                }
                rcHtml += `<div>Job ID: <span style="${jobStyle}">${ev.job_id || 'N/A'}</span></div>`;
                rcHtml += `<div>Recibido AC: <span style="color:var(--color-white)">${ev.time_received || 'N/A'}</span>${transmissionHtml}</div>`;
                
                let internalLatencyHtml = '';
                let displayLat = '';
                if (ev.status === 'sent' && ev.latency_sec !== null && ev.latency_sec !== undefined) {
                    let lat = ev.latency_sec;
                    displayLat = formatLatency(lat, 3);
                    internalLatencyHtml = ` <span style="color:var(--color-green-bright); font-size:0.75rem; font-weight:bold;">(Hub: ${displayLat})</span>`;
                }
                rcHtml += `<div>Enviado RC: <span style="color:var(--color-white)">${ev.time_sent || 'N/A'}</span>${internalLatencyHtml}</div>`;
                
                let latencyBadge = '';
                if (ev.rc_latency_sec !== null && ev.rc_latency_sec !== undefined) {
                    let rcLat = ev.rc_latency_sec;
                    let c = rcLat <= 2 ? 'var(--color-green-bright)' : (rcLat <= 9 ? 'var(--color-orange)' : 'var(--color-red)');
                    latencyBadge = `<span style="font-size: 0.7rem; background: ${c}; color: var(--color-black); font-weight: bold; padding: 2px 4px; border-radius: 4px; margin-left: 5px;" title="Tiempo de respuesta (latencia de red SOAP) de Recurso Confiable">${rcLat.toFixed(3)}s</span>`;
                }
                
                let rcStatusHtml = '';
                if (ev.status === 'sent') {
                    rcStatusHtml = `<span style="color:var(--color-white)">${ev.time_received_rc}</span>`;
                } else if (ev.status === 'failed') {
                    rcStatusHtml = `<span class="badge failed" style="${badgeStyle}">Error</span>` + (ev.time_received_rc !== 'Fallido' ? ` <span style="color:var(--color-white)">${ev.time_received_rc}</span>` : '');
                } else {
                    rcStatusHtml = `<span class="badge ${statusClass}" style="${badgeStyle}">${statusText}</span>`;
                }
                rcHtml += `<div>RC Confirma: ${rcStatusHtml}${latencyBadge}</div>`;
                
                let rcFormatFinal = ev.rc_format || {};
                if (ev.job_id && typeof rcFormatFinal === 'object') {
                    rcFormatFinal = { ...rcFormatFinal, _rc_assigned_job_id: ev.job_id };
                }
                const jsonPayload = encodeURIComponent(JSON.stringify(rcFormatFinal, null, 2));
                const safeTime = fallbackTime.replace(/[: ()]/g, '');
                
                // Texto plano para copiar
                const plainTransmission = transmissionHtml ? `(Demora Transmisión: ${ev.transmission_latency_sec.toFixed(1)}s)` : '';
                const plainText = `Job ID: ${ev.job_id || 'N/A'}
Recibido AC: ${ev.time_received || 'N/A'} ${plainTransmission}
Enviado RC: ${ev.time_sent || 'N/A'} (Hub: ${displayLat || '0.000s'})
RC Confirma: ${ev.time_received_rc || 'N/A'} ${ev.rc_latency_sec ? ev.rc_latency_sec.toFixed(3) + 's' : '0.000s'}`;
                const plainTextEncoded = encodeURIComponent(plainText);
                
                rcHtml += `<div style="margin-top: 8px; display: flex; gap: 5px; flex-wrap: wrap; align-items: center;">`;
                rcHtml += `${statusHtml}`;
                rcHtml += `<button onclick='viewRawJson(${JSON.stringify(rcFormatFinal).replace(/'/g, "&#39;")}, "Payload Procesado (Hacia SOAP RC)")' style="background: #047857; color: white; border: none; padding: 2px 6px; border-radius: 4px; font-size: 0.7em; cursor: pointer;">
                                🚀 JSON RC
                            </button>`;
                rcHtml += `<a href="data:text/json;charset=utf-8,${jsonPayload}" download="evento_RC_${ev.chassis}_${safeTime}.json" style="color: var(--color-green-bright); text-decoration: none; font-size: 0.7rem; border: 1px solid var(--color-green-bright); padding: 2px 6px; border-radius: 4px; display: inline-block;">📥 Bajar</a>`;
                rcHtml += `<button onclick="navigator.clipboard.writeText(decodeURIComponent('${plainTextEncoded}')); this.innerText='✅ Copiado!'; setTimeout(()=>this.innerText='📋 Copiar Info', 2000)" style="background: #2563EB; color: white; border: none; padding: 2px 6px; border-radius: 4px; font-size: 0.7em; cursor: pointer;" title="Copiar bloque de Trazabilidad">📋 Copiar Info</button>`;
                
                if (ev.status === 'failed' && ev.rc_response) {
                    let errStr = String(ev.rc_response).replace(/'/g, "\\'").replace(/"/g, '&quot;').replace(/\n/g, ' ');
                    rcHtml += `<button style="background-color: rgba(239, 68, 68, 0.2); border: 1px solid #EF4444; padding: 2px 5px; color: #EF4444; font-size: 0.7rem; border-radius:4px; cursor:pointer;" onclick="alert('Error RC:\\n${errStr}')">⚠️ Error</button>`;
                }
                rcHtml += `</div>`;
                rcHtml += `</div>`;
                
                let chassisHtml = `
                    <div style="display: flex; flex-direction: column; align-items: flex-start;">
                        <span style="color:var(--color-yellow); font-weight:bold;">${ev.chassis || 'N/A'}</span>
                        ${ev.serial && ev.serial !== ev.chassis ? `<span style="color:var(--color-gray); font-size: 0.8rem;">IMEI: ${ev.serial}</span>` : ''}
                        <div style="margin-top: 4px; display: flex; gap: 5px;">
                            <button onclick='viewRawJson(${JSON.stringify(ev.raw_data || "{}").replace(/'/g, "&#39;")}, "Payload Original (Crudo del Proveedor)")' style="background: #374151; color: white; border: none; padding: 2px 6px; border-radius: 4px; font-size: 0.7em; cursor: pointer;" title="Ver payload JSON original sin procesar">
                                📄 JSON Origen
                            </button>
                            <a href="data:text/json;charset=utf-8,${encodeURIComponent(JSON.stringify(ev.raw_data || {}))}" download="origen_${ev.provider}_${safeTime}.json" style="color: var(--color-gray-label); text-decoration: none; font-size: 0.7rem; border: 1px solid var(--color-gray-label); padding: 2px 6px; border-radius: 4px; display: inline-block;" title="Descargar JSON Original">📥 Bajar</a>
                        </div>
                    </div>
                `;

                tr.innerHTML = `
                    <td>
                        <span style="font-weight:bold; color:var(--color-white)">${ev.provider.toUpperCase()}</span><br>
                        <span class="badge ${badgeClass}" style="margin-top:5px">${ev.env.toUpperCase()}</span>
                    </td>
                    <td>
                        ${chassisHtml}
                    </td>
                    <td>${locHtml}</td>
                    <td>${sensorHtml}</td>
                    <td>${rcHtml}</td>
                `;
                tbody.appendChild(tr);
            });
        }
        
        // UI Helpers
        // Configuración
        async function loadConfig() {
            try {
                const res = await fetch('/api/config');
                currentConfigs = await res.json();
                const tbody = document.getElementById('config-table-body');
                tbody.innerHTML = '';

                // Agrupar por proveedor
                const grouped = {};
                currentConfigs.forEach((c, idx) => {
                    c._originalIdx = idx; // Guardar id original para guardar datos después
                    if(!grouped[c.provider_name]) grouped[c.provider_name] = [];
                    grouped[c.provider_name].push(c);
                });

                Object.keys(grouped).forEach(provider => {
                    const envs = grouped[provider];
                    envs.forEach((c, i) => {
                        const tr = document.createElement('tr');
                        
                        let providerCell = '';
                        if (i === 0) {
                            providerCell = `<td rowspan="${envs.length}" style="font-weight:bold; color:var(--color-text); vertical-align: middle;">${c.provider_name}</td>`;
                        }

                        tr.innerHTML = `
                            ${providerCell}
                            <td><span class="badge ${c.env === 'PROD' ? 'sent' : 'pending'}">${c.env}</span></td>
                            <td>
                                <label class="switch">
                                    <input type="checkbox" id="active_${c._originalIdx}" ${c.is_active ? 'checked' : ''}>
                                    <span class="slider"></span>
                                </label>
                            </td>
                            <td>
                                <div style="display: flex; align-items: center;">
                                    <label class="switch" title="Si está activo, los eventos se envían a RC pero de forma simulada. Útil para pruebas.">
                                        <input type="checkbox" id="mock_${c._originalIdx}" ${c.use_mock ? 'checked' : ''} onchange="
                                            const lbl = document.getElementById('mock_lbl_${c._originalIdx}');
                                            if (this.checked) {
                                                lbl.innerText = 'SIMULANDO';
                                                lbl.style.color = 'var(--color-green-vibrant)';
                                            } else {
                                                lbl.innerText = 'REAL';
                                                lbl.style.color = '#EF4444';
                                            }
                                        ">
                                        <span class="slider"></span>
                                    </label>
                                    <span id="mock_lbl_${c._originalIdx}" style="font-size: 0.75rem; font-weight: 700; margin-left: 8px; display: inline-block; width: 80px; color: ${c.use_mock ? 'var(--color-green-vibrant)' : '#EF4444'};">
                                        ${c.use_mock ? 'SIMULANDO' : 'REAL'}
                                    </span>
                                </div>
                            </td>
                            <td class="center-switch">
                                <label class="switch" title="${(c.provider_type || 'pull').toLowerCase() === 'push'
                                    ? 'PUSH: activar solo si el proveedor envía estado continuo (no transiciones). SOS siempre emite.'
                                    : 'PULL: filtra sensores repetidos. SOS y eventos momentáneos siempre emiten. Transiciones detectadas.'}" style="display: inline-block; margin: 0 auto;">
                                    <input type="checkbox" id="dedup_${c._originalIdx}" ${c.enable_state_dedup !== false ? 'checked' : ''}>
                                    <span class="slider"></span>
                                </label>
                            </td>
                            <td>${c.provider_name.toLowerCase() === 'protrack' ? '<input class="form-control" type="text" disabled value="--- N/A ---" style="width: 100px; color: var(--color-gray); background: var(--level-1); text-align: center; border: 1px dashed var(--card-border);" title="No aplica para proveedores PULL">' : `<input class="form-control" type="text" id="webhook_header_${c._originalIdx}" value="${c.webhook_auth_header || 'x-api-key'}" style="width: 100px;">`}</td>
                            <td>${c.provider_name.toLowerCase() === 'protrack' ? '<input class="form-control" type="text" disabled value="--- N/A (Es PULL) ---" style="color: var(--color-gray); background: var(--level-1); font-style: italic; border: 1px dashed var(--card-border);" title="No aplica para proveedores PULL">' : `<input class="form-control" type="password" id="webhook_auth_${c._originalIdx}" placeholder="${c.has_webhook_auth ? '•••••••• (Cifrado)' : ''}" title="Dejar vacío para mantener el actual">`}</td>
                            <td><input class="form-control" type="text" id="user_${c._originalIdx}" value="${c.rc_user || ''}"></td>
                            <td><input class="form-control" type="password" id="pass_${c._originalIdx}" placeholder="${c.has_rc_password ? '•••••••• (Cifrado)' : ''}" title="Dejar vacío para mantener el actual"></td>
                            <td><input class="form-control" type="number" id="purge_${c._originalIdx}" value="${c.purge_interval_min}" style="width: 80px;"></td>
                            <td><input class="form-control" type="number" id="run_int_${c._originalIdx}" value="${c.run_interval_sec}" style="width: 80px;"></td>
                            <td>
                                <select id="queue_${c._originalIdx}" class="form-control" style="width: 100px; background-color: var(--level-0); color: var(--color-text); cursor: pointer;">
                                    <option value="sqlite" ${c.queue_backend === 'sqlite' ? 'selected' : ''}>SQLite</option>
                                    <option value="postgres" ${c.queue_backend === 'postgres' ? 'selected' : ''}>PostgreSQL</option>
                                    <option value="redis" ${c.queue_backend === 'redis' ? 'selected' : ''}>Redis</option>
                                </select>
                            </td>

                        `;
                        tbody.appendChild(tr);
                    });
                });
            } catch(e) {
                console.error("Error al cargar config", e);
            }
        }

        async function saveConfig() {
            const updates = currentConfigs.map((c, idx) => ({
                id: c.id,
                is_active: document.getElementById(`active_${idx}`).checked,
                use_mock: document.getElementById(`mock_${idx}`).checked,
                rc_user: document.getElementById(`user_${idx}`).value,
                rc_password: document.getElementById(`pass_${idx}`).value,
                webhook_auth_secret: document.getElementById(`webhook_auth_${idx}`) ? document.getElementById(`webhook_auth_${idx}`).value : null,
                webhook_auth_header: document.getElementById(`webhook_header_${idx}`) ? document.getElementById(`webhook_header_${idx}`).value : null,
                purge_interval_min: parseInt(document.getElementById(`purge_${idx}`).value) || 15,
                run_interval_sec: parseInt(document.getElementById(`run_int_${idx}`).value) || 5,
                queue_backend: document.getElementById(`queue_${idx}`).value,
                enable_state_dedup: document.getElementById(`dedup_${idx}`) ? document.getElementById(`dedup_${idx}`).checked : c.enable_state_dedup
            }));

            try {
                const res = await fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(updates)
                });
                if(res.ok) {
                    alert('¡Configuración guardada exitosamente!');
                    loadConfig(); // reload
                } else {
                    alert('Error al guardar');
                }
            } catch(e) {
                console.error("Error guardando config", e);
            }
        }

        // Auditoría
        async function loadLogs() {
            try {
                const res = await fetch('/api/logs');
                const logs = await res.json();
                const tbody = document.getElementById('logs-table-body');
                tbody.innerHTML = '';

                if (logs.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="3" style="text-align:center; color: var(--color-gray)">No hay logs de auditoría disponibles aún.</td></tr>';
                    return;
                }

                logs.forEach((log, idx) => {
                    const tr = document.createElement('tr');
                    const payloadStr = JSON.stringify(log.payload, null, 2);
                    // Botón de descarga Base64/URI
                    const encodedPayload = encodeURIComponent(payloadStr);
                    const cleanDate = log.timestamp.replace(/[:.T-]/g, '');
                    
                    tr.innerHTML = `
                        <td style="white-space: nowrap; font-size: 0.85rem">${log.timestamp.replace('T', ' ')} (UTC)</td>
                        <td><span class="badge pending">${log.provider.toUpperCase()}</span></td>
                        <td>
                            <div style="display:flex; justify-content:space-between; margin-bottom: 5px;">
                                <span style="color:var(--color-gray); font-size:0.8rem;">JSON Raw Data</span>
                                <a href="data:text/json;charset=utf-8,${encodedPayload}" download="payload_${log.provider}_${cleanDate}.json" style="color: var(--color-green-bright); text-decoration: none; font-size: 0.8rem; border: 1px solid var(--color-green-bright); padding: 2px 8px; border-radius: 4px; transition: all 0.2s;">📥 Descargar JSON</a>
                            </div>
                            <pre style="background: rgba(0,0,0,0.5); padding: 15px; border-radius: 6px; font-size: 0.8rem; color: var(--color-green-bright); max-height: 400px; overflow-y: auto; margin:0; white-space: pre-wrap; word-wrap: break-word; width: 100%;">${payloadStr}</pre>
                        </td>
                    `;
                    tbody.appendChild(tr);
                });
            } catch(e) {
                console.error("Error cargando logs", e);
            }
        }

        async function clearLogs() {
            if(!confirm("¿Seguro que deseas vaciar todo el historial de auditoría?")) return;
            try {
                await fetch('/api/logs', { method: 'DELETE' });
                await loadLogs();
            } catch (err) {
                console.error("Error limpiando logs", err);
            }
        }

        // Simulador
        async function loadSimulator() {
            try {
                // Traer proveedores unicos
                const res = await fetch('/api/config');
                const configs = await res.json();
                const providerSelect = document.getElementById('sim-provider');
                providerSelect.innerHTML = '';
                
                const uniqueProviders = [...new Set(configs.map(c => c.provider_name.toLowerCase()))];
                
                if (uniqueProviders.length === 0) {
                    providerSelect.innerHTML = '<option value="">(No hay proveedores configurados)</option>';
                } else {
                    uniqueProviders.forEach(p => {
                        const opt = document.createElement('option');
                        opt.value = p;
                        opt.textContent = p.toUpperCase();
                        providerSelect.appendChild(opt);
                    });
                }
                
                document.getElementById('sim-result').textContent = '';
            } catch(e) {
                console.error("Error cargando opciones del simulador", e);
            }
        }

        async function sendSimulation() {
            const provider = document.getElementById('sim-provider').value;
            const payloadStr = document.getElementById('sim-payload').value;
            const resultDiv = document.getElementById('sim-result');
            
            if (!provider) {
                alert("Debes seleccionar un proveedor.");
                return;
            }
            if (!payloadStr.trim()) {
                alert("Debes pegar un JSON válido en la caja.");
                return;
            }

            // Validar que sea JSON valido
            let jsonPayload;
            try {
                jsonPayload = JSON.parse(payloadStr);
            } catch (e) {
                alert("El texto ingresado NO es un JSON válido. Revisa los corchetes y comillas.");
                return;
            }

            resultDiv.style.color = "var(--color-gray)";
            resultDiv.textContent = "Disparando...";

            try {
                // Pegar directo al webhook con env=test
                const res = await fetch(`/${provider}/webhook?env=test`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(jsonPayload)
                });
                
                if (res.ok) {
                    resultDiv.style.color = "var(--color-green-bright)";
                    resultDiv.textContent = "¡Éxito! El webhook devolvió estado " + res.status + ". Ve al Dashboard Principal o a Logs para verificar.";
                } else {
                    resultDiv.style.color = "#EF4444";
                    resultDiv.textContent = "Error: El webhook devolvió estado " + res.status;
                }
            } catch (error) {
                resultDiv.style.color = "#EF4444";
                resultDiv.textContent = "Fallo de conexión al enviar el simulacro.";
                console.error(error);
            }
        }

        function toggleMenu() {
            const sidebar = document.getElementById('sidebar');
            const overlay = document.getElementById('overlay');
            sidebar.classList.toggle('active');
            
            if (sidebar.classList.contains('active')) {
                overlay.style.display = 'block';
                setTimeout(() => overlay.classList.add('active'), 10);
            } else {
                overlay.classList.remove('active');
                setTimeout(() => overlay.style.display = 'none', 300);
            }
        }

        // Lógica de Visor de BD y Settings
        async function loadDatabases() {
            const select = document.getElementById('db-select');
            select.innerHTML = '<option value="">Cargando...</option>';
            try {
                const res = await fetch('/api/db-viewer/databases');
                const dbs = await res.json();
                select.innerHTML = '<option value="">-- Seleccione BD --</option>';
                dbs.forEach(db => {
                    select.innerHTML += `<option value="${db.name}">${db.name}</option>`;
                });
            } catch(e) {
                select.innerHTML = '<option value="">Error cargando BDs</option>';
            }
        }

        async function loadTables() {
            const dbName = document.getElementById('db-select').value;
            const tableSelect = document.getElementById('table-select');
            if(!dbName) {
                tableSelect.innerHTML = '<option value="">Seleccione una base de datos primero...</option>';
                return;
            }
            tableSelect.innerHTML = '<option value="">Cargando tablas...</option>';
            try {
                const res = await fetch(`/api/db-viewer/tables?db_name=${encodeURIComponent(dbName)}`);
                const data = await res.json();
                if(data.error) throw new Error(data.error);
                tableSelect.innerHTML = '<option value="">-- Seleccione Tabla --</option>';
                data.tables.forEach(t => {
                    tableSelect.innerHTML += `<option value="${t}">${t}</option>`;
                });
            } catch(e) {
                tableSelect.innerHTML = '<option value="">Error cargando tablas</option>';
            }
        }

        // Estado global del editor de BD
        let _dbEditorState = { db: null, table: null, editable: false, pendingEdit: null };

        async function loadQueryData() {
            const dbName = document.getElementById('db-select').value;
            const tableName = document.getElementById('table-select').value;
            if(!dbName || !tableName) {
                alert("Debe seleccionar una base de datos y una tabla.");
                return;
            }
            
            const thead = document.getElementById('db-viewer-thead');
            const tbody = document.getElementById('db-viewer-tbody');
            const info = document.getElementById('db-viewer-info');
            const badge = document.getElementById('db-edit-badge');
            
            thead.innerHTML = '<tr><th>Cargando datos...</th></tr>';
            tbody.innerHTML = '';
            info.textContent = 'Mostrando 0 registros.';
            badge.style.display = 'none';
            
            try {
                const res = await fetch(`/api/db-viewer/query?db_name=${encodeURIComponent(dbName)}&table=${encodeURIComponent(tableName)}&limit=50&offset=0`);
                const data = await res.json();
                if(data.error) throw new Error(data.error);

                _dbEditorState.db = dbName;
                _dbEditorState.table = tableName;
                _dbEditorState.editable = data.editable;

                // Badge: editable o solo lectura
                if (data.editable) {
                    badge.textContent = '✏️ Edición habilitada — doble clic en celda para editar';
                    badge.style.cssText = 'display:inline-block; font-size:0.78rem; padding:0.3rem 0.8rem; border-radius:20px; font-weight:700; background:rgba(16,185,129,0.15); color:#10B981; border:1px solid rgba(16,185,129,0.3);';
                } else {
                    badge.textContent = '🔒 Solo lectura — tabla operativa protegida';
                    badge.style.cssText = 'display:inline-block; font-size:0.78rem; padding:0.3rem 0.8rem; border-radius:20px; font-weight:700; background:rgba(248,113,113,0.1); color:#f87171; border:1px solid rgba(248,113,113,0.25);';
                }

                // Generar Columnas — ocultar columna __rowid__ al usuario
                const visibleCols = data.columns.filter(c => c !== '__rowid__');
                thead.innerHTML = '<tr>' + visibleCols.map(c => `<th>${c}</th>`).join('') + '</tr>';
                
                // Generar Filas
                if(data.rows.length === 0) {
                    tbody.innerHTML = `<tr><td colspan="${visibleCols.length}" style="text-align:center;">No hay datos en esta tabla.</td></tr>`;
                } else {
                    tbody.innerHTML = data.rows.map(row => {
                        const rowid = row[0]; // primer elemento es siempre __rowid__
                        const cells = row.slice(1); // resto son los datos visibles
                        return '<tr>' + cells.map((val, colIdx) => {
                            const colName = visibleCols[colIdx];
                            const title = String(val).replace(/"/g, '&quot;');
                            const editAttr = data.editable ? `ondblclick="openEditModal(${rowid}, '${colName}', this)" style="cursor:pointer; white-space:nowrap; max-width:200px; overflow:hidden; text-overflow:ellipsis;"` : `style="white-space:nowrap; max-width:200px; overflow:hidden; text-overflow:ellipsis;"`;
                            return `<td ${editAttr} title="${title}">${val !== null ? val : '<em>NULL</em>'}</td>`;
                        }).join('') + '</tr>';
                    }).join('');
                }
                info.textContent = `Mostrando ${data.rows.length} de ${data.total} registros en "${tableName}". (Límite visual 50)`;
            } catch(e) {
                thead.innerHTML = '<tr><th>Error</th></tr>';
                tbody.innerHTML = `<tr><td style="color:var(--color-red)">${e.message}</td></tr>`;
            }
        }

        function openEditModal(rowid, colName, tdEl) {
            if (!_dbEditorState.editable) return;
            const currentVal = tdEl.innerText.trim();
            _dbEditorState.pendingEdit = { rowid, colName, tdEl, originalVal: currentVal };
            document.getElementById('db-edit-modal-target').textContent = `${_dbEditorState.table} (fila ${rowid})`;
            document.getElementById('db-edit-modal-col').textContent = colName;
            document.getElementById('db-edit-modal-val').textContent = currentVal;
            document.getElementById('db-edit-password').value = '';
            document.getElementById('db-edit-error').style.display = 'none';
            const modal = document.getElementById('db-edit-modal');
            modal.style.display = 'flex';
            setTimeout(() => document.getElementById('db-edit-password').focus(), 100);
        }

        function closeEditModal() {
            document.getElementById('db-edit-modal').style.display = 'none';
            _dbEditorState.pendingEdit = null;
        }

        async function confirmCellEdit() {
            const pass = document.getElementById('db-edit-password').value;
            const errEl = document.getElementById('db-edit-error');
            const { rowid, colName, tdEl } = _dbEditorState.pendingEdit;
            const newValue = _dbEditorState.pendingEdit.originalVal;

            if (!pass) { errEl.textContent = 'Debes ingresar la contraseña.'; errEl.style.display='block'; return; }

            try {
                const res = await fetch('/api/db-viewer/update_cell', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        db_name: _dbEditorState.db,
                        table: _dbEditorState.table,
                        rowid: rowid,
                        column_name: colName,
                        new_value: newValue,
                        password: pass
                    })
                });
                const result = await res.json();
                if (!res.ok) {
                    errEl.textContent = result.detail || 'Error desconocido';
                    errEl.style.display = 'block';
                    return;
                }
                // Éxito: cerrar modal y recargar tabla
                closeEditModal();
                tdEl.style.transition = 'background 0.4s';
                tdEl.style.background = 'rgba(16,185,129,0.25)';
                setTimeout(() => { tdEl.style.background = ''; loadQueryData(); }, 800);
            } catch(e) {
                errEl.textContent = 'Error de conexión: ' + e.message;
                errEl.style.display = 'block';
            }
        }

        // Cerrar modal al presionar Escape
        document.addEventListener('keydown', e => {
            if (e.key === 'Escape') closeEditModal();
            if (e.key === 'Enter' && document.getElementById('db-edit-modal').style.display === 'flex') confirmCellEdit();
        });

        // Inicio
        
        initSSE();


        const API_BASE = "/api/config";
        let ipaasProviders = [];
        let currentIpaasProvider = null;
        let sessionUuid = crypto.randomUUID();
        let pollingInterval = null;

        function loadIntegrationStudio() {
            const inspectorUrl = window.location.origin + "/inspector/catch/" + sessionUuid;
            
            const inspectorEl = document.getElementById('inspectorWebhookUrl');
            if (inspectorEl) inspectorEl.innerText = inspectorUrl;
            
            const mappingInspectorEl = document.getElementById('webhookUrlInspector');
            if (mappingInspectorEl) mappingInspectorEl.innerText = inspectorUrl;
            
            loadProviders();
        }

        async function loadProviders() {
            try {
                const res = await fetch(API_BASE + '/providers');
                ipaasProviders = await res.json();
                const select = document.getElementById('providerSelect');
                select.innerHTML = '<option value="">-- Selecciona un Proveedor --</option>';
                ipaasProviders.forEach(p => {
                    select.innerHTML += `<option value="${p.provider_name}|${p.env}">${p.provider_name.toUpperCase()} (${p.env})</option>`;
                });
                select.onchange = function() {
                    const val = this.value;
                    if(val) {
                        const [name, env] = val.split('|');
                        currentIpaasProvider = {name, env};
                        
                        // Actualizar URL de Producción
                        const prodUrl = window.location.origin + `/webhook/dynamic/${name}?env=${env}`;
                        const prodEl = document.getElementById('prodWebhookUrl');
                        if (prodEl) prodEl.innerText = prodUrl;

                        loadMapping(name, env);
                        if(typeof loadEnrichment === 'function') loadEnrichment(name, env);
                    } else {
                        currentIpaasProvider = null;
                    }
                };
            } catch (e) {
                console.error("Error loading providers", e);
            }
        }

        async function loadMapping(name, env) {
            try {
                const res = await fetch(`${API_BASE}/${name}/${env}/mapping`);
                const data = await res.json();
                document.querySelectorAll('#view-integrations .mapping-row input').forEach(inp => inp.value = '');
                
                const rawMapping = data.mapping || data;
                let baseMapping = rawMapping;
                
                if (rawMapping.base_mapping) {
                    baseMapping = rawMapping.base_mapping;
                    _currentRules = rawMapping.trigger_rules || [];
                    const defaultRule = rawMapping.default_rule || {};
                    const defRc = document.getElementById('default_rc_code');
                    const defLab = document.getElementById('default_rc_label');
                    if(defRc) defRc.value = defaultRule.rc_code || '1';
                    if(defLab) defLab.value = defaultRule.label || 'Reporte GPS';
                } else {
                    _currentRules = [];
                }
                renderTriggerRules(_currentRules);
                
                for(const key in baseMapping) {
                    const inp = document.getElementById('map_' + key);
                    if(inp) inp.value = baseMapping[key];
                }
                
                // Actualizar estilo visual del map_code si hay reglas
                updateMapCodeVisual();
                
                if (data.fetch && Object.keys(data.fetch).length > 0) {
                    const f = data.fetch;
                    if(f.url) document.getElementById('pullUrl').value = f.url;
                    if(f.method) document.getElementById('pullMethod').value = f.method;
                    if(f.auth_type) {
                        document.getElementById('authType').value = f.auth_type;
                        document.getElementById('pullAuthFields').style.display = f.auth_type === 'none' ? 'none' : 'block';
                    }
                    if(f.auth_user) document.getElementById('pullAuthUser').value = f.auth_user;
                    if(f.auth_pass) document.getElementById('pullAuthPass').value = f.auth_pass;
                    if(f.bearer_token) {
                        const bt = document.getElementById('bearerTokenValue');
                        if(bt) bt.value = f.bearer_token;
                    }
                } else {
                    document.getElementById('pullUrl').value = '';
                    document.getElementById('pullMethod').value = 'GET';
                    document.getElementById('authType').value = 'none';
                    document.getElementById('pullAuthFields').style.display = 'none';
                    document.getElementById('pullAuthUser').value = '';
                    document.getElementById('pullAuthPass').value = '';
                    const bt = document.getElementById('bearerTokenValue');
                    if(bt) bt.value = '';
                }
            } catch (e) {
                console.error("Error loading mapping", e);
            }
        }

        function _buildFullPayload() {
            const defaultRcCode  = document.getElementById('default_rc_code')?.value  || '1';
            const defaultRcLabel = document.getElementById('default_rc_label')?.value || 'Reporte GPS';
            const baseMapping = getCurrentBaseMapping();

            const fullSchema = {
                base_mapping:  baseMapping,
                trigger_rules: _currentRules,
                default_rule: {
                    enabled:   true,
                    rc_code:   defaultRcCode,
                    label:     defaultRcLabel,
                    fire_when: 'always'
                }
            };
            
            const elUrl = document.getElementById('pullUrl');
            const elMethod = document.getElementById('pullMethod');
            const elAuthType = document.getElementById('authType');
            const elUser = document.getElementById('pullAuthUser');
            const elPass = document.getElementById('pullAuthPass');
            const elBearer = document.getElementById('bearerTokenValue');
            const elHeaders = document.getElementById('pullHeaders');
            const elBody = document.getElementById('pullBody');

            const fetchData = {
                url: elUrl ? elUrl.value.trim() : "",
                method: elMethod ? elMethod.value : "GET",
                auth_type: elAuthType ? elAuthType.value : "none",
                auth_user: elUser ? elUser.value.trim() : "",
                auth_pass: elPass ? elPass.value.trim() : "",
                bearer_token: elBearer ? elBearer.value.trim() : "",
                headers: elHeaders ? elHeaders.value.trim() : "",
                body: elBody ? elBody.value.trim() : ""
            };
            
            return {
                mapping: fullSchema,
                fetch: fetchData
            };
        }

        async function saveMapping(event) {
            await _saveConfigUnified(event, "💾 Guardar Mapeo", "saveToast");
        }
        
        async function saveTriggerRules(event) {
            await _saveConfigUnified(event, "💾 Guardar Reglas", "rulesSaveToast");
        }
        
        async function savePullConfig(event) {
            await _saveConfigUnified(event, "💾 Guardar Configuración PULL", null);
        }

        async function _saveConfigUnified(event, originalText, toastId) {
            if(!currentIpaasProvider) {
                alert("Selecciona un proveedor primero.");
                return;
            }

            let btn = null;
            if (event && event.target) {
                btn = event.target;
                originalText = btn.innerText;
                btn.innerText = "⏳ Guardando...";
                btn.style.opacity = "0.7";
            }

            const payload = _buildFullPayload();
            
            try {
                const res = await fetch(`${API_BASE}/${currentIpaasProvider.name}/${currentIpaasProvider.env}/mapping`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if(data.status === 'success') {
                    if (btn) {
                        btn.innerText = "✅ Guardado!";
                        btn.style.opacity = "1";
                        btn.style.background = "#10B981";
                        
                        if (toastId) {
                            const toast = document.getElementById(toastId);
                            if (toast) {
                                toast.style.display = 'block';
                                setTimeout(() => toast.style.display = 'none', 3000);
                            }
                        }
                        
                        setTimeout(() => {
                            btn.innerText = originalText;
                            btn.style.background = "var(--color-green-vibrant)";
                        }, 2500);
                    }
                } else {
                    if(btn) { btn.innerText = originalText; btn.style.opacity = "1"; }
                    alert("Error al guardar: " + data.message);
                }
            } catch(e) {
                if(btn) { btn.innerText = originalText; btn.style.opacity = "1"; }
                alert("Error de red al guardar.");
            }
        }
        
        // ============================================================
        // INTEGRATION STUDIO — Sistema de Tabs y Motor de Reglas
        // ============================================================

        function switchStudioTab(tabName) {
            document.querySelectorAll('.studio-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.studio-panel').forEach(p => p.classList.remove('active'));
            document.getElementById(`stab-${tabName}`).classList.add('active');
            document.getElementById(`spanel-${tabName}`).classList.add('active');
        }

        function setIntakeMode(mode) {
            const isPull = (mode === 'pull');
            document.getElementById('mode-push-btn').classList.toggle('active', !isPull);
            document.getElementById('mode-pull-btn').classList.toggle('active',  isPull);
            document.getElementById('intake-push-section').style.display = isPull ? 'none'  : 'block';
            document.getElementById('intake-pull-section').style.display = isPull ? 'block' : 'none';
        }

        function _ruleId() {
            return 'rule_' + Date.now().toString(36);
        }

        function updateMapCodeVisual() {
            const mapCodeInp = document.getElementById('map_code');
            if(!mapCodeInp) return;
            if(_currentRules.length > 0) {
                mapCodeInp.value = "⚡ " + _currentRules.length + " Regla(s) Controlando";
                mapCodeInp.disabled = true;
                mapCodeInp.style.background = "rgba(16,185,129,0.05)";
                mapCodeInp.style.color = "var(--color-green-vibrant)";
                mapCodeInp.style.border = "1px solid rgba(16,185,129,0.3)";
                mapCodeInp.title = "Las reglas de disparo dinámicas están activas y sobrescribirán este código.";
            } else {
                if (mapCodeInp.value.startsWith("⚡")) mapCodeInp.value = "";
                mapCodeInp.disabled = false;
                mapCodeInp.style.background = "";
                mapCodeInp.style.color = "";
                mapCodeInp.style.border = "";
                mapCodeInp.title = "";
            }
        }

        function renderTriggerRules(rules) {
            const container = document.getElementById('trigger-rules-list');
            if (!rules || rules.length === 0) {
                container.innerHTML = '<p style="color:var(--color-gray-label);font-size:0.82rem;text-align:center;padding:1rem;">Sin reglas configuradas. Agregá una con el botón de abajo.</p>';
                updateMapCodeVisual();
                return;
            }

            const OPERATORS = [
                { value: 'eq',         label: '== igual a' },
                { value: 'neq',        label: '≠ distinto' },
                { value: 'gt',         label: '> mayor que' },
                { value: 'lt',         label: '< menor que' },
                { value: 'gte',        label: '>= mayor/igual' },
                { value: 'lte',        label: '<= menor/igual' },
                { value: 'exists',     label: '✓ existe' },
                { value: 'not_exists', label: '✗ no existe' },
            ];

            container.innerHTML = rules.map((r, i) => {
                const opOptions = OPERATORS.map(o =>
                    `<option value="${o.value}" ${r.operator === o.value ? 'selected' : ''}>${o.label}</option>`
                ).join('');

                // event_type: 'state' (default) | 'momentary'
                const evTypeOptions = [
                    { value: 'state',     label: 'Estado' },
                    { value: 'momentary', label: 'Momentáneo' },
                ].map(o =>
                    `<option value="${o.value}" ${(r.event_type || 'state') === o.value ? 'selected' : ''}>${o.label}</option>`
                ).join('');

                return `
                <div class="rule-row ${r.enabled ? '' : 'disabled'}" id="rulerow-${r.id}">
                    <input type="text"  class="form-control" style="font-size:0.8rem;" value="${r.field   || ''}" placeholder="campo del payload"  onchange="updateRule('${r.id}','field',   this.value)">
                    <select             class="form-control" style="font-size:0.8rem;"                                                              onchange="updateRule('${r.id}','operator',this.value)">${opOptions}</select>
                    <input type="text"  class="form-control" style="font-size:0.8rem;" value="${r.value   || '1'}" placeholder="valor (ej: 1)"    onchange="updateRule('${r.id}','value',   this.value)">
                    <input type="text"  class="form-control" style="font-size:0.8rem;" value="${r.label   || ''}" placeholder="descripción"        onchange="updateRule('${r.id}','label',   this.value)">
                    <input type="text"  class="form-control" style="font-size:0.8rem;" value="${r.rc_code || ''}" placeholder="código RC"          onchange="updateRule('${r.id}','rc_code', this.value)">
                    <select title="Estado: filtra repeticiones (motor, puerta). Momentáneo: siempre emite (SOS, crash)." class="form-control" style="font-size:0.8rem;" onchange="updateRule('${r.id}','event_type',this.value)">${evTypeOptions}</select>
                    <input type="text"  class="form-control" style="font-size:0.8rem;" value="${r.dedup_key || ''}" placeholder="ej: doorstatus" title="Agrupa estados mutuamente excluyentes. Ej: code=10 (abierto) y code=34 (cerrado) con key='door'. Vacío = usa rc_code." onchange="updateRule('${r.id}','dedup_key',this.value)">
                    <div style="display:flex;gap:4px;align-items:center;">
                        <input type="checkbox" ${r.enabled ? 'checked' : ''} title="Activar/desactivar" style="width:16px;height:16px;accent-color:var(--color-green-vibrant);cursor:pointer;" onchange="updateRule('${r.id}','enabled',this.checked)">
                        <button class="btn-delete-rule" onclick="deleteRule('${r.id}')">✕</button>
                    </div>
                </div>`;
            }).join('');
            updateMapCodeVisual();
        }

        let _currentRules = [];

        function addTriggerRule() {
            _currentRules.push({
                id:         _ruleId(),
                field:      '',
                operator:   'eq',
                value:      '1',
                rc_code:    '',
                label:      '',
                enabled:    true,
                event_type: 'state',   // 'state' | 'momentary'
                dedup_key:  ''         // vacío = usar rc_code como key
            });
            renderTriggerRules(_currentRules);
        }

        function deleteRule(ruleId) {
            _currentRules = _currentRules.filter(r => r.id !== ruleId);
            renderTriggerRules(_currentRules);
        }

        function updateRule(ruleId, key, value) {
            const rule = _currentRules.find(r => r.id === ruleId);
            if (!rule) return;
            rule[key] = value;
            const row = document.getElementById(`rulerow-${ruleId}`);
            if (row) row.classList.toggle('disabled', !rule.enabled);
        }

        function getCurrentBaseMapping() {
            const fields = ['chassis_number','latitude','longitude','speed','date','code',
                            'ignition','temperature','odometer','battery','altitude','course',
                            'humidity','serial_number','shipment','vehicle_type','vehicle_brand','vehicle_model'];
            const mapping = {};
            fields.forEach(f => {
                const el = document.getElementById(`map_${f}`);
                if (!el) return;
                const val = el.value?.trim();
                // Bloqueo de seguridad: Evitar guardar el string visual "⚡ X Reglas"
                if (f === 'code' && val && val.startsWith('⚡')) return;
                if (val) mapping[f] = val;
            });
            return mapping;
        }

        // --- Logica de Diccionarios (Enriquecimiento) ---
        function toggleEnrichment() {
            const isEnabled = document.getElementById('enableEnrichment').checked;
            document.getElementById('enrichmentPanel').style.display = isEnabled ? 'block' : 'none';
        }
        
        async function loadEnrichment(name, env) {
            try {
                const res = await fetch(`${API_BASE}/${name}/${env}/enrichment`);
                const data = await res.json();
                
                if (data && (data.url || data.timezone_offset !== undefined)) {
                    document.getElementById('enableEnrichment').checked = data.enabled || false;
                    toggleEnrichment();
                    document.getElementById('timezoneOffset').value = data.timezone_offset !== undefined ? data.timezone_offset : 0;
                    document.getElementById('dictUrl').value = data.url || "";
                    document.getElementById('dictMethod').value = data.method || "GET";
                    document.getElementById('dictFrequency').value = data.frequency || 24;
                    document.getElementById('dictKeyPath').value = data.key_path || "";
                    document.getElementById('dictValuePath').value = data.value_path || "";
                    document.getElementById('dictAuthType').value = data.auth_type || "none";
                    document.getElementById('dictAuthUser').value = data.auth_user || "";
                    document.getElementById('dictAuthPass').value = data.auth_pass || "";
                    document.getElementById('dictAuthFields').style.display = (data.auth_type && data.auth_type !== 'none') ? 'block' : 'none';
                } else {
                    document.getElementById('enableEnrichment').checked = false;
                    toggleEnrichment();
                    document.getElementById('timezoneOffset').value = 0;
                    document.getElementById('dictUrl').value = "";
                    document.getElementById('dictKeyPath').value = "";
                    document.getElementById('dictValuePath').value = "";
                    document.getElementById('dictAuthType').value = "none";
                    document.getElementById('dictAuthUser').value = "";
                    document.getElementById('dictAuthPass').value = "";
                    document.getElementById('dictAuthFields').style.display = 'none';
                }
            } catch (e) {
                console.error("Error loading enrichment", e);
            }
        }
        
        async function saveEnrichment(event) {
            if(!currentIpaasProvider) {
                alert("Selecciona un proveedor primero.");
                return;
            }
            
            let btn = null;
            let originalText = "💾 Guardar Diccionario";
            if (event && event.target) {
                btn = event.target;
                originalText = btn.innerText;
                btn.innerText = "⏳ Guardando...";
                btn.style.opacity = "0.7";
            }
            
            const payload = {
                enabled: document.getElementById('enableEnrichment').checked,
                timezone_offset: parseInt(document.getElementById('timezoneOffset').value) || 0,
                url: document.getElementById('dictUrl').value.trim(),
                method: document.getElementById('dictMethod').value,
                frequency: parseInt(document.getElementById('dictFrequency').value) || 24,
                key_path: document.getElementById('dictKeyPath').value.trim(),
                value_path: document.getElementById('dictValuePath').value.trim(),
                auth_type: document.getElementById('dictAuthType').value,
                auth_user: document.getElementById('dictAuthUser').value.trim(),
                auth_pass: document.getElementById('dictAuthPass').value.trim()
            };
            
            try {
                const res = await fetch(`${API_BASE}/${currentIpaasProvider.name}/${currentIpaasProvider.env}/enrichment`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if(data.status === 'success') {
                    if (btn) {
                        btn.innerText = "✅ Guardado!";
                        btn.style.opacity = "1";
                        btn.style.background = "#10B981";
                        setTimeout(() => {
                            btn.innerText = originalText;
                            btn.style.background = "var(--color-green-vibrant)";
                        }, 2500);
                    }
                } else {
                    if(btn) { btn.innerText = originalText; btn.style.opacity = "1"; }
                    alert("Error al guardar: " + data.message);
                }
            } catch(e) {
                if(btn) { btn.innerText = originalText; btn.style.opacity = "1"; }
                alert("Error de red.");
            }
        }

        function switchInspectorTab(tabId) {
            document.getElementById('inspector-tab-push').style.display = tabId === 'push' ? 'block' : 'none';
            document.getElementById('inspector-tab-manual').style.display = tabId === 'manual' ? 'block' : 'none';
            document.getElementById('itab-push').style.background = tabId === 'push' ? 'var(--color-green-vibrant)' : 'transparent';
            document.getElementById('itab-push').style.color = tabId === 'push' ? 'white' : 'var(--color-gray)';
            document.getElementById('itab-push').style.border = tabId === 'push' ? 'none' : '1px solid var(--card-border)';
            document.getElementById('itab-manual').style.background = tabId === 'manual' ? 'var(--color-green-vibrant)' : 'transparent';
            document.getElementById('itab-manual').style.color = tabId === 'manual' ? 'white' : 'var(--color-gray)';
            document.getElementById('itab-manual').style.border = tabId === 'manual' ? 'none' : '1px solid var(--card-border)';
        }

        function startListening() {
            const btn = document.getElementById('btnListen');
            const status = document.getElementById('listenStatus');
            if(pollingInterval) {
                clearInterval(pollingInterval);
                pollingInterval = null;
                btn.innerText = "Comenzar a Escuchar (Polling)";
                status.innerText = "";
                return;
            }
            btn.innerText = "⏹ Detener Escucha";
            status.innerText = "Esperando petición POST en la URL temporal...";
            pollingInterval = setInterval(async () => {
                try {
                    const res = await fetch(`/inspector/catch/${sessionUuid}/latest`);
                    const data = await res.json();
                    if(data.has_data) {
                        showPayload(data.data.payload);
                        clearInterval(pollingInterval);
                        pollingInterval = null;
                        btn.innerText = "Comenzar a Escuchar (Polling)";
                        status.innerText = "¡Payload Recibido Exitosamente!";
                        status.style.color = "var(--color-green-vibrant)";
                    }
                } catch(e) {}
            }, 2000);
        }

        // --- Variables de Estado para el Click-to-Map ---
        let activeMappingInput = null;

        document.addEventListener('DOMContentLoaded', () => {
            // Inicializar listeners en los inputs del motor de mapeo
            const mapInputs = document.querySelectorAll('.mapping-row input.form-control');
            mapInputs.forEach(input => {
                input.addEventListener('focus', function() {
                    // Quitar la clase a todos
                    mapInputs.forEach(i => i.classList.remove('mapper-input-active'));
                    // Asignarla al seleccionado
                    this.classList.add('mapper-input-active');
                    activeMappingInput = this;
                });
            });
            
            // Si el usuario hace clic fuera de todo, podríamos quitar la clase, pero es mejor
            // dejarlo seleccionado para que sea mas comodo.
        });

        // --- Modo Manual (Pegar JSON) ---
        function loadManualJson() {
            const rawJson = document.getElementById('manualJsonInput').value.trim();
            if (!rawJson) {
                alert("Por favor, pega un JSON primero.");
                return;
            }
            try {
                const parsed = JSON.parse(rawJson);
                showPayload(parsed);
                // Vaciar la caja después de cargar por limpieza
                document.getElementById('manualJsonInput').value = '';
            } catch (e) {
                alert("Error: El texto ingresado no es un formato JSON válido.");
            }
        }

        function showPayload(data) {
            document.getElementById('payloadResult').style.display = 'block';
            const container = document.getElementById('jsonTreeContainer');
            container.innerHTML = ''; // Limpiar anterior
            
            const ul = document.createElement('ul');
            ul.className = 'json-tree';
            buildTree(data, '', ul);
            container.appendChild(ul);
        }

        function buildTree(obj, currentPath, parentElement) {
            if (typeof obj !== 'object' || obj === null) return;

            const keys = Object.keys(obj);
            keys.forEach(key => {
                const val = obj[key];
                const newPath = currentPath ? `${currentPath}.${key}` : key;
                
                const li = document.createElement('li');
                li.className = 'json-node';

                const keySpan = document.createElement('span');
                keySpan.className = 'json-key';
                keySpan.textContent = `"${key}": `;
                li.appendChild(keySpan);

                if (typeof val === 'object' && val !== null) {
                    const typeSpan = document.createElement('span');
                    typeSpan.style.color = 'var(--color-gray)';
                    typeSpan.textContent = Array.isArray(val) ? '[ ... ]' : '{ ... }';
                    li.appendChild(typeSpan);
                    
                    const subUl = document.createElement('ul');
                    subUl.className = 'json-tree';
                    buildTree(val, newPath, subUl);
                    li.appendChild(subUl);
                } else {
                    const valSpan = document.createElement('span');
                    valSpan.className = 'json-value';
                    valSpan.textContent = typeof val === 'string' ? `"${val}"` : String(val);
                    
                    const clickableWrapper = document.createElement('span');
                    clickableWrapper.className = 'json-clickable';
                    clickableWrapper.appendChild(valSpan);
                    clickableWrapper.title = `Clic para mapear: ${newPath}`;
                    clickableWrapper.onclick = (e) => {
                        e.stopPropagation();
                        injectMapping(newPath);
                    };
                    
                    li.appendChild(clickableWrapper);
                }
                parentElement.appendChild(li);
            });
        }

        function injectMapping(path) {
            if (!activeMappingInput) {
                alert("💡 Primero haz clic en una de las cajas de la derecha (Ej. Latitud) para decirme dónde rellenar este dato.");
                return;
            }
            activeMappingInput.value = path;
            
            // Animación de flash (Trigger reflow para reiniciar animacion)
            activeMappingInput.classList.remove('mapper-flash');
            void activeMappingInput.offsetWidth; 
            activeMappingInput.classList.add('mapper-flash');
        }

        async function createNewProvider() {
            const name = prompt("Ingrese el nombre del nuevo proveedor (Ej. geotab, samsara):");
            if(!name) return;
            
            try {
                const res = await fetch('/api/config/providers', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({provider_name: name})
                });
                const data = await res.json();
                if(data.status === 'success') {
                    alert("Proveedor creado exitosamente.");
                    await loadProviders();
                    if (document.getElementById('view-config').style.display === 'flex') {
                        loadConfig();
                    } else {
                        switchView('integrations');
                    }
                } else {
                    alert("Error: " + data.message);
                }
            } catch(e) {
                alert("Error de red");
            }
        }

        function viewRawJson(rawJsonStr, title = "Payload Original") {
            try {
                // El raw_data llega como string desde la DB (sqlite lo guarda como text)
                const parsed = typeof rawJsonStr === 'string' ? JSON.parse(rawJsonStr) : rawJsonStr;
                const pretty = JSON.stringify(parsed, null, 4);
                
                // Crear modal on the fly
                const overlay = document.createElement('div');
                overlay.style.position = 'fixed';
                overlay.style.top = '0'; overlay.style.left = '0'; overlay.style.width = '100vw'; overlay.style.height = '100vh';
                overlay.style.backgroundColor = 'rgba(0,0,0,0.8)';
                overlay.style.zIndex = '9999';
                overlay.style.display = 'flex';
                overlay.style.alignItems = 'center';
                overlay.style.justifyContent = 'center';
                
                const modal = document.createElement('div');
                modal.style.backgroundColor = 'var(--color-black)';
                modal.style.padding = '20px';
                modal.style.borderRadius = '8px';
                modal.style.border = '1px solid var(--color-gray-dark)';
                modal.style.width = '600px';
                modal.style.maxWidth = '90vw';
                
                modal.innerHTML = `
                    <h3 style="margin-top:0; color: white;">${title}</h3>
                    <textarea id="json-copy-area" readonly style="width: 100%; height: 300px; background: #111; color: #0f0; font-family: monospace; border: none; padding: 10px; margin-bottom: 10px;">${pretty}</textarea>
                    <div style="display: flex; justify-content: flex-end; gap: 10px;">
                        <button id="btn-copy-json" style="background: var(--color-green-bright); color: black; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; font-weight: bold;">Copiar</button>
                        <button id="btn-close-json" style="background: #ef4444; color: white; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; font-weight: bold;">Cerrar</button>
                    </div>
                `;
                
                overlay.appendChild(modal);
                document.body.appendChild(overlay);
                
                document.getElementById('btn-close-json').onclick = () => overlay.remove();
                document.getElementById('btn-copy-json').onclick = () => {
                    const txt = document.getElementById('json-copy-area');
                    txt.select();
                    document.execCommand('copy');
                    alert('¡Copiado al portapapeles!');
                };
            } catch (e) {
                alert("Error parseando JSON: " + e.message);
            }
        }
        function toggleCompactMode(enabled) {
            if (enabled) {
                document.body.classList.add('compact-mode');
                localStorage.setItem('compactMode', 'true');
            } else {
                document.body.classList.remove('compact-mode');
                localStorage.setItem('compactMode', 'false');
            }
        }

        document.addEventListener('DOMContentLoaded', () => {
            const isCompact = localStorage.getItem('compactMode') === 'true';
            const toggleEl = document.getElementById('compact-mode-toggle');
            if (toggleEl) toggleEl.checked = isCompact;
            if (isCompact) document.body.classList.add('compact-mode');
        });

        async function loadRetentionConfig() {
            try {
                const res = await fetch('/api/config/retention');
                if(res.ok) {
                    const data = await res.json();
                    document.getElementById('audit-retention-select').value = data.audit_retention_days || 30;
                    document.getElementById('processed-retention-select').value = data.processed_retention_days || 30;
                    document.getElementById('processed-logs-toggle').checked = data.processed_logs_enabled;
                }
            } catch(e) { console.error("Error loading retention config", e); }
        }

        async function saveRetentionConfig() {
            const audit_days = parseInt(document.getElementById('audit-retention-select').value);
            const processed_days = parseInt(document.getElementById('processed-retention-select').value);
            try {
                const res = await fetch('/api/config/retention', {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ audit_retention_days: audit_days, processed_retention_days: processed_days })
                });
                if(res.ok) {
                    alert('Retención actualizada correctamente');
                } else {
                    const err = await res.json();
                    alert('Error: ' + err.detail);
                }
            } catch(e) {
                alert('Error de conexión al guardar retención');
            }
        }

        async function toggleProcessedLogs(enabled) {
            try {
                const res = await fetch('/api/config/processed-logs-toggle', {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ enabled: enabled })
                });
                if(res.ok) {
                    const data = await res.json();
                    alert(data.message);
                } else {
                    const err = await res.json();
                    alert('Error: ' + err.detail);
                    // Revert toggle
                    document.getElementById('processed-logs-toggle').checked = !enabled;
                }
            } catch(e) {
                alert('Error de conexión al cambiar el switch');
                document.getElementById('processed-logs-toggle').checked = !enabled;
            }
        }

        function validatePurgeForm() {
            const confirmText = document.getElementById('purge-confirm-text').value;
            const password = document.getElementById('purge-password').value;
            const btn = document.getElementById('btn-purge-now');
            
            if(confirmText === 'PURGAR' && password.trim() !== '') {
                btn.disabled = false;
                btn.style.opacity = '1';
                btn.style.cursor = 'pointer';
            } else {
                btn.disabled = true;
                btn.style.opacity = '0.5';
                btn.style.cursor = 'not-allowed';
            }
        }

        async function executePurge() {
            const category = document.querySelector('input[name="purge-category"]:checked').value;
            const days = parseInt(document.getElementById('purge-days').value);
            const confirmText = document.getElementById('purge-confirm-text').value;
            const password = document.getElementById('purge-password').value;
            
            if(days < 7) {
                alert('El mínimo de días es 7');
                return;
            }
            
            if(!window.confirm(`⚠️ Esto borrará permanentemente los logs anteriores a ${days} días.\n\n¿Continuar?`)) {
                return;
            }
            
            try {
                const res = await fetch('/api/config/purge-logs', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        category: category,
                        days: days,
                        confirm_text: confirmText,
                        admin_password: password
                    })
                });
                
                if(res.ok) {
                    const data = await res.json();
                    const mbytes = (data.freed_bytes / (1024*1024)).toFixed(2);
                    alert(`Purgados ${data.deleted_files} archivos, ${mbytes} MB liberados`);
                    
                    const auditLog = document.getElementById('purge-audit-log');
                    const now = new Date().toLocaleString();
                    auditLog.innerHTML = `Última purga: ${now} | Categoría: ${category} | Archivos: ${data.deleted_files} | Liberado: ${mbytes} MB`;
                    auditLog.style.display = 'block';
                    
                    document.getElementById('purge-confirm-text').value = '';
                    document.getElementById('purge-password').value = '';
                    validatePurgeForm();
                } else {
                    const err = await res.json();
                    alert('Error: ' + err.detail);
                }
            } catch(e) {
                alert('Error de conexión en purga');
            }
        }
