import test from 'node:test';
import assert from 'node:assert/strict';
import {
  HABITAT_ZONES,
  HABITAT_FAR_LAYOUT,
  HABITAT_ZONE_TYPES,
  habitatZoneKey,
  habitatSlotPosition,
  habitatDeckCorners,
} from '../noema/static/world-habitat.mjs';

test('economic habitat keeps canonical entity families in bounded functional zones', () => {
  assert.equal(habitatZoneKey({ type: 'provider' }), 'providers');
  assert.equal(habitatZoneKey({ type: 'agent' }), 'agents');
  assert.equal(habitatZoneKey({ type: 'mission' }), 'missions');
  assert.equal(habitatZoneKey({ type: 'market' }), 'markets');
  assert.equal(habitatZoneKey({ type: 'forecast' }), 'markets');
  assert.equal(habitatZoneKey({ type: 'wallet' }), 'resources');
  assert.equal(habitatZoneKey({ type: 'tool' }), 'resources');
  assert.equal(habitatZoneKey({ type: 'evidence' }), 'research');
  assert.deepEqual(HABITAT_ZONE_TYPES.resources, ['wallet', 'tool']);
});

test('layout is deterministic and contains no synthetic runtime state', () => {
  const first = habitatSlotPosition('agents', 5);
  const second = habitatSlotPosition('agents', 5);
  assert.deepEqual(first, second);
  assert.equal(typeof first.x, 'number');
  assert.equal(typeof first.y, 'number');
  assert.equal(typeof first.z, 'number');

  const deck = habitatDeckCorners('markets', 12);
  assert.equal(deck.length, 4);
  assert.ok(deck.every(point => Number.isFinite(point.x) && Number.isFinite(point.y) && Number.isFinite(point.z)));
  assert.equal('status' in HABITAT_ZONES.markets, false);
  assert.equal('balance' in HABITAT_ZONES.resources, false);
});

test('every far-view zone has a named habitat surface', () => {
  for (const key of Object.keys(HABITAT_FAR_LAYOUT)) {
    assert.ok(HABITAT_ZONES[key], key);
    assert.ok(HABITAT_ZONES[key].name.length > 3, key);
    assert.ok(HABITAT_ZONES[key].purpose.length > 3, key);
  }
});
