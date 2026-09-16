const assert = require('node:assert/strict');

global.window = { Celestra: {} };
global.document = { readyState: 'loading', addEventListener() {} };
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
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
