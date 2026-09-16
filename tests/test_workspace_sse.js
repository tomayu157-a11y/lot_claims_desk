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
    querySelectorAll() { return []; },
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
  const submit = listeners.find((item) => item.type === 'submit');
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
