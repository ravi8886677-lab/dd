        // State
        let currentTab = 'chat';
        let selectedTopics = new Set();
        let searchQuery = '';
        let diaryImportDone = false;
        let fromDate = '';
        let toDate = '';
        let searchDebounce = null;

        // Non-deletable preset node IDs. Loaded from /api/graph/presets on
        // boot so the JS side never drifts from FIXED_BRANCHES in graph.py.
        // Seeded with 'root' so the delete button stays hidden if the fetch
        // hasn't completed yet (fail-closed).
        let PRESET_NODE_IDS = new Set(['root']);

        // DOM Elements
        const searchInput = document.getElementById('search-input');
        const fromDateInput = document.getElementById('from-date');
        const toDateInput = document.getElementById('to-date');
        const mealsFromDateInput = document.getElementById('meals-from-date');
        const mealsToDateInput = document.getElementById('meals-to-date');
        const topicsCloud = document.getElementById('topics-cloud');
        const connPane = document.getElementById('connections-content');
        const settingsPane = document.getElementById('settings-content');
        const chatPane = document.getElementById('chat-content');
        const chatLog = document.getElementById('chat-log');
        const chatInput = document.getElementById('chat-input');
        const memoriesPane = document.getElementById('memories-content');
        const mealsPane = document.getElementById('meals-content');
        const graphContent = document.getElementById('graph-content');
        const memoriesContent = memoriesPane.querySelector('.memory-list');
        const mealsContent = mealsPane.querySelector('.memory-list');
        const tabs = document.querySelectorAll('.tab');

        // Shared utilities
        function escapeHtml(str) {
            if (!str) return '';
            return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                      .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
        }

        // API calls
        async function fetchMemories() {
            const params = new URLSearchParams();
            if (searchQuery) params.set('search', searchQuery);
            if (selectedTopics.size > 0) params.set('topic', Array.from(selectedTopics).join(','));
            if (fromDate) params.set('from_date', fromDate);
            if (toDate) params.set('to_date', toDate);

            const response = await fetch('/api/memories?' + params);
            return response.json();
        }

        async function fetchTopics() {
            const response = await fetch('/api/topics');
            return response.json();
        }

        async function fetchMeals() {
            const params = new URLSearchParams();
            if (fromDate) params.set('from_date', fromDate);
            if (toDate) params.set('to_date', toDate);

            const response = await fetch('/api/meals?' + params);
            return response.json();
        }

        async function fetchStats() {
            const response = await fetch('/api/stats');
            return response.json();
        }

        async function deleteMemory(id) {
            const response = await fetch('/api/memory/' + id, { method: 'DELETE' });
            return response.json();
        }

        async function deleteMeal(id) {
            const response = await fetch('/api/meal/' + id, { method: 'DELETE' });
            return response.json();
        }

        // Render functions
        function renderTopics(topics) {
            if (!topics.length) {
                topicsCloud.innerHTML = '<div class="empty-state"><p>No topics yet</p></div>';
                return;
            }

            topicsCloud.innerHTML = topics.map(topic => `
                <button class="topic-tag ${selectedTopics.has(topic.name) ? 'active' : ''}"
                        data-topic="${escapeHtml(topic.name)}">
                    ${escapeHtml(topic.name)}
                    <span class="topic-count">${topic.count}</span>
                </button>
            `).join('');

            // Add click handlers
            topicsCloud.querySelectorAll('.topic-tag').forEach(tag => {
                tag.addEventListener('click', () => {
                    const topic = tag.dataset.topic;
                    if (selectedTopics.has(topic)) {
                        selectedTopics.delete(topic);
                    } else {
                        selectedTopics.add(topic);
                    }
                    renderTopics(topics);
                    loadMemories();
                });
            });
        }

        function formatDate(dateStr) {
            const date = new Date(dateStr + 'T00:00:00');
            const now = new Date();
            const diff = Math.floor((now - date) / (1000 * 60 * 60 * 24));

            if (diff === 0) return 'Today';
            if (diff === 1) return 'Yesterday';
            if (diff < 7) return `${diff} days ago`;

            return date.toLocaleDateString('en-US', {
                weekday: 'short',
                month: 'short',
                day: 'numeric',
                year: date.getFullYear() !== now.getFullYear() ? 'numeric' : undefined
            });
        }

        function renderMemories(memories) {
            if (!memories.length) {
                memoriesContent.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-icon">🌙</div>
                        <div class="empty-title">No memories found</div>
                        <p>Try adjusting your search or filters</p>
                    </div>
                `;
                return;
            }

            memoriesContent.innerHTML = memories.map(memory => `
                <article class="memory-card" data-id="${memory.id}">
                    <div class="memory-header">
                        <div class="memory-date">
                            <span>📅</span>
                            ${formatDate(memory.date_utc)}
                        </div>
                        <div class="memory-actions">
                            <button class="action-btn delete" title="Delete memory">🗑️</button>
                        </div>
                    </div>
                    <p class="memory-summary">${escapeHtml(memory.summary)}</p>
                    ${memory.topics_list.length ? `
                        <div class="memory-topics">
                            ${memory.topics_list.map(t => `<span class="memory-topic">${escapeHtml(t)}</span>`).join('')}
                        </div>
                    ` : ''}
                </article>
            `).join('');

            // Add delete handlers
            memoriesContent.querySelectorAll('.action-btn.delete').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    const card = e.target.closest('.memory-card');
                    const id = card.dataset.id;

                    if (confirm('Delete this memory?')) {
                        const result = await deleteMemory(id);
                        if (result.success) {
                            card.remove();
                            showToast('Memory deleted', 'success');
                            loadStats();

        // ── YOLO mode ────────────────────────────────────────────────
        // The window is granted here, by a person clicking, and nowhere
        // else. Jarvis reads web pages and tool descriptions written by
        // other people; if a tool could grant, that text could grant.
        let yoloTimer = null;

        async function refreshYolo() {
            try {
                const res = await fetch('/api/yolo');
                const state = await res.json();
                renderYolo(state);
            } catch (e) {
                // A dashboard that cannot read the state should not claim
                // the window is open.
                renderYolo({ active: false, label: 'unavailable', choices: [15, 30] });
            }
        }

        function describeDuration(minutes) {
            // Mirrors approval.describe_duration: "480 min" is not a
            // thing anyone says, and the slider reaches eight hours.
            const total = Math.round(minutes);
            if (total < 60) return total + ' min';
            const hours = Math.floor(total / 60);
            const rest = total % 60;
            return rest === 0 ? hours + 'h' : hours + 'h ' + rest + 'm';
        }

        function renderYolo(state) {
            const bar = document.querySelector('.yolo-bar');
            const label = document.getElementById('yolo-label');
            const slider = document.getElementById('yolo-slider');
            const duration = document.getElementById('yolo-duration');
            const start = document.getElementById('yolo-start');
            const stop = document.getElementById('yolo-stop');
            if (!bar || !label || !slider) return;

            if (state.min_minutes) slider.min = state.min_minutes;
            if (state.max_minutes) slider.max = state.max_minutes;
            if (state.step_minutes) slider.step = state.step_minutes;
            duration.textContent = describeDuration(Number(slider.value));

            bar.classList.toggle('on', !!state.active);
            label.textContent = state.active
                ? 'YOLO mode: on — ' + state.label
                : 'YOLO mode: off — risky actions will ask first';

            start.textContent = state.active ? '↻ Restart' : '🚀 Start';
            stop.style.display = state.active ? '' : 'none';

            if (yoloTimer) clearInterval(yoloTimer);
            if (state.active) {
                // Keep the countdown honest without polling constantly.
                yoloTimer = setInterval(refreshYolo, 5000);
            }
        }

        async function setYolo(body) {
            try {
                const res = await fetch('/api/yolo', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                renderYolo(await res.json());
            } catch (e) {
                console.error('could not change YOLO mode', e);
            }
            document.getElementById('yolo-slider')?.addEventListener('input', (e) => {
            document.getElementById('yolo-duration').textContent =
                describeDuration(Number(e.target.value));
        });
        document.getElementById('yolo-start')?.addEventListener('click', () => {
            setYolo({ minutes: Number(document.getElementById('yolo-slider').value) });
        });
        document.getElementById('yolo-stop')?.addEventListener('click', () => {
            setYolo({ off: true });
        });

        refreshYolo();
        }

        refreshYolo();

                        } else {
                            showToast('Failed to delete', 'error');
                        }
                    }
                });
            });
        }

        function renderMeals(meals) {
            if (!meals.length) {
                mealsContent.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-icon">🍽️</div>
                        <div class="empty-title">No meals logged</div>
                        <p>Meal tracking data will appear here</p>
                    </div>
                `;
                return;
            }

            mealsContent.innerHTML = meals.map(meal => `
                <div class="meal-card" data-id="${meal.id}">
                    <div class="meal-info">
                        <div class="meal-header">
                            <h3>${meal.description}</h3>
                            <button class="action-btn delete meal-delete" title="Delete meal">🗑️</button>
                        </div>
                        <div class="meal-time">${new Date(meal.ts_utc).toLocaleString()}</div>
                    </div>
                    <div class="meal-macros">
                        ${meal.calories_kcal ? `
                            <div class="macro">
                                <div class="macro-value">${Math.round(meal.calories_kcal)}</div>
                                <div class="macro-label">kcal</div>
                            </div>
                        ` : ''}
                        ${meal.protein_g ? `
                            <div class="macro">
                                <div class="macro-value">${Math.round(meal.protein_g)}g</div>
                                <div class="macro-label">protein</div>
                            </div>
                        ` : ''}
                        ${meal.carbs_g ? `
                            <div class="macro">
                                <div class="macro-value">${Math.round(meal.carbs_g)}g</div>
                                <div class="macro-label">carbs</div>
                            </div>
                        ` : ''}
                        ${meal.fat_g ? `
                            <div class="macro">
                                <div class="macro-value">${Math.round(meal.fat_g)}g</div>
                                <div class="macro-label">fat</div>
                            </div>
                        ` : ''}
                    </div>
                </div>
            `).join('');

            // Add delete handlers for meals
            mealsContent.querySelectorAll('.meal-delete').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    const card = e.target.closest('.meal-card');
                    const id = card.dataset.id;

                    if (confirm('Delete this meal?')) {
                        const result = await deleteMeal(id);
                        if (result.success) {
                            card.remove();
                            showToast('Meal deleted', 'success');
                            loadStats();
                        } else {
                            showToast('Failed to delete meal', 'error');
                        }
                    }
                });
            });
        }

        function showToast(message, type = 'success') {
            const toast = document.createElement('div');
            toast.className = `toast ${type}`;
            toast.innerHTML = `
                <span>${type === 'success' ? '✅' : '❌'}</span>
                <span>${message}</span>
            `;
            document.body.appendChild(toast);

            setTimeout(() => toast.remove(), 3000);
        }

        // Load data
        async function loadMemories() {
            memoriesContent.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            try {
                const { memories } = await fetchMemories();
                renderMemories(memories);
            } catch (e) {
                memoriesContent.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">Failed to load memories</div></div>';
            }
        }

        async function loadMeals() {
            mealsContent.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            try {
                const { meals } = await fetchMeals();
                renderMeals(meals);
            } catch (e) {
                mealsContent.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">Failed to load meals</div></div>';
            }
        }

        async function loadTopics() {
            try {
                const { topics } = await fetchTopics();
                renderTopics(topics);
            } catch (e) {
                topicsCloud.innerHTML = '<div class="empty-state"><p>Failed to load topics</p></div>';
            }
        }

        // Which machine this window is reading from. With more than one
        // device on the account it is the only thing distinguishing this
        // view from another machine's.
        async function loadIdentity() {
            const badge = document.getElementById('identity-device');
            if (!badge) return;
            try {
                const identity = await (await fetch('/api/identity')).json();
                if (!identity || !identity.device) return;
                document.getElementById('identity-device-name').textContent = identity.device.name;
                const others = (identity.devices || []).length - 1;
                const workspace = identity.workspace ? identity.workspace.name : '';
                badge.title = others > 0
                    ? workspace + ' workspace · ' + others + ' other device' + (others === 1 ? '' : 's')
                    : workspace + ' workspace · this is your only device';
            } catch (e) {}
        }

        async function loadStats() {
            let totalMemories = 0;
            let totalTokens = 0;

            try {
                const stats = await fetchStats();
                totalMemories = stats.total_memories || 0;
                document.getElementById('stats-memories').textContent = totalMemories;
                document.getElementById('stats-meals').textContent = stats.total_meals || 0;
            } catch (e) {}

            // Load graph stats separately
            try {
                const graphStats = await (await fetch('/api/graph/stats')).json();
                totalTokens = graphStats.total_tokens || 0;
                document.getElementById('stats-nodes').textContent = graphStats.total_nodes || 0;
            } catch (e) {}

            // First-time migration: offer to import diary entries if the graph
            // holds no knowledge yet but the user has diary data.
            if (totalTokens === 0 && totalMemories > 0 && !diaryImportDone) {
                showImportDiaryModal(true);
            }
        }

        // Event handlers
        searchInput.addEventListener('input', (e) => {
            clearTimeout(searchDebounce);
            searchDebounce = setTimeout(() => {
                searchQuery = e.target.value.trim();
                loadMemories();
            }, 300);
        });

        fromDateInput.addEventListener('change', (e) => {
            fromDate = e.target.value;
            mealsFromDateInput.value = fromDate;
            loadMemories();
        });

        toDateInput.addEventListener('change', (e) => {
            toDate = e.target.value;
            mealsToDateInput.value = toDate;
            loadMemories();
        });

        mealsFromDateInput.addEventListener('change', (e) => {
            fromDate = e.target.value;
            fromDateInput.value = fromDate;
            loadMeals();
        });

        mealsToDateInput.addEventListener('change', (e) => {
            toDate = e.target.value;
            toDateInput.value = toDate;
            loadMeals();
        });

        // ── Activity ──────────────────────────────────────────────────
        //
        // The action log, rendered. Three things each row has to make
        // obvious: whether it was allowed, whether it finished, and
        // whether anyone checked the result. "Not checked" is shown as
        // its own state rather than folded into success, because the
        // whole point of the log is that a call returning is not
        // evidence that anything happened.
        const ACTIVITY_ICONS = {
            builtin: '\u{1F527}', mcp: '\u{1F50C}', human: '\u{1F464}',
            unknown: '\u2753',
        };

        function activityStatus(action) {
            if (action.decision === 'denied') {
                return { label: 'refused', className: 'status-denied' };
            }
            // A human decision has nothing to come back from, so it is
            // not waiting on an outcome and must not read as a fault.
            if (action.tool_source === 'human') {
                return { label: 'recorded', className: 'status-unchecked' };
            }
            if (!action.outcome) {
                return { label: 'never finished', className: 'status-unfinished' };
            }
            if (action.outcome === 'error') {
                return { label: 'failed', className: 'status-error' };
            }
            if (action.verification === 'confirmed') {
                return { label: 'confirmed', className: 'status-confirmed' };
            }
            if (action.verification === 'failed') {
                return { label: 'not verified', className: 'status-error' };
            }
            return { label: 'not checked', className: 'status-unchecked' };
        }

        async function loadActivity() {
            const list = document.getElementById('activity-list');
            if (!list) return;
            try {
                const data = await (await fetch('/api/actions')).json();
                const actions = data.actions || [];
                if (!actions.length) {
                    list.innerHTML = '<div class="empty-state">Nothing yet. '
                        + 'Actions Jarvis takes will appear here.</div>';
                    return;
                }
                list.innerHTML = actions.map(action => {
                    const status = activityStatus(action);
                    const icon = ACTIVITY_ICONS[action.tool_source] || ACTIVITY_ICONS.unknown;
                    const when = new Date(action.ts_utc).toLocaleString();
                    const detail = action.decision_reason || action.outcome_detail || '';
                    const args = action.arguments || '';
                    return '<div class="memory-card">'
                        + '<div class="memory-card-header">'
                        + '<span class="memory-date">' + icon + ' '
                        + escapeHtml(action.tool_name) + '</span>'
                        + '<span class="conn-pill ' + status.className + '">'
                        + status.label + '</span>'
                        + '</div>'
                        + '<div class="memory-summary">' + escapeHtml(when) + '</div>'
                        + (detail ? '<div class="memory-summary">'
                            + escapeHtml(detail) + '</div>' : '')
                        + (args ? '<div class="memory-topics"><code>'
                            + escapeHtml(args) + '</code></div>' : '')
                        + '</div>';
                }).join('');
            } catch (e) {
                list.innerHTML = '<div class="empty-state">Could not read the '
                    + 'action log.</div>';
            }
        }

        function switchTab(tabName) {
            tabs.forEach(t => t.classList.remove('active'));
            document.querySelector(`.tab[data-tab="${tabName}"]`).classList.add('active');
            currentTab = tabName;

            // Hide all panes
            chatPane.style.display = 'none';
            memoriesPane.style.display = 'none';
            graphContent.style.display = 'none';
            mealsPane.style.display = 'none';
            connPane.style.display = 'none';
            settingsPane.style.display = 'none';
            const activityPane = document.getElementById('activity-content');
            if (activityPane) activityPane.style.display = 'none';

            if (currentTab === 'chat') {
                chatPane.style.display = '';
                chatInput.focus();
            } else if (currentTab === 'memories') {
                memoriesPane.style.display = '';
                loadMemories();
            } else if (currentTab === 'graph') {
                graphContent.style.display = '';
                initGraph();
            } else if (currentTab === 'connections') {
                connPane.style.display = '';
                loadCatalogue();
                loadRegistry();
                loadConnections();
            } else if (currentTab === 'activity') {
                if (activityPane) activityPane.style.display = '';
                loadActivity();
            } else if (currentTab === 'settings') {
                settingsPane.style.display = '';
                loadSettings();
            } else {
                mealsPane.style.display = '';
                loadMeals();
            }
        }

        tabs.forEach(tab => {
            tab.addEventListener('click', () => switchTab(tab.dataset.tab));
        });

        // ── Settings ──────────────────────────────────────────────────
        async function loadSettings() {
            const d = await (await fetch('/api/settings')).json();
            document.getElementById('set-provider').value = d.llm_provider || 'openai_compatible';
            document.getElementById('set-baseurl').value = d.llm_base_url || '';
            document.getElementById('set-chat').value = d.llm_chat_model || '';
            document.getElementById('set-fast').value = d.fast_model || '';
            document.getElementById('set-embed').value = d.embedding_model || '';
            // The key is never sent to the page. A hint identifies which one
            // is loaded without exposing the credential.
            document.getElementById('set-key').placeholder = d.has_key
                ? `key ending ${d.key_hint} is saved — leave blank to keep it`
                : 'paste your API key';
        }

        function settingsPayload() {
            return {
                llm_provider: document.getElementById('set-provider').value,
                llm_base_url: document.getElementById('set-baseurl').value.trim(),
                llm_chat_model: document.getElementById('set-chat').value.trim(),
                fast_model: document.getElementById('set-fast').value.trim(),
                embedding_model: document.getElementById('set-embed').value.trim(),
                llm_api_key: document.getElementById('set-key').value.trim()
            };
        }

        function setStatus(text, cls) {
            const el = document.getElementById('set-status');
            el.textContent = text;
            el.className = 'settings-status ' + (cls || '');
        }

        document.getElementById('set-save').addEventListener('click', async () => {
            setStatus('Saving…');
            const res = await fetch('/api/settings', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(settingsPayload())
            });
            const d = await res.json();
            if (res.ok) {
                document.getElementById('set-key').value = '';
                setStatus('Saved — applies on your next message.', 'ok');
                loadSettings();
            } else {
                setStatus(d.error || 'Could not save.', 'bad');
            }
        });

        document.getElementById('set-test').addEventListener('click', async () => {
            setStatus('Testing…');
            const res = await fetch('/api/settings/test', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(settingsPayload())
            });
            const d = await res.json();
            if (res.ok) setStatus(`Connected — ${d.count} models available.`, 'ok');
            else setStatus(d.error || 'Connection failed.', 'bad');
        });

        // ── Connections (MCP) ─────────────────────────────────────────
        async function loadCatalogue() {
            const grid = document.getElementById('conn-catalogue');
            grid.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            try {
                const data = await (await fetch('/api/mcp/catalogue')).json();
                if (!data.entries.length) {
                    grid.innerHTML = '';
                    return;
                }
                // No "verified" tick anywhere here: Jarvis does not vet these
                // servers, and a badge implying otherwise would undercut the
                // whole reason the MCP security layer exists.
                grid.innerHTML = data.entries.map(e => {
                    const keyField = e.needs_api_key && !e.configured
                        ? `<input class="conn-key" data-name="${escapeHtml(e.name)}"
                                  type="password" placeholder="API key"
                                  title="${escapeHtml(e.api_key_hint)}" />`
                        : '';
                    const button = e.configured
                        ? '<button class="conn-added" disabled>Added</button>'
                        : `<button class="conn-catalogue-add btn-primary"
                                   data-name="${escapeHtml(e.name)}">Add</button>`;
                    return `<div class="conn-tile${e.configured ? ' is-added' : ''}">
                        <div class="conn-tile-name">${escapeHtml(e.display_name)}</div>
                        <div class="conn-tile-desc">${escapeHtml(e.description)}</div>
                        <div class="conn-tile-foot">
                            <span class="conn-tag">${escapeHtml(e.category)}</span>
                            ${keyField}
                            ${button}
                        </div>
                        ${e.needs_api_key && !e.configured
                            ? `<div class="conn-tile-hint">${escapeHtml(e.api_key_hint)}</div>` : ''}
                    </div>`;
                }).join('');

                grid.querySelectorAll('.conn-catalogue-add').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const name = btn.dataset.name;
                        const keyInput = grid.querySelector('.conn-key[data-name="' + name + '"]');
                        btn.disabled = true;
                        const res = await fetch('/api/mcp/catalogue/' + encodeURIComponent(name), {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({api_key: keyInput ? keyInput.value.trim() : ''})
                        });
                        if (!res.ok) {
                            // 409 (name taken), 400 (nothing pinned to install) and 404 all
                            // need saying: a button that silently re-enables invites another
                            // click and the same silence.
                            const body = await res.json().catch(() => ({}));
                            const foot = btn.parentElement;
                            let note = foot.querySelector('.conn-error');
                            if (!note) {
                                note = document.createElement('div');
                                note.className = 'conn-error';
                                foot.appendChild(note);
                            }
                            note.textContent = body.error || 'Could not add that server.';
                            btn.disabled = false;
                            return;
                        }
                        loadCatalogue();
                        loadRegistry();
                        loadConnections();
                    });
                });
            } catch (err) {
                grid.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div>'
                    + '<div class="empty-title">Could not load the directory</div></div>';
            }
        }

        // What the client can actually speak. `sse` is deliberately
        // absent, and the server refuses it too.
        const REMOTE_TRANSPORTS = new Set(['streamable-http', 'streamable_http', 'http', 'https']);

        let registryEntries = [];

        function renderRegistry() {
            const grid = document.getElementById('conn-registry');
            const needle = document.getElementById('conn-registry-search').value.trim().toLowerCase();
            const shown = registryEntries.filter(e => !needle
                || e.name.toLowerCase().includes(needle)
                || (e.description || '').toLowerCase().includes(needle));
            if (!shown.length) {
                grid.innerHTML = '<div class="empty-state"><div class="empty-icon">🌍</div>'
                    + '<div class="empty-title">Nothing cached yet</div>'
                    + '<p>Refresh to fetch the registry.</p></div>';
                return;
            }
            grid.innerHTML = shown.slice(0, 60).map(e => {
                // The proof is about identity, never about safety, so it is
                // worded as who was checked rather than as a tick.
                const proof = e.namespace_proof === 'github'
                    ? 'GitHub account checked' : 'Domain checked';
                // Who the data goes to is the question a hosted server
                // raises, and the registry is thick with aggregators
                // re-publishing other people's tools. Naming the host is
                // not a safety verdict, it is the fact the user needs to
                // reach one.
                let host = '';
                if (e.remote_url) {
                    try { host = new URL(e.remote_url).hostname; } catch (err) { host = ''; }
                }
                let action;
                if (e.configured) {
                    action = '<button class="conn-added" disabled>Added</button>';
                } else if (e.install) {
                    action = `<button class="conn-registry-add btn-primary"
                                      data-name="${escapeHtml(e.name)}">Add</button>`;
                } else if (e.remote_url && REMOTE_TRANSPORTS.has(
                        (e.remote_transport || 'streamable-http').toLowerCase())) {
                    // Hosted, and speakable. Nothing is installed: the
                    // browser opens, you approve, the token goes to the
                    // keychain. "Connect" rather than "Add" because that
                    // is what the click actually does.
                    action = `<button class="conn-registry-add btn-primary"
                                      data-name="${escapeHtml(e.name)}">Connect</button>`;
                } else {
                    action = `<span class="conn-tag" title="${e.remote_url
                        ? 'A hosted server speaking a transport Jarvis cannot use yet.'
                        : 'No pinned package, so it cannot be launched safely.'}">not installable</span>`;
                }
                return `<div class="conn-tile${e.configured ? ' is-added' : ''}">
                    <div class="conn-tile-name">${escapeHtml(e.title || e.name)}</div>
                    <div class="conn-tile-desc">${escapeHtml(e.description)}</div>
                    <div class="conn-tile-foot">
                        <span class="conn-tag" title="${escapeHtml(proof)}">${escapeHtml(e.namespace)}</span>
                        ${action}
                    </div>
                    <div class="conn-tile-hint">${escapeHtml(proof)} · v${escapeHtml(e.version)}${
                        host ? ' · sends your data to ' + escapeHtml(host) : ''}</div>
                </div>`;
            }).join('');

            grid.querySelectorAll('.conn-registry-add').forEach(btn => {
                btn.addEventListener('click', async () => {
                    btn.disabled = true;
                    const res = await fetch('/api/mcp/registry/add', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({name: btn.dataset.name})
                    });
                    if (!res.ok) {
                        // 409 (name taken), 400 (nothing pinned to install) and 404 all
                        // need saying: a button that silently re-enables invites another
                        // click and the same silence.
                        const body = await res.json().catch(() => ({}));
                        const foot = btn.parentElement;
                        let note = foot.querySelector('.conn-error');
                        if (!note) {
                            note = document.createElement('div');
                            note.className = 'conn-error';
                            foot.appendChild(note);
                        }
                        note.textContent = body.error || 'Could not add that server.';
                        btn.disabled = false;
                        return;
                    }
                    loadCatalogue();
                    loadRegistry();
                    loadConnections();
                });
            });
        }

        async function loadRegistry() {
            try {
                const data = await (await fetch('/api/mcp/registry')).json();
                registryEntries = data.entries || [];
                const age = document.getElementById('conn-registry-age');
                // A cached directory that hides its age invites acting on
                // stale data, so the fetch time is always on screen.
                age.textContent = data.fetched_at
                    ? 'Cached ' + new Date(data.fetched_at * 1000).toLocaleString()
                    : 'Never fetched';
                renderRegistry();
            } catch (err) {
                registryEntries = [];
                renderRegistry();
            }
        }

        document.getElementById('conn-registry-refresh').addEventListener('click', async () => {
            const btn = document.getElementById('conn-registry-refresh');
            const age = document.getElementById('conn-registry-age');
            btn.disabled = true;
            age.textContent = 'Fetching…';
            const res = await fetch('/api/mcp/registry/refresh', {method: 'POST'});
            btn.disabled = false;
            if (!res.ok) {
                age.textContent = 'Could not reach the registry';
                return;
            }
            loadRegistry();
        });

        document.getElementById('conn-registry-search')
            .addEventListener('input', renderRegistry);

        async function loadConnections() {
            const list = document.getElementById('conn-list');
            list.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            try {
                const data = await (await fetch('/api/mcp')).json();
                if (!data.servers.length) {
                    list.innerHTML = '<div class="empty-state">'
                        + '<div class="empty-icon">🔌</div>'
                        + '<div class="empty-title">No connections yet</div>'
                        + '<p>Add an MCP server above to give Jarvis more tools.</p></div>';
                    return;
                }
                list.innerHTML = data.servers.map(s => {
                    // A configured server offering zero tools is a failure worth
                    // showing — silence here used to look like success.
                    const ok = s.tools > 0;
                    const label = ok ? `${s.tools} tool${s.tools === 1 ? '' : 's'}`
                                     : (s.error ? 'failed' : 'no tools');
                    const cmd = escapeHtml([s.command].concat(s.args || []).join(' '));
                    return `<div class="conn-card">
                        <div class="conn-meta">
                            <div class="conn-name">${escapeHtml(s.name)}</div>
                            <div class="conn-cmd">${cmd}</div>
                            ${s.error ? `<div class="conn-cmd">⚠️ ${escapeHtml(s.error)}</div>` : ''}
                        </div>
                        <span class="conn-pill ${ok ? 'ok' : 'bad'}">${label}</span>
                        <button class="conn-remove" data-name="${escapeHtml(s.name)}">Remove</button>
                    </div>`;
                }).join('');

                list.querySelectorAll('.conn-remove').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        await fetch('/api/mcp/' + encodeURIComponent(btn.dataset.name),
                                    {method: 'DELETE'});
                        loadCatalogue();
                        loadRegistry();
                        loadConnections();
                    });
                });
            } catch (err) {
                list.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div>'
                    + '<div class="empty-title">Could not load connections</div></div>';
            }
        }

        document.getElementById('conn-add-btn').addEventListener('click', async () => {
            const name = document.getElementById('conn-name').value.trim();
            const command = document.getElementById('conn-command').value.trim();
            const args = document.getElementById('conn-args').value.trim();
            if (!name || !command) return;

            const res = await fetch('/api/mcp', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name, command, args: args ? args.split(/\s+/) : []})
            });
            if (res.ok) {
                document.getElementById('conn-name').value = '';
                document.getElementById('conn-command').value = '';
                document.getElementById('conn-args').value = '';
                loadCatalogue();
                loadRegistry();
                loadConnections();
            }
        });

        // ── HUD rail ──────────────────────────────────────────────────
        // Telemetry is polled, not pushed: these numbers are ambient, and a
        // socket for three gauges is not worth the reconnect logic.
        function setGauge(id, barId, percent, label) {
            document.getElementById(id).textContent = label;
            document.getElementById(barId).style.width = Math.max(0, Math.min(100, percent)) + '%';
        }

        async function loadSystem() {
            try {
                const d = await (await fetch('/api/system')).json();
                if (d.cpu !== undefined) setGauge('sys-cpu', 'bar-cpu', d.cpu, d.cpu.toFixed(0) + '%');
                if (d.memory) setGauge('sys-mem', 'bar-mem', d.memory.percent,
                    `${d.memory.used_gb}/${d.memory.total_gb} GB`);
                if (d.disk) setGauge('sys-disk', 'bar-disk', d.disk.percent,
                    `${d.disk.free_gb} GB free`);
                if (d.provider) document.getElementById('sys-provider').textContent = d.provider;
                if (d.model) document.getElementById('sys-model').textContent = d.model;
            } catch (e) { /* rail is ambient; a failed poll is not worth an alert */ }
        }

        async function loadWeather() {
            const body = document.getElementById('weather-body');
            try {
                const res = await fetch('/api/weather');
                const d = await res.json();
                if (!res.ok) {
                    // Say it plainly rather than showing a number nobody can trust.
                    body.textContent = 'Unavailable — enable location in config.';
                    return;
                }
                body.textContent = (d.text || '').split('\n').slice(0, 4).join('\n');
            } catch (e) {
                body.textContent = 'Unavailable.';
            }
        }

        loadSystem();
        loadWeather();
        setInterval(loadSystem, 5000);
        setInterval(loadWeather, 15 * 60 * 1000);

        // Activate focuses the composer. It does not start the microphone:
        // a browser cannot reach the mic through Flask, and a button that
        // implies otherwise would be a lie about what this page can do.
        document.getElementById('activate-btn').addEventListener('click', () => {
            chatInput.focus();
        });

        // Mirror the orb's state into the rail readout.
        const _setOrbState = setOrbState;
        setOrbState = function (state) {
            _setOrbState(state);
            const el = document.getElementById('sys-state');
            if (el) el.textContent = state.toUpperCase();
        };

        // ── Orb ───────────────────────────────────────────────────────
        // Same geometry as the desktop widget (orb_widget.py): points spread
        // over a sphere by a Fibonacci spiral, spun about the vertical axis
        // and projected flat, with depth driving size and opacity. Kept in
        // step with MOTION there — speaking is the only state that ripples,
        // because that is the cue that Jarvis is talking.
        const ORB_MOTION = {
            idle:     {spin: 0.18, breathe: 0.028, ripple: 0,     rippleSpeed: 0,   turbulence: 0,     brightness: 0.70},
            thinking: {spin: 0.85, breathe: 0.030, ripple: 0,     rippleSpeed: 0,   turbulence: 0.10,  brightness: 0.85},
            speaking: {spin: 0.34, breathe: 0.030, ripple: 0.115, rippleSpeed: 3.1, turbulence: 0.015, brightness: 1.00}
        };
        const ORB_POINTS = (() => {
            const n = 640, golden = Math.PI * (3 - Math.sqrt(5)), pts = [];
            for (let i = 0; i < n; i++) {
                const y = 1 - (2 * i) / (n - 1);
                const r = Math.sqrt(Math.max(0, 1 - y * y));
                const th = golden * i;
                pts.push([Math.cos(th) * r, y, Math.sin(th) * r]);
            }
            return pts;
        })();

        let orbState = 'idle';
        const orbCanvas = document.getElementById('orb');
        const orbCtx = orbCanvas.getContext('2d');
        const orbLabel = document.getElementById('orb-state');

        function setOrbState(state) {
            orbState = state;
            orbLabel.textContent = state;
        }

        function drawOrb(t) {
            const m = ORB_MOTION[orbState] || ORB_MOTION.idle;
            const w = orbCanvas.width, h = orbCanvas.height;
            const cx = w / 2, cy = h / 2, radius = Math.min(w, h) * 0.235;
            orbCtx.clearRect(0, 0, w, h);

            // ── HUD instrumentation ────────────────────────────────
            // Layered concentric tracks, counter-rotating at different
            // rates. Every layer's speed is derived from m.spin, so the
            // whole assembly accelerates when Jarvis is thinking and
            // settles when idle — the rings report state, not decoration.
            const glow = (colour, blur) => {
                orbCtx.shadowColor = colour;
                orbCtx.shadowBlur = blur;
            };
            const CY = '103,232,249', CY_DEEP = '34,211,238';

            // 1. Outer dashed containment ring.
            glow(`rgba(${CY_DEEP},0.95)`, 18);
            orbCtx.strokeStyle = `rgba(${CY},${0.85 * m.brightness})`;
            orbCtx.lineWidth = 2;
            orbCtx.setLineDash([14, 9]);
            orbCtx.lineDashOffset = -t * m.spin * 90;
            orbCtx.beginPath();
            orbCtx.arc(cx, cy, radius * 1.95, 0, Math.PI * 2);
            orbCtx.stroke();
            orbCtx.setLineDash([]);

            // 2. Heavy segmented arcs — the ring that reads as "active".
            const seg = [
                {r: 1.72, from: 0.15, sweep: 1.55, dir:  1, w: 7,   a: 0.95},
                {r: 1.72, from: 3.30, sweep: 1.20, dir:  1, w: 7,   a: 0.95},
                {r: 1.52, from: 1.90, sweep: 2.10, dir: -1, w: 3.5, a: 0.80},
                {r: 1.52, from: 5.10, sweep: 0.80, dir: -1, w: 3.5, a: 0.80},
                {r: 1.34, from: 0.60, sweep: 2.60, dir:  1, w: 2,   a: 0.55}
            ];
            for (const s of seg) {
                const a0 = s.from + t * m.spin * 1.6 * s.dir;
                glow(`rgba(${CY_DEEP},0.95)`, 20);
                orbCtx.strokeStyle = `rgba(${CY},${s.a * m.brightness})`;
                orbCtx.lineWidth = s.w;
                orbCtx.lineCap = 'round';
                orbCtx.beginPath();
                orbCtx.arc(cx, cy, radius * s.r, a0, a0 + s.sweep);
                orbCtx.stroke();
            }
            orbCtx.lineCap = 'butt';

            // 3. Fine tick track.
            glow(`rgba(${CY_DEEP},0.8)`, 8);
            for (let i = 0; i < 72; i++) {
                const a = (i / 72) * Math.PI * 2 - t * m.spin * 0.55;
                const major = i % 6 === 0;
                const inner = radius * 1.80;
                const outer = radius * (major ? 1.92 : 1.86);
                orbCtx.strokeStyle = `rgba(${CY},${(major ? 0.9 : 0.45) * m.brightness})`;
                orbCtx.lineWidth = major ? 2 : 1;
                orbCtx.beginPath();
                orbCtx.moveTo(cx + Math.cos(a) * inner, cy + Math.sin(a) * inner);
                orbCtx.lineTo(cx + Math.cos(a) * outer, cy + Math.sin(a) * outer);
                orbCtx.stroke();
            }

            // 4. Inner data ring: discrete blocks, like a readout.
            for (let i = 0; i < 40; i++) {
                const a = (i / 40) * Math.PI * 2 + t * m.spin * 2.2;
                const lit = (i * 7 + Math.floor(t * 6)) % 5 !== 0;
                orbCtx.strokeStyle = `rgba(${CY},${(lit ? 0.75 : 0.16) * m.brightness})`;
                orbCtx.lineWidth = 3;
                orbCtx.beginPath();
                orbCtx.arc(cx, cy, radius * 1.16, a, a + 0.10);
                orbCtx.stroke();
            }

            // 5. Core disc behind the particles, so the sphere sits in light.
            const core = orbCtx.createRadialGradient(cx, cy, 0, cx, cy, radius * 1.05);
            core.addColorStop(0, `rgba(${CY_DEEP},${0.42 * m.brightness})`);
            core.addColorStop(0.65, `rgba(${CY_DEEP},${0.14 * m.brightness})`);
            core.addColorStop(1, 'rgba(4,10,20,0)');
            orbCtx.shadowBlur = 0;
            orbCtx.fillStyle = core;
            orbCtx.beginPath();
            orbCtx.arc(cx, cy, radius * 1.05, 0, Math.PI * 2);
            orbCtx.fill();

            // 5b. Luminous sphere body: shaded globe under the particles,
            // with a hot core at the centre — reads as a solid object, not
            // a scatter of dots.
            const body = orbCtx.createRadialGradient(
                cx - radius * 0.35, cy - radius * 0.35, radius * 0.08,
                cx, cy, radius);
            body.addColorStop(0, `rgba(126,232,250,${0.28 * m.brightness})`);
            body.addColorStop(0.55, `rgba(${CY_DEEP},${0.12 * m.brightness})`);
            body.addColorStop(0.92, `rgba(8,24,43,${0.42 * m.brightness})`);
            body.addColorStop(1, `rgba(${CY_DEEP},${0.30 * m.brightness})`);
            orbCtx.fillStyle = body;
            orbCtx.beginPath();
            orbCtx.arc(cx, cy, radius, 0, Math.PI * 2);
            orbCtx.fill();

            // Rim light around the sphere edge.
            glow(`rgba(${CY_DEEP},0.9)`, 22);
            orbCtx.strokeStyle = `rgba(${CY},${0.5 * m.brightness})`;
            orbCtx.lineWidth = 1.5;
            orbCtx.beginPath();
            orbCtx.arc(cx, cy, radius, 0, Math.PI * 2);
            orbCtx.stroke();

            const swell = 1 + m.breathe * Math.sin(t * 1.9);
            const spun = m.spin * t, cs = Math.cos(spun), sn = Math.sin(spun);
            const drawn = [];

            for (let i = 0; i < ORB_POINTS.length; i++) {
                const [x, y, z] = ORB_POINTS[i];
                let r = swell;
                if (m.ripple) r += m.ripple * Math.sin(y * 3.4 - t * m.rippleSpeed);
                if (m.turbulence) {
                    r += m.turbulence * Math.sin(i * 12.9898 + t * 1.7) * Math.cos(i * 4.1414 + t * 0.9);
                }
                const rx = (x * cs - z * sn) * r;
                const rz = (x * sn + z * cs) * r;
                const depth = Math.max(0, Math.min(1, (rz + 1) / 2));
                drawn.push([cx + rx * radius, cy - y * r * radius, depth]);
            }
            drawn.sort((a, b) => a[2] - b[2]);  // far side first
            orbCtx.shadowColor = 'rgba(34,211,238,0.9)';
            orbCtx.shadowBlur = 10;

            for (const [px, py, depth] of drawn) {
                const size = 0.8 + 2.4 * Math.pow(depth, 1.3);
                const alpha = (0.22 + 0.78 * Math.pow(depth, 1.6)) * m.brightness;
                const cr = Math.round(0x0e + (0x67 - 0x0e) * depth);
                const cg = Math.round(0x5f + (0xe8 - 0x5f) * depth);
                const cb = Math.round(0x74 + (0xf9 - 0x74) * depth);
                orbCtx.fillStyle = `rgba(${cr},${cg},${cb},${alpha})`;
                orbCtx.beginPath();
                orbCtx.arc(px, py, size, 0, Math.PI * 2);
                orbCtx.fill();
            }
            orbCtx.shadowBlur = 0;

            // Hot core point at the centre of the label glow.
            orbCtx.save();
            glow(`rgba(${CY},1)`, 30);
            orbCtx.fillStyle = `rgba(238,250,255,${0.9 * m.brightness})`;
            orbCtx.beginPath();
            orbCtx.arc(cx, cy + radius * 0.22, radius * 0.028 * (1 + 0.3 * Math.sin(t * 2.4)), 0, Math.PI * 2);
            orbCtx.fill();
            orbCtx.restore();

            // Core label, over the sphere.
            orbCtx.save();
            orbCtx.shadowColor = 'rgba(34,211,238,1)';
            orbCtx.shadowBlur = 20;
            orbCtx.fillStyle = `rgba(238,250,255,${0.98 * m.brightness})`;
            orbCtx.font = `700 ${Math.round(radius * 0.26)}px 'JetBrains Mono', monospace`;
            orbCtx.textAlign = 'center';
            orbCtx.textBaseline = 'middle';
            orbCtx.fillText('J.A.R.V.I.S', cx, cy - radius * 0.04);
            orbCtx.restore();
        }

        (function orbLoop(start) {
            const step = (now) => {
                drawOrb((now - start) / 1000);
                requestAnimationFrame(step);
            };
            requestAnimationFrame(step);
        })(performance.now());

        // ── Chat ──────────────────────────────────────────────────────
        let chatBusy = false;

        function appendBubble(role, text) {
            const empty = chatLog.querySelector('.empty-state');
            if (empty) empty.remove();
            document.querySelector('.hud-core')?.classList.add('talking');
            const row = document.createElement('div');
            row.className = 'chat-msg ' + role;
            row.innerHTML = `<div class="chat-bubble">${escapeHtml(text)}</div>`;
            chatLog.appendChild(row);
            chatLog.scrollTop = chatLog.scrollHeight;
            return row;
        }

        async function sendChat() {
            const message = chatInput.value.trim();
            if (!message || chatBusy) return;

            chatBusy = true;
            setOrbState('thinking');
            chatInput.value = '';
            chatInput.style.height = 'auto';
            appendBubble('user', message);

            // The reply engine plans, may call tools, then answers — that
            // can run to tens of seconds, so the wait needs to be visible
            // or the page looks broken.
            const pending = appendBubble('assistant pending', 'Thinking…');

            try {
                const response = await fetch('/api/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message})
                });
                const data = await response.json();
                pending.remove();
                if (!response.ok) {
                    appendBubble('assistant error', data.error || 'Something went wrong.');
                } else {
                    appendBubble('assistant', data.reply);
                    // Hold SPEAKING for roughly as long as the reply takes to
                    // read, so the ripple lines up with the user reading it.
                    setOrbState('speaking');
                    const dwell = Math.min(9000, 1200 + (data.reply || '').length * 45);
                    setTimeout(() => { if (!chatBusy) setOrbState('idle'); }, dwell);
                }
            } catch (err) {
                pending.remove();
                appendBubble('assistant error', 'Could not reach Jarvis: ' + err.message);
            } finally {
                chatBusy = false;
                if (orbState === 'thinking') setOrbState('idle');
                chatInput.focus();
            }
        }

        document.getElementById('chat-send').addEventListener('click', sendChat);

        chatInput.addEventListener('keydown', (e) => {
            // Enter sends; Shift+Enter is a newline, as in every chat app.
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendChat();
            }
        });

        chatInput.addEventListener('input', () => {
            chatInput.style.height = 'auto';
            chatInput.style.height = Math.min(chatInput.scrollHeight, 160) + 'px';
        });

        document.getElementById('chat-reset').addEventListener('click', async () => {
            const res = await fetch('/api/chat/reset', {method: 'POST'});
            let saved = false;
            try { saved = (await res.json()).saved; } catch (e) { /* keep false */ }
            chatLog.innerHTML = '<div class="empty-state">'
                + '<div class="empty-icon">💬</div>'
                + '<div class="empty-title">Fresh conversation</div>'
                + (saved
                    ? '<p>The previous one was saved to the diary.</p>'
                    : '<p>Nothing new to save.</p>') + '</div>';
            document.querySelector('.hud-core')?.classList.remove('talking');
            setOrbState('idle');
        });

        // Diary maintenance button lives in the diary tab's sidebar, which
        // renders on page load (diary is the default tab). Wire its handler
        // here on the always-run setup path — initGraph only fires when the
        // user opens the Knowledge tab, and the diary clean button must
        // work even for users who never visit it.
        document.getElementById('btn-scrub-deflections').addEventListener('click', () => {
            showScrubDeflectionsModal();
        });

        document.getElementById('btn-optimise-topics').addEventListener('click', () => {
            showOptimiseTopicsModal();
        });

        // ─── Graph Explorer ────────────────────────────────────────────
        let graphInitialised = false;
        let graphNodes = [];
        let graphEdges = [];
        let selectedNodeId = null;
        let graphZoom = 1;
        let graphPanX = 0;
        let graphPanY = 0;
        let isDragging = false;
        let dragStartX = 0;
        let dragStartY = 0;
        let hoveredNodeId = null;

        // Layout positions (computed once per data load)
        const nodePositions = new Map();

        const canvas = document.getElementById('graph-canvas');
        const ctx = canvas.getContext('2d');

        async function loadPresetNodeIds() {
            try {
                const resp = await fetch('/api/graph/presets');
                const data = await resp.json();
                if (Array.isArray(data.ids)) {
                    PRESET_NODE_IDS = new Set(data.ids);
                }
            } catch (e) {
                // Fail-closed: leave the seeded {'root'} set in place.
                console.warn('Failed to load preset node IDs; falling back to root-only.', e);
            }
        }

        async function initGraph() {
            if (!graphInitialised) {
                setupCanvasEvents();
                graphInitialised = true;
            }
            resizeCanvas();
            await loadPresetNodeIds();
            loadGraphData();
            loadTreeData();
        }

        function resizeCanvas() {
            const container = canvas.parentElement;
            canvas.width = container.clientWidth * window.devicePixelRatio;
            canvas.height = container.clientHeight * window.devicePixelRatio;
            canvas.style.width = container.clientWidth + 'px';
            canvas.style.height = container.clientHeight + 'px';
            ctx.setTransform(window.devicePixelRatio, 0, 0, window.devicePixelRatio, 0, 0);
        }

        async function loadGraphData() {
            try {
                const resp = await fetch('/api/graph/nodes?max_depth=10');
                const data = await resp.json();
                graphNodes = data.nodes || [];
                graphEdges = data.edges || [];
                computeLayout();
                fitToView();
                drawGraph();
            } catch (e) {
                console.error('Failed to load graph:', e);
            }
        }

        async function loadTreeData() {
            const container = document.getElementById('tree-container');
            try {
                const resp = await fetch('/api/graph/tree?max_depth=10');
                const tree = await resp.json();
                container.innerHTML = '';
                if (tree.node) {
                    renderTreeNode(container, tree, 0);
                }
            } catch (e) {
                container.innerHTML = '<div class="detail-empty"><p>Failed to load tree</p></div>';
            }
        }

        function renderTreeNode(container, treeData, depth) {
            const node = treeData.node;
            const children = treeData.children || [];
            const hasChildren = children.length > 0;

            const el = document.createElement('div');

            const nodeEl = document.createElement('div');
            nodeEl.className = 'tree-node' + (selectedNodeId === node.id ? ' selected' : '');
            nodeEl.dataset.nodeId = node.id;
            nodeEl.style.paddingLeft = (0.75 + depth * 0.75) + 'rem';

            const toggle = document.createElement('span');
            toggle.className = 'tree-toggle' + (hasChildren ? ' expanded' : ' leaf');
            toggle.textContent = '▶';

            const nameSpan = document.createElement('span');
            nameSpan.className = 'tree-node-name';
            nameSpan.textContent = node.name;

            nodeEl.appendChild(toggle);
            nodeEl.appendChild(nameSpan);

            if (hasChildren) {
                const countSpan = document.createElement('span');
                countSpan.className = 'tree-node-count';
                countSpan.textContent = children.length;
                nodeEl.appendChild(countSpan);
            }

            nodeEl.addEventListener('click', (e) => {
                e.stopPropagation();
                selectNode(node.id);
            });

            el.appendChild(nodeEl);

            if (hasChildren) {
                const childContainer = document.createElement('div');
                childContainer.className = 'tree-children';

                toggle.addEventListener('click', (e) => {
                    e.stopPropagation();
                    childContainer.classList.toggle('collapsed');
                    toggle.classList.toggle('expanded');
                });

                children.forEach(child => {
                    renderTreeNode(childContainer, child, depth + 1);
                });

                el.appendChild(childContainer);
            }

            container.appendChild(el);
        }

        function computeLayout() {
            nodePositions.clear();
            if (graphNodes.length === 0) return;

            // Build adjacency for tree layout
            const childrenMap = new Map();
            graphNodes.forEach(n => childrenMap.set(n.id, []));
            graphEdges.forEach(e => {
                const list = childrenMap.get(e.source);
                if (list) list.push(e.target);
            });

            // Radial tree layout
            const root = graphNodes.find(n => n.id === 'root') || graphNodes[0];
            const visited = new Set();
            const RING_SPACING = 160;
            const MIN_ARC = 40;

            function layoutSubtree(nodeId, cx, cy, startAngle, endAngle, depth) {
                if (visited.has(nodeId)) return;
                visited.add(nodeId);

                nodePositions.set(nodeId, { x: cx, y: cy });

                const kids = childrenMap.get(nodeId) || [];
                if (kids.length === 0) return;

                const radius = RING_SPACING * (depth + 1);
                const arcPerChild = (endAngle - startAngle) / kids.length;

                kids.forEach((kidId, i) => {
                    const angle = startAngle + arcPerChild * (i + 0.5);
                    const kx = cx + Math.cos(angle) * radius;
                    const ky = cy + Math.sin(angle) * radius;
                    const halfArc = arcPerChild * 0.45;
                    layoutSubtree(kidId, kx, ky, angle - halfArc, angle + halfArc, depth + 1);
                });
            }

            layoutSubtree(root.id, 0, 0, 0, Math.PI * 2, 0);

            // Place any unvisited nodes in a line below
            let offsetX = -200;
            graphNodes.forEach(n => {
                if (!visited.has(n.id)) {
                    nodePositions.set(n.id, { x: offsetX, y: 500 });
                    offsetX += 100;
                }
            });
        }

        function fitToView() {
            if (nodePositions.size === 0) return;

            const cw = canvas.width / window.devicePixelRatio;
            const ch = canvas.height / window.devicePixelRatio;
            const padding = 80;

            let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
            nodePositions.forEach(pos => {
                minX = Math.min(minX, pos.x);
                maxX = Math.max(maxX, pos.x);
                minY = Math.min(minY, pos.y);
                maxY = Math.max(maxY, pos.y);
            });

            const graphW = maxX - minX || 1;
            const graphH = maxY - minY || 1;
            graphZoom = Math.min((cw - padding * 2) / graphW, (ch - padding * 2) / graphH, 2);
            graphZoom = Math.max(graphZoom, 0.1);

            const centerX = (minX + maxX) / 2;
            const centerY = (minY + maxY) / 2;
            graphPanX = cw / 2 - centerX * graphZoom;
            graphPanY = ch / 2 - centerY * graphZoom;

            drawGraph();
        }

        function drawGraph() {
            const cw = canvas.width / window.devicePixelRatio;
            const ch = canvas.height / window.devicePixelRatio;
            ctx.clearRect(0, 0, cw, ch);

            ctx.save();
            ctx.translate(graphPanX, graphPanY);
            ctx.scale(graphZoom, graphZoom);

            // Draw edges
            ctx.lineWidth = 1.5 / graphZoom;
            graphEdges.forEach(edge => {
                const from = nodePositions.get(edge.source);
                const to = nodePositions.get(edge.target);
                if (!from || !to) return;

                ctx.beginPath();
                ctx.moveTo(from.x, from.y);
                ctx.lineTo(to.x, to.y);
                ctx.strokeStyle = 'rgba(245, 158, 11, 0.15)';
                ctx.stroke();
            });

            // Draw nodes
            graphNodes.forEach(node => {
                const pos = nodePositions.get(node.id);
                if (!pos) return;

                const isSelected = node.id === selectedNodeId;
                const isHovered = node.id === hoveredNodeId;
                const isRoot = node.id === 'root';
                const baseRadius = isRoot ? 24 : Math.max(12, Math.min(20, 10 + node.access_count * 0.5));
                const radius = baseRadius;

                // Glow for selected/hovered
                if (isSelected || isHovered) {
                    ctx.beginPath();
                    ctx.arc(pos.x, pos.y, radius + 6, 0, Math.PI * 2);
                    ctx.fillStyle = isSelected
                        ? 'rgba(245, 158, 11, 0.25)'
                        : 'rgba(245, 158, 11, 0.12)';
                    ctx.fill();
                }

                // Node circle
                ctx.beginPath();
                ctx.arc(pos.x, pos.y, radius, 0, Math.PI * 2);

                if (isSelected) {
                    ctx.fillStyle = '#f59e0b';
                } else if (isRoot) {
                    ctx.fillStyle = '#1a1d26';
                } else if (node.has_children) {
                    ctx.fillStyle = '#1e222c';
                } else {
                    ctx.fillStyle = '#161920';
                }
                ctx.fill();

                ctx.lineWidth = (isSelected ? 2.5 : 1.5) / graphZoom;
                ctx.strokeStyle = isSelected ? '#fbbf24' : isHovered ? '#f59e0b' : '#27272a';
                ctx.stroke();

                // Label
                const fontSize = Math.max(10, 12 / graphZoom);
                ctx.font = `500 ${fontSize}px Outfit, sans-serif`;
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillStyle = isSelected ? '#fef3c7' : '#f4f4f5';

                // Truncate name to fit
                let label = node.name;
                if (label.length > 14) label = label.slice(0, 12) + '…';
                ctx.fillText(label, pos.x, pos.y);
            });

            ctx.restore();
        }

        function getNodeAtPosition(screenX, screenY) {
            const x = (screenX - graphPanX) / graphZoom;
            const y = (screenY - graphPanY) / graphZoom;

            for (let i = graphNodes.length - 1; i >= 0; i--) {
                const node = graphNodes[i];
                const pos = nodePositions.get(node.id);
                if (!pos) continue;

                const isRoot = node.id === 'root';
                const radius = isRoot ? 24 : Math.max(12, Math.min(20, 10 + node.access_count * 0.5));
                const dx = pos.x - x;
                const dy = pos.y - y;
                if (dx * dx + dy * dy <= (radius + 4) * (radius + 4)) {
                    return node;
                }
            }
            return null;
        }

        function setupCanvasEvents() {
            canvas.addEventListener('mousedown', (e) => {
                isDragging = true;
                dragStartX = e.offsetX;
                dragStartY = e.offsetY;
            });

            canvas.addEventListener('mousemove', (e) => {
                if (isDragging) {
                    graphPanX += e.offsetX - dragStartX;
                    graphPanY += e.offsetY - dragStartY;
                    dragStartX = e.offsetX;
                    dragStartY = e.offsetY;
                    drawGraph();
                } else {
                    const node = getNodeAtPosition(e.offsetX, e.offsetY);
                    const newHovered = node ? node.id : null;
                    if (newHovered !== hoveredNodeId) {
                        hoveredNodeId = newHovered;
                        canvas.style.cursor = newHovered ? 'pointer' : 'grab';
                        drawGraph();
                    }
                }
            });

            canvas.addEventListener('mouseup', (e) => {
                const wasDrag = Math.abs(e.offsetX - dragStartX) > 3 || Math.abs(e.offsetY - dragStartY) > 3;
                isDragging = false;

                if (!wasDrag) {
                    const node = getNodeAtPosition(e.offsetX, e.offsetY);
                    if (node) {
                        selectNode(node.id);
                    }
                }
            });

            canvas.addEventListener('wheel', (e) => {
                e.preventDefault();
                const delta = e.deltaY > 0 ? 0.9 : 1.1;
                const mouseX = e.offsetX;
                const mouseY = e.offsetY;

                // Zoom towards mouse position
                graphPanX = mouseX - (mouseX - graphPanX) * delta;
                graphPanY = mouseY - (mouseY - graphPanY) * delta;
                graphZoom *= delta;
                graphZoom = Math.max(0.05, Math.min(5, graphZoom));

                drawGraph();
            }, { passive: false });

            // Toolbar
            document.getElementById('btn-zoom-in').addEventListener('click', () => {
                const cw = canvas.width / window.devicePixelRatio;
                const ch = canvas.height / window.devicePixelRatio;
                graphPanX = cw/2 - (cw/2 - graphPanX) * 1.3;
                graphPanY = ch/2 - (ch/2 - graphPanY) * 1.3;
                graphZoom *= 1.3;
                drawGraph();
            });

            document.getElementById('btn-zoom-out').addEventListener('click', () => {
                const cw = canvas.width / window.devicePixelRatio;
                const ch = canvas.height / window.devicePixelRatio;
                graphPanX = cw/2 - (cw/2 - graphPanX) * 0.7;
                graphPanY = ch/2 - (ch/2 - graphPanY) * 0.7;
                graphZoom *= 0.7;
                drawGraph();
            });

            document.getElementById('btn-fit').addEventListener('click', fitToView);

            document.getElementById('btn-add-node').addEventListener('click', () => {
                showCreateNodeModal(selectedNodeId || 'root');
            });

            document.getElementById('btn-import-diary').addEventListener('click', () => {
                showImportDiaryModal();
            });

            document.getElementById('btn-consolidate-all').addEventListener('click', () => {
                showConsolidateAllModal();
            });

            // Resize observer
            new ResizeObserver(() => {
                if (currentTab === 'graph') {
                    resizeCanvas();
                    drawGraph();
                }
            }).observe(canvas.parentElement);
        }

        async function selectNode(nodeId) {
            selectedNodeId = nodeId;

            // Update tree selection highlight in-place (no re-render)
            document.querySelectorAll('.tree-node').forEach(el => {
                el.classList.toggle('selected', el.dataset.nodeId === nodeId);
            });

            // Redraw graph
            drawGraph();

            // Load node details
            const sidebar = document.getElementById('detail-sidebar');
            sidebar.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

            try {
                const resp = await fetch('/api/graph/node/' + nodeId);
                const data = await resp.json();
                renderNodeDetail(data);
            } catch (e) {
                sidebar.innerHTML = '<div class="detail-empty"><p>Failed to load node</p></div>';
            }
        }

        function renderNodeDetail(data) {
            const { node, children, ancestors } = data;
            const sidebar = document.getElementById('detail-sidebar');

            const breadcrumb = ancestors.map((a, i) => {
                const isLast = i === ancestors.length - 1;
                return `<span onclick="selectNode('${a.id}')">${escapeHtml(a.name)}</span>` +
                       (isLast ? '' : '<span class="sep"> › </span>');
            }).join('');

            const childrenHtml = children.length > 0
                ? children.map(c => `
                    <div class="detail-child" onclick="selectNode('${c.id}')">
                        <span>${c.has_children || c.data_token_count > 0 ? '📁' : '📄'}</span>
                        <span class="detail-child-name">${escapeHtml(c.name)}</span>
                        <span class="tree-node-count">${c.data_token_count}t</span>
                    </div>
                `).join('')
                : '<div style="color: var(--text-muted); font-size: 0.85rem;">No children</div>';

            const dataHtml = node.data
                ? `<div class="detail-data">${escapeHtml(node.data)}</div>`
                : '<div style="color: var(--text-muted); font-size: 0.85rem; font-style: italic;">No data stored</div>';

            const lastAccessed = new Date(node.last_accessed).toLocaleDateString('en-GB', {
                day: 'numeric', month: 'short', year: 'numeric'
            });

            sidebar.innerHTML = `
                <div class="detail-breadcrumb">${breadcrumb}</div>
                <div class="detail-name">${escapeHtml(node.name)}</div>
                <div class="detail-description">${escapeHtml(node.description)}</div>

                <div class="detail-meta">
                    <div class="detail-meta-item">
                        <div class="detail-meta-label">Accesses</div>
                        <div class="detail-meta-value">${node.access_count}</div>
                    </div>
                    <div class="detail-meta-item">
                        <div class="detail-meta-label">Tokens</div>
                        <div class="detail-meta-value">${node.data_token_count}</div>
                    </div>
                    <div class="detail-meta-item">
                        <div class="detail-meta-label">Last seen</div>
                        <div class="detail-meta-value">${lastAccessed}</div>
                    </div>
                    <div class="detail-meta-item">
                        <div class="detail-meta-label">Children</div>
                        <div class="detail-meta-value">${children.length}</div>
                    </div>
                </div>

                <div class="detail-section">
                    <div class="detail-section-title">💾 Data</div>
                    ${dataHtml}
                </div>

                <div class="detail-section">
                    <div class="detail-section-title">📂 Children</div>
                    <div class="detail-children-list">${childrenHtml}</div>
                </div>

                <div class="detail-actions">
                    <button class="detail-action-btn" onclick="editNode('${node.id}')">✏️ Edit</button>
                    <button class="detail-action-btn" onclick="showCreateNodeModal('${node.id}')">➕ Add child</button>
                    ${!PRESET_NODE_IDS.has(node.id) ? `<button class="detail-action-btn delete" onclick="deleteNode('${node.id}')">🗑️ Delete</button>` : ''}
                </div>
            `;
        }

        async function editNode(nodeId) {
            const resp = await fetch('/api/graph/node/' + nodeId);
            const { node } = await resp.json();

            const sidebar = document.getElementById('detail-sidebar');
            sidebar.innerHTML = `
                <div class="detail-name">✏️ Edit Node</div>
                <div class="modal-field">
                    <label>Name</label>
                    <input type="text" class="detail-edit-field" id="edit-name" value="${escapeHtml(node.name)}" />
                </div>
                <div class="modal-field">
                    <label>Description</label>
                    <textarea class="detail-edit-field" id="edit-desc" rows="3">${escapeHtml(node.description)}</textarea>
                </div>
                <div class="modal-field">
                    <label>Data</label>
                    <textarea class="detail-edit-field" id="edit-data" rows="8">${escapeHtml(node.data)}</textarea>
                </div>
                <div class="detail-actions">
                    <button class="detail-action-btn" onclick="saveNodeEdit('${nodeId}')" style="background: var(--accent-glow); border-color: var(--accent-primary); color: var(--accent-secondary);">💾 Save</button>
                    <button class="detail-action-btn" onclick="selectNode('${nodeId}')">Cancel</button>
                </div>
            `;
        }

        async function saveNodeEdit(nodeId) {
            const name = document.getElementById('edit-name').value.trim();
            const description = document.getElementById('edit-desc').value.trim();
            const data = document.getElementById('edit-data').value;

            if (!name) { showToast('Name is required', 'error'); return; }

            try {
                await fetch('/api/graph/node/' + nodeId, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, description, data })
                });
                showToast('Node updated', 'success');
                loadGraphData();
                loadTreeData();
                selectNode(nodeId);
            } catch (e) {
                showToast('Failed to update', 'error');
            }
        }

        async function deleteNode(nodeId) {
            if (!confirm('Delete this node? Children will be orphaned.')) return;

            try {
                await fetch('/api/graph/node/' + nodeId, { method: 'DELETE' });
                showToast('Node deleted', 'success');
                selectedNodeId = null;
                document.getElementById('detail-sidebar').innerHTML =
                    '<div class="detail-empty"><div class="empty-icon">🧠</div><p>Select a node to view its details</p></div>';
                loadGraphData();
                loadTreeData();
            } catch (e) {
                showToast('Failed to delete', 'error');
            }
        }

        function showCreateNodeModal(parentId) {
            // Remove existing modal if any
            const existing = document.querySelector('.modal-overlay');
            if (existing) existing.remove();

            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal">
                    <h3>✨ New Memory Node</h3>
                    <div class="modal-field">
                        <label>Name</label>
                        <input type="text" class="detail-edit-field" id="new-node-name" placeholder="e.g. Work Projects" />
                    </div>
                    <div class="modal-field">
                        <label>Description</label>
                        <textarea class="detail-edit-field" id="new-node-desc" rows="2" placeholder="Brief description of what this node holds…"></textarea>
                    </div>
                    <div class="modal-field">
                        <label>Data (optional)</label>
                        <textarea class="detail-edit-field" id="new-node-data" rows="4" placeholder="Initial memories…"></textarea>
                    </div>
                    <div class="modal-actions">
                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                        <button class="modal-btn primary" id="btn-create-node">Create</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) overlay.remove();
            });

            document.getElementById('btn-create-node').addEventListener('click', async () => {
                const name = document.getElementById('new-node-name').value.trim();
                const description = document.getElementById('new-node-desc').value.trim();
                const data = document.getElementById('new-node-data').value;

                if (!name) { showToast('Name is required', 'error'); return; }

                try {
                    const resp = await fetch('/api/graph/node', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ name, description, data, parent_id: parentId })
                    });
                    const result = await resp.json();
                    overlay.remove();
                    showToast('Node created', 'success');
                    loadGraphData();
                    loadTreeData();
                    if (result.node) selectNode(result.node.id);
                } catch (e) {
                    showToast('Failed to create node', 'error');
                }
            });

            document.getElementById('new-node-name').focus();
        }

        function showImportDiaryModal(firstTime = false) {
            const existing = document.querySelector('.modal-overlay');
            if (existing) existing.remove();

            const title = firstTime
                ? '🧠 Build Your Knowledge Graph'
                : '📥 Import from Diary';
            const description = firstTime
                ? 'You have diary entries that can be imported into the new knowledge graph. '
                  + 'This will extract facts from your conversation history and organise them '
                  + 'into a searchable knowledge base. This may take a while for large diaries.'
                : 'Import all existing diary entries into graph memory. Each diary summary '
                  + 'will be processed through the LLM to extract facts and organise them '
                  + 'into the graph. This may take a while for large diaries.';
            const cancelLabel = firstTime ? 'Not Now' : 'Cancel';

            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal">
                    <h3>${title}</h3>
                    <p style="color: var(--text-secondary); margin-bottom: 16px; line-height: 1.5;">
                        ${description}
                    </p>
                    <div id="import-progress" style="display: none;">
                        <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                            <span id="import-status" style="color: var(--text-secondary); font-size: 0.85em;">Processing…</span>
                            <span id="import-count" style="color: var(--accent-primary); font-size: 0.85em; font-family: var(--font-mono);">0/0</span>
                        </div>
                        <div style="background: var(--bg-tertiary); border-radius: 6px; height: 8px; overflow: hidden;">
                            <div id="import-bar" style="background: var(--accent-primary); height: 100%; width: 0%; transition: width 0.3s ease; border-radius: 6px;"></div>
                        </div>
                        <div id="import-log" style="margin-top: 12px; max-height: 200px; overflow-y: auto; font-size: 0.8em; font-family: var(--font-mono); color: var(--text-muted); line-height: 1.6;"></div>
                    </div>
                    <div class="modal-actions" id="import-actions">
                        <button class="modal-btn secondary" id="btn-cancel-import">${cancelLabel}</button>
                        <button class="modal-btn primary" id="btn-start-import">Start Import</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            const dismiss = () => overlay.remove();
            document.getElementById('btn-cancel-import').addEventListener('click', dismiss);
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay && !overlay.dataset.importing) dismiss();
            });

            document.getElementById('btn-start-import').addEventListener('click', async () => {
                overlay.dataset.importing = 'true';
                document.getElementById('import-progress').style.display = 'block';
                document.getElementById('btn-start-import').disabled = true;
                document.getElementById('btn-start-import').textContent = 'Importing…';

                try {
                    const resp = await fetch('/api/graph/import-diary', { method: 'POST' });
                    const reader = resp.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';

                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;

                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\n');
                        buffer = lines.pop();

                        for (const line of lines) {
                            if (!line.trim()) continue;
                            try {
                                const msg = JSON.parse(line);
                                if (msg.type === 'start') {
                                    document.getElementById('import-count').textContent = `0/${msg.total}`;
                                } else if (msg.type === 'progress') {
                                    const pct = Math.round((msg.processed / msg.total) * 100);
                                    document.getElementById('import-bar').style.width = pct + '%';
                                    document.getElementById('import-count').textContent = `${msg.processed}/${msg.total}`;
                                    document.getElementById('import-status').textContent = `Processing ${msg.date}…`;
                                    const log = document.getElementById('import-log');
                                    const icon = msg.error ? '❌' : '📅';
                                    const detail = msg.error ? `error: ${msg.error}` : `${msg.facts} fact${msg.facts !== 1 ? 's' : ''}`;
                                    log.innerHTML += `<div>${icon} ${msg.date} — ${detail}</div>`;
                                    log.scrollTop = log.scrollHeight;
                                } else if (msg.type === 'complete') {
                                    document.getElementById('import-status').textContent = msg.message;
                                    document.getElementById('import-bar').style.width = '100%';
                                    document.getElementById('import-actions').innerHTML = `
                                        <button class="modal-btn primary" onclick="this.closest('.modal-overlay').remove()">Done</button>
                                    `;
                                    delete overlay.dataset.importing;
                                    diaryImportDone = true;
                                    loadGraphData();
                                    loadTreeData();
                                    loadStats();
                                    showToast('Diary import complete', 'success');
                                } else if (msg.type === 'error') {
                                    document.getElementById('import-status').textContent = 'Error: ' + msg.message;
                                    document.getElementById('import-actions').innerHTML = `
                                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                                    `;
                                    delete overlay.dataset.importing;
                                    showToast('Import failed', 'error');
                                }
                            } catch (e) { /* skip malformed lines */ }
                        }
                    }
                } catch (e) {
                    document.getElementById('import-status').textContent = 'Connection error: ' + e.message;
                    document.getElementById('import-actions').innerHTML = `
                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                    `;
                    delete overlay.dataset.importing;
                    showToast('Import failed', 'error');
                }
            });
        }

        function showConsolidateAllModal() {
            const existing = document.querySelector('.modal-overlay');
            if (existing) existing.remove();

            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal">
                    <h3>🧹 Consolidate All Nodes</h3>
                    <p style="color: var(--text-secondary); margin-bottom: 16px; line-height: 1.5;">
                        Re-run the merge prompt over every populated node. Dedupes near-duplicate lines, drops contradictions, folds repeated activities into patterns, and prunes common knowledge. Useful after a prompt change to back-fill the new rules across historical data. Cannot be undone.
                    </p>
                    <div id="consolidate-progress" style="display: none;">
                        <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                            <span id="consolidate-status" style="color: var(--text-secondary); font-size: 0.85em;">Processing…</span>
                            <span id="consolidate-count" style="color: var(--accent-primary); font-size: 0.85em; font-family: var(--font-mono);">0 nodes</span>
                        </div>
                        <div style="background: var(--bg-tertiary); border-radius: 6px; height: 8px; overflow: hidden;">
                            <div id="consolidate-bar" style="background: var(--accent-primary); height: 100%; width: 0%; transition: width 0.3s ease; border-radius: 6px;"></div>
                        </div>
                        <div id="consolidate-log" style="margin-top: 12px; max-height: 200px; overflow-y: auto; font-size: 0.8em; font-family: var(--font-mono); color: var(--text-muted); line-height: 1.6;"></div>
                    </div>
                    <div class="modal-actions" id="consolidate-actions">
                        <button class="modal-btn secondary" id="btn-cancel-consolidate">Cancel</button>
                        <button class="modal-btn primary" id="btn-start-consolidate">Start</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            const dismiss = () => overlay.remove();
            document.getElementById('btn-cancel-consolidate').addEventListener('click', dismiss);
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay && !overlay.dataset.consolidating) dismiss();
            });

            document.getElementById('btn-start-consolidate').addEventListener('click', async () => {
                overlay.dataset.consolidating = 'true';
                document.getElementById('consolidate-progress').style.display = 'block';
                document.getElementById('btn-start-consolidate').disabled = true;
                document.getElementById('btn-start-consolidate').textContent = 'Consolidating…';

                try {
                    const resp = await fetch('/api/graph/consolidate-all', { method: 'POST' });
                    const reader = resp.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';
                    let nodeCount = 0;
                    let totalNodes = 0;

                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;

                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\n');
                        buffer = lines.pop();

                        for (const line of lines) {
                            if (!line.trim()) continue;
                            try {
                                const msg = JSON.parse(line);
                                if (msg.type === 'start') {
                                    totalNodes = msg.total || 0;
                                    document.getElementById('consolidate-count').textContent = `0 / ${totalNodes} node${totalNodes !== 1 ? 's' : ''}`;
                                } else if (msg.type === 'progress') {
                                    nodeCount++;
                                    const countLabel = totalNodes
                                        ? `${nodeCount} / ${totalNodes} node${totalNodes !== 1 ? 's' : ''}`
                                        : `${nodeCount} node${nodeCount !== 1 ? 's' : ''}`;
                                    document.getElementById('consolidate-count').textContent = countLabel;
                                    document.getElementById('consolidate-status').textContent = `Consolidating ${msg.node}…`;
                                    const log = document.getElementById('consolidate-log');
                                    const arrow = msg.delta < 0 ? '⬇️' : (msg.delta > 0 ? '⬆️' : '➖');
                                    log.innerHTML += `<div>${arrow} ${msg.node} — ${msg.before} → ${msg.after} lines (Δ${msg.delta})</div>`;
                                    log.scrollTop = log.scrollHeight;
                                    // Real progress when the total is known; fall back to indeterminate pulse otherwise.
                                    const pct = totalNodes
                                        ? Math.min(100, Math.round((nodeCount / totalNodes) * 100))
                                        : 50 + (nodeCount % 2) * 50;
                                    document.getElementById('consolidate-bar').style.width = pct + '%';
                                } else if (msg.type === 'complete') {
                                    document.getElementById('consolidate-bar').style.width = '100%';
                                    document.getElementById('consolidate-status').textContent = `Done — ${msg.nodes} node${msg.nodes !== 1 ? 's' : ''}, ${msg.total_before} → ${msg.total_after} lines (Δ${msg.total_delta})`;
                                    document.getElementById('consolidate-actions').innerHTML = `
                                        <button class="modal-btn primary" onclick="this.closest('.modal-overlay').remove()">Done</button>
                                    `;
                                    delete overlay.dataset.consolidating;
                                    loadGraphData();
                                    loadTreeData();
                                    loadStats();
                                    showToast('Graph consolidated', 'success');
                                } else if (msg.type === 'error') {
                                    document.getElementById('consolidate-status').textContent = 'Error: ' + msg.message;
                                    document.getElementById('consolidate-bar').style.width = '0%';
                                    document.getElementById('consolidate-actions').innerHTML = `
                                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                                    `;
                                    delete overlay.dataset.consolidating;
                                    showToast('Consolidation failed', 'error');
                                }
                            } catch (e) { /* skip malformed lines */ }
                        }
                    }
                } catch (e) {
                    document.getElementById('consolidate-status').textContent = 'Connection error: ' + e.message;
                    // Reset the bar so a half-filled UI doesn't linger next to an error message.
                    document.getElementById('consolidate-bar').style.width = '0%';
                    document.getElementById('consolidate-actions').innerHTML = `
                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                    `;
                    delete overlay.dataset.consolidating;
                    showToast('Consolidation failed', 'error');
                }
            });
        }

        function showScrubDeflectionsModal() {
            const existing = document.querySelector('.modal-overlay');
            if (existing) existing.remove();

            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            // Body copy is intentionally explicit about *what stays* and
            // *what is removed*. Users have correctly worried about
            // "clean" buttons quietly destroying data — say exactly what
            // happens so the action is unsurprising.
            overlay.innerHTML = `
                <div class="modal">
                    <h3>🧹 Clean up deflection narration</h3>
                    <p style="color: var(--text-secondary); margin-bottom: 12px; line-height: 1.5;">
                        Asks the chat model to rewrite each old diary entry, removing only
                        sentences that narrate the assistant's failures (for example
                        "the assistant could not…", "offered to search…", "did not have
                        information"). The rest of each entry stays verbatim.
                    </p>
                    <p style="color: var(--text-secondary); margin-bottom: 16px; line-height: 1.5;">
                        If a summary is <em>entirely</em> deflection narration it is left as-is rather
                        than emptied. No diary entries are deleted. Requires the chat model
                        to be running. Cannot be undone.
                    </p>
                    <div id="scrub-progress" style="display: none;">
                        <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                            <span id="scrub-status" style="color: var(--text-secondary); font-size: 0.85em;">Processing…</span>
                            <span id="scrub-count" style="color: var(--accent-primary); font-size: 0.85em; font-family: var(--font-mono);">0 entries</span>
                        </div>
                        <div style="background: var(--bg-tertiary); border-radius: 6px; height: 8px; overflow: hidden;">
                            <div id="scrub-bar" style="background: var(--accent-primary); height: 100%; width: 0%; transition: width 0.3s ease; border-radius: 6px;"></div>
                        </div>
                        <div id="scrub-log" style="margin-top: 12px; max-height: 200px; overflow-y: auto; font-size: 0.8em; font-family: var(--font-mono); color: var(--text-muted); line-height: 1.6;"></div>
                    </div>
                    <div class="modal-actions" id="scrub-actions">
                        <button class="modal-btn secondary" id="btn-cancel-scrub">Cancel</button>
                        <button class="modal-btn primary" id="btn-start-scrub">Start</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            const dismiss = () => overlay.remove();
            document.getElementById('btn-cancel-scrub').addEventListener('click', dismiss);
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay && !overlay.dataset.scrubbing) dismiss();
            });

            document.getElementById('btn-start-scrub').addEventListener('click', async () => {
                overlay.dataset.scrubbing = 'true';
                document.getElementById('scrub-progress').style.display = 'block';
                // The sweep is one synchronous LLM call per row; on a
                // multi-year diary that's many minutes. The user must
                // be able to bail out without closing the browser. When
                // the AbortController fires, the fetch reader rejects
                // with AbortError and the Flask generator gets a closed
                // pipe on its next yield, ending the sweep cleanly. Any
                // rows already rewritten stay rewritten — partial
                // progress is the design (the bulk sweep is idempotent,
                // so a re-run picks up where this run stopped).
                const controller = new AbortController();
                let processed = 0;
                let totalRows = 0;
                document.getElementById('scrub-actions').innerHTML = `
                    <button class="modal-btn secondary" id="btn-abort-scrub">Abort</button>
                `;
                document.getElementById('btn-abort-scrub').addEventListener('click', () => {
                    controller.abort();
                });

                try {
                    const resp = await fetch('/api/diary/scrub-deflections', {
                        method: 'POST',
                        signal: controller.signal,
                    });
                    const reader = resp.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';

                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;

                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\n');
                        buffer = lines.pop();

                        for (const line of lines) {
                            if (!line.trim()) continue;
                            try {
                                const msg = JSON.parse(line);
                                if (msg.type === 'start') {
                                    totalRows = msg.total || 0;
                                    document.getElementById('scrub-count').textContent =
                                        `0 / ${totalRows} entr${totalRows === 1 ? 'y' : 'ies'}`;
                                } else if (msg.type === 'progress') {
                                    processed = msg.processed;
                                    const countLabel = totalRows
                                        ? `${processed} / ${totalRows} entr${totalRows === 1 ? 'y' : 'ies'}`
                                        : `${processed} entr${processed === 1 ? 'y' : 'ies'}`;
                                    document.getElementById('scrub-count').textContent = countLabel;
                                    document.getElementById('scrub-status').textContent = `Cleaning ${msg.date_utc}…`;
                                    const log = document.getElementById('scrub-log');
                                    let icon, detail;
                                    if (msg.error) {
                                        icon = '❌';
                                        detail = `error: ${msg.error}`;
                                    } else if (msg.would_empty) {
                                        // Model wanted to empty the row; kept original instead.
                                        icon = '🛡️';
                                        detail = 'would have emptied · kept original';
                                    } else if (msg.rewritten) {
                                        const delta = (msg.chars_before || 0) - (msg.chars_after || 0);
                                        icon = '🧹';
                                        detail = `rewritten · ${delta} chars removed`;
                                    } else {
                                        icon = '➖';
                                        detail = 'clean';
                                    }
                                    // Use textContent on a constructed node
                                    // rather than innerHTML+=. The values
                                    // come from server-controlled JSON, but
                                    // a corrupted DB row could contain a
                                    // malformed date_utc and the endpoint
                                    // surfaces an exception class name on
                                    // error — neither should be able to
                                    // inject markup into the modal log.
                                    const entry = document.createElement('div');
                                    entry.textContent = `${icon} ${msg.date_utc} — ${detail}`;
                                    log.appendChild(entry);
                                    log.scrollTop = log.scrollHeight;
                                    const pct = totalRows
                                        ? Math.min(100, Math.round((processed / totalRows) * 100))
                                        : 50 + (processed % 2) * 50;
                                    document.getElementById('scrub-bar').style.width = pct + '%';
                                } else if (msg.type === 'complete') {
                                    document.getElementById('scrub-bar').style.width = '100%';
                                    const summary = msg.rows === 0
                                        ? 'No diary entries found.'
                                        : `Done — ${msg.rows_rewritten} of ${msg.rows} entr${msg.rows === 1 ? 'y' : 'ies'} rewritten`
                                          + (msg.rows_would_empty ? ` (${msg.rows_would_empty} kept original to avoid emptying)` : '');
                                    document.getElementById('scrub-status').textContent = summary;
                                    document.getElementById('scrub-actions').innerHTML = `
                                        <button class="modal-btn primary" onclick="this.closest('.modal-overlay').remove()">Done</button>
                                    `;
                                    delete overlay.dataset.scrubbing;
                                    loadStats();
                                    loadMemories();
                                    showToast('Diary cleaned', 'success');
                                } else if (msg.type === 'error') {
                                    document.getElementById('scrub-status').textContent = 'Error: ' + msg.message;
                                    document.getElementById('scrub-bar').style.width = '0%';
                                    document.getElementById('scrub-actions').innerHTML = `
                                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                                    `;
                                    delete overlay.dataset.scrubbing;
                                    showToast('Diary clean failed', 'error');
                                }
                            } catch (e) { /* skip malformed lines */ }
                        }
                    }
                } catch (e) {
                    if (e.name === 'AbortError') {
                        // User-initiated abort. Partial progress stays
                        // in the DB (the sweep is per-row idempotent and
                        // re-running picks up where this run stopped).
                        const summary = totalRows
                            ? `Stopped — ${processed} of ${totalRows} entr${totalRows === 1 ? 'y' : 'ies'} processed`
                            : 'Stopped before any entries were processed';
                        document.getElementById('scrub-status').textContent = summary;
                        document.getElementById('scrub-actions').innerHTML = `
                            <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                        `;
                        delete overlay.dataset.scrubbing;
                        loadStats();
                        loadMemories();
                        // No toast on user-initiated abort — the modal
                        // status update communicates the partial result.
                    } else {
                        document.getElementById('scrub-status').textContent = 'Connection error: ' + e.message;
                        document.getElementById('scrub-bar').style.width = '0%';
                        document.getElementById('scrub-actions').innerHTML = `
                            <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                        `;
                        delete overlay.dataset.scrubbing;
                        showToast('Diary clean failed', 'error');
                    }
                }
            });
        }

        function showOptimiseTopicsModal() {
            const existing = document.querySelector('.modal-overlay');
            if (existing) existing.remove();

            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal">
                    <h3>🏷️ Optimise tags</h3>
                    <p style="color: var(--text-secondary); margin-bottom: 12px; line-height: 1.5;">
                        Uses the chat model to build a normalised tag taxonomy across all diary entries.
                        Near-synonyms are merged into one canonical form (e.g. "cook" and "cooking"
                        both become "cooking"). Compound tags that cover clearly distinct topics are split.
                    </p>
                    <p style="color: var(--text-secondary); margin-bottom: 16px; line-height: 1.5;">
                        Only the tags are changed — diary text is untouched. No entries are deleted.
                        Requires the chat model to be running. Cannot be undone.
                    </p>
                    <div id="optimise-progress" style="display: none;">
                        <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                            <span id="optimise-status" style="color: var(--text-secondary); font-size: 0.85em;">Processing…</span>
                            <span id="optimise-count" style="color: var(--accent-primary); font-size: 0.85em; font-family: var(--font-mono);">0 entries</span>
                        </div>
                        <div style="background: var(--bg-tertiary); border-radius: 6px; height: 8px; overflow: hidden;">
                            <div id="optimise-bar" style="background: var(--accent-primary); height: 100%; width: 0%; transition: width 0.3s ease; border-radius: 6px;"></div>
                        </div>
                        <div id="optimise-log" style="margin-top: 12px; max-height: 200px; overflow-y: auto; font-size: 0.8em; font-family: var(--font-mono); color: var(--text-muted); line-height: 1.6;"></div>
                    </div>
                    <div class="modal-actions" id="optimise-actions">
                        <button class="modal-btn secondary" id="btn-cancel-optimise">Cancel</button>
                        <button class="modal-btn primary" id="btn-start-optimise">Start</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            const dismiss = () => overlay.remove();
            document.getElementById('btn-cancel-optimise').addEventListener('click', dismiss);
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay && !overlay.dataset.optimising) dismiss();
            });

            document.getElementById('btn-start-optimise').addEventListener('click', async () => {
                overlay.dataset.optimising = 'true';
                document.getElementById('optimise-progress').style.display = 'block';
                document.getElementById('btn-start-optimise').disabled = true;
                document.getElementById('btn-start-optimise').textContent = 'Optimising…';

                try {
                    const resp = await fetch('/api/diary/optimise-topics', { method: 'POST' });
                    const reader = resp.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';
                    let processed = 0;
                    let totalRows = 0;

                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;

                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\n');
                        buffer = lines.pop();

                        for (const line of lines) {
                            if (!line.trim()) continue;
                            try {
                                const msg = JSON.parse(line);
                                if (msg.type === 'start') {
                                    totalRows = msg.total || 0;
                                    document.getElementById('optimise-status').textContent = 'Building tag taxonomy…';
                                    document.getElementById('optimise-count').textContent =
                                        `0 / ${totalRows} entr${totalRows === 1 ? 'y' : 'ies'}`;
                                } else if (msg.type === 'progress') {
                                    processed = msg.processed;
                                    const countLabel = totalRows
                                        ? `${processed} / ${totalRows} entr${totalRows === 1 ? 'y' : 'ies'}`
                                        : `${processed} entr${processed === 1 ? 'y' : 'ies'}`;
                                    document.getElementById('optimise-count').textContent = countLabel;
                                    document.getElementById('optimise-status').textContent = `Applying to ${msg.date_utc}…`;
                                    const log = document.getElementById('optimise-log');
                                    let icon, detail;
                                    if (msg.error) {
                                        icon = '❌';
                                        detail = `error: ${msg.error}`;
                                    } else if (!msg.topics_changed) {
                                        icon = '➖';
                                        detail = 'no change';
                                    } else {
                                        const oldN = msg.old_topic_count || 0;
                                        const newN = msg.new_topic_count || 0;
                                        icon = '🏷️';
                                        detail = newN < oldN
                                            ? `${oldN} → ${newN} tags (merged)`
                                            : newN > oldN
                                                ? `${oldN} → ${newN} tags (split)`
                                                : `${newN} tag${newN === 1 ? '' : 's'} updated`;
                                    }
                                    const entry = document.createElement('div');
                                    entry.textContent = `${icon} ${msg.date_utc} — ${detail}`;
                                    log.appendChild(entry);
                                    log.scrollTop = log.scrollHeight;
                                    const pct = totalRows
                                        ? Math.min(100, Math.round((processed / totalRows) * 100))
                                        : 50 + (processed % 2) * 50;
                                    document.getElementById('optimise-bar').style.width = pct + '%';
                                } else if (msg.type === 'complete') {
                                    document.getElementById('optimise-bar').style.width = '100%';
                                    let summary;
                                    if (msg.rows === 0) {
                                        summary = 'No diary entries found.';
                                    } else {
                                        const parts = [];
                                        if (msg.rows_changed > 0) {
                                            parts.push(`${msg.rows_changed} of ${msg.rows} entr${msg.rows === 1 ? 'y' : 'ies'} updated`);
                                        } else {
                                            parts.push(`${msg.rows} entr${msg.rows === 1 ? 'y' : 'ies'} checked — all tags already optimal`);
                                        }
                                        if (msg.topics_merged > 0) parts.push(`${msg.topics_merged} tag${msg.topics_merged === 1 ? '' : 's'} merged`);
                                        if (msg.topics_expanded > 0) parts.push(`${msg.topics_expanded} tag${msg.topics_expanded === 1 ? '' : 's'} split`);
                                        summary = 'Done — ' + parts.join(', ');
                                    }
                                    document.getElementById('optimise-status').textContent = summary;
                                    document.getElementById('optimise-actions').innerHTML = `
                                        <button class="modal-btn primary" onclick="this.closest('.modal-overlay').remove()">Done</button>
                                    `;
                                    delete overlay.dataset.optimising;
                                    loadStats();
                                    loadTopics();
                                    loadMemories();
                                    showToast('Tags optimised', 'success');
                                } else if (msg.type === 'error') {
                                    document.getElementById('optimise-status').textContent = 'Error: ' + msg.message;
                                    document.getElementById('optimise-bar').style.width = '0%';
                                    document.getElementById('optimise-actions').innerHTML = `
                                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                                    `;
                                    delete overlay.dataset.optimising;
                                    showToast('Tag optimisation failed', 'error');
                                }
                            } catch (e) { /* skip malformed lines */ }
                        }
                    }
                } catch (e) {
                    document.getElementById('optimise-status').textContent = 'Connection error: ' + e.message;
                    document.getElementById('optimise-bar').style.width = '0%';
                    document.getElementById('optimise-actions').innerHTML = `
                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                    `;
                    delete overlay.dataset.optimising;
                    showToast('Tag optimisation failed', 'error');
                }
            });
        }

        // Initial load. Chat is the default tab, so the diary lists load
        // lazily when that tab is opened; stats and topics feed the header
        // and sidebar, which are cheap and always visible.
        loadStats();
        loadTopics();
        loadIdentity();
        chatInput.focus();
