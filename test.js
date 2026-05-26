
        let currentConfigs = [];
        let statsInterval;

        // Vistas
        function switchView(view) {
            document.getElementById('view-dashboard').style.display = view === 'dashboard' ? 'flex' : 'none';
            document.getElementById('view-config').style.display = view === 'config' ? 'flex' : 'none';
            document.getElementById('view-simulator').style.display = view === 'simulator' ? 'flex' : 'none';
            document.getElementById('view-logs').style.display = view === 'logs' ? 'flex' : 'none';
            
            document.getElementById('tab-dashboard').classList.toggle('active-tab', view === 'dashboard');
            document.getElementById('tab-config').classList.toggle('active-tab', view === 'config');
            document.getElementById('tab-simulator').classList.toggle('active-tab', view === 'simulator');
            document.getElementById('tab-logs').classList.toggle('active-tab', view === 'logs');
            
            toggleMenu(); // Cierra el menú al elegir

            clearInterval(statsInterval);
            
            if (view === 'config') {
                loadConfig();
            } else if (view === 'simulator') {
                loadSimulator();
            } else if (view === 'logs') {
                loadLogs();
            } else {
                fetchStats();
                statsInterval = setInterval(fetchStats, 2000);
            }
        }

        // Dashboard Stats
        async function fetchStats() {
            try {
                const response = await fetch('/api/stats');
                if (!response.ok) throw new Error('Network response was not ok');
                const data = await response.json();
                
                updateValue('val-pending', data.pending);
                updateValue('val-sent', data.sent);
                updateValue('val-failed', data.failed);

                allRecentEvents = data.recent;
                
                // Populate provider dropdown dynamically
                const dropdown = document.getElementById('filter-provider');
                const existingOptions = Array.from(dropdown.options).map(o => o.value);
                const uniqueProviders = [...new Set(data.recent.map(e => e.provider.toLowerCase()))];
                uniqueProviders.forEach(p => {
                    if (!existingOptions.includes(p)) {
                        const opt = document.createElement('option');
                        opt.value = p;
                        opt.innerText = p.toUpperCase();
                        dropdown.appendChild(opt);
                    }
                });

                renderRecentTable();

                document.getElementById('sync-status').textContent = 'Conectado y Escuchando';
                document.querySelector('.pulse').style.backgroundColor = 'var(--color-green-bright)';
            } catch (error) {
                console.error('Error fetching stats:', error);
                document.getElementById('sync-status').textContent = 'Pérdida de Conexión';
                document.querySelector('.pulse').style.backgroundColor = '#EF4444';
            }
        }

        function updateValue(elementId, newValue) {
            const el = document.getElementById(elementId);
            if (el.textContent !== String(newValue)) {
                el.textContent = newValue;
                el.classList.remove('value-update');
                void el.offsetWidth;
                el.classList.add('value-update');
            }
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
            
            let labelText = "";
            if (currentStatusFilter !== 'all' || currentProviderFilter !== 'all') {
                labelText = `(Filtrado: ${currentStatusFilter !== 'all' ? currentStatusFilter.toUpperCase() : ''} ${currentProviderFilter !== 'all' ? currentProviderFilter.toUpperCase() : ''})`;
            }
            document.getElementById('current-filter-label').innerText = labelText;

            if (filtered.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color: var(--color-gray)">No hay eventos con estos filtros.</td></tr>';
                return;
            }

            filtered.forEach(ev => {
                const tr = document.createElement('tr');
                
                let statusClass = 'pending';
                let statusText = 'En Cola';
                if (ev.status === 'sent') { statusClass = 'sent'; statusText = 'Enviado'; }
                if (ev.status === 'failed') { statusClass = 'failed'; statusText = 'Error'; }

                let badgeClass = 'pending';
                if (ev.env.toLowerCase() === 'prod') badgeClass = 'sent';
                if (ev.env.toLowerCase() === 'test') badgeClass = 'pending';

                let rawHtml = `<pre style="font-size:0.7rem; background:rgba(0,0,0,0.5); padding:10px; border-radius:4px; max-height:150px; overflow-y:auto; color:var(--color-green-bright); margin-top:5px; white-space:pre-wrap;">${JSON.stringify(ev.rc_format, null, 2)}</pre>`;

                let locHtml = `<div style="font-size:0.8rem; color:var(--color-gray); line-height: 1.4;">`;
                locHtml += `<div>Fecha GPS: <span style="color:var(--color-white); font-weight:bold;">${ev.device_date}</span></div>`;
                locHtml += `<div>Coords: <span style="color:var(--color-white)">${ev.coords}</span></div>`;
                locHtml += `<div>Dir: <span style="color:var(--color-white)">${ev.course !== null ? ev.course + '°' : 'N/A'}</span> | Alt: <span style="color:var(--color-white)">${ev.altitude !== null ? ev.altitude + 'm' : 'N/A'}</span></div>`;
                locHtml += `</div>`;

                let sensorHtml = `<div style="font-size:0.8rem; color:var(--color-gray); line-height: 1.4;">`;
                sensorHtml += `<div>Velocidad: <span style="color:var(--color-white)">${ev.speed} km/h</span></div>`;
                sensorHtml += `<div>Ignición: <span style="${ev.ignition==='ON'?'color:var(--color-green-bright); font-weight:bold;':'color:var(--color-red)'}">${ev.ignition}</span></div>`;
                sensorHtml += `<div>Batería: <span style="color:var(--color-white)">${ev.battery !== null ? ev.battery + '%' : 'N/A'}</span> | Temp: <span style="color:var(--color-white)">${ev.temperature !== null ? ev.temperature + '°' : 'N/A'}</span></div>`;
                sensorHtml += `</div>`;

                let opHtml = `<div style="font-size:0.8rem; color:var(--color-gray); line-height: 1.4;">`;
                opHtml += `<div>Job ID: <span style="color:var(--color-green-bright); font-weight:bold;">${ev.job_id || 'N/A'}</span></div>`;
                opHtml += `<div>Código EV: <span style="color:var(--color-yellow)">${ev.code}</span></div>`;
                opHtml += `<div>Odom: <span style="color:var(--color-white)">${ev.odometer !== null ? ev.odometer : 'N/A'}</span></div>`;
                
                // Exportador JSON (Usa formato exacto de RC)
                const jsonPayload = encodeURIComponent(JSON.stringify(ev.rc_format, null, 2));
                const safeTime = ev.time.replace(/[: ()]/g, '');
                opHtml += `<div style="margin-top: 6px;"><a href="data:text/json;charset=utf-8,${jsonPayload}" download="evento_RC_${ev.chassis}_${safeTime}.json" style="color: var(--color-green-bright); text-decoration: none; font-size: 0.75rem; border: 1px solid var(--color-green-bright); padding: 2px 6px; border-radius: 4px; display: inline-block; transition: all 0.2s;" title="Descargar Payload formato RC">📥 JSON RC</a></div>`;
                opHtml += `</div>`;
                
                let statusHtml = `<span class="badge ${statusClass}">${statusText.toUpperCase()}</span><br>`;
                statusHtml += `<span style="font-size:0.75rem; color:var(--color-gray); display:inline-block; margin-top:4px;">${ev.time}</span>`;
                
                tr.innerHTML = `
                    <td>
                        <span style="font-weight:bold; color:var(--color-white)">${ev.provider.toUpperCase()}</span><br>
                        <span class="badge ${badgeClass}" style="margin-top:5px">${ev.env.toUpperCase()}</span>
                    </td>
                    <td>
                        <span style="color:var(--color-yellow); font-weight:bold;">${ev.chassis}</span><br>
                        <button onclick="toggleRaw(this)" style="margin-top:5px; background:none; border:1px solid var(--color-gray); color:var(--color-white); border-radius:4px; font-size:0.7rem; cursor:pointer; padding:2px 5px;">Ver JSON RC</button>
                        <div class="raw-data-row" style="display:none; margin-top:10px;">${rawHtml}</div>
                    </td>
                    <td style="white-space: nowrap;">${statusHtml}</td>
                    <td>${locHtml}</td>
                    <td>${sensorHtml}</td>
                    <td>${opHtml}</td>
                `;
                tbody.appendChild(tr);
            });
        }

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
                            providerCell = `<td rowspan="${envs.length}" style="font-weight:bold; color:var(--color-white); vertical-align: middle;">${c.provider_name}</td>`;
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
                            <td><input class="form-control" type="text" id="user_${c._originalIdx}" value="${c.rc_user}"></td>
                            <td><input class="form-control" type="password" id="pass_${c._originalIdx}" value="${c.rc_password}"></td>
                            <td><input class="form-control" type="number" id="purge_${c._originalIdx}" value="${c.purge_interval_min}" style="width: 80px;"></td>
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
                rc_user: document.getElementById(`user_${idx}`).value,
                rc_password: document.getElementById(`pass_${idx}`).value,
                purge_interval_min: parseInt(document.getElementById(`purge_${idx}`).value) || 15
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

        // Inicio
        fetchStats();
        statsInterval = setInterval(fetchStats, 2000);
    