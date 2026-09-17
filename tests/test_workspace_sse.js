const assert = require('node:assert/strict');

const listeners = [];
const roots = {};

function element() {
  const attributes = new Map();
  const classes = new Set();
  const value = {
    children: [],
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      toggle(name, force) {
        const present = force === undefined ? !classes.has(name) : force;
        if (present) classes.add(name); else classes.delete(name);
        return present;
      },
      contains(name) { return classes.has(name); }
    },
    style: {},
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
    addEventListener() {},
    removeEventListener() {},
    querySelector(selector) {
      const matches = (node) => {
        if (selector[0] === '.') return node.classList.contains(selector.slice(1));
        const attribute = selector.match(/^\[([^=\]]+)/);
        return attribute ? node.hasAttribute(attribute[1]) : false;
      };
      const find = (node) => {
        for (const child of node.children) {
          if (matches(child)) return child;
          const nested = find(child);
          if (nested) return nested;
        }
        return null;
      };
      return find(this);
    },
    querySelectorAll(selector) {
      const matches = (node) => {
        if (selector[0] === '.') return node.classList.contains(selector.slice(1));
        const attribute = selector.match(/^\[([^=\]]+)/);
        return attribute ? node.hasAttribute(attribute[1]) : false;
      };
      const found = [];
      const collect = (node) => {
        for (const child of node.children) {
          if (matches(child)) found.push(child);
          collect(child);
        }
      };
      collect(this);
      return found;
    },
    setAttribute(name, content) { attributes.set(name, String(content)); },
    getAttribute(name) { return attributes.get(name) || ''; },
    hasAttribute(name) { return attributes.has(name); },
    contains() { return true; },
    focus() {},
    closest(selector) {
      let node = this;
      while (node) {
        if (selector[0] === '.' && node.classList.contains(selector.slice(1))) return node;
        const attribute = selector.match(/^\[([^=\]]+)/);
        if (attribute && node.hasAttribute(attribute[1])) return node;
        node = node.parentNode;
      }
      return null;
    }
  };
  Object.defineProperty(value, 'className', {
    get() { return [...classes].join(' '); },
    set(names) { classes.clear(); String(names).split(/\s+/).filter(Boolean).forEach((name) => classes.add(name)); }
  });
  Object.defineProperty(value, 'innerHTML', {
    get() { return this._innerHTML || ''; },
    set(html) {
      this._innerHTML = html;
      this.firstChild = { textContent: '' };
      this.firstElementChild = this;
    }
  });
  Object.defineProperty(value, 'textContent', {
    get() { return this._textContent || ''; },
    set(content) {
      this._textContent = String(content);
      if (content === '') this.children = [];
    }
  });
  return value;
}

const flashArea = element();
global.window = {
  Celestra: {}, setTimeout, location: { hash: '', href: 'http://test/', origin: 'http://test' }
};
global.document = {
  readyState: 'loading',
  body: {
    style: {},
    appendChild(node) { roots[node.id] = node; return node; }
  },
  activeElement: null,
  addEventListener(type, handler) { listeners.push({ type, handler }); },
  contains() { return true; },
  getElementById(id) { return roots[id] || null; },
  createElement() { return element(); },
  querySelector(selector) { return selector === '[data-flash-area]' ? flashArea : null; },
  querySelectorAll() { return []; }
};
require('../celestra/static/js/app.js');

function responseFromChunks(chunks) {
  let index = 0;
  return {
    ok: true,
    body: {
      getReader() {
        return {
          async read() {
            if (index === chunks.length) return { done: true };
            return { done: false, value: new TextEncoder().encode(chunks[index++]) };
          }
        };
      }
    }
  };
}

async function main() {
  const events = [];
  const finalFrame = 'event: answer_completed\ndata: {"message_id":"wmsg_final","sources":[]}';
  await window.Celestra.readSSE(responseFromChunks([finalFrame]), (type, payload) => {
    events.push({ type, payload });
  });
  assert.deepEqual(events, [{
    type: 'answer_completed', payload: { message_id: 'wmsg_final', sources: [] }
  }]);

  const splitEvents = [];
  const splitFrame = 'event: research_status\ndata: {"message_id":"wmsg_split","detail":"checking_evidence"}\n\n';
  await window.Celestra.readSSE(responseFromChunks([
    splitFrame.slice(0, 9), splitFrame.slice(9, 40), splitFrame.slice(40)
  ]), (type, payload) => {
    splitEvents.push({ type, payload });
  });
  assert.deepEqual(splitEvents, [{
    type: 'research_status', payload: { message_id: 'wmsg_split', detail: 'checking_evidence' }
  }]);

  const domReady = listeners.find((item) => item.type === 'DOMContentLoaded');
  domReady.handler();
  const workspace = element();
  const preview = element();
  preview.hidden = true;
  preview.innerHTML = '<p>Existing workspace document</p>';
  workspace.getAttribute = (name) => ({
    'data-run-id': 'run_one', 'data-insight-id': 'ins_one'
  })[name] || '';
  workspace.querySelector = (selector) => (
    selector === '[data-workspace-preview]' ? preview : null
  );
  const proposalButton = element();
  proposalButton.closest = (selector) => (
    selector === '[data-workspace-propose]' ? proposalButton :
      (selector === '[data-insight-workspace]' ? workspace : null)
  );
  global.fetch = async () => ({
    ok: false,
    headers: { get: () => 'application/json' },
    json: async () => ({ detail: 'The model is unavailable. Try again.' })
  });
  listeners
    .filter((item) => item.type === 'click')
    .forEach((item) => item.handler({ target: proposalButton, preventDefault() {} }));
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(preview.hidden, true);
  assert.equal(preview.innerHTML, '<p>Existing workspace document</p>');
  assert.equal(flashArea.children.length, 1);
  assert.equal(flashArea.children[0].firstChild.textContent, 'The model is unavailable. Try again.');

  // A cross-origin redirect must never be treated as trusted server HTML.
  global.fetch = async () => ({
    ok: true,
    url: 'https://outside.example/proposal',
    headers: { get: () => 'application/json' },
    json: async () => ({ proposal_html: '<p>Untrusted preview</p>' })
  });
  listeners
    .filter((item) => item.type === 'click')
    .forEach((item) => item.handler({ target: proposalButton, preventDefault() {} }));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(preview.hidden, true);
  assert.equal(preview.innerHTML, '<p>Existing workspace document</p>');

  const continuePreview = element();
  continuePreview.hidden = false;
  const continueInput = element();
  let continueFocuses = 0;
  continueInput.focus = () => { continueFocuses += 1; };
  const continueWorkspace = element();
  continueWorkspace.setAttribute('data-insight-workspace', '');
  continueWorkspace.querySelector = (selector) => (
    selector === '[data-workspace-preview]' ? continuePreview :
      (selector === '[data-workspace-input]' ? continueInput : null)
  );
  const continueButton = element();
  continueButton.closest = (selector) => (
    selector === '[data-workspace-continue]' ? continueButton :
      (selector === '[data-insight-workspace]' ? continueWorkspace : null)
  );
  listeners
    .filter((item) => item.type === 'click')
    .forEach((item) => item.handler({ target: continueButton, preventDefault() {} }));
  assert.equal(continuePreview.hidden, true);
  assert.equal(continueFocuses, 1);

  const statusMessages = element();
  statusMessages.setAttribute('data-workspace-messages', '');
  const statusComposer = element();
  statusComposer.setAttribute('data-workspace-composer', '');
  const statusInput = element();
  statusInput.setAttribute('data-workspace-input', '');
  statusInput.value = 'Show the provider status.';
  statusComposer.appendChild(statusInput);
  const statusWorkspace = element();
  statusWorkspace.setAttribute('data-insight-workspace', '');
  statusWorkspace.setAttribute('data-run-id', 'run_status');
  statusWorkspace.setAttribute('data-insight-id', 'ins_status');
  statusWorkspace.appendChild(statusMessages);
  statusWorkspace.appendChild(statusComposer);
  let releaseStatusFrame;
  let statusRead = 0;
  global.fetch = async () => ({
    ok: true,
    body: { getReader() { return { async read() {
      if (statusRead++ === 0) return {
        done: false,
        value: new TextEncoder().encode('event: research_status\ndata: {"detail":"provider_waiting_on_dns"}\n\n')
      };
      if (statusRead === 2) {
        await new Promise((resolve) => { releaseStatusFrame = resolve; });
        return {
          done: false,
          value: new TextEncoder().encode('event: answer_completed\ndata: {"sources":[]}\n\n')
        };
      }
      return { done: true };
    } }; } }
  });
  const submit = listeners.find((item) => item.type === 'submit');
  submit.handler({ target: statusComposer, preventDefault() {} });
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  const statusAssistant = statusMessages.children[1];
  assert.equal(
    statusAssistant.querySelector('[data-workspace-message-state]').textContent,
    'Provider Waiting On Dns'
  );
  releaseStatusFrame();
  await new Promise((resolve) => setImmediate(resolve));

  const originalQuerySelector = global.document.querySelector;
  const originalQuerySelectorAll = global.document.querySelectorAll;
  const applyInput = element();
  applyInput.setAttribute('data-workspace-input', '');
  const applyComposer = element();
  applyComposer.setAttribute('data-workspace-composer', '');
  applyComposer.appendChild(applyInput);
  const applyWorkspace = element();
  applyWorkspace.setAttribute('data-insight-workspace', '');
  applyWorkspace.setAttribute('data-run-id', 'run_apply');
  applyWorkspace.setAttribute('data-insight-id', 'ins_apply');
  applyWorkspace.appendChild(applyComposer);
  const selectedCard = element();
  selectedCard.setAttribute('data-insight-id', 'ins_apply');
  selectedCard.replaceWith = (fresh) => { selectedCard.replacement = fresh; };
  const otherCard = element();
  otherCard.setAttribute('data-insight-id', 'ins_other');
  const gate = element();
  gate.setAttribute('data-gate-panel', '');
  gate.setAttribute('data-gate-url', '/gate');
  gate.replaceWith = (fresh) => { gate.replacement = fresh; };
  global.document.querySelector = (selector) => (
    selector === '[data-flash-area]' ? flashArea :
      (selector === '[data-gate-panel][data-gate-url]' ? gate : null)
  );
  global.document.querySelectorAll = (selector) => (
    selector === '[data-insight-id]' ? [applyWorkspace, selectedCard, otherCard] : []
  );
  let openedWorkspace = '';
  let insightRefreshes = 0;
  window.Celestra.openModal = (html) => { openedWorkspace = html; };
  window.Celestra.refreshInsights = () => { insightRefreshes += 1; };
  const applyButton = element();
  applyButton.setAttribute('data-workspace-apply-url', '/runs/run_apply/insights/ins_apply/workspace/proposals/wprop/apply');
  applyButton.closest = (selector) => (
    selector === '[data-workspace-apply-url]' ? applyButton :
      (selector === '[data-insight-workspace]' ? applyWorkspace : null)
  );
  let applyFetches = 0;
  global.fetch = async () => {
    applyFetches += 1;
    if (applyFetches === 1) {
      return {
        ok: true,
        headers: { get: () => 'application/json' },
        json: async () => ({ card_html: '<article>Selected replacement</article>', workspace_html: '<section>Reopened workspace</section>' })
      };
    }
    return { ok: true, text: async () => '<section>Refreshed gate</section>' };
  };
  listeners
    .filter((item) => item.type === 'click')
    .forEach((item) => item.handler({ target: applyButton, preventDefault() {} }));
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(selectedCard.replacement, 'only the selected card is replaced');
  assert.equal(otherCard.replacement, undefined);
  assert.equal(openedWorkspace, '<section>Reopened workspace</section>');
  assert.ok(gate.replacement, 'gate counts are refreshed after Apply');
  assert.ok(insightRefreshes >= 2, 'insight counts are refreshed after Apply');
  global.document.querySelector = originalQuerySelector;
  global.document.querySelectorAll = originalQuerySelectorAll;

  const failedPreview = element();
  failedPreview.hidden = false;
  failedPreview.innerHTML = '<p>Keep this proposal</p>';
  const failedInput = element();
  failedInput.setAttribute('data-workspace-input', '');
  const failedComposer = element();
  failedComposer.setAttribute('data-workspace-composer', '');
  failedComposer.appendChild(failedInput);
  const failedWorkspace = element();
  failedWorkspace.setAttribute('data-insight-workspace', '');
  failedWorkspace.setAttribute('data-run-id', 'run_failed');
  failedWorkspace.setAttribute('data-insight-id', 'ins_failed');
  failedWorkspace.querySelector = (selector) => (
    selector === '[data-workspace-preview]' ? failedPreview :
      (selector === '[data-workspace-composer]' ? failedComposer :
        (selector === '[data-workspace-input]' ? failedInput : null))
  );
  const failedApply = element();
  failedApply.setAttribute('data-workspace-apply-url', '/runs/run_failed/insights/ins_failed/workspace/proposals/wprop/apply');
  failedApply.closest = (selector) => (
    selector === '[data-workspace-apply-url]' ? failedApply :
      (selector === '[data-insight-workspace]' ? failedWorkspace : null)
  );
  global.fetch = async () => ({
    ok: false,
    headers: { get: () => 'application/json' },
    json: async () => ({ detail: 'Apply failed.' })
  });
  listeners
    .filter((item) => item.type === 'click')
    .forEach((item) => item.handler({ target: failedApply, preventDefault() {} }));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(failedPreview.hidden, false);
  assert.equal(failedPreview.innerHTML, '<p>Keep this proposal</p>');
  assert.equal(failedComposer.getAttribute('aria-busy'), 'false');
  assert.equal(failedInput.disabled, false);

  const enterMessages = element();
  enterMessages.setAttribute('data-workspace-messages', '');
  const enterComposer = element();
  enterComposer.setAttribute('data-workspace-composer', '');
  const enterInput = element();
  enterInput.setAttribute('data-workspace-input', '');
  enterInput.value = 'Research the missing codes.';
  enterComposer.appendChild(enterInput);
  const enterWorkspace = element();
  enterWorkspace.setAttribute('data-insight-workspace', '');
  enterWorkspace.setAttribute('data-run-id', 'run_one');
  enterWorkspace.setAttribute('data-insight-id', 'ins_one');
  enterWorkspace.appendChild(enterMessages);
  enterWorkspace.appendChild(enterComposer);
  let enterFetches = 0;
  global.fetch = async () => {
    enterFetches += 1;
    return responseFromChunks([
      'event: answer_completed\ndata: {"message_id":"wmsg_enter","sources":[]}\n\n'
    ]);
  };
  const keydown = listeners.find((item) => item.type === 'keydown');
  assert.ok(keydown, 'the workspace composer should handle Enter');
  let enterPrevented = false;
  keydown.handler({
    target: enterInput,
    key: 'Enter',
    shiftKey: false,
    isComposing: false,
    preventDefault() { enterPrevented = true; }
  });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(enterPrevented, true);
  assert.equal(enterFetches, 1);
  assert.equal(enterInput.value, '');

  enterInput.value = 'Keep this on a new line.';
  let shiftEnterPrevented = false;
  keydown.handler({
    target: enterInput,
    key: 'Enter',
    shiftKey: true,
    isComposing: false,
    preventDefault() { shiftEnterPrevented = true; }
  });
  assert.equal(shiftEnterPrevented, false);
  assert.equal(enterFetches, 1);
  assert.equal(enterInput.value, 'Keep this on a new line.');

  enterInput.value = '正在输入';
  let composingPrevented = false;
  keydown.handler({
    target: enterInput,
    key: 'Enter',
    shiftKey: false,
    isComposing: true,
    preventDefault() { composingPrevented = true; }
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(composingPrevented, false);
  assert.equal(enterFetches, 1);
  assert.equal(enterInput.value, '正在输入');

  const typingMessages = element();
  typingMessages.setAttribute('data-workspace-messages', '');
  const typingComposer = element();
  typingComposer.setAttribute('data-workspace-composer', '');
  const typingInput = element();
  typingInput.setAttribute('data-workspace-input', '');
  typingInput.value = 'Explain this finding.';
  typingComposer.appendChild(typingInput);
  const typingWorkspace = element();
  typingWorkspace.setAttribute('data-insight-workspace', '');
  typingWorkspace.setAttribute('data-run-id', 'run_one');
  typingWorkspace.setAttribute('data-insight-id', 'ins_one');
  const availableSources = element();
  availableSources.setAttribute('data-workspace-sources', '');
  availableSources.setAttribute('data-source-list', '');
  const existingSource = element();
  existingSource.setAttribute('data-source-item', '');
  existingSource.setAttribute('data-source-id', 'ev_existing');
  existingSource.href = 'https://example.org/existing';
  existingSource.textContent = 'Existing source';
  availableSources.appendChild(existingSource);
  typingWorkspace.appendChild(availableSources);
  typingWorkspace.appendChild(typingMessages);
  typingWorkspace.appendChild(typingComposer);
  const sourcePayload = Array.from({ length: 6 }, (_, index) => ({
    id: `ev_${index + 1}`,
    url: `https://example.org/source-${index + 1}`,
    organization: `Source ${index + 1}`,
    source_name: `Source ${index + 1}`
  }));
  global.fetch = async () => responseFromChunks([
    'event: answer_delta\ndata: {"message_id":"wmsg_typing","text":"One two three four"}\n\n',
    `event: answer_completed\ndata: ${JSON.stringify({ message_id: 'wmsg_typing', sources: sourcePayload })}\n\n`
  ]);
  const scheduled = [];
  const nativeWindowTimeout = window.setTimeout;
  window.setTimeout = (callback) => { scheduled.push(callback); return scheduled.length; };
  submit.handler({ target: typingComposer, preventDefault() {} });
  await new Promise((resolve) => setImmediate(resolve));

  const typingAssistant = typingMessages.children[1];
  const typingContent = typingAssistant.querySelector('[data-workspace-message-content]');
  const typingState = typingAssistant.querySelector('[data-workspace-message-state]');
  assert.notEqual(typingContent.textContent, 'One two three four');
  assert.equal(typingState.textContent, 'Answering…');

  while (scheduled.length) {
    scheduled.shift()();
    await Promise.resolve();
  }
  await new Promise((resolve) => setImmediate(resolve));
  window.setTimeout = nativeWindowTimeout;

  assert.equal(typingContent.textContent, 'One two three four');
  assert.equal(typingState.textContent, 'Complete');
  const messageSources = typingAssistant.querySelector('[data-workspace-message-sources]');
  const sourceItems = messageSources.querySelectorAll('[data-source-item]');
  const sourceToggle = messageSources.querySelector('[data-source-toggle]');
  assert.equal(sourceItems.length, 6);
  assert.deepEqual(sourceItems.map((item) => item.hidden), [false, false, false, false, true, true]);
  assert.equal(sourceToggle.textContent, '+2 more');
  assert.equal(sourceToggle.getAttribute('aria-expanded'), 'false');
  const availableSourceItems = availableSources.querySelectorAll('[data-source-item]');
  const availableSourceToggle = availableSources.querySelector('[data-source-toggle]');
  assert.equal(availableSourceItems.length, 7);
  assert.equal(availableSourceItems[0], existingSource);
  assert.equal(availableSourceToggle.textContent, '+3 more');

  listeners
    .filter((item) => item.type === 'click')
    .forEach((item) => item.handler({ target: sourceToggle, preventDefault() {} }));
  assert.deepEqual(sourceItems.map((item) => item.hidden), [false, false, false, false, false, false]);
  assert.equal(sourceToggle.textContent, 'Show less');
  assert.equal(sourceToggle.getAttribute('aria-expanded'), 'true');

  listeners
    .filter((item) => item.type === 'click')
    .forEach((item) => item.handler({ target: sourceToggle, preventDefault() {} }));
  assert.deepEqual(sourceItems.map((item) => item.hidden), [false, false, false, false, true, true]);
  assert.equal(sourceToggle.textContent, '+2 more');

  const messages = element();
  messages.setAttribute('data-workspace-messages', '');
  const composer = element();
  composer.setAttribute('data-workspace-composer', '');
  const input = element();
  input.setAttribute('data-workspace-input', '');
  input.value = 'Please research this finding.';
  composer.appendChild(input);
  const send = element();
  send.setAttribute('data-workspace-send', '');
  composer.appendChild(send);
  const errorWorkspace = element();
  errorWorkspace.setAttribute('data-insight-workspace', '');
  errorWorkspace.setAttribute('data-run-id', 'run_one');
  errorWorkspace.setAttribute('data-insight-id', 'ins_one');
  errorWorkspace.appendChild(messages);
  errorWorkspace.appendChild(composer);
  const duplicateErrors = [
    'event: error\ndata: {"message_id":"wmsg_error","detail":"Initial failure."}\n\n',
    'event: error\ndata: {"message_id":"wmsg_error","detail":"Latest failure."}\n\n'
  ];
  global.fetch = async () => responseFromChunks(duplicateErrors);
  submit.handler({ target: composer, preventDefault() {} });
  await new Promise((resolve) => setImmediate(resolve));

  const assistant = messages.children[1];
  const errors = assistant.children.filter((child) => child.classList.contains('insight-workspace-error'));
  assert.equal(errors.length, 1);
  assert.equal(errors[0].textContent, 'Latest failure. Try again.');
  assert.equal(assistant.classList.contains('is-failed'), true);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
