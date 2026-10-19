/**
 * FeedPulse Web App - Core Controller
 * Manages reactive UI state, REST API communication, feeds CRUD, live inspector, and toasts.
 */

const App = (function () {
  // Reactive State
  const state = {
    user: null,
    feeds: [],
    destinations: [],
    stats: {},
    activeTab: 'feeds',
    incTags: [],
    excTags: [],
    searchQuery: '',
    statusFilter: 'all',
  };

  // DOM Elements cache
  let dom = {};

  function init() {
    cacheDom();
    bindEvents();
    loadUserAndStats();
    loadFeeds();
  }

  function cacheDom() {
    dom = {
      // Header & User
      userName: document.getElementById('user-name'),
      userRole: document.getElementById('user-role'),
      userAvatar: document.getElementById('user-avatar'),
      quickCheckBtn: document.getElementById('btn-quick-check'),

      // Stats
      statTotal: document.getElementById('stat-total-feeds'),
      statActive: document.getElementById('stat-active-feeds'),
      statPaused: document.getElementById('stat-paused-feeds'),
      statInterval: document.getElementById('stat-interval'),
      navCount: document.getElementById('nav-feeds-count'),

      // Tabs & Views
      tabBtns: document.querySelectorAll('.tab-btn'),
      views: document.querySelectorAll('.view-panel'),

      // Feeds View
      feedsGrid: document.getElementById('feeds-grid'),
      feedsEmpty: document.getElementById('feeds-empty-state'),
      searchInput: document.getElementById('feeds-search-input'),
      searchClear: document.getElementById('search-clear-btn'),
      statusFilter: document.getElementById('filter-status-select'),
      refreshBtn: document.getElementById('btn-refresh-feeds'),
      emptyAddBtn: document.getElementById('btn-empty-add'),

      // Add Feed Form
      addForm: document.getElementById('add-feed-form'),
      addUrl: document.getElementById('add-feed-url'),
      addTitle: document.getElementById('add-feed-title'),
      validateRssBtn: document.getElementById('btn-validate-rss'),
      urlValidationHint: document.getElementById('add-url-validation-hint'),
      incInput: document.getElementById('add-inc-keywords'),
      incContainer: document.getElementById('inc-tags-container'),
      excInput: document.getElementById('add-exc-keywords'),
      excContainer: document.getElementById('exc-tags-container'),
      presetChips: document.querySelectorAll('.preset-chip'),
      addMinSize: document.getElementById('add-min-size'),
      addMinUnit: document.getElementById('add-min-size-unit'),
      addMaxSize: document.getElementById('add-max-size'),
      addMaxUnit: document.getElementById('add-max-size-unit'),
      addDestSelect: document.getElementById('add-destination'),
      addCaseSensitive: document.getElementById('add-case-sensitive'),

      // Live Inspector
      inspectUrlInput: document.getElementById('inspect-url-input'),
      runInspectBtn: document.getElementById('btn-run-inspect'),
      inspectResults: document.getElementById('inspect-results'),

      // Settings
      toggleRichText: document.getElementById('toggle-rich-text'),
      settingsIntervalVal: document.getElementById('settings-interval-val'),
      settingsDedupVal: document.getElementById('settings-dedup-val'),
      settingsUserId: document.getElementById('settings-user-id'),
      settingsUserRole: document.getElementById('settings-user-role'),
      forceCheckSettingsBtn: document.getElementById('btn-force-check-settings'),

      // Edit Modal
      editModal: document.getElementById('edit-modal-backdrop'),
      editForm: document.getElementById('edit-feed-form'),
      editCloseBtn: document.getElementById('btn-close-edit-modal'),
      editCancelBtn: document.getElementById('btn-cancel-edit'),
      editFeedId: document.getElementById('edit-feed-id'),
      editTargetId: document.getElementById('edit-target-id'),
      editFeedTitle: document.getElementById('edit-feed-title'),
      editFeedUrl: document.getElementById('edit-feed-url'),
      editIncKeywords: document.getElementById('edit-inc-keywords'),
      editExcKeywords: document.getElementById('edit-exc-keywords'),
      editCaseSensitive: document.getElementById('edit-case-sensitive'),

      // Toasts
      toastContainer: document.getElementById('toast-container'),
    };
  }

  function bindEvents() {
    // Tab switching
    dom.tabBtns.forEach(btn => {
      btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    if (dom.emptyAddBtn) {
      dom.emptyAddBtn.addEventListener('click', () => switchTab('add'));
    }

    // Quick Check button
    dom.quickCheckBtn.addEventListener('click', triggerSystemCheck);
    if (dom.forceCheckSettingsBtn) {
      dom.forceCheckSettingsBtn.addEventListener('click', triggerSystemCheck);
    }

    // Feeds search & filters
    dom.searchInput.addEventListener('input', e => {
      state.searchQuery = e.target.value.trim().toLowerCase();
      dom.searchClear.style.display = state.searchQuery ? 'block' : 'none';
      renderFeeds();
    });

    dom.searchClear.addEventListener('click', () => {
      dom.searchInput.value = '';
      state.searchQuery = '';
      dom.searchClear.style.display = 'none';
      renderFeeds();
    });

    dom.statusFilter.addEventListener('change', e => {
      state.statusFilter = e.target.value;
      renderFeeds();
    });

    dom.refreshBtn.addEventListener('click', () => {
      TelegramBridge.hapticImpact('light');
      loadFeeds(true);
    });

    // Tag Inputs for Add Feed
    setupTagInput(dom.incInput, dom.incContainer, state.incTags, 'chip-inc');
    setupTagInput(dom.excInput, dom.excContainer, state.excTags, 'chip-exc');

    // Preset Chips
    dom.presetChips.forEach(chip => {
      chip.addEventListener('click', () => {
        TelegramBridge.hapticImpact('light');
        const type = chip.dataset.type;
        const val = chip.dataset.val;
        if (type === 'inc' && !state.incTags.includes(val)) {
          state.incTags.push(val);
          renderTags(dom.incContainer, dom.incInput, state.incTags, 'chip-inc');
        } else if (type === 'exc' && !state.excTags.includes(val)) {
          state.excTags.push(val);
          renderTags(dom.excContainer, dom.excInput, state.excTags, 'chip-exc');
        }
      });
    });

    // Validate RSS URL button
    dom.validateRssBtn.addEventListener('click', validateRssUrl);

    // Add Feed Submit
    dom.addForm.addEventListener('submit', handleAddFeedSubmit);

    // Live Inspector
    dom.runInspectBtn.addEventListener('click', () => {
      const url = dom.inspectUrlInput.value.trim();
      if (!url) {
        showToast('Please enter an RSS URL to inspect', 'error');
        return;
      }
      inspectLiveFeed(null, url);
    });

    // Settings Toggle Rich Text
    dom.toggleRichText.addEventListener('change', handleToggleRichText);

    // Edit Modal events
    dom.editCloseBtn.addEventListener('click', closeEditModal);
    dom.editCancelBtn.addEventListener('click', closeEditModal);
    dom.editForm.addEventListener('submit', handleEditFeedSubmit);
  }

  // ---------------- Navigation ----------------
  function switchTab(tabId) {
    TelegramBridge.hapticImpact('light');
    state.activeTab = tabId;

    dom.tabBtns.forEach(btn => {
      const isActive = btn.dataset.tab === tabId;
      btn.classList.toggle('active', isActive);
      btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });

    dom.views.forEach(view => {
      view.classList.toggle('active', view.id === `view-${tabId}`);
    });

    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // ---------------- Tag Input Helper ----------------
  function setupTagInput(input, container, tagList, chipClass) {
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ',') {
        e.preventDefault();
        const val = input.value.trim().replace(/^,+|,+$/g, '');
        if (val && !tagList.includes(val)) {
          tagList.push(val);
          renderTags(container, input, tagList, chipClass);
          input.value = '';
        }
      } else if (e.key === 'Backspace' && !input.value && tagList.length > 0) {
        tagList.pop();
        renderTags(container, input, tagList, chipClass);
      }
    });

    // Also handle comma pasting
    input.addEventListener('blur', () => {
      const val = input.value.trim().replace(/^,+|,+$/g, '');
      if (val && !tagList.includes(val)) {
        tagList.push(val);
        renderTags(container, input, tagList, chipClass);
        input.value = '';
      }
    });
  }

  function renderTags(container, input, tagList, chipClass) {
    // Remove existing badges
    container.querySelectorAll('.tag-badge').forEach(b => b.remove());

    tagList.forEach((tag, idx) => {
      const badge = document.createElement('span');
      badge.className = `tag-badge ${chipClass === 'chip-exc' ? 'tag-badge-danger' : ''}`;
      badge.innerHTML = `
        ${escapeHtml(tag)}
        <span class="tag-remove" data-idx="${idx}">&times;</span>
      `;
      badge.querySelector('.tag-remove').addEventListener('click', e => {
        e.stopPropagation();
        tagList.splice(idx, 1);
        renderTags(container, input, tagList, chipClass);
      });
      container.insertBefore(badge, input);
    });
  }

  // ---------------- API Calls ----------------

  async function loadUserAndStats() {
    // Pre-populate with Telegram SDK user info immediately
    const tgUser = TelegramBridge.getUser();
    if (tgUser && dom.userName) {
      dom.userName.textContent = tgUser.first_name || 'User';
      dom.userAvatar.textContent = (tgUser.first_name || 'U').charAt(0).toUpperCase();
    }

    try {
      const res = await fetch('/api/me', { credentials: 'omit', headers: TelegramBridge.getAuthHeaders() });
      if (!res.ok) throw new Error('Failed to load profile');
      const data = await res.json();
      state.user = data.user;
      state.destinations = data.destinations || [];
      state.stats = data.stats || {};

      // Populate Header
      dom.userName.textContent = state.user.first_name || tgUser?.first_name || 'User';
      dom.userRole.textContent = data.role || 'User';
      dom.userAvatar.textContent = (state.user.first_name || tgUser?.first_name || 'U').charAt(0).toUpperCase();

      // Populate Settings View
      dom.settingsUserId.textContent = state.user.id || '---';
      dom.settingsUserRole.textContent = data.role || 'User';
      dom.settingsIntervalVal.textContent = `${state.stats.poll_interval_seconds || 120} seconds`;
      dom.toggleRichText.checked = Boolean(state.stats.is_rich_text);

      // Populate Destination Select dropdown
      dom.addDestSelect.innerHTML = '';
      state.destinations.forEach(dest => {
        const opt = document.createElement('option');
        opt.value = dest.id;
        opt.textContent = dest.title;
        dom.addDestSelect.appendChild(opt);
      });

      updateStatsRibbon();
    } catch (err) {
      console.warn('Profile load warning:', err);
    }
  }

  async function loadFeeds(showToastNotice = false) {
    try {
      const res = await fetch('/api/feeds', { credentials: 'omit', headers: TelegramBridge.getAuthHeaders() });
      if (!res.ok) throw new Error('Failed to load feeds');
      const data = await res.json();
      state.feeds = data.feeds || [];
      renderFeeds();
      updateStatsRibbon();
      if (showToastNotice) {
        showToast('Feeds updated', 'success');
      }
    } catch (err) {
      dom.feedsGrid.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">⚠️</div>
          <h3>Failed to Load Feeds</h3>
          <p>${escapeHtml(err.message)}</p>
        </div>
      `;
    }
  }

  function updateStatsRibbon() {
    const total = state.feeds.length;
    const active = state.feeds.filter(f => !f.is_paused).length;
    const paused = state.feeds.filter(f => f.is_paused).length;
    const interval = state.stats.poll_interval_seconds ? `${state.stats.poll_interval_seconds}s` : '120s';

    dom.statTotal.textContent = total;
    dom.statActive.textContent = active;
    dom.statPaused.textContent = paused;
    dom.statInterval.textContent = interval;
    dom.navCount.textContent = total;
  }

  // ---------------- Rendering Feeds ----------------

  function renderFeeds() {
    let list = [...state.feeds];

    // Filter by search query
    if (state.searchQuery) {
      const q = state.searchQuery;
      list = list.filter(f => {
        const title = (f.title || '').toLowerCase();
        const url = (f.url || '').toLowerCase();
        const inc = (f.filter_keywords || '').toLowerCase();
        const exc = (f.filter_exclude || '').toLowerCase();
        return title.includes(q) || url.includes(q) || inc.includes(q) || exc.includes(q);
      });
    }

    // Filter by status
    if (state.statusFilter === 'active') {
      list = list.filter(f => !f.is_paused);
    } else if (state.statusFilter === 'paused') {
      list = list.filter(f => f.is_paused);
    }

    if (list.length === 0) {
      dom.feedsGrid.innerHTML = '';
      dom.feedsEmpty.style.display = 'flex';
      return;
    }

    dom.feedsEmpty.style.display = 'none';
    dom.feedsGrid.innerHTML = '';

    list.forEach(feed => {
      const card = createFeedCard(feed);
      dom.feedsGrid.appendChild(card);
    });
  }

  function createFeedCard(feed) {
    const fid = feed.feed_id || feed._id;
    const title = feed.title || 'Untitled Feed';
    const isPaused = Boolean(feed.is_paused);
    const url = feed.url || '';
    const incFilter = feed.filter_keywords || '';
    const excFilter = feed.filter_exclude || '';
    const target = feed.target_id || '';
    const isPersonal = target && !target.startsWith('-100');

    const card = document.createElement('div');
    card.className = `feed-card ${isPaused ? 'paused' : 'active'}`;
    card.id = `feed-card-${fid}`;

    card.innerHTML = `
      <div class="feed-card-header">
        <div class="feed-title-block">
          <span class="feed-id-badge">FEED #${fid}</span>
          <h3 class="feed-title">${escapeHtml(title)}</h3>
        </div>
        <span class="feed-status-badge ${isPaused ? 'paused' : 'active'}">
          ${isPaused ? '⏸️ Paused' : '🟢 Active'}
        </span>
      </div>

      <div class="feed-url-row" title="${escapeHtml(url)}">
        🔗 ${escapeHtml(url)}
      </div>

      <div class="feed-meta-row">
        <span class="meta-chip">
          ${isPersonal ? '👤 Personal DM' : '👥 Group Chat'}
        </span>
        ${incFilter ? `<span class="meta-chip chip-inc" title="Include keywords">🔍 +${escapeHtml(incFilter)}</span>` : ''}
        ${excFilter ? `<span class="meta-chip chip-exc" title="Exclude keywords">🚫 -${escapeHtml(excFilter)}</span>` : ''}
        ${feed.max_size ? `<span class="meta-chip">📏 Max: ${formatBytes(feed.max_size)}</span>` : ''}
      </div>

      <div class="feed-card-actions">
        <button class="btn-sm btn-secondary btn-inspect" data-id="${fid}" data-url="${escapeHtml(url)}">
          🔍 Live Preview
        </button>
        <button class="btn-sm btn-secondary btn-pause" data-id="${fid}" data-paused="${isPaused}">
          ${isPaused ? '▶️ Resume' : '⏸️ Pause'}
        </button>
        <button class="btn-sm btn-secondary btn-edit" data-id="${fid}">
          ✏️ Edit
        </button>
        <button class="btn-sm btn-danger-ghost btn-delete" data-id="${fid}" data-target="${target}">
          🗑️ Delete
        </button>
      </div>
    `;

    // Action handlers
    card.querySelector('.btn-inspect').addEventListener('click', () => {
      TelegramBridge.hapticImpact('light');
      dom.inspectUrlInput.value = url;
      switchTab('inspect');
      inspectLiveFeed(fid, url);
    });

    card.querySelector('.btn-pause').addEventListener('click', () => {
      toggleFeedPause(fid, !isPaused, target);
    });

    card.querySelector('.btn-edit').addEventListener('click', () => {
      openEditModal(feed);
    });

    card.querySelector('.btn-delete').addEventListener('click', () => {
      confirmDeleteFeed(fid, title, target);
    });

    return card;
  }

  // ---------------- Actions ----------------

  async function toggleFeedPause(feedId, setPaused, targetId) {
    TelegramBridge.hapticImpact('medium');
    try {
      const res = await fetch(`/api/feeds/${feedId}/pause`, {
        method: 'POST',
        credentials: 'omit',
        headers: TelegramBridge.getAuthHeaders(),
        body: JSON.stringify({ paused: setPaused, target_id: targetId }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to update feed');

      showToast(setPaused ? 'Feed paused' : 'Feed resumed', 'success');
      TelegramBridge.hapticNotification('success');
      loadFeeds();
    } catch (err) {
      showToast(err.message, 'error');
      TelegramBridge.hapticNotification('error');
    }
  }

  async function confirmDeleteFeed(feedId, title, targetId) {
    TelegramBridge.hapticImpact('medium');
    if (!confirm(`Are you sure you want to delete feed "${title}"?`)) return;

    try {
      const res = await fetch(`/api/feeds/${feedId}?target_id=${encodeURIComponent(targetId || '')}`, {
        method: 'DELETE',
        credentials: 'omit',
        headers: TelegramBridge.getAuthHeaders(),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to delete feed');

      showToast('Feed deleted successfully', 'success');
      TelegramBridge.hapticNotification('success');
      loadFeeds();
    } catch (err) {
      showToast(err.message, 'error');
      TelegramBridge.hapticNotification('error');
    }
  }

  async function validateRssUrl() {
    const url = dom.addUrl.value.trim();
    if (!url) {
      showToast('Please enter an RSS URL first', 'error');
      return;
    }

    dom.validateRssBtn.disabled = true;
    dom.validateRssBtn.textContent = '⏳ Testing...';

    try {
      const res = await fetch(`/api/inspect?url=${encodeURIComponent(url)}`, {
        credentials: 'omit',
        headers: TelegramBridge.getAuthHeaders(),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Feed check failed');

      dom.urlValidationHint.innerHTML = `✅ <b>Valid Feed:</b> Found ${data.total_items} items (Channel: "${escapeHtml(data.title)}").`;
      dom.urlValidationHint.style.color = 'var(--green-success)';
      if (!dom.addTitle.value && data.title) {
        dom.addTitle.value = data.title;
      }
      showToast('Feed URL verified!', 'success');
      TelegramBridge.hapticNotification('success');
    } catch (err) {
      dom.urlValidationHint.innerHTML = `❌ <b>Error:</b> ${escapeHtml(err.message)}`;
      dom.urlValidationHint.style.color = 'var(--rose-danger)';
      showToast(err.message, 'error');
      TelegramBridge.hapticNotification('error');
    } finally {
      dom.validateRssBtn.disabled = false;
      dom.validateRssBtn.textContent = '⚡ Test';
    }
  }

  async function handleAddFeedSubmit(e) {
    e.preventDefault();
    const url = dom.addUrl.value.trim();
    if (!url) {
      showToast('Feed URL is required', 'error');
      return;
    }

    const title = dom.addTitle.value.trim();
    const targetId = dom.addDestSelect.value;
    const caseSensitive = dom.addCaseSensitive.checked;

    // Calculate byte sizes
    let minSize = null;
    if (dom.addMinSize.value) {
      const val = parseFloat(dom.addMinSize.value);
      minSize = dom.addMinUnit.value === 'GB' ? Math.round(val * 1024 * 1024 * 1024) : Math.round(val * 1024 * 1024);
    }

    let maxSize = null;
    if (dom.addMaxSize.value) {
      const val = parseFloat(dom.addMaxSize.value);
      maxSize = dom.addMaxUnit.value === 'GB' ? Math.round(val * 1024 * 1024 * 1024) : Math.round(val * 1024 * 1024);
    }

    const payload = {
      url,
      title: title || undefined,
      target_id: targetId || undefined,
      filter_keywords: state.incTags.join(', ') || undefined,
      filter_exclude: state.excTags.join(', ') || undefined,
      case_sensitive: caseSensitive,
      min_size: minSize,
      max_size: maxSize,
    };

    const submitBtn = document.getElementById('btn-submit-add');
    submitBtn.disabled = true;
    submitBtn.textContent = '⏳ Subscribing...';

    try {
      const res = await fetch('/api/feeds/add', {
        method: 'POST',
        credentials: 'omit',
        headers: TelegramBridge.getAuthHeaders(),
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to add feed');

      showToast(`Subscribed to "${data.title}" successfully!`, 'success');
      TelegramBridge.hapticNotification('success');

      // Reset form
      dom.addForm.reset();
      state.incTags = [];
      state.excTags = [];
      renderTags(dom.incContainer, dom.incInput, state.incTags, 'chip-inc');
      renderTags(dom.excContainer, dom.excInput, state.excTags, 'chip-exc');

      // Switch to subscriptions list & reload
      switchTab('feeds');
      loadFeeds();
    } catch (err) {
      showToast(err.message, 'error');
      TelegramBridge.hapticNotification('error');
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = '⚡ Subscribe to Feed';
    }
  }

  // ---------------- Live Inspector ----------------

  async function inspectLiveFeed(feedId, url) {
    dom.inspectResults.innerHTML = `
      <div class="loading-state">
        <div class="spinner"></div>
        <p>Fetching real-time RSS entries & torrent metadata...</p>
      </div>
    `;

    try {
      const endpoint = feedId ? `/api/feeds/${feedId}/items` : `/api/inspect?url=${encodeURIComponent(url)}`;
      const res = await fetch(endpoint, {
        credentials: 'omit',
        headers: TelegramBridge.getAuthHeaders(),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to fetch items');

      if (!data.items || data.items.length === 0) {
        dom.inspectResults.innerHTML = `
          <div class="empty-state">
            <div class="empty-icon">📭</div>
            <h3>No Items Found</h3>
            <p>The feed returned 0 entries or could not be parsed.</p>
          </div>
        `;
        return;
      }

      dom.inspectResults.innerHTML = `
        <div class="card-glass" style="margin-bottom: 12px; padding: 12px 16px;">
          <strong>📡 ${escapeHtml(data.title)}</strong>
          <span style="color: var(--text-muted); font-size: 12px; margin-left: 8px;">(${data.items.length} items parsed)</span>
        </div>
      `;

      data.items.forEach(item => {
        const itemCard = document.createElement('div');
        itemCard.className = 'inspect-item-card';

        const sizeLabel = item.file_size ? formatBytes(item.file_size) : (item.size_bytes ? formatBytes(item.size_bytes) : null);
        const hasSeeds = item.seeds !== null && item.seeds !== undefined && item.seeds !== '?';

        itemCard.innerHTML = `
          <h4 class="item-title">${escapeHtml(item.title)}</h4>
          <div class="item-meta-pills">
            ${sizeLabel ? `<span class="meta-chip">📦 ${sizeLabel}</span>` : ''}
            ${hasSeeds ? `<span class="meta-chip" style="color: var(--green-success);">🔺 ${item.seeds} seeds</span>` : ''}
            ${item.category ? `<span class="meta-chip">🏷️ ${escapeHtml(item.category)}</span>` : ''}
            ${item.published ? `<span class="meta-chip">📅 ${escapeHtml(item.published)}</span>` : ''}
          </div>
          <div class="item-actions">
            ${item.magnet_uri ? `
              <button class="btn-sm btn-primary btn-copy-magnet" data-magnet="${escapeHtml(item.magnet_uri)}">
                🧲 Copy Magnet
              </button>
            ` : ''}
            ${item.torrent_url ? `
              <a href="${escapeHtml(item.torrent_url)}" target="_blank" class="btn-sm btn-secondary" style="text-decoration: none;">
                📥 .torrent
              </a>
            ` : ''}
            ${item.link ? `
              <a href="${escapeHtml(item.link)}" target="_blank" class="btn-sm btn-secondary" style="text-decoration: none;">
                🌐 Details
              </a>
            ` : ''}
          </div>
        `;

        const magnetBtn = itemCard.querySelector('.btn-copy-magnet');
        if (magnetBtn) {
          magnetBtn.addEventListener('click', () => {
            copyToClipboard(item.magnet_uri, 'Magnet link copied to clipboard!');
          });
        }

        dom.inspectResults.appendChild(itemCard);
      });

    } catch (err) {
      dom.inspectResults.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">❌</div>
          <h3>Inspection Failed</h3>
          <p>${escapeHtml(err.message)}</p>
        </div>
      `;
    }
  }

  // ---------------- Edit Modal ----------------

  function openEditModal(feed) {
    TelegramBridge.hapticImpact('light');
    dom.editFeedId.value = feed.feed_id || feed._id;
    dom.editTargetId.value = feed.target_id || '';
    dom.editFeedTitle.value = feed.title || '';
    dom.editFeedUrl.value = feed.url || '';
    dom.editIncKeywords.value = feed.filter_keywords || '';
    dom.editExcKeywords.value = feed.filter_exclude || '';
    dom.editCaseSensitive.checked = Boolean(feed.case_sensitive);

    dom.editModal.style.display = 'flex';
  }

  function closeEditModal() {
    dom.editModal.style.display = 'none';
  }

  async function handleEditFeedSubmit(e) {
    e.preventDefault();
    const feedId = dom.editFeedId.value;
    const payload = {
      title: dom.editFeedTitle.value.trim() || undefined,
      url: dom.editFeedUrl.value.trim() || undefined,
      filter_keywords: dom.editIncKeywords.value.trim() || undefined,
      filter_exclude: dom.editExcKeywords.value.trim() || undefined,
      case_sensitive: dom.editCaseSensitive.checked,
      target_id: dom.editTargetId.value || undefined,
    };

    try {
      const res = await fetch(`/api/feeds/${feedId}/edit`, {
        method: 'POST',
        credentials: 'omit',
        headers: TelegramBridge.getAuthHeaders(),
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to edit feed');

      showToast('Feed updated successfully', 'success');
      TelegramBridge.hapticNotification('success');
      closeEditModal();
      loadFeeds();
    } catch (err) {
      showToast(err.message, 'error');
      TelegramBridge.hapticNotification('error');
    }
  }

  // ---------------- Settings & System Check ----------------

  async function triggerSystemCheck() {
    TelegramBridge.hapticImpact('medium');
    dom.quickCheckBtn.classList.add('spinning');
    try {
      const res = await fetch('/api/system/check', {
        method: 'POST',
        credentials: 'omit',
        headers: TelegramBridge.getAuthHeaders(),
      });
      const data = await res.json();
      showToast(data.message || 'Poll check started', 'success');
      TelegramBridge.hapticNotification('success');
    } catch (err) {
      showToast('Could not trigger check', 'error');
    } finally {
      setTimeout(() => dom.quickCheckBtn.classList.remove('spinning'), 1000);
    }
  }

  async function handleToggleRichText(e) {
    const isChecked = e.target.checked;
    TelegramBridge.hapticImpact('light');
    try {
      await fetch('/api/settings/richtext', {
        method: 'POST',
        credentials: 'omit',
        headers: TelegramBridge.getAuthHeaders(),
        body: JSON.stringify({ enabled: isChecked }),
      });
      showToast(`Rich text formatting ${isChecked ? 'enabled' : 'disabled'}`, 'success');
    } catch (err) {
      showToast('Could not save setting', 'error');
    }
  }

  // ---------------- Utilities ----------------

  function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    const icon = type === 'success' ? '✅' : (type === 'error' ? '❌' : 'ℹ️');

    toast.innerHTML = `
      <span class="toast-icon">${icon}</span>
      <span class="toast-msg">${escapeHtml(message)}</span>
    `;

    dom.toastContainer.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(10px)';
      toast.style.transition = 'all 0.3s';
      setTimeout(() => toast.remove(), 300);
    }, 3500);
  }

  function copyToClipboard(text, successMsg = 'Copied!') {
    TelegramBridge.hapticImpact('medium');
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).then(() => {
        showToast(successMsg, 'success');
      }).catch(() => fallbackCopy(text, successMsg));
    } else {
      fallbackCopy(text, successMsg);
    }
  }

  function fallbackCopy(text, successMsg) {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    showToast(successMsg, 'success');
  }

  function formatBytes(bytes) {
    if (!bytes) return null;
    if (typeof bytes === 'string') {
      if (isNaN(Number(bytes))) return bytes;
      bytes = Number(bytes);
    }
    if (bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return (bytes / Math.pow(1024, i)).toFixed(2) + ' ' + units[i];
  }

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  return { init, switchTab };
})();

document.addEventListener('DOMContentLoaded', App.init);
