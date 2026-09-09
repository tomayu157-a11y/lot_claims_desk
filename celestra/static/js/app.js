/* ==========================================================================
   Celestra front-end. Vanilla JS, zero dependencies, nothing fetched from a CDN.

   Public surface
     Celestra.initEventStream(runId, lastSeq)
     Celestra.postJSON(url, body, options)
     Celestra.openModal(el) / closeModal()
     Celestra.openSlideOver(el) / closeSlideOver()
   ========================================================================== */
(function () {
  'use strict';

  var Celestra = window.Celestra || {};
  window.Celestra = Celestra;

  /* ---------------------------------------------------------------- utils */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }
  function on(root, type, selector, handler) {
    root.addEventListener(type, function (ev) {
      var target = ev.target.closest(selector);
      if (target && root.contains(target)) handler(ev, target);
    });
  }
  function announce(node, text) {
    if (node && node.textContent !== text) node.textContent = text;
  }
  function normaliseProgress(value) {
    var n = parseFloat(value);
    if (isNaN(n)) return null;
    if (n <= 1) n = n * 100;
    return Math.max(0, Math.min(100, n));
  }
  function titleise(value) {
    if (!value) return '';
    return String(value).replace(/_/g, ' ').replace(/\b\w/g, function (c) {
      return c.toUpperCase();
    });
  }

  var STATUS_LABELS = {
    queued: 'Queued',
    researching: 'Researching…',
    synthesising: 'Synthesising…',
    complete: 'Complete',
    failed: 'Failed',
    skipped: 'Skipped',
    blocked: 'Blocked'
  };
  var BUSY = { researching: 1, synthesising: 1 };

  /* ------------------------------------------------------------ 1. stream */
  var ICON_CHECK =
    '<svg class="ic" width="15" height="15" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
  var ICON_ALERT =
    '<svg class="ic" width="15" height="15" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true"><path d="M12 9v4"/>' +
    '<path d="M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 ' +
    '1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg>';
  var ICON_DOT = '<span class="chip-dot" aria-hidden="true"></span>';

  function statusMarkup(status) {
    var label = STATUS_LABELS[status] || titleise(status);
    if (BUSY[status]) return '<span class="spinner" aria-hidden="true"></span><span>' + label + '</span>';
    if (status === 'complete') return ICON_CHECK + '<span>' + label + '</span>';
    if (status === 'failed') return ICON_ALERT + '<span>' + label + '</span>';
    return ICON_DOT + '<span>' + label + '</span>';
  }

  function agentCard(key) {
    if (!key) return null;
    return document.querySelector('[data-agent-key="' + String(key).replace(/"/g, '') + '"]');
  }

  function applyAgentStatus(card, status) {
    if (!card || !status) return;
    var holder = $('[data-agent-status]', card);
    card.setAttribute('data-status', status);
    card.classList.remove('is-complete', 'is-failed', 'is-blocked');
    if (status === 'complete') card.classList.add('is-complete');
    if (status === 'failed') card.classList.add('is-failed');
    if (status === 'blocked' || status === 'skipped') card.classList.add('is-blocked');
    if (holder) {
      holder.className = 'agent-status status status-' + status;
      holder.innerHTML = statusMarkup(status);
    }
    var progress = $('[data-agent-progress]', card);
    if (progress) {
      progress.classList.toggle('is-complete', status === 'complete');
      progress.classList.toggle('is-failed', status === 'failed');
    }
    if (status === 'complete') applyAgentProgress(card, 100);
  }

  function applyAgentProgress(card, value) {
    var pct = normaliseProgress(value);
    if (card == null || pct === null) return;
    var wrap = $('[data-agent-progress]', card);
    var bar = wrap ? $('.progress-bar', wrap) : null;
    if (bar) bar.style.width = pct.toFixed(1) + '%';
    if (wrap) wrap.setAttribute('aria-valuenow', Math.round(pct));
  }

  function applyAgentMessage(card, message) {
    if (!card || message == null) return;
    announce($('[data-agent-message]', card), message);
  }

  function markSourceUsed(name, key) {
    var chip = null;
    if (key) chip = document.querySelector('[data-source-key="' + String(key).replace(/"/g, '') + '"]');
    if (!chip && name) {
      chip = $$('[data-source-name]').filter(function (el) {
        return el.getAttribute('data-source-name').toLowerCase() === String(name).toLowerCase();
      })[0];
    }
    var strip = $('[data-source-strip]');
    if (!chip && strip && name) {
      chip = document.createElement('span');
      chip.className = 'chip chip-sm chip-source';
      chip.setAttribute('data-source-name', name);
      if (key) chip.setAttribute('data-source-key', key);
      chip.textContent = name;
      var more = $('[data-source-more]', strip);
      if (more) strip.querySelector('.chip-row').insertBefore(chip, more);
      else strip.querySelector('.chip-row').appendChild(chip);
    }
    if (chip) chip.classList.add('is-used');
  }

  function setPhaseState(key, state, label) {
    var group = document.querySelector('[data-phase="' + String(key).replace(/"/g, '') + '"]');
    if (!group) return;
    group.classList.remove('is-complete', 'is-running', 'is-failed', 'is-gated', 'is-queued');
    group.classList.add('is-' + state);
    var holder = $('[data-phase-state]', group);
    if (holder) {
      if (state === 'running') {
        holder.innerHTML = '<span class="spinner" aria-hidden="true"></span> ' + (label || 'Running');
      } else if (state === 'complete') {
        holder.innerHTML = ICON_CHECK + ' ' + (label || 'Complete');
      } else if (state === 'failed') {
        holder.innerHTML = ICON_ALERT + ' ' + (label || 'An agent failed');
      } else {
        holder.innerHTML = ICON_DOT + ' ' + (label || 'Queued');
      }
    }
  }

  function refreshPhaseFromCards(key) {
    var group = document.querySelector('[data-phase="' + String(key).replace(/"/g, '') + '"]');
    if (!group) return;
    var cards = $$('[data-agent-key]', group);
    if (!cards.length) return;
    var done = cards.filter(function (c) { return c.getAttribute('data-status') === 'complete'; }).length;
    var failed = cards.some(function (c) { return c.getAttribute('data-status') === 'failed'; });
    var busy = cards.some(function (c) { return BUSY[c.getAttribute('data-status')]; });
    if (done === cards.length) setPhaseState(key, 'complete');
    else if (failed && !busy) setPhaseState(key, 'failed');
    else if (busy || done) setPhaseState(key, 'running', 'Running · ' + done + ' / ' + cards.length + ' agents done');
  }

  function phaseOfCard(card) {
    var group = card ? card.closest('[data-phase]') : null;
    return group ? group.getAttribute('data-phase') : null;
  }

  function bumpCounter(selector) {
    var el = $(selector);
    if (!el) return;
    var current = parseInt(el.getAttribute('data-count') || el.textContent, 10);
    if (isNaN(current)) current = 0;
    current += 1;
    el.setAttribute('data-count', current);
    el.textContent = current;
  }

  Celestra.initEventStream = function (runId, lastSeq) {
    if (!runId || typeof window.EventSource === 'undefined') return null;

    var seq = parseInt(lastSeq, 10) || 0;
    var attempts = 0;
    var source = null;
    var closed = false;
    var moved = false;
    var timer = null;
    var live = $('[data-stream-live]');

    function url() {
      return '/runs/' + encodeURIComponent(runId) + '/events?lastSeq=' + seq;
    }

    function parse(ev) {
      var data = {};
      try { data = JSON.parse(ev.data || '{}'); } catch (err) { data = {}; }
      if (data && typeof data.seq === 'number' && data.seq > seq) seq = data.seq;
      return data;
    }

    function handle(type, data) {
      var key = data.agent_key || data.agent || data.key;
      var card = agentCard(key);

      switch (type) {
        case 'run_started':
          announce(live, 'Research run started.');
          break;

        case 'phase_started':
          if (data.phase) setPhaseState(data.phase, 'running', 'Running');
          var title = $('[data-page-title]');
          var sub = $('[data-page-sub]');
          if (data.phase === 'mapping') {
            if (title) title.textContent = 'Mapping & Synthesis is running';
            if (sub) sub.textContent = 'The four remaining agents are building on the discovery ' +
              'findings you approved. When they finish, the run moves to Final approval.';
            var gateNote = $('[data-phase-gate]');
            if (gateNote) {
              gateNote.classList.add('is-passed');
              var span = $('span', gateNote);
              if (span) span.innerHTML = '<strong>Review gate passed.</strong> You approved the discovery findings.';
            }
          }
          announce(live, (data.name || titleise(data.phase)) + ' phase started.');
          break;

        case 'agent_status':
          applyAgentStatus(card, data.status);
          if (data.message) applyAgentMessage(card, data.message);
          if (data.progress != null) applyAgentProgress(card, data.progress);
          if (card && data.questions_total) {
            var qEl = $('[data-agent-questions]', card);
            if (qEl) {
              qEl.hidden = false;
              qEl.textContent = (data.questions_answered || 0) + ' / ' + data.questions_total;
            }
          }
          refreshPhaseFromCards(phaseOfCard(card));
          announce(live, (data.agent_name || titleise(key)) + ': ' +
            (STATUS_LABELS[data.status] || titleise(data.status)));
          break;

        case 'agent_progress':
          applyAgentProgress(card, data.progress);
          applyAgentMessage(card, data.message);
          if (data.status) applyAgentStatus(card, data.status);
          break;

        case 'source_used':
          markSourceUsed(data.source_name || data.name, data.source_key || data.source_id);
          if (card && data.message) applyAgentMessage(card, data.message);
          break;

        case 'question_status':
          if (card && data.message) applyAgentMessage(card, data.message);
          if (card && data.questions_total) {
            var counterEl = $('[data-agent-questions]', card);
            if (counterEl) {
              counterEl.textContent = (data.questions_answered || 0) + ' / ' + data.questions_total;
            }
          }
          break;

        case 'insight_added':
          bumpCounter('[data-live-insights]');
          break;

        case 'contradiction_added':
          bumpCounter('[data-live-contradictions]');
          break;

        case 'stage_complete':
          if (card) applyAgentStatus(card, 'complete');
          announce(live, (data.name || data.stage || 'Stage') + ' complete.');
          break;

        case 'run_complete':
          announce(live, 'Every agent has finished. Opening final approval…');
          setPhaseState('mapping', 'complete');
          moved = true;
          teardown();
          var next = data.redirect || ('/runs/' + encodeURIComponent(runId) + '/approval');
          window.setTimeout(function () { window.location.assign(next); }, 900);
          break;

        case 'review_required':
          announce(live, 'Discovery finished. Opening the findings for your review…');
          setPhaseState('discovery', 'complete');
          moved = true;
          teardown();
          var reviewUrl = data.redirect || ('/runs/' + encodeURIComponent(runId) + '/review');
          window.setTimeout(function () { window.location.assign(reviewUrl); }, 900);
          break;

        case 'run_resumed':
          announce(live, 'Mapping & Synthesis started.');
          break;

        case 'run_failed':
          announce(live, 'Run failed: ' + (data.error || data.message || 'unknown error'));
          var banner = $('[data-run-error]');
          if (banner) {
            banner.hidden = false;
            var body = $('[data-run-error-message]', banner) || banner;
            body.textContent = data.error || data.message || 'The run failed.';
          }
          moved = true;
          teardown();
          break;

        case 'stream_end':
          teardown();
          // The stream can only end because the run paused, finished or failed.
          // If none of those events reached this tab, ask the server where the
          // run is now instead of leaving a stale page.
          if (!moved) {
            window.setTimeout(function () {
              window.location.assign('/runs/' + encodeURIComponent(runId));
            }, 1200);
          }
          break;

        case 'heartbeat':
        default:
          break;
      }
    }

    var TYPES = ['run_started', 'phase_started', 'agent_status', 'agent_progress', 'source_used',
      'question_status', 'insight_added', 'contradiction_added', 'stage_complete',
      'review_required', 'run_resumed', 'context_published', 'wave_started', 'wave_complete',
      'run_complete', 'run_failed', 'stream_end', 'heartbeat'];

    function connect() {
      if (closed) return;
      source = new EventSource(url());

      source.addEventListener('open', function () { attempts = 0; });

      TYPES.forEach(function (type) {
        source.addEventListener(type, function (ev) { handle(type, parse(ev)); });
      });

      source.addEventListener('message', function (ev) {
        var data = parse(ev);
        if (data && data.type) handle(data.type, data);
      });

      source.addEventListener('error', function () {
        if (closed) return;
        if (source) { source.close(); source = null; }
        attempts += 1;
        var delay = Math.min(30000, 800 * Math.pow(2, attempts - 1));
        delay = delay + Math.floor(Math.random() * 250);
        announce(live, 'Connection lost. Reconnecting…');
        timer = window.setTimeout(connect, delay);
      });
    }

    function teardown() {
      closed = true;
      if (timer) { window.clearTimeout(timer); timer = null; }
      if (source) { source.close(); source = null; }
    }

    connect();
    window.addEventListener('beforeunload', teardown);
    return { close: teardown, lastSeq: function () { return seq; } };
  };

  /* ------------------------------------------------- 2. insight filtering */
  var CONF_RANK = { requires_input: 0, ready: 1 };

  function initInsights() {
    var list = $('[data-insight-list]');
    if (!list) return;

    var state = { category: 'all', query: '', sort: 'confidence' };
    var countEl = $('[data-insight-count]');
    var emptyEl = $('[data-insight-empty]');

    function cards() { return $$('[data-insight-id]', list); }

    function matches(card) {
      var cat = (card.getAttribute('data-category') || '').toLowerCase();
      var conf = (card.getAttribute('data-confidence') || '').toLowerCase();
      var decided = (card.getAttribute('data-decision') || 'pending') !== 'pending';
      if (state.category === 'requires_input') {
        if (conf !== 'requires_input' || decided) return false;
      } else if (state.category === 'ready') {
        if (conf !== 'ready' && !decided) return false;
      } else if (state.category !== 'all' && cat !== state.category) {
        return false;
      }
      if (!state.query) return true;
      return (card.getAttribute('data-search') || card.textContent || '')
        .toLowerCase().indexOf(state.query) !== -1;
    }

    function apply() {
      var visible = 0;
      cards().forEach(function (card) {
        var show = matches(card);
        card.hidden = !show;
        card.setAttribute('data-hidden', show ? '0' : '1');
        if (show) visible += 1;
      });
      if (countEl) countEl.textContent = visible;
      if (emptyEl) emptyEl.hidden = visible !== 0;
    }

    function sort() {
      var items = cards();
      items.sort(function (a, b) {
        if (state.sort === 'sources') {
          return (parseInt(b.getAttribute('data-sources'), 10) || 0) -
                 (parseInt(a.getAttribute('data-sources'), 10) || 0);
        }
        if (state.sort === 'stage') {
          return (a.getAttribute('data-stage') || '').localeCompare(b.getAttribute('data-stage') || '');
        }
        var ra = CONF_RANK[a.getAttribute('data-confidence')];
        var rb = CONF_RANK[b.getAttribute('data-confidence')];
        ra = ra === undefined ? 9 : ra;
        rb = rb === undefined ? 9 : rb;
        return ra - rb;
      });
      items.forEach(function (card) { list.appendChild(card); });
    }

    on(document, 'click', '[data-filter-category]', function (ev, btn) {
      ev.preventDefault();
      state.category = (btn.getAttribute('data-filter-category') || 'all').toLowerCase();
      $$('[data-filter-category]').forEach(function (el) {
        el.setAttribute('aria-pressed', el === btn ? 'true' : 'false');
      });
      apply();
    });

    var search = $('[data-insight-search]');
    if (search) {
      search.addEventListener('input', function () {
        state.query = search.value.trim().toLowerCase();
        apply();
      });
    }

    var sortSelect = $('[data-insight-sort]');
    if (sortSelect) {
      state.sort = sortSelect.value || state.sort;
      sortSelect.addEventListener('change', function () {
        state.sort = sortSelect.value;
        sort();
        apply();
      });
    }

    sort();
    apply();
    Celestra.refreshInsights = function () { sort(); apply(); };
  }

  /* ------------------------------- 3. modal: focus trap, escape, counter */
  var modalRoot, slideRoot, lastFocused;

  function focusables(container) {
    return $$(
      'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]),' +
      'select:not([disabled]), [tabindex]:not([tabindex="-1"])', container
    ).filter(function (el) { return el.offsetParent !== null || el === document.activeElement; });
  }

  function trap(container, ev) {
    if (ev.key !== 'Tab') return;
    var items = focusables(container);
    if (!items.length) { ev.preventDefault(); return; }
    var first = items[0];
    var last = items[items.length - 1];
    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  }

  function root(id, cls) {
    var el = document.getElementById(id);
    if (!el) {
      el = document.createElement('div');
      el.id = id;
      if (cls) el.className = cls;
      document.body.appendChild(el);
    }
    return el;
  }

  function activate(container, onClose) {
    lastFocused = document.activeElement;
    document.body.style.overflow = 'hidden';
    var dialog = $('[role="dialog"]', container) || container;
    var auto = $('[data-autofocus]', container) || focusables(dialog)[0];
    if (auto) auto.focus();

    function keydown(ev) {
      if (ev.key === 'Escape') { ev.preventDefault(); onClose(); return; }
      trap(dialog, ev);
    }
    container.addEventListener('keydown', keydown);
    container._celestraKeydown = keydown;

    container.addEventListener('mousedown', function (ev) {
      var t = ev.target;
      if (t === container || t.hasAttribute('data-modal-backdrop') ||
          t.hasAttribute('data-slideover-backdrop')) {
        onClose();
      }
    });
    initCounters(container);
  }

  function deactivate(container) {
    if (!container) return;
    if (container._celestraKeydown) {
      container.removeEventListener('keydown', container._celestraKeydown);
      container._celestraKeydown = null;
    }
    container.innerHTML = '';
    if (!$('#modal-root') || !$('#modal-root').innerHTML) {
      if (!$('#slideover-root') || !$('#slideover-root').innerHTML) {
        document.body.style.overflow = '';
      }
    }
    if (lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus();
  }

  Celestra.closeModal = function () { deactivate(modalRoot); };
  Celestra.closeSlideOver = function () { deactivate(slideRoot); };

  Celestra.openModal = function (html) {
    modalRoot = root('modal-root');
    modalRoot.innerHTML = html;
    activate(modalRoot, Celestra.closeModal);
    return modalRoot;
  };

  Celestra.openSlideOver = function (html) {
    slideRoot = root('slideover-root');
    slideRoot.innerHTML = html;
    activate(slideRoot, Celestra.closeSlideOver);
    return slideRoot;
  };

  function initCounters(scope) {
    $$('[data-counter-for]', scope || document).forEach(function (counter) {
      var field = document.getElementById(counter.getAttribute('data-counter-for'));
      if (!field) return;
      var max = parseInt(field.getAttribute('maxlength'), 10) ||
                parseInt(counter.getAttribute('data-counter-max'), 10) || 500;
      function render() {
        counter.textContent = field.value.length + '/' + max;
        counter.classList.toggle('is-over', field.value.length > max);
      }
      field.addEventListener('input', render);
      render();
    });
  }
  Celestra.initCounters = initCounters;

  /* ------------------------------------------------- 4. remote panels */
  async function fetchFragment(url) {
    var res = await fetch(url, { headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' });
    var text = await res.text();
    if (!res.ok) throw new Error(text || ('Request failed: ' + res.status));
    return text;
  }

  /* ------------------------------------------------------ 5. postJSON */
  Celestra.postJSON = async function (url, body, options) {
    options = options || {};
    var res = await fetch(url, {
      method: options.method || 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'text/html, application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body || {})
    });

    var type = res.headers.get('Content-Type') || '';
    var payload = type.indexOf('application/json') !== -1 ? await res.json() : await res.text();

    if (!res.ok) {
      var message = (payload && payload.detail) || (payload && payload.error) ||
        (typeof payload === 'string' ? payload : 'Request failed');
      if (options.onError) options.onError(message);
      else flash(String(message).slice(0, 400), 'is-danger');
      return { ok: false, payload: payload, status: res.status };
    }

    var html = typeof payload === 'string' ? payload : (payload && payload.html);
    var target = options.swap;
    if (typeof target === 'string') target = $(target);

    if (html && target) {
      var holder = document.createElement('div');
      holder.innerHTML = html.trim();
      var fresh = holder.firstElementChild;
      if (fresh) {
        target.replaceWith(fresh);
        initCounters(fresh);
        if (Celestra.refreshInsights) Celestra.refreshInsights();
        fresh.classList.add('is-flash');
        window.setTimeout(function () { fresh.classList.remove('is-flash'); }, 1600);
      }
    }
    refreshGate();

    if (payload && payload.redirect) window.location.assign(payload.redirect);
    if (options.onSuccess) options.onSuccess(payload);
    return { ok: true, payload: payload, html: html };
  };

  async function refreshGate() {
    var panel = $('[data-gate-panel][data-gate-url]');
    if (!panel) return;
    try {
      var html = await fetchFragment(panel.getAttribute('data-gate-url'));
      var holder = document.createElement('div');
      holder.innerHTML = html.trim();
      var fresh = holder.firstElementChild;
      if (fresh) panel.replaceWith(fresh);
    } catch (err) { /* the page still works; the panel is just stale */ }
  }
  Celestra.refreshGate = refreshGate;

  function flash(message, kind) {
    var area = $('[data-flash-area]');
    if (!area) { return; }
    var box = document.createElement('div');
    box.className = 'banner ' + (kind || 'is-info');
    box.setAttribute('role', 'status');
    box.innerHTML = '<div class="banner-body"></div>';
    box.firstChild.textContent = message;
    area.appendChild(box);
  }
  Celestra.flash = flash;

  /* ---------------------------------------------------- 6. wiring */
  function payloadFor(btn) {
    var data = {};
    var raw = btn.getAttribute('data-payload');
    if (raw) { try { data = JSON.parse(raw); } catch (err) { data = {}; } }
    var scopeSel = btn.getAttribute('data-fields-from');
    var scope = scopeSel ? $(scopeSel) : btn.closest('[data-action-scope]');
    if (scope) {
      $$('[data-field]', scope).forEach(function (field) {
        data[field.getAttribute('data-field')] = field.value;
      });
    }
    return data;
  }

  function wire() {
    modalRoot = root('modal-root');
    slideRoot = root('slideover-root');

    /* dismissible banners */
    on(document, 'click', '[data-dismiss-banner]', function (ev, btn) {
      var banner = btn.closest('.banner');
      if (banner) banner.remove();
      var key = btn.getAttribute('data-dismiss-banner');
      if (key && window.localStorage) {
        try { window.localStorage.setItem('celestra.dismissed.' + key, '1'); } catch (err) { /* ignore */ }
      }
    });
    $$('[data-dismiss-banner]').forEach(function (btn) {
      var key = btn.getAttribute('data-dismiss-banner');
      if (!key || !window.localStorage) return;
      try {
        if (window.localStorage.getItem('celestra.dismissed.' + key) === '1') {
          var banner = btn.closest('.banner');
          if (banner) banner.remove();
        }
      } catch (err) { /* ignore */ }
    });

    /* modal */
    on(document, 'click', '[data-modal-url]', async function (ev, btn) {
      ev.preventDefault();
      btn.setAttribute('aria-busy', 'true');
      try {
        Celestra.openModal(await fetchFragment(btn.getAttribute('data-modal-url')));
      } catch (err) {
        flash('Could not open that dialog.', 'is-danger');
      } finally {
        btn.removeAttribute('aria-busy');
      }
    });
    on(document, 'click', '[data-modal-close]', function (ev) {
      ev.preventDefault();
      Celestra.closeModal();
    });

    /* slide-over */
    on(document, 'click', '[data-evidence-url]', async function (ev, btn) {
      ev.preventDefault();
      btn.setAttribute('aria-busy', 'true');
      try {
        Celestra.openSlideOver(await fetchFragment(btn.getAttribute('data-evidence-url')));
      } catch (err) {
        flash('Could not load the evidence panel.', 'is-danger');
      } finally {
        btn.removeAttribute('aria-busy');
      }
    });
    on(document, 'click', '[data-slideover-close]', function (ev) {
      ev.preventDefault();
      Celestra.closeSlideOver();
    });

    /* JSON actions: approve / modify / add input / contradiction review */
    on(document, 'click', '[data-action-url]', async function (ev, btn) {
      ev.preventDefault();
      if (btn.disabled) return;
      var payload = payloadFor(btn);
      var required = btn.getAttribute('data-requires-field');
      if (required && !String(payload[required] || '').trim()) {
        var scope = btn.closest('[data-action-scope]');
        var field = scope ? $('[data-field="' + required + '"]', scope) : null;
        if (field) { field.focus(); field.classList.add('is-invalid'); }
        flash('Write something first: this action sends your text to Celestra.', 'is-danger');
        return;
      }
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      var original = btn.innerHTML;
      var busyLabel = btn.getAttribute('data-busy-label');
      if (busyLabel) btn.innerHTML = '<span class="spinner" aria-hidden="true"></span> ' + busyLabel;
      var swap = btn.getAttribute('data-swap');
      var closes = btn.hasAttribute('data-closes-modal');
      try {
        var result = await Celestra.postJSON(btn.getAttribute('data-action-url'), payload, {
          swap: swap ? $(swap) : null
        });
        if (result.ok && closes) Celestra.closeModal();
      } finally {
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
        if (busyLabel) btn.innerHTML = original;
      }
    });

    /* the gate form: never submit while blocked */
    on(document, 'submit', '[data-gate-form]', function (ev, form) {
      var button = $('button[type="submit"]', form);
      if (button && button.disabled) { ev.preventDefault(); return; }
      if (button) {
        button.disabled = true;
        button.innerHTML = '<span class="spinner" aria-hidden="true"></span> Working…';
      }
    });

    /* mode selector on the new-project form */
    $$('[data-mode-radio]').forEach(function (radio) {
      radio.addEventListener('change', function () {
        var reveal = $('[data-mode-reveal]');
        if (!reveal) return;
        var single = radio.value === 'single' && radio.checked;
        reveal.hidden = !single;
        var select = $('select', reveal);
        if (select) select.required = single;
      });
      if (radio.checked) radio.dispatchEvent(new Event('change'));
    });

    initInsights();
    initCounters(document);

    var stream = $('[data-run-stream]');
    if (stream) {
      Celestra.initEventStream(
        stream.getAttribute('data-run-stream'),
        stream.getAttribute('data-last-seq') || 0
      );
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();
