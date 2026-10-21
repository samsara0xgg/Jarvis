// The day as the brain serves it, read by the desktop's own parser. contracts/day/timeline.json holds two bodies of GET /inherent/day
// that the brain's contract test (tests/contract/test_day_contracts.py) produces from the real route; this feeds each to `tidyReply`
// (src/dayView.ts, the one pure reader of that body) and checks the timeline reads everything it draws. Run: node --test scripts/verify-contracts.mjs
// (verify-all.mjs runs it as `node scripts/verify-contracts.mjs`, which runs the same tests). It reads the files in place, so a brain that
// renames a field fails here after `pytest tests/contract --update-contracts`, not in the pane. New files only: no src/ change.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { tidyReply } from '../src/dayView.ts';

const file = new URL('../../../contracts/day/timeline.json', import.meta.url);
const { examples } = JSON.parse(readFileSync(file, 'utf8'));

// What the pane reads of each kind of item, beyond the fields every item has (id, kind, lane, state, start_ms, end_ms). A field the brain
// stops sending is a row without it (`tidy` never invents one), so each is named here and must reach the pane as the brain sent it.
const READ = {
  sleep: ['minutes', 'stages'], stay: ['place', 'lat', 'lng'], move: ['activity'], work: ['active_s', 'projects', 'apps'], talk: ['turns'],
  reminder: ['text', 'fired', 'acknowledged'], call: ['app'], event: ['title', 'source', 'confirmed'],
};
const COMMON = ['id', 'kind', 'lane', 'state', 'start_ms', 'end_ms'];
// The fields that become a list of another shape on the way in (a stage keeps its three fields, a top-list entry `{name, seconds}`).
const LISTS = new Set(['stages', 'projects', 'apps']);

for (const name of Object.keys(examples)) {
  const sent = examples[name];
  const read = tidyReply(structuredClone(sent));

  test(`${name}: the day's envelope reads whole`, () => {
    for (const key of ['date', 'tz', 'start_ms', 'end_ms', 'now_ms']) assert.equal(read[key], sent[key], key);
    assert.deepEqual(read.missing, sent.missing);
    assert.deepEqual(read.labels.map(x => x.title), sent.labels.map(x => x.title));
    assert.equal(read.line?.text ?? null, sent.line?.text ?? null);
    assert.deepEqual(read.highlights, sent.highlights.map(({ id, label, by }) => ({ id, label, by })));
    assert.deepEqual(read.summary, sent.summary);
  });

  test(`${name}: every item is drawn, in the order sent`, () => {
    assert.equal(read.items.length, sent.items.length, 'an item was left out');
    sent.items.forEach((item, i) => {
      for (const key of COMMON) assert.deepEqual(read.items[i][key], item[key], `${item.id}.${key}`);
    });
  });

  test(`${name}: each kind arrives with the fields the pane reads`, () => {
    sent.items.forEach((item, i) => {
      for (const key of READ[item.kind] ?? []) {
        if (item[key] == null) continue;
        const got = read.items[i][key];
        if (LISTS.has(key)) assert.equal(got?.length, item[key].length, `${item.id}.${key} lost entries`);
        else assert.deepEqual(got, item[key], `${item.id}.${key}`);
      }
    });
  });

  test(`${name}: what he was doing reads with its words, clues and the items it is made of`, () => {
    const doing = sent.spans.filter(x => x.lane === 'doing');
    assert.equal(read.spans.length, doing.length, 'a doing span was left out');
    doing.forEach((span, i) => {
      const got = read.spans[i];
      for (const key of ['id', 'kind', 'start_ms', 'end_ms', 'title', 'category', 'basis', 'confidence', 'place_id', 'item_ids']) {
        assert.deepEqual(got[key], span[key] ?? null, `${span.id}.${key}`);
      }
      assert.deepEqual(got.clues.map(x => x.text), span.clues.map(x => x.text.trim()), `${span.id} clues`);
    });
  });
}

// The files must hold what the tests above need, or they pass on nothing: every kind the pane reads is in the full day with each field it
// reads, and the day names something he was doing, a label, a mark of each author and a question to answer.
test('the full day carries every kind and field the pane reads', () => {
  const { items, spans, labels, highlights } = examples.full;
  for (const [kind, keys] of Object.entries(READ)) {
    const of = items.filter(x => x.kind === kind);
    assert.ok(of.length, `no ${kind} item in the example`);
    for (const key of keys) assert.ok(of.some(x => x[key] != null), `no ${kind} item sends ${key}`);
  }
  assert.ok(spans.some(x => x.lane === 'doing' && x.clues.length), 'no doing span cites a clue');
  assert.ok(spans.some(x => x.lane === 'doing' && x.question), 'no doing span asks a question');
  assert.ok(labels.length);
  assert.deepEqual([...new Set(highlights.map(x => x.by))].sort(), ['her', 'him']);
});

// And the reader does hold the brain to it: a renamed field is not read, which is what the checks above exist to catch.
test('a renamed field is noticed, not read as something else', () => {
  const sent = structuredClone(examples.full);
  for (const item of sent.items) { item.begin_ms = item.start_ms; delete item.start_ms; }
  assert.equal(tidyReply(sent).items.length, 0, 'items with no start_ms are drawn');
  const moved = structuredClone(examples.full);
  delete moved.end_ms;
  assert.throws(() => tidyReply(moved), /no usable bounds/);
  const words = structuredClone(examples.full);
  for (const item of words.items) if (item.kind === 'reminder') { item.said = item.text; delete item.text; }
  assert.equal(tidyReply(words).items.find(x => x.kind === 'reminder').text, undefined);
});
