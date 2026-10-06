import test from 'node:test';
import assert from 'node:assert/strict';
import { workstationKind, workstationDescriptor } from '../noema/static/world-workstations.mjs';

test('canonical habitat entities map to presentation-only workstation kinds', () => {
  assert.equal(workstationKind({ type: 'mission' }), 'command');
  assert.equal(workstationKind({ type: 'provider', id: 'provider:openai' }), 'cognition');
  assert.equal(workstationKind({ type: 'provider', id: 'provider:venue:kalshi' }), 'market');
  assert.equal(workstationKind({ type: 'market' }), 'market');
  assert.equal(workstationKind({ type: 'forecast' }), 'forecast');
  assert.equal(workstationKind({ type: 'wallet' }), 'vault');
  assert.equal(workstationKind({ type: 'tool', id: 'tool:execution-gateway' }), 'gateway');
  assert.equal(workstationKind({ type: 'experiment' }), 'experiment');
  assert.equal(workstationKind({ type: 'evidence' }), 'archive');
  assert.equal(workstationKind({ type: 'agent' }), null);
});

test('workstation descriptors contain presentation metadata only', () => {
  const descriptor = workstationDescriptor({ type: 'wallet', status: 'connected' });
  assert.equal(descriptor.kind, 'vault');
  assert.equal(descriptor.label, 'Treasury vault');
  assert.equal('healthy' in descriptor, false);
  assert.equal('authorized' in descriptor, false);
  assert.equal('balance' in descriptor, false);
});
