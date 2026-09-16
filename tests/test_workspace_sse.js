const assert = require('node:assert/strict');

const listeners = [];
const roots = {};

function element() {
  const value = {
    children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    style: {},
    appendChild(child) { this.children.push(child); return child; },
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    setAttribute() {},
    getAttribute() { return ''; },
    hasAttribute() { return false; },
    contains() { return true; },
    focus() {}
  };
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
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
