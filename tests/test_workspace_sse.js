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
  const submit = listeners.find((item) => item.type === 'submit');
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
